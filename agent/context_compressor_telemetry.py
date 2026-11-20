"""Content-free per-attempt compression telemetry for ContextCompressor.

Split from ``agent/context_compressor.py`` (file size cap), which imports it at module level; this module
must never import that module at module level (import cycle).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agent.model_metadata import estimate_messages_tokens_rough
from agent.model_metadata_token_memo import forget_message_token_memo

logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> int | None:
    """Best-effort integer coercion for telemetry fields."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class CompressionTelemetryMixin:
    """Per-attempt telemetry dict: seeded at attempt start, filled by each compression phase."""

    def _create_compression_telemetry_payload(
        self, *, current_tokens: int | None = None, attempt_id: str | None = None, session_id: str | None = None,
        trigger_source: str | None = None,
    ) -> dict[str, Any]:
        """Build isolated content-free telemetry without mutating the compressor."""
        seed = getattr(self, "_compression_telemetry_seed", None)
        seed = seed if isinstance(seed, dict) else {}
        attempt_id = attempt_id or seed.get("attempt_id")
        session_id = session_id or seed.get("session_id")
        trigger_source = trigger_source or seed.get("trigger_source")
        try:
            budget_report = self.get_budget_report()
        except Exception:
            logger.debug("Could not read the compression budget for telemetry", exc_info=True)
            budget_report = {}  # Observability must not block compression.
        return {
            "budget_context_limit": _safe_int(budget_report.get("context_limit")),
            "budget_output_reservation": _safe_int(budget_report.get("output_reservation")),
            "budget_usable_tokens": _safe_int(budget_report.get("usable_tokens")),
            "budget_effective_ratio": budget_report.get("effective_model_ratio"),
            "budget_requested_cap": _safe_int(budget_report.get("requested_cap")),
            "budget_effective_cap": _safe_int(budget_report.get("effective_cap")),
            "budget_proportional_trigger": _safe_int(budget_report.get("proportional_trigger")),
            "budget_actual_trigger": _safe_int(budget_report.get("actual_trigger")),
            "budget_limiting_reason": budget_report.get("limiting_reason"),
            "budget_limiting_source": budget_report.get("limiting_source"),
            "budget_cap_overrides_ratio": bool(budget_report.get("cap_overrides_ratio")),
            "event": "compression_attempt", "attempt_id": attempt_id or uuid.uuid4().hex,
            "session_id": session_id or "", "trigger_source": trigger_source or "unknown",
            "overflow_reason": seed.get("overflow_reason"),
            "main_provider": self.provider or None, "main_model": self.model or None,
            "main_context_limit": _safe_int(self.context_length),
            "current_estimated_tokens": _safe_int(current_tokens),
            "effective_threshold": _safe_int(self.threshold_tokens), "protected_head_tokens": None,
            "protected_tail_tokens": None, "middle_window_tokens": None, "prellm_skip_count": 0,
            "aux_prompt_tokens": None, "aux_prompt_chars": None, "aux_prompt_bytes": None,
            "estimated_aux_prompt_tokens": None, "aux_provider_prompt_tokens": None,
            "aux_provider_completion_tokens": None, "aux_provider_total_tokens": None,
            "aux_output_reservation": None, "aux_provider": None, "aux_model": None,
            "effective_reasoning": None, "effective_aux_context": None, "fit_margin": None,
            "chunking": None, "chunk_count": None,
            "total_duration_ms": None, "aux_call_duration_ms": None, "queue_wait_ms": None, "prompt_build_ms": None,
            "time_to_first_progress_ms": None, "summary_generation_ms": None, "commit_ms": None,
            "fallback_used": False, "commit_status": "unknown", "split_status": "unknown", "failure_class": None,
            # Lean-sampling coverage (filled by _record_summary_input_coverage; None on the legacy path).
            "summary_input_chars": None, "summary_input_sampled_chars": None, "summary_input_omitted_chars": None,
            "summary_input_record_count": None, "summary_input_sampled_record_count": None,
            "summary_input_elided_record_count": None,
            # Candidate effect is captured by the compressor, then replaced with the exact committed shape.
            "method": "none", "items_dropped": 0, "messages_before": None, "messages_after": None,
            "tokens_before": None, "tokens_after": None, "tokens_reclaimed": None, "token_count_method": None,
            "tool_results_pruned": None, "reasoning_items_pruned": None, "has_focus_topic": False,
            # True when the summary came from an opt-in prepared (background) pass (compression.prepare_ahead).
            "prepared_summary": False,
        }
    def _begin_compression_telemetry(
        self, *, current_tokens: int | None, attempt_id: str | None = None, session_id: str | None = None,
        trigger_source: str | None = None,
    ) -> dict[str, Any]:
        """Initialize content-free per-attempt compression telemetry."""
        seed = getattr(self, "_compression_telemetry_seed", None)
        seed = seed if isinstance(seed, dict) else {}
        attempt_id = attempt_id or seed.get("attempt_id")
        session_id = session_id or seed.get("session_id")
        trigger_source = trigger_source or seed.get("trigger_source")
        existing = getattr(self, "_active_compression_telemetry", None)
        if isinstance(existing, dict) and attempt_id and existing.get("attempt_id") == attempt_id:
            if current_tokens is not None and existing.get("current_estimated_tokens") is None:
                existing["current_estimated_tokens"] = _safe_int(current_tokens)
            return existing
        telemetry = self._create_compression_telemetry_payload(
            current_tokens=current_tokens,
            attempt_id=attempt_id,
            session_id=session_id,
            trigger_source=trigger_source,
        )
        self._active_compression_telemetry = self._last_compression_telemetry = telemetry
        return telemetry


    def _record_compression_regions(
        self, *, head_messages: list[dict[str, Any]], middle_messages: list[dict[str, Any]],
        tail_messages: list[dict[str, Any]],
    ) -> None:
        telemetry = getattr(self, "_active_compression_telemetry", None)
        if isinstance(telemetry, dict):
            telemetry["protected_head_tokens"] = estimate_messages_tokens_rough(head_messages)
            telemetry["middle_window_tokens"] = estimate_messages_tokens_rough(middle_messages)
            telemetry["protected_tail_tokens"] = estimate_messages_tokens_rough(tail_messages)

    def _record_aux_compression_call(
        self, *, prompt_messages: list[dict[str, Any]], max_tokens: int | None, duration_ms: int,
        aux_provider: str | None = None, aux_model: str | None = None,
        effective_aux_context: int | None = None, phase_timings: dict[str, Any] | None = None,
        response: Any = None, reasoning: Any = None,
    ) -> None:
        telemetry = getattr(self, "_active_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            return
        telemetry["aux_prompt_tokens"] = estimate_messages_tokens_rough(prompt_messages)
        # Fresh summary prompts need no memo after their one estimate.
        forget_message_token_memo(prompt_messages)
        telemetry["estimated_aux_prompt_tokens"] = telemetry["aux_prompt_tokens"]
        telemetry["aux_prompt_chars"] = sum(
            len(m.get("content", "")) for m in prompt_messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        telemetry["aux_prompt_bytes"] = sum(
            len(m.get("content", "").encode("utf-8")) for m in prompt_messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        usage = getattr(response, "usage", None) if response is not None else None
        if usage is not None:
            if isinstance(usage, dict):
                telemetry["aux_provider_prompt_tokens"] = _safe_int(usage.get("prompt_tokens"))
                telemetry["aux_provider_completion_tokens"] = _safe_int(usage.get("completion_tokens"))
                telemetry["aux_provider_total_tokens"] = _safe_int(usage.get("total_tokens"))
            else:
                telemetry["aux_provider_prompt_tokens"] = _safe_int(getattr(usage, "prompt_tokens", None))
                telemetry["aux_provider_completion_tokens"] = _safe_int(getattr(usage, "completion_tokens", None))
                telemetry["aux_provider_total_tokens"] = _safe_int(getattr(usage, "total_tokens", None))
        else:
            telemetry["aux_provider_prompt_tokens"] = None
            telemetry["aux_provider_completion_tokens"] = None
            telemetry["aux_provider_total_tokens"] = None
        telemetry["aux_output_reservation"] = _safe_int(max_tokens)
        # Route, prompt size and provider usage describe the latest call;
        # aux_call_duration_ms accumulates all calls made by this attempt.
        telemetry["aux_provider"] = aux_provider or None
        telemetry["aux_model"] = aux_model or None
        telemetry["effective_reasoning"] = None
        telemetry["effective_aux_context"] = None
        telemetry["fit_margin"] = None
        if reasoning is not None:
            _SAFE_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "minimal", "off", "none", "auto", "default"}
            if isinstance(reasoning, str) and reasoning.lower() in _SAFE_REASONING_EFFORTS:
                telemetry["effective_reasoning"] = reasoning.lower()
            elif isinstance(reasoning, dict):
                effort = reasoning.get("effort")
                if isinstance(effort, str) and effort.lower() in _SAFE_REASONING_EFFORTS:
                    telemetry["effective_reasoning"] = effort.lower()
                elif reasoning.get("enabled") is False:
                    telemetry["effective_reasoning"] = "off"
                else:
                    telemetry["effective_reasoning"] = None
            else:
                telemetry["effective_reasoning"] = None
        elif "effective_reasoning" not in telemetry:
            telemetry["effective_reasoning"] = None
        if effective_aux_context is not None:
            telemetry["effective_aux_context"] = _safe_int(effective_aux_context)
        if telemetry.get("effective_aux_context") is not None and telemetry.get("aux_prompt_tokens") is not None:
            telemetry["fit_margin"] = (telemetry["effective_aux_context"] - telemetry["aux_prompt_tokens"]
                                       - (telemetry["aux_output_reservation"] or 0))
        telemetry["aux_call_duration_ms"] = (telemetry.get("aux_call_duration_ms") or 0) + max(0, int(duration_ms))
        for key in ("queue_wait_ms", "prompt_build_ms", "time_to_first_progress_ms", "summary_generation_ms", "commit_ms"):
            if not isinstance(phase_timings, dict) or key not in phase_timings:
                continue
            value = _safe_int(phase_timings[key])
            # Wait and generation phases accumulate across retries; the rest are point readings.
            accumulate = key in {"queue_wait_ms", "summary_generation_ms"} and value is not None
            telemetry[key] = (telemetry.get(key) or 0) + value if accumulate else value


    def _note_aux_route_fallback(self, route: dict[str, str]) -> None:
        """``call_llm`` answered the summary from a fallback route after the configured one failed (dead
        endpoint, quota): record it like the compressor's own main-model fallback, so telemetry says
        ``fallback_used`` and, when the main model answered, the user is told their compression model is broken."""
        failed_model = route.get("failed_model")
        if not failed_model:
            return
        telemetry = getattr(self, "_active_compression_telemetry", None)
        if isinstance(telemetry, dict):
            telemetry["fallback_used"] = True
            telemetry["failure_class"] = telemetry.get("failure_class") or "aux_model_fallback"
        # The user warning says "summarised with your main model": only the main-agent route earns it, never a
        # fallback_chain entry that happens to share the model name on another provider.
        if str(route.get("fallback_label") or "").startswith("main-agent("):
            self._last_aux_model_failure_model = failed_model
            failure = route.get("failure") or "unknown error"
            self._last_aux_model_failure_error = failure if len(failure) <= 220 else failure[:217].rstrip() + "..."

    def _compression_method(self) -> str:
        """How the summary that replaced the middle window was produced."""
        if getattr(self, "_last_summary_fallback_used", False):
            return "deterministic_fallback"
        if getattr(self, "_last_aux_model_failure_error", None):
            return "aux_fallback_main"
        return "llm_summary"

    def _record_compression_effect(
        self, messages_before: int, compressed: list[dict[str, Any]], tokens_before: int, tool_results_pruned: int,
        reasoning_items_pruned: int,
    ) -> None:
        """Record what a finished rewrite did. ``tokens_before`` and the after count are rough message-only
        estimates (system prompt and tool schemas excluded), so they compare like for like."""
        telemetry = getattr(self, "_active_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            return
        tokens_after = estimate_messages_tokens_rough(compressed)
        telemetry.update(
            method=self._compression_method(), items_dropped=getattr(self, "_last_summary_dropped_count", 0) or 0,
            messages_before=messages_before, messages_after=len(compressed), tokens_before=tokens_before,
            tokens_after=tokens_after, tokens_reclaimed=tokens_before - tokens_after,
            token_count_method="estimate_rough", tool_results_pruned=tool_results_pruned,
            reasoning_items_pruned=reasoning_items_pruned,
        )

    def _record_committed_compression_effect(
        self, messages_before: list[dict[str, Any]], messages_after: list[dict[str, Any]],
    ) -> None:
        """Replace candidate counts with the exact transcript shape that crossed the commit boundary."""
        telemetry = getattr(self, "_active_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            return
        tokens_before = estimate_messages_tokens_rough(messages_before)
        tokens_after = estimate_messages_tokens_rough(messages_after)
        telemetry.update(
            messages_before=len(messages_before), messages_after=len(messages_after),
            tokens_before=tokens_before, tokens_after=tokens_after,
            tokens_reclaimed=tokens_before - tokens_after, token_count_method="estimate_rough",
        )
