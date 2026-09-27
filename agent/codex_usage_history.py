"""Codex analytics and telemetry transport layer.

Provides raw transport for fetching historical quota analytics and daily token usage breakdowns
from the Codex backend without normalization, filtering, or persistence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Optional

import httpx

from agent.account_usage import (
    _codex_backend_urls,
    _codex_headers,
    _get_json,
    _resolve_codex_usage_credentials,
)

logger = logging.getLogger(__name__)


def fetch_codex_usage_history(
    *,
    days: int = 7,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch raw Codex usage telemetry endpoints for the resolved account identity.

    Queries:
      - {usage_url}/plan_limit_history?days={days}
      - {usage_url}/daily-token-usage-breakdown?start_date={start_date}&end_date={end_date}&group_by=day

    Validates integer days between 1 and 30 (rejects boolean values). Dates are computed in UTC.
    Refreshes credentials at most once upon 401 across the entire fetch; fails if account identity
    changes during refresh, stopping remaining requests.

    Returns:
        dict containing provider, fetched_at, account_id, plan_limit_history, and
        daily_token_usage_breakdown results.
    """
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 30):
        raise ValueError(f"days must be an integer between 1 and 30, got {days!r}")

    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    end_date = now.date().isoformat()
    start_date = (now.date() - timedelta(days=days)).isoformat()

    try:
        token, resolved_base_url, cred_account_id = _resolve_codex_usage_credentials(base_url, api_key)
    except Exception:
        logger.debug("Failed resolving initial Codex credentials")
        unavailable_endpoint = {
            "status": "unavailable",
            "data": None,
            "error": "credentials_unavailable",
        }
        return {
            "provider": "openai-codex",
            "fetched_at": fetched_at,
            "account_id": None,
            "plan_limit_history": unavailable_endpoint.copy(),
            "daily_token_usage_breakdown": unavailable_endpoint.copy(),
        }

    headers = _codex_headers(token, cred_account_id)
    pinned_account_id = cred_account_id or headers.get("ChatGPT-Account-ID")
    if not pinned_account_id:
        logger.debug("Codex account identity missing; marking telemetry unavailable")
        missing_identity_endpoint = {
            "status": "unavailable",
            "data": None,
            "error": "missing_account_identity",
        }
        return {
            "provider": "openai-codex",
            "fetched_at": fetched_at,
            "account_id": None,
            "plan_limit_history": missing_identity_endpoint.copy(),
            "daily_token_usage_breakdown": missing_identity_endpoint.copy(),
        }

    current_token = token
    current_base_url = resolved_base_url
    current_headers = headers
    current_usage_url = _codex_backend_urls(current_base_url)[0]
    refreshed = False
    account_mismatch_occurred = False

    def _fetch_endpoint_url(endpoint_path_with_query: str) -> dict[str, Any]:
        nonlocal current_token, current_base_url, current_headers, current_usage_url, refreshed, account_mismatch_occurred

        if account_mismatch_occurred:
            return {"status": "error", "data": None, "error": "account_mismatch"}

        while True:
            target_url = f"{current_usage_url}{endpoint_path_with_query}"
            try:
                payload = _get_json(target_url, current_headers, timeout=15.0)
                if payload is None:
                    return {"status": "unavailable", "data": None, "error": "null_response"}
                if not isinstance(payload, dict):
                    return {"status": "invalid_response", "data": None, "error": "invalid_shape"}
                return {"status": "ok", "data": payload, "error": None}
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code == 401 and not refreshed:
                    refreshed = True
                    try:
                        new_token, new_base_url, new_cred_account_id = _resolve_codex_usage_credentials(
                            base_url, api_key, force_refresh=True
                        )
                        new_headers = _codex_headers(new_token, new_cred_account_id)
                        new_account_id = new_cred_account_id or new_headers.get("ChatGPT-Account-ID")
                        if new_account_id != pinned_account_id:
                            logger.warning("Codex account identity changed upon 401 refresh")
                            account_mismatch_occurred = True
                            return {"status": "error", "data": None, "error": "account_mismatch"}

                        current_token = new_token
                        current_base_url = new_base_url
                        current_headers = new_headers
                        current_usage_url = _codex_backend_urls(current_base_url)[0]
                        continue
                    except Exception:
                        logger.debug("Failed refreshing Codex credentials after 401")
                        return {"status": "error", "data": None, "error": "refresh_failed"}

                if status_code in (401, 403, 404):
                    return {"status": "unavailable", "data": None, "error": f"http_{status_code}"}
                elif status_code == 429:
                    return {"status": "error", "data": None, "error": "http_429"}
                elif status_code >= 500:
                    return {"status": "error", "data": None, "error": f"http_{status_code}"}
                return {"status": "error", "data": None, "error": f"http_{status_code}"}
            except (ValueError, json.JSONDecodeError):
                return {"status": "invalid_response", "data": None, "error": "malformed_json"}
            except httpx.TimeoutException:
                return {"status": "error", "data": None, "error": "timeout"}
            except httpx.RequestError:
                return {"status": "error", "data": None, "error": "transport_error"}
            except Exception:
                return {"status": "error", "data": None, "error": "transport_error"}

    plan_limit_result = _fetch_endpoint_url(f"/plan_limit_history?days={days}")
    daily_tokens_result = _fetch_endpoint_url(
        f"/daily-token-usage-breakdown?start_date={start_date}&end_date={end_date}&group_by=day"
    )

    return {
        "provider": "openai-codex",
        "fetched_at": fetched_at,
        "account_id": pinned_account_id,
        "plan_limit_history": plan_limit_result,
        "daily_token_usage_breakdown": daily_tokens_result,
    }
