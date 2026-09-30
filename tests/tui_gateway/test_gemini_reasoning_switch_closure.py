"""Tests for Gemini reasoning switch closure on Gateway/TUI surface (Action Item 3, Milestone 5)."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tui_gateway.server as server
from tui_gateway.model_switch import (
    _snapshot_agent_model_runtime,
    _restore_agent_model_runtime,
    _apply_switch_reasoning,
)


def _make_gateway_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="high"):
    agent = MagicMock()
    agent.model = model
    agent.provider = provider
    agent.requested_provider = provider
    agent.base_url = ""
    agent.api_mode = ""
    agent.api_key = ""
    agent.reasoning_config = {"enabled": True, "effort": effort} if effort else None
    agent.effort_by_base = {model: effort} if effort else {}
    agent._primary_runtime = {
        "model": model,
        "provider": provider,
        "reasoning_config": {"enabled": True, "effort": effort} if effort else None,
    }
    return agent


@pytest.mark.parametrize("cmd,expected_err", [
    ("/model gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
    ("/model gemini-3.8-flash --reasoning none", "gemini-3.8-flash has no 'none' effort"),
    ("/model gemini-3.1-pro --reasoning medium", "gemini-3.1-pro has no 'medium' effort"),
    ("/model claude-sonnet-4-6 --reasoning low", "is not supported for model 'claude-sonnet-4-6'"),
    ("/model gemini-oauth:gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
    ("/model gemini-2:gemini-3.1-pro --reasoning medium", "gemini-3.1-pro has no 'medium' effort"),
    ("/model gemini-3.8-flash-high --reasoning medium", "Conflicting reasoning effort: alias 'gemini-3.8-flash-high' implies 'high'"),
])
def test_gateway_typed_validation_before_provider_resolution(cmd, expected_err):
    """Pin: invalid typed efforts fail before switch_model(), network, credential or agent mutation."""
    agent = _make_gateway_agent()
    session = {"agent": agent}

    with patch("hermes_cli.model_switch.switch_model") as mock_switch_model:
        with pytest.raises(ValueError) as excinfo:
            server._apply_model_switch("s1", session, cmd)

        assert expected_err in str(excinfo.value)
        # Invariant: switch_model is never called for invalid efforts!
        mock_switch_model.assert_not_called()


def test_gateway_switch_updates_effort_by_base_and_reasoning_config():
    """Pin: /model 3.8 --reasoning low updates active reasoning_config and effort_by_base."""
    agent = _make_gateway_agent(model="gemini-3.1-pro", effort="high")
    agent.effort_by_base = {"gemini-3.1-pro": "high"}
    session = {"agent": agent}

    fake_result = SimpleNamespace(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        base_url="",
        api_key="",
        api_mode="",
        model_info={},
        warning_message=None,
    )

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_result),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s1", session, "/model gemini-3.8-flash --reasoning low")

        # Invariant: agent.reasoning_config and effort_by_base updated!
        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        assert agent.effort_by_base.get("gemini-3.8-flash") == "low"
        # Invariant: primary runtime also updated so fallback returns to switch reasoning!
        assert agent._primary_runtime["reasoning_config"] == {"enabled": True, "effort": "low"}


def test_gateway_switch_global_persists_canonical_reasoning_override():
    """Pin: /model 3.8 --reasoning low --global writes agent.reasoning_overrides[canonical_base]."""
    agent = _make_gateway_agent(model="gemini-3.8-flash", effort="high")
    session = {"agent": agent}

    fake_result = SimpleNamespace(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        base_url="",
        api_key="",
        api_mode="",
        model_info={},
        warning_message=None,
    )

    written_keys = {}
    def mock_write(k, v):
        written_keys[k] = v

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_result),          patch("hermes_cli.model_switch.persist_model_selection"),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None),          patch("hermes_cli.config.load_config", return_value={"agent": {"reasoning_effort": "high"}}),          patch.object(server, "_write_config_key", side_effect=mock_write):

        server._apply_model_switch("s1", session, "/model gemini-3.8-flash --reasoning low --global")

        # Invariant: writes canonical reasoning_overrides, does NOT overwrite flat reasoning_effort!
        assert "agent.reasoning_overrides" in written_keys
        assert written_keys["agent.reasoning_overrides"]["gemini-3.8-flash"] == "low"
        assert "agent.reasoning_effort" not in written_keys


def test_gateway_once_snapshot_and_restore_cycle():
    """Pin: --once snapshot deepcopies effort_by_base, and restore completely reverts the one-turn choice."""
    agent = _make_gateway_agent(model="gemini-3.8-flash", effort="high")
    agent.effort_by_base = {"gemini-3.8-flash": "high"}

    # 1. Take snapshot
    snapshot = _snapshot_agent_model_runtime(agent)
    assert snapshot["effort_by_base"] == {"gemini-3.8-flash": "high"}

    # 2. One-turn switch applied
    _apply_switch_reasoning("s1", {}, agent, "low", persist_global=False, one_turn=True,
                            target_model="gemini-3.8-flash", target_provider="gemini-oauth")
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    # Invariant: effort_by_base was NOT updated during --once switch!
    assert agent.effort_by_base == {"gemini-3.8-flash": "high"}

    # Simulate rogue mutation during turn
    agent.effort_by_base["gemini-3.8-flash"] = "low"

    # 3. Restore after turn
    _restore_agent_model_runtime(agent, snapshot)
    # Invariant: restored map completely replaces mutated map!
    assert agent.effort_by_base == {"gemini-3.8-flash": "high"}
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_gateway_sequential_memory_preservation():
    """Pin:
    3.8 low -> 3.1 high -> 3.8 = low.
    3.8 medium -> Claude -> 3.8 = medium.
    """
    agent = _make_gateway_agent(model="gemini-3.8-flash", effort="low")
    agent.effort_by_base = {"gemini-3.8-flash": "low"}

    # Switch to 3.1 high
    _apply_switch_reasoning("s1", {}, agent, "high", persist_global=False, one_turn=False,
                            target_model="gemini-3.1-pro", target_provider="gemini-oauth")
    assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}

    # Switch back to 3.8 without --reasoning -> re-resolves from effort_by_base!
    from agent.reasoning_selection import resolve_effective_reasoning_config
    eff_cfg = resolve_effective_reasoning_config(
        config={}, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base=agent.effort_by_base
    )
    assert eff_cfg == {"enabled": True, "effort": "low"}

    # Sequence 2: 3.8 medium -> Claude -> 3.8 = medium
    agent.effort_by_base = {"gemini-3.8-flash": "medium"}
    # Claude has no effort
    claude_cfg = resolve_effective_reasoning_config(
        config={}, provider="gemini-oauth", model="claude-sonnet-4-6", effort_by_base=agent.effort_by_base
    )
    assert claude_cfg is None
    # Back to 3.8 -> still medium!
    eff_cfg_38 = resolve_effective_reasoning_config(
        config={}, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base=agent.effort_by_base
    )
    assert eff_cfg_38 == {"enabled": True, "effort": "medium"}
def test_gateway_configured_alias_post_resolution_validation_backstop():
    """Pin: configured alias resolving to Cloud Code with invalid effort
    is rejected after switch_model resolution and before commit.
    Alias 'mygem' -> gemini-oauth / gemini-3.8-flash
    /model mygem --reasoning max
    """
    agent = _make_gateway_agent(model="gpt-4o", provider="openrouter", effort=None)
    agent.effort_by_base = {}
    session = {"agent": agent}

    fake_result = SimpleNamespace(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        base_url="",
        api_key="",
        api_mode="",
        model_info={},
        warning_message=None,
    )

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_result),          patch.object(server, "_commit_agent_switch") as mock_commit:

        with pytest.raises(ValueError) as excinfo:
            server._apply_model_switch("s1", session, "/model mygem --reasoning max")

        assert "gemini-3.8-flash has no 'max' effort" in str(excinfo.value)
        # Invariant: commit was NOT called!
        mock_commit.assert_not_called()
        assert agent.model == "gpt-4o"
        assert agent.effort_by_base == {}
        assert "model_override" not in session
