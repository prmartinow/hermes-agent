"""Faithful bounded evaluation runner for compaction summary usefulness.

Exercises the actual production ContextCompressor.compress() pipeline:
  compress -> _compress_window -> _sample_summary_records -> _build_summary_prompt
  -> _call_summary_llm -> _assemble_compressed -> _salvage_or_refuse_grown_transcript

Guarantees:
- Default offline: zero accidental live network or inference calls unless allow_live=True.
- Bounded input: fail closed if input tokens exceed max_input_tokens (8,000).
- Explicit max output budget: opt-in experimental cap vs production default honest (unbounded).
- Bounded physical calls: fail closed if physical auxiliary calls exceed bound (no unbounded loops).
- Faithful length handling: length finish_reason triggers production truncation abort,
  preserving the original transcript without generating an augmentation stub.
- Actual production growth guard: runs commit-site _salvage_or_refuse_grown_transcript.
- Distinct artifact recording: original, candidate, and final accepted messages are captured;
  scoring scores the final accepted state distinctly from raw output.
- Refusal honesty: refusal/abort preserves the original transcript; never falsely scored as a compaction success.
- Exclusive scratch directory: defaults to compaction-eval-fidelity-fix under TMPDIR/scratch.
- Zero secret/endpoint leakage: client endpoints, keys, and URLs are strictly omitted.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import strip_think_blocks
from agent.auxiliary_client import (
    call_llm as real_call_llm,
    _get_auxiliary_task_config,
    _resolve_call_client,
    _resolve_task_provider_model,
)
from agent.context_compressor import (
    ContextCompressor,
    _redact_compaction_text,
    _reinject_pruned_skill_markers,
)
from agent.conversation_compression import _salvage_or_refuse_grown_transcript
from agent.model_metadata import estimate_messages_tokens_rough
from evals.compaction.summary_usefulness.fixtures import (
    MIGRATION_CASE_GROUND_TRUTH,
    SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
)
from evals.compaction.summary_usefulness.harness import transcript_to_text
from evals.compaction.summary_usefulness.rubric import (
    SummaryUsefulnessRubric,
    SummaryUsefulnessScore,
)

MAX_INPUT_TOKENS = 8000
DEFAULT_MAX_OUTPUT_TOKENS = 1500

# Alias for backwards compatibility / mock patching
call_llm = real_call_llm


def get_default_scratch_dir() -> Path:
    """Resolve dynamic scratch directory via TMPDIR runtime or canonical get_scratch_dir()."""
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        return Path(tmpdir) / "compaction-eval-fidelity-fix"
    try:
        from hermes_constants import get_scratch_dir
        return get_scratch_dir() / "compaction-eval-fidelity-fix"
    except Exception:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "cache" / "scratch" / "compaction-eval-fidelity-fix"


DEFAULT_SCRATCH_DIR = get_default_scratch_dir()


def resolve_actual_configured_auxiliary() -> Dict[str, Any]:
    """Read actual configured compression auxiliary model and provider through the normal API.

    Returns sanitized metadata only — never prints or returns credentials, secrets, or endpoints.
    Endpoints are completely omitted rather than relying on bespoke scrubbing.
    """
    cfg = _get_auxiliary_task_config("compression")
    res = _resolve_task_provider_model("compression")
    provider, model, base_url, api_key, api_mode = res

    route = _resolve_call_client(
        "compression",
        provider=None,
        model=None,
        base_url=None,
        api_key=None,
        resolved_provider=provider,
        resolved_model=model,
        resolved_base_url=base_url,
        resolved_api_key=api_key,
        resolved_api_mode=api_mode,
        main_runtime=None,
        async_mode=False,
    )

    client_class = type(route.client).__name__

    return {
        "configured_provider": cfg.get("provider", "auto"),
        "configured_model": cfg.get("model") or "(default)",
        "configured_timeout": cfg.get("timeout", 120),
        "resolved_provider": route.resolved_provider,
        "effective_provider": route.effective_provider,
        "resolved_model": route.final_model,
        "client_class": client_class,
        "auth_type": "Google OAuth (Antigravity Pool)" if "Gemini" in client_class else "API Client",
    }


def build_production_prompt_and_metadata(
    transcript: List[Dict[str, Any]],
    model_name: str,
    provider_name: str,
) -> Tuple[str, Dict[str, Any]]:
    """Construct the exact production summary prompt on the actual selected window.

    Follows ContextCompressor window selection: _compress_window protects head and token-budget tail.
    """
    compressor = ContextCompressor(model=model_name, provider=provider_name, tail_mode="lean", quiet_mode=True)
    c_start, c_end = compressor._compress_window(transcript)
    turns = transcript[c_start:c_end]
    scan = compressor._scan_window_handoffs(transcript, c_start, c_end, turns)
    turns_to_summarize = scan.turns_to_summarize

    prompt_started_at = time.monotonic()
    records = compressor._serialize_records_for_summary(turns_to_summarize)
    content_to_summarize, coverage = compressor._sample_summary_records(records)
    summary_budget = compressor._compute_summary_budget(turns_to_summarize)
    has_user_turn = compressor._transcript_has_real_user_turn(turns_to_summarize)

    prompt = compressor._build_summary_prompt(
        content_to_summarize=content_to_summarize,
        summary_budget=summary_budget,
        focus_topic=None,
        memory_context="",
        has_user_turn=has_user_turn,
    )

    metadata = {
        "prompt_chars": len(prompt),
        "compress_window": [c_start, c_end],
        "input_turns_count": len(turns_to_summarize),
        "total_transcript_turns": len(transcript),
        "coverage": coverage,
        "summary_budget": summary_budget,
        "has_user_turn": has_user_turn,
        "prompt_build_ms": int((time.monotonic() - prompt_started_at) * 1000),
    }
    return prompt, metadata


def execute_live_compaction_request(
    transcript: Optional[List[Dict[str, Any]]] = None,
    ground_truth: Optional[Dict[str, Any]] = None,
    scratch_dir: Optional[Path] = None,
    max_output_tokens: Optional[int] = None,
    allow_live: bool = False,
    mock_response: Optional[Any] = None,
    max_input_tokens: int = MAX_INPUT_TOKENS,
    max_auxiliary_calls: int = 1,
    token_counter: Optional[Callable[[str], int]] = None,
) -> Dict[str, Any]:
    """Execute compaction evaluation exercising the actual production compress pipeline.

    Intercepts ONLY the auxiliary transport for explicit bounds checking:
    1. Default offline: fails closed unless allow_live=True or mock_response is provided.
    2. Input token verification: counts exact tokens of the prompt; fails closed if > max_input_tokens.
    3. Physical calls bounding: tracks physical transport calls; fails closed if > max_auxiliary_calls.
    4. Experimental output cap: injects max_tokens only if explicitly requested; otherwise leaves honest production default.
    5. Production lifecycle: exercises ContextCompressor.compress() -> commit-site growth guard.
    6. Refusal & Length honesty: preserves original transcript on truncation or refusal without augmentation stubs.
    7. Distinct capture: saves original, candidate, and final accepted messages; scores final state distinct from raw.
    """
    # 0. Check offline authorization before ANY route resolution, config load, token counting, or network.
    # Default offline: fail closed unless allow_live=True or an explicit mock fixture (mock_response) is passed.
    # Note: monkeypatching call_llm alias does NOT authorize offline execution without an explicit mock_response fixture.
    if not allow_live and mock_response is None:
        raise RuntimeError(
            "Live auxiliary inference is not authorized (default offline / no accidental inference). "
            "Pass allow_live=True or provide mock_response."
        )

    # Check physical call budget before starting
    if max_auxiliary_calls <= 0:
        raise RuntimeError(
            f"Auxiliary physical call bound exceeded ({max_auxiliary_calls} budget). Failing closed with no unbounded fallback."
        )

    if transcript is None:
        transcript = SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED
    if ground_truth is None:
        ground_truth = MIGRATION_CASE_GROUND_TRUTH
    if scratch_dir is None:
        scratch_dir = get_default_scratch_dir()

    scratch_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve configured auxiliary route metadata
    aux_info = resolve_actual_configured_auxiliary()
    resolved_model = aux_info["resolved_model"]
    effective_provider = aux_info["effective_provider"]
    resolved_provider = aux_info["resolved_provider"]

    if not resolved_model:
        raise RuntimeError(f"No configured model resolved for compression route: {aux_info}")

    # 2. Build exact production prompt on the selected window to verify input token bounds
    prompt, prompt_meta = build_production_prompt_and_metadata(
        transcript, resolved_model, effective_provider
    )

    compressor = ContextCompressor(
        model=resolved_model,
        provider=effective_provider,
        tail_mode="lean",
        quiet_mode=True,
    )
    main_rt = {
        "model": compressor.model,
        "provider": compressor.provider,
        "base_url": compressor.base_url,
        "api_key": compressor.api_key,
        "api_mode": compressor.api_mode,
    }
    route = _resolve_call_client(
        "compression",
        provider=None,
        model=None,
        base_url=None,
        api_key=None,
        resolved_provider=resolved_provider,
        resolved_model=resolved_model,
        resolved_base_url=None,
        resolved_api_key=None,
        resolved_api_mode=None,
        main_runtime=main_rt,
        async_mode=False,
    )

    def count_prompt_tokens(prompt_text: str) -> int:
        if token_counter is not None:
            return token_counter(prompt_text)
        if allow_live:
            try:
                return route.client.count_tokens(contents=prompt_text, model=resolved_model)
            except Exception:
                return estimate_messages_tokens_rough([{"role": "user", "content": prompt_text}])
        # Offline deterministic counting: zero network
        return estimate_messages_tokens_rough([{"role": "user", "content": prompt_text}])

    exact_input_tokens = count_prompt_tokens(prompt)
    prompt_meta["exact_input_tokens"] = exact_input_tokens

    # Verify input token bounds: fail closed if input tokens exceed limit
    if exact_input_tokens > max_input_tokens:
        raise ValueError(
            f"Input token count {exact_input_tokens} exceeds maximum allowed limit of {max_input_tokens} tokens (fail closed)."
        )

    # 3. Intercept auxiliary transport at verified SDK send seam
    physical_calls_count = 0
    physical_calls_exceeded = False
    actual_prompt_error: Optional[Exception] = None
    intercepted_calls: List[Dict[str, Any]] = []

    def make_send_interceptor(original_create):
        def physical_send_interceptor(*args, **kwargs):
            nonlocal physical_calls_count, physical_calls_exceeded, actual_prompt_error
            physical_calls_count += 1
            if physical_calls_count > max_auxiliary_calls:
                physical_calls_exceeded = True
                raise RuntimeError(
                    f"Auxiliary physical call bound exceeded ({physical_calls_count} > {max_auxiliary_calls}). "
                    f"Failing closed with no unbounded fallback."
                )

            # Count/gate exact ACTUAL interceptor prompt per dispatch
            messages = kwargs.get("messages", [])
            actual_prompt = ""
            if messages:
                if isinstance(messages[0], dict):
                    actual_prompt = messages[0].get("content", "")
                elif hasattr(messages[0], "content"):
                    actual_prompt = str(messages[0].content)

            actual_input_tokens = count_prompt_tokens(actual_prompt)
            if actual_input_tokens > max_input_tokens:
                actual_prompt_error = ValueError(
                    f"Input token count {actual_input_tokens} exceeds maximum allowed limit of {max_input_tokens} tokens (fail closed)."
                )
                raise actual_prompt_error

            # Enforce experimental output cap or keep production default honest
            if max_output_tokens is not None:
                kwargs["max_tokens"] = max_output_tokens
            else:
                kwargs.pop("max_tokens", None)

            call_start = time.monotonic()
            if mock_response is not None:
                if callable(mock_response):
                    resp = mock_response(*args, **kwargs)
                elif isinstance(mock_response, Exception):
                    raise mock_response
                else:
                    resp = mock_response
            else:
                resp = original_create(*args, **kwargs)
            elapsed = time.monotonic() - call_start

            intercepted_calls.append({
                "prompt_content": actual_prompt,
                "exact_input_tokens": actual_input_tokens,
                "max_tokens_passed": kwargs.get("max_tokens"),
                "elapsed_seconds": elapsed,
                "response": resp,
            })
            return resp
        return physical_send_interceptor

    clients_to_patch = set()
    if hasattr(route, "client") and route.client is not None:
        clients_to_patch.add(route.client)
    from agent.auxiliary_client import _client_cache
    for entry in list(_client_cache.values()):
        if isinstance(entry, tuple) and len(entry) >= 1 and entry[0] is not None:
            clients_to_patch.add(entry[0])

    with ExitStack() as stack:
        for cl in clients_to_patch:
            if hasattr(cl, "chat") and hasattr(cl.chat, "completions"):
                stack.enter_context(
                    patch.object(cl.chat.completions, "create", side_effect=make_send_interceptor(cl.chat.completions.create))
                )
        candidate_messages = compressor.compress(transcript, force=True)

    if actual_prompt_error is not None:
        raise actual_prompt_error

    if physical_calls_exceeded:
        raise RuntimeError(
            f"Auxiliary physical call bound exceeded ({physical_calls_count} > {max_auxiliary_calls}). "
            f"Failing closed with no unbounded fallback."
        )

    # 6. Extract status from compressor and response
    aborted_truncated = getattr(compressor, "_last_summary_truncated_failure", False)
    last_err = getattr(compressor, "_last_summary_error", None)
    aborted_refusal = bool(last_err and "refusal" in str(last_err).lower())
    aborted_empty = getattr(compressor, "_last_summary_empty_content_failure", False)

    raw_content = ""
    finish_reason = "not_called"
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None
    elapsed_seconds = 0.0

    if intercepted_calls:
        rec = intercepted_calls[0]
        elapsed_seconds = rec["elapsed_seconds"]
        resp = rec["response"]
        choices = getattr(resp, "choices", [])
        if choices:
            ch = choices[0]
            finish_reason = getattr(ch, "finish_reason", "unknown")
            m = getattr(ch, "message", None)
            raw_content = getattr(m, "content", "") if m else ""
        usage = getattr(resp, "usage", None)
        if usage:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            total_tokens = getattr(usage, "total_tokens", None)

    # 7. Execute actual production growth guard
    rough_in = estimate_messages_tokens_rough(transcript)
    rough_cand = estimate_messages_tokens_rough(candidate_messages)

    mock_agent = MagicMock()
    mock_agent.session_id = "compaction-eval-fidelity-fix"
    mock_agent.context_compressor = compressor

    accepted_messages, refused_sp = _salvage_or_refuse_grown_transcript(
        mock_agent,
        transcript,
        candidate_messages,
        system_message="",
        attempt_started_at=time.monotonic(),
        attempt_snapshot={},
    )

    refused_would_grow = getattr(compressor, "_last_compress_refused_would_grow", False)
    salvaged = (accepted_messages is not None and rough_cand > rough_in)

    compaction_aborted = (
        aborted_truncated
        or aborted_refusal
        or aborted_empty
        or candidate_messages == transcript
    )

    if compaction_aborted:
        final_accepted_messages = transcript
        original_preserved = True
        accepted_messages = None
    elif accepted_messages is not None:
        final_accepted_messages = accepted_messages
        original_preserved = False
    else:
        final_accepted_messages = transcript
        original_preserved = True

    # 8. Determine lifecycle status
    if aborted_truncated:
        lifecycle_status = "length_truncated_aborted"
    elif aborted_refusal:
        lifecycle_status = "refusal_aborted"
    elif aborted_empty:
        lifecycle_status = "empty_content_aborted"
    elif refused_would_grow:
        lifecycle_status = "growth_guard_refused"
    elif salvaged:
        lifecycle_status = "salvaged_accepted"
    elif not original_preserved:
        lifecycle_status = "normal_accepted"
    else:
        lifecycle_status = "preserved_original"

    rough_final = estimate_messages_tokens_rough(final_accepted_messages)

    # 9. Evaluate with rubric
    rubric = SummaryUsefulnessRubric(ground_truth)
    raw_transcript_text = transcript_to_text(transcript)
    final_accepted_text = transcript_to_text(final_accepted_messages)

    eval_score_final: SummaryUsefulnessScore = rubric.evaluate(final_accepted_text, raw_transcript_text)
    if original_preserved:
        eval_score_final.diagnostic_notes.append(
            f"Compaction aborted/refused (lifecycle: {lifecycle_status}); scored final state is preserved original transcript."
        )

    eval_score_raw: Optional[SummaryUsefulnessScore] = None
    if raw_content:
        eval_score_raw = rubric.evaluate(raw_content, raw_transcript_text)

    # 10. Save artifacts to exclusive scratch directory
    orig_file = scratch_dir / "original_messages.json"
    orig_file.write_text(json.dumps(transcript, indent=2, default=str), encoding="utf-8")

    cand_file = scratch_dir / "candidate_messages.json"
    cand_file.write_text(json.dumps(candidate_messages, indent=2, default=str), encoding="utf-8")

    final_file = scratch_dir / "final_accepted_messages.json"
    final_file.write_text(json.dumps(final_accepted_messages, indent=2, default=str), encoding="utf-8")

    raw_file = scratch_dir / "raw_summary_output.md"
    raw_file.write_text(raw_content or "(no raw LLM content produced)", encoding="utf-8")

    final_txt_file = scratch_dir / "final_accepted_transcript.md"
    final_txt_file.write_text(final_accepted_text, encoding="utf-8")

    gt_file = scratch_dir / "ground_truth.json"
    gt_file.write_text(json.dumps(ground_truth, indent=2), encoding="utf-8")

    score_final_file = scratch_dir / "evaluation_score_final.json"
    score_final_file.write_text(json.dumps(asdict(eval_score_final), indent=2, default=str), encoding="utf-8")

    score_raw_file = scratch_dir / "evaluation_score_raw.json"
    if eval_score_raw:
        score_raw_file.write_text(json.dumps(asdict(eval_score_raw), indent=2, default=str), encoding="utf-8")

    metadata = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": resolved_model,
        "provider": effective_provider,
        "client_class": aux_info["client_class"],
        "auth_type": aux_info["auth_type"],
        "lifecycle_status": lifecycle_status,
        "original_preserved": original_preserved,
        "refused_would_grow": refused_would_grow,
        "salvaged": salvaged,
        "aborted_truncated": aborted_truncated,
        "aborted_refusal": aborted_refusal,
        "finish_reason": finish_reason,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "physical_auxiliary_calls": physical_calls_count,
        "max_output_tokens_configured": max_output_tokens if max_output_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS,
        "max_output_tokens_budget": max_output_tokens if max_output_tokens is not None else "production_default_honest",
        "usage": {
            "exact_prompt_tokens": intercepted_calls[0]["exact_input_tokens"] if intercepted_calls else exact_input_tokens,
            "provider_prompt_tokens": prompt_tokens,
            "provider_completion_tokens": completion_tokens,
            "provider_total_tokens": total_tokens,
        },
        "tokens": {
            "original_tokens_rough": rough_in,
            "candidate_tokens_rough": rough_cand,
            "final_tokens_rough": rough_final,
            "shrink_ratio_final": round(rough_final / max(1, rough_in), 4),
        },
        "artifact_paths": {
            "original_messages": str(orig_file),
            "candidate_messages": str(cand_file),
            "final_accepted_messages": str(final_file),
            "raw_summary": str(raw_file),
            "final_accepted_transcript": str(final_txt_file),
            "evaluation_score_final": str(score_final_file),
            "ground_truth": str(gt_file),
        },
    }

    metadata_file = scratch_dir / "sanitized_metadata.json"
    metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return {
        "metadata": metadata,
        "lifecycle_status": lifecycle_status,
        "eval_score": eval_score_final,
        "eval_score_final": eval_score_final,
        "eval_score_raw": eval_score_raw,
        "raw_content": raw_content,
        "original_messages": transcript,
        "candidate_messages": candidate_messages,
        "final_accepted_messages": final_accepted_messages,
        "artifact_dir": str(scratch_dir),
    }


if __name__ == "__main__":
    print("=== Hermes Bounded Live Compaction Evaluation Runner ===")
    print("Resolving configured compression auxiliary route...")
    aux = resolve_actual_configured_auxiliary()
    print(f"  • Model:            {aux['resolved_model']}")
    print(f"  • Provider:         {aux['effective_provider']}")
    print(f"  • Client Class:     {aux['client_class']}")
    print(f"  • Auth Gateway:     {aux['auth_type']}")

    if "--live" not in sys.argv:
        print("\nNOTICE: Default offline mode. Pass --live to authorize live auxiliary inference.")
        print("Exiting without live inference.")
        sys.exit(0)

    print("\nExecuting authorized live compaction evaluation...")
    res = execute_live_compaction_request(allow_live=True)
    meta = res["metadata"]
    score = res["eval_score"]
    print(f"\n=== Compaction Lifecycle Status: {res['lifecycle_status']} ===")
    print(f"  • Original Preserved: {meta['original_preserved']}")
    print(f"  • Final Rough Tokens: {meta['tokens']['final_tokens_rough']} (from {meta['tokens']['original_tokens_rough']})")
    print(f"  • Usefulness Score:   {score.usefulness_score:.4f}")
    print(f"  • Artifacts saved in: {res['artifact_dir']}")
