"""Unit tests for Codex usage analytics and telemetry transport layer.

Verifies:
1. Days validation (1..30 integer, rejects boolean and non-integers).
2. Resolved account identity enforcement and graceful missing-identity handling.
3. Accurate URL construction and UTC date formatting for daily token breakdowns.
4. Single 401 retry per fetch operation.
5. Immediate termination of subsequent requests upon account mismatch on 401 refresh.
6. Error code stability for 403, 404, 429, 5xx, timeouts, and transport errors.
7. Null payload (unavailable) and malformed JSON / non-dict responses (invalid_response).
8. Absence of account IDs and raw credential exception objects in logs.
"""

from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from agent.codex_usage_history import fetch_codex_usage_history


def _make_http_status_error(status_code: int, url: str = "https://test.local") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status_code=status_code, request=request)
    return httpx.HTTPStatusError(message=f"HTTP {status_code}", request=request, response=response)


class TestCodexUsageHistoryValidation:
    """Validation of parameter inputs."""

    @pytest.mark.parametrize("invalid_days", [True, False, 0, -1, 31, 100, 7.5, "7", None, []])
    def test_invalid_days_rejected(self, invalid_days: Any):
        with pytest.raises(ValueError, match="days must be an integer between 1 and 30"):
            fetch_codex_usage_history(days=invalid_days)

    @pytest.mark.parametrize("valid_days", [1, 7, 14, 30])
    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_valid_days_accepted(self, mock_get_json, mock_headers, mock_creds, valid_days: int):
        mock_creds.return_value = ("test-token", "https://chatgpt.com/backend-api", "acc-123")
        mock_headers.return_value = {"Authorization": "Bearer test-token", "ChatGPT-Account-ID": "acc-123"}
        mock_get_json.return_value = {"ok": True}

        res = fetch_codex_usage_history(days=valid_days)
        assert res["provider"] == "openai-codex"
        assert res["account_id"] == "acc-123"
        assert res["plan_limit_history"]["status"] == "ok"
        assert res["daily_token_usage_breakdown"]["status"] == "ok"


class TestCodexUsageHistoryIdentityAndCredentials:
    """Identity resolution and security guardrails."""

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    def test_credentials_resolution_failure(self, mock_creds, caplog):
        mock_creds.side_effect = RuntimeError("sensitive-secret-token-failed")

        with caplog.at_level(logging.DEBUG):
            result = fetch_codex_usage_history()

        assert result["provider"] == "openai-codex"
        assert result["account_id"] is None
        assert result["plan_limit_history"] == {
            "status": "unavailable",
            "data": None,
            "error": "credentials_unavailable",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "unavailable",
            "data": None,
            "error": "credentials_unavailable",
        }
        # Verify no raw credential exception or tokens leaked to logs
        assert "sensitive-secret-token-failed" not in caplog.text

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    def test_missing_account_identity(self, mock_headers, mock_creds, caplog):
        mock_creds.return_value = ("token-without-acc", "https://chatgpt.com/backend-api", None)
        mock_headers.return_value = {"Authorization": "Bearer token-without-acc"}

        with caplog.at_level(logging.DEBUG):
            result = fetch_codex_usage_history()

        assert result["account_id"] is None
        assert result["plan_limit_history"] == {
            "status": "unavailable",
            "data": None,
            "error": "missing_account_identity",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "unavailable",
            "data": None,
            "error": "missing_account_identity",
        }


class TestCodexUsageHistoryTransport:
    """Mocked HTTP transport, endpoint URLs, and parameter verification."""

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_successful_fetch_urls_and_utc_dates(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("live-token", "https://chatgpt.com/backend-api", "acc-target-789")
        mock_headers.return_value = {
            "Authorization": "Bearer live-token",
            "ChatGPT-Account-ID": "acc-target-789",
        }
        plan_data = {"coverage_complete": True, "periods": []}
        token_data = {"metrics": []}
        mock_get_json.side_effect = [plan_data, token_data]

        fixed_now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        with patch("agent.codex_usage_history.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            result = fetch_codex_usage_history(days=7)

        # Expected URLs
        expected_start = "2026-09-19"
        expected_end = "2026-09-26"
        expected_plan_url = "https://chatgpt.com/backend-api/wham/usage/plan_limit_history?days=7"
        expected_tokens_url = (
            f"https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown"
            f"?start_date={expected_start}&end_date={expected_end}&group_by=day"
        )

        assert mock_get_json.call_count == 2
        calls = mock_get_json.call_args_list
        assert calls[0][0][0] == expected_plan_url
        assert calls[1][0][0] == expected_tokens_url

        # Check return structure: NO 'endpoints' key, exact top-level fields
        assert set(result.keys()) == {
            "provider",
            "fetched_at",
            "account_id",
            "plan_limit_history",
            "daily_token_usage_breakdown",
        }
        assert result["provider"] == "openai-codex"
        assert result["account_id"] == "acc-target-789"
        assert result["plan_limit_history"] == {"status": "ok", "data": plan_data, "error": None}
        assert result["daily_token_usage_breakdown"] == {"status": "ok", "data": token_data, "error": None}

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_401_single_refresh_success(self, mock_get_json, mock_headers, mock_creds):
        # Initial resolution
        mock_creds.side_effect = [
            ("initial-token", "https://chatgpt.com/backend-api", "acc-111"),
            ("refreshed-token", "https://chatgpt.com/backend-api", "acc-111"),
        ]
        mock_headers.side_effect = [
            {"Authorization": "Bearer initial-token", "ChatGPT-Account-ID": "acc-111"},
            {"Authorization": "Bearer refreshed-token", "ChatGPT-Account-ID": "acc-111"},
        ]
        # Request sequence: 1st endpoint 401 -> 1st endpoint retry ok -> 2nd endpoint ok
        mock_get_json.side_effect = [
            _make_http_status_error(401),
            {"periods": [1]},
            {"breakdown": [2]},
        ]

        result = fetch_codex_usage_history(days=7)

        assert mock_creds.call_count == 2
        # Second resolution must have force_refresh=True
        assert mock_creds.call_args_list[1] == call(None, None, force_refresh=True)

        assert result["plan_limit_history"]["status"] == "ok"
        assert result["plan_limit_history"]["data"] == {"periods": [1]}
        assert result["daily_token_usage_breakdown"]["status"] == "ok"
        assert result["daily_token_usage_breakdown"]["data"] == {"breakdown": [2]}

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_401_account_mismatch_stops_remaining_requests(
        self, mock_get_json, mock_headers, mock_creds, caplog
    ):
        mock_creds.side_effect = [
            ("initial-token", "https://chatgpt.com/backend-api", "acc-pinned"),
            ("other-token", "https://chatgpt.com/backend-api", "acc-other-secret"),
        ]
        mock_headers.side_effect = [
            {"Authorization": "Bearer initial-token", "ChatGPT-Account-ID": "acc-pinned"},
            {"Authorization": "Bearer other-token", "ChatGPT-Account-ID": "acc-other-secret"},
        ]
        # 1st request 401
        mock_get_json.side_effect = [_make_http_status_error(401)]

        with caplog.at_level(logging.WARNING):
            result = fetch_codex_usage_history(days=7)

        # Mismatch must return error on plan_limit_history and stop further network calls
        assert mock_get_json.call_count == 1
        assert result["plan_limit_history"] == {
            "status": "error",
            "data": None,
            "error": "account_mismatch",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "error",
            "data": None,
            "error": "account_mismatch",
        }
        # Account IDs must NOT appear in log messages
        assert "acc-pinned" not in caplog.text
        assert "acc-other-secret" not in caplog.text

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_401_refresh_failure(self, mock_get_json, mock_headers, mock_creds, caplog):
        mock_creds.side_effect = [
            ("token-1", "https://chatgpt.com/backend-api", "acc-1"),
            RuntimeError("secret-oauth-refresh-exception"),
        ]
        mock_headers.return_value = {"Authorization": "Bearer token-1", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.side_effect = [_make_http_status_error(401), {"ok": True}]

        with caplog.at_level(logging.DEBUG):
            result = fetch_codex_usage_history(days=7)

        assert result["plan_limit_history"] == {
            "status": "error",
            "data": None,
            "error": "refresh_failed",
        }
        assert "secret-oauth-refresh-exception" not in caplog.text

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_at_most_one_refresh_across_entire_fetch(self, mock_get_json, mock_headers, mock_creds):
        # Refresh succeeds on 1st request, but 2nd request also gets 401
        mock_creds.side_effect = [
            ("token-1", "https://chatgpt.com/backend-api", "acc-1"),
            ("token-2", "https://chatgpt.com/backend-api", "acc-1"),
        ]
        mock_headers.side_effect = [
            {"Authorization": "Bearer token-1", "ChatGPT-Account-ID": "acc-1"},
            {"Authorization": "Bearer token-2", "ChatGPT-Account-ID": "acc-1"},
        ]
        mock_get_json.side_effect = [
            _make_http_status_error(401),  # 1st call fails
            {"period": "success"},        # 1st call retry succeeds
            _make_http_status_error(401),  # 2nd call fails with 401
        ]

        result = fetch_codex_usage_history(days=7)

        # Refresh must happen at most ONCE (total 2 credential calls)
        assert mock_creds.call_count == 2
        assert result["plan_limit_history"]["status"] == "ok"
        # 2nd call cannot refresh again, returns unavailable with http_401
        assert result["daily_token_usage_breakdown"] == {
            "status": "unavailable",
            "data": None,
            "error": "http_401",
        }

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_unsupported_error_codes_403_and_404(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("token", "https://chatgpt.com/backend-api", "acc-1")
        mock_headers.return_value = {"Authorization": "Bearer token", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.side_effect = [
            _make_http_status_error(403),
            _make_http_status_error(404),
        ]

        result = fetch_codex_usage_history(days=7)
        assert result["plan_limit_history"] == {
            "status": "unavailable",
            "data": None,
            "error": "http_403",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "unavailable",
            "data": None,
            "error": "http_404",
        }

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_error_status_codes_429_and_500(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("token", "https://chatgpt.com/backend-api", "acc-1")
        mock_headers.return_value = {"Authorization": "Bearer token", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.side_effect = [
            _make_http_status_error(429),
            _make_http_status_error(503),
        ]

        result = fetch_codex_usage_history(days=7)
        assert result["plan_limit_history"] == {
            "status": "error",
            "data": None,
            "error": "http_429",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "error",
            "data": None,
            "error": "http_503",
        }

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_transport_and_timeout_errors(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("token", "https://chatgpt.com/backend-api", "acc-1")
        mock_headers.return_value = {"Authorization": "Bearer token", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.side_effect = [
            httpx.TimeoutException("Read timed out"),
            httpx.ConnectError("Connection refused"),
        ]

        result = fetch_codex_usage_history(days=7)
        assert result["plan_limit_history"] == {
            "status": "error",
            "data": None,
            "error": "timeout",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "error",
            "data": None,
            "error": "transport_error",
        }

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_null_payload_unavailable(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("token", "https://chatgpt.com/backend-api", "acc-1")
        mock_headers.return_value = {"Authorization": "Bearer token", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.return_value = None

        result = fetch_codex_usage_history(days=7)
        assert result["plan_limit_history"] == {
            "status": "unavailable",
            "data": None,
            "error": "null_response",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "unavailable",
            "data": None,
            "error": "null_response",
        }

    @patch("agent.codex_usage_history._resolve_codex_usage_credentials")
    @patch("agent.codex_usage_history._codex_headers")
    @patch("agent.codex_usage_history._get_json")
    def test_malformed_json_and_invalid_object_shape(self, mock_get_json, mock_headers, mock_creds):
        mock_creds.return_value = ("token", "https://chatgpt.com/backend-api", "acc-1")
        mock_headers.return_value = {"Authorization": "Bearer token", "ChatGPT-Account-ID": "acc-1"}
        mock_get_json.side_effect = [
            json.JSONDecodeError("Expecting value", "bad json", 0),
            ["not", "a", "dict"],
        ]

        result = fetch_codex_usage_history(days=7)
        assert result["plan_limit_history"] == {
            "status": "invalid_response",
            "data": None,
            "error": "malformed_json",
        }
        assert result["daily_token_usage_breakdown"] == {
            "status": "invalid_response",
            "data": None,
            "error": "invalid_shape",
        }
