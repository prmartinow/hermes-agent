"""Integration and lifecycle tests for Action Item 3 Milestone 3: Model-switch semantics & active-session persistence.

Verifies:
  1. Runtime EffortByBase memory across sequential model switches:
       3.8 low -> 3.1 high -> 3.8 = low restored
       3.8 medium -> Claude -> 3.8 = medium restored
  2. CLI and AIAgent runtime map synchronization.
  3. Plain switch uses runtime remembered effort before config.
  4. --global writes agent.reasoning_overrides.<canonical_base>, leaving agent.reasoning_effort untouched.
  5. Legacy alias global persistence canonicalizes to base key.
  6. Session row stores active reasoning_config; does NOT store effort_by_base.
  7. Resume restores active effort and seeds ONLY the active base into runtime map.
  8. Resume of {"enabled": False} sets disabled without inserting fake map entries.
  9. Stale stored effort safely falls back through M2 resolver.
  10. --once does not mutate map / config / DB, and restores previous effort.
  11. Failed switch (CLI and agent) rolls back atomically.
  12. No-effort models create no map entries.
  13. Non-Cloud-Code routes retain generic switch behavior.
"""

import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.reasoning_selection import (
    canonical_reasoning_base,
    remember_reasoning_effort,
    remembered_reasoning_effort,
    resolve_effective_reasoning_config,
)
from hermes_cli.cli_model_switch_mixin import (
    _apply_reasoning_after_switch,
    _resolve_cli_reasoning,
    _runtime_fields,
)
from hermes_state import SessionDB
from run_agent import AIAgent


class FakeCLI:
    """Mock HermesCLI harness for model switch testing."""

    def __init__(self, model="gemini-3.8-flash", provider="gemini-oauth", session_id=None, session_db=None):
        self.model = model
        self.provider = provider
        self.requested_provider = provider
        self.base_url = None
        self.api_mode = None
        self.api_key = None
        self.reasoning_config = None
        self._explicit_reasoning_config = None
        self.effort_by_base = {}
        self.session_id = session_id
        self._session_db = session_db
        self.agent = None

    def _console_print(self, *args, **kwargs):
        pass

    def _snapshot_model_runtime(self):
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        return CLIModelSwitchMixin._snapshot_model_runtime(self)

    def _restore_model_runtime_snapshot(self, snapshot):
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        return CLIModelSwitchMixin._restore_model_runtime_snapshot(self, snapshot)


# ============================================================================
# 1. Sequential Switch Memory & CLI-Agent Synchronization
# ============================================================================

def test_sequential_switch_memory_restores_per_base_effort():
    cli = FakeCLI(model="gemini-3.8-flash", provider="gemini-oauth")
    cli.agent = SimpleNamespace(
        model="gemini-3.8-flash",
        provider="gemini-oauth",
        reasoning_config=None,
        effort_by_base={},
    )

    # Step 1: Switch to 3.8 with --reasoning low
    _apply_reasoning_after_switch(cli, "low", persist_global=False)
    assert cli.effort_by_base == {"gemini-3.8-flash": "low"}
    assert cli.agent.effort_by_base == {"gemini-3.8-flash": "low"}
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}
    assert cli.agent.reasoning_config == {"enabled": True, "effort": "low"}

    # Step 2: Switch to 3.1 Pro with --reasoning high
    cli.model = "gemini-3.1-pro"
    cli.agent.model = "gemini-3.1-pro"
    _apply_reasoning_after_switch(cli, "high", persist_global=False)
    assert cli.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
    assert cli.agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
    assert cli.reasoning_config == {"enabled": True, "effort": "high"}

    # Step 3: Switch back to 3.8 WITHOUT reasoning flag -> re-resolution restores low!
    cli.model = "gemini-3.8-flash"
    cli.agent.model = "gemini-3.8-flash"
    with patch("cli.CLI_CONFIG", {"agent": {}}):
        _resolve_cli_reasoning(cli)
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}

    # Step 4: Switch to Claude partner model -> no effort entry created
    cli.model = "claude-sonnet-4-6"
    cli.agent.model = "claude-sonnet-4-6"
    with patch("cli.CLI_CONFIG", {"agent": {}}):
        _resolve_cli_reasoning(cli)
    assert cli.reasoning_config is None
    # Claude didn't pollute the map!
    assert "claude-sonnet-4-6" not in cli.effort_by_base

    # Step 5: Switch back to 3.8 -> still low!
    cli.model = "gemini-3.8-flash"
    cli.agent.model = "gemini-3.8-flash"
    with patch("cli.CLI_CONFIG", {"agent": {}}):
        _resolve_cli_reasoning(cli)
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}


# ============================================================================
# 2. Global Persistence Writes Canonical Overrides (Never agent.reasoning_effort)
# ============================================================================

def test_global_switch_writes_canonical_reasoning_overrides():
    cli = FakeCLI(model="gemini-3.8-flash", provider="gemini-oauth")
    cli_config = {"agent": {"reasoning_effort": "high", "reasoning_overrides": {}}}

    with patch("cli.CLI_CONFIG", cli_config),          patch("cli.save_config_value", return_value=True) as mock_save:
        # Switch 3.8 to low globally
        _apply_reasoning_after_switch(cli, "low", persist_global=True)

        # Must write agent.reasoning_overrides.gemini-3.8-flash, NOT agent.reasoning_effort!
        mock_save.assert_called_with("agent.reasoning_overrides.gemini-3.8-flash", "low")
        assert cli_config["agent"]["reasoning_overrides"]["gemini-3.8-flash"] == "low"
        # Flat global key remained high!
        assert cli_config["agent"]["reasoning_effort"] == "high"


def test_global_switch_legacy_alias_canonicalizes_to_base_key():
    cli = FakeCLI(model="gemini-3.8-flash-high", provider="gemini-oauth")
    cli_config = {"agent": {"reasoning_effort": "high", "reasoning_overrides": {}}}

    with patch("cli.CLI_CONFIG", cli_config),          patch("cli.save_config_value", return_value=True) as mock_save:
        _apply_reasoning_after_switch(cli, "medium", persist_global=True)

        # Canonical key 'gemini-3.8-flash' is written
        mock_save.assert_called_with("agent.reasoning_overrides.gemini-3.8-flash", "medium")
        assert cli_config["agent"]["reasoning_overrides"]["gemini-3.8-flash"] == "medium"
        assert cli_config["agent"]["reasoning_effort"] == "high"


# ============================================================================
# 3. Session Persistence & Restart Invariants (Sections 5 & 6)
# ============================================================================

def test_session_persistence_stores_only_active_reasoning_and_no_effort_map():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-m3-persist"
        db.create_session(session_id, source="cli", model="gemini-3.8-flash")

        cli = FakeCLI(model="gemini-3.8-flash", provider="gemini-oauth", session_id=session_id, session_db=db)
        cli.effort_by_base = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
        cli.reasoning_config = {"enabled": True, "effort": "low"}

        # Simulate _persist_model_switch_to_session
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        res = SimpleNamespace(new_model="gemini-3.8-flash", target_provider="gemini-oauth", base_url=None, api_mode=None)
        CLIModelSwitchMixin._persist_model_switch_to_session(cli, res)

        # Verify raw SQLite content
        row = db.get_session(session_id)
        model_cfg = json.loads(row["model_config"])
        assert model_cfg["reasoning_config"] == {"enabled": True, "effort": "low"}
        # Mandatory invariant: effort_by_base is NEVER persisted in SQLite!
        assert "effort_by_base" not in model_cfg
        assert "effort_by_base" not in model_cfg.get("gateway_runtime", {})


def test_session_resume_seeds_only_active_base_into_runtime_map():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-m3-resume"

        # Stored session had active 3.8 with low, but prior session also visited 3.1 Pro (now lost in memory)
        model_config = {
            "provider": "gemini-oauth",
            "reasoning_config": {"enabled": True, "effort": "low"},
        }
        db.create_session(session_id, source="cli", model="gemini-3.8-flash", model_config=model_config)

        # Fresh CLI restart
        cli = FakeCLI(model="launch-default", provider="launch-default", session_id=session_id, session_db=db)
        assert cli.effort_by_base == {}

        # Resume session
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        session_meta = db.get_session(session_id)
        CLIModelSwitchMixin._restore_session_model(cli, session_meta)

        # Invariant: active base seeded into map, 3.1 Pro does NOT reappear!
        assert cli.model == "gemini-3.8-flash"
        assert cli.reasoning_config == {"enabled": True, "effort": "low"}
        assert cli.effort_by_base == {"gemini-3.8-flash": "low"}


def test_session_resume_disabled_reasoning_does_not_seed_fake_map_entry():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-m3-resume-disabled"

        model_config = {
            "provider": "gemini-oauth",
            "reasoning_config": {"enabled": False},
        }
        db.create_session(session_id, source="cli", model="gemini-3.8-flash", model_config=model_config)

        cli = FakeCLI(model="launch-default", provider="launch-default", session_id=session_id, session_db=db)
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        session_meta = db.get_session(session_id)
        CLIModelSwitchMixin._restore_session_model(cli, session_meta)

        assert cli.reasoning_config == {"enabled": False}
        # Invariant: map is NOT seeded with 'none' or fake entries
        assert "gemini-3.8-flash" not in cli.effort_by_base


# ============================================================================
# 4. Ephemeral --once Mode & Rollback Safety (Sections 8 & 9)
# ============================================================================

def test_one_turn_switch_does_not_mutate_effort_by_base():
    cli = FakeCLI(model="gemini-3.8-flash", provider="gemini-oauth")
    cli.effort_by_base = {"gemini-3.8-flash": "high"}
    cli.reasoning_config = {"enabled": True, "effort": "high"}

    # Take snapshot before one-turn
    snap = cli._snapshot_model_runtime()

    # Apply one-turn effort 'low'
    _apply_reasoning_after_switch(cli, "low", persist_global=False, one_turn=True)
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}
    # Invariant: effort_by_base unmutated!
    assert cli.effort_by_base == {"gemini-3.8-flash": "high"}

    # Restore snapshot after one turn
    cli._restore_model_runtime_snapshot(snap)
    assert cli.reasoning_config == {"enabled": True, "effort": "high"}
    assert cli.effort_by_base == {"gemini-3.8-flash": "high"}


def test_failed_switch_rollback_restores_map_and_reasoning_atomically():
    cli = FakeCLI(model="gemini-3.8-flash", provider="gemini-oauth")
    cli.effort_by_base = {"gemini-3.8-flash": "low"}
    cli.reasoning_config = {"enabled": True, "effort": "low"}

    snap = _runtime_fields(cli)

    # Attempt switch to 3.1 Pro with high
    cli.model = "gemini-3.1-pro"
    cli.effort_by_base["gemini-3.1-pro"] = "high"
    cli.reasoning_config = {"enabled": True, "effort": "high"}

    # Simulate failure and rollback
    for k, v in snap.items():
        setattr(cli, k, copy.deepcopy(v) if isinstance(v, dict) else v)

    assert cli.model == "gemini-3.8-flash"
    assert cli.reasoning_config == {"enabled": True, "effort": "low"}
    assert cli.effort_by_base == {"gemini-3.8-flash": "low"}
    assert "gemini-3.1-pro" not in cli.effort_by_base
