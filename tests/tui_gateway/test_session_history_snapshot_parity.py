import threading
import pytest
from hermes_state import SessionDB
from tui_gateway import server


def test_hydration_snapshot_full_parity_plain_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "test-session-plain-parity"

    db.create_session(sid, "Test Plain Parity")
    for i in range(1, 45):
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
        # 1. Fetch canonical full projection
        full_resp = server.handle_request({
            "id": "full",
            "method": "session.history",
            "params": {"session_id": sid},
        })
        assert "result" in full_resp
        canonical_messages = full_resp["result"]["messages"]
        assert len(canonical_messages) == 44

        # 2. Page through using server-side hydration snapshot (limit=10)
        paged_messages = []
        cursor = 0
        snapshot_token = None
        has_more = True

        while has_more:
            params = {"session_id": sid, "limit": 10}
            if snapshot_token is not None:
                params["snapshot_token"] = snapshot_token
                params["cursor"] = cursor
            else:
                params["cursor"] = 0

            page_resp = server.handle_request({
                "id": f"page-{cursor}",
                "method": "session.history",
                "params": params,
            })
            assert "result" in page_resp
            res = page_resp["result"]
            snapshot_token = res["snapshot_token"]
            cursor = res["next_cursor"]
            has_more = res["has_more"]
            paged_messages.extend(res["messages"])

        # Parity assertion: paged messages equal canonical messages
        assert len(paged_messages) == len(canonical_messages)
        assert [m["text"] for m in paged_messages] == [m["text"] for m in canonical_messages]
        assert [m.get("row_id") for m in paged_messages] == [m.get("row_id") for m in canonical_messages]

        # 3. Verify tail_limit parity
        tail_resp = server.handle_request({
            "id": "tail",
            "method": "session.history",
            "params": {"session_id": sid, "tail_limit": 15},
        })
        assert "result" in tail_resp
        tail_messages = tail_resp["result"]["messages"]
        assert len(tail_messages) == 15
        assert [m["text"] for m in tail_messages] == [m["text"] for m in canonical_messages[-15:]]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_hydration_snapshot_full_parity_compaction_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_sid = "parent-session"
    child_sid = "child-session"

    db.create_session(parent_sid, "Parent Session")
    for i in range(1, 11):
        db.append_message(parent_sid, "user" if i % 2 == 1 else "assistant", f"Parent Msg {i}")

    # Child session forks from parent
    db.create_session(child_sid, "Child Session", parent_session_id=parent_sid)
    for i in range(1, 15):
        db.append_message(child_sid, "user" if i % 2 == 1 else "assistant", f"Child Msg {i}")

    server._sessions[child_sid] = {
        "session_key": child_sid,
        "history": [],
        "profile_home": str(tmp_path),
        "history_lock": threading.Lock(),
        "running": False,
        "agent": None,
        "created_at": 1.0,
        "last_active": 1.0,
    }

    try:
        # Full canonical
        full_resp = server.handle_request({
            "id": "full",
            "method": "session.history",
            "params": {"session_id": child_sid},
        })
        canonical_messages = full_resp["result"]["messages"]

        # Page through snapshot
        paged_messages = []
        cursor = 0
        snapshot_token = None
        has_more = True

        while has_more:
            params = {"session_id": child_sid, "limit": 6}
            if snapshot_token is not None:
                params["snapshot_token"] = snapshot_token
                params["cursor"] = cursor
            else:
                params["cursor"] = 0

            page_resp = server.handle_request({
                "id": f"page-{cursor}",
                "method": "session.history",
                "params": params,
            })
            res = page_resp["result"]
            snapshot_token = res["snapshot_token"]
            cursor = res["next_cursor"]
            has_more = res["has_more"]
            paged_messages.extend(res["messages"])

        assert len(paged_messages) == len(canonical_messages)
        assert [m["text"] for m in paged_messages] == [m["text"] for m in canonical_messages]
    finally:
        server._sessions.pop(child_sid, None)
        db.close()


def test_hydration_snapshot_immutable_against_late_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "test-session-immutable-boundary"

    db.create_session(sid, "Test Immutable Boundary")
    for i in range(1, 21):
        db.append_message(sid, "user" if i % 2 == 1 else "assistant", f"Msg {i}")

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
        # Initiate snapshot page 1 (cursor: 0, limit: 10)
        p1 = server.handle_request({
            "id": "p1",
            "method": "session.history",
            "params": {"session_id": sid, "cursor": 0, "limit": 10},
        })["result"]

        token = p1["snapshot_token"]
        assert p1["count"] == 10
        assert p1["total"] == 20

        # While client is streaming, late turns arrive in database!
        db.append_message(sid, "user", "Late Msg 21")
        db.append_message(sid, "assistant", "Late Msg 22")

        # Page 2 from snapshot
        p2 = server.handle_request({
            "id": "p2",
            "method": "session.history",
            "params": {"session_id": sid, "snapshot_token": token, "cursor": p1["next_cursor"], "limit": 10},
        })["result"]

        assert p2["count"] == 10
        assert p2["has_more"] is False
        assert p2["messages"][-1]["text"] == "Msg 20"
        # Late messages were NOT included in the immutable snapshot!
        assert not any(m["text"] == "Late Msg 21" for m in p2["messages"])
    finally:
        server._sessions.pop(sid, None)
        db.close()
