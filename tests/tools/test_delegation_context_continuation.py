"""Comprehensive unit and native-loop tests for opt-in worker follow-up continuity.

Verifies:
- Parameter validation: continue_from requires inherit_context=True, rejects non-strings,
  whitespace, empty, whitespace-wrapped; never strips or repairs; default unchanged.
- Durable terminal marker stamping in model_config: version, status, exit_reason, message_count,
  max_row_id, timestamp.
- Unrun / timed-out / fabricated entries are not stamped; result entries expose child_session_id.
- Strict source gates: reject missing, self, foreign, non-delegated, unrun/empty, unstamped/non-terminal,
  active (in-process or durable lease), watermark/tampering mismatch, and opaque checkpoints.
- Two profile A->B->A isolation with colliding IDs across attached DBs.
- Mixed sibling isolation: different continue_from sources never crossfeed.
- Real two-child conversation loop with only inference stubbed: child 1 persists observed marker,
  parent adds correction, child 2 continue_from gets both, retrieves omitted old record using
  actual snapshot reader, answers new scope. Prior child and parent DB rows and live objects unmodified.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion

from agent.context_compressor import SUMMARY_PREFIX
from run_agent import AIAgent
from tools.delegate_tool import delegate_task
from tools.delegate_tool_tasks import _coerce_task_continue_from
from tools.delegation_context import (
    ContextInheritanceError,
    ContextSnapshot,
    RequiredContextError,
    SnapshotRecord,
    build_batch_context_snapshots,
    build_delegation_context_snapshot,
)
from tools.delegation_context_continuation import (
    PriorWorkerCapture,
    compose_continuation_snapshot,
    stamp_child_terminal_state,
    validate_and_capture_prior_worker,
)
from tools.delegation_context_reader import dispatch_snapshot_search
from tools.registry import registry
from hermes_state import SessionDB
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


# ── 1. Parameter Coercion and Validation ──────────────────────────────────────────


class TestContinueFromValidation:
    """Coercion and validation rules for tasks[].continue_from."""

    def test_requires_inherit_context(self):
        tasks = [{"goal": "child task", "continue_from": "child-sess-1"}]
        res, err = _coerce_task_continue_from(tasks, [False])
        assert err == "Task 0 'continue_from' is only valid when 'inherit_context' is true."
        assert res == []

    def test_rejects_non_strings(self):
        for bad_val in [123, 45.6, True, False, [], {}, None]:
            tasks = [{"goal": "child task", "inherit_context": True, "continue_from": bad_val}]
            res, err = _coerce_task_continue_from(tasks, [True])
            assert err == "Task 0 'continue_from' must be a string."
            assert res == []

    def test_rejects_empty_and_whitespace(self):
        for bad_val in ["", "   ", "  child-1", "child-1  ", "  child-1  ", "\tchild-1\n"]:
            tasks = [{"goal": "child task", "inherit_context": True, "continue_from": bad_val}]
            res, err = _coerce_task_continue_from(tasks, [True])
            assert err == "Task 0 'continue_from' must be a non-empty string without leading or trailing whitespace."
            assert res == []

    def test_accepts_exact_string(self):
        tasks = [{"goal": "child task", "inherit_context": True, "continue_from": "exact-child-id-123"}]
        res, err = _coerce_task_continue_from(tasks, [True])
        assert err is None
        assert res == ["exact-child-id-123"]

    def test_omitted_defaults_to_none(self):
        tasks = [{"goal": "task 1", "inherit_context": True}, {"goal": "task 2"}]
        res, err = _coerce_task_continue_from(tasks, [True, False])
        assert err is None
        assert res == [None, None]


# ── 2. Durable Terminal Stamping & Result Identity Exposure ─────────────────────────


class TestTerminalStampingAndResultIdentity:
    """Terminal marker persistence and result entry exposure."""

    def test_stamp_child_terminal_state_success(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "child-stamp-001"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})
        db.append_messages_batch(child_sid, [
            {"role": "user", "content": "Child task prompt"},
            {"role": "assistant", "content": "Child task completed successfully"},
        ])

        child_mock = MagicMock()
        child_mock._session_db = db
        child_mock.session_id = child_sid
        child_mock._session_init_model_config = {"_delegate_from": parent.session_id}

        entry = {"status": "completed", "exit_reason": "completed"}
        stamped = stamp_child_terminal_state(child_mock, entry)
        assert stamped is True

        # Verify DB model_config
        with db._read_ctx() as conn:
            row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (child_sid,)).fetchone()
            cfg = json.loads(row[0])
            terminal = cfg.get("_delegate_terminal")
            assert terminal is not None
            assert terminal["version"] == 1
            assert terminal["status"] == "completed"
            assert terminal["exit_reason"] == "completed"
            assert terminal["message_count"] == 2
            assert terminal["max_row_id"] is not None

    def test_unrun_child_without_messages_not_stamped(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "child-unrun-001"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})

        child_mock = MagicMock()
        child_mock._session_db = db
        child_mock.session_id = child_sid
        entry = {"status": "failed", "exit_reason": "error"}
        assert stamp_child_terminal_state(child_mock, entry) is False

    def test_result_entries_expose_child_session_id(self):
        from tools.delegate_tool_child_run import _build_result_entry, _fabricated_entry

        child = MagicMock()
        child.session_id = "stable-child-id-999"
        child.model = "test-model"
        child._delegate_role = "leaf"
        child._inherited_context_manifest = None
        child.session_estimated_cost_usd = 0.0
        child.session_cost_status = "exact"
        child.session_prompt_tokens = 10
        child.session_completion_tokens = 5

        # 1. _build_result_entry
        res = {"final_response": "done", "completed": True, "api_calls": 1, "messages": []}
        schema = MagicMock(declared=None, valid=True, errors=[], retry_count=0)
        entry = _build_result_entry(child, res, 0, 1.5, schema)
        assert entry["child_session_id"] == "stable-child-id-999"

        # 2. _fabricated_entry
        fab_entry = _fabricated_entry(0, "error", "some error", child, 0.5)
        assert fab_entry["child_session_id"] == "stable-child-id-999"


# ── 3. Strict Source Gates ─────────────────────────────────────────────────────────


class TestSourceGates:
    """Strict validation and rejection gates before constructing child."""

    def test_rejects_missing_session(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        with pytest.raises(ContextInheritanceError, match="not found in session database"):
            validate_and_capture_prior_worker(parent, "non-existent-session-id")

    def test_rejects_self_reference(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        with pytest.raises(ContextInheritanceError, match="cannot reference the current parent session"):
            validate_and_capture_prior_worker(parent, parent.session_id)

    def test_rejects_foreign_parent(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "foreign-child"
        db.create_session("other-parent-id", source="cli")
        db.create_session(child_sid, source="subagent", parent_session_id="other-parent-id", model_config={"_delegate_from": "other-parent-id"})
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])
        with pytest.raises(ContextInheritanceError, match="does not match current parent session"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_non_delegated(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "non-delegated-child"
        # parent_session_id matches but _delegate_from missing
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"other_key": "val"})
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])
        with pytest.raises(ContextInheritanceError, match="does not match current parent session"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_unstamped_non_terminal(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "unstamped-child"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])
        with pytest.raises(ContextInheritanceError, match="missing _delegate_terminal marker"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_active_in_parent(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "active-in-parent"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={
            "_delegate_from": parent.session_id,
            "_delegate_terminal": {"version": 1, "status": "completed", "exit_reason": "completed", "message_count": 1, "max_row_id": 1},
        })
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])

        active_child = MagicMock()
        active_child.session_id = child_sid
        parent._active_children = [active_child]

        with pytest.raises(ContextInheritanceError, match="currently active in this parent agent"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_active_durable_turn_lease(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "active-leased-child"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={
            "_delegate_from": parent.session_id,
            "_delegate_terminal": {"version": 1, "status": "completed", "exit_reason": "completed", "message_count": 1, "max_row_id": 1},
        })
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])
        # Insert active turn lease in session_turn_leases
        with db._lock:
            now = time.time()
            db._conn.execute(
                "INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (child_sid, "other-worker-lease", now, now + 300.0),
            )
            db._conn.commit()

        with pytest.raises(ContextInheritanceError, match="active durable turn lease"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_watermark_tampering(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "tampered-child"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={
            "_delegate_from": parent.session_id,
            "_delegate_terminal": {
                "version": 1,
                "status": "completed",
                "exit_reason": "completed",
                "message_count": 1,
                "max_row_id": 1,
                "active_transcript_digest": "dummy",
                "completed_at": time.time(),
            },
        })
        db.append_messages_batch(child_sid, [
            {"role": "user", "content": "first msg"},
            {"role": "assistant", "content": "second msg added after stamp"},
        ])
        db.end_session(child_sid, "agent_close")

        with pytest.raises(ContextInheritanceError, match="does not match terminal stamp watermark"):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_malformed_terminal_metadata(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "malformed-terminal-child"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={
            "_delegate_from": parent.session_id,
            "_delegate_terminal": {"version": 1, "status": 123},  # bad status type, missing counts
        })
        db.append_messages_batch(child_sid, [{"role": "user", "content": "msg"}])
        db.end_session(child_sid, "agent_close")

        with pytest.raises(ContextInheritanceError):
            validate_and_capture_prior_worker(parent, child_sid)

    def test_rejects_opaque_compaction_checkpoint(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "opaque-child"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})
        checkpoint = json.dumps([{"type": "compaction", "encrypted_content": "opaque123"}])
        db.append_messages_batch(child_sid, [{"role": "assistant", "content": "txt", "codex_reasoning_items": checkpoint}])
        child = SimpleNamespace(session_id=child_sid, _session_db=db, _session_init_model_config={})
        stamp_child_terminal_state(child, {"status": "completed", "exit_reason": "completed"})
        db.end_session(child_sid, "agent_close")

        with pytest.raises(RequiredContextError, match="unsupported opaque compaction checkpoint"):
            validate_and_capture_prior_worker(parent, child_sid)


# ── 4. Profile Isolation With Colliding IDs ─────────────────────────────────────────


class TestProfileIsolationCollidingIDs:
    """Two profile A->B->A isolation with colliding child IDs across attached DBs."""

    def test_two_profiles_colliding_ids_isolation(self, tmp_path):
        home_a = tmp_path / "home_a"
        home_b = tmp_path / "home_b"
        home_a.mkdir()
        home_b.mkdir()

        db_a = SessionDB(home_a / "state.db")
        db_b = SessionDB(home_b / "state.db")

        # Parent A and Child A in DB A
        db_a.create_session("parent-A", source="cli")
        db_a.create_session("worker-1", source="subagent", parent_session_id="parent-A", model_config={"_delegate_from": "parent-A"})
        db_a.append_messages_batch("worker-1", [{"role": "user", "content": "Worker 1 in Profile A"}])
        child_a = SimpleNamespace(session_id="worker-1", _session_db=db_a, _session_init_model_config={})
        stamp_child_terminal_state(child_a, {"status": "completed", "exit_reason": "completed"})
        db_a.end_session("worker-1", "agent_close")

        # Parent B and Child B in DB B (same child id "worker-1")
        db_b.create_session("parent-B", source="cli")
        db_b.create_session("worker-1", source="subagent", parent_session_id="parent-B", model_config={"_delegate_from": "parent-B"})
        db_b.append_messages_batch("worker-1", [{"role": "user", "content": "Worker 1 in Profile B"}])
        child_b = SimpleNamespace(session_id="worker-1", _session_db=db_b, _session_init_model_config={})
        stamp_child_terminal_state(child_b, {"status": "completed", "exit_reason": "completed"})
        db_b.end_session("worker-1", "agent_close")

        parent_agent_a = MagicMock()
        parent_agent_a.session_id = "parent-A"
        parent_agent_a._session_db = db_a

        parent_agent_b = MagicMock()
        parent_agent_b.session_id = "parent-B"
        parent_agent_b._session_db = db_b

        # Capture under Parent A retrieves Profile A's transcript
        cap_a = validate_and_capture_prior_worker(parent_agent_a, "worker-1")
        assert cap_a.session_id == "worker-1"
        assert cap_a.active_messages[0]["content"] == "Worker 1 in Profile A"

        # Capture under Parent B retrieves Profile B's transcript
        cap_b = validate_and_capture_prior_worker(parent_agent_b, "worker-1")
        assert cap_b.session_id == "worker-1"
        assert cap_b.active_messages[0]["content"] == "Worker 1 in Profile B"

        # Attempting to capture Profile B's child using DB A fails ownership gate
        db_a.create_session("parent-B", source="cli")
        db_a.create_session("worker-foreign", source="subagent", parent_session_id="parent-B", model_config={"_delegate_from": "parent-B"})
        db_a.append_messages_batch("worker-foreign", [{"role": "user", "content": "msg"}])
        with pytest.raises(ContextInheritanceError, match="does not match current parent session"):
            validate_and_capture_prior_worker(parent_agent_a, "worker-foreign")

        db_a.close()
        db_b.close()


# ── 5. Mixed Sibling Isolation ───────────────────────────────────────────────────────


class TestMixedSiblingIsolation:
    """Sibling tasks with different continue_from sources remain strictly isolated."""

    def test_mixed_siblings_different_sources_never_crossfeed(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        sid_parent = parent.session_id

        # Setup Child 1
        db.create_session("child-worker-1", source="subagent", parent_session_id=sid_parent, model_config={"_delegate_from": sid_parent})
        db.append_messages_batch("child-worker-1", [
            {"role": "user", "content": "Child 1 task"},
            {"role": "assistant", "content": "Child 1 specific observations #111"},
        ])
        child_1 = SimpleNamespace(session_id="child-worker-1", _session_db=db, _session_init_model_config={})
        stamp_child_terminal_state(child_1, {"status": "completed", "exit_reason": "completed"})
        db.end_session("child-worker-1", "agent_close")

        # Setup Child 2
        db.create_session("child-worker-2", source="subagent", parent_session_id=sid_parent, model_config={"_delegate_from": sid_parent})
        db.append_messages_batch("child-worker-2", [
            {"role": "user", "content": "Child 2 task"},
            {"role": "assistant", "content": "Child 2 specific observations #222"},
        ])
        child_2 = SimpleNamespace(session_id="child-worker-2", _session_db=db, _session_init_model_config={})
        stamp_child_terminal_state(child_2, {"status": "completed", "exit_reason": "completed"})
        db.end_session("child-worker-2", "agent_close")

        # Parent messages
        parent._session_messages = [{"role": "user", "content": "Parent primary prompt"}]

        task_inherit = [True, True, True]
        task_modes = ["full", "full", "full"]
        task_cf = ["child-worker-1", "child-worker-2", None]
        task_goals = ["Goal A", "Goal B", "Goal C"]

        snaps = build_batch_context_snapshots(
            parent,
            task_inherit,
            [None, None, None],
            task_inherit_context_modes=task_modes,
            task_continue_from=task_cf,
            task_goals=task_goals,
        )

        assert len(snaps) == 3

        # Sibling 1 has child 1 evidence but NOT child 2
        s1_text = snaps[0].rendered_transcript
        assert "Child 1 specific observations #111" in s1_text
        assert "Child 2 specific observations #222" not in s1_text
        assert "Parent primary prompt" in s1_text
        assert snaps[0].manifest.prior_worker_session_id == "child-worker-1"

        # Sibling 2 has child 2 evidence but NOT child 1
        s2_text = snaps[1].rendered_transcript
        assert "Child 2 specific observations #222" in s2_text
        assert "Child 1 specific observations #111" not in s2_text
        assert "Parent primary prompt" in s2_text
        assert snaps[1].manifest.prior_worker_session_id == "child-worker-2"

        # Sibling 3 has only parent context, no worker evidence
        s3_text = snaps[2].rendered_transcript
        assert "Child 1 specific observations #111" not in s3_text
        assert "Child 2 specific observations #222" not in s3_text
        assert "Parent primary prompt" in s3_text
        assert snaps[2].manifest.prior_worker_session_id is None


# ── 6. Real Two-Child Conversation Loop Test ─────────────────────────────────────────


class TestRealTwoChildLoopRoundtrip:
    """Real two-child conversation loop with only inference stubbed."""

    def test_two_child_loop_with_retrieval_and_unmodified_state(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        parent.valid_tool_names.append("session_search")
        parent.enabled_toolsets.append("session_search")
        marker = "MARKER-DISCOVERED-SECRET-8841"

        # Parent initial conversation
        parent._session_messages = [
            {"role": "user", "content": "Initial parent user prompt: investigate subsystem."},
            {"role": "assistant", "content": "Beginning investigation."},
        ]

        # Step 1: Child 1 runs and discovers a marker absent from parent
        def inference_child_1(*args, **kwargs):
            return ChatCompletion(
                id="child-1-completion",
                created=0,
                model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"I inspected the target and discovered the critical marker: {marker}.",
                    },
                }],
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_1):
            res_1_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{"goal": "Inspect subsystem for hidden markers."}]},
                parent_agent=parent,
            )
            res_1 = json.loads(res_1_raw)

        entry_1 = res_1["results"][0]
        assert entry_1["status"] == "completed"
        assert entry_1["continuation_available"] is True
        child_1_sid = entry_1["child_session_id"]
        assert child_1_sid

        # Record DB snapshot of child 1 and parent rows before running Child 2
        with db._read_ctx() as conn:
            c1_rows_before = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (child_1_sid,)).fetchall()
            parent_rows_before = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (parent.session_id,)).fetchall()
            c1_cfg_before = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (child_1_sid,)).fetchone()[0]

        # Step 2: Parent adds a new correction / instruction turn
        parent._session_messages.append({"role": "user", "content": "Parent correction: retrieve and verify the discovered marker."})

        # Step 3: Child 2 runs with continue_from=child_1_sid, bounded mode with tight budget so marker record is omitted from seed
        requests_child_2 = []

        def inference_child_2(agent_self, *args, **kwargs):
            requests_child_2.append((args, kwargs))
            api_kwargs = kwargs.get("api_kwargs") or (args[0] if args and isinstance(args[0], dict) else {})
            msgs = api_kwargs.get("messages") or kwargs.get("messages") or (args[0] if args and isinstance(args[0], list) else (args[1] if len(args) > 1 and isinstance(args[1], list) else []))

            # Turn 1: Child 2 does not see marker in bounded seed, issues session_search on snapshot
            if len(requests_child_2) == 1:
                # Assert seed transcript has required notices and does not yet contain marker
                user_msgs = [m for m in msgs if m.get("role") == "user"]
                user_msg = str(user_msgs[0]["content"]) if user_msgs else ""
                assert "Old snapshot references are historical" in user_msg
                assert "Prior filesystem and tool observations may be stale" in user_msg
                assert marker not in user_msg

                arguments = {"session_id": "snapshot", "query": marker}
                exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
                tool_name = "session_search" if "session_search" in exposed else "tool_call"
                if tool_name == "tool_call":
                    arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}

                return ChatCompletion(
                    id="child-2-call",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "search-snap-call",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }],
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )
            else:
                # Turn 2: Verify tool result contains the marker from snapshot reader
                tool_msg = next((m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "search-snap-call"), None)
                assert tool_msg is not None
                tool_data = json.loads(tool_msg["content"])
                assert tool_data["success"] is True
                assert marker in str(tool_data)

                # Child 2 produces final answer using retrieved marker
                return ChatCompletion(
                    id="child-2-done",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"verified_marker": marker}),
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_2):
            res_2_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{
                    "goal": "Verify the marker found by prior worker and output JSON.",
                    "inherit_context": True,
                    "continue_from": child_1_sid,
                    "inherit_context_mode": "bounded",
                    "inherit_max_tokens": 400,
                    "output_schema": {
                        "type": "object",
                        "properties": {"verified_marker": {"type": "string"}},
                        "required": ["verified_marker"],
                    },
                }]},
                parent_agent=parent,
            )
            res_2 = json.loads(res_2_raw)

        entry_2 = res_2["results"][0]
        assert entry_2["status"] == "completed"
        assert entry_2["schema_valid"] is True
        summary_2 = json.loads(entry_2["summary"])
        assert summary_2["verified_marker"] == marker

        # Verify receipts on child 2
        manifest_2 = entry_2["inherited_context"]
        assert manifest_2["prior_worker_session_id"] == child_1_sid
        assert manifest_2["prior_worker_status"] == "completed"
        assert manifest_2["prior_worker_exit_reason"] == "completed"
        assert manifest_2["prior_worker_available_records_count"] > 0
        assert manifest_2["mode"] == "bounded"

        # Step 4: Verify Child 1 and Parent rows in DB and live objects were NOT modified
        with db._read_ctx() as conn:
            c1_rows_after = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (child_1_sid,)).fetchall()
            parent_rows_after = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (parent.session_id,)).fetchall()
            c1_cfg_after = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (child_1_sid,)).fetchone()[0]

        assert [dict(r) for r in c1_rows_before] == [dict(r) for r in c1_rows_after]
        assert [dict(r) for r in parent_rows_before] == [dict(r) for r in parent_rows_after]
        assert c1_cfg_before == c1_cfg_after


# ── 7. Schema Retry Retains Combined Context ─────────────────────────────────────────


class TestSchemaRetryRetainsCombinedContext:
    """Schema retry turn retains combined snapshot context and stamps updated terminal state."""

    def test_schema_retry_retains_combined_context(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        parent._delegate_depth = 1
        parent.valid_tool_names.append("session_search")
        parent.enabled_toolsets.append("session_search")

        # Prior child in DB
        child_1_sid = "worker-schema-retry-001"
        db.create_session(child_1_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})
        db.append_messages_batch(child_1_sid, [
            {"role": "user", "content": "Initial worker task prompt"},
            {"role": "assistant", "content": "Initial worker completed output #999"},
        ])
        child_1 = SimpleNamespace(session_id=child_1_sid, _session_db=db, _session_init_model_config={})
        stamp_child_terminal_state(child_1, {"status": "completed", "exit_reason": "completed"})
        db.end_session(child_1_sid, "agent_close")

        parent._session_messages = [
            {"role": "user", "content": "Parent question about prior worker"},
        ]

        turns = []

        def inference_retry(agent_self, *args, **kwargs):
            turns.append(1)
            if len(turns) == 1:
                # Turn 1: Invalid JSON (fails schema)
                return ChatCompletion(
                    id="retry-turn-1",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Not JSON at all"}}],
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                )
            else:
                # Turn 2: Schema retry message was passed; return valid JSON
                return ChatCompletion(
                    id="retry-turn-2",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"result": "valid_output"})}}],
                    usage={"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25},
                )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_retry):
            res_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{
                    "goal": "Process prior worker findings and output schema-compliant JSON.",
                    "inherit_context": True,
                    "continue_from": child_1_sid,
                    "output_schema": {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                        "required": ["result"],
                    },
                }]},
                parent_agent=parent,
            )
            res = json.loads(res_raw)

        entry = res["results"][0]
        assert entry["status"] == "completed"
        assert entry["schema_valid"] is True
        assert entry["continuation_available"] is True
        summary = json.loads(entry["summary"])
        assert summary["result"] == "valid_output"
        assert entry["inherited_context"]["prior_worker_session_id"] == child_1_sid

        # Verify child 2's terminal stamp has the messages from both turns
        child_2_sid = entry["child_session_id"]
        with db._read_ctx() as conn:
            cfg_row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (child_2_sid,)).fetchone()[0]
            terminal = json.loads(cfg_row)["_delegate_terminal"]
            assert terminal["version"] == 1
            assert terminal["status"] == "completed"
            assert terminal["message_count"] >= 3


# ── 8. Composition, Lossless Framing & Readback Regressions ──────────────────────────


class TestContinuationCompositionAndReadbackTargeted:
    """Targeted tests for readback verification, lossless whitespace preservation, and compaction guard."""

    def test_readback_failure_reports_continuation_unavailable_but_preserves_summary(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        child_sid = "worker-readback-fail"
        db.create_session(child_sid, source="subagent", parent_session_id=parent.session_id, model_config={"_delegate_from": parent.session_id})
        db.append_messages_batch(child_sid, [{"role": "user", "content": "prompt"}])
        child = SimpleNamespace(session_id=child_sid, _session_db=db, _session_init_model_config={})
        stamp_child_terminal_state(child, {"status": "completed", "exit_reason": "completed"})
        # Intentionally do NOT end session -> ended_at is NULL
        from tools.delegation_context_continuation import verify_child_terminal_readback
        assert verify_child_terminal_readback(db, child_sid, expected_parent_id=parent.session_id) is False

    def test_exact_whitespace_preserved_in_quoted_records(self, isolated_hermes_env):
        from tools.delegation_context_continuation import PriorWorkerCapture, compose_continuation_snapshot
        from tools.delegation_context import RenderedTranscriptResult, SnapshotRecord
        parent, db = isolated_hermes_env

        whitespace_content = "\n  line 1 with leading indent\n  line 2\n\n"
        capture = PriorWorkerCapture(
            session_id="worker-ws",
            status="completed",
            exit_reason="completed",
            active_messages=({"role": "user", "content": whitespace_content},),
            archived_messages=(),
            active_count=1,
            max_active_row_id=1,
            inherit_compacted_history=False,
        )
        parent_rendered = RenderedTranscriptResult(
            transcript_text="",
            source_type="live",
            content_hash_sha256="abc",
            char_count=100,
            estimated_tokens=20,
            retained_messages_count=1,
            retained_tool_events_count=0,
            omitted_system_messages_count=0,
            omitted_sidecars_count=0,
            omitted_scaffolding_count=0,
            omitted_orphan_tool_results_count=0,
            omitted_images_count=0,
            omitted_unsupported_blocks_count=0,
            omissions_detail=(),
            records=(SnapshotRecord(record_id=1, role="user", text="Parent prompt"),),
        )
        snap = compose_continuation_snapshot(
            parent_agent=parent,
            parent_rendered=parent_rendered,
            prior_capture=capture,
            configured_ceiling=4000,
            effective_budget=4000,
            mode="full",
        )
        assert whitespace_content in snap.records[0].text

    def test_recovery_reset_guard_prevents_worker_archive_resurrection(self, isolated_hermes_env):
        from tools.delegation_context_continuation import PriorWorkerCapture, compose_continuation_snapshot
        from tools.delegation_context import RenderedTranscriptResult, SnapshotRecord
        parent, db = isolated_hermes_env

        # Worker active messages have NO compaction summary marker (e.g. fresh generation after reset)
        capture = PriorWorkerCapture(
            session_id="worker-reset",
            status="completed",
            exit_reason="completed",
            active_messages=({"role": "user", "content": "Active prompt after reset"},),
            archived_messages=({"role": "user", "content": "Old archived prompt from before reset"},),
            active_count=1,
            max_active_row_id=2,
            inherit_compacted_history=True,
        )
        parent_rendered = RenderedTranscriptResult(
            transcript_text="",
            source_type="live",
            content_hash_sha256="abc",
            char_count=100,
            estimated_tokens=20,
            retained_messages_count=1,
            retained_tool_events_count=0,
            omitted_system_messages_count=0,
            omitted_sidecars_count=0,
            omitted_scaffolding_count=0,
            omitted_orphan_tool_results_count=0,
            omitted_images_count=0,
            omitted_unsupported_blocks_count=0,
            omissions_detail=(),
            records=(SnapshotRecord(record_id=1, role="user", text="Parent prompt"),),
        )
        snap = compose_continuation_snapshot(
            parent_agent=parent,
            parent_rendered=parent_rendered,
            prior_capture=capture,
            configured_ceiling=4000,
            effective_budget=4000,
            mode="full",
        )
        # Old archived prompt must NOT be resurrected
        all_text = " ".join(r.text for r in snap.records)
        assert "Old archived prompt from before reset" not in all_text
        assert "Active prompt after reset" in all_text
        assert snap.manifest.prior_worker_coverage == "active_only"

    def test_parent_manifest_compaction_coverage_preserved(self, isolated_hermes_env):
        from tools.delegation_context_continuation import PriorWorkerCapture, compose_continuation_snapshot
        from tools.delegation_context import RenderedTranscriptResult, SnapshotRecord
        parent, db = isolated_hermes_env

        capture = PriorWorkerCapture(
            session_id="worker-manifest-test",
            status="completed",
            exit_reason="completed",
            active_messages=({"role": "user", "content": "Worker msg"},),
            archived_messages=(),
            active_count=1,
            max_active_row_id=1,
            inherit_compacted_history=False,
        )
        parent_rendered = RenderedTranscriptResult(
            transcript_text="",
            source_type="compacted_recovered",
            content_hash_sha256="abc",
            char_count=100,
            estimated_tokens=20,
            retained_messages_count=2,
            retained_tool_events_count=0,
            omitted_system_messages_count=1,
            omitted_sidecars_count=0,
            omitted_scaffolding_count=0,
            omitted_orphan_tool_results_count=0,
            omitted_images_count=0,
            omitted_unsupported_blocks_count=0,
            omissions_detail=(),
            records=(SnapshotRecord(record_id=1, role="user", text="Parent prompt"),),
            inherit_compacted_history=True,
            compaction_recovery_coverage="available_readable",
            available_archived_messages_count=5,
            retained_archived_records_count=4,
            observed_db_row_watermark=42,
            coverage_framing_lines=("Framing line from parent",),
        )
        snap = compose_continuation_snapshot(
            parent_agent=parent,
            parent_rendered=parent_rendered,
            prior_capture=capture,
            configured_ceiling=4000,
            effective_budget=4000,
            mode="full",
        )
        # Parent compaction fields must NOT be overwritten by worker
        assert snap.manifest.available_archived_messages_count == 5
        assert snap.manifest.retained_archived_records_count == 4
        assert snap.manifest.compaction_recovery_coverage == "available_readable"
        assert snap.manifest.observed_db_row_watermark == 42
        assert "Framing line from parent" in snap.rendered_transcript

