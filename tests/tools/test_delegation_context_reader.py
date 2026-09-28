"""Focused unit tests for snapshot-scoped session search reader foundation.

Verifies:
- Deterministic 1-based chronological immutable records (frozen dataclasses, no mutable dicts).
- Live parent appends do not affect existing detached snapshots.
- Exact multi-page reassembly of long Unicode strings without permanent 2000-char truncation.
- Strict input validation (bool rejection, non-negative bounds, window=0 constraint on content_offset).
- Explicit rejection of unsupported snapshot parameters (profile, sort, after, before, exclude_session_ids).
- Literal search safety (no regex compilation or ReDoS) and accurate zero-match reporting without loops.
- Multi-record window navigation and aggregate budget continuation.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from tools.delegation_context import (
    ContextSnapshot,
    SnapshotManifest,
    SnapshotRecord,
    build_delegation_context_snapshot,
)
from tools.session_search_tool import session_search


def _make_sample_snapshot(records: list[SnapshotRecord], snapshot_id: str = "snap-test-12345") -> ContextSnapshot:
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        source_type="live_session_messages",
        content_hash_sha256="abc123def4567890" * 4,
        char_count=sum(len(r.text) for r in records),
        estimated_tokens=sum(len(r.text) for r in records) // 4,
        token_budget=64000,
        retained_messages_count=len(records),
        retained_tool_events_count=sum(1 for r in records if r.role == "tool"),
        omitted_system_messages_count=0,
        omitted_sidecars_count=0,
        omitted_scaffolding_count=0,
        omitted_images_count=0,
        omitted_unsupported_blocks_count=0,
        omitted_orphan_tool_results_count=0,
    )
    return ContextSnapshot(
        manifest=manifest,
        rendered_transcript="[HISTORICAL CONTEXT]",
        records=tuple(records),
    )


def test_immutable_records_chronological_data():
    """Records are deterministic 1-based, frozen dataclasses, preventing mutation."""
    parent = SimpleNamespace(
        _session_messages=[
            {"role": "user", "content": "First instruction"},
            {"role": "assistant", "content": "Working on it..."},
            {"role": "user", "content": "Second instruction"},
        ]
    )
    snapshot = build_delegation_context_snapshot(parent)
    assert isinstance(snapshot.records, tuple)
    assert len(snapshot.records) == 3

    # Check deterministic 1-based IDs and ordering
    for idx, rec in enumerate(snapshot.records, start=1):
        assert rec.record_id == idx
        assert rec.id == idx
        assert isinstance(rec, SnapshotRecord)

    assert snapshot.records[0].role == "user"
    assert snapshot.records[0].text == "First instruction"
    assert snapshot.records[1].role == "assistant"
    assert snapshot.records[2].role == "user"
    assert snapshot.records[2].text == "Second instruction"

    # Immutability check: cannot mutate attributes
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.records[0].text = "Mutated"

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.records = ()


def test_live_parent_append_does_not_change_snapshot():
    """Live parent modifications after snapshot capture do not bleed into the snapshot."""
    parent_messages = [
        {"role": "user", "content": "Original user goal"},
        {"role": "assistant", "content": "Initial response"},
    ]
    parent = SimpleNamespace(_session_messages=parent_messages)
    snapshot = build_delegation_context_snapshot(parent)

    assert len(snapshot.records) == 2
    original_char_count = snapshot.manifest.char_count

    # Live parent appends new conversation turns
    parent_messages.append({"role": "user", "content": "Subsequent live instruction"})
    parent_messages.append({"role": "assistant", "content": "Live answer"})

    # Snapshot remains strictly detached and unchanged
    assert len(snapshot.records) == 2
    assert snapshot.records[-1].text == "Initial response"
    assert snapshot.manifest.char_count == original_char_count


def test_exact_pagination_reassembly_long_unicode_string():
    """Large Unicode message (>25,000 chars) is completely reassembled across pages with window=0."""
    # Build a rich multi-byte Unicode string with CJK, emojis, math symbols, accents
    base_phrase = "🚀 Hermes inherited context 🌟 | 汉语/日本語/한국어 | café naïve façade | ∑_{i=1}^N x_i ≈ ∞ 🎯 \n"
    target_length = 26000
    repeats = (target_length // len(base_phrase)) + 1
    original_unicode_text = (base_phrase * repeats)[:target_length]

    records = [
        SnapshotRecord(record_id=1, role="user", text=original_unicode_text),
        SnapshotRecord(record_id=2, role="assistant", text="Acknowledged."),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-unicode-page-test")
    sid = f"snapshot:{snapshot.manifest.snapshot_id}"

    reassembled_parts: list[str] = []
    current_offset = 0
    page_size = 4000
    page_count = 0

    while True:
        res_raw = session_search(
            session_id=sid,
            around_message_id=1,
            window=0,
            content_offset=current_offset,
            max_chars=page_size,
            snapshot=snapshot,
        )
        res = json.loads(res_raw)
        assert res["success"] is True, res
        assert res["mode"] == "snapshot_record"
        assert res["around_message_id"] == 1
        assert len(res["messages"]) == 1

        msg = res["messages"][0]
        assert msg["id"] == 1
        assert msg["role"] == "user"
        assert msg["content_offset"] == current_offset
        assert msg["total_chars"] == len(original_unicode_text)

        chunk = msg["content"]
        assert len(chunk) == msg["content_length"]
        reassembled_parts.append(chunk)
        page_count += 1

        if not res["has_more"]:
            assert res.get("next_content_offset") is None
            assert msg.get("truncated") is False
            break

        assert res.get("next_content_offset") is not None
        assert msg.get("truncated") is True
        current_offset = res["next_content_offset"]

    reassembled_text = "".join(reassembled_parts)
    assert len(reassembled_text) == len(original_unicode_text)
    assert reassembled_text == original_unicode_text
    assert page_count > 1  # Proves multi-page traversal occurred without permanent 2000-char cutoff


def test_invalid_window_offset_flags():
    """Validates negative bounds, bool inputs, and window=0 constraint for content_offset."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Hello world"),
        SnapshotRecord(record_id=2, role="assistant", text="Hi there"),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-validation-test")
    sid = f"snapshot:{snapshot.manifest.snapshot_id}"

    # content_offset > 0 without window=0
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=5, content_offset=10, snapshot=snapshot))
    assert res["success"] is False
    assert "window=0" in res["error"]

    # content_offset > 0 with no around_message_id
    res = json.loads(session_search(session_id=sid, content_offset=10, snapshot=snapshot))
    assert res["success"] is False
    assert "window=0" in res["error"]

    # Negative content_offset
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=0, content_offset=-5, snapshot=snapshot))
    assert res["success"] is False
    assert "greater than or equal to 0" in res["error"]

    # Boolean content_offset
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=0, content_offset=True, snapshot=snapshot))
    assert res["success"] is False
    assert "boolean" in res["error"]

    # Boolean around_message_id
    res = json.loads(session_search(session_id=sid, around_message_id=True, snapshot=snapshot))
    assert res["success"] is False
    assert "boolean" in res["error"]

    # Negative around_message_id
    res = json.loads(session_search(session_id=sid, around_message_id=-1, snapshot=snapshot))
    assert res["success"] is False
    assert "greater than or equal to 1" in res["error"]

    # Negative window
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=-2, snapshot=snapshot))
    assert res["success"] is False
    assert "greater than or equal to 0" in res["error"]

    # Boolean window
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=False, snapshot=snapshot))
    assert res["success"] is False
    assert "boolean" in res["error"]

    # Non-positive max_chars
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=0, max_chars=0, snapshot=snapshot))
    assert res["success"] is False
    assert "positive integer" in res["error"]

    # Boolean max_chars
    res = json.loads(session_search(session_id=sid, around_message_id=1, window=0, max_chars=True, snapshot=snapshot))
    assert res["success"] is False
    assert "boolean" in res["error"]


def test_unsupported_options_rejected():
    """Unsupported snapshot-only filtering options fail closed with explicit errors."""
    snapshot = _make_sample_snapshot([SnapshotRecord(record_id=1, role="user", text="Test")], snapshot_id="snap-reject-opts")
    sid = f"snapshot:{snapshot.manifest.snapshot_id}"

    # sort rejected
    res = json.loads(session_search(session_id=sid, sort="newest", snapshot=snapshot))
    assert res["success"] is False
    assert "sort" in res["error"]

    # after rejected
    res = json.loads(session_search(session_id=sid, after="2026-01-01", snapshot=snapshot))
    assert res["success"] is False
    assert "after" in res["error"]

    # before rejected
    res = json.loads(session_search(session_id=sid, before="2026-01-02", snapshot=snapshot))
    assert res["success"] is False
    assert "before" in res["error"]

    # exclude_session_ids rejected
    res = json.loads(session_search(session_id=sid, exclude_session_ids=["s-other"], snapshot=snapshot))
    assert res["success"] is False
    assert "exclude_session_ids" in res["error"]

    # profile rejected
    res = json.loads(session_search(session_id=sid, profile="other-profile", snapshot=snapshot))
    assert res["success"] is False
    assert "profile" in res["error"]


def test_literal_search_and_empty_search():
    """Search matches literal text (not regex) and accurately reports empty hits without loops."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Config regex: [a-z]+.*$ matches patterns"),
        SnapshotRecord(record_id=2, role="assistant", text="Here is a regular expression example: [0-9]+"),
        SnapshotRecord(record_id=3, role="tool", text="Result from scanner: ok"),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-literal-search")
    sid = f"snapshot:{snapshot.manifest.snapshot_id}"

    # Literal search with regex metacharacters
    raw = session_search(session_id=sid, query="[a-z]+.*$", snapshot=snapshot)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["total_matches"] == 1
    assert res["results"][0]["id"] == 1
    assert "[a-z]+.*$" in res["results"][0]["snippet"]

    # Empty search query match
    raw_empty = session_search(session_id=sid, query="nonexistent query string that does not appear", snapshot=snapshot)
    res_empty = json.loads(raw_empty)
    assert res_empty["success"] is True
    assert res_empty["total_matches"] == 0
    assert res_empty["count"] == 0
    assert res_empty["results"] == []
    assert res_empty["has_more"] is False
    assert res_empty.get("next_around_message_id") is None


def test_multi_record_scroll_aggregate_budget():
    """Scroll navigation across records honors aggregate budget and returns continuation."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Message 1 " * 100),       # ~1000 chars
        SnapshotRecord(record_id=2, role="assistant", text="Message 2 " * 100),  # ~1000 chars
        SnapshotRecord(record_id=3, role="user", text="Message 3 " * 100),       # ~1000 chars
        SnapshotRecord(record_id=4, role="assistant", text="Message 4 " * 100),  # ~1000 chars
        SnapshotRecord(record_id=5, role="user", text="Message 5 " * 100),       # ~1000 chars
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-scroll-budget")
    sid = f"snapshot:{snapshot.manifest.snapshot_id}"

    # Window 2 around message 3 -> records 1..5. Budget 2500 chars (fits ~2.5 messages).
    raw = session_search(session_id=sid, around_message_id=3, window=2, max_chars=2500, snapshot=snapshot)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["mode"] == "snapshot_scroll"
    assert res["has_more"] is True
    assert res["next_around_message_id"] is not None

    total_chars_returned = sum(len(m["content"]) for m in res["messages"])
    assert total_chars_returned <= 2500


def test_exact_budget_boundary_no_empty_skip():
    """When budget hits exactly 0 at record boundary, does not append empty chunk or skip record."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="A" * 100),
        SnapshotRecord(record_id=2, role="assistant", text="B" * 100),
        SnapshotRecord(record_id=3, role="user", text="C" * 100),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-exact-budget")
    sid = "snapshot"

    # Window scroll around 1, window 2 -> records 1, 2, 3.
    # Budget is exactly 100 chars (fits record 1 exactly, leaves 0).
    raw = session_search(session_id=sid, around_message_id=1, window=2, max_chars=100, snapshot=snapshot)
    res = json.loads(raw)
    assert res["success"] is True
    assert len(res["messages"]) == 1
    assert res["messages"][0]["id"] == 1
    assert res["messages"][0]["content"] == "A" * 100
    assert res["has_more"] is True
    assert res["next_around_message_id"] == 2
    assert res.get("next_content_offset") is None
    assert res["next_call"]["start_message_id"] == 2
    assert "around_message_id" not in res["next_call"]

    # Follow next_call to fetch record 2
    raw2 = session_search(snapshot=snapshot, **dict(res["next_call"], max_chars=100))
    res2 = json.loads(raw2)
    assert res2["success"] is True
    assert len(res2["messages"]) == 1
    assert res2["messages"][0]["id"] == 2
    assert res2["messages"][0]["content"] == "B" * 100


def test_empty_record_and_offset_contracts():
    """Empty records allow offset=0 but reject offset > 0."""
    records = [
        SnapshotRecord(record_id=1, role="user", text=""),
        SnapshotRecord(record_id=2, role="assistant", text="Non-empty"),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-empty-rec")
    sid = "snapshot"

    # offset=0 on empty record succeeds
    raw = session_search(session_id=sid, around_message_id=1, window=0, content_offset=0, snapshot=snapshot)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["messages"][0]["content"] == ""
    assert res["messages"][0]["total_chars"] == 0
    assert res["has_more"] is False

    # offset > 0 on empty record fails
    raw_err = session_search(session_id=sid, around_message_id=1, window=0, content_offset=5, snapshot=snapshot)
    res_err = json.loads(raw_err)
    assert res_err["success"] is False
    assert "exceeds message length 0" in res_err["error"]


def test_unicode_original_offset_fidelity_and_regex_literal():
    """Unicode search preserves original character offsets and treats regex metacharacters literally."""
    text = "Prefix İstanbul has regex pattern .* in text"
    records = [
        SnapshotRecord(record_id=1, role="user", text=text),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-unicode-pos")
    sid = "snapshot"

    # Search for 'stan' (case-insensitive inside İstanbul)
    raw = session_search(session_id=sid, query="stan", snapshot=snapshot)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["total_matches"] == 1
    match = res["results"][0]
    expected_pos = text.find("stan")  # Pos in ORIGINAL text
    assert match["match_position"] == expected_pos
    assert "İstanbul" in match["snippet"]

    # Search for literal '.*'
    raw_regex = session_search(session_id=sid, query=".*", snapshot=snapshot)
    res_regex = json.loads(raw_regex)
    assert res_regex["success"] is True
    assert res_regex["total_matches"] == 1
    match_regex = res_regex["results"][0]
    expected_regex_pos = text.find(".*")
    assert match_regex["match_position"] == expected_regex_pos
    assert ".*" in match_regex["snippet"]


def test_search_matches_pagination_and_budget():
    """Search matches > limit paginate via query + around_message_id without routing to window mode."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Target match alpha"),
        SnapshotRecord(record_id=2, role="assistant", text="Irrelevant message"),
        SnapshotRecord(record_id=3, role="user", text="Target match beta"),
        SnapshotRecord(record_id=4, role="assistant", text="Target match gamma"),
        SnapshotRecord(record_id=5, role="user", text="Target match delta"),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-search-paging")
    sid = "snapshot"

    # Page 1: limit 2
    raw1 = session_search(session_id=sid, query="Target", limit=2, snapshot=snapshot)
    res1 = json.loads(raw1)
    assert res1["success"] is True
    assert res1["mode"] == "snapshot_search"
    assert res1["total_matches"] == 4
    assert res1["count"] == 2
    assert [r["id"] for r in res1["results"]] == [1, 3]
    assert res1["has_more"] is True
    assert res1["next_around_message_id"] == 4
    assert "messages" not in res1  # No duplicate messages alias in search payload
    assert res1["next_call"]["around_message_id"] == 4
    assert res1["next_call"]["query"] == "Target"

    # Page 2: continue using next_call (query + around_message_id=4)
    raw2 = session_search(**res1["next_call"], snapshot=snapshot)
    res2 = json.loads(raw2)
    assert res2["success"] is True
    assert res2["mode"] == "snapshot_search"  # Stays in search mode!
    assert [r["id"] for r in res2["results"]] == [4, 5]
    assert res2["has_more"] is False
    assert "next_around_message_id" not in res2
    assert "next_call" not in res2


def test_snapshot_refs_validation_and_alias():
    """Tests session_id='snapshot' alias, exact matching, whitespace errors, foreign refs, and fake snapshot."""
    records = [SnapshotRecord(record_id=1, role="user", text="Hello")]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-exact-id-123")

    # Own snapshot alias 'snapshot' succeeds
    raw_own = session_search(session_id="snapshot", snapshot=snapshot)
    res_own = json.loads(raw_own)
    assert res_own["success"] is True
    assert res_own["snapshot_id"] == "snap-exact-id-123"

    # Exact matching ref 'snapshot:snap-exact-id-123' succeeds
    raw_exact = session_search(session_id="snapshot:snap-exact-id-123", snapshot=snapshot)
    res_exact = json.loads(raw_exact)
    assert res_exact["success"] is True

    # Foreign ref fails
    raw_foreign = session_search(session_id="snapshot:other-snap-999", snapshot=snapshot)
    res_foreign = json.loads(raw_foreign)
    assert res_foreign["success"] is False
    assert "does not match" in res_foreign["error"]

    # Whitespace-wrapped refs route to explicit error
    raw_ws1 = session_search(session_id="  snapshot  ", snapshot=snapshot)
    res_ws1 = json.loads(raw_ws1)
    assert res_ws1["success"] is False
    assert "whitespace" in res_ws1["error"]

    raw_ws2 = session_search(session_id=" snapshot:snap-exact-id-123 ", snapshot=snapshot)
    res_ws2 = json.loads(raw_ws2)
    assert res_ws2["success"] is False
    assert "whitespace" in res_ws2["error"]

    # Missing snapshot fails closed
    raw_none = session_search(session_id="snapshot", snapshot=None)
    res_none = json.loads(raw_none)
    assert res_none["success"] is False
    assert "No inherited snapshot is attached" in res_none["error"]

    # Fake snapshot (not ContextSnapshot) fails closed
    fake = {"manifest": snapshot.manifest, "records": snapshot.records}
    raw_fake = session_search(session_id="snapshot", snapshot=fake)
    res_fake = json.loads(raw_fake)
    assert res_fake["success"] is False
    assert "ContextSnapshot" in res_fake["error"]


def test_malformed_numeric_and_unsupported_types():
    """Validates that non-int numbers (floats, strings) and malformed arguments error cleanly."""
    records = [SnapshotRecord(record_id=1, role="user", text="Test")]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-types-test")
    sid = "snapshot"

    # Float/string window rejected
    res = json.loads(session_search(session_id=sid, window="5", snapshot=snapshot))
    assert res["success"] is False
    assert "integer" in res["error"]

    res = json.loads(session_search(session_id=sid, window=2.5, snapshot=snapshot))
    assert res["success"] is False
    assert "integer" in res["error"]

    # Float/string around_message_id rejected
    res = json.loads(session_search(session_id=sid, around_message_id="1", snapshot=snapshot))
    assert res["success"] is False
    assert "integer" in res["error"]

    # Float/string limit rejected
    res = json.loads(session_search(session_id=sid, limit="3", snapshot=snapshot))
    assert res["success"] is False
    assert "integer" in res["error"]

    # Invalid query type
    res = json.loads(session_search(session_id=sid, query=123, snapshot=snapshot))
    assert res["success"] is False
    assert "query must be a string" in res["error"]

    # Overlong query
    res = json.loads(session_search(session_id=sid, query="a" * 1001, snapshot=snapshot))
    assert res["success"] is False
    assert "maximum length" in res["error"]

    # Invalid role_filter type
    res = json.loads(session_search(session_id=sid, role_filter=123, snapshot=snapshot))
    assert res["success"] is False
    assert "role_filter must be a string" in res["error"]

    # Malformed non-sequence exclude_session_ids does not throw
    res = json.loads(session_search(session_id=sid, exclude_session_ids=123, snapshot=snapshot))
    assert res["success"] is False
    assert "exclude_session_ids" in res["error"]


def test_browse_sequential_continuation_no_loops():
    """Browse sequentially steps forward through records using next_call without repeated prefixes."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Turn 1"),
        SnapshotRecord(record_id=2, role="assistant", text="Turn 2"),
        SnapshotRecord(record_id=3, role="user", text="Turn 3"),
        SnapshotRecord(record_id=4, role="assistant", text="Turn 4"),
        SnapshotRecord(record_id=5, role="user", text="Turn 5"),
    ]
    snapshot = _make_sample_snapshot(records, snapshot_id="snap-browse-steps")
    sid = "snapshot"

    # Browse initial page (limit 2)
    raw1 = session_search(session_id=sid, limit=2, snapshot=snapshot)
    res1 = json.loads(raw1)
    assert res1["success"] is True
    assert res1["mode"] == "snapshot_browse"
    assert [m["id"] for m in res1["messages"]] == [1, 2]
    assert res1["has_more"] is True
    assert res1["next_around_message_id"] == 3
    assert res1["next_call"]["start_message_id"] == 3
    assert "around_message_id" not in res1["next_call"]

    # Follow next_call to record 3
    raw2 = session_search(**res1["next_call"], snapshot=snapshot)
    res2 = json.loads(raw2)
    assert res2["success"] is True
    assert [m["id"] for m in res2["messages"]] == [3, 4]
    assert res2["has_more"] is True
    res3 = json.loads(session_search(**res2["next_call"], snapshot=snapshot))
    assert [m["id"] for m in res3["messages"]] == [5]
    assert res3["has_more"] is False

