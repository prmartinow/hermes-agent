"""Exercise recovery read boundaries against real isolated SQLite databases."""
import copy
from unittest.mock import patch

import pytest

from persistence.conversation.messages import NewMessage
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_messages, seed_session


@pytest.fixture
def recovery_db(tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, "owned", "test")
    seed_session(db, "other", "test")
    try:
        yield db
    finally:
        db.close()


def compact_history(store, sid, messages, *, tail_count=0):
    store.messages.compact(sid, [NewMessage.from_live(m) for m in messages], carried_tail_count=tail_count)


def prepare_archive(db, sid):
    history = [
        {"role": "user", "content": f"{sid}: old request"},
        {"role": "assistant", "content": f"{sid}: old answer"},
        {"role": "user", "content": f"{sid}: carried request"},
        {"role": "assistant", "content": f"{sid}: carried answer"},
    ]
    seed_messages(db, sid, *history)
    generation = [{"role": "user", "content": f"{sid}: summary", "_compressed_summary": True}]
    generation.extend(copy.deepcopy(history[-2:]))
    compact_history(db, sid, generation, tail_count=2)
    return history, generation


def test_exact_session_single_query_and_real_carried_tail(recovery_db):
    db = recovery_db
    old, generation = prepare_archive(db, "owned")
    prepare_archive(db, "other")
    with patch.object(raw_holder(db), "_read_all", wraps=raw_holder(db)._read_all) as read:
        captured = db.messages.get_compaction_recovery_messages("owned")
    assert read.call_count == 1
    assert read.call_args.args[1] == ("owned",)
    assert [m["content"] for m in captured["archived_messages"]] == [m["content"] for m in old[:2]]
    assert [m["content"] for m in captured["active_messages"]] == [m["content"] for m in generation]
    assert captured["active_messages"][0]["_compressed_summary"] is True
    assert all("other:" not in m["content"] for key in ("archived_messages", "active_messages") for m in captured[key])


def test_write_after_fetch_cannot_tear_captured_groups(recovery_db):
    db = recovery_db
    _, generation = prepare_archive(db, "owned")
    before = db.messages.get_compaction_recovery_messages("owned")
    original = raw_holder(db)._read_all
    reads = []
    def read_then_compact(sql, params):
        rows = original(sql, params)
        reads.append(sql)
        compact_history(db, "owned", [
            {"role": "user", "content": "A later generation.", "_compressed_summary": True},
        ])
        return rows
    with patch.object(raw_holder(db), "_read_all", read_then_compact):
        captured = db.messages.get_compaction_recovery_messages("owned")
    assert len(reads) == 1
    assert captured == before
    assert [m["content"] for m in captured["active_messages"]] == [m["content"] for m in generation]
    assert db.messages.get_compaction_recovery_messages("owned") != captured


@pytest.mark.parametrize("invalid", [None, True, 1, 1.0, "", [], {}])
def test_session_identifier_is_not_coerced_or_queried(recovery_db, invalid):
    with patch.object(raw_holder(recovery_db), "_read_all") as read:
        with pytest.raises(ValueError, match="non-empty string"):
            recovery_db.messages.get_compaction_recovery_messages(invalid)
        read.assert_not_called()
