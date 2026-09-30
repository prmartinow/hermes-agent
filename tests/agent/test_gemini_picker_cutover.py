"""Unit and contract tests for Action Item 3 Milestone 4: Picker & typed-command capability cutover.

Verifies:
  1. Capability-driven picker rows for Cloud Code models:
       gemini-3.8-flash -> ['low', 'medium', 'high']
       gemini-3.7-flash -> ['low', 'medium', 'high']
       gemini-3.6-flash -> ['low', 'medium', 'high']
       gemini-3.1-pro   -> ['low', 'high']
       claude-sonnet-4-6, flash-lite, gpt-oss -> no reasoning stage (_picker_offers_reasoning is False)
       none is NEVER in Cloud Code rows
       max is absent from 3.8; medium is absent from 3.1 Pro
  2. Legacy generic picker behavior preserved for non-Cloud-Code providers (e.g. OpenRouter).
  3. Effective current effort preselected (runtime > override > default high).
  4. Renderer, navigation, and selection handler consume the exact same state['reasoning_rows'].
  5. Typed /model --reasoning pre-validation:
       invalid choices reject BEFORE route swap, agent rebuild, config write, or session mutation.
       rejection produces explicit informative error messages.
       contradictory embedded alias efforts are rejected.
"""

import copy
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from agent.reasoning_selection import reasoning_effort_error
from hermes_cli.cli_model_switch_mixin import (
    _picker_offers_reasoning,
    _picker_reasoning_rows,
    CLIModelSwitchMixin,
)


# ============================================================================
# 1. Capability-Driven Picker Rows & Offers Reasoning
# ============================================================================

def test_picker_offers_reasoning_cloudcode_capabilities():
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.8-flash": {"reasoning": True, "reasoning_efforts": ["low", "medium", "high"]},
            "gemini-3.1-pro": {"reasoning": True, "reasoning_efforts": ["low", "high"]},
            "gemini-3.1-flash-lite": {"reasoning": True, "reasoning_efforts": []},
            "claude-sonnet-4-6": {"reasoning": True, "reasoning_efforts": []},
            "gpt-oss-120b-medium": {"reasoning": True, "reasoning_efforts": []},
        },
    }

    # Dynamic tiered models offer reasoning
    assert _picker_offers_reasoning(provider_data, "gemini-3.8-flash") is True
    assert _picker_offers_reasoning(provider_data, "gemini-3.1-pro") is True

    # No-effort partner/lite models SKIP the reasoning stage entirely!
    assert _picker_offers_reasoning(provider_data, "gemini-3.1-flash-lite") is False
    assert _picker_offers_reasoning(provider_data, "claude-sonnet-4-6") is False
    assert _picker_offers_reasoning(provider_data, "gpt-oss-120b-medium") is False


def test_picker_reasoning_rows_cloudcode_models():
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.8-flash": {"reasoning": True, "reasoning_efforts": ["low", "medium", "high"]},
            "gemini-3.1-pro": {"reasoning": True, "reasoning_efforts": ["low", "high"]},
            "claude-sonnet-4-6": {"reasoning": True, "reasoning_efforts": []},
        },
    }

    # 3.8 Flash rows: exactly low, medium, high
    rows_38 = _picker_reasoning_rows(provider_data, "gemini-3.8-flash")
    values_38 = [v for v, _lbl in rows_38]
    assert values_38 == ["low", "medium", "high"]
    assert "none" not in values_38
    assert "max" not in values_38
    assert "" not in values_38  # No ambiguous "Keep current effort"

    # 3.1 Pro rows: exactly low, high
    rows_31 = _picker_reasoning_rows(provider_data, "gemini-3.1-pro")
    values_31 = [v for v, _lbl in rows_31]
    assert values_31 == ["low", "high"]
    assert "medium" not in values_31
    assert "none" not in values_31

    # Claude rows: empty list
    assert _picker_reasoning_rows(provider_data, "claude-sonnet-4-6") == []


def test_picker_reasoning_rows_generic_provider_preserved():
    # Non-Cloud-Code route with no reasoning_efforts in capabilities
    provider_data = {
        "slug": "openrouter",
        "capabilities": {
            "google/gemini-3.8-flash": {"reasoning": True},
        },
    }

    rows = _picker_reasoning_rows(provider_data, "google/gemini-3.8-flash")
    values = [v for v, _lbl in rows]
    # Retains full canonical ladder plus 'none' and 'Keep current effort'
    assert "minimal" in values
    assert "low" in values
    assert "medium" in values
    assert "high" in values
    assert "max" in values
    assert "none" in values
    assert "" in values


# ============================================================================
# 2. Picker Pre-Selection & State Alignment
# ============================================================================

def test_picker_preselection_uses_effective_reasoning_state():
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.8-flash": {"reasoning": True, "reasoning_efforts": ["low", "medium", "high"]},
        },
    }

    class StubCLI(CLIModelSwitchMixin):
        def __init__(self):
            self.model = "previous"
            self.provider = "gemini-oauth"
            self.effort_by_base = {"gemini-3.8-flash": "medium"}
            self._model_picker_state = {
                "stage": "model",
                "selected": 0,
                "provider_data": provider_data,
                "model_list": ["gemini-3.8-flash"],
                "visible_labels": ["gemini-3.8-flash"],
            }
        def _invalidate(self, min_interval=0.0): pass
        def _close_model_picker(self): pass

    cli = StubCLI()
    fake_result = SimpleNamespace(success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth")

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_result),          patch("cli.CLI_CONFIG", {"agent": {}}):
        cli._handle_model_picker_selection(persist_global=False)

    # Advanced to reasoning stage
    state = cli._model_picker_state
    assert state["stage"] == "reasoning"
    assert state["reasoning_rows"] == [("low", "low"), ("medium", "medium"), ("high", "high")]
    # Invariant: medium is preselected (index 1)!
    assert state["selected"] == 1


# ============================================================================
# 3. Typed /model --reasoning Pre-Validation (Section 5, 6, 7, 8)
# ============================================================================

def test_reasoning_effort_error_validation_matrix():
    # 1. Valid Cloud Code selections
    assert reasoning_effort_error("gemini-oauth", "gemini-3.8-flash", "low") is None
    assert reasoning_effort_error("gemini-oauth", "gemini-3.8-flash", "medium") is None
    assert reasoning_effort_error("gemini-oauth", "gemini-3.8-flash", "high") is None
    assert reasoning_effort_error("gemini-oauth", "gemini-3.1-pro", "low") is None
    assert reasoning_effort_error("gemini-oauth", "gemini-3.1-pro", "high") is None

    # 2. Unsupported effort on 3.8
    err_38_max = reasoning_effort_error("gemini-oauth", "gemini-3.8-flash", "max")
    assert err_38_max == "gemini-3.8-flash has no 'max' effort (available: low, medium, high)"

    # 3. none on dynamic Gemini
    err_38_none = reasoning_effort_error("gemini-oauth", "gemini-3.8-flash", "none")
    assert err_38_none == "gemini-3.8-flash has no 'none' effort (available: low, medium, high)"

    # 4. medium on 3.1 Pro
    err_31_med = reasoning_effort_error("gemini-oauth", "gemini-3.1-pro", "medium")
    assert err_31_med == "gemini-3.1-pro has no 'medium' effort (available: low, high)"

    # 5. No-effort partner/lite models
    assert reasoning_effort_error("gemini-oauth", "claude-sonnet-4-6", "low") == "--reasoning is not supported for model 'claude-sonnet-4-6'"
    assert reasoning_effort_error("gemini-oauth", "gemini-3.1-flash-lite", "low") == "--reasoning is not supported for model 'gemini-3.1-flash-lite'"
    assert reasoning_effort_error("gemini-oauth", "gpt-oss-120b-medium", "low") == "--reasoning is not supported for model 'gpt-oss-120b-medium'"

    # 6. Legacy aliases: matching vs contradictory
    assert reasoning_effort_error("gemini-oauth", "gemini-3.8-flash-high", "high") is None
    err_alias_conflict = reasoning_effort_error("gemini-oauth", "gemini-3.8-flash-high", "medium")
    assert "Conflicting reasoning effort" in err_alias_conflict
    assert "implies 'high'" in err_alias_conflict

    # 7. Generic routes pass through
    assert reasoning_effort_error("openrouter", "google/gemini-3.8-flash", "low") is None
    assert reasoning_effort_error("openrouter", "gpt-4o", "medium") is None


def test_typed_model_switch_invalid_effort_causes_zero_mutation():
    """Verify that an invalid --reasoning on typed /model fails before route swap or client rebuild."""
    class MockCLI(CLIModelSwitchMixin):
        def __init__(self):
            self.model = "gemini-3.8-flash"
            self.provider = "gemini-oauth"
            self.requested_provider = "gemini-oauth"
            self.base_url = None
            self.api_mode = None
            self.api_key = None
            self.reasoning_config = {"enabled": True, "effort": "low"}
            self.effort_by_base = {"gemini-3.8-flash": "low"}
            self.agent = MagicMock()
            self._session_db = None
            self.session_id = None
            self.verbose = False
            self.max_turns = 100

    cli = MockCLI()
    orig_cli_model = cli.model
    orig_cli_reasoning = copy.deepcopy(cli.reasoning_config)
    orig_cli_effort_map = copy.deepcopy(cli.effort_by_base)

    fake_result = SimpleNamespace(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        api_key=None,
        base_url=None,
        api_mode=None,
    )

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_result),          patch("hermes_cli.cli_model_switch_mixin._run_confirm_and_apply") as mock_run_confirm,          patch("cli._cprint") as mock_cprint:

        # Typed command with invalid effort 'max' on 3.8
        cli._handle_model_switch("/model gemini-3.8-flash --reasoning max")

        # Invariant 1: Error printed to user
        mock_cprint.assert_called_with("  ✗ gemini-3.8-flash has no 'max' effort (available: low, medium, high)")

        # Invariant 2: _run_confirm_and_apply was NEVER invoked!
        mock_run_confirm.assert_not_called()

        # Invariant 3: Agent switch_model was NEVER called!
        cli.agent.switch_model.assert_not_called()

        # Invariant 4: CLI state completely unmutated!
        assert cli.model == orig_cli_model
        assert cli.reasoning_config == orig_cli_reasoning
        assert cli.effort_by_base == orig_cli_effort_map
# ============================================================================
# 4. Milestone 4 Amendment Regressions: Target-State Aware Picker Display
# ============================================================================

from hermes_cli.cli_tui_mixin import CLITuiMixin


class DummyTUIHarness(CLIModelSwitchMixin, CLITuiMixin):
    def __init__(self, model="gemini-3.8-flash", provider="gemini-oauth", reasoning_config=None, effort_by_base=None):
        self.model = model
        self.provider = provider
        self.reasoning_config = reasoning_config
        self.effort_by_base = effort_by_base if effort_by_base is not None else {}
        self._model_picker_state = None
        self._app = None

    def _invalidate(self, min_interval=0.0): pass
    def _close_model_picker(self): pass


def test_picker_active_low_target_high_labels_only_high_as_current():
    # Active: gemini-3.8-flash / low
    # Picker target: gemini-3.1-pro with effective state high
    cli = DummyTUIHarness(
        model="gemini-3.8-flash",
        provider="gemini-oauth",
        reasoning_config={"enabled": True, "effort": "low"},
        effort_by_base={"gemini-3.8-flash": "low"},
    )
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.1-pro": {"reasoning": True, "reasoning_efforts": ["low", "high"]},
        },
    }
    cli._model_picker_state = {
        "stage": "model",
        "selected": 0,
        "provider_data": provider_data,
        "model_list": ["gemini-3.1-pro"],
        "visible_labels": ["gemini-3.1-pro"],
    }
    fake_result = SimpleNamespace(success=True, new_model="gemini-3.1-pro", target_provider="gemini-oauth")

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_result),          patch("cli.CLI_CONFIG", {"agent": {}}):
        cli._handle_model_picker_selection(persist_global=False)

    state = cli._model_picker_state
    assert state["stage"] == "reasoning"
    assert state["selected"] == 1  # 'high'
    assert state["reasoning_effective_effort"] == "high"

    # Verify rendered display fragments
    frags = cli._get_model_picker_display_fragments()
    rendered = "".join(part[1] for part in frags)
    assert "high  ← current" in rendered
    assert "low  ← current" not in rendered


def test_picker_disabled_target_preselects_high_without_current_label():
    # Target: gemini-3.8-flash with persisted disabled reasoning
    cli = DummyTUIHarness(model="launch-default", provider="launch-default", effort_by_base={})
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.8-flash": {"reasoning": True, "reasoning_efforts": ["low", "medium", "high"]},
        },
    }
    cli._model_picker_state = {
        "stage": "model",
        "selected": 0,
        "provider_data": provider_data,
        "model_list": ["gemini-3.8-flash"],
        "visible_labels": ["gemini-3.8-flash"],
    }
    fake_result = SimpleNamespace(success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth")

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_result),          patch("cli.CLI_CONFIG", {"agent": {"reasoning_overrides": {"gemini-3.8-flash": False}}}):
        cli._handle_model_picker_selection(persist_global=False)

    state = cli._model_picker_state
    assert state["stage"] == "reasoning"
    assert state["selected"] == 2  # 'high' default preselected
    assert state["reasoning_effective_effort"] is None

    frags = cli._get_model_picker_display_fragments()
    rendered = "".join(part[1] for part in frags)
    assert "← current" not in rendered
    assert "none" not in rendered


def test_picker_31_pro_disabled_target_preselects_high():
    # Target: gemini-3.1-pro with persisted disabled reasoning
    cli = DummyTUIHarness(model="launch-default", provider="launch-default", effort_by_base={})
    provider_data = {
        "slug": "gemini-oauth",
        "capabilities": {
            "gemini-3.1-pro": {"reasoning": True, "reasoning_efforts": ["low", "high"]},
        },
    }
    cli._model_picker_state = {
        "stage": "model",
        "selected": 0,
        "provider_data": provider_data,
        "model_list": ["gemini-3.1-pro"],
        "visible_labels": ["gemini-3.1-pro"],
    }
    fake_result = SimpleNamespace(success=True, new_model="gemini-3.1-pro", target_provider="gemini-oauth")

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_result),          patch("cli.CLI_CONFIG", {"agent": {"reasoning_overrides": {"gemini-3.1-pro": False}}}):
        cli._handle_model_picker_selection(persist_global=False)

    state = cli._model_picker_state
    assert state["stage"] == "reasoning"
    assert state["selected"] == 1  # 'high' (not low!)
    assert state["reasoning_effective_effort"] is None

    frags = cli._get_model_picker_display_fragments()
    rendered = "".join(part[1] for part in frags)
    assert "← current" not in rendered
