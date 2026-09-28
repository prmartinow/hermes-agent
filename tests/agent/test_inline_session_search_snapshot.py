"""Focused tests for snapshot-scoped inline session_search tool execution.

Verifies:
- Seam in agent/inline_tool_executors.py routes before any DB callback or DB acquisition.
- No DB callbacks occur on valid, missing, forged, or profile snapshot requests.
- No sibling object leak across batch-spawned subagents.
- Normal DB session_search remains completely unaffected.
- Actual child agent execution loop dispatch to session_search with stubbed inference.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext, resolve_invoke_tool_executor
from hermes_state import SessionDB
from tools.delegation_context import (
    ContextSnapshot,
    SnapshotManifest,
    SnapshotRecord,
    build_batch_context_snapshots,
)


def _make_snapshot(snapshot_id: str, records: list[SnapshotRecord]) -> ContextSnapshot:
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        source_type="live_session_messages",
        content_hash_sha256="deadbeef" * 8,
        char_count=sum(len(r.text) for r in records),
        estimated_tokens=100,
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


def test_no_db_callbacks_on_valid_missing_forged_profile(tmp_path):
    """Proves that inline tool executor routes before any DB acquisition / recall DB call."""
    mock_db_getter = MagicMock(side_effect=AssertionError("DB access must never be attempted on snapshot routes!"))

    records = [
        SnapshotRecord(record_id=1, role="user", text="Implement caching layer"),
        SnapshotRecord(record_id=2, role="assistant", text="Using LRU cache strategy"),
    ]
    snapshot = _make_snapshot("snap-auth-valid", records)

    # 1. Valid snapshot route: DB getter NEVER called, returns records
    agent = SimpleNamespace(
        _inherited_context_snapshot=snapshot,
        _get_session_db_for_recall=mock_db_getter,
        session_id="child-subagent-1",
    )
    ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")

    res_raw = INLINE_TOOL_EXECUTORS["session_search"](
        agent,
        {"session_id": "snapshot:snap-auth-valid", "around_message_id": 1, "window": 0},
        ctx,
    )
    res = json.loads(res_raw)
    assert res["success"] is True
    assert res["messages"][0]["id"] == 1
    assert res["messages"][0]["content"] == "Implement caching layer"
    assert mock_db_getter.call_count == 0

    # 2. Missing snapshot on agent: fails closed, DB getter NEVER called
    agent_no_snap = SimpleNamespace(
        _inherited_context_snapshot=None,
        _get_session_db_for_recall=mock_db_getter,
        session_id="child-subagent-2",
    )
    res_raw_missing = INLINE_TOOL_EXECUTORS["session_search"](
        agent_no_snap,
        {"session_id": "snapshot:snap-auth-valid"},
        ctx,
    )
    res_missing = json.loads(res_raw_missing)
    assert res_missing["success"] is False
    assert "No inherited snapshot is attached" in res_missing["error"]
    assert mock_db_getter.call_count == 0

    # 3. Forged snapshot ID: fails closed, DB getter NEVER called
    res_raw_forged = INLINE_TOOL_EXECUTORS["session_search"](
        agent,
        {"session_id": "snapshot:snap-forged-other-agent"},
        ctx,
    )
    res_forged = json.loads(res_raw_forged)
    assert res_forged["success"] is False
    assert "does not match" in res_forged["error"]
    assert mock_db_getter.call_count == 0

    # 4. Profile requested: disallowed on snapshot routes, fails closed, DB getter NEVER called
    res_raw_prof = INLINE_TOOL_EXECUTORS["session_search"](
        agent,
        {"session_id": "snapshot:snap-auth-valid", "profile": "production"},
        ctx,
    )
    res_prof = json.loads(res_raw_prof)
    assert res_prof["success"] is False
    assert "profile" in res_prof["error"]
    assert mock_db_getter.call_count == 0


def test_no_sibling_object_leak():
    """Batch-spawned sibling subagents cannot cross-read each other's snapshots."""
    parent = SimpleNamespace(
        _session_messages=[
            {"role": "user", "content": "Parent context instruction"},
        ]
    )
    snapshots = build_batch_context_snapshots(
        parent,
        task_inherit_contexts=[True, True],
        task_inherit_max_tokens=[None, None],
    )
    assert len(snapshots) == 2
    snap_a, snap_b = snapshots[0], snapshots[1]
    assert snap_a is not None and snap_b is not None
    assert snap_a.manifest.snapshot_id != snap_b.manifest.snapshot_id

    mock_db_getter = MagicMock(side_effect=AssertionError("DB access attempted"))
    child_a = SimpleNamespace(
        _inherited_context_snapshot=snap_a,
        _get_session_db_for_recall=mock_db_getter,
        session_id="child-agent-a",
    )
    child_b = SimpleNamespace(
        _inherited_context_snapshot=snap_b,
        _get_session_db_for_recall=mock_db_getter,
        session_id="child-agent-b",
    )
    ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")

    # Child A requests Child B's snapshot ID -> fails closed
    res_a_cross = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
        child_a,
        {"session_id": f"snapshot:{snap_b.manifest.snapshot_id}"},
        ctx,
    ))
    assert res_a_cross["success"] is False
    assert "does not match" in res_a_cross["error"]

    # Child B requests Child A's snapshot ID -> fails closed
    res_b_cross = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
        child_b,
        {"session_id": f"snapshot:{snap_a.manifest.snapshot_id}"},
        ctx,
    ))
    assert res_b_cross["success"] is False
    assert "does not match" in res_b_cross["error"]

    # Each child can read its own snapshot
    res_a_own = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
        child_a,
        {"session_id": f"snapshot:{snap_a.manifest.snapshot_id}"},
        ctx,
    ))
    assert res_a_own["success"] is True

    res_b_own = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
        child_b,
        {"session_id": f"snapshot:{snap_b.manifest.snapshot_id}"},
        ctx,
    ))
    assert res_b_own["success"] is True
    assert mock_db_getter.call_count == 0


def test_controls_normal_db_search_unaffected(tmp_path, monkeypatch):
    """Normal non-snapshot session_search calls still query SessionDB as before."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db = SessionDB(hermes_home / "state.db")
    db.create_session("sess-control-1", source="cli")
    db.append_message("sess-control-1", role="user", content="Control query search target")
    db._conn.commit()

    agent = SimpleNamespace(
        _inherited_context_snapshot=None,
        _get_session_db_for_recall=lambda: db,
        session_id="agent-gw-control",
    )
    ctx = InlineToolContext(effective_task_id="task-control", tool_call_id="call-control")

    try:
        # Discovery query against database
        raw_res = INLINE_TOOL_EXECUTORS["session_search"](
            agent,
            {"query": "Control query search target"},
            ctx,
        )
        res = json.loads(raw_res)
        assert res["success"] is True
        assert res["mode"] == "discover"
        assert len(res["results"]) >= 1
        assert res["results"][0]["session_id"] == "sess-control-1"
    finally:
        db.close()


def test_actual_child_loop_dispatch_to_session_search():
    """Child execution dispatcher executes session_search on its attached snapshot."""
    records = [
        SnapshotRecord(record_id=1, role="user", text="Setup initial architecture"),
        SnapshotRecord(record_id=2, role="assistant", text="Created src/core/engine.py"),
        SnapshotRecord(record_id=3, role="user", text="Add benchmark suite"),
    ]
    snapshot = _make_snapshot("snap-child-loop-1", records)

    child_agent = SimpleNamespace(
        _inherited_context_snapshot=snapshot,
        _get_session_db_for_recall=MagicMock(side_effect=AssertionError("DB accessed")),
        _memory_manager=None,
        session_id="child-exec-session-42",
    )

    # Resolve inline executor through production dispatcher
    executor = resolve_invoke_tool_executor(child_agent, "session_search")
    assert executor is not None

    ctx = InlineToolContext(effective_task_id="task-child-loop", tool_call_id="call-child-1")

    # Simulate child tool invocation during inference turn
    tool_args = {
        "session_id": "snapshot:snap-child-loop-1",
        "query": "architecture",
    }
    result_json = executor(child_agent, tool_args, ctx)
    result = json.loads(result_json)

    assert result["success"] is True
    assert result["mode"] == "snapshot_search"
    assert result["total_matches"] == 1
    assert result["results"][0]["id"] == 1
    assert "Setup initial architecture" in result["results"][0]["snippet"]
