import threading
import pytest
from hermes_state import SessionDB
from tui_gateway import server


def test_session_history_keyset_and_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "test-session-keyset"

    db.create_session(sid, "Test Session")
    for i in range(1, 25):
        db.append_message(sid, "user" if i % 2 == 1 else "assistant", f"Message {i}")

    server._sessions[sid] = {
        "session_key": sid,
        "history": [],
        "profile_home": str(tmp_path),
        "history_lock": threading.Lock(),
        "running": False,
        "agent": None,
        "created_at": 1.0,
        "last_active": 1.0,
    }

    try:
        # 1. Forward keyset paging (limit=10)
        res1 = server.handle_request({
            "id": "1",
            "method": "session.history",
            "params": {"session_id": sid, "after_row_id": 0, "limit": 10},
        })
        assert "result" in res1, res1
        r1 = res1["result"]
        assert r1["count"] == 10
        assert r1["after_row_id"] == 0
        assert r1["snapshot_max_row_id"] == 24
        assert r1["next_after_row_id"] == 10
        assert r1["has_more"] is True
        assert r1["messages"][0]["text"] == "Message 1"
        assert r1["messages"][-1]["text"] == "Message 10"

        # 2. Second page (after_row_id=10, limit=10, passing snapshot_max_row_id)
        res2 = server.handle_request({
            "id": "2",
            "method": "session.history",
            "params": {"session_id": sid, "after_row_id": 10, "snapshot_max_row_id": 24, "limit": 10},
        })
        assert "result" in res2, res2
        r2 = res2["result"]
        assert r2["count"] == 10
        assert r2["after_row_id"] == 10
        assert r2["snapshot_max_row_id"] == 24
        assert r2["next_after_row_id"] == 20
        assert r2["has_more"] is True
        assert r2["messages"][0]["text"] == "Message 11"
        assert r2["messages"][-1]["text"] == "Message 20"

        # 3. Final page (after_row_id=20, limit=10)
        res3 = server.handle_request({
            "id": "3",
            "method": "session.history",
            "params": {"session_id": sid, "after_row_id": 20, "snapshot_max_row_id": 24, "limit": 10},
        })
        assert "result" in res3, res3
        r3 = res3["result"]
        assert r3["count"] == 4
        assert r3["after_row_id"] == 20
        assert r3["next_after_row_id"] == 24
        assert r3["has_more"] is False
        assert r3["messages"][0]["text"] == "Message 21"
        assert r3["messages"][-1]["text"] == "Message 24"

        # 4. Tail limit query (tail_limit=5)
        res_tail = server.handle_request({
            "id": "4",
            "method": "session.history",
            "params": {"session_id": sid, "tail_limit": 5},
        })
        assert "result" in res_tail, res_tail
        rt = res_tail["result"]
        assert rt["count"] == 5
        assert rt["messages"][0]["text"] == "Message 20"
        assert rt["messages"][-1]["text"] == "Message 24"
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_session_history_keyset_edge_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    sid1 = "session-1"
    sid2 = "session-2"

    db.create_session(sid1, "Session 1")
    db.create_session(sid2, "Session 2")

    # Interleave rows across sessions:
    # row 1 (s1), row 2 (s2), row 3 (s1), row 4 (s2), row 5 (s1)
    db.append_message(sid1, "user", "S1 Msg 1")
    db.append_message(sid2, "user", "S2 Msg 1")
    db.append_message(sid1, "assistant", "S1 Msg 2")
    db.append_message(sid2, "assistant", "S2 Msg 2")
    db.append_message(sid1, "user", "S1 Msg 3")

    server._sessions[sid1] = {
        "session_key": sid1,
        "history": [],
        "profile_home": str(tmp_path),
        "history_lock": threading.Lock(),
        "running": False,
        "agent": None,
        "created_at": 1.0,
        "last_active": 1.0,
    }

    try:
        # Paging s1 with limit=2
        page1 = server.handle_request({
            "id": "p1",
            "method": "session.history",
            "params": {"session_id": sid1, "after_row_id": 0, "limit": 2},
        })
        r1 = page1["result"]
        assert r1["count"] == 2
        # s1 rows were row 1 and row 3 (row 2 was s2)
        assert r1["next_after_row_id"] == 3
        assert r1["snapshot_max_row_id"] == 5
        assert r1["has_more"] is True

        # While page 1 was taken, a new row is appended to s1 (row 6)
        db.append_message(sid1, "assistant", "S1 Msg 4 (late)")

        # Page 2 respects the original snapshot_max_row_id (5) and ignores row 6
        page2 = server.handle_request({
            "id": "p2",
            "method": "session.history",
            "params": {"session_id": sid1, "after_row_id": 3, "snapshot_max_row_id": 5, "limit": 2},
        })
        r2 = page2["result"]
        assert r2["count"] == 1
        assert r2["messages"][0]["text"] == "S1 Msg 3"
        assert r2["next_after_row_id"] == 5
        assert r2["has_more"] is False
    finally:
        server._sessions.pop(sid1, None)
        db.close()
