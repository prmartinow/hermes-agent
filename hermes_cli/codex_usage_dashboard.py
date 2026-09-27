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
