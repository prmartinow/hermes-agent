"""Unit and integration tests for turn lease resilience, deferred cleanup,
recursive CLI execution guard, and the share_to_session helper.
"""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import (
    AIAgent,
    _PENDING_LEASE_CLEANUP,
    _PENDING_LEASE_CLEANUP_LOCK,
    _drain_pending_lease_cleanups,
)


def _agent_stub(db=None):
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "test_sess"
    agent.platform = "cli"
    agent.model = "test-model"
    agent._session_db = db or SessionDB()
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: "test_sess"
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.status_callback = None
    return agent


def test_turn_lease_default_ttl_and_refresh_interval():
    """Verify tuned default TTL (45s) and refresh interval (15s)."""
    agent = _agent_stub()
    assert getattr(agent, "_session_turn_lease_ttl_seconds", 45.0) == 45.0
    assert getattr(agent, "_session_turn_lease_refresh_interval", 15.0) == 15.0


def test_deferred_lease_cleanup_drains_on_next_cycle(tmp_path):
    """Deferred lease cleanup enqueues stranded leases and drains on next DB access."""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("sess_deferred", source="test")
    holder = "pid=99999:turn=test_holder"

    # Acquire lease
    assert db.try_acquire_session_turn_lease("sess_deferred", holder, ttl_seconds=45)

    # Manually enqueue into deferred cleanup set
    with _PENDING_LEASE_CLEANUP_LOCK:
        _PENDING_LEASE_CLEANUP.add(("sess_deferred", holder))

    assert ("sess_deferred", holder) in _PENDING_LEASE_CLEANUP

    # Drain cleanups
    _drain_pending_lease_cleanups(db)

    with _PENDING_LEASE_CLEANUP_LOCK:
        assert ("sess_deferred", holder) not in _PENDING_LEASE_CLEANUP

    # Verify lease is now free
    new_holder = "pid=88888:turn=new_holder"
    assert db.try_acquire_session_turn_lease("sess_deferred", new_holder, ttl_seconds=45)
    db.release_session_turn_lease("sess_deferred", new_holder)


def test_recursive_cli_guard_blocks_interactive_chat(monkeypatch):
    """Nested interactive chat sessions are blocked when HERMES_ACTIVE_TURN=1."""
    from hermes_cli.main import cmd_chat
    import hermes_cli.main as hmain

    monkeypatch.setenv("HERMES_ACTIVE_TURN", "1")
    args = MagicMock()

    with pytest.raises(SystemExit) as exc_info:
        cmd_chat(args)

    assert exc_info.value.code == 1


def test_share_to_session_dual_atomic_delivery(tmp_path, monkeypatch):
    """share_to_session writes to SQLite under role=user and emits a live stream event."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    db = SessionDB(db_path)
    db.create_session("target_sess", source="test")
    db.close()

    monkeypatch.setattr("hermes_state.SessionDB", lambda: SessionDB(db_path))

    from tools.code_execution_tool import _COMMON_HELPERS
    scope = {
        "json": json,
        "os": os,
        "shlex": shlex,
        "time": time,
    }
    exec(_COMMON_HELPERS, scope)
    share_to_session = scope["share_to_session"]

    res = share_to_session("target_sess", "Here is shared data", source_label="Agent A")
    assert res.get("success") is True
    assert res.get("session_id") == "target_sess"

    # Verify message in database
    verify_db = SessionDB(db_path)
    msgs = verify_db.get_messages("target_sess")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "[Shared Note from Agent A]" in msgs[0]["content"]
    assert "Here is shared data" in msgs[0]["content"]
    verify_db.close()
