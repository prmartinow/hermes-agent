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


class TestCodexQuotaTimeline:
    """Tests for GET /api/codex/quota-timeline endpoint and service."""

    def test_quota_timeline_missing_db_does_not_create(self, tmp_path, standalone_client: TestClient):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline

        missing_home = tmp_path / "does_not_exist"
        token = set_hermes_home_override(missing_home)
        try:
            missing_db = missing_home / "state.db"
            assert not missing_db.exists()

            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200
            assert not missing_db.exists()

            data = resp.json()
            assert data["provider"] == "openai-codex"
            assert data["days"] == 7
            assert data["attribution_status"] == "correlated_only"
            assert data["external_usage_possible"] is True
            assert data["activity_scope"] == "profile_local_activity"
            assert data["confirmed_same_account"] is False
            assert data["rows"] == []
            assert data["snapshots"] == []
            assert data["intervals"] == []
            assert data["checkpoint_deltas"] == []

            # Client query param cannot inject filesystem db (query param is ignored/removed)
            resp_inject = standalone_client.get(f"/api/codex/quota-timeline?days=7&db_path={missing_db}")
            assert resp_inject.status_code == 200
            assert not missing_db.exists()

            # Internal test injection remains supported directly at service layer
            internal_res = get_codex_quota_timeline(days=7, db_path=missing_db)
            assert internal_res["rows"] == []
            assert not missing_db.exists()
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_parameter_validation_bounds(self, standalone_client: TestClient):
        from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline

        # HTTP query param validation: days must be 1..30
        resp_zero = standalone_client.get("/api/codex/quota-timeline?days=0")
        assert resp_zero.status_code == 422

        resp_over = standalone_client.get("/api/codex/quota-timeline?days=31")
        assert resp_over.status_code == 422

        resp_invalid = standalone_client.get("/api/codex/quota-timeline?days=abc")
        assert resp_invalid.status_code == 422

        # HTTP window_id validation
        resp_bad_window = standalone_client.get("/api/codex/quota-timeline?window_id=invalid_window")
        assert resp_bad_window.status_code == 422

        resp_valid_window = standalone_client.get("/api/codex/quota-timeline?window_id=secondary")
        assert resp_valid_window.status_code == 200

        # Profile authorization / canonical resolution at route level
        resp_nonexistent = standalone_client.get("/api/codex/quota-timeline?profile=nonexistent_test_profile_xyz")
        assert resp_nonexistent.status_code == 404

        resp_invalid_profile = standalone_client.get("/api/codex/quota-timeline?profile=../bad")
        assert resp_invalid_profile.status_code == 400

        # Service-level validation
        with pytest.raises(ValueError):
            get_codex_quota_timeline(days=0)
        with pytest.raises(ValueError):
            get_codex_quota_timeline(days=31)
        with pytest.raises(ValueError):
            get_codex_quota_timeline(days=True)
        with pytest.raises(ValueError):
            get_codex_quota_timeline(window_id="invalid")
        with pytest.raises(ValueError):
            get_codex_quota_timeline(window_id=123)

    def test_quota_timeline_existing_db_read_only_no_writes(self, tmp_path, standalone_client: TestClient):
        """Ensure existing DB is accessed read-only: no schema creation or writes."""
        import os
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            db_file.touch()
            # Read-only permissions on disk
            os.chmod(db_file, 0o444)
            try:
                resp = standalone_client.get("/api/codex/quota-timeline?days=7")
                assert resp.status_code == 200
                data = resp.json()
                assert data["rows"] == []
                assert data["intervals"] == []
            finally:
                os.chmod(db_file, 0o644)
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_public_api_allowlist_no_raw_error_message(self, tmp_path, standalone_client: TestClient):
        import time
        from hermes_cli.codex_quota_snapshots import record_codex_quota_snapshot
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            now = time.time()

            leak_message = "FATAL_LEAK_SECRET_TOKEN_DO_NOT_EXPOSE"
            record_codex_quota_snapshot({
                "account_id": "acc_leak_test",
                "observed_at": now - 200,
                "status": "error",
                "error_code": "TIMEOUT",
                "error_message": leak_message,
            }, db_path=db_file)

            record_codex_quota_snapshot({
                "account_id": "acc_leak_test",
                "observed_at": now - 100,
                "status": "ok",
                "primary_used_percent": 20.0,
            }, db_path=db_file)

            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200

            text = resp.text
            assert leak_message not in text

            data = resp.json()
            assert len(data["rows"]) == 2
            for row in data["rows"]:
                assert "error_message" not in row
            for inv in data["intervals"]:
                assert leak_message not in inv["description"]
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_end_to_end_mocked_collector_to_route_correct_deltas(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, standalone_client: TestClient
    ):
        """End-to-end test: collector mocked HTTP -> checkpoint -> route -> correct deltas (not lifetime totals)."""
        import sqlite3
        import time
        from hermes_cli.codex_quota_collector import collect_once
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            conn = sqlite3.connect(str(db_file))
            conn.execute("""
                CREATE TABLE session_model_usage (
                    session_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    billing_provider TEXT NOT NULL DEFAULT '',
                    billing_base_url TEXT NOT NULL DEFAULT '',
                    billing_mode TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL DEFAULT '',
                    api_call_count INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
                )
            """)
            # Step 1: Initial activity at t0 for both sessions
            conn.execute("""
                INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count, input_tokens, output_tokens)
                VALUES ('sess-1', 'gpt-5.4', 'openai-codex', 5, 1000, 200)
            """)
            conn.execute("""
                INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count, input_tokens, output_tokens)
                VALUES ('sess-2', 'gpt-5.4', 'openai-codex', 1, 200, 50)
            """)
            conn.commit()
            conn.close()

            account_id = "acc_e2e_test"
            t0 = time.time() - 300
            monkeypatch.setattr(
                "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
                lambda base_url, api_key: ("token-1", "https://api.openai.com", account_id),
            )
            monkeypatch.setattr("time.time", lambda: t0)
            payload_t0 = {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {"used_percent": 10.0, "reset_at": 1779846359, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 25.0, "reset_at": "2026-06-01T00:00:00Z", "limit_window_seconds": 604800},
                },
            }
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: payload_t0)

            rec0 = collect_once(db_path=db_file)
            assert rec0 is not None

            # Step 2: More activity accumulates in session_model_usage
            conn = sqlite3.connect(str(db_file))
            # sess-1 increments from (5 calls, 1000 in, 200 out) to (12 calls, 2500 in, 500 out) -> delta +7, +1500, +300
            conn.execute("""
                UPDATE session_model_usage
                SET api_call_count = 12, input_tokens = 2500, output_tokens = 500
                WHERE session_id = 'sess-1'
            """)
            # sess-2 increments from (1 call, 200 in, 50 out) to (4 calls, 1000 in, 200 out) -> delta +3, +800, +150
            conn.execute("""
                UPDATE session_model_usage
                SET api_call_count = 4, input_tokens = 1000, output_tokens = 200
                WHERE session_id = 'sess-2'
            """)
            conn.commit()
            conn.close()

            t1 = t0 + 120
            monkeypatch.setattr("time.time", lambda: t1)
            payload_t1 = {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {"used_percent": 18.0, "reset_at": 1779846359, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 32.0, "reset_at": "2026-06-01T00:00:00Z", "limit_window_seconds": 604800},
                },
            }
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: payload_t1)

            rec1 = collect_once(db_path=db_file)
            assert rec1 is not None

            # Step 3: Query route via temp home override without query param
            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200
            data = resp.json()

            assert len(data["rows"]) == 2
            assert len(data["intervals"]) == 1

            inv = data["intervals"][0]
            assert inv["delta_used_percent"] == 8.0
            assert inv["kind"] == "depletion"
            assert inv["status"] == "ok"

            delta = inv["checkpoint_delta"]
            assert delta["status"] == "ok"
            assert delta["has_baseline"] is True
            assert delta["has_discontinuity"] is False
            assert delta["attribution_status"] == "correlated_only"
            assert delta["external_usage_possible"] is True
            assert delta["activity_scope"] == "profile_local_activity"
            assert delta["confirmed_same_account"] is False

            # CRITICAL: Verify deltas are computed between t0 and t1, NOT lifetime totals!
            total_delta = delta["total_delta"]
            assert total_delta["api_call_count"] == 10
            assert total_delta["input_tokens"] == 2300
            assert total_delta["output_tokens"] == 450

            # Session deltas
            session_deltas = delta["session_deltas"]
            assert len(session_deltas) == 2
            s1 = next(s for s in session_deltas if s["session_id"] == "sess-1")
            assert s1["delta_counters"]["api_call_count"] == 7
            assert s1["delta_counters"]["input_tokens"] == 1500
            assert s1["delta_counters"]["output_tokens"] == 300
            assert s1["activity_scope"] == "profile_local_activity"
            assert s1["confirmed_same_account"] is False

            s2 = next(s for s in session_deltas if s["session_id"] == "sess-2")
            assert s2["delta_counters"]["api_call_count"] == 3
            assert s2["delta_counters"]["input_tokens"] == 800
            assert s2["delta_counters"]["output_tokens"] == 150
            assert s2["activity_scope"] == "profile_local_activity"
            assert s2["confirmed_same_account"] is False
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_missing_checkpoint_and_errors(self, tmp_path, standalone_client: TestClient):
        """Verify handling of missing checkpoints, error gaps, and unknown baseline preservation."""
        import time
        from hermes_cli.codex_quota_snapshots import record_codex_quota_snapshot
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            now = time.time()
            acc = "acc_missing_chk"

            # 1. Successful snapshot at t0 without any checkpoint (e.g. legacy or failed checkpoint capture)
            record_codex_quota_snapshot({
                "account_id": acc,
                "observed_at": now - 300,
                "status": "ok",
                "primary_used_percent": 10.0,
            }, db_path=db_file)

            # 2. HTTP error snapshot at t1
            record_codex_quota_snapshot({
                "account_id": acc,
                "observed_at": now - 200,
                "status": "error",
                "error_code": "HTTP_429",
                "primary_used_percent": None,
            }, db_path=db_file)

            # 3. Successful snapshot at t2
            record_codex_quota_snapshot({
                "account_id": acc,
                "observed_at": now - 100,
                "status": "ok",
                "primary_used_percent": 15.0,
            }, db_path=db_file)

            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200
            data = resp.json()

            assert len(data["rows"]) == 3
            assert len(data["intervals"]) == 2

            # Both intervals cross the error or missing checkpoint -> gap/unknown
            inv0 = data["intervals"][0]
            assert inv0["kind"] == "gap"
            assert inv0["delta_used_percent"] is None
            assert inv0["checkpoint_delta"]["status"] == "unknown"
            assert inv0["checkpoint_delta"]["total_delta"] is None

            inv1 = data["intervals"][1]
            assert inv1["kind"] == "gap"
            assert inv1["delta_used_percent"] is None
            assert inv1["checkpoint_delta"]["status"] == "unknown"
            assert inv1["checkpoint_delta"]["total_delta"] is None
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_account_switches_no_cross_account_join(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, standalone_client: TestClient
    ):
        """Verify account switches avoid cross-account joins and preserve profile-local labels."""
        import sqlite3
        import time
        from hermes_cli.codex_quota_collector import collect_once
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            conn = sqlite3.connect(str(db_file))
            conn.execute("""
                CREATE TABLE session_model_usage (
                    session_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    billing_provider TEXT NOT NULL DEFAULT '',
                    billing_base_url TEXT NOT NULL DEFAULT '',
                    billing_mode TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL DEFAULT '',
                    api_call_count INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
                )
            """)
            conn.execute("""
                INSERT INTO session_model_usage (session_id, model, billing_provider, api_call_count, input_tokens, output_tokens)
                VALUES ('sess-switch', 'gpt-5.4', 'openai-codex', 2, 400, 100)
            """)
            conn.commit()
            conn.close()

            base_time = time.time() - 1000

            # Phase 1: Account A
            monkeypatch.setattr(
                "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
                lambda base_url, api_key: ("token-A", "https://api.openai.com", "account-A"),
            )
            monkeypatch.setattr("time.time", lambda: base_time)
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: {
                "rate_limit": {"primary_window": {"used_percent": 5.0, "limit_window_seconds": 18000}}
            })
            collect_once(db_path=db_file)

            # Account A at t1
            monkeypatch.setattr("time.time", lambda: base_time + 100)
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: {
                "rate_limit": {"primary_window": {"used_percent": 8.0, "limit_window_seconds": 18000}}
            })
            collect_once(db_path=db_file)

            # Phase 2: Switch to Account B!
            conn = sqlite3.connect(str(db_file))
            conn.execute("""
                UPDATE session_model_usage
                SET api_call_count = 10, input_tokens = 2000, output_tokens = 500
                WHERE session_id = 'sess-switch'
            """)
            conn.commit()
            conn.close()

            monkeypatch.setattr(
                "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
                lambda base_url, api_key: ("token-B", "https://api.openai.com", "account-B"),
            )
            # Account B at t2
            monkeypatch.setattr("time.time", lambda: base_time + 200)
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: {
                "rate_limit": {"primary_window": {"used_percent": 40.0, "limit_window_seconds": 18000}}
            })
            collect_once(db_path=db_file)

            # Account B at t3 (more activity)
            conn = sqlite3.connect(str(db_file))
            conn.execute("""
                UPDATE session_model_usage
                SET api_call_count = 15, input_tokens = 3000, output_tokens = 750
                WHERE session_id = 'sess-switch'
            """)
            conn.commit()
            conn.close()

            monkeypatch.setattr("time.time", lambda: base_time + 300)
            monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: {
                "rate_limit": {"primary_window": {"used_percent": 52.0, "limit_window_seconds": 18000}}
            })
            collect_once(db_path=db_file)

            # Query route
            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200
            data = resp.json()

            assert len(data["rows"]) == 4
            # Exactly 2 intervals: 1 for Account A, 1 for Account B. NO cross-account join!
            intervals = data["intervals"]
            assert len(intervals) == 2

            inv_a = intervals[0]
            assert inv_a["account_id"] == "account-A"
            assert inv_a["delta_used_percent"] == 3.0

            inv_b = intervals[1]
            assert inv_b["account_id"] == "account-B"
            assert inv_b["delta_used_percent"] == 12.0

            b_delta = inv_b["checkpoint_delta"]
            assert b_delta["account_id"] == "account-B"
            assert b_delta["status"] == "ok"
            assert b_delta["total_delta"]["api_call_count"] == 5
            assert b_delta["total_delta"]["input_tokens"] == 1000
            assert b_delta["total_delta"]["output_tokens"] == 250
            assert b_delta["activity_scope"] == "profile_local_activity"
            assert b_delta["confirmed_same_account"] is False
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_existing_quota_without_checkpoints_shows_quota(self, tmp_path, standalone_client: TestClient):
        """Existing quota snapshots without activity checkpoints table must still show quota (not hide all)."""
        import sqlite3
        import time
        from hermes_cli.codex_quota_snapshots import record_codex_quota_snapshot
        from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(tmp_path)
        try:
            db_file = tmp_path / "state.db"
            now = time.time()

            record_codex_quota_snapshot({
                "account_id": "acc_quota_only",
                "observed_at": now - 300,
                "status": "ok",
                "primary_used_percent": 25.0,
                "primary_reset_at": "2026-04-01T00:00:00Z",
            }, db_path=db_file)

            record_codex_quota_snapshot({
                "account_id": "acc_quota_only",
                "observed_at": now - 100,
                "status": "ok",
                "primary_used_percent": 30.0,
                "primary_reset_at": "2026-04-01T00:00:00Z",
            }, db_path=db_file)

            with sqlite3.connect(str(db_file)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                assert "codex_quota_snapshots" in tables
                assert "codex_activity_checkpoints" not in tables

            data = get_codex_quota_timeline(days=7, db_path=db_file)
            assert len(data["rows"]) == 2
            assert len(data["snapshots"]) == 2
            assert len(data["intervals"]) == 1

            inv = data["intervals"][0]
            assert inv["account_id"] == "acc_quota_only"
            assert inv["delta_used_percent"] == 5.0
            assert inv["checkpoint_delta"]["status"] == "missing_checkpoint"

            with sqlite3.connect(str(db_file)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                assert "codex_activity_checkpoints" not in tables

            resp = standalone_client.get("/api/codex/quota-timeline?days=7")
            assert resp.status_code == 200
            route_data = resp.json()
            assert len(route_data["rows"]) == 2
            assert len(route_data["intervals"]) == 1
        finally:
            reset_hermes_home_override(token)

    def test_quota_timeline_empty_db_no_ddl_and_schema_unmodified(self, tmp_path):
        """Calling quota-timeline on empty DB performs zero DDL and preserves empty schema."""
        import sqlite3
        from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline

        db_file = tmp_path / "empty_timeline.db"
        with sqlite3.connect(str(db_file)) as conn:
            pass

        data = get_codex_quota_timeline(days=7, db_path=db_file)
        assert data["rows"] == []
        assert data["snapshots"] == []
        assert data["intervals"] == []
        assert data["checkpoint_deltas"] == []

        with sqlite3.connect(str(db_file)) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) == 0

    def test_quota_timeline_schema_and_data_before_after_identical(self, tmp_path):
        """Calling get_codex_quota_timeline preserves identical schema and data before and after."""
        import sqlite3
        import time
        from hermes_cli.codex_quota_snapshots import record_codex_quota_snapshot
        from hermes_cli.codex_usage_attribution import capture_checkpoint
        from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline

        db_file = tmp_path / "state.db"
        now = time.time()
        record_codex_quota_snapshot({
            "account_id": "acc_schema_test",
            "observed_at": now - 200,
            "status": "ok",
            "primary_used_percent": 10.0,
        }, db_path=db_file)
        capture_checkpoint("acc_schema_test", observed_at=now - 200, db_path=db_file)

        def _state():
            with sqlite3.connect(str(db_file)) as c:
                c.row_factory = sqlite3.Row
                schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
                s_data = c.execute("SELECT * FROM codex_quota_snapshots ORDER BY id").fetchall()
                c_data = c.execute("SELECT * FROM codex_activity_checkpoints ORDER BY checkpoint_id").fetchall()
                return [dict(r) for r in schema], [dict(r) for r in s_data], [dict(r) for r in c_data]

        s_before, snap_before, chk_before = _state()

        res = get_codex_quota_timeline(days=7, db_path=db_file)
        assert len(res["rows"]) == 1

        s_after, snap_after, chk_after = _state()
        assert s_before == s_after
        assert snap_before == snap_after
        assert chk_before == chk_after
