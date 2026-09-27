"""Tests for the single-shot Codex quota collector unit."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from hermes_cli.codex_quota_collector import (
    _acquire_collector_lease,
    _active_collectors,
    _collectors_lock,
    _release_collector_lease,
    _stop_all_collectors,
    collect_once,
    is_collector_running,
    start_codex_quota_collector,
    stop_codex_quota_collector,
)
from hermes_cli.codex_quota_snapshots import (
    derive_codex_quota_intervals,
    list_codex_quota_snapshots,
    record_codex_quota_snapshot,
)


@pytest.fixture(autouse=True)
def cleanup_collectors():
    yield
    _stop_all_collectors()


def test_collect_once_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "quota_success.db"

    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("mock-token", "https://api.openai.com", "acct_test_123"),
    )
    fake_payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 18.5,
                "reset_at": 1779846359,
                "limit_window_seconds": 18000,
            },
            "secondary_window": {
                "used_percent": 45.0,
                "reset_at": "2026-06-01T00:00:00Z",
                "limit_window_seconds": 604800,
            },
        },
    }
    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._get_json",
        lambda url, headers, timeout=15.0: fake_payload,
    )

    rec = collect_once(db_path=db_file)
    assert rec is not None
    assert rec.account_id == "acct_test_123"
    assert rec.status == "ok"
    assert rec.error_code is None
    assert rec.error_message is None
    assert rec.plan_type == "pro"
    assert rec.primary_used_percent == 18.5
    assert rec.primary_window_seconds == 18000
    assert rec.primary_reset_at is not None
    assert rec.secondary_used_percent == 45.0
    assert rec.secondary_window_seconds == 604800
    assert rec.secondary_reset_at == "2026-06-01T00:00:00Z"

    stored = list_codex_quota_snapshots(db_path=db_file)
    assert len(stored) == 1
    assert stored[0].id == rec.id
    assert stored[0].account_id == "acct_test_123"


def test_collect_once_missing_identity_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "quota_unresolved.db"

    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("mock-token", "https://api.openai.com", None),
    )
    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._codex_headers",
        lambda token, account_id: {"Authorization": "Bearer mock-token"},
    )

    rec = collect_once(db_path=db_file)
    assert rec is None
    assert not db_file.exists()


def test_collect_once_failed_http_error_safe_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "quota_error.db"
    account_id = "acct_gap"

    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("mock-token", "https://api.openai.com", account_id),
    )

    req = httpx.Request("GET", "https://api.openai.com/backend-api/codex/usage")
    resp = httpx.Response(429, request=req)

    def _raise_http_error(url, headers, timeout=15.0):
        raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)

    monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", _raise_http_error)

    # 1. Prior successful snapshot at t0
    t0 = time.time() - 300
    record_codex_quota_snapshot({
        "account_id": account_id,
        "observed_at": t0,
        "status": "ok",
        "primary_used_percent": 10.0,
        "primary_reset_at": "2026-06-01T00:00:00Z",
    }, db_path=db_file)

    # 2. Failed HTTP collection at t1
    rec = collect_once(db_path=db_file)
    assert rec is not None
    assert rec.status == "error"
    assert rec.error_code == "HTTP_429"
    assert rec.error_message is None
    assert rec.primary_used_percent is None

    # 3. Subsequent successful snapshot at t2
    t2 = time.time() + 300
    record_codex_quota_snapshot({
        "account_id": account_id,
        "observed_at": t2,
        "status": "ok",
        "primary_used_percent": 25.0,
        "primary_reset_at": "2026-06-01T00:00:00Z",
    }, db_path=db_file)

    stored = list_codex_quota_snapshots(db_path=db_file)
    assert len(stored) == 3
    assert stored[1].status == "error"
    assert stored[1].error_code == "HTTP_429"

    # Intervals across the error snapshot must safely degrade into gaps without corruption
    intervals = derive_codex_quota_intervals(stored, window_id="primary")
    assert len(intervals) == 2
    assert intervals[0].kind == "gap"
    assert intervals[0].delta_used_percent is None
    assert intervals[1].kind == "gap"
    assert intervals[1].delta_used_percent is None


def test_collector_lifecycle_duplicate_start_and_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_collect(**kwargs):
        nonlocal call_count
        call_count += 1
        return None

    monkeypatch.setattr("hermes_cli.codex_quota_collector.collect_once", mock_collect)

    home = tmp_path / "home_dup"
    home.mkdir(parents=True, exist_ok=True)
    db_file = home / "state.db"

    # Initial start
    started = start_codex_quota_collector(db_path=db_file, interval_seconds=0.05)
    assert started is True
    assert is_collector_running(db_path=db_file) is True

    # Duplicate start for the same resolved home must be refused (singleton)
    dup_started = start_codex_quota_collector(db_path=db_file, interval_seconds=0.05)
    assert dup_started is False

    # Stop running collector
    stopped = stop_codex_quota_collector(db_path=db_file)
    assert stopped is True
    assert is_collector_running(db_path=db_file) is False

    # Second stop is a clean no-op returning False
    assert stop_codex_quota_collector(db_path=db_file) is False


def test_collector_lifecycle_reacquire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("hermes_cli.codex_quota_collector.collect_once", lambda **kwargs: None)

    home = tmp_path / "home_reacquire"
    home.mkdir(parents=True, exist_ok=True)
    db_file = home / "state.db"

    # Start, stop, and reacquire in sequence
    assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is True
    assert stop_codex_quota_collector(db_path=db_file) is True
    assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is True
    assert stop_codex_quota_collector(db_path=db_file) is True

    # Simulate another process holding the lock file lease
    lock_file = home / ".codex_quota_collector.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+", encoding="utf-8") as external_lease:
        import fcntl
        fcntl.flock(external_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Cannot acquire lease while another process holds it
            assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is False
        finally:
            fcntl.flock(external_lease.fileno(), fcntl.LOCK_UN)

    # Reacquire succeeds immediately once the external lease is released
    assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is True
    assert stop_codex_quota_collector(db_path=db_file) is True


def test_collector_scope_aba_temp_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home_a = tmp_path / "scope_a"
    home_b = tmp_path / "scope_b"
    home_a.mkdir(parents=True, exist_ok=True)
    home_b.mkdir(parents=True, exist_ok=True)

    from hermes_constants import get_hermes_home

    baseline_home = get_hermes_home().resolve()
    observed_homes = []

    def mock_resolve(base_url, api_key):
        observed_homes.append(get_hermes_home().resolve())
        return ("mock-token", "https://api.openai.com", "acct_scope")

    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        mock_resolve,
    )
    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector.record_codex_quota_snapshot",
        lambda obs, db_path=None, profile=None: None,
    )
    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._get_json",
        lambda url, headers, timeout=15.0: {},
    )

    # 1. collect_once: Scope A
    collect_once(profile=str(home_a))
    assert len(observed_homes) == 1
    assert observed_homes[0] == home_a.resolve()
    assert get_hermes_home().resolve() == baseline_home

    # 2. collect_once: Scope B
    collect_once(profile=str(home_b))
    assert len(observed_homes) == 2
    assert observed_homes[1] == home_b.resolve()
    assert get_hermes_home().resolve() == baseline_home

    # 3. collect_once: Scope A again (A-B-A)
    collect_once(profile=str(home_a))
    assert len(observed_homes) == 3
    assert observed_homes[2] == home_a.resolve()
    assert get_hermes_home().resolve() == baseline_home

    # Background collectors for A and B can run concurrently without collision
    assert start_codex_quota_collector(profile=str(home_a), interval_seconds=0.05) is True
    assert start_codex_quota_collector(profile=str(home_b), interval_seconds=0.05) is True
    assert is_collector_running(profile=str(home_a)) is True
    assert is_collector_running(profile=str(home_b)) is True

    # Stopping A leaves B intact, and A can be reacquired (A-B-A lifecycle)
    assert stop_codex_quota_collector(profile=str(home_a)) is True
    assert is_collector_running(profile=str(home_a)) is False
    assert is_collector_running(profile=str(home_b)) is True

    assert start_codex_quota_collector(profile=str(home_a), interval_seconds=0.05) is True
    assert is_collector_running(profile=str(home_a)) is True

    assert stop_codex_quota_collector(profile=str(home_a)) is True
    assert stop_codex_quota_collector(profile=str(home_b)) is True


def test_collect_once_captures_checkpoint_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify that successful quota collection captures an activity checkpoint with the same observation_id/timestamp."""
    import sqlite3
    from hermes_cli.codex_usage_attribution import get_checkpoint

    db_file = tmp_path / "quota_checkpoint.db"
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
        VALUES ('sess-1', 'gpt-5.4', 'openai-codex', 7, 1200, 300)
    """)
    conn.commit()
    conn.close()

    account_id = "acct_chk_test"
    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("mock-token", "https://api.openai.com", account_id),
    )
    fake_payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 20.0, "reset_at": 1779846359, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 50.0, "reset_at": "2026-06-01T00:00:00Z", "limit_window_seconds": 604800},
        },
    }
    monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: fake_payload)

    rec = collect_once(db_path=db_file)
    assert rec is not None
    assert rec.status == "ok"
    assert rec.account_id == account_id

    # Checkpoint captured with same observation_id and timestamp
    chk = get_checkpoint(rec.observation_id, account_id=account_id, db_path=db_file)
    assert chk is not None
    assert chk.checkpoint_id == rec.observation_id
    assert chk.account_id == account_id
    assert chk.observed_at == rec.observed_at

    entry = chk.get_counters("sess-1", "gpt-5.4", "")
    assert entry is not None
    assert entry.api_call_count == 7
    assert entry.input_tokens == 1200
    assert entry.output_tokens == 300


def test_collect_once_checkpoint_capture_failure_does_not_lose_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify failure to capture checkpoint does not invent zero or lose the persisted quota sample."""
    db_file = tmp_path / "quota_checkpoint_fail.db"
    account_id = "acct_fail_safe"

    monkeypatch.setattr(
        "hermes_cli.codex_quota_collector._resolve_codex_usage_credentials",
        lambda base_url, api_key: ("mock-token", "https://api.openai.com", account_id),
    )
    fake_payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 15.0, "reset_at": 1779846359, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 30.0, "reset_at": "2026-06-01T00:00:00Z", "limit_window_seconds": 604800},
        },
    }
    monkeypatch.setattr("hermes_cli.codex_quota_collector._get_json", lambda url, headers, timeout=15.0: fake_payload)

    def _broken_capture(*args, **kwargs):
        raise RuntimeError("Simulated disk/checkpoint corruption")

    monkeypatch.setattr("hermes_cli.codex_usage_attribution.capture_checkpoint", _broken_capture)

    # Collector must NOT crash and MUST preserve the quota snapshot
    rec = collect_once(db_path=db_file)
    assert rec is not None
    assert rec.status == "ok"
    assert rec.account_id == account_id
    assert rec.primary_used_percent == 15.0

    stored = list_codex_quota_snapshots(db_path=db_file)
    assert len(stored) == 1
    assert stored[0].primary_used_percent == 15.0


def test_stop_collector_lease_race_blocks_until_worker_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "stop_race_home"
    home.mkdir(parents=True, exist_ok=True)
    db_file = home / "state.db"
    lock_file = home / ".codex_quota_collector.lock"

    entered_collect = threading.Event()
    block_collect = threading.Event()

    def mock_blocked_collect(**kwargs):
        entered_collect.set()
        block_collect.wait(timeout=5.0)
        return None

    monkeypatch.setattr("hermes_cli.codex_quota_collector.collect_once", mock_blocked_collect)

    try:
        # 1. Start collector
        assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.01) is True
        assert is_collector_running(db_path=db_file) is True
        assert entered_collect.wait(timeout=2.0) is True

        # 2. Stop with tiny timeout while worker is blocked in collect_once
        # Must return truthful bool (False because worker thread did not exit)
        stopped = stop_codex_quota_collector(db_path=db_file, timeout=0.01)
        assert stopped is False

        # 3. Handle must be retained in registry and worker still alive
        assert is_collector_running(db_path=db_file) is True
        with _collectors_lock:
            assert home.resolve() in _active_collectors

        # 4. Duplicate start must be blocked while stopping/alive
        dup_started = start_codex_quota_collector(db_path=db_file, interval_seconds=0.01)
        assert dup_started is False

        # 5. Independent lease acquisition must be blocked (worker holds exclusive flock)
        external_lease = _acquire_collector_lease(lock_file)
        assert external_lease is None

        # 6. Idempotent stop while still blocked returns False
        assert stop_codex_quota_collector(db_path=db_file, timeout=0.01) is False

        # 7. Release worker and wait for exit
        block_collect.set()

        # Wait for worker thread to exit
        deadline = time.time() + 3.0
        while time.time() < deadline and is_collector_running(db_path=db_file):
            time.sleep(0.02)
        assert is_collector_running(db_path=db_file) is False

        # Further stop after exit is clean no-op returning False
        assert stop_codex_quota_collector(db_path=db_file, timeout=2.0) is False

        # 8. Independent lease acquisition now succeeds
        external_lease = _acquire_collector_lease(lock_file)
        assert external_lease is not None
        _release_collector_lease(external_lease)

        # 9. Second start now succeeds cleanly
        assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is True
        assert is_collector_running(db_path=db_file) is True
        assert stop_codex_quota_collector(db_path=db_file, timeout=2.0) is True
        assert is_collector_running(db_path=db_file) is False

    finally:
        block_collect.set()
        _stop_all_collectors(timeout=2.0)


def test_stop_all_collectors_retains_stopping_handle_and_avoids_double_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "stop_all_race_home"
    home.mkdir(parents=True, exist_ok=True)
    db_file = home / "state.db"
    lock_file = home / ".codex_quota_collector.lock"

    entered_collect = threading.Event()
    block_collect = threading.Event()

    def mock_blocked_collect(**kwargs):
        entered_collect.set()
        block_collect.wait(timeout=5.0)
        return None

    monkeypatch.setattr("hermes_cli.codex_quota_collector.collect_once", mock_blocked_collect)

    try:
        # Start quota collector
        assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.01) is True
        assert is_collector_running(db_path=db_file) is True
        assert entered_collect.wait(timeout=2.0) is True

        # Call _stop_all_collectors with tiny timeout
        # Must return truthful bool (False because worker is still alive)
        all_stopped = _stop_all_collectors(timeout=0.01)
        assert all_stopped is False

        # Collector must still be registered and alive
        assert is_collector_running(db_path=db_file) is True
        with _collectors_lock:
            assert home.resolve() in _active_collectors

        # Duplicate start blocked while stopping
        assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.01) is False

        # Independent lease acquisition blocked
        external_lease = _acquire_collector_lease(lock_file)
        assert external_lease is None

        # Release blocking event so worker exits
        block_collect.set()

        # Wait for worker thread to exit naturally
        deadline = time.time() + 3.0
        while time.time() < deadline and is_collector_running(db_path=db_file):
            time.sleep(0.02)
        assert is_collector_running(db_path=db_file) is False

        # Handle safely removed from registry on actual exit
        with _collectors_lock:
            assert home.resolve() not in _active_collectors

        # Independent lease acquisition succeeds (worker released lease on exit; no double close)
        external_lease = _acquire_collector_lease(lock_file)
        assert external_lease is not None
        _release_collector_lease(external_lease)

        # Subsequent start and stop succeed cleanly without side effects
        assert start_codex_quota_collector(db_path=db_file, interval_seconds=0.05) is True
        assert is_collector_running(db_path=db_file) is True
        assert _stop_all_collectors(timeout=2.0) is True
        assert is_collector_running(db_path=db_file) is False

    finally:
        block_collect.set()
        _stop_all_collectors(timeout=2.0)
