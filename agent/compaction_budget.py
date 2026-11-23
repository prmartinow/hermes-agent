"""Effective compaction budget calculation and read-only previews.

ContextCompressor retains lease, transcript and recovery ownership. The mixin shares
one calculation between its live trigger and the reporting surfaces.
"""
from __future__ import annotations

from contextlib import suppress
from typing import Any, Sequence

_UNSET = object()


def compressor_max_tokens(agent):
    """``agent.max_tokens``, or the native-Gemini adapter default when unset: generateContent
    still sends maxOutputTokens=65,535 and the threshold is pct×(window − max_tokens), so
    reserving 0 let the provider 400 before compaction fired."""
    if agent.max_tokens is not None:
        return agent.max_tokens
    with suppress(Exception):
        from agent.gemini_native_adapter import (
            GEMINI_DEFAULT_MAX_OUTPUT_TOKENS, is_native_gemini_base_url
        )
        _gemini_provider = str(agent.provider or "").strip().lower() in {
            "gemini", "google", "google-gemini", "google-ai-studio",
        }
        if _gemini_provider or is_native_gemini_base_url(agent.base_url):
            return GEMINI_DEFAULT_MAX_OUTPUT_TOKENS
    return None


def _normalize_exempt_models(raw: Any) -> tuple[str, ...]:
    """Normalize user-configured exempt models/families into a tuple of lowercase strings."""
    if not raw:
        return ()
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        return tuple(parts)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(x).strip().lower() for x in raw if str(x).strip())
    if isinstance(raw, dict):
        return tuple(str(k).strip().lower() for k, v in raw.items() if v)
    return ()


def check_model_cap_exemption(
    model: str,
    exempt_configs: Sequence[str] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Match the configured case-insensitive model-name patterns."""
    name = model.strip().lower()
    if name and any(pattern in name for pattern in _normalize_exempt_models(exempt_configs)):
        return True, "model_family_exemption", "threshold_tokens_exempt_models"
    return False, None, None


class CompactionBudgetReport(dict):
    """Structured, non-mutating report of effective compaction budget and trigger derivation."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'CompactionBudgetReport' object has no attribute {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(f"'CompactionBudgetReport' object has no attribute {name!r}") from None

    def to_dict(self) -> dict:
        return dict(self)


def _configured_cap(raw: Any, context_limit: int) -> tuple[int | None, int | None]:
    """None and non-positive values leave the proportional trigger uncapped."""
    try:
        requested = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None, None
    return (requested, min(requested, context_limit)) if requested > 0 else (None, None)


def _output_reservation(max_tokens: int | None, endpoint_limit: int, model: str, provider: str) -> tuple[int, str]:
    if max_tokens is not None and max_tokens > 0:
        return int(max_tokens), "explicit_max_tokens"
    if endpoint_limit > 0:
        return endpoint_limit, "endpoint_metadata"
    if model or provider:
        from agent.model_metadata_endpoint_cache import get_model_max_output_tokens

        limit = get_model_max_output_tokens(model, provider)
        if limit > 0:
            return limit, "model_metadata"
    return 0, "none"


class CompactionBudgetMixin:
    """Budget arithmetic and previews; no transcript or session mutation."""

    def _log_updated_budget(self, model: str, context_length: int, provider: str) -> None:
        if self.quiet_mode:
            return
        from agent.context_compressor import logger

        cap_overrides, eff_cap = False, None
        try:
            report = self.get_budget_report(
                model=model, context_length=context_length, provider=provider, max_tokens=self.max_tokens,
                threshold_tokens_cap=self.threshold_tokens_cap, aux_context_ceiling=self._aux_context_ceiling,
            )
            cap_overrides = bool(report.get("cap_overrides_ratio"))
            eff_cap = report.get("effective_cap")
        except Exception:  # health: allow BLE001 -- optional diagnostics log the traceback and cannot interrupt a turn
            logger.exception("Could not calculate the updated compaction budget")
        if cap_overrides:
            logger.info(
                "Context compressor model updated: model=%s context_length=%d threshold=%d (cap %s overrides configured ratio %.0f%%) provider=%s",
                self.model, self.context_length, self.threshold_tokens,
                eff_cap, self.threshold_percent * 100, self.provider or "none",
            )
        else:
            logger.info(
                "Context compressor model updated: model=%s context_length=%d threshold=%d (%.0f%%) provider=%s",
                self.model, self.context_length, self.threshold_tokens,
                self.threshold_percent * 100, self.provider or "none",
            )

    def _cached_endpoint_output_limit(self, model: str, provider: str) -> int:
        if provider != self.provider:
            return 0
        snapshot = getattr(self, "_resolved_endpoint_output_limit", None)
        if model == self.model and snapshot is not None:
            return snapshot
        from agent.model_metadata_endpoint_cache import cached_endpoint_output_limit
        return cached_endpoint_output_limit(model, self.base_url, self.api_key)

    @classmethod
    def compute_budget_report(
        cls,
        context_length: int,
        threshold_percent: float,
        max_tokens: int | None = None,
        model: str = "",
        provider: str = "",
        threshold_tokens_cap: Any = None,
        aux_context_ceiling: int | None = None,
        base_threshold_percent: float | None = None,
        model_thresholds: dict[str, float] | None = None,
        apply_ratio_floor: bool = True,
        threshold_tokens_exempt_models: Any = None,
        threshold_tokens_exempt_families: Any = None,
        endpoint_output_limit: int = 0,
    ) -> CompactionBudgetReport:
        """Pure calculation of the structured compaction budget report.

        Uses the production threshold math and returns all intermediate derivation stages,
        including usable tokens, effective ratio, requested/effective caps, proportional
        and actual triggers, and limiting reason/source.
        """
        from agent.context_compressor import MINIMUM_CONTEXT_LENGTH, resolve_model_threshold

        context_limit = max(1, int(context_length))

        # Model threshold resolution if model and thresholds are supplied
        if model_thresholds and model:
            base_percent = resolve_model_threshold(
                model, model_thresholds,
                base_threshold_percent if base_threshold_percent is not None else threshold_percent,
                provider,
            )
        elif base_threshold_percent is not None:
            base_percent = float(base_threshold_percent)
        else:
            base_percent = float(threshold_percent)

        if apply_ratio_floor:
            effective_percent = cls._effective_threshold_percent(context_limit, base_percent)
        else:
            effective_percent = float(threshold_percent)

        output_reservation, output_reservation_source = _output_reservation(
            max_tokens, endpoint_output_limit, model, provider,
        )

        usable_input_budget = cls._effective_input_window(context_limit, output_reservation)
        pct_value = int(usable_input_budget * effective_percent)
        floored = max(pct_value, MINIMUM_CONTEXT_LENGTH)
        trigger_cap = int(usable_input_budget * cls._MIN_CTX_TRIGGER_RATIO)
        small_window_capped = False
        if usable_input_budget > 0 and floored > pct_value and floored > trigger_cap:
            floored = max(pct_value, trigger_cap)
            small_window_capped = True
        hard_input_cap = max(1, usable_input_budget - 1024)

        if usable_input_budget > 0 and floored >= usable_input_budget:
            candidate_trigger = max(
                1,
                min(
                    trigger_cap,
                    hard_input_cap,
                    usable_input_budget - 1,
                ),
            )
            if candidate_trigger == hard_input_cap:
                limiting_reason = "safety_clamp"
                limiting_source = "safety_headroom"
            elif candidate_trigger == trigger_cap:
                limiting_reason = "small_context_ceiling"
                limiting_source = "min_context_ratio"
            elif candidate_trigger == usable_input_budget - 1:
                limiting_reason = "usable_tokens_limit"
                limiting_source = "usable_tokens_ceiling"
            else:
                limiting_reason = "minimum_safe_floor"
                limiting_source = "absolute_floor"
        else:
            candidate_trigger = min(floored, hard_input_cap)
            if candidate_trigger == hard_input_cap and hard_input_cap < floored:
                limiting_reason = "safety_clamp"
                limiting_source = "safety_headroom"
            elif floored > pct_value:
                if small_window_capped and floored == trigger_cap:
                    limiting_reason = "small_context_ceiling"
                    limiting_source = "min_context_ratio"
                elif floored == MINIMUM_CONTEXT_LENGTH:
                    limiting_reason = "minimum_context_floor"
                    limiting_source = "minimum_context_floor"
                else:
                    limiting_reason = "proportional"
                    limiting_source = "model_ratio"
            else:
                limiting_reason = "proportional"
                limiting_source = "model_ratio"

        exempt_config = (
            threshold_tokens_exempt_models
            if threshold_tokens_exempt_models is not None
            else threshold_tokens_exempt_families
        )

        requested_cap, nominal_effective_cap = _configured_cap(threshold_tokens_cap, context_limit)
        effective_cap = nominal_effective_cap

        is_exempt = False
        exemption_reason = None
        config_source = None
        if requested_cap is not None and exempt_config:
            is_exempt, exemption_reason, config_source = check_model_cap_exemption(
                model=model,
                exempt_configs=exempt_config,
            )

        actual_trigger = candidate_trigger
        cap_overrides_ratio = False

        if is_exempt:
            effective_cap = None
            cap_overrides_ratio = False
        elif effective_cap is not None and effective_cap < actual_trigger:
            actual_trigger = effective_cap
            limiting_reason = "effective_cap"
            limiting_source = "threshold_tokens_cap"
            cap_overrides_ratio = True

        if aux_context_ceiling is not None and isinstance(aux_context_ceiling, int) and 0 < aux_context_ceiling < actual_trigger:
            actual_trigger = aux_context_ceiling
            limiting_reason = "aux_context_ceiling"
            limiting_source = "auxiliary_summarizer"

        return CompactionBudgetReport(
            context_limit=context_limit,
            context_length=context_limit,
            output_reservation=output_reservation,
            output_reservation_source=output_reservation_source,
            max_tokens=output_reservation,
            usable_tokens=usable_input_budget,
            usable_input_budget=usable_input_budget,
            effective_model_ratio=effective_percent,
            effective_threshold_percent=effective_percent,
            base_model_ratio=base_percent,
            base_threshold_percent=base_percent,
            configured_ratio=base_percent,
            requested_cap=requested_cap,
            requested_absolute_cap=requested_cap,
            threshold_tokens_cap=requested_cap,
            effective_cap=effective_cap,
            effective_threshold_cap=effective_cap,
            nominal_effective_cap=nominal_effective_cap,
            cap_exempt=is_exempt,
            is_cap_exempt=is_exempt,
            exemption_reason=exemption_reason,
            cap_exemption_reason=exemption_reason,
            config_source=config_source,
            cap_exemption_source=config_source,
            proportional_trigger=pct_value,
            proportional_tokens=pct_value,
            actual_trigger=actual_trigger,
            actual_threshold_tokens=actual_trigger,
            threshold_tokens=actual_trigger,
            limiting_reason=limiting_reason,
            limiting_source=limiting_source,
            cap_overrides_ratio=cap_overrides_ratio,
            safety_clamp_tokens=hard_input_cap,
            safety_headroom=1024,
            minimum_context_floor=MINIMUM_CONTEXT_LENGTH,
            small_context_ceiling=trigger_cap,
            aux_context_ceiling=aux_context_ceiling if isinstance(aux_context_ceiling, int) and aux_context_ceiling > 0 else None,
            model=model or "",
            provider=provider or "",
        )


    def get_budget_report(
        self,
        model: str | None = None,
        context_length: int | None = None,
        provider: str | None = None,
        max_tokens: Any = _UNSET,
        threshold_tokens_cap: Any = _UNSET,
        aux_context_ceiling: Any = _UNSET,
        threshold_tokens_exempt_models: Any = _UNSET,
        threshold_tokens_exempt_families: Any = _UNSET,
    ) -> CompactionBudgetReport:
        """Produce a structured non-mutating report of the compaction budget.

        Uses the same production computation as the live trigger derivation.
        Never mutates the compressor instance.
        """
        from agent.context_compressor import get_model_context_length, resolve_model_threshold

        effective_model = self.model if model is None else model
        effective_provider = self.provider if provider is None else provider

        if context_length is not None:
            effective_ctx = int(context_length)
        elif getattr(self, "_resolved_context_length", None) is not None:
            effective_ctx = self._resolved_context_length
        else:
            effective_ctx = get_model_context_length(
                effective_model,
                base_url=self.base_url,
                api_key=self.api_key,
                config_context_length=self._config_context_length,
                provider=effective_provider,
                custom_providers=self.custom_providers,
            )

        if model is not None or provider is not None:
            base_percent = resolve_model_threshold(
                effective_model,
                self.model_thresholds,
                self._config_threshold_percent,
                effective_provider,
            )
        else:
            base_percent = getattr(self, "_base_threshold_percent", self.threshold_percent)

        effective_max_tokens = self.max_tokens if max_tokens is _UNSET else self._coerce_max_tokens(max_tokens)
        effective_cap = self.threshold_tokens_cap if threshold_tokens_cap is _UNSET else self._coerce_threshold_tokens_cap(threshold_tokens_cap)
        effective_aux_ceiling = getattr(self, "_aux_context_ceiling", None) if aux_context_ceiling is _UNSET else aux_context_ceiling
        if threshold_tokens_exempt_models is not _UNSET:
            effective_exempt = threshold_tokens_exempt_models
        elif threshold_tokens_exempt_families is not _UNSET:
            effective_exempt = threshold_tokens_exempt_families
        else:
            effective_exempt = getattr(self, "threshold_tokens_exempt_models", None)

        return self.compute_budget_report(
            context_length=effective_ctx,
            threshold_percent=base_percent,
            max_tokens=effective_max_tokens,
            model=effective_model,
            provider=effective_provider,
            threshold_tokens_cap=effective_cap,
            aux_context_ceiling=effective_aux_ceiling,
            base_threshold_percent=base_percent,
            model_thresholds=self.model_thresholds,
            apply_ratio_floor=True,
            threshold_tokens_exempt_models=effective_exempt,
            endpoint_output_limit=self._cached_endpoint_output_limit(effective_model, effective_provider),
        )


    preview_budget_report = get_budget_report
    get_effective_budget = get_budget_report


    def _emit_init_summary_once(self) -> None:
        """Emit the init log line once, on first context-length resolution (keeps __init__ non-blocking)."""
        from agent.context_compressor import logger
        if not getattr(self, "_log_init_summary", False):
            return
        self._log_init_summary = False
        cap_overrides = False
        eff_cap = None
        try:
            budget_fn = getattr(self, "get_budget_report", None)
            if callable(budget_fn):
                report = budget_fn(
                    model=self.model,
                    context_length=self._resolved_context_length,
                    provider=self.provider,
                    max_tokens=self.max_tokens,
                    threshold_tokens_cap=self.threshold_tokens_cap,
                    aux_context_ceiling=self._aux_context_ceiling,
                )
                cap_overrides = bool(report.get("cap_overrides_ratio"))
                eff_cap = report.get("effective_cap")
        except Exception:  # health: allow BLE001 -- optional diagnostics log the traceback and cannot interrupt a turn
            logger.exception("Could not calculate the initial compaction budget")

        if cap_overrides:
            logger.info(
                "Context compressor initialized: model=%s context_length=%d threshold=%d (cap %s overrides configured ratio %.0f%%) "
                "target_ratio=%.0f%% tail_budget=%d provider=%s base_url=%s",
                self.model, self._resolved_context_length, self.threshold_tokens,
                eff_cap, self.threshold_percent * 100, self.summary_target_ratio * 100,
                self.tail_token_budget,
                self.provider or "none", self.base_url or "none",
            )
        else:
            logger.info(
                "Context compressor initialized: model=%s context_length=%d threshold=%d (%.0f%%) "
                "target_ratio=%.0f%% tail_budget=%d provider=%s base_url=%s",
                self.model, self._resolved_context_length, self.threshold_tokens,
                self.threshold_percent * 100, self.summary_target_ratio * 100,
                self.tail_token_budget,
                self.provider or "none", self.base_url or "none",
            )


    def _derive_trigger(self, model: str, context_length: int, provider: str) -> tuple[float, float, int]:
        """``(base_percent, effective_percent, threshold_tokens)`` for a model/window, from the raw config
        value so a switch away from an overridden model falls back correctly. Pure: the one place the
        trigger math lives, shared by ``update_model`` and the switch guard's preview so the number the
        guard quotes is the number the compressor installs (#83450). Excludes the auxiliary-summariser
        ceiling, which the feasibility probe re-derives per runtime."""
        report = self.get_budget_report(
            model=model,
            context_length=context_length,
            provider=provider,
            aux_context_ceiling=None,
        )
        return report["base_threshold_percent"], report["effective_model_ratio"], report["actual_trigger"]


    def _effective_threshold_cap(self, context_length: int) -> int | None:
        """The configured ``threshold_tokens`` cap clamped to the window; None when no cap is configured or model is exempt."""
        is_exempt, _, _ = check_model_cap_exemption(
            model=self.model,
            exempt_configs=getattr(self, "threshold_tokens_exempt_models", None),
        )
        if is_exempt:
            return None
        cap = self.threshold_tokens_cap
        return min(cap, context_length) if cap is not None and cap > 0 else None


    def _apply_threshold_tokens_cap(self) -> None:
        """Clamp threshold_tokens to the configured cap (itself clamped to the context length) and to the
        auxiliary summariser's window when the feasibility probe installed one."""
        from agent.context_compressor import logger
        cap = self._effective_threshold_cap(self.context_length)
        if cap is not None and cap < self.threshold_tokens:
            if not self.quiet_mode:
                logger.info(
                    "Compaction budget cap overrides configured ratio: cap=%d overrides ratio_trigger=%d (effective trigger=%d)",
                    cap, self.threshold_tokens, cap,
                )
            self.threshold_tokens = cap
        # Durable, so every recomputation honours it rather than a one-time assignment (#114707).
        _aux_ceiling = getattr(self, "_aux_context_ceiling", None)
        if isinstance(_aux_ceiling, int) and 0 < _aux_ceiling < self.threshold_tokens:
            if not self.quiet_mode:
                logger.info(
                    "Compaction budget auxiliary ceiling applied: aux_ceiling=%d < trigger=%d",
                    _aux_ceiling, self.threshold_tokens,
                )
            self.threshold_tokens = _aux_ceiling
