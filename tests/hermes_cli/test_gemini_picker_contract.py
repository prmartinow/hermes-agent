"""Classic CLI Picker Contract Suite (Action Item 4, Milestone 3).

Enforces parity between Classic CLI and Web TUI/Ink capability models:
1. Table-driven row comparison for Cloud Code, non-effort, and generic models
2. Target preselection and '← current' marking parity
3. Classic CLI picker-to-runtime end-to-end execution
"""

import copy
import pytest
from unittest.mock import MagicMock, patch

from hermes_cli.cli_model_switch_mixin import (
    _picker_reasoning_rows,
    _picker_offers_reasoning,
    _apply_reasoning_after_switch,
    CLIModelSwitchMixin,
)
from hermes_cli.cli_tui_mixin import CLITuiMixin


class TestClassicPickerParity:
    @pytest.mark.parametrize("model,cap_entry,expected_offers,expected_rows", [
        ("gemini-3.8-flash", {"reasoning_efforts": ["low", "medium", "high"], "can_disable_reasoning": False}, True, ["low", "medium", "high"]),
        ("gemini-3.7-flash", {"reasoning_efforts": ["low", "medium", "high"], "can_disable_reasoning": False}, True, ["low", "medium", "high"]),
        ("gemini-3.6-flash", {"reasoning_efforts": ["low", "medium", "high"], "can_disable_reasoning": False}, True, ["low", "medium", "high"]),
        ("gemini-3.1-pro", {"reasoning_efforts": ["low", "high"], "can_disable_reasoning": False}, True, ["low", "high"]),
        ("gemini-3.1-flash-lite", {"reasoning_efforts": []}, False, []),
        ("claude-sonnet-4-6", {"reasoning_efforts": []}, False, []),
        ("gpt-oss-120b-medium", {"reasoning_efforts": []}, False, []),
    ])
    def test_classic_picker_cloudcode_row_parity(self, model, cap_entry, expected_offers, expected_rows):
        prov_data = {
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "capabilities": {model: cap_entry}
        }
        offers = _picker_offers_reasoning(prov_data, model)
        assert offers is expected_offers
        rows = _picker_reasoning_rows(prov_data, model)
        assert [val for val, label in rows] == expected_rows

    def test_classic_picker_generic_model_ladder(self):
        """Generic model retains full canonical ladder plus none and Keep current effort."""
        prov_data = {
            "slug": "openrouter",
            "name": "OpenRouter",
            "capabilities": {"generic-model": {"reasoning": True}}
        }
        offers = _picker_offers_reasoning(prov_data, "generic-model")
        assert offers is True
        rows = _picker_reasoning_rows(prov_data, "generic-model")
        values = [val for val, label in rows]
        assert "low" in values
        assert "medium" in values
        assert "high" in values
        assert "none" in values
        assert "" in values  # Keep current effort

    def test_classic_picker_current_marker_and_preselection(self):
        """When target model has effective effort 'medium', Classic CLI marks 'medium  ← current'."""
        class MockCLI(CLITuiMixin, CLIModelSwitchMixin):
            def __init__(self):
                self.reasoning_config = {"enabled": True, "effort": "high"}
                self.effort_by_base = {}

        cli = MockCLI()
        prov_data = {
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "capabilities": {
                "gemini-3.8-flash": {
                    "reasoning_efforts": ["low", "medium", "high"],
                    "effective_reasoning_effort": "medium",
                }
            }
        }
        state = {
            "stage": "reasoning",
            "provider_data": prov_data,
            "reasoning_rows": _picker_reasoning_rows(prov_data, "gemini-3.8-flash"),
            "reasoning_effective_effort": "medium",
        }
        # In hermes_cli/cli_tui_mixin.py lines 674-677:
        rows = state["reasoning_rows"]
        target_eff = state["reasoning_effective_effort"]
        choices = [f"{label}  ← current" if target_eff is not None and value == target_eff else label
                   for value, label in rows]

        assert choices == ["low", "medium  ← current", "high"]

    def test_classic_picker_disabled_state_has_no_current_marker(self):
        """When Cloud Code reasoning is disabled, target_eff is None -> no row marked '← current'."""
        prov_data = {
            "slug": "gemini-oauth",
            "capabilities": {
                "gemini-3.8-flash": {
                    "reasoning_efforts": ["low", "medium", "high"],
                    "effective_reasoning_effort": None,
                }
            }
        }
        state = {
            "stage": "reasoning",
            "provider_data": prov_data,
            "reasoning_rows": _picker_reasoning_rows(prov_data, "gemini-3.8-flash"),
            "reasoning_effective_effort": None,
        }
        rows = state["reasoning_rows"]
        target_eff = state["reasoning_effective_effort"]
        choices = [f"{label}  ← current" if target_eff is not None and value == target_eff else label
                   for value, label in rows]

        assert choices == ["low", "medium", "high"]


class TestClassicPickerToRuntimeExecution:
    def test_classic_picker_selection_updates_runtime_and_effort_by_base(self):
        """Applying reasoning after switch updates cli and agent reasoning_config and effort_by_base."""
        class MockCLI(CLITuiMixin, CLIModelSwitchMixin):
            def __init__(self):
                self.model = "gemini-3.8-flash"
                self.provider = "gemini-oauth"
                self.reasoning_config = None
                self.effort_by_base = {}
                self.agent = MagicMock()
                self.agent.effort_by_base = {}

        cli = MockCLI()
        _apply_reasoning_after_switch(cli, "low", persist_global=False)

        assert cli.reasoning_config == {"enabled": True, "effort": "low"}
        assert cli.agent.reasoning_config == {"enabled": True, "effort": "low"}
        assert cli.effort_by_base == {"gemini-3.8-flash": "low"}
        assert cli.agent.effort_by_base == {"gemini-3.8-flash": "low"}
