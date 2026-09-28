"""Focused tests for per-task inheritance budget override and initial request preflight.

Tests:
1. Schema & strict positive integer validation for inherit_max_tokens.
2. Override immutability: global config and caller task dicts remain untouched.
3. Sibling state & mixed budgets in batch: batch fails if any required inherited task cannot fit;
   when fitting, tasks have independent receipts reflecting requested and effective budgets.
4. >64k acceptance under genuine child room (Gemini 1M window) with low-level inference mocked.
5. Window and output reservation overflow rejection.
6. Honest compression trigger preflight: fails closed when initial request would immediately compress.
7. Cleanup: constructed-but-unrun children are detached and closed on preflight errors.
8. Default isolated tasks remain completely unchanged.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion

from agent.gemini_native_adapter import build_gemini_request
from agent.session_persistence import SessionPersistenceMixin
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.delegate_tool_config import _get_inherit_max_tokens
from tools.delegate_tool_tasks import _coerce_task_inherit_context, _coerce_task_inherit_max_tokens
from tools.delegation_context import (
    BudgetExceededError,
    build_batch_context_snapshots,
    build_delegation_context_snapshot,
)
from tools.delegation_context_budget import (
    calculate_task_inheritance_budget,
    preflight_child_initial_request,
    preflight_children_budget,
)
from tools.registry import registry


class RealParentAgent(SessionPersistenceMixin):
    """Parent agent using SessionPersistenceMixin with an isolated SessionDB."""

    def __init__(
        self,
        db: SessionDB,
        session_id: str = "parent-sess-budget",
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
def budget_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate HERMES_HOME and SessionDB under tmp_path."""
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr("tools.delegate_tool_config._get_max_spawn_depth", lambda: 2)
    db_path = home / "state.db"
    db = SessionDB(db_path)
    db.create_session("parent-sess-budget", source="cli")
    agent = RealParentAgent(db, session_id="parent-sess-budget", depth=1)
    try:
        yield agent, db
    finally:
        db.close()


# ── 1. Schema & Strict Validation ─────────────────────────────────────────────


class TestValidationAndSchema:
    def test_schema_declares_inherit_max_tokens(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "inherit_max_tokens" in props
        assert props["inherit_max_tokens"]["type"] == "integer"
        desc = props["inherit_max_tokens"]["description"]
        assert "requires inherit_context: true" in desc
        assert "64,000" in desc

    def test_strict_positive_integer_validation(self):
        # inherit_max_tokens without inherit_context: true fails
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_max_tokens": 100000}]
        _, err = _coerce_task_inherit_max_tokens(tasks, [False])
        assert err == "Task 0 'inherit_max_tokens' is only valid when 'inherit_context' is true."

        # inherit_max_tokens with inherit_context: False explicitly fails
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": False, "inherit_max_tokens": 100000}]
        _, err = _coerce_task_inherit_max_tokens(tasks, [False])
        assert err == "Task 0 'inherit_max_tokens' is only valid when 'inherit_context' is true."

        # inherit_max_tokens: None fails
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": None}]
        _, err = _coerce_task_inherit_max_tokens(tasks, [True])
        assert err == "Task 0 'inherit_max_tokens' must be a positive integer."

        # inherit_max_tokens: bool (True or False) fails
        for b_val in (True, False):
            tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": b_val}]
            _, err = _coerce_task_inherit_max_tokens(tasks, [True])
            assert err == "Task 0 'inherit_max_tokens' must be a positive integer."

        # inherit_max_tokens: <= 0 fails
        for non_pos in (0, -1, -500):
            tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": non_pos}]
            _, err = _coerce_task_inherit_max_tokens(tasks, [True])
            assert err == "Task 0 'inherit_max_tokens' must be a positive integer."

        # inherit_max_tokens: float fails
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": 128000.5}]
        _, err = _coerce_task_inherit_max_tokens(tasks, [True])
        assert err == "Task 0 'inherit_max_tokens' must be a positive integer."

        # inherit_max_tokens: string fails
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": "128000"}]
        _, err = _coerce_task_inherit_max_tokens(tasks, [True])
        assert err == "Task 0 'inherit_max_tokens' must be a positive integer."

        # Valid positive int succeeds
        tasks = [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": 128000}]
        toks, err = _coerce_task_inherit_max_tokens(tasks, [True])
        assert err is None
        assert toks == [128000]

    def test_handler_rejects_invalid_inherit_max_tokens(self, budget_test_env):
        agent, _ = budget_test_env
        handler = registry.get_entry("delegate_task").handler

        res_str = handler({
            "tasks": [{"goal": "Goal with at least 10 chars", "inherit_max_tokens": 100000}]
        }, parent_agent=agent)
        assert "is only valid when 'inherit_context' is true" in res_str

        res_str = handler({
            "tasks": [{"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": "large"}]
        }, parent_agent=agent)
        assert "must be a positive integer" in res_str


# ── 2. Immutability & Sibling State ──────────────────────────────────────────


class TestImmutabilityAndSiblings:
    def test_override_does_not_mutate_config_or_caller_dicts(self, budget_test_env):
        agent, _ = budget_test_env
        agent._session_messages = [{"role": "user", "content": "Parent user query."}]

        orig_default = _get_inherit_max_tokens()
        assert orig_default == 64000

        task_dict = {"goal": "Goal with at least 10 chars", "inherit_context": True, "inherit_max_tokens": 120000}
        task_dict_copy = copy.deepcopy(task_dict)

        with patch("run_agent.AIAgent.run_conversation", return_value={"final_response": "ok", "completed": True}):
            handler = registry.get_entry("delegate_task").handler
            handler({"tasks": [task_dict]}, parent_agent=agent)

        # Global config default is unchanged
        assert _get_inherit_max_tokens() == orig_default
        # Caller task dictionary is untouched
        assert task_dict == task_dict_copy

    def test_mixed_budgets_in_batch_fails_if_any_cannot_fit(self, budget_test_env):
        agent, _ = budget_test_env
        # Create a parent history of ~70,000 tokens (exceeds default 64k ceiling)
        # ~4 chars per token -> ~280,000 characters
        long_content = "important historical fact " * 11000  # ~286,000 chars, ~71,500 tokens
        agent._session_messages = [{"role": "user", "content": long_content}]

        # Task 0 has override 120,000 (would fit ~71,500 tokens)
        # Task 1 has default ceiling 64,000 (cannot fit ~71,500 tokens)
        tasks = [
            {"goal": "Task 0 description with override", "inherit_context": True, "inherit_max_tokens": 120000},
            {"goal": "Task 1 description with default", "inherit_context": True},
        ]

        with patch("tools.delegate_tool._build_children") as spy_build:
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({"tasks": tasks}, parent_agent=agent)

        # The whole batch must fail closed before constructing any children!
        assert "exceeds effective token budget" in res_str
        assert spy_build.call_count == 0

    def test_mixed_budgets_receipt_accuracy_and_shared_transcript(self, budget_test_env):
        agent, _ = budget_test_env
        # Moderate history ~10,000 tokens (fits in both 64k and 100k)
        agent._session_messages = [{"role": "user", "content": "historical knowledge " * 2000}]

        flags = [True, True]
        overrides = [100000, None]

        snapshots = build_batch_context_snapshots(
            agent,
            flags,
            overrides,
            child_model="gemini-3.8-flash-high",
            child_provider="google",
        )

        assert len(snapshots) == 2
        snap0, snap1 = snapshots[0], snapshots[1]

        # Invariant: identical immutable rendered_transcript string reference in memory (no duplication)
        assert snap0.rendered_transcript is snap1.rendered_transcript

        # Manifest receipts accurately report requested vs effective budget
        m0 = snap0.manifest.to_dict()
        m1 = snap1.manifest.to_dict()

        assert m0["requested_budget"] == 100000
        assert m0["effective_budget"] == 100000
        assert m0["token_budget"] == 100000

        assert m1["requested_budget"] == 64000
        assert m1["effective_budget"] == 64000
        assert m1["token_budget"] == 64000

        # Sibling isolation: mutating one dict does not affect the other
        m0["custom"] = "mutated"
        assert "custom" not in m1


# ── 3. Real Child Path & >64k Acceptance ─────────────────────────────────────


class TestRealChildPathAndOverride:
    def test_greater_than_64k_accepted_with_override_and_real_loop(self, budget_test_env):
        agent, db = budget_test_env
        # Persist a parent history of ~70,000 tokens
        # ~280,000 characters
        chunk = "context_key=ROUNDTRIP_KEY_400001 " * 8500
        agent._persist_session([
            {"role": "user", "content": chunk},
        ])

        requests = []

        def low_level_inference(child, api_kwargs, **kwargs):
            requests.append(copy.deepcopy(api_kwargs))
            # Verify Gemini converter processes the assembled request successfully
            wire = build_gemini_request(
                messages=api_kwargs["messages"], model="gemini-3.8-flash-high",
            )
            assert "ROUNDTRIP_KEY_400001" in json.dumps(wire["contents"])
            return ChatCompletion(
                id="syn-1", created=0, model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": '{"result": "success"}'}}],
                usage={"prompt_tokens": 72000, "completion_tokens": 10, "total_tokens": 72010},
            )

        handler = registry.get_entry("delegate_task").handler

        # First verify: without inherit_max_tokens, default 64,000 rejects this 70k context!
        res_fail_str = handler({
            "tasks": [{"goal": "Goal with at least 10 chars", "inherit_context": True}]
        }, parent_agent=agent)
        assert "exceeds effective token budget" in res_fail_str

        # Second verify: with inherit_max_tokens: 128000, it is accepted and executes!
        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", low_level_inference):
            res_success_str = handler({
                "tasks": [{
                    "goal": "Goal with at least 10 chars",
                    "inherit_context": True,
                    "inherit_max_tokens": 128000,
                }]
            }, parent_agent=agent)

        res = json.loads(res_success_str)
        entry = res["results"][0]
        assert entry["status"] == "completed"
        assert "inherited_context" in entry
        manifest = entry["inherited_context"]
        assert manifest["requested_budget"] == 128000
        assert manifest["effective_budget"] == 128000
        assert manifest["token_budget"] == 128000
        assert len(requests) == 1
        from agent.model_metadata import estimate_request_tokens_rough

        assembled_estimate = estimate_request_tokens_rough(
            requests[0]["messages"], tools=requests[0].get("tools", []),
        )
        # Worker-thread cwd/timezone hints settle after preview. Verify the
        # documented conservative framing reserve against the real request.
        preflight = manifest["preflight"]
        assert preflight["guarded_initial_input_tokens"] >= assembled_estimate
        assert (preflight["guarded_initial_input_tokens"] - assembled_estimate
                <= 2 * preflight["request_framing_reserve"])


# ── 4. Initial Request Preflight & Honest Compression Preview ─────────────────


class TestPreflightAndCompressionGuard:
    def test_preflight_rejects_model_window_overflow(self, budget_test_env):
        agent, _ = budget_test_env
        agent._session_messages = [{"role": "user", "content": "Some context."}]

        child = MagicMock()
        child.model = "small-model"
        child.provider = "test"
        child.base_url = ""
        child.api_key = ""
        child.tools = []
        child._cached_system_prompt = None
        child.ephemeral_system_prompt = ""
        child._build_system_prompt = MagicMock(return_value="System prompt")
        child._delegate_images = []
        child.prefill_messages = []

        # Child snapshot attached
        snap = build_delegation_context_snapshot(agent, child_model="small-model", child_provider="test")
        child._inherited_context_snapshot = snap

        # Context compressor mock reporting tiny usable tokens
        cc = MagicMock()
        cc.get_budget_report.return_value = {
            "context_limit": 100,
            "output_reservation": 50,
            "usable_tokens": 50,
            "actual_trigger": 40,
        }
        child.context_compressor = cc

        err = preflight_child_initial_request(0, {"goal": "Long goal description", "inherit_context": True}, child)
        assert err is not None
        assert "exceeds context window" in err

    def test_preflight_rejects_immediate_compression_trigger(self, budget_test_env):
        agent, _ = budget_test_env
        agent._session_messages = [{"role": "user", "content": "Some context."}]

        child = MagicMock()
        child.model = "test-model"
        child.provider = "test"
        child.base_url = ""
        child.api_key = ""
        child.tools = []
        child._cached_system_prompt = None
        child.ephemeral_system_prompt = ""
        child._build_system_prompt = MagicMock(return_value="System prompt")
        child._delegate_images = []
        child.prefill_messages = []

        snap = build_delegation_context_snapshot(agent, child_model="test-model", child_provider="test")
        child._inherited_context_snapshot = snap

        # Context compressor mock where actual_trigger is smaller than request tokens
        # Initial request will be ~50-100 tokens, so set actual_trigger = 10
        cc = MagicMock()
        cc.get_budget_report.return_value = {
            "context_limit": 100000,
            "output_reservation": 1000,
            "usable_tokens": 99000,
            "actual_trigger": 10,
        }
        child.context_compressor = cc

        err = preflight_child_initial_request(0, {"goal": "Goal with at least 10 chars", "inherit_context": True}, child)
        assert err is not None
        assert "reaches or exceeds child compression trigger" in err
        assert "Full inherited snapshot cannot be preserved without immediate compaction" in err

    def test_constructed_children_cleaned_up_on_preflight_error(self, budget_test_env):
        agent, _ = budget_test_env
        agent._session_messages = [{"role": "user", "content": "Context line."}]

        # Simulate preflight rejecting by patching preflight_children_budget to fail
        with patch("tools.delegate_tool.preflight_children_budget", return_value="Preflight compression error"):
            handler = registry.get_entry("delegate_task").handler
            res_str = handler({
                "tasks": [{"goal": "Goal with at least 10 chars", "inherit_context": True}]
            }, parent_agent=agent)

        assert "Preflight compression error" in res_str
        # Verify active children were cleaned up from parent
        assert len(agent._active_children) == 0

    def test_default_isolated_tasks_unaffected_by_preflight(self, budget_test_env):
        agent, _ = budget_test_env
        agent._session_messages = [{"role": "user", "content": "Context line."}]

        # Isolated task (inherit_context omitted or False)
        child = MagicMock()
        child._inherited_context_snapshot = None  # No inherited context

        err = preflight_child_initial_request(0, {"goal": "Isolated task goal"}, child)
        assert err is None

    def test_task_scoped_prompt_is_counted_without_resolving_threshold(self):
        from types import SimpleNamespace

        class PreviewOnlyBudget:
            @property
            def threshold_tokens(self):
                raise AssertionError("Preflight must not read the mutating threshold property")

            def get_budget_report(self):
                return {"context_limit": 20000, "output_reservation": 2000,
                        "usable_tokens": 18000, "actual_trigger": 1000}

        child = SimpleNamespace(
            _inherited_context_snapshot=SimpleNamespace(rendered_transcript="context"),
            _cached_system_prompt="base", ephemeral_system_prompt="output contract " * 1000,
            tools=[], prefill_messages=[], _delegate_images=[], model="test-model",
            context_compressor=PreviewOnlyBudget(),
        )
        error = preflight_child_initial_request(0, {"goal": "task", "inherit_context": True}, child)
        assert "child compression trigger" in error

    def test_failed_prompt_or_budget_preview_does_not_fall_back_to_guesses(self):
        from types import SimpleNamespace

        child = SimpleNamespace(
            _inherited_context_snapshot=SimpleNamespace(rendered_transcript="context"),
            _cached_system_prompt=None, ephemeral_system_prompt="", tools=[],
            prefill_messages=[], _delegate_images=[], model="test-model",
            _build_system_prompt=MagicMock(side_effect=RuntimeError("synthetic prompt failure")),
        )
        task = {"goal": "task", "inherit_context": True}
        assert "cannot preview its system prompt" in preflight_child_initial_request(0, task, child)
        child._cached_system_prompt = "base"
        child.context_compressor = SimpleNamespace(get_budget_report=lambda: {})
        assert "incomplete compression budget report" in preflight_child_initial_request(0, task, child)

    def test_missing_required_snapshot_is_not_accepted(self):
        from types import SimpleNamespace

        error = preflight_child_initial_request(
            0, {"goal": "task", "inherit_context": True}, SimpleNamespace(),
        )
        assert "requires an inherited snapshot" in error
