"""Unit and integration tests for the sanitized Codex usage dashboard API and service.

Verifies:
1. Exact top-level contract: provider, fetched_at, account_id, plan_limit_history,
   daily_token_usage_breakdown.
2. Exact child contracts: {status, data, error}.
3. Allowlist sanitization: sensitive and unknown fields discarded.
4. Nullable vs 0 distinction preserved accurately.
5. Transport non-ok status and error codes preserved.
6. Schema normalization errors produce status='invalid_response', data=None, error='schema_error'
   without leaking raw payloads in responses or logs.
7. Day parameter validation bounds (1..30) and defaults (days=7).
8. Absence of raw account IDs and sensitive tokens in application logs.
9. Profile scoping integration and invalid profile handling.
10. Unchanged existing Gemini routes behavior.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.codex_usage_dashboard import get_codex_usage_dashboard
from hermes_cli.web_routers.gemini import router as gemini_router


@pytest.fixture
def standalone_client() -> TestClient:
    """Dependency-safe test client with only the gemini router mounted."""
    app = FastAPI()
    app.include_router(gemini_router)
    return TestClient(app, raise_server_exceptions=False)


def _synthetic_valid_raw_history(
    account_id: str = "acc_test_123",
    plan_limit_data: dict[str, Any] | None = None,
    daily_tokens_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan_limit_data is None:
        plan_limit_data = {
            "data_as_of": "2026-09-27T08:00:00Z",
            "coverage_start": "2026-09-20T00:00:00Z",
            "coverage_complete": True,
            "approximate": False,
            "sensitive_billing_code": "secret_code",
            "periods": [
                {
                    "starts_at": "2026-09-20T00:00:00Z",
                    "ends_at": "2026-09-20T05:00:00Z",
                    "window_minutes": 300,
                    "plan_type": "pro",
                    "used_basis_points": 1250,
                    "accounting_complete": True,
                    "internal_period_id": 9999,
                }
            ],
        }

    if daily_tokens_data is None:
        daily_tokens_data = {
            "data_freshness_ts": "2026-09-27T08:00:00Z",
            "units": "tokens",
            "cost_usd": "14.50",
            "credits": 100,
            "days": [
                {
                    "date": "2026-09-26",
                    "groups": [{"id": "g1"}],
                    "models": [
                        {
                            "model": "gpt-5",
                            "cached_text_input_tokens": 100,
                            "uncached_text_input_tokens": 200,
                            "text_output_tokens": 50,
                            "total_tokens": 350,
                            "cost_usd": "0.15",
                        }
                    ],
                }
            ],
        }

    return {
        "provider": "openai-codex",
        "fetched_at": "2026-09-27T12:00:00+00:00",
        "account_id": account_id,
        "plan_limit_history": {
            "status": "ok",
            "data": plan_limit_data,
            "error": None,
        },
        "daily_token_usage_breakdown": {
            "status": "ok",
            "data": daily_tokens_data,
            "error": None,
        },
    }


class TestCodexUsageDashboardContractAndAllowlist:
    """Verifies response structure, allowlisting, and sanitization."""

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_exact_contract_keys_and_allowlist(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
    ):
        mock_fetch.return_value = _synthetic_valid_raw_history()

        resp = standalone_client.get("/api/codex/usage-history?days=7")
        assert resp.status_code == 200
        body = resp.json()

        # EXACT top-level keys
        assert set(body.keys()) == {
            "provider",
            "fetched_at",
            "account_id",
            "plan_limit_history",
            "daily_token_usage_breakdown",
        }
        assert body["provider"] == "openai-codex"
        assert body["account_id"] == "acc_test_123"
        assert body["fetched_at"] == "2026-09-27T12:00:00+00:00"

        # EXACT child keys
        for key in ("plan_limit_history", "daily_token_usage_breakdown"):
            assert set(body[key].keys()) == {"status", "data", "error"}
            assert body[key]["status"] == "ok"
            assert body[key]["error"] is None

        # Verify plan_limit_history allowlist (sensitive fields discarded)
        plan_data = body["plan_limit_history"]["data"]
        assert "sensitive_billing_code" not in plan_data
        assert set(plan_data.keys()) == {
            "data_as_of",
            "coverage_start",
            "coverage_complete",
            "approximate",
            "periods",
        }
        period = plan_data["periods"][0]
        assert "internal_period_id" not in period
        assert set(period.keys()) == {
            "starts_at",
            "ends_at",
            "window_minutes",
            "plan_type",
            "used_basis_points",
            "accounting_complete",
        }

        # Verify daily_token_usage_breakdown allowlist (cost, credits, groups discarded)
        token_data = body["daily_token_usage_breakdown"]["data"]
        assert "cost_usd" not in token_data
        assert "credits" not in token_data
        assert set(token_data.keys()) == {"data_freshness_ts", "units", "days"}
        day = token_data["days"][0]
        assert "groups" not in day
        model = day["models"][0]
        assert "cost_usd" not in model
        assert set(model.keys()) == {
            "model",
            "cached_text_input_tokens",
            "uncached_text_input_tokens",
            "text_output_tokens",
            "total_tokens",
        }

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_nullable_vs_zero_distinction(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
    ):
        raw = _synthetic_valid_raw_history(
            plan_limit_data={
                "data_as_of": None,
                "coverage_start": None,
                "coverage_complete": None,
                "approximate": None,
                "periods": [
                    {
                        "starts_at": "2026-09-20T00:00:00Z",
                        "ends_at": "2026-09-20T05:00:00Z",
                        "window_minutes": 0,
                        "plan_type": None,
                        "used_basis_points": 0,
                        "accounting_complete": False,
                    },
                    {
                        "starts_at": "2026-09-20T05:00:00Z",
                        "ends_at": "2026-09-20T10:00:00Z",
                        "window_minutes": None,
                        "plan_type": None,
                        "used_basis_points": None,
                        "accounting_complete": None,
                    },
                ],
            },
            daily_tokens_data={
                "data_freshness_ts": None,
                "units": None,
                "days": [
                    {
                        "date": "2026-09-26",
                        "models": [
                            {
                                "model": "gpt-5-zero",
                                "cached_text_input_tokens": 0,
                                "uncached_text_input_tokens": 0,
                                "text_output_tokens": 0,
                                "total_tokens": 0,
                            },
                            {
                                "model": "gpt-5-null",
                                "cached_text_input_tokens": None,
                                "uncached_text_input_tokens": None,
                                "text_output_tokens": None,
                                "total_tokens": None,
                            },
                        ],
                    }
                ],
            },
        )
        mock_fetch.return_value = raw

        resp = standalone_client.get("/api/codex/usage-history")
        assert resp.status_code == 200
        body = resp.json()

        p_zero = body["plan_limit_history"]["data"]["periods"][0]
        assert p_zero["window_minutes"] == 0
        assert p_zero["used_basis_points"] == 0
        assert p_zero["accounting_complete"] is False

        p_null = body["plan_limit_history"]["data"]["periods"][1]
        assert p_null["window_minutes"] is None
        assert p_null["used_basis_points"] is None
        assert p_null["accounting_complete"] is None

        m_zero = body["daily_token_usage_breakdown"]["data"]["days"][0]["models"][0]
        assert m_zero["cached_text_input_tokens"] == 0
        assert m_zero["uncached_text_input_tokens"] == 0
        assert m_zero["text_output_tokens"] == 0
        assert m_zero["total_tokens"] == 0

        m_null = body["daily_token_usage_breakdown"]["data"]["days"][0]["models"][1]
        assert m_null["cached_text_input_tokens"] is None
        assert m_null["uncached_text_input_tokens"] is None
        assert m_null["text_output_tokens"] is None
        assert m_null["total_tokens"] is None


class TestCodexUsageDashboardTransportFailuresAndSchemaErrors:
    """Verifies transport error preservation and schema error sanitization."""

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_transport_unavailable_and_error_preserved(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
    ):
        mock_fetch.return_value = {
            "provider": "openai-codex",
            "fetched_at": "2026-09-27T12:00:00+00:00",
            "account_id": None,
            "plan_limit_history": {
                "status": "unavailable",
                "data": None,
                "error": "credentials_unavailable",
            },
            "daily_token_usage_breakdown": {
                "status": "error",
                "data": None,
                "error": "http_429",
            },
        }

        resp = standalone_client.get("/api/codex/usage-history")
        assert resp.status_code == 200
        body = resp.json()

        assert body["account_id"] is None
        assert body["plan_limit_history"] == {
            "status": "unavailable",
            "data": None,
            "error": "credentials_unavailable",
        }
        assert body["daily_token_usage_breakdown"] == {
            "status": "error",
            "data": None,
            "error": "http_429",
        }

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_schema_error_yields_invalid_response_without_payload_leak(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ):
        # Raw payloads containing malformed schemas and sensitive markers
        mock_fetch.return_value = {
            "provider": "openai-codex",
            "fetched_at": "2026-09-27T12:00:00+00:00",
            "account_id": "acc_private_id",
            "plan_limit_history": {
                "status": "ok",
                "data": {
                    "periods": "malformed_string_not_a_list_SECRET_LEAK_MARKER",
                },
                "error": None,
            },
            "daily_token_usage_breakdown": {
                "status": "ok",
                "data": {
                    "data": [
                        {
                            "date": "2026-09-26",
                            "models": [
                                {
                                    "model": "gpt-5",
                                    "cached_text_input_tokens": -500,  # Negative integer violates schema
                                }
                            ],
                        }
                    ]
                },
                "error": None,
            },
        }

        with caplog.at_level(logging.WARNING):
            resp = standalone_client.get("/api/codex/usage-history")

        assert resp.status_code == 200
        body = resp.json()

        # Both schema errors become invalid_response with data: None and error: schema_error
        assert body["plan_limit_history"] == {
            "status": "invalid_response",
            "data": None,
            "error": "schema_error",
        }
        assert body["daily_token_usage_breakdown"] == {
            "status": "invalid_response",
            "data": None,
            "error": "schema_error",
        }

        # Response does not leak sensitive payload marker
        assert "SECRET_LEAK_MARKER" not in resp.text

        # Logs do not leak sensitive payload marker or raw account ID
        assert "SECRET_LEAK_MARKER" not in caplog.text
        assert "acc_private_id" not in caplog.text


class TestCodexUsageDashboardParameterValidation:
    """Verifies bounds [1..30] and days default."""

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_default_days_is_seven(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
    ):
        mock_fetch.return_value = _synthetic_valid_raw_history()
        resp = standalone_client.get("/api/codex/usage-history")
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with(days=7)

    @pytest.mark.parametrize("valid_day", [1, 7, 14, 30])
    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_valid_days_accepted(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
        valid_day: int,
    ):
        mock_fetch.return_value = _synthetic_valid_raw_history()
        resp = standalone_client.get(f"/api/codex/usage-history?days={valid_day}")
        assert resp.status_code == 200
        mock_fetch.assert_called_with(days=valid_day)

    @pytest.mark.parametrize("invalid_day", [0, -1, 31, 100, "abc"])
    def test_out_of_bounds_days_rejected_by_fastapi(
        self,
        standalone_client: TestClient,
        invalid_day: Any,
    ):
        resp = standalone_client.get(f"/api/codex/usage-history?days={invalid_day}")
        assert resp.status_code == 422

    @pytest.mark.parametrize("invalid_day", [True, False, 0, 31, "7", None, 7.5])
    def test_service_level_validation(self, invalid_day: Any):
        with pytest.raises(ValueError, match="days must be an integer between 1 and 30"):
            get_codex_usage_dashboard(days=invalid_day)


class TestCodexUsageDashboardProfileScopeAndMountedBehavior:
    """Verifies profile scoping and unchanged Gemini routes."""

    @patch("hermes_cli.codex_usage_dashboard.fetch_codex_usage_history")
    def test_valid_profile_scope(
        self,
        mock_fetch: MagicMock,
        standalone_client: TestClient,
    ):
        mock_fetch.return_value = _synthetic_valid_raw_history()
        # Default or None profile scopes cleanly
        resp = standalone_client.get("/api/codex/usage-history?days=7&profile=default")
        assert resp.status_code == 200

    def test_nonexistent_profile_returns_404(
        self,
        standalone_client: TestClient,
    ):
        resp = standalone_client.get("/api/codex/usage-history?days=7&profile=nonexistent_test_profile_xyz")
        assert resp.status_code == 404

    @patch("hermes_cli.auth.list_account_events")
    def test_existing_gemini_account_history_unchanged(
        self,
        mock_events: MagicMock,
        standalone_client: TestClient,
    ):
        mock_events.return_value = {"events": [], "total": 0}
        resp = standalone_client.get("/api/gemini/account-history")
        assert resp.status_code == 200
        assert resp.json() == {"events": [], "total": 0}

    @patch("hermes_cli.auth.list_gemini_session_histories")
    def test_existing_gemini_session_histories_unchanged(
        self,
        mock_histories: MagicMock,
        standalone_client: TestClient,
    ):
        mock_histories.return_value = {"sessions": []}
        resp = standalone_client.get("/api/gemini/session-histories")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}

    @patch("hermes_cli.auth.get_gemini_quota_timeline")
    def test_existing_gemini_quota_timeline_unchanged(
        self,
        mock_timeline: MagicMock,
        standalone_client: TestClient,
    ):
        mock_timeline.return_value = {"timeline": []}
        resp = standalone_client.get("/api/gemini/quota-timeline")
        assert resp.status_code == 200
        assert resp.json() == {"timeline": []}

    def test_route_mounted_in_web_server_app(self):
        from hermes_cli import web_server
        matched = False
        for route in web_server.app.routes:
            if getattr(route, "path", None) == "/api/codex/usage-history":
                matched = True
                assert "GET" in route.methods
                break
        assert matched, "/api/codex/usage-history not found in web_server.app routes"
