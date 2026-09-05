"""Tests for session lineage traversal and multi-generation resume in SessionDB.

Validates that SessionDB correctly computes root-to-tip lineages, isolates
explicit branch sessions, dedupes replayed user turns across ancestor
boundaries, and provides coherent model_history vs. display_history across
lineage chains.
"""

from __future__ import annotations

import pytest
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


def test_session_lineage_root_to_tip_traversal(db: SessionDB):
    """Lineage correctly traverses parent_session_id pointers from root to tip."""
    db.create_session(session_id="root_sess", source="cli")
    db.create_session(session_id="child_sess", source="cli", parent_session_id="root_sess")
    db.create_session(session_id="grandchild_sess", source="cli", parent_session_id="child_sess")

    chain = db._session_lineage_root_to_tip("grandchild_sess")
    assert chain == ["root_sess", "child_sess", "grandchild_sess"]
    assert db.get_conversation_root("grandchild_sess") == "root_sess"


def test_lineage_resume_model_vs_display_history(db: SessionDB):
    """Resume across lineage serves only tip active rows to model, all ancestors to display."""
    db.create_session(session_id="gen1", source="cli")
    db.append_message("gen1", role="user", content="Gen 1 prompt")
    db.append_message("gen1", role="assistant", content="Gen 1 reply")

    db.create_session(session_id="gen2", source="cli", parent_session_id="gen1")
    db.append_message("gen2", role="user", content="Gen 2 prompt")
    db.append_message("gen2", role="assistant", content="Gen 2 reply")

    model_history, display_history = db.get_resume_conversations("gen2")

    # model_history is strictly gen2 (the tip)
    model_contents = [m["content"] for m in model_history]
    assert "Gen 1 prompt" not in model_contents
    assert "Gen 1 reply" not in model_contents
    assert "Gen 2 prompt" in model_contents
    assert "Gen 2 reply" in model_contents

    # display_history includes gen1 (ancestor) + gen2 (tip)
    display_contents = [m["content"] for m in display_history]
    assert display_contents == ["Gen 1 prompt", "Gen 1 reply", "Gen 2 prompt", "Gen 2 reply"]


def test_explicit_branch_session_isolated_from_parent_lineage(db: SessionDB):
    """Explicit branch sessions (_branched_from) are isolated from parent message stream."""
    db.create_session(session_id="parent_sess", source="cli")
    db.append_message("parent_sess", role="user", content="Initial prompt")
    db.append_message("parent_sess", role="assistant", content="Initial answer")

    # Create explicit branch
    db.create_session(
        session_id="branch_sess",
        source="cli",
        parent_session_id="parent_sess",
        model_config={"_branched_from": "parent_sess"},
    )
    db.append_message("branch_sess", role="user", content="Branch prompt")
    db.append_message("branch_sess", role="assistant", content="Branch answer")

    # Post-branch append on parent
    db.append_message("parent_sess", role="user", content="Late parent prompt")
    db.append_message("parent_sess", role="assistant", content="Late parent answer")

    model_history, display_history = db.get_resume_conversations("branch_sess")

    # The branch session should only see its own messages, not the parent's later messages
    branch_display_contents = [m["content"] for m in display_history]
    assert "Branch prompt" in branch_display_contents
    assert "Branch answer" in branch_display_contents
    assert "Late parent prompt" not in branch_display_contents


def test_replayed_user_message_deduplication(db: SessionDB):
    """Consecutive duplicate user turns across lineage boundaries are deduped in display."""
    db.create_session(session_id="parent_dedup", source="cli")
    db.append_message("parent_dedup", role="user", content="Shared user ask")

    db.create_session(session_id="child_dedup", source="cli", parent_session_id="parent_dedup")
    db.append_message("child_dedup", role="user", content="Shared user ask")
    db.append_message("child_dedup", role="assistant", content="Answer to ask")

    _, display_history = db.get_resume_conversations("child_dedup")
    user_msgs = [m for m in display_history if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "Shared user ask"
