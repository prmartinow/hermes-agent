"""Independent checks for exact backing-record identity and whole-record seeds."""
import hashlib
import json
from types import SimpleNamespace

from tools.delegation_context import _render_parent_transcript, SnapshotRecord
from tools.delegation_context_selection import select_bounded_context_records


def test_source_digest_is_canonical_and_includes_outer_whitespace():
    parent = SimpleNamespace(_session_messages=[{"role": "user", "content": "  exact text\n"}])
    first = _render_parent_transcript(parent)
    second = _render_parent_transcript(parent)
    canonical = json.dumps([r.to_dict() for r in first.records], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    assert first.source_hash_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert first.source_hash_sha256 == second.source_hash_sha256
    parent._session_messages[0]["content"] = "exact text"
    assert _render_parent_transcript(parent).source_hash_sha256 != first.source_hash_sha256


def test_whole_record_seed_preserves_whitespace_and_unicode():
    text = "  λ = 'İstanbul'\n    return λ\n\n"
    record = SnapshotRecord(record_id=1, role="user", text=text)
    result = select_bounded_context_records((record,), effective_budget=1000)
    assert f"Record ID: 1]\n{text}\n" in result.rendered_transcript
    assert result.selected_records == (record,)
    assert result.omitted_records_count == 0


def test_large_keyword_dump_does_not_crowd_out_concise_decision():
    from agent.model_metadata import estimate_tokens_rough
    from tools.delegation_context_selection import render_bounded_transcript
    decision = SnapshotRecord(1, "assistant", "alpha setting must remain STRICT")
    dump = SnapshotRecord(2, "assistant", "alpha notes " * 100)
    latest = SnapshotRecord(3, "user", "Proceed.")
    budget = estimate_tokens_rough(render_bounded_transcript(
        [dump, latest], 3, "live_session_messages"))
    result = select_bounded_context_records(
        (decision, dump, latest), goal="alpha", effective_budget=budget)
    assert result.selected_record_ids == (1, 3)
    assert result.estimated_tokens <= budget


def test_scope_resolution_failure_restores_parent_tool_names_and_fails_closed():
    from unittest.mock import patch
    import model_tools
    from tools.delegation_context_budget import preflight_child_initial_request
    before = list(model_tools._last_resolved_tool_names)
    child = SimpleNamespace(
        valid_tool_names={"tool_call"},
        _inherited_context_manifest={"mode": "bounded"},
        _inherited_context_snapshot=SimpleNamespace(rendered_transcript="context"),
    )
    def unavailable(_):
        model_tools._last_resolved_tool_names = ["unexpected-global-mutation"]
        raise RuntimeError("synthetic resolution failure")
    with patch("agent.tool_executor._tool_search_scoped_names", unavailable):
        error = preflight_child_initial_request(0, {"inherit_context": True}, child)
    assert "cannot verify its scoped history reader" in error
    assert model_tools._last_resolved_tool_names == before
