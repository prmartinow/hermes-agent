"""Test session list total_message_count calculation across compression chains."""

from pathlib import Path
from hermes_state import SessionDB


def test_list_sessions_rich_total_message_count_preservation(tmp_path):
    """Verify that compressed conversations report cumulative total_message_count in sidebar."""
    db = SessionDB(tmp_path / "state.db")
    
    # 1. Root conversation: 3 messages
    db.create_session("sess_root", "Project discussion")
    db.append_message("sess_root", "user", "Message 1")
    db.append_message("sess_root", "assistant", "Message 2")
    db.append_message("sess_root", "user", "Message 3")
    
    # 2. Compaction happens -> creates continuation tip
    db.create_session("sess_tip", "Project discussion (continued)", parent_session_id="sess_root")
    db.end_session("sess_root", end_reason="compression")
    db.append_message("sess_tip", "user", "Message 4 (post-compaction)")
    db.append_message("sess_tip", "assistant", "Message 5 (reply)")
    
    # 3. Query rich session list
    sessions = db.list_sessions_rich()
    assert len(sessions) == 1, f"Expected 1 projected session, got {len(sessions)}"
    
    s = sessions[0]
    assert s["id"] == "sess_tip"
    assert s["message_count"] == 2  # active in-context messages
    assert s["total_message_count"] == 5  # total historical messages across lineage


def test_list_sessions_rich_in_place_compaction_total_count(tmp_path):
    """Verify that in-place compacted conversations report cumulative total_message_count in sidebar."""
    db = SessionDB(tmp_path / "state.db")

    session_id = "sess_inplace"
    db.create_session(session_id, "In-place discussion")

    # 1. Add 6 initial messages
    for i in range(1, 7):
        db.append_message(session_id, "user" if i % 2 == 1 else "assistant", f"Message {i}")

    # 2. In-place compaction soft-archives turns (active=0, compacted=1) and leaves 2 active turns
    compacted_set = [
        {"role": "system", "content": "Summary of prior turns"},
        {"role": "assistant", "content": "Message 6"},
    ]
    db.archive_and_compact(session_id, compacted_set)

    # 3. Post-compaction user message added
    db.append_message(session_id, "user", "Message 7")

    # 4. Query rich session list
    sessions = db.list_sessions_rich()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["id"] == session_id
    # message_count reflects only the live active prompt context (summary + message 6 + message 7 = 3)
    assert s["message_count"] == 3
    # total_message_count reflects cumulative visual history (active=1 OR compacted=1)
    assert s["total_message_count"] > s["message_count"]
    # 6 pre-compaction + 2 compacted inserted + 1 new turn = 9 rows total with active=1 or compacted=1
    assert s["total_message_count"] == 9

