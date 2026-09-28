"""Tests for portable current-turn-preserving context inheritance in native delegation.

Validates:
1. Schema & strict boolean task normalization (strict rejection of explicit None/non-bool).
2. Source precedence: live _session_messages (even if empty) > conversation_history > session_db.
3. Chronological pairing: matching prior calls only, one completion per call, orphan detection without
   rendering orphan text, duplicate/reused ID handling, and mixed parallel batch scaffolding omission.
4. Immutability & parent/sibling isolation: frozen dataclass snapshots, deep-copied parent history,
   sibling manifest dictionary independence, and untouched caller task dictionaries.
5. Multimodal extraction & sanitization: image URLs, signed credentials, and base64 previews never
   exposed; non-string text blocks rejected; generic placeholders; sanitized omission receipts.
6. Fail-closed behavior on empty/missing context, real opaque compaction checkpoints, and budget
   clamping with small-window floor safety and conservative headroom.
7. Real temporary SessionDB integration: actual persistence path (_persist_session) -> registry handler ->
   delegate_task -> normal child runner -> captured child request -> real build_gemini_request (Gemini 3.8).
8. Default false unchanged, mixed batches, one snapshot per batch, failure before constructing/spawning
   any child, completion manifest receipts, and schema-retry retaining inherited evidence.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.gemini_native_adapter import build_gemini_request
from agent.native_compaction import has_compaction_checkpoint
from agent.session_persistence import SessionPersistenceMixin
from hermes_state import SessionDB
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.delegate_tool_tasks import _coerce_task_inherit_context
from tools.delegation_context import (
    BudgetExceededError,
    RequiredContextError,
    build_delegation_context_snapshot,
    resolve_parent_messages,
)
from tools.registry import registry


# ── Real SessionDB & Persistence Fixture ────────────────────────────────────


class RealParentAgent(SessionPersistenceMixin):
    """Real parent agent utilizing SessionPersistenceMixin with an isolated SessionDB."""

    def __init__(
        self,
        db: SessionDB,
        session_id: str = "parent-sess-001",
        model: str = "gemini-3.8-flash-high",
        depth: int = 1,
    ) -> None:
        self.session_id = session_id
        self._session_db = db
        self._session_db_created = True
        self._flushed_db_message_ids: set = set()
        self._last_flushed_db_idx = 0
        self._db_flush_scan_prefix: list = []
        self._session_messages: list = []
        self.conversation_history = None
        self._persist_disabled = False
        self._persist_lock_obj = None
        self.model = model
        self.provider = "google"
        self.base_url = "https://generativelanguage.googleapis.com"
        self.api_key = "test-api-key"
        self._delegate_depth = depth
        self.valid_tool_names = ["delegate_task", "read_file"]
        self.enabled_toolsets = ["delegation", "file"]
        self.disabled_toolsets: list = []
        self._active_children: list = []
        self._active_children_lock = threading.Lock()
        self._print_fn = None
        self.tool_progress_callback = None
        self.thinking_callback = None


@pytest.fixture
def isolated_hermes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HERMES_HOME and database under a test temporary directory."""
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr("tools.delegate_tool_config._get_max_spawn_depth", lambda: 2)
    db_path = home / "state.db"
    db = SessionDB(db_path)
    db.create_session("parent-sess-001", source="cli")
    agent = RealParentAgent(db, session_id="parent-sess-001", depth=1)
    try:
        yield agent, db
    finally:
        db.close()


# ── 1. Schema & Task Normalization ───────────────────────────────────────────


class TestTaskNormalizationAndSchema:
    def test_schema_declares_inherit_context_with_warning(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "inherit_context" in props
        assert props["inherit_context"]["type"] == "boolean"
        desc = props["inherit_context"]["description"]
        assert "WARNING:" in desc
        assert "transmits sanitized historical context across provider boundaries" in desc

    def test_strict_boolean_validation(self):
        # Default False when omitted
        tasks = [{"goal": "run audit"}]
        flags, err = _coerce_task_inherit_context(tasks)
        assert err is None
        assert flags == [False]

        # Valid explicit booleans
        tasks = [{"goal": "g1", "inherit_context": True}, {"goal": "g2", "inherit_context": False}]
        flags, err = _coerce_task_inherit_context(tasks)
        assert err is None
        assert flags == [True, False]

        # Strict rejection of explicit None
        tasks = [{"goal": "g1", "inherit_context": None}]
        flags, err = _coerce_task_inherit_context(tasks)
        assert err == "Task 0 'inherit_context' must be a boolean."
        assert flags == []

        # Strict rejection of strings, numbers, collections
        for invalid in ["true", "false", 1, 0, [], {}]:
            tasks = [{"goal": "g1", "inherit_context": invalid}]
            flags, err = _coerce_task_inherit_context(tasks)
            assert err == "Task 0 'inherit_context' must be a boolean."


# ── 2. Source Precedence ─────────────────────────────────────────────────────


class TestSourcePrecedence:
    def test_live_session_messages_wins_over_stale_history_and_db(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "db message"}])
        agent.conversation_history = [{"role": "user", "content": "history message"}]
        agent._session_messages = [{"role": "user", "content": "live session message"}]

        msgs, src = resolve_parent_messages(agent)
        assert src == "live_session_messages"
        assert msgs[0]["content"] == "live session message"

    def test_empty_live_session_messages_fails_closed_never_falls_back_to_db(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "db message"}])
        agent.conversation_history = [{"role": "user", "content": "history message"}]
        agent._session_messages = []

        msgs, src = resolve_parent_messages(agent)
        assert src == "live_session_messages"
        assert msgs == []

        with pytest.raises(RequiredContextError, match="is empty; cannot inherit context"):
            build_delegation_context_snapshot(agent)

    def test_conversation_history_fallback_when_session_messages_none(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "db message"}])
        agent._session_messages = None
        agent.conversation_history = [{"role": "user", "content": "history message"}]

        msgs, src = resolve_parent_messages(agent)
        assert src == "conversation_history"
        assert msgs[0]["content"] == "history message"

    def test_session_db_fallback_when_live_and_history_absent(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "db message only"}])
        agent._session_messages = None
        agent.conversation_history = None

        msgs, src = resolve_parent_messages(agent)
        assert src == "session_db"
        assert msgs[0]["content"] == "db message only"

    def test_no_source_fails_closed(self):
        class EmptyAgent:
            pass

        with pytest.raises(RequiredContextError, match="no accessible conversation context"):
            resolve_parent_messages(EmptyAgent())


# ── 3. Chronological Pairing & Scaffolding Preservation ───────────────────────


class TestChronologicalPairingAndSanitization:
    def test_current_turn_and_completed_evidence_preserved_and_scaffolding_omitted(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        messages = [
            {"role": "system", "content": "You are Hermes. CANARY_SECRET_SYSTEM_PROMPT."},
            {"role": "user", "content": "Turn 1: Please inspect system config."},
            {
                "role": "assistant",
                "content": "Checking configuration.",
                "thought": "Internal reasoning step",
                "tool_calls": [
                    {
                        "id": "call_read_conf",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "config.json"}'},
                    },
                    {
                        "id": "call_in_flight_delegate",
                        "type": "function",
                        "function": {"name": "delegate_task", "arguments": '{"tasks": [{"goal": "subtask"}]}'},
                    },
                ],
            },
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call_read_conf",
                "content": "config: {auth_mode: token_v2}",
            },
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        # Current turn user requirement preserved
        assert "Turn 1: Please inspect system config." in transcript
        # Completed tool call & result preserved
        assert "call_read_conf" in transcript
        assert "auth_mode: token_v2" in transcript
        # Incomplete in-flight scaffolding omitted
        assert "call_in_flight_delegate" not in transcript
        # Provider reasoning sidecar stripped
        assert "Internal reasoning step" not in transcript
        # System canary stripped
        assert "CANARY_SECRET_SYSTEM_PROMPT" not in transcript

        assert snapshot.manifest.retained_messages_count == 3
        assert snapshot.manifest.retained_tool_events_count == 1
        assert snapshot.manifest.omitted_system_messages_count == 1
        assert snapshot.manifest.omitted_sidecars_count == 1
        assert snapshot.manifest.omitted_scaffolding_count == 1
        assert snapshot.manifest.omitted_orphan_tool_results_count == 0

    def test_result_before_call_is_orphan_and_not_rendered(self, isolated_hermes_env):
        """Chronological pairing: tool result appearing before its call is an orphan; text never rendered."""
        agent, db = isolated_hermes_env
        messages = [
            {"role": "user", "content": "Run diagnosis."},
            # Premature tool result before assistant declares call_x
            {"role": "tool", "name": "check_status", "tool_call_id": "call_x", "content": "SECRET_PREMATURE_OUTPUT"},
            # Assistant declares call_x after the result was already delivered
            {
                "role": "assistant",
                "content": "Now issuing call",
                "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "check_status", "arguments": "{}"}}],
            },
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        # Premature result text is NEVER rendered
        assert "SECRET_PREMATURE_OUTPUT" not in transcript
        # Result was counted as orphan
        assert snapshot.manifest.omitted_orphan_tool_results_count == 1
        # The later tool call remains unresolved scaffolding
        assert snapshot.manifest.omitted_scaffolding_count == 1
        assert "call_x" not in transcript

    def test_duplicate_and_reused_call_ids_chronological_matching(self, isolated_hermes_env):
        """Reused call IDs across distinct turns are matched strictly chronologically (FIFO)."""
        agent, db = isolated_hermes_env
        messages = [
            {"role": "user", "content": "Turn 1 user request"},
            {
                "role": "assistant",
                "content": "Turn 1 assistant call",
                "tool_calls": [{"id": "call_reused", "function": {"name": "probe", "arguments": '{"n": 1}'}}],
            },
            {"role": "tool", "name": "probe", "tool_call_id": "call_reused", "content": "probe result 1"},
            {"role": "user", "content": "Turn 2 user request"},
            {
                "role": "assistant",
                "content": "Turn 2 assistant call",
                "tool_calls": [{"id": "call_reused", "function": {"name": "probe", "arguments": '{"n": 2}'}}],
            },
            {"role": "tool", "name": "probe", "tool_call_id": "call_reused", "content": "probe result 2"},
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        assert "probe result 1" in transcript
        assert "probe result 2" in transcript
        assert snapshot.manifest.omitted_orphan_tool_results_count == 0
        assert snapshot.manifest.omitted_scaffolding_count == 0
        assert snapshot.manifest.retained_tool_events_count == 2

    def test_duplicate_results_for_single_call_counts_orphan(self, isolated_hermes_env):
        """One completion per call: duplicate results for an already-completed call count as orphans."""
        agent, db = isolated_hermes_env
        messages = [
            {"role": "user", "content": "Do step"},
            {
                "role": "assistant",
                "content": "Calling tool",
                "tool_calls": [{"id": "call_single", "function": {"name": "op", "arguments": "{}"}}],
            },
            {"role": "tool", "name": "op", "tool_call_id": "call_single", "content": "valid result"},
            # Duplicate tool result
            {"role": "tool", "name": "op", "tool_call_id": "call_single", "content": "DUPLICATE_RESULT_TEXT"},
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        assert "valid result" in transcript
        assert "DUPLICATE_RESULT_TEXT" not in transcript
        assert snapshot.manifest.omitted_orphan_tool_results_count == 1
        assert snapshot.manifest.retained_tool_events_count == 1

    def test_mixed_parallel_complete_and_incomplete_scaffolding(self, isolated_hermes_env):
        """In parallel tool batch, completed calls are retained and unresolved calls are omitted as scaffolding."""
        agent, db = isolated_hermes_env
        messages = [
            {"role": "user", "content": "Parallel query"},
            {
                "role": "assistant",
                "content": "Starting parallel tasks",
                "tool_calls": [
                    {"id": "call_done", "function": {"name": "done_fn", "arguments": "{}"}},
                    {"id": "call_pending", "function": {"name": "pending_fn", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "name": "done_fn", "tool_call_id": "call_done", "content": "done output"},
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        assert "done output" in transcript
        assert "call_done" in transcript
        assert "call_pending" not in transcript
        assert snapshot.manifest.omitted_scaffolding_count == 1
        assert snapshot.manifest.omitted_orphan_tool_results_count == 0

    def test_multimodal_extraction_and_url_sanitization(self, isolated_hermes_env):
        """Image URLs (with presigned tokens) and base64 payloads are never exposed. Dict text fields validated."""
        agent, db = isolated_hermes_env
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect image evidence:"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://s3.amazonaws.com/evidence.png?X-Amz-Signature=LEAKED_CREDENTIAL_KEY"},
                    },
                    {
                        "type": "image",
                        "image_url": "data:image/png;base64,SECRET_BASE64_RAW_IMAGE_BYTES_THAT_MUST_NOT_LEAK",
                    },
                    # Non-string text field must not be str-cast
                    {"type": "text", "text": {"malformed": "dict_val"}},
                    # Untrusted type string must not be reflected
                    {"type": "<script>alert(1)</script>"},
                ],
            },
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        assert "LEAKED_CREDENTIAL_KEY" not in transcript
        assert "SECRET_BASE64_RAW_IMAGE_BYTES" not in transcript
        assert "<script>" not in transcript
        assert "dict_val" not in transcript

        assert "[Omitted image #1]" in transcript
        assert "[Omitted image #2]" in transcript
        assert "[Omitted unsupported multimodal block]" in transcript
        assert snapshot.manifest.omitted_images_count == 2
        assert snapshot.manifest.omitted_unsupported_blocks_count == 2

        # Verify omission receipts in manifest are fully sanitized (no URLs, credentials, or raw fields)
        for detail in snapshot.manifest.omissions_detail:
            assert "LEAKED_CREDENTIAL_KEY" not in detail
            assert "SECRET_BASE64" not in detail
            assert "<script>" not in detail


# ── 4. Immutability & Detachment ─────────────────────────────────────────────


class TestImmutabilityAndDetachment:
    def test_snapshot_and_manifest_are_frozen(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "hello test"}])
        snapshot = build_delegation_context_snapshot(agent)

        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            snapshot.rendered_transcript = "MUTATED"

        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            snapshot.manifest.snapshot_id = "MUTATED"

    def test_parent_messages_unmodified_by_snapshot(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent query"}])
        orig_copy = copy.deepcopy(agent._session_messages)

        snapshot = build_delegation_context_snapshot(agent)
        assert agent._session_messages == orig_copy

        # Mutating parent live list after snapshot does not mutate snapshot
        agent._session_messages.append({"role": "user", "content": "subsequent prompt"})
        assert "subsequent prompt" not in snapshot.rendered_transcript

    def test_sibling_manifest_isolation(self, isolated_hermes_env):
        """Two children receiving manifests from the same batch snapshot do not share mutable references."""
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "shared prompt"}])
        snapshot = build_delegation_context_snapshot(agent)

        manifest_1 = snapshot.manifest.to_dict()
        manifest_2 = snapshot.manifest.to_dict()

        manifest_1["custom_child_field"] = "child_1_mutation"
        assert "custom_child_field" not in manifest_2
        assert "custom_child_field" not in snapshot.manifest.to_dict()

    def test_caller_task_list_not_mutated(self, isolated_hermes_env):
        """Caller task dicts are not mutated by delegate_task (no inherit_context: False injected)."""
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent instructions"}])

        task1 = {"goal": "task 1"}
        task2 = {"goal": "task 2", "inherit_context": True}
        caller_tasks = [task1, task2]

        with patch("run_agent.AIAgent.run_conversation", return_value={"final_response": "ok", "completed": True}):
            delegate_task(tasks=caller_tasks, parent_agent=agent)

        # task1 was not given inherit_context; it must not be mutated
        assert "inherit_context" not in task1
        assert task2.get("inherit_context") is True


# ── 5. Compaction & Token Budgeting ──────────────────────────────────────────


class TestCompactionAndBudgetClamping:
    def test_missing_user_prompt_fails_closed(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "assistant", "content": "bot message only"}])
        with pytest.raises(RequiredContextError, match="No user prompt found"):
            build_delegation_context_snapshot(agent)

    def test_real_opaque_compaction_checkpoint_fails_closed(self, isolated_hermes_env):
        """Native compaction checkpoint with encrypted_content fails closed."""
        agent, db = isolated_hermes_env
        compaction_item = {"type": "compaction", "encrypted_content": "opaque_server_ciphertext_blob_xyz"}
        assert has_compaction_checkpoint([compaction_item]) is True

        messages = [
            {"role": "user", "content": "Perform migration."},
            {
                "role": "assistant",
                "content": "State after checkpoint",
                "codex_reasoning_items": [compaction_item],
            },
        ]
        agent._persist_session(messages)

        with pytest.raises(RequiredContextError, match="unsupported opaque compaction checkpoint"):
            build_delegation_context_snapshot(agent)

    def test_plain_text_summary_is_allowed(self, isolated_hermes_env):
        """Plain-text summary messages (e.g. from context compressor) are allowed and rendered."""
        agent, db = isolated_hermes_env
        messages = [
            {"role": "user", "content": "Historical query 1"},
            {"role": "assistant", "content": "[HISTORICAL TASK SUMMARY]\n1. Setup database\n2. Run migration"},
            {"role": "user", "content": "Now execute the final step."},
        ]
        agent._persist_session(messages)
        snapshot = build_delegation_context_snapshot(agent)
        transcript = snapshot.rendered_transcript

        assert "HISTORICAL TASK SUMMARY" in transcript
        assert "Now execute the final step." in transcript
        assert snapshot.manifest.retained_messages_count == 3

    def test_small_window_budget_floor_never_exceeds_window(self, isolated_hermes_env):
        """In very small context windows (e.g. 500 tokens), available budget floor is 0 <= 500 tokens."""
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "short user message"}])

        with patch("agent.model_metadata.get_model_context_length", return_value=500):
            # Window 500 tokens: reserve min(500, max(2048, 125)) = 500, window_available = 0
            with pytest.raises(BudgetExceededError) as exc_info:
                build_delegation_context_snapshot(agent)
            msg = str(exc_info.value)
            assert "approximate token estimate" in msg
            assert "effective token budget (0 tokens" in msg

    def test_budget_exceeded_fails_closed_with_approximate_reporting(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        long_content = "word " * 10000
        agent._persist_session([{"role": "user", "content": long_content}])

        with pytest.raises(BudgetExceededError, match="approximate token estimate"):
            build_delegation_context_snapshot(agent, config_override_tokens=500)


# ── 6. Real Temporary SessionDB Registry Dispatch Integration ─────────────────


class TestRealRegistryIntegration:
    """Exercises real temporary SessionDB -> registry handler -> delegate_task -> child runner -> build_gemini_request."""

    def test_real_registry_dispatch_first_turn_user_and_completed_evidence(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        # 1. Real persistence path before dispatch
        turn_messages = [
            {"role": "system", "content": "Hermes Agent system canary CANARY_SECRET_PWD."},
            {"role": "user", "content": "Requirement: migrate tenant_id to UUIDv7 format."},
            {
                "role": "assistant",
                "content": "Investigating table definitions...",
                "tool_calls": [
                    {
                        "id": "call_schema_check",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "schema.sql"}'},
                    },
                    {
                        "id": "call_in_flight_delegate",
                        "type": "function",
                        "function": {"name": "delegate_task", "arguments": '{"tasks": [{"goal": "run migration"}]}'},
                    },
                ],
            },
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call_schema_check",
                "content": "CREATE TABLE tenants (id UUID PRIMARY KEY, name TEXT);",
            },
        ]
        agent._persist_session(turn_messages)

        # Verify real SQLite database holds the rows
        persisted = db.get_messages_as_conversation("parent-sess-001")
        assert len(persisted) == 4

        captured_child_requests = []

        def fake_run_conversation(self, user_message, **kwargs):
            captured_child_requests.append(user_message)
            return {"final_response": "Migration script drafted.", "completed": True, "api_calls": 1}

        with patch("run_agent.AIAgent.run_conversation", fake_run_conversation):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler(
                {"tasks": [{"goal": "Generate migration script in v7.py", "inherit_context": True}]},
                parent_agent=agent,
            )

        res = json.loads(res_str)
        assert "results" in res
        assert len(res["results"]) == 1
        entry = res["results"][0]
        assert entry["status"] == "completed"
        assert "inherited_context" in entry
        assert isinstance(entry["inherited_context"], dict)
        assert entry["inherited_context"]["retained_tool_events_count"] == 1

        # Check captured child request
        assert len(captured_child_requests) == 1
        child_user_msg = captured_child_requests[0]
        assert "Requirement: migrate tenant_id to UUIDv7 format." in child_user_msg
        assert "CREATE TABLE tenants (id UUID PRIMARY KEY, name TEXT);" in child_user_msg
        assert "call_schema_check" in child_user_msg
        # Incomplete scaffolding omitted
        assert "call_in_flight_delegate" not in child_user_msg
        # System canary omitted
        assert "CANARY_SECRET_PWD" not in child_user_msg

        # Exercise real build_gemini_request with the captured child request for Gemini 3.8 model
        gemini_payload = build_gemini_request(
            messages=[{"role": "user", "content": child_user_msg}],
            model="gemini-3.8-flash-high",
        )
        assert "contents" in gemini_payload
        contents_str = json.dumps(gemini_payload["contents"])
        assert "UUIDv7" in contents_str
        assert "CREATE TABLE tenants" in contents_str

    def test_default_false_unchanged(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent history"}])

        captured = []

        def fake_run_conversation(self, user_message, **kwargs):
            captured.append(user_message)
            return {"final_response": "done", "completed": True}

        with patch("run_agent.AIAgent.run_conversation", fake_run_conversation):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({"tasks": [{"goal": "isolated goal"}]}, parent_agent=agent)

        res = json.loads(res_str)
        assert "inherited_context" not in res["results"][0]
        assert captured[0] == "isolated goal"

    def test_strict_none_and_nonbool_rejection(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "prompt"}])

        handler = registry.get_entry("delegate_task").handler

        # Explicit None
        res_str = handler({"tasks": [{"goal": "task", "inherit_context": None}]}, parent_agent=agent)
        assert "Task 0 'inherit_context' must be a boolean." in res_str

        # String
        res_str = handler({"tasks": [{"goal": "task", "inherit_context": "true"}]}, parent_agent=agent)
        assert "Task 0 'inherit_context' must be a boolean." in res_str

        # Int
        res_str = handler({"tasks": [{"goal": "task", "inherit_context": 1}]}, parent_agent=agent)
        assert "Task 0 'inherit_context' must be a boolean." in res_str

    def test_mixed_batch_isolation(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "shared parent knowledge"}])

        captured = []

        def fake_run_conversation(self, user_message, **kwargs):
            captured.append(user_message)
            return {"final_response": "ok", "completed": True}

        tasks = [
            {"goal": "task with inheritance", "inherit_context": True},
            {"goal": "task without inheritance"},
        ]

        with patch("run_agent.AIAgent.run_conversation", fake_run_conversation):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({"tasks": tasks}, parent_agent=agent)

        res = json.loads(res_str)
        results = res["results"]
        assert "inherited_context" in results[0]
        assert "inherited_context" not in results[1]
        assert "task without inheritance" in captured
        inherited = next(message for message in captured if message != "task without inheritance")
        assert "shared parent knowledge" in inherited

    def test_one_snapshot_per_batch(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent query"}])

        with patch("tools.delegate_tool.build_delegation_context_snapshot", wraps=build_delegation_context_snapshot) as spy_snapshot, \
                patch("run_agent.AIAgent.run_conversation", return_value={"final_response": "ok", "completed": True}):
            handler = registry.get_entry("delegate_task").handler
            handler(
                {"tasks": [
                    {"goal": "First delegated task goal description", "inherit_context": True},
                    {"goal": "Second delegated task goal description", "inherit_context": True},
                ]},
                parent_agent=agent,
            )

        assert spy_snapshot.call_count == 1

    def test_fail_before_constructing_or_spawning_children(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        # Empty conversation fails closed
        agent._session_messages = []

        with patch("tools.delegate_tool._build_children") as spy_build:
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({"tasks": [{"goal": "Goal with at least 10 chars", "inherit_context": True}]}, parent_agent=agent)

        assert "cannot inherit context" in res_str
        assert spy_build.call_count == 0

    def test_completion_manifest_in_result_and_no_magicmock(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "inspect stats"}])

        with patch("run_agent.AIAgent.run_conversation", return_value={"final_response": "ok", "completed": True}):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({"tasks": [{"goal": "t1", "inherit_context": True}]}, parent_agent=agent)

        res = json.loads(res_str)
        manifest = res["results"][0]["inherited_context"]
        assert isinstance(manifest, dict)
        assert "snapshot_id" in manifest
        assert "token_budget" in manifest
        assert "snapshot_digest" in manifest

    def test_real_schema_retry_retains_inherited_evidence(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([
            {"role": "user", "content": "Requirement: field code must be uppercase."},
            {
                "role": "assistant",
                "content": "Checking schema...",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "spec.txt"}'}}],
            },
            {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": "valid_format=ABC-123"},
        ])

        turns = []

        def fake_run_conversation(self, user_message, **kwargs):
            turns.append(user_message)
            if len(turns) == 1:
                # Turn 1 invalid according to schema
                return {
                    "final_response": "plain text not json",
                    "completed": True,
                    "api_calls": 1,
                    "messages": [{"role": "user", "content": user_message}, {"role": "assistant", "content": "plain text not json"}],
                }
            else:
                # Turn 2 (schema retry) returns valid json satisfying schema
                return {
                    "final_response": json.dumps({"code": "ABC-123"}),
                    "completed": True,
                    "api_calls": 1,
                    "messages": [{"role": "user", "content": user_message}, {"role": "assistant", "content": json.dumps({"code": "ABC-123"})}],
                }

        schema = {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        }

        with patch("run_agent.AIAgent.run_conversation", fake_run_conversation):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler(
                {"tasks": [{"goal": "Output json code", "inherit_context": True, "output_schema": schema}]},
                parent_agent=agent,
            )

        res = json.loads(res_str)
        result = res["results"][0]
        assert result["schema_valid"] is True
        assert result["schema_retries"] == 1
        assert len(turns) == 2
        # First turn received inherited context with evidence
        assert "valid_format=ABC-123" in turns[0]
        # Second turn received schema retry validation message
        assert "validation error" in turns[1].lower() or "schema" in turns[1].lower()

    def test_real_child_loop_keeps_context_through_schema_retry(self, isolated_hermes_env):
        """Run the real child loop/persistence; replace only inference, not run_conversation."""
        from openai.types.chat import ChatCompletion
        from run_agent import AIAgent

        parent, db = isolated_hermes_env
        parent._persist_session([
            {"role": "user", "content": "Inherited requirement: code must equal CONTEXT-ROUNDTRIP-83."},
        ])
        parent_before = copy.deepcopy(db.get_messages_as_conversation(parent.session_id))
        requests = []
        child_ids = []

        def inference(child, api_kwargs, **kwargs):
            requests.append(copy.deepcopy(api_kwargs))
            child_ids.append(child.session_id)
            # Exercise the actual Gemini converter on the actual assembled request.
            wire = build_gemini_request(
                messages=api_kwargs["messages"], model="gemini-3.8-flash-high",
            )
            assert "CONTEXT-ROUNDTRIP-83" in json.dumps(wire["contents"])
            content = "not json" if len(requests) == 1 else '{"code":"CONTEXT-ROUNDTRIP-83"}'
            return ChatCompletion(
                id=f"synthetic-{len(requests)}", created=0, model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": content}}],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference):
            result = json.loads(registry.get_entry("delegate_task").handler(
                {"tasks": [{"goal": "Return the required code as JSON", "inherit_context": True,
                            "output_schema": {"type": "object", "properties": {"code": {"type": "string"}},
                                              "required": ["code"]}}]},
                parent_agent=parent,
            ))
        entry = result["results"][0]
        assert entry["status"] == "completed"
        assert entry["schema_valid"] is True
        assert entry["schema_retries"] == 1
        assert len(requests) == 2
        assert len(set(child_ids)) == 1 and child_ids[0] != parent.session_id
        assert db.get_messages_as_conversation(parent.session_id) == parent_before
        assert sum("CONTEXT-ROUNDTRIP-83" in str(m.get("content", ""))
                   for m in requests[1]["messages"] if m["role"] == "user") == 1


    def test_tool_call_identifiers_are_not_normalized(self):
        from types import SimpleNamespace

        parent = SimpleNamespace(_session_messages=[
            {"role": "user", "content": "Inspect the tool evidence."},
            {"role": "assistant", "tool_calls": [
                {"id": " call-1", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "FALSE_MATCH_CANARY"},
            {"role": "tool", "tool_call_id": " call-1", "content": "EXACT_MATCH_EVIDENCE"},
        ])
        snapshot = build_delegation_context_snapshot(parent, child_model="gemini-3.8-flash-high")
        assert "FALSE_MATCH_CANARY" not in snapshot.rendered_transcript
        assert "EXACT_MATCH_EVIDENCE" in snapshot.rendered_transcript
        assert snapshot.manifest.omitted_orphan_tool_results_count == 1
