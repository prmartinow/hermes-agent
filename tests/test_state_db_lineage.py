"""Tests for session lineage traversal and multi-generation resume in SessionDB.

Validates that SessionDB correctly computes root-to-tip lineages, isolates
explicit branch sessions, dedupes replayed user turns across ancestor
boundaries, and provides coherent model_history vs. display_history across
lineage chains.
"""

from __future__ import annotations

import pytest
from persistence.conversation.sessions import SessionLineageKind
from persistence_assembly import Persistence, open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session


@pytest.fixture()
def db(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as store:
        yield store


def resume(db, session_id):
    result = db.messages.read_resume_conversations(session_id)
    return ([m.as_openai_message() for m in result.model], [m.as_openai_message() for m in result.display])


def test_session_lineage_root_to_tip_traversal(db: Persistence):
    """Lineage correctly traverses parent_session_id pointers from root to tip."""
    seed_session(db, "root_sess", source="cli")
    seed_session(db, "child_sess", source="cli", parent_session_id="root_sess")
    seed_session(db, "grandchild_sess", source="cli", parent_session_id="child_sess")

    db.sessions.end("root_sess", "compression")
    db.sessions.end("child_sess", "compression")
    assert db.lineage.resume_lineage_ids("grandchild_sess") == ("root_sess", "child_sess", "grandchild_sess")
    assert db.lineage.conversation_root("grandchild_sess") == "root_sess"


def test_lineage_resume_model_vs_display_history(db: Persistence):
    """Resume across lineage serves only tip active rows to model, all ancestors to display."""
    seed_session(db, "gen1", source="cli")
    seed_messages(db, "gen1", {"role": "user", "content": "Gen 1 prompt"})
    seed_messages(db, "gen1", {"role": "assistant", "content": "Gen 1 reply"})

    db.sessions.end("gen1", "compression")
    seed_session(db, "gen2", source="cli", parent_session_id="gen1")
    seed_messages(db, "gen2", {"role": "user", "content": "Gen 2 prompt"})
    seed_messages(db, "gen2", {"role": "assistant", "content": "Gen 2 reply"})

    model_history, display_history = resume(db, "gen2")

    # model_history is strictly gen2 (the tip)
    model_contents = [m["content"] for m in model_history]
    assert "Gen 1 prompt" not in model_contents
    assert "Gen 1 reply" not in model_contents
    assert "Gen 2 prompt" in model_contents
    assert "Gen 2 reply" in model_contents

    # display_history includes gen1 (ancestor) + gen2 (tip)
    display_contents = [m["content"] for m in display_history]
    assert display_contents == ["Gen 1 prompt", "Gen 1 reply", "Gen 2 prompt", "Gen 2 reply"]


def test_explicit_branch_session_isolated_from_parent_lineage(db: Persistence):
    """Explicit branch sessions (_branched_from) are isolated from parent message stream."""
    seed_session(db, "parent_sess", source="cli")
    seed_messages(db, "parent_sess", {"role": "user", "content": "Initial prompt"})
    seed_messages(db, "parent_sess", {"role": "assistant", "content": "Initial answer"})

    # Create explicit branch
    seed_session(
        db,
        "branch_sess",
        source="cli",
        parent_session_id="parent_sess",
        lineage_kind=SessionLineageKind.BRANCH,
    )
    seed_messages(db, "branch_sess", {"role": "user", "content": "Branch prompt"})
    seed_messages(db, "branch_sess", {"role": "assistant", "content": "Branch answer"})

    # Post-branch append on parent
    seed_messages(db, "parent_sess", {"role": "user", "content": "Late parent prompt"})
    seed_messages(db, "parent_sess", {"role": "assistant", "content": "Late parent answer"})

    _model_history, display_history = resume(db, "branch_sess")

    # The branch session should only see its own messages, not the parent's later messages
    branch_display_contents = [m["content"] for m in display_history]
    assert "Branch prompt" in branch_display_contents
    assert "Branch answer" in branch_display_contents
    assert "Late parent prompt" not in branch_display_contents


def test_replayed_user_message_deduplication(db: Persistence):
    """Consecutive duplicate user turns across lineage boundaries are deduped in display."""
    seed_session(db, "parent_dedup", source="cli")
    seed_messages(db, "parent_dedup", {"role": "user", "content": "Shared user ask"})

    db.sessions.end("parent_dedup", "compression")
    seed_session(db, "child_dedup", source="cli", parent_session_id="parent_dedup")
    seed_messages(db, "child_dedup", {"role": "user", "content": "Shared user ask"})
    seed_messages(db, "child_dedup", {"role": "assistant", "content": "Answer to ask"})

    _, display_history = resume(db, "child_dedup")
    user_msgs = [m for m in display_history if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "Shared user ask"
