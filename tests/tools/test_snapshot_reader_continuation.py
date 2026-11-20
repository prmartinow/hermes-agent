"""Whole-chain tests for snapshot cursors, not just the first next_call."""
import json

import pytest

from tools.delegation_context import SnapshotRecord
from tools.session_search_tool import session_search
from tests.tools.test_delegation_context_reader import _make_sample_snapshot


@pytest.mark.parametrize("initial", [{"limit": 2}, {"around_message_id": 1, "window": 2}])
def test_next_calls_preserve_every_character_and_record(initial):
    records = [
        SnapshotRecord(1, "user", "αβ🚀" * 300),
        SnapshotRecord(2, "assistant", "FILTERED_OUT"),
        SnapshotRecord(3, "user", ""),
        SnapshotRecord(4, "user", "end/漢字" * 90),
        SnapshotRecord(5, "user", "last record"),
    ]
    snapshot = _make_sample_snapshot(records, "snap-all-pages")
    args = dict(initial, session_id="snapshot", max_chars=97, role_filter="user")
    seen = set()
    reconstructed = {}
    for _ in range(100):
        cursor = json.dumps(args, sort_keys=True)
        assert cursor not in seen, "Continuation loop"
        seen.add(cursor)
        page = json.loads(session_search(snapshot=snapshot, **args))
        assert page["success"] is True
        assert page["content_chars_returned"] <= 97
        for message in page["messages"]:
            assert message["role"] == "user"
            previous = reconstructed.get(message["id"], "")
            assert message["content_offset"] == len(previous), "Skipped or repeated content"
            reconstructed[message["id"]] = previous + message["content"]
        if not page["has_more"]:
            break
        args = page["next_call"]
        assert args["max_chars"] == 97
        assert args["role_filter"] == "user"
    else:
        pytest.fail("Pagination did not terminate")
    assert reconstructed == {r.record_id: r.text for r in records if r.role == "user"}


def test_search_cursor_preserves_filter_and_content_budget():
    records = tuple(SnapshotRecord(i, "user" if i % 2 else "assistant",
                                   "prefix " * 20 + "needle " + str(i)) for i in range(1, 10))
    snapshot = _make_sample_snapshot(list(records), "snap-search-pages")
    args = {"session_id": "snapshot", "query": "needle", "role_filter": "user",
            "max_chars": 10, "limit": 2}
    ids = []
    for _ in range(10):
        page = json.loads(session_search(snapshot=snapshot, **args))
        assert page["success"] is True
        assert page["total_matches"] == 5
        assert page["content_chars_returned"] <= 10
        for result in page["results"]:
            assert result["role"] == "user"
            original = records[result["id"] - 1].text
            assert original[result["snippet_offset"]:result["snippet_offset"] + len(result["snippet"])] == result["snippet"]
            assert "needle" in result["snippet"]
            ids.append(result["id"])
        if not page["has_more"]:
            break
        args = page["next_call"]
        assert args["max_chars"] == 10 and args["role_filter"] == "user"
    else:
        pytest.fail("Search continuation did not terminate")
    assert ids == [1, 3, 5, 7, 9]


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.5])
def test_invalid_sequential_start_is_rejected(value):
    snapshot = _make_sample_snapshot([SnapshotRecord(1, "user", "x")])
    page = json.loads(session_search(session_id="snapshot", snapshot=snapshot, start_message_id=value))
    assert page["success"] is False


def test_sequential_start_conflicts_are_explicit():
    snapshot = _make_sample_snapshot([SnapshotRecord(1, "user", "x")])
    for conflict in [{"query": "x"}, {"around_message_id": 1}]:
        page = json.loads(session_search(session_id="snapshot", snapshot=snapshot,
                                        start_message_id=1, **conflict))
        assert page["success"] is False
