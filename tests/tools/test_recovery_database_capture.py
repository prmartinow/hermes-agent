"""Exercise recovery read boundaries against real isolated SQLite databases."""
import copy
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


@pytest.fixture
def recovery_db(tmp_path):
    db = SessionDB(tmp_path / "capture.db")
    db.create_session("owned", source="test")
    db.create_session("other", source="test")
    try:
        yield db
    finally:
        db.close()


def prepare_archive(db, sid):
    history = [
        {"role": "user", "content": f"{sid}: old request"},
        {"role": "assistant", "content": f"{sid}: old answer"},
        {"role": "user", "content": f"{sid}: carried request"},
        {"role": "assistant", "content": f"{sid}: carried answer"},
    ]
    db.append_messages_batch(sid, history)
    generation = [{"role": "user", "content": f"{sid}: summary", "_compressed_summary": True}]
    generation.extend(copy.deepcopy(history[-2:]))
    db.archive_and_compact(sid, generation, tail_count=2)
    return history, generation


def test_exact_session_single_query_and_real_carried_tail(recovery_db):
    db = recovery_db
    old, generation = prepare_archive(db, "owned")
    prepare_archive(db, "other")
    with patch.object(db, "_read_all", wraps=db._read_all) as read:
        captured = db.get_compaction_recovery_messages("owned")
    assert read.call_count == 1
    assert read.call_args.args[1] == ("owned",)
    assert [m["content"] for m in captured["archived_messages"]] == [m["content"] for m in old[:2]]
    assert [m["content"] for m in captured["active_messages"]] == [m["content"] for m in generation]
    assert captured["active_messages"][0]["_compressed_summary"] is True
    assert all("other:" not in m["content"] for key in ("archived_messages", "active_messages") for m in captured[key])


def test_write_after_fetch_cannot_tear_captured_groups(recovery_db):
    db = recovery_db
    _, generation = prepare_archive(db, "owned")
    before = db.get_compaction_recovery_messages("owned")
    original = db._read_all
    reads = []
    def read_then_compact(sql, params):
        rows = original(sql, params)
        reads.append(sql)
        db.archive_and_compact("owned", [
            {"role": "user", "content": "A later generation.", "_compressed_summary": True},
        ])
        return rows
    with patch.object(db, "_read_all", read_then_compact):
        captured = db.get_compaction_recovery_messages("owned")
    assert len(reads) == 1
    assert captured == before
    assert [m["content"] for m in captured["active_messages"]] == [m["content"] for m in generation]
    assert db.get_compaction_recovery_messages("owned") != captured


@pytest.mark.parametrize("invalid", [None, True, 1, 1.0, "", [], {}])
def test_session_identifier_is_not_coerced_or_queried(recovery_db, invalid):
    with patch.object(recovery_db, "_read_all") as read:
        with pytest.raises(ValueError, match="non-empty string"):
            recovery_db.get_compaction_recovery_messages(invalid)
        read.assert_not_called()
