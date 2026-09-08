"""Tests for full transcript display of compacted sessions in SessionDB.

Validates that get_resume_conversations() includes in-place compacted messages
(active=0, compacted=1) in display_history so the visual transcript renders
the full conversation history from Turn 1, while keeping model_history filtered
to active=1 rows for prompt caching and LLM replay. Also ensures soft-deleted
undo/rewind rows (active=0, compacted=0) remain strictly excluded.
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


def test_in_place_compaction_display_vs_model_history(db: SessionDB):
    """display_history includes compacted turns from Turn 1; model_history only active turns."""
    session_id = "compacted_sess_1"
    db.create_session(session_id=session_id, source="cli")

    # Turn 1
    db.append_message(session_id=session_id, role="user", content="Turn 1: What is Python?")
    db.append_message(session_id=session_id, role="assistant", content="Turn 1: Python is a programming language.")

    # Turn 2
    db.append_message(session_id=session_id, role="user", content="Turn 2: Explain list comprehensions.")
    db.append_message(session_id=session_id, role="assistant", content="Turn 2: List comprehensions provide a concise syntax.")

    # Perform in-place compaction
    summary_content = "Summary: User asked about Python and list comprehensions."
    compacted_payload = [
        {"role": "system", "content": summary_content},
        {"role": "user", "content": "Turn 2: Explain list comprehensions."},
        {"role": "assistant", "content": "Turn 2: List comprehensions provide a concise syntax."},
    ]
    db.archive_and_compact(session_id, compacted_payload)

    # Resume the session
    model_history, display_history = db.get_resume_conversations(session_id)

    # Model history: only active post-compaction rows
    model_contents = [m["content"] for m in model_history]
    assert "Turn 1: What is Python?" not in model_contents
    assert "Turn 1: Python is a programming language." not in model_contents
    assert summary_content in model_contents
    assert "Turn 2: Explain list comprehensions." in model_contents

    # Display history: includes full transcript starting from Turn 1
    display_contents = [m["content"] for m in display_history]
    assert display_contents[0] == "Turn 1: What is Python?"
    assert display_contents[1] == "Turn 1: Python is a programming language."
    assert "Turn 2: Explain list comprehensions." in display_contents
    assert len(display_history) >= 4


def test_preservation_of_turn_1_initial_prompts_upon_resume(db: SessionDB):
    """Turn 1 initial prompt survives at the top of display_history across compactions."""
    session_id = "sess_turn_1_preservation"
    db.create_session(session_id=session_id, source="cli")

    initial_prompt = "Build a complete microservice architecture for billing."
    db.append_message(session_id=session_id, role="user", content=initial_prompt)
    db.append_message(session_id=session_id, role="assistant", content="I will design the billing microservice architecture.")

    # Subsequent turns before compaction
    db.append_message(session_id=session_id, role="user", content="Add Stripe webhook handling.")
    db.append_message(session_id=session_id, role="assistant", content="Stripe webhook endpoint added.")

    # In-place compaction
    summary_msg = "Compacted context: Microservice architecture with Stripe webhooks established."
    compacted_set = [
        {"role": "system", "content": summary_msg},
        {"role": "user", "content": "Add Stripe webhook handling."},
        {"role": "assistant", "content": "Stripe webhook endpoint added."},
    ]
    db.archive_and_compact(session_id, compacted_set)

    # Post-compaction turn
    db.append_message(session_id=session_id, role="user", content="Now implement invoice PDF generation.")
    db.append_message(session_id=session_id, role="assistant", content="Invoice PDF generation is now implemented.")

    model_history, display_history = db.get_resume_conversations(session_id)

    # Display history begins with Turn 1 initial prompt
    assert display_history[0]["role"] == "user"
    assert display_history[0]["content"] == initial_prompt

    # Display history ends with latest post-compaction response
    assert display_history[-1]["role"] == "assistant"
    assert display_history[-1]["content"] == "Invoice PDF generation is now implemented."

    # Model history receives only active rows (summary + post-compaction)
    assert not any(m.get("content") == initial_prompt for m in model_history)
    assert model_history[-1]["content"] == "Invoice PDF generation is now implemented."


def test_exclusion_of_soft_deleted_undo_messages(db: SessionDB):
    """Soft-deleted rewind/undo rows (active=0, compacted=0) remain strictly excluded."""
    session_id = "sess_undo_exclusion"
    db.create_session(session_id=session_id, source="cli")

    # Turn 1
    db.append_message(session_id=session_id, role="user", content="Turn 1 valid prompt")
    db.append_message(session_id=session_id, role="assistant", content="Turn 1 valid reply")

    # In-place compaction
    db.archive_and_compact(
        session_id,
        [
            {"role": "system", "content": "Summary of Turn 1"},
            {"role": "user", "content": "Turn 1 valid prompt"},
            {"role": "assistant", "content": "Turn 1 valid reply"},
        ],
    )

    # Turn 2
    db.append_message(session_id=session_id, role="user", content="Turn 2 valid prompt")
    db.append_message(session_id=session_id, role="assistant", content="Turn 2 valid reply")

    # Turn 3 (mistake turn to be rewound)
    mistake_user_msg = "Turn 3 accidental bad prompt"
    db.append_message(session_id=session_id, role="user", content=mistake_user_msg)
    db.append_message(session_id=session_id, role="assistant", content="Turn 3 accidental bad reply")

    # Find ID of the mistake user message
    messages = db.get_messages(session_id=session_id, include_inactive=True)
    mistake_msg = next(m for m in messages if m["content"] == mistake_user_msg)
    mistake_id = mistake_msg["id"]

    # Perform rewind/undo to mistake message
    rewind_res = db.rewind_to_message(session_id, target_message_id=mistake_id)
    assert rewind_res["rewound_count"] >= 2

    # Verify SQLite row flags directly:
    # Turn 1 messages: active=0, compacted=1 (compacted)
    # Turn 3 messages: active=0, compacted=0 (soft-deleted undo)
    with db._read_ctx() as conn:
        assert conn is not None
        compacted_rows = conn.execute(
            "SELECT content, active, compacted FROM messages WHERE session_id = ? AND compacted = 1",
            (session_id,),
        ).fetchall()
        undone_rows = conn.execute(
            "SELECT content, active, compacted FROM messages WHERE session_id = ? AND active = 0 AND compacted = 0",
            (session_id,),
        ).fetchall()

    assert any(r["content"] == "Turn 1 valid prompt" for r in compacted_rows)
    assert any(r["content"] == mistake_user_msg for r in undone_rows)
    assert any(r["content"] == "Turn 3 accidental bad reply" for r in undone_rows)

    # Resume the session
    model_history, display_history = db.get_resume_conversations(session_id)

    # Neither model_history nor display_history should contain undone/rewound messages
    for msg in model_history:
        assert msg.get("content") != mistake_user_msg
        assert msg.get("content") != "Turn 3 accidental bad reply"

    for msg in display_history:
        assert msg.get("content") != mistake_user_msg
        assert msg.get("content") != "Turn 3 accidental bad reply"

    # display_history still contains valid Turn 1 (compacted) and Turn 2 (active)
    display_contents = [m["content"] for m in display_history]
    assert "Turn 1 valid prompt" in display_contents
    assert "Turn 2 valid prompt" in display_contents


def test_resume_viewport_conversations_with_compacted_messages(db: SessionDB):
    """get_resume_viewport_conversations counts and slices active=1 OR compacted=1 rows."""
    session_id = "sess_viewport_compacted"
    db.create_session(session_id=session_id, source="cli")

    for i in range(1, 6):
        db.append_message(session_id=session_id, role="user", content=f"User msg {i}")
        db.append_message(session_id=session_id, role="assistant", content=f"Assistant reply {i}")

    # Compact turns 1-3 away
    compacted_set = [
        {"role": "system", "content": "Summary of msgs 1-3"},
        {"role": "user", "content": "User msg 4"},
        {"role": "assistant", "content": "Assistant reply 4"},
        {"role": "user", "content": "User msg 5"},
        {"role": "assistant", "content": "Assistant reply 5"},
    ]
    db.archive_and_compact(session_id, compacted_set)

    # Add a soft-deleted undo turn
    db.append_message(session_id=session_id, role="user", content="Mistake to undo")
    db.append_message(session_id=session_id, role="assistant", content="Mistake reply to undo")
    all_msgs = db.get_messages(session_id, include_inactive=True)
    undo_target = next(m for m in all_msgs if m["content"] == "Mistake to undo")
    db.rewind_to_message(session_id, undo_target["id"])

    total_count, viewport = db.get_resume_viewport_conversations(session_id, viewport_limit=20)

    # Total count includes compacted rows and active rows, but excludes soft-deleted rows
    # 10 original rows (6 compacted, 4 archived during compact) + 5 newly inserted active rows = 15 rows with active=1 OR compacted=1
    assert total_count == 15
    viewport_contents = [m["content"] for m in viewport]
    assert "User msg 1" in viewport_contents
    assert "Mistake to undo" not in viewport_contents



def test_get_messages_with_ancestors_and_compaction(db: SessionDB):
    """get_messages with include_ancestors=True retrieves full lineage across parent & child sessions."""
    parent_id = "sess_lineage_parent"
    child_id = "sess_lineage_child"
    db.create_session(session_id=parent_id, source="cli")

    # Parent turns
    db.append_message(session_id=parent_id, role="user", content="Parent Turn 1: Project init")
    db.append_message(session_id=parent_id, role="assistant", content="Parent Turn 1: Initialized project")

    # Child session linked to parent
    db.create_session(session_id=child_id, source="cli", parent_session_id=parent_id)
    db.append_message(session_id=child_id, role="user", content="Child Turn 2: Add API routes")
    db.append_message(session_id=child_id, role="assistant", content="Child Turn 2: Added API routes")

    # In-place compact child session
    db.archive_and_compact(
        child_id,
        [
            {"role": "system", "content": "Summary of prior turns"},
            {"role": "user", "content": "Child Turn 2: Add API routes"},
            {"role": "assistant", "content": "Child Turn 2: Added API routes"},
        ],
    )
    db.append_message(session_id=child_id, role="user", content="Child Turn 3: Add database sink")
    db.append_message(session_id=child_id, role="assistant", content="Child Turn 3: Added database sink")

    # Default read on child returns only active rows of child
    active_msgs = db.get_messages(child_id)
    active_contents = [m["content"] for m in active_msgs]
    assert "Parent Turn 1: Project init" not in active_contents

    # Ancestor + Compacted read returns complete history from parent Turn 1 to child Turn 3
    full_msgs = db.get_messages(child_id, include_compacted=True, include_ancestors=True)
    full_contents = [m["content"] for m in full_msgs]

    assert full_contents[0] == "Parent Turn 1: Project init"
    assert "Child Turn 2: Add API routes" in full_contents
    assert full_contents[-1] == "Child Turn 3: Added database sink"
