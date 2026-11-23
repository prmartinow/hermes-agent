"""Tests for full transcript display of compacted sessions in the profile store.

Validates that get_resume_conversations() includes in-place compacted messages
(active=0, compacted=1) in display_history so the visual transcript renders
the full conversation history from Turn 1, while keeping model_history filtered
to active=1 rows for prompt caching and LLM replay. Also ensures soft-deleted
undo/rewind rows (active=0, compacted=0) remain strictly excluded.
"""

from __future__ import annotations

import pytest
from persistence.conversation.messages import NewMessage, TranscriptRead, TranscriptView
from persistence_assembly import Persistence, open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session


@pytest.fixture()
def db(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as store:
        yield store


def compact(db, session_id, messages):
    db.messages.compact(session_id, [NewMessage.from_live(message) for message in messages])


def resume(db, session_id):
    result = db.messages.read_resume_conversations(session_id)
    return ([m.as_openai_message() for m in result.model], [m.as_openai_message() for m in result.display])


def test_in_place_compaction_display_vs_model_history(db: Persistence):
    """display_history includes compacted turns from Turn 1; model_history only active turns."""
    session_id = "compacted_sess_1"
    seed_session(db, session_id, "cli")

    # Turn 1
    db.messages.append(session_id, NewMessage(role="user", content="Turn 1: What is Python?"))
    db.messages.append(session_id, NewMessage(role="assistant", content="Turn 1: Python is a programming language."))

    # Turn 2
    db.messages.append(session_id, NewMessage(role="user", content="Turn 2: Explain list comprehensions."))
    db.messages.append(session_id, NewMessage(role="assistant", content="Turn 2: List comprehensions provide a concise syntax."))

    # Perform in-place compaction
    summary_content = "Summary: User asked about Python and list comprehensions."
    compacted_payload = [
        {"role": "system", "content": summary_content},
        {"role": "user", "content": "Turn 2: Explain list comprehensions."},
        {"role": "assistant", "content": "Turn 2: List comprehensions provide a concise syntax."},
    ]
    compact(db, session_id, compacted_payload)

    # Resume the session
    model_history, display_history = resume(db, session_id)

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


def test_preservation_of_turn_1_initial_prompts_upon_resume(db: Persistence):
    """Turn 1 initial prompt survives at the top of display_history across compactions."""
    session_id = "sess_turn_1_preservation"
    seed_session(db, session_id, "cli")

    initial_prompt = "Build a complete microservice architecture for billing."
    db.messages.append(session_id, NewMessage(role="user", content=initial_prompt))
    db.messages.append(session_id, NewMessage(role="assistant", content="I will design the billing microservice architecture."))

    # Subsequent turns before compaction
    db.messages.append(session_id, NewMessage(role="user", content="Add Stripe webhook handling."))
    db.messages.append(session_id, NewMessage(role="assistant", content="Stripe webhook endpoint added."))

    # In-place compaction
    summary_msg = "Compacted context: Microservice architecture with Stripe webhooks established."
    compacted_set = [
        {"role": "system", "content": summary_msg},
        {"role": "user", "content": "Add Stripe webhook handling."},
        {"role": "assistant", "content": "Stripe webhook endpoint added."},
    ]
    compact(db, session_id, compacted_set)

    # Post-compaction turn
    db.messages.append(session_id, NewMessage(role="user", content="Now implement invoice PDF generation."))
    db.messages.append(session_id, NewMessage(role="assistant", content="Invoice PDF generation is now implemented."))

    model_history, display_history = resume(db, session_id)

    # Display history begins with Turn 1 initial prompt
    assert display_history[0]["role"] == "user"
    assert display_history[0]["content"] == initial_prompt

    # Display history ends with latest post-compaction response
    assert display_history[-1]["role"] == "assistant"
    assert display_history[-1]["content"] == "Invoice PDF generation is now implemented."

    # Model history receives only active rows (summary + post-compaction)
    assert not any(m.get("content") == initial_prompt for m in model_history)
    assert model_history[-1]["content"] == "Invoice PDF generation is now implemented."


def test_exclusion_of_soft_deleted_undo_messages(db: Persistence):
    """Soft-deleted rewind/undo rows (active=0, compacted=0) remain strictly excluded."""
    session_id = "sess_undo_exclusion"
    seed_session(db, session_id, "cli")

    # Turn 1
    db.messages.append(session_id, NewMessage(role="user", content="Turn 1 valid prompt"))
    db.messages.append(session_id, NewMessage(role="assistant", content="Turn 1 valid reply"))

    # In-place compaction
    compact(
        db,
        session_id,
        [
            {"role": "system", "content": "Summary of Turn 1"},
            {"role": "user", "content": "Turn 1 valid prompt"},
            {"role": "assistant", "content": "Turn 1 valid reply"},
        ],
    )

    # Turn 2
    db.messages.append(session_id, NewMessage(role="user", content="Turn 2 valid prompt"))
    db.messages.append(session_id, NewMessage(role="assistant", content="Turn 2 valid reply"))

    # Turn 3 (mistake turn to be rewound)
    mistake_user_msg = "Turn 3 accidental bad prompt"
    db.messages.append(session_id, NewMessage(role="user", content=mistake_user_msg))
    db.messages.append(session_id, NewMessage(role="assistant", content="Turn 3 accidental bad reply"))

    # Rewind the latest visible user turn and its assistant reply.
    rewind_res = db.messages.rewind_user_turn(session_id, 2)
    assert rewind_res.rewound_count >= 2

    # Verify SQLite row flags directly:
    # Turn 1 messages: active=0, compacted=1 (compacted)
    # Turn 3 messages: active=0, compacted=0 (soft-deleted undo)
    with raw_holder(db)._read_ctx() as conn:
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
    model_history, display_history = resume(db, session_id)

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


def test_get_messages_with_ancestors_and_compaction(db: Persistence):
    """get_messages with include_ancestors=True retrieves full lineage across parent & child sessions."""
    parent_id = "sess_lineage_parent"
    child_id = "sess_lineage_child"
    seed_session(db, parent_id, "cli")

    # Parent turns
    db.messages.append(parent_id, NewMessage(role="user", content="Parent Turn 1: Project init"))
    db.messages.append(parent_id, NewMessage(role="assistant", content="Parent Turn 1: Initialized project"))

    # Child session linked to parent
    db.sessions.end(parent_id, "compression")
    seed_session(db, child_id, "cli", parent_session_id=parent_id)
    db.messages.append(child_id, NewMessage(role="user", content="Child Turn 2: Add API routes"))
    db.messages.append(child_id, NewMessage(role="assistant", content="Child Turn 2: Added API routes"))

    # In-place compact child session
    compact(
        db,
        child_id,
        [
            {"role": "system", "content": "Summary of prior turns"},
            {"role": "user", "content": "Child Turn 2: Add API routes"},
            {"role": "assistant", "content": "Child Turn 2: Added API routes"},
        ],
    )
    db.messages.append(child_id, NewMessage(role="user", content="Child Turn 3: Add database sink"))
    db.messages.append(child_id, NewMessage(role="assistant", content="Child Turn 3: Added database sink"))

    # Default read on child returns only active rows of child
    active_msgs = transcript_rows(db, child_id)
    active_contents = [m["content"] for m in active_msgs]
    assert "Parent Turn 1: Project init" not in active_contents

    # Ancestor + Compacted read returns complete history from parent Turn 1 to child Turn 3
    full_msgs = transcript_rows(db, child_id, TranscriptRead(view=TranscriptView.DISPLAY, include_lineage=True))
    full_contents = [m["content"] for m in full_msgs]

    assert full_contents[0] == "Parent Turn 1: Project init"
    assert "Child Turn 2: Add API routes" in full_contents
    assert full_contents[-1] == "Child Turn 3: Added database sink"
