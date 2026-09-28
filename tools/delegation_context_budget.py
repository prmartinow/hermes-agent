"""Topical helper for delegation context budget calculation and initial request preflight.

Invariants:
- Strict validation of per-task inherit_max_tokens overrides.
- Non-mutating preflight preview of initial child request tokens against context window,
  output reservation, and compression trigger.
- Refuses model-window/output-reservation overflow and immediate compression triggers.
- Isolated tasks (inherit_context=False) remain untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from agent.model_metadata import (
    estimate_request_tokens_rough,
    get_model_context_length,
)

logger = logging.getLogger("tools.delegation_context_budget")


def calculate_task_inheritance_budget(
    task_override: Optional[int],
    *,
    child_model: Optional[str] = None,
    child_base_url: Optional[str] = None,
    child_api_key: Optional[str] = None,
    child_provider: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    """Calculate the configured ceiling, effective budget, child context window, and reserve.

    Returns:
        (configured_ceiling, effective_budget, child_context_window, reserve)
    """
    from tools.delegate_tool_config import _get_inherit_max_tokens

    child_context_window = get_model_context_length(
        model=child_model or "",
        base_url=child_base_url or "",
        api_key=child_api_key or "",
        provider=child_provider or "",
    )

    reserve = min(child_context_window, max(2048, int(child_context_window * 0.25)))
    window_available = max(0, child_context_window - reserve)

    configured_ceiling = task_override if task_override is not None else _get_inherit_max_tokens()
    effective_budget = min(configured_ceiling, window_available)

    return configured_ceiling, effective_budget, child_context_window, reserve


def preflight_child_initial_request(
    task_index: int,
    task_dict: Dict[str, Any],
    child: Any,
    parent_agent: Any = None,
) -> Optional[str]:
    """Preflight check for a constructed child before execution begins.

    Checks:
    1. Complete initial child request budget:
       inherited transcript + goal/context/schema + real system prompt/tools + output reservation.
    2. Model-window / output budget overflow:
       initial request tokens + output reservation > context limit.
    3. Non-mutating preview of effective child compression trigger:
       if initial request tokens >= actual_trigger, fails closed to prevent immediate
       compaction from destroying the full inherited snapshot baseline.

    Isolated tasks (without inherited context) return None (unchanged behavior).
    Returns an actionable error string on failure, or None on success.
    """
    if not task_dict.get("inherit_context"):
        return None

    snapshot = getattr(child, "_inherited_context_snapshot", None)
    rendered_transcript = getattr(snapshot, "rendered_transcript", None)
    if not isinstance(rendered_transcript, str):
        return f"Task {task_index} requires an inherited snapshot but none is available; fails closed."

    # 1. Assemble the initial user turn exactly as SubagentRun / run_conversation will
    goal = task_dict.get("goal", "")
    effective_goal = f"{rendered_transcript}\n\n=== DELEGATED TASK ===\n{goal}"

    images = list(getattr(child, "_delegate_images", None) or [])
    if images:
        from tools.delegate_tool_child_run import _build_child_goal_message
        user_message = _build_child_goal_message(effective_goal, images, child)
    else:
        user_message = effective_goal

    prefill = list(getattr(child, "prefill_messages", None) or [])
    messages: List[Dict[str, Any]] = prefill + [{"role": "user", "content": user_message}]

    # Task/context/schema instructions are appended at API time, outside the
    # cached base prompt. Include both in the initial-request estimate.
    system_prompt = getattr(child, "_cached_system_prompt", None)
    if not system_prompt:
        try:
            system_prompt = child._build_system_prompt()
        except Exception as exc:
            return f"Task {task_index} cannot preview its system prompt ({type(exc).__name__}); fails closed."
    ephemeral = getattr(child, "ephemeral_system_prompt", None) or ""
    if not isinstance(system_prompt, str) or not isinstance(ephemeral, str):
        return f"Task {task_index} cannot preview its complete system prompt; fails closed."
    effective_system = (system_prompt + "\n\n" + ephemeral).strip()
    tools = getattr(child, "tools", None) or []
    # Count the system message envelope too, matching assembled API messages.
    request_messages = ([{"role": "system", "content": effective_system}] if effective_system else []) + messages
    request_tokens = estimate_request_tokens_rough(request_messages, tools=tools)

    # threshold_tokens is a lazy, MUTATING property. The report is the
    # production-supported non-mutating preview; never replace it with guesses.
    cc = getattr(child, "context_compressor", None)
    try:
        report = cc.get_budget_report()
    except Exception as exc:
        return f"Task {task_index} cannot preview its compression budget ({type(exc).__name__}); fails closed."
    required = ("context_limit", "output_reservation", "usable_tokens", "actual_trigger")
    if not isinstance(report, dict) or any(type(report.get(k)) is not int for k in required):
        return f"Task {task_index} has an incomplete compression budget report; fails closed."
    context_limit, output_reservation, usable_tokens, actual_trigger = (report[k] for k in required)
    if context_limit <= 0 or min(output_reservation, usable_tokens, actual_trigger) < 0:
        return f"Task {task_index} has an invalid compression budget report; fails closed."
    c_model = str(getattr(child, "model", "") or "")

    # Worker-thread runtime hints (cwd/timezone) settle after this preview.
    # Reuse the compressor safety margin for that late-bound framing.
    framing_reserve = max(0, int(report.get("safety_headroom", 1024)))
    guarded_tokens = request_tokens + framing_reserve

    # 6. Check model window / output reservation overflow
    if (guarded_tokens + output_reservation > context_limit) or (guarded_tokens > usable_tokens):
        return (
            f"Task {task_index} initial request (~{request_tokens:,} tokens plus {framing_reserve:,} framing reserve) with output reservation "
            f"({output_reservation:,} tokens) exceeds context window ({context_limit:,} tokens; "
            f"usable input budget {usable_tokens:,} tokens for model {c_model!r}). Fails closed."
        )

    # 7. Check immediate compression trigger
    if guarded_tokens >= actual_trigger:
        return (
            f"Task {task_index} initial request (~{request_tokens:,} tokens plus {framing_reserve:,} framing reserve) reaches or exceeds "
            f"child compression trigger ({actual_trigger:,} tokens for model {c_model!r}; "
            f"context window {context_limit:,}, output reservation {output_reservation:,}). "
            f"Full inherited snapshot cannot be preserved without immediate compaction; fails closed."
        )

    manifest = getattr(child, "_inherited_context_manifest", None)
    if isinstance(manifest, dict):
        child._inherited_context_manifest = dict(manifest, preflight={
            "estimated_initial_input_tokens": request_tokens,
            "request_framing_reserve": framing_reserve,
            "guarded_initial_input_tokens": guarded_tokens,
            "output_reservation": output_reservation,
            "context_limit": context_limit,
            "usable_input_tokens": usable_tokens,
            "compression_trigger": actual_trigger,
            "estimate_only": True,
        })
    return None


def preflight_children_budget(
    children: List[Tuple[int, Dict[str, Any], Any]],
    parent_agent: Any = None,
) -> Optional[str]:
    """Run initial request preflight on all constructed children.

    Returns the first failure error string, or None if all pass.
    """
    for item in children:
        task_index = item[0]
        task_dict = item[1]
        child = item[2]
        err = preflight_child_initial_request(task_index, task_dict, child, parent_agent=parent_agent)
        if err:
            return err
    return None
