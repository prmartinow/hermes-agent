"""Durable multi-turn undo through the TUI command dispatcher."""

from tests.fixtures.storage_read import conversation
from tests.fixtures.storage_seed import seed_messages, seed_session
from tests.gateway.fixtures.storage_handles import storage_handle
from tui_gateway import server
from tests.tui_gateway.test_tui_gateway_server import _session


def test_slash_undo_multi_turn(tmp_path, monkeypatch):
    """The command.dispatch /undo N route rewinds N durable user turns and returns prefill."""
    db = storage_handle(tmp_path)
    seed_session(db, "session-key", "tui")
    history = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "reply 2"},
        {"role": "user", "content": "turn 3"},
        {"role": "assistant", "content": "reply 3"},
    ]
    seed_messages(db, "session-key", *history)
    server._sessions["sid"] = _session(running=False, history=history)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    try:
        resp = server.handle_request(
            {"id": "1", "method": "command.dispatch", "params": {"session_id": "sid", "name": "undo", "arg": "2"}}
        )
        assert resp.get("result"), f"got error: {resp.get('error')}"
        assert resp["result"]["type"] == "prefill"
        assert resp["result"]["message"] == "turn 2"
        assert "Undid 2 turns (4 message(s))" in resp["result"]["notice"]
        assert len(server._sessions["sid"]["history"]) == 2
        assert server._sessions["sid"]["history"][-1]["content"] == "reply 1"
        assert [message["content"] for message in conversation(db, "session-key")] == ["turn 1", "reply 1"]
    finally:
        server._sessions.pop("sid", None)
        db.close()
