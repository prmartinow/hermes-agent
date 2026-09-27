"""Codex usage dashboard service layer.

Provides on-demand sanitized and normalized telemetry for the dashboard web API.
Delegates to agent.codex_usage_history.fetch_codex_usage_history and normalizes
payloads using agent.codex_usage_history_schema to ensure sensitive fields are
stripped and schema violations fail safely without leaking raw payloads.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.codex_usage_history import fetch_codex_usage_history
from agent.codex_usage_history_schema import (
    normalize_plan_history,
    normalize_token_history,
)
from hermes_cli.web_server_profiles import _config_profile_scope

logger = logging.getLogger(__name__)


def _sanitize_endpoint_result(
    raw_result: Any,
    normalizer_fn: Any,
    endpoint_name: str,
) -> dict[str, Any]:
    """Sanitize and normalize an endpoint result dictionary.

    Preserves transport non-ok status and error codes.
    On schema normalization failure, returns status 'invalid_response', data None,
    and error 'schema_error' without leaking raw payload.
    """
    if not isinstance(raw_result, dict):
        return {
            "status": "invalid_response",
            "data": None,
            "error": "invalid_shape",
        }

    status = raw_result.get("status")
    if status != "ok":
        return {
            "status": status if status is not None else "unavailable",
            "data": None,
            "error": raw_result.get("error"),
        }

    raw_data = raw_result.get("data")
    try:
        normalized = normalizer_fn(raw_data)
        return {
            "status": "ok",
            "data": normalized,
            "error": None,
        }
    except Exception as exc:
        logger.warning(
            "Schema normalization failed for %s: %s",
            endpoint_name,
            type(exc).__name__,
        )
        return {
            "status": "invalid_response",
            "data": None,
            "error": "schema_error",
        }


def get_codex_usage_dashboard(
    *,
    days: int = 7,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve sanitized and normalized Codex usage telemetry for dashboard display.

    Parameters:
        days: Number of days of history to request (1..30).
        profile: Optional profile scope name for credential resolution.

    Returns:
        Exact schema:
        {
            "provider": str,
            "fetched_at": str,
            "account_id": Optional[str],
            "plan_limit_history": {
                "status": str,
                "data": Optional[dict],
                "error": Optional[str],
            },
            "daily_token_usage_breakdown": {
                "status": str,
                "data": Optional[dict],
                "error": Optional[str],
            },
        }
    """
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 30):
        raise ValueError(f"days must be an integer between 1 and 30, got {days!r}")

    with _config_profile_scope(profile):
        raw = fetch_codex_usage_history(days=days)

    if not isinstance(raw, dict):
        logger.warning("fetch_codex_usage_history returned non-dict")
        return {
            "provider": "openai-codex",
            "fetched_at": "",
            "account_id": None,
            "plan_limit_history": {
                "status": "invalid_response",
                "data": None,
                "error": "invalid_shape",
            },
            "daily_token_usage_breakdown": {
                "status": "invalid_response",
                "data": None,
                "error": "invalid_shape",
            },
        }

    plan_limit = _sanitize_endpoint_result(
        raw.get("plan_limit_history"),
        normalize_plan_history,
        "plan_limit_history",
    )
    daily_tokens = _sanitize_endpoint_result(
        raw.get("daily_token_usage_breakdown"),
        normalize_token_history,
        "daily_token_usage_breakdown",
    )

    # Note: account_id is returned as authenticated UI scoping identity,
    # but never emitted to application logs.
    return {
        "provider": raw.get("provider", "openai-codex"),
        "fetched_at": raw.get("fetched_at"),
        "account_id": raw.get("account_id"),
        "plan_limit_history": plan_limit,
        "daily_token_usage_breakdown": daily_tokens,
    }


def get_codex_quota_timeline(
    *,
    days: int = 7,
    profile: Optional[str] = None,
    account_id: Optional[str] = None,
    window_id: str = "primary",
    db_path: Optional[Any] = None,
    ref_time: Optional[float] = None,
) -> dict[str, Any]:
    """Retrieve persisted Codex quota observations, intervals, and matching activity checkpoint deltas.

    Parameters:
        days: Number of days of history to retrieve (1..30).
        profile: Optional profile scope name.
        account_id: Optional account ID to filter by.
        window_id: Quota window identifier ('primary' or 'secondary').
        db_path: Optional explicit database path.
        ref_time: Optional reference epoch timestamp for time bounding.

    Returns:
        Exact schema:
        {
            "provider": "openai-codex",
            "days": int,
            "attribution_status": "correlated_only",
            "external_usage_possible": True,
            "activity_scope": "profile_local_activity",
            "confirmed_same_account": False,
            "rows": List[dict],
            "snapshots": List[dict],
            "intervals": List[dict],
            "checkpoint_deltas": List[dict],
        }
    """
    import time
    from pathlib import Path
    from hermes_cli.codex_quota_snapshots import (
        _connect_ro_db,
        _normalize_window_id,
        _table_exists,
        derive_codex_quota_intervals,
        get_codex_quota_db_path,
        list_codex_quota_snapshots,
    )
    from hermes_cli.codex_usage_attribution import (
        ActivityCounters,
        derive_checkpoint_differences,
        get_checkpoint,
    )

    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 30):
        raise ValueError(f"days must be an integer between 1 and 30, got {days!r}")

    if not isinstance(window_id, str):
        raise ValueError("window_id must be a string")
    try:
        canon_window = _normalize_window_id(window_id)
    except ValueError:
        raise ValueError("Invalid window_id. Expected 'primary' or 'secondary'.")

    # Canonical profile scope; read missing DB should not create
    target_path = get_codex_quota_db_path(profile=profile, db_path=db_path)
    if not target_path.exists():
        return {
            "provider": "openai-codex",
            "days": days,
            "attribution_status": "correlated_only",
            "external_usage_possible": True,
            "activity_scope": "profile_local_activity",
            "confirmed_same_account": False,
            "rows": [],
            "snapshots": [],
            "intervals": [],
            "checkpoint_deltas": [],
        }

    # Open target DB read-only to verify tables exist without triggering schema creation or writes.
    has_snapshots_table = False
    has_checkpoints_table = False
    ro_conn = _connect_ro_db(target_path)
    if ro_conn is not None:
        try:
            has_snapshots_table = _table_exists(ro_conn, "codex_quota_snapshots")
            has_checkpoints_table = _table_exists(ro_conn, "codex_activity_checkpoints")
        finally:
            ro_conn.close()

    if not has_snapshots_table:
        return {
            "provider": "openai-codex",
            "days": days,
            "attribution_status": "correlated_only",
            "external_usage_possible": True,
            "activity_scope": "profile_local_activity",
            "confirmed_same_account": False,
            "rows": [],
            "snapshots": [],
            "intervals": [],
            "checkpoint_deltas": [],
        }

    now_epoch = float(ref_time) if ref_time is not None else time.time()
    since = now_epoch - (float(days) * 86400.0)
    clean_account_id = account_id.strip() if account_id and isinstance(account_id, str) and account_id.strip() else None

    records = list_codex_quota_snapshots(
        account_id=clean_account_id,
        since=since,
        until=now_epoch,
        order_desc=False,
        db_path=target_path,
        profile=profile,
    )

    # Public API allowlist: explicitly exclude raw error_message
    sanitized_rows = []
    for r in records:
        sanitized_rows.append({
            "id": r.id,
            "observation_id": r.observation_id,
            "observed_at": r.observed_at,
            "time_label": r.time_label,
            "account_id": r.account_id,
            "status": r.status,
            "error_code": r.error_code,
            "plan_type": r.plan_type,
            "primary_used_percent": r.primary_used_percent,
            "primary_reset_at": r.primary_reset_at,
            "primary_window_seconds": r.primary_window_seconds,
            "secondary_used_percent": r.secondary_used_percent,
            "secondary_reset_at": r.secondary_reset_at,
            "secondary_window_seconds": r.secondary_window_seconds,
            "created_at": r.created_at,
        })

    raw_intervals = derive_codex_quota_intervals(
        records,
        account_id=clean_account_id,
        window_id=canon_window,
        db_path=target_path,
        profile=profile,
    )

    def _find_checkpoint(acc_id: str, obs_id: Optional[str], obs_time: float) -> Optional[Any]:
        if not has_checkpoints_table:
            return None
        if obs_id:
            chk = get_checkpoint(obs_id, account_id=acc_id, db_path=target_path, profile=profile)
            if chk is not None:
                return chk
        chk_conn = _connect_ro_db(target_path)
        if chk_conn is None:
            return None
        try:
            cur = chk_conn.execute(
                "SELECT checkpoint_id FROM codex_activity_checkpoints WHERE account_id = ? AND ABS(observed_at - ?) < 0.001 LIMIT 1",
                (acc_id, float(obs_time)),
            )
            row = cur.fetchone()
            if row:
                return get_checkpoint(row["checkpoint_id"], account_id=acc_id, db_path=target_path, profile=profile)
        except Exception:
            pass
        finally:
            chk_conn.close()
        return None

    formatted_intervals = []
    checkpoint_deltas = []

    for inv in raw_intervals:
        acc = inv.account_id
        start_rec = next((r for r in records if r.account_id == acc and abs(r.observed_at - inv.start_time) < 1e-4), None)
        end_rec = next((r for r in records if r.account_id == acc and abs(r.observed_at - inv.end_time) < 1e-4), None)

        start_obs_id = start_rec.observation_id if start_rec else None
        end_obs_id = end_rec.observation_id if end_rec else None

        chk_start = _find_checkpoint(acc, start_obs_id, inv.start_time)
        chk_end = _find_checkpoint(acc, end_obs_id, inv.end_time)

        # Sanitize interval description so no raw internal error messages leak
        safe_description = inv.description
        if inv.kind == "gap":
            prev_code = start_rec.error_code if start_rec else None
            curr_code = end_rec.error_code if end_rec else None
            code_label = curr_code or prev_code or "gap"
            safe_description = f"Observation gap or collection error ({code_label})"

        # Avoid cross-account endpoint joins, gaps/reset delta unknown
        if inv.kind == "gap" or inv.status == "gap":
            delta_dict = {
                "account_id": acc,
                "start_checkpoint_id": chk_start.checkpoint_id if chk_start else start_obs_id,
                "end_checkpoint_id": chk_end.checkpoint_id if chk_end else end_obs_id,
                "start_observed_at": inv.start_time,
                "end_observed_at": inv.end_time,
                "status": "unknown",
                "has_baseline": False,
                "has_discontinuity": True,
                "attribution_status": "correlated_only",
                "external_usage_possible": True,
                "activity_scope": "profile_local_activity",
                "confirmed_same_account": False,
                "total_delta": None,
                "known_delta": ActivityCounters().to_dict(),
                "discontinuity_reasons": ["Interval is an observation gap or error; usage delta is unknown"],
                "session_deltas": [],
            }
        elif inv.kind == "reset_or_replenishment":
            delta_dict = {
                "account_id": acc,
                "start_checkpoint_id": chk_start.checkpoint_id if chk_start else start_obs_id,
                "end_checkpoint_id": chk_end.checkpoint_id if chk_end else end_obs_id,
                "start_observed_at": inv.start_time,
                "end_observed_at": inv.end_time,
                "status": "reset_or_replenishment",
                "has_baseline": chk_start is not None,
                "has_discontinuity": True,
                "attribution_status": "correlated_only",
                "external_usage_possible": True,
                "activity_scope": "profile_local_activity",
                "confirmed_same_account": False,
                "total_delta": None,
                "known_delta": ActivityCounters().to_dict(),
                "discontinuity_reasons": ["Quota reset or replenishment occurred over interval; quota delta is not regular depletion"],
                "session_deltas": [],
            }
        elif chk_end is None:
            delta_dict = {
                "account_id": acc,
                "start_checkpoint_id": chk_start.checkpoint_id if chk_start else start_obs_id,
                "end_checkpoint_id": end_obs_id,
                "start_observed_at": inv.start_time,
                "end_observed_at": inv.end_time,
                "status": "missing_checkpoint",
                "has_baseline": False,
                "has_discontinuity": True,
                "attribution_status": "correlated_only",
                "external_usage_possible": True,
                "activity_scope": "profile_local_activity",
                "confirmed_same_account": False,
                "total_delta": None,
                "known_delta": ActivityCounters().to_dict(),
                "discontinuity_reasons": ["Missing end activity checkpoint"],
                "session_deltas": [],
            }
        else:
            diff_res = derive_checkpoint_differences(
                start_endpoint=chk_start,
                end_endpoint=chk_end,
                db_path=target_path,
                profile=profile,
            )

            session_deltas = []
            for sd in diff_res.session_deltas:
                session_deltas.append({
                    "session_id": sd.session_id,
                    "model": sd.model,
                    "task": sd.task,
                    "status": sd.status,
                    "is_discontinuity": sd.is_discontinuity,
                    "has_baseline": sd.has_baseline,
                    "activity_scope": "profile_local_activity",
                    "confirmed_same_account": False,
                    "baseline_counters": sd.baseline_counters.to_dict() if sd.baseline_counters else None,
                    "current_counters": sd.current_counters.to_dict() if sd.current_counters else None,
                    "delta_counters": sd.delta_counters.to_dict() if sd.delta_counters else None,
                    "reason": sd.reason,
                })

            delta_dict = {
                "account_id": diff_res.account_id,
                "start_checkpoint_id": diff_res.start_checkpoint_id,
                "end_checkpoint_id": diff_res.end_checkpoint_id,
                "start_observed_at": diff_res.start_observed_at,
                "end_observed_at": diff_res.end_observed_at,
                "status": diff_res.status,
                "has_baseline": diff_res.has_baseline,
                "has_discontinuity": diff_res.has_discontinuity,
                "attribution_status": "correlated_only",
                "external_usage_possible": True,
                "activity_scope": "profile_local_activity",
                "confirmed_same_account": False,
                "total_delta": diff_res.total_delta.to_dict() if diff_res.total_delta else None,
                "known_delta": diff_res.known_delta.to_dict() if diff_res.known_delta else ActivityCounters().to_dict(),
                "discontinuity_reasons": list(diff_res.discontinuity_reasons),
                "session_deltas": session_deltas,
            }

        inv_dict = {
            "account_id": inv.account_id,
            "window_id": inv.window_id,
            "start_time": inv.start_time,
            "end_time": inv.end_time,
            "duration_seconds": inv.duration_seconds,
            "start_used_percent": inv.start_used_percent,
            "end_used_percent": inv.end_used_percent,
            "delta_used_percent": inv.delta_used_percent,
            "start_reset_at": inv.start_reset_at,
            "end_reset_at": inv.end_reset_at,
            "reset_at_changed": inv.reset_at_changed,
            "kind": inv.kind,
            "status": inv.status,
            "description": safe_description,
            "checkpoint_delta": delta_dict,
        }
        formatted_intervals.append(inv_dict)
        checkpoint_deltas.append(delta_dict)

    return {
        "provider": "openai-codex",
        "days": days,
        "attribution_status": "correlated_only",
        "external_usage_possible": True,
        "activity_scope": "profile_local_activity",
        "confirmed_same_account": False,
        "rows": sanitized_rows,
        "snapshots": sanitized_rows,
        "intervals": formatted_intervals,
        "checkpoint_deltas": checkpoint_deltas,
    }
