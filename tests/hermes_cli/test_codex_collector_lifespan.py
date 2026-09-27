"""Targeted tests for Codex quota collector integration into web_server dashboard lifespan."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from hermes_cli.codex_quota_collector import (
    _active_collectors,
    _collectors_lock,
    _stop_all_collectors,
    is_collector_running,
    start_codex_quota_collector,
    stop_codex_quota_collector,
)
import hermes_cli.web_server as web_server_mod


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure each test runs with an isolated temporary home and no residual collectors."""
    test_home = tmp_path / "hermes_home"
    test_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    token = set_hermes_home_override(test_home)

    # Disarm other heavy background tasks in web_server lifespan
    monkeypatch.setattr(web_server_mod, "_warm_gateway_module", lambda: None)
    monkeypatch.setattr(web_server_mod, "_eager_reconcile_own_session_db", lambda: None)
    monkeypatch.setattr("tui_gateway.methods_groups.start_hosted_room_service", lambda: None)
    monkeypatch.setattr("tui_gateway.methods_groups.stop_hosted_room_service", lambda **kwargs: True)
    monkeypatch.setattr("hermes_cli.free_tier_bootstrap.start_background_bootstrap", lambda: None)
    monkeypatch.setattr("hermes_cli.auth.start_gemini_quota_watcher_daemon", lambda **kwargs: None)
    monkeypatch.setattr("hermes_cli.auth.stop_gemini_quota_watcher_daemon", lambda: None)

    # Clean app.state.initial_profile
    old_initial_profile = getattr(web_server_mod.app.state, "initial_profile", None)
    web_server_mod.app.state.initial_profile = ""

    _stop_all_collectors()
    try:
        yield test_home
    finally:
        _stop_all_collectors()
        if old_initial_profile is not None:
            web_server_mod.app.state.initial_profile = old_initial_profile
        else:
            if hasattr(web_server_mod.app.state, "initial_profile"):
                delattr(web_server_mod.app.state, "initial_profile")
        reset_hermes_home_override(token)


def test_server_import_no_collector_side_effects(isolate_test_environment: Path):
    """Importing web_server must never start background collectors or touch SQLite databases."""
    assert not is_collector_running()
    with _collectors_lock:
        assert len(_active_collectors) == 0

    lock_file = isolate_test_environment / ".codex_quota_collector.lock"
    assert not lock_file.exists()
    assert not (isolate_test_environment / "state.db").exists()


def test_lifespan_starts_and_stops_collector_for_default_profile(monkeypatch: pytest.MonkeyPatch):
    """Lifespan must start the collector once for default profile and stop it on shutdown."""
    start_calls = []
    stop_calls = []

    def mock_start(profile=None, **kwargs):
        start_calls.append(profile)
        return True

    def mock_stop(profile=None, **kwargs):
        stop_calls.append(profile)
        return True

    monkeypatch.setattr("hermes_cli.codex_quota_collector.start_codex_quota_collector", mock_start)
    monkeypatch.setattr("hermes_cli.codex_quota_collector.stop_codex_quota_collector", mock_stop)

    web_server_mod.app.state.initial_profile = ""

    with TestClient(web_server_mod.app, raise_server_exceptions=False):
        assert len(start_calls) == 1
        assert start_calls[0] is None
        assert len(stop_calls) == 0

    assert len(stop_calls) == 1
    assert stop_calls[0] is None


def test_lifespan_starts_and_stops_collector_for_served_custom_profile(monkeypatch: pytest.MonkeyPatch):
    """Lifespan must bind to the served owning profile configured on app.state."""
    start_calls = []
    stop_calls = []

    def mock_start(profile=None, **kwargs):
        start_calls.append(profile)
        return True

    def mock_stop(profile=None, **kwargs):
        stop_calls.append(profile)
        return True

    monkeypatch.setattr("hermes_cli.codex_quota_collector.start_codex_quota_collector", mock_start)
    monkeypatch.setattr("hermes_cli.codex_quota_collector.stop_codex_quota_collector", mock_stop)

    web_server_mod.app.state.initial_profile = "custom_agent_profile"

    with TestClient(web_server_mod.app, raise_server_exceptions=False):
        assert len(start_calls) == 1
        assert start_calls[0] == "custom_agent_profile"
        assert len(stop_calls) == 0

    assert len(stop_calls) == 1
    assert stop_calls[0] == "custom_agent_profile"


def test_lifespan_reads_env_profile_when_initial_profile_unset(monkeypatch: pytest.MonkeyPatch):
    """When app.state.initial_profile is unset, HERMES_PROFILE env var is honored."""
    start_calls = []
    stop_calls = []

    def mock_start(profile=None, **kwargs):
        start_calls.append(profile)
        return True

    def mock_stop(profile=None, **kwargs):
        stop_calls.append(profile)
        return True

    monkeypatch.setattr("hermes_cli.codex_quota_collector.start_codex_quota_collector", mock_start)
    monkeypatch.setattr("hermes_cli.codex_quota_collector.stop_codex_quota_collector", mock_stop)
    monkeypatch.setenv("HERMES_PROFILE", "env_profile")

    web_server_mod.app.state.initial_profile = ""

    with TestClient(web_server_mod.app, raise_server_exceptions=False):
        assert len(start_calls) == 1
        assert start_calls[0] == "env_profile"
        assert len(stop_calls) == 0

    assert len(stop_calls) == 1
    assert stop_calls[0] == "env_profile"


def test_lifespan_real_collector_no_credentials_resilience(
    isolate_test_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Real lifespan execution with unmocked collector in an empty home with no credentials.

    Collector starts cleanly, attempts credential resolution, safely refuses DB operations
    without error, and cleanly shuts down without leaving orphan threads or hanging.
    """
    # Block any external HTTP calls
    def fail_on_network(*args, **kwargs):
        raise AssertionError("Network access attempted during offline credentials test")

    monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", fail_on_network)

    assert not is_collector_running()

    with TestClient(web_server_mod.app, raise_server_exceptions=False):
        # Collector thread should be active
        assert is_collector_running()
        # Lock lease file was created for singleton protection
        lock_file = isolate_test_environment / ".codex_quota_collector.lock"
        assert lock_file.exists()

    # After lifespan shutdown, collector must be stopped
    assert not is_collector_running()
    # No spurious quota snapshots were saved because identity resolution failed
    assert not (isolate_test_environment / "state.db").exists()


def test_lifespan_duplicate_start_prevented_by_lease(
    isolate_test_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """While dashboard lifespan is active, duplicate start attempts (e.g. reload/process) are refused."""
    monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda *a, **kw: {})

    with TestClient(web_server_mod.app, raise_server_exceptions=False):
        assert is_collector_running()
        # Attempting a second start for the same profile/home must return False
        dup_started = start_codex_quota_collector()
        assert dup_started is False
        assert is_collector_running()

    # After shutdown, lease is released and reacquisition succeeds
    assert not is_collector_running()
    reacquired = start_codex_quota_collector()
    assert reacquired is True
    assert is_collector_running()
    assert stop_codex_quota_collector() is True
    assert not is_collector_running()
