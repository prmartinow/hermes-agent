"""Codex usage and plan limit history normalization schemas.

Provides strict normalization and sanitization functions for:
- Daily token usage breakdown (DailyProductSurfaceUsageResponse)
- 7-day plan limit history (PlanLimitHistory)

Security and validation guarantees:
- Sensitive fields (cost_usd, credits, spend_control, attribution, internal IDs) are discarded.
- Extra keys are discarded; unknown nulls are preserved.
- Types are strictly validated: booleans are never accepted as numeric, numbers must be finite and non-negative.
- List lengths bounded to <= 10000 entries with explicit ValueError.
- Consumption usage groups are not supported and are omitted.
- Models absent or null in daily usage normalize to empty list [] (not evidence of zero usage).
- Null or missing token counts remain None.
"""

from __future__ import annotations

import math
from typing import Any

MAX_LIST_BOUND = 10000


def _validate_bool(val: Any, name: str, allow_none: bool = True) -> bool | None:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if not isinstance(val, bool):
        raise ValueError(f"{name} must be a boolean, got {type(val).__name__}")
    return val


def _validate_str(val: Any, name: str, allow_none: bool = True) -> str | None:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if not isinstance(val, str):
        raise ValueError(f"{name} must be a string, got {type(val).__name__}")
    return val


def _validate_nonnegative_int(val: Any, name: str, allow_none: bool = True) -> int | None:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    # Reject booleans as numeric (in Python bool is a subclass of int)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be an integer, got {type(val).__name__}")
    if not math.isfinite(val) or val < 0:
        raise ValueError(f"{name} must be finite and non-negative, got {val}")
    if isinstance(val, float):
        if not val.is_integer():
            raise ValueError(f"{name} must be an integer, got {val}")
        return int(val)
    return val


def _validate_nonnegative_number(val: Any, name: str, allow_none: bool = True) -> float | int | None:
    if val is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(val).__name__}")
    if not math.isfinite(val) or val < 0:
        raise ValueError(f"{name} must be finite and non-negative, got {val}")
    return val


def normalize_token_history(payload: Any) -> dict[str, Any]:
    """Normalize daily token usage history payload.

    Converts raw DailyProductSurfaceUsageResponse into a sanitized dict:
    {
        "data_freshness_ts": Optional[str],
        "units": Optional[str],
        "days": [
            {
                "date": str,
                "models": [
                    {
                        "model": str,
                        "cached_text_input_tokens": Optional[int],
                        "uncached_text_input_tokens": Optional[int],
                        "text_output_tokens": Optional[int],
                        "total_tokens": Optional[int],
                    }
                ]
            }
        ]
    }

    Note on group support:
    Consumption usage groups (groups) are omitted for now; no group support is provided.
    Missing or null models list defaults to empty list [] (not evidence of zero usage).
    Missing or null token counts stay None.
    Extra keys (e.g. cost_usd, credits, groups, attribution) are discarded.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Payload must be a dictionary, got {type(payload).__name__}")

    data_freshness_ts = _validate_str(payload.get("data_freshness_ts"), "data_freshness_ts", allow_none=True)
    units = _validate_str(payload.get("units"), "units", allow_none=True)

    raw_days = payload.get("data") if "data" in payload else payload.get("days")
    if raw_days is None:
        raise ValueError("Payload missing required 'data' or 'days' field")
    if not isinstance(raw_days, list):
        raise ValueError(f"Days must be a list, got {type(raw_days).__name__}")
    if len(raw_days) > MAX_LIST_BOUND:
        raise ValueError(f"Days list exceeds maximum bound of {MAX_LIST_BOUND} rows (got {len(raw_days)})")

    normalized_days: list[dict[str, Any]] = []
    for idx, day in enumerate(raw_days):
        if not isinstance(day, dict):
            raise ValueError(f"Day entry at index {idx} must be a dictionary")

        date_val = day.get("date")
        if not isinstance(date_val, str):
            raise ValueError(f"Day entry at index {idx} missing valid 'date' string, got {type(date_val).__name__}")

        raw_models = day.get("models")
        if raw_models is None:
            # Models absent or null -> [] (not evidence of zero)
            normalized_models: list[dict[str, Any]] = []
        elif not isinstance(raw_models, list):
            raise ValueError(f"Day entry at index {idx} 'models' must be a list or null, got {type(raw_models).__name__}")
        else:
            if len(raw_models) > MAX_LIST_BOUND:
                raise ValueError(
                    f"Models list at index {idx} exceeds maximum bound of {MAX_LIST_BOUND} (got {len(raw_models)})"
                )
            normalized_models = []
            for m_idx, m in enumerate(raw_models):
                if not isinstance(m, dict):
                    raise ValueError(f"Model at day {idx} index {m_idx} must be a dictionary")

                model_name = m.get("model")
                if not isinstance(model_name, str):
                    raise ValueError(f"Model entry at day {idx} index {m_idx} must have 'model' string")

                cached_tokens = _validate_nonnegative_int(
                    m.get("cached_text_input_tokens"),
                    f"day[{idx}].models[{m_idx}].cached_text_input_tokens",
                    allow_none=True,
                )
                uncached_tokens = _validate_nonnegative_int(
                    m.get("uncached_text_input_tokens"),
                    f"day[{idx}].models[{m_idx}].uncached_text_input_tokens",
                    allow_none=True,
                )
                output_tokens = _validate_nonnegative_int(
                    m.get("text_output_tokens"),
                    f"day[{idx}].models[{m_idx}].text_output_tokens",
                    allow_none=True,
                )
                total_tokens = _validate_nonnegative_int(
                    m.get("total_tokens"),
                    f"day[{idx}].models[{m_idx}].total_tokens",
                    allow_none=True,
                )

                normalized_models.append({
                    "model": model_name,
                    "cached_text_input_tokens": cached_tokens,
                    "uncached_text_input_tokens": uncached_tokens,
                    "text_output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                })

        normalized_days.append({
            "date": date_val,
            "models": normalized_models,
        })

    return {
        "data_freshness_ts": data_freshness_ts,
        "units": units,
        "days": normalized_days,
    }


def normalize_plan_history(payload: Any) -> dict[str, Any]:
    """Normalize 7-day plan limit history payload.

    Returns allowlisted dictionary:
    {
        "data_as_of": Optional[str],
        "coverage_start": Optional[str],
        "coverage_complete": Optional[bool],
        "approximate": Optional[bool],
        "periods": [
            {
                "starts_at": str,
                "ends_at": str,
                "window_minutes": Optional[int],
                "plan_type": Optional[str],
                "used_basis_points": Optional[float | int],
                "accounting_complete": Optional[bool],
            }
        ]
    }

    Note on breakdowns:
    Breakdown dimensions are omitted initially.
    Extra keys (e.g. boundary_tolerance_seconds, id, breakdowns) are discarded.
    Unknown nulls are preserved.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Payload must be a dictionary, got {type(payload).__name__}")

    data_as_of = _validate_str(payload.get("data_as_of"), "data_as_of", allow_none=True)
    coverage_start = _validate_str(payload.get("coverage_start"), "coverage_start", allow_none=True)
    coverage_complete = _validate_bool(payload.get("coverage_complete"), "coverage_complete", allow_none=True)
    approximate = _validate_bool(payload.get("approximate"), "approximate", allow_none=True)

    raw_periods = payload.get("periods")
    if raw_periods is None:
        raise ValueError("Payload missing required 'periods' field")
    if not isinstance(raw_periods, list):
        raise ValueError(f"Periods must be a list, got {type(raw_periods).__name__}")
    if len(raw_periods) > MAX_LIST_BOUND:
        raise ValueError(f"Periods list exceeds maximum bound of {MAX_LIST_BOUND} rows (got {len(raw_periods)})")

    normalized_periods: list[dict[str, Any]] = []
    for idx, p in enumerate(raw_periods):
        if not isinstance(p, dict):
            raise ValueError(f"Period at index {idx} must be a dictionary")

        starts_at = p.get("starts_at")
        if not isinstance(starts_at, str):
            raise ValueError(f"Period at index {idx} missing valid 'starts_at' string, got {type(starts_at).__name__}")

        ends_at = p.get("ends_at")
        if not isinstance(ends_at, str):
            raise ValueError(f"Period at index {idx} missing valid 'ends_at' string, got {type(ends_at).__name__}")

        window_minutes = _validate_nonnegative_int(
            p.get("window_minutes"),
            f"periods[{idx}].window_minutes",
            allow_none=True,
        )
        plan_type = _validate_str(p.get("plan_type"), f"periods[{idx}].plan_type", allow_none=True)
        used_basis_points = _validate_nonnegative_number(
            p.get("used_basis_points"),
            f"periods[{idx}].used_basis_points",
            allow_none=True,
        )
        accounting_complete = _validate_bool(
            p.get("accounting_complete"),
            f"periods[{idx}].accounting_complete",
            allow_none=True,
        )

        normalized_periods.append({
            "starts_at": starts_at,
            "ends_at": ends_at,
            "window_minutes": window_minutes,
            "plan_type": plan_type,
            "used_basis_points": used_basis_points,
            "accounting_complete": accounting_complete,
        })

    return {
        "data_as_of": data_as_of,
        "coverage_start": coverage_start,
        "coverage_complete": coverage_complete,
        "approximate": approximate,
        "periods": normalized_periods,
    }
