"""Tests for /redo handling in tui_gateway and SessionDB.

Verifies:
1. Single-turn redo after undo restores database and in-memory history.
2. Multi-turn redo restores turns in forward chronological order.
3. Branch invalidation: new user message after undo prevents redoing old turns.
4. Redo idempotency: redoing with empty redo stack returns notice and is safe no-op.
5. session.redo RPC method restores history and returns messages.
6. Compaction safety: rows marked compacted=1 are never resurrected by redo.
7. Tools: turns with tool calls and tool results are completely restored.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()
    mod._db = None


@pytest.fixture()
def db(hermes_home):
    return SessionDB(db_path=hermes_home / "state.db")


def _call(server, method, **params):
    return server._methods[method](1, params)


def test_sessiondb_redo_single_and_multi_turn(db):
    sid = "test-sessiondb-redo"
    db.create_session(sid, "tui", model="test")
    m1 = db.append_message(sid, "user", "q1")
    m2 = db.append_message(sid, "assistant", "a1")
    m3 = db.append_message(sid, "user", "q2")
    m4 = db.append_message(sid, "assistant", "a2")
    m5 = db.append_message(sid, "user", "q3")
    m6 = db.append_message(sid, "assistant", "a3")

    # Undo turn 3 and turn 2
    db.rewind_to_message(sid, m5)
    db.rewind_to_message(sid, m3)
    assert len(db.get_messages_as_conversation(sid)) == 2

    # Redo 1 turn -> restores turn 2 (q2, a2)
    res = db.redo_turn(sid, 1)
    assert res["restored_turns"] == 1
    assert res["restored_count"] == 2
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 4
    assert conv[2]["content"] == "q2"
    assert conv[3]["content"] == "a2"

    # Redo next turn -> restores turn 3 (q3, a3)
    res2 = db.redo_turn(sid, 1)
    assert res2["restored_turns"] == 1
    assert res2["restored_count"] == 2
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 6
    assert conv[4]["content"] == "q3"
    assert conv[5]["content"] == "a3"

    # Redo again -> empty stack
    res3 = db.redo_turn(sid, 1)
    assert res3["restored_turns"] == 0
    assert res3["restored_count"] == 0
    assert len(conv) == 6


def test_redo_branch_invalidation_after_new_prompt(db):
    sid = "test-branch-invalidation"
    db.create_session(sid, "tui", model="test")
    m1 = db.append_message(sid, "user", "q1")
    m2 = db.append_message(sid, "assistant", "a1")
    m3 = db.append_message(sid, "user", "q2_old")
    m4 = db.append_message(sid, "assistant", "a2_old")

    # Undo turn 2
    db.rewind_to_message(sid, m3)
    assert len(db.get_messages_as_conversation(sid)) == 2

    # User types a fresh prompt instead of redoing
    m5 = db.append_message(sid, "user", "q2_new")
    m6 = db.append_message(sid, "assistant", "a2_new")
    assert len(db.get_messages_as_conversation(sid)) == 4

    # Redo must return 0 because max_active_id is m6 (id > old undone rows)
    res = db.redo_turn(sid, 1)
    assert res["restored_turns"] == 0
    assert res["restored_count"] == 0
    assert len(db.get_messages_as_conversation(sid)) == 4


def test_redo_preserves_compacted_messages(db):
    sid = "test-compacted-safety"
    db.create_session(sid, "tui", model="test")
    m1 = db.append_message(sid, "user", "old1")
    m2 = db.append_message(sid, "assistant", "old_ans1")

    # Simulate in-place compaction: active=0, compacted=1
    db._execute_write(lambda conn: conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ?", (sid,)))

    # Insert summary and new turn
    m3 = db.append_message(sid, "user", "[CONTEXT COMPACTION] summary", _compressed_summary=True)
    m4 = db.append_message(sid, "user", "turn2")
    m5 = db.append_message(sid, "assistant", "turn2_ans")

    # Undo turn 2
    db.rewind_to_message(sid, m4)

    # Redo turn 2
    res = db.redo_turn(sid, 1)
    assert res["restored_turns"] == 1

    # Compacted rows must still be active=0, compacted=1
    with db._read_ctx() as conn:
        compacted_rows = conn.execute(
            "SELECT id, active, compacted FROM messages WHERE session_id = ? AND compacted = 1",
            (sid,),
        ).fetchall()
        assert len(compacted_rows) == 2
        for r in compacted_rows:
            assert r["active"] == 0
            assert r["compacted"] == 1


def test_redo_restores_tool_calls_and_results(db):
    sid = "test-redo-tools"
    db.create_session(sid, "tui", model="test")
    m1 = db.append_message(sid, "user", "q1")
    m2 = db.append_message(sid, "assistant", "a1")
    m3 = db.append_message(sid, "user", "read file")
    m4 = db.append_message(sid, "assistant", "", tool_calls=[{"id": "call_1", "function": {"name": "read_file"}}])
    m5 = db.append_message(sid, "tool", "contents of file", tool_call_id="call_1")
    m6 = db.append_message(sid, "assistant", "I read the file.")

    assert len(db.get_messages_as_conversation(sid)) == 6
    db.rewind_to_message(sid, m3)
    assert len(db.get_messages_as_conversation(sid)) == 2

    res = db.redo_turn(sid, 1)
    assert res["restored_turns"] == 1
    assert res["restored_count"] == 4  # user + assistant(call) + tool + assistant(resp)
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 6
    assert conv[2]["content"] == "read file"
    assert conv[3]["tool_calls"] is not None
    assert conv[4]["role"] == "tool"
    assert conv[5]["content"] == "I read the file."


def test_gateway_command_dispatch_redo(server, db):
    sid = "sid-gw-redo"
    session_key = "tui-gw-redo-1"
    db.create_session(session_key, source="tui")
    m1 = db.append_message(session_key, "user", "hello")
    m2 = db.append_message(session_key, "assistant", "world")

    history = db.get_messages_as_conversation(session_key)
    agent = MagicMock()
    agent._memory_manager = MagicMock()
    agent._last_flushed_db_idx = len(history)
    s = {
        "session_key": session_key,
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "agent": agent,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    server._db = db

    # Undo
    undo_resp = _call(server, "command.dispatch", session_id=sid, name="undo", arg="")
    assert "error" not in undo_resp
    assert len(s["history"]) == 0

    # Redo
    redo_resp = _call(server, "command.dispatch", session_id=sid, name="redo", arg="")
    assert "error" not in redo_resp
    assert redo_resp["result"]["type"] == "notice"
    assert "Redid 1 turn" in redo_resp["result"]["notice"]
    assert len(s["history"]) == 2
    assert s["history"][0]["content"] == "hello"
    assert s["history"][1]["content"] == "world"

    # Redo again -> nothing to redo
    redo_resp2 = _call(server, "command.dispatch", session_id=sid, name="redo", arg="")
    assert "error" not in redo_resp2
    assert "Nothing to redo" in redo_resp2["result"]["notice"]


def test_gateway_session_redo_rpc(server, db):
    sid = "sid-rpc-redo"
    session_key = "tui-rpc-redo-1"
    db.create_session(session_key, source="tui")
    m1 = db.append_message(session_key, "user", "msg1")
    m2 = db.append_message(session_key, "assistant", "reply1")

    history = db.get_messages_as_conversation(session_key)
    agent = MagicMock()
    agent._memory_manager = MagicMock()
    agent._last_flushed_db_idx = len(history)
    s = {
        "session_key": session_key,
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "agent": agent,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    server._db = db

    # Undo via RPC
    undo_resp = _call(server, "session.undo", session_id=sid)
    assert "error" not in undo_resp
    assert undo_resp["result"]["removed"] == 2

    # Redo via RPC
    redo_resp = _call(server, "session.redo", session_id=sid)
    assert "error" not in redo_resp
    assert redo_resp["result"]["restored_turns"] == 1
    assert redo_resp["result"]["restored_count"] == 2
    assert len(redo_resp["result"]["messages"]) == 2
    assert len(s["history"]) == 2
