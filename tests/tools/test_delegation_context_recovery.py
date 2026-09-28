"""Comprehensive tests for opt-in same-session compaction recovery in delegation snapshots.

Verifies:
- Normal archive + carried tail + unpersisted latest
- Repeated local compactions
- Undo / superseded rows excluded (active=0, compacted=0)
- Legitimate identical turns and reused tool IDs preserved
- Compaction summaries retained as derivative context
- Missing DB / no archive honestly reported as unavailable / active_only
- Opaque checkpoints fail closed even when archives exist
- Omitted flag / default behavior unchanged (no archives read)
- Mixed sibling batch isolation: tasks without flag never receive archives, immutable records shared per group
- Active / DB generation mismatch and race conditions reject before child construction
- Snapshot immutability after subsequent database appends
- Bounded reader retrieves old recovered archive marker
- Real AIAgent child-loop roundtrip with stubbed inference and prohibited recall DB acquisition
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from openai.types.chat import ChatCompletion

from agent.context_compressor import SUMMARY_PREFIX
from run_agent import AIAgent
from tools.delegate_tool import delegate_task
from tools.delegate_tool_tasks import _coerce_task_inherit_compacted_history
from tools.delegation_context import (
    ContextInheritanceError,
    ContextRecoveryError,
    RequiredContextError,
    SnapshotRecord,
    build_batch_context_snapshots,
    build_delegation_context_snapshot,
)
from tools.delegation_context_reader import dispatch_snapshot_search
from tools.registry import registry
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


class TestCompactionRecoveryValidation:
    """Flag validation rules for inherit_compacted_history."""

    def test_flag_requires_inherit_context(self):
        # inherit_compacted_history without inherit_context must fail
        tasks = [{"goal": "test", "inherit_compacted_history": True}]
        flags, err = _coerce_task_inherit_compacted_history(tasks, [False])
        assert err == "Task 0 'inherit_compacted_history' is only valid when 'inherit_context' is true."
        assert flags == []

        # Explicit False also requires inherit_context
        tasks = [{"goal": "test", "inherit_compacted_history": False}]
        flags, err = _coerce_task_inherit_compacted_history(tasks, [False])
        assert err == "Task 0 'inherit_compacted_history' is only valid when 'inherit_context' is true."
        assert flags == []

    def test_strict_boolean_check(self):
        for invalid in ["true", "false", 1, 0, [], {}, None]:
            tasks = [{"goal": "test", "inherit_context": True, "inherit_compacted_history": invalid}]
            flags, err = _coerce_task_inherit_compacted_history(tasks, [True])
            assert err == "Task 0 'inherit_compacted_history' must be a boolean."
            assert flags == []

    def test_omitted_defaults_to_false(self):
        tasks = [{"goal": "test", "inherit_context": True}]
        flags, err = _coerce_task_inherit_compacted_history(tasks, [True])
        assert err is None
        assert flags == [False]


class TestCompactionRecoveryRealDB:
    """Tests utilizing real SessionDB, archive_and_compact, and SQLite operations."""

    def test_normal_archive_carried_tail_unpersisted_latest(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        # 1. Historical turns before compaction
        old_turns = [
            {"role": "user", "content": "Initial user specification: implement feature alpha."},
            {"role": "assistant", "content": "Acknowledged feature alpha specification."},
        ]
        db.append_messages_batch(sid, old_turns)

        # 2. In-place compaction: archives old turns, carries tail_count=1
        summary_text = f"{SUMMARY_PREFIX}\nFeature alpha was specified and acknowledged."
        compacted_generation = [
            {"role": "assistant", "content": summary_text, "_compressed_summary": True},
            {"role": "user", "content": "Carried tail turn: continue alpha."},
            {"role": "assistant", "content": "Carried response: alpha in progress."},
        ]
        db.archive_and_compact(sid, compacted_generation, tail_count=1)

        # 3. Synchronize agent's live state to match DB active messages PLUS unpersisted latest
        active_from_db = db.get_messages_as_conversation(sid)
        unpersisted_turn = {"role": "user", "content": "Unpersisted latest: verify alpha status now."}
        agent._session_messages = copy.deepcopy(active_from_db) + [unpersisted_turn]

        # 4. Snapshot with opt-in compaction recovery
        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        manifest = snapshot.manifest

        assert manifest.inherit_compacted_history is True
        assert manifest.compaction_recovery_coverage == "available_readable"
        assert manifest.available_archived_messages_count == 1
        assert manifest.retained_archived_records_count == 1
        assert manifest.observed_db_row_watermark is not None

        # Verify all turns present in chronological order: archive -> summary -> carried -> unpersisted
        record_texts = [r.text for r in snapshot.records]
        assert any("Initial user specification" in t for t in record_texts)
        assert any("Feature alpha was specified" in t for t in record_texts)
        assert any("Carried tail turn" in t for t in record_texts)
        assert any("Unpersisted latest" in t for t in record_texts)
        assert "Unpersisted latest" in snapshot.records[-1].text

    def test_repeated_local_compactions(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        # Generation 1
        db.append_messages_batch(sid, [
            {"role": "user", "content": "Gen 1: Step A details."},
            {"role": "assistant", "content": "Gen 1: Completed Step A."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nStep A complete.", "_compressed_summary": True},
            {"role": "user", "content": "Gen 2: Step B details."},
            {"role": "assistant", "content": "Gen 2: Completed Step B."},
        ])

        # Generation 2 (repeated compaction in same session)
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nStep A and Step B complete.", "_compressed_summary": True},
            {"role": "user", "content": "Gen 3: Step C details."},
        ])

        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))
        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)

        assert snapshot.manifest.compaction_recovery_coverage == "available_readable"
        assert snapshot.manifest.available_archived_messages_count >= 2
        texts = [r.text for r in snapshot.records]
        assert any("Step A details" in t for t in texts)
        assert any("Step B details" in t for t in texts)
        assert any("Step C details" in t for t in texts)

    def test_undo_superseded_rows_excluded(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        # Valid archive turn
        db.append_messages_batch(sid, [
            {"role": "user", "content": "Legitimate archived turn."},
            {"role": "assistant", "content": "Archived response."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Turn to be rewound."},
            {"role": "assistant", "content": "Response to be rewound."},
        ])

        # Rewind to soft-delete the last turn (marks active=0, compacted=0)
        live = db.get_messages(sid)
        rewind_target = next(m for m in reversed(live) if m["role"] == "user")
        db.rewind_to_message(sid, rewind_target["id"])

        # Sync agent live messages to match active rows
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))
        agent._session_messages.append({"role": "user", "content": "Fresh user prompt after rewind."})

        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        texts = [r.text for r in snapshot.records]

        # Rewound/superseded turns (active=0, compacted=0) must NOT be present
        assert not any("Turn to be rewound" in t for t in texts)
        assert not any("Response to be rewound" in t for t in texts)
        # Legitimate archive turns must be present
        assert any("Legitimate archived turn" in t for t in texts)
        assert any("Fresh user prompt after rewind" in t for t in texts)

    def test_legitimate_identical_turns_and_reused_tool_ids_preserved(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        # Turn 1: tool call with ID call-123
        tc1 = [{"id": "call-123", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]
        db.append_messages_batch(sid, [
            {"role": "user", "content": "Run repeated inspection."},
            {"role": "assistant", "content": None, "tool_calls": tc1},
            {"role": "tool", "tool_call_id": "call-123", "name": "read_file", "content": "inspection result 1"},
        ])

        # Compact
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nInspected file.", "_compressed_summary": True},
            # Turn 2 in active generation: identical content and reused tool ID call-123
            {"role": "user", "content": "Run repeated inspection."},
            {"role": "assistant", "content": None, "tool_calls": tc1},
            {"role": "tool", "tool_call_id": "call-123", "name": "read_file", "content": "inspection result 2"},
        ])

        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))
        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)

        tool_records = [r for r in snapshot.records if r.role == "tool"]
        # Both tool turns must survive without deduplication on ID or content
        assert len(tool_records) == 2
        assert tool_records[0].tool_call_id == "call-123"
        assert tool_records[1].tool_call_id == "call-123"
        assert "inspection result 1" in tool_records[0].text
        assert "inspection result 2" in tool_records[1].text

    def test_summaries_retained_as_derivative_context(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Historical request."},
            {"role": "assistant", "content": "Historical response."},
        ])
        summary_marker = f"{SUMMARY_PREFIX}\nHistorical request summarized."
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": summary_marker, "_compressed_summary": True},
            {"role": "user", "content": "Latest user request."},
        ])

        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))
        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)

        # Summary turn must be preserved in records and labeled in transcript
        assert any(summary_marker in r.text for r in snapshot.records)
        assert "ASSISTANT COMPACTION SUMMARY (DERIVATIVE CONTEXT)" in snapshot.rendered_transcript

    def test_missing_db_and_no_archive_honest_receipts(self, isolated_hermes_env):
        agent, db = isolated_hermes_env

        # Case 1: Missing DB
        agent._session_db = None
        agent._session_messages = [{"role": "user", "content": "Active prompt only."}]
        snap_no_db = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        assert snap_no_db.manifest.compaction_recovery_coverage == "unavailable"
        assert snap_no_db.manifest.available_archived_messages_count == 0
        assert "Compaction History Recovery: unavailable" in snap_no_db.rendered_transcript

        # Case 2: Session with active rows only (never compacted)
        agent._session_db = db
        db.create_session("uncompacted-sess", source="cli")
        agent.session_id = "uncompacted-sess"
        agent._session_messages = [{"role": "user", "content": "Uncompacted session prompt."}]
        agent._persist_session(agent._session_messages)
        snap_uncompacted = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        assert snap_uncompacted.manifest.compaction_recovery_coverage == "active_only"
        assert snap_uncompacted.manifest.available_archived_messages_count == 0
        assert "Compaction History Recovery: active_only" in snap_uncompacted.rendered_transcript

        # Case 3: Archive rows exist, but visible live generation lacks a recognized compaction summary marker
        db.create_session("unexplained-archive-sess", source="cli")
        agent.session_id = "unexplained-archive-sess"
        db.append_messages_batch("unexplained-archive-sess", [
            {"role": "user", "content": "Old forgotten turn."},
        ])
        db.archive_and_compact("unexplained-archive-sess", [
            # Active turn without any summary prefix or _compressed_summary marker
            {"role": "user", "content": "Plain reset user prompt."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation("unexplained-archive-sess"))
        snap_unexplained = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        # Unexplained archive rows must NOT be restored to prevent resurrecting reset/cleared context
        assert snap_unexplained.manifest.compaction_recovery_coverage == "active_only"
        assert snap_unexplained.manifest.retained_archived_records_count == 0
        assert not any("Old forgotten turn" in r.text for r in snap_unexplained.records)

    def test_opaque_checkpoint_fails_closed_even_with_archive(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Prior turn."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True,
             "codex_reasoning_items": json.dumps([{"type": "compaction", "encrypted_content": "opaque-blob"}])},
            {"role": "user", "content": "Latest prompt."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        with pytest.raises(RequiredContextError, match="unsupported opaque compaction checkpoint"):
            build_delegation_context_snapshot(agent, inherit_compacted_history=True)

    def test_no_flag_behavior_unchanged(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Archived secret marker 9999."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Active prompt."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        # Default / omitted flag
        snapshot = build_delegation_context_snapshot(agent)
        assert snapshot.manifest.inherit_compacted_history is False
        assert snapshot.manifest.compaction_recovery_coverage is None
        assert not any("Archived secret marker 9999" in r.text for r in snapshot.records)

    def test_mixed_siblings_source_isolation(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Archived secret evidence."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Active prompt."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        # Batch of 4 tasks with mixed inheritance and recovery settings
        task_inherit_contexts = [True, True, True, True]
        task_inherit_compacted = [False, True, False, True]
        task_max_tokens = [None, None, None, None]

        snapshots = build_batch_context_snapshots(
            agent,
            task_inherit_contexts,
            task_max_tokens,
            task_inherit_compacted_histories=task_inherit_compacted,
        )

        snap0, snap1, snap2, snap3 = snapshots

        # Tasks 0 and 2 did not request recovery -> must NOT have archives
        assert not any("Archived secret evidence" in r.text for r in snap0.records)
        assert not any("Archived secret evidence" in r.text for r in snap2.records)
        assert snap0.records is snap2.records  # Shared immutable records backing

        # Tasks 1 and 3 requested recovery -> must have archives
        assert any("Archived secret evidence" in r.text for r in snap1.records)
        assert any("Archived secret evidence" in r.text for r in snap3.records)
        assert snap1.records is snap3.records  # Shared immutable records backing

    def test_active_db_mismatch_and_changed_live_source_reject(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Archived turn."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Active persisted turn in DB."},
        ])

        # Mismatch: live list differs from DB active rows
        agent._session_messages = [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Divergent turn in memory."},
        ]

        with pytest.raises(ContextRecoveryError, match="active generation mismatch"):
            build_delegation_context_snapshot(agent, inherit_compacted_history=True)

        # Race test: live messages list modified during query
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))
        original_get = db.get_compaction_recovery_messages

        def racy_get(session_id):
            agent._session_messages.append({"role": "user", "content": "Concurrent mid-query append."})
            return original_get(session_id)

        with patch.object(db, "get_compaction_recovery_messages", side_effect=racy_get):
            with pytest.raises(ContextRecoveryError, match="race detected"):
                build_delegation_context_snapshot(agent, inherit_compacted_history=True)

    def test_snapshot_immutable_after_later_db_append(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [
            {"role": "user", "content": "Archived turn."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "user", "content": "Active turn."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        snapshot = build_delegation_context_snapshot(agent, inherit_compacted_history=True)
        initial_records_count = len(snapshot.records)
        initial_hash = snapshot.manifest.content_hash_sha256

        # Append new turns to DB and agent
        db.append_message(sid, "user", "Later turn appended after snapshot.")
        agent._session_messages.append({"role": "user", "content": "Later turn in memory."})

        # Snapshot state must remain completely unaltered
        assert len(snapshot.records) == initial_records_count
        assert snapshot.manifest.content_hash_sha256 == initial_hash
        assert not any("Later turn" in r.text for r in snapshot.records)

    def test_bounded_reader_retrieves_old_recovered_marker(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        marker = f"RECOVERED-NONCE-{uuid.uuid4().hex[:8]}"

        db.append_messages_batch(sid, [
            {"role": "user", "content": f"Archived turn containing authorization nonce: {marker}. " + ("padding details " * 30)},
            {"role": "assistant", "content": "Archived response."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nPrior turns compacted.", "_compressed_summary": True},
            {"role": "user", "content": "Latest user request: execute bounded task."},
        ])
        agent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        # Build bounded snapshot with a tight budget so Record 1 is omitted from the seed
        snapshot = build_delegation_context_snapshot(
            agent,
            inherit_compacted_history=True,
            inherit_context_mode="bounded",
            config_override_tokens=350,
        )

        assert snapshot.manifest.mode == "bounded"
        assert snapshot.manifest.compaction_recovery_coverage == "available_readable"
        # Record 1 must NOT be in the initial seed transcript
        assert marker not in snapshot.rendered_transcript

        # Use dispatch_snapshot_search to retrieve record 1 on-demand from the snapshot
        raw = dispatch_snapshot_search(
            snapshot,
            "snapshot",
            query="authorization nonce",
            limit=5,
        )
        retrieval = json.loads(raw)
        assert retrieval["success"] is True
        assert marker in retrieval["results"][0]["snippet"]


class TestRealChildLoopRecoveryRoundtrip:
    """Real AIAgent child loop integration test with inference stubbed and recall DB acquisition prohibited."""

    def test_real_child_loop_recovers_archived_context_and_retrieves(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        parent.enabled_toolsets = ["delegation", "session_search"]
        sid = parent.session_id

        marker = f"ARCHIVED-SECRET-{uuid.uuid4().hex[:8]}"

        # Archived turn containing secret
        db.append_messages_batch(sid, [
            {"role": "user", "content": f"Archived authorization key is {marker}. " + ("padding details " * 40)},
            {"role": "assistant", "content": "Acknowledged secret key."},
        ])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSecret key recorded in archives.", "_compressed_summary": True},
            {"role": "user", "content": "Latest user prompt: delegate task to recover key."},
        ])
        parent._session_messages = copy.deepcopy(db.get_messages_as_conversation(sid))

        requests = []
        child_ids = []
        emitted_tool_names = []

        def inference(child, api_kwargs, **kwargs):
            requests.append(copy.deepcopy(api_kwargs))
            child_ids.append(child.session_id)

            if len(requests) == 1:
                # Turn 1: Bounded seed must omit the archive turn
                user_msgs = [m for m in api_kwargs["messages"] if m.get("role") == "user"]
                assert marker not in str(user_msgs[0]["content"])

                # Child retrieves omitted record 1 via session_search
                arguments = {
                    "session_id": "snapshot",
                    "around_message_id": 1,
                    "window": 0,
                    "max_chars": 1000,
                }
                exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
                tool_name = "session_search" if "session_search" in exposed else "tool_call"
                if tool_name == "tool_call":
                    arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}

                emitted_tool_names.append(tool_name)
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "recovery-retrieval-call",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                    }],
                }
                finish_reason = "tool_calls"
            else:
                # Turn 2: Verify tool output contains the recovered secret from snapshot
                outputs = [
                    m for m in api_kwargs["messages"]
                    if m.get("role") == "tool" and m.get("tool_call_id") == "recovery-retrieval-call"
                ]
                assert len(outputs) == 1
                payload = json.loads(outputs[0]["content"])
                assert payload["success"] is True
                found_text = payload["messages"][0]["content"] if "messages" in payload else payload["results"][0]["snippet"]
                assert marker in found_text

                message = {"role": "assistant", "content": json.dumps({"recovered_key": marker})}
                finish_reason = "stop"

            return ChatCompletion(
                id=f"synthetic-recovery-reader-{len(requests)}",
                created=0,
                model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{"index": 0, "finish_reason": finish_reason, "message": message}],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference), \
                patch.object(AIAgent, "_get_session_db_for_recall",
                             side_effect=AssertionError("Snapshot route must not acquire recall DB")) as recall:
            result = json.loads(registry.get_entry("delegate_task").handler(
                {"tasks": [{
                    "goal": "Retrieve the archived authorization key from snapshot history and return JSON.",
                    "inherit_context": True,
                    "inherit_compacted_history": True,
                    "inherit_context_mode": "bounded",
                    "inherit_max_tokens": 350,
                    "output_schema": {
                        "type": "object",
                        "properties": {"recovered_key": {"type": "string"}},
                        "required": ["recovered_key"],
                    },
                }]},
                parent_agent=parent,
            ))

        entry = result["results"][0]
        assert entry["status"] == "completed"
        assert entry["schema_valid"] is True
        summary = json.loads(entry["summary"])
        assert summary["recovered_key"] == marker

        # Verify receipts
        manifest = entry["inherited_context"]
        assert manifest["inherit_compacted_history"] is True
        assert manifest["compaction_recovery_coverage"] == "available_readable"
        assert manifest["available_archived_messages_count"] == 2
        assert manifest["retained_archived_records_count"] == 2
        assert recall.call_count == 0

    @pytest.mark.parametrize("bad_tool_calls", [
        "not-a-list",
        ["not-a-dict"],
        [{"id": 123, "function": {"name": "tool", "arguments": "{}"}}],
        [{"id": True, "function": {"name": "tool", "arguments": "{}"}}],
        [{"id": "call_1", "function": 123}],
        [{"id": "call_1", "function": {"name": 123, "arguments": "{}"}}],
        [{"id": "call_1", "function": {"name": "tool", "arguments": 123}}],
    ])
    def test_alignment_fails_closed_on_malformed_tool_calls(self, isolated_hermes_env, bad_tool_calls):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [{"role": "user", "content": "Archived turn."}])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "function": {"name": "tool", "arguments": "{}"}}]},
        ])
        live = copy.deepcopy(db.get_messages_as_conversation(sid))
        live[1]["tool_calls"] = bad_tool_calls
        agent._session_messages = live

        with pytest.raises(ContextRecoveryError, match="Context recovery failed: active generation mismatch"):
            build_delegation_context_snapshot(agent, inherit_compacted_history=True)

    @pytest.mark.parametrize("bad_tcid", [123, True, 45.6, []])
    def test_alignment_fails_closed_on_typed_tool_call_ids(self, isolated_hermes_env, bad_tcid):
        agent, db = isolated_hermes_env
        sid = agent.session_id

        db.append_messages_batch(sid, [{"role": "user", "content": "Archived turn."}])
        db.archive_and_compact(sid, [
            {"role": "assistant", "content": f"{SUMMARY_PREFIX}\nSummary.", "_compressed_summary": True},
            {"role": "tool", "name": "tool", "tool_call_id": "valid_call_id", "content": "result"},
        ])
        live = copy.deepcopy(db.get_messages_as_conversation(sid))
        live[1]["tool_call_id"] = bad_tcid
        agent._session_messages = live

        with pytest.raises(ContextRecoveryError, match="Context recovery failed: active generation mismatch"):
            build_delegation_context_snapshot(agent, inherit_compacted_history=True)
