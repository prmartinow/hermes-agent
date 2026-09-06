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
