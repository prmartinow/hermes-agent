"""Focused unit and integration tests for opt-in bounded context inheritance.

Invariants verified:
- Default / full inheritance behavior remains unchanged.
- Oversized full inheritance fails closed while bounded inheritance fits within budget.
- Underlying immutable history records remain complete and retrievable in bounded mode.
- Latest user prompt is never evicted, even under tight budget.
- Different sibling tasks with different goals/budgets share the exact same backing records.
- Complete, honest coverage receipts (mode, digests, counts, policy).
- Tiny budgets that cannot fit the latest user prompt fail closed explicitly.
- Strict validation of inherit_context_mode (exact strings, requires inherit_context=True).
- Reader availability gating (direct and deferred accepted, missing fails closed, full mode exempt).
"""

import copy
import hashlib
import json
import pytest
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from tools.delegate_tool import delegate_task
from tools.delegation_context import (
    BudgetExceededError,
    ContextSnapshot,
    RequiredContextError,
    SnapshotManifest,
    SnapshotRecord,
    build_batch_context_snapshots,
    build_delegation_context_snapshot,
)
from tools.delegation_context_budget import (
    is_session_search_callable,
    preflight_child_initial_request,
)
from tools.delegation_context_reader import dispatch_snapshot_search
from tools.delegation_context_selection import (
    select_bounded_context_records,
)
from tools.registry import registry
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


class TestBoundedContextSelection:
    """Deterministic selection and budgeting invariants."""

    def test_latest_user_prompt_never_evicted(self):
        records = (
            SnapshotRecord(record_id=1, role="user", text="alpha topic details " * 20),
            SnapshotRecord(record_id=2, role="assistant", text="alpha response"),
            SnapshotRecord(record_id=3, role="user", text="beta topic details " * 20),
            SnapshotRecord(record_id=4, role="assistant", text="beta response"),
            SnapshotRecord(record_id=5, role="user", text="Final instruction: do task gamma."),
        )
        # Even with goal heavily favoring 'alpha', latest user prompt (record 5) MUST be included
        res = select_bounded_context_records(
            records,
            goal="Tell me all about alpha topic details",
            effective_budget=350,
            source_type="live_session_messages",
        )
        assert 5 in res.selected_record_ids
        assert "Final instruction: do task gamma." in res.rendered_transcript

    def test_tiny_budget_explicit_failure(self):
        records = (
            SnapshotRecord(record_id=1, role="user", text="Hello world"),
        )
        with pytest.raises(BudgetExceededError) as exc_info:
            select_bounded_context_records(
                records,
                goal="Any goal",
                effective_budget=10,  # Far too small for framing + prompt
            )
        assert "exceeds effective token budget" in str(exc_info.value)
        assert "latest user prompt alone" in str(exc_info.value)

    def test_bounded_fits_while_full_exceeds_budget(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        # Create a multi-turn history with ~600 tokens
        history = [
            {"role": "user", "content": "Evidence part 1: database config is host=localhost port=5432."},
            {"role": "assistant", "content": "Acknowledged database config."},
            {"role": "user", "content": "Evidence part 2: cache config is host=redis-node port=6379 " * 10},
            {"role": "assistant", "content": "Acknowledged redis config."},
            {"role": "user", "content": "Current request: check the database settings."},
        ]
        agent._persist_session(history)

        # Budget of 250 tokens: full mode should fail, bounded mode should fit!
        with pytest.raises(BudgetExceededError):
            build_delegation_context_snapshot(
                agent,
                config_override_tokens=250,
                inherit_context_mode="full",
            )

        snap = build_delegation_context_snapshot(
            agent,
            config_override_tokens=250,
            inherit_context_mode="bounded",
            goal="Check database settings",
        )
        assert snap.manifest.mode == "bounded"
        assert snap.manifest.estimated_tokens <= 250
        assert snap.manifest.omitted_records_count > 0
        # Underlying records remain 100% intact!
        assert len(snap.records) == 5

        # Check that omitted record is still retrievable via reader
        res_json = dispatch_snapshot_search(
            snap,
            "snapshot",
            around_message_id=3,
            window=0,
            max_chars=1000,
        )
        res = json.loads(res_json)
        assert res["success"] is True
        assert "redis-node" in res["messages"][0]["content"]

    def test_sibling_tasks_share_immutable_records_with_different_seeds(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        history = [
            {"role": "user", "content": "Topic A discussion about postgresql schemas and tables."},
            {"role": "assistant", "content": "Postgres schema noted."},
            {"role": "user", "content": "Topic B discussion about redis keys and cache eviction."},
            {"role": "assistant", "content": "Redis caching noted."},
            {"role": "user", "content": "Latest turn: coordinate deployment."},
        ]
        agent._persist_session(history)

        snaps = build_batch_context_snapshots(
            agent,
            task_inherit_contexts=[True, True],
            task_inherit_max_tokens=[250, 250],
            task_inherit_context_modes=["bounded", "bounded"],
            task_goals=["Audit postgresql schemas", "Analyze redis keys"],
        )
        assert len(snaps) == 2
        snap1, snap2 = snaps[0], snaps[1]
        assert snap1 is not None and snap2 is not None

        # Underlying records tuple is identical object in memory!
        assert snap1.records is snap2.records
        assert snap1.manifest.source_hash_sha256 == snap2.manifest.source_hash_sha256
        assert snap1.manifest.source_records_count == snap2.manifest.source_records_count

        # But initial seeds differ according to their task goals!
        assert "postgresql" in snap1.rendered_transcript
        assert "redis" in snap2.rendered_transcript
        assert snap1.manifest.content_hash_sha256 != snap2.manifest.content_hash_sha256

    def test_accurate_counts_and_receipts(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([
            {"role": "user", "content": "Step 1: initialize cluster"},
            {"role": "assistant", "content": "Cluster initialized"},
            {"role": "user", "content": "Step 2: deploy workload"},
        ])
        snap = build_delegation_context_snapshot(
            agent,
            config_override_tokens=5000,
            inherit_context_mode="bounded",
            goal="deploy workload",
        )
        m = snap.manifest.to_dict()
        assert m["mode"] == "bounded"
        assert m["source_records_count"] == 3
        assert m["selection_policy"] == "deterministic_terms_density_and_recency"
        assert "source_digest" in m
        assert m["source_digest"].startswith("sha256:")
        assert "seed_estimated_tokens" in m
        assert "seed_content_hash_sha256" in m
        assert isinstance(m["selected_record_ids"], list)
        assert m["omitted_records_count"] == 3 - len(m["selected_record_ids"])


class TestValidationAndGating:
    """Strict argument validation and reader availability gating."""

    def test_inherit_context_mode_validation(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        handler = registry.get_entry("delegate_task").handler

        # 1. Non-exact string / wrong case fails
        res1 = json.loads(handler({
            "tasks": [{"goal": "valid goal 123", "inherit_context": True, "inherit_context_mode": "BOUNDED"}]
        }, parent_agent=agent))
        assert "error" in res1
        assert "inherit_context_mode" in res1["error"]

        # 2. Unknown mode fails
        res2 = json.loads(handler({
            "tasks": [{"goal": "valid goal 123", "inherit_context": True, "inherit_context_mode": "compressed"}]
        }, parent_agent=agent))
        assert "error" in res2
        assert "inherit_context_mode" in res2["error"]

        # 3. inherit_context=False with inherit_context_mode fails
        res3 = json.loads(handler({
            "tasks": [{"goal": "valid goal 123", "inherit_context": False, "inherit_context_mode": "bounded"}]
        }, parent_agent=agent))
        assert "error" in res3
        assert "only valid when 'inherit_context' is true" in res3["error"]

    def test_missing_reader_fails_preflight(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent query"}])

        child = MagicMock()
        child.valid_tool_names = {"read_file", "patch"}
        child.tools = [{"type": "function", "function": {"name": "read_file"}}]
        child.enabled_toolsets = ["file"]
        child.disabled_toolsets = []
        child._inherited_context_manifest = {"mode": "bounded"}
        child._inherited_context_snapshot = MagicMock(rendered_transcript="some text")

        err = preflight_child_initial_request(
            0,
            {"goal": "test goal", "inherit_context": True, "inherit_context_mode": "bounded"},
            child,
            parent_agent=agent,
        )
        assert err is not None
        assert "session_search is not callable" in err or "session_search is not available" in err

    def test_deferred_reader_accepted(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent.valid_tool_names = {"tool_call", "tool_search", "tool_describe"}
        agent.enabled_toolsets = ["delegation", "session_search"]
        assert is_session_search_callable(agent) is True

    def test_full_mode_exempt_from_reader_requirement(self, isolated_hermes_env):
        agent, db = isolated_hermes_env
        agent._persist_session([{"role": "user", "content": "parent query"}])
        child = MagicMock()
        child.model = "test-model"
        child.valid_tool_names = {"read_file"}
        child.tools = []
        child._cached_system_prompt = "system prompt"
        child.ephemeral_system_prompt = ""
        child._delegate_images = []
        child.prefill_messages = []
        cc = MagicMock()
        cc.get_budget_report.return_value = {
            "context_limit": 100000,
            "output_reservation": 2000,
            "usable_tokens": 80000,
            "actual_trigger": 75000,
            "safety_headroom": 1024,
        }
        child.context_compressor = cc
        child._inherited_context_manifest = {"mode": "full"}
        snap = build_delegation_context_snapshot(agent, inherit_context_mode="full")
        child._inherited_context_snapshot = snap

        # Full mode does not require session_search
        err = preflight_child_initial_request(
            0,
            {"goal": "test goal 12345", "inherit_context": True, "inherit_context_mode": "full"},
            child,
            parent_agent=agent,
        )
        assert err is None
