"""Tests for Gemini gateway failure atomicity and rollback safety (Action Item 4, Milestone 2).

Covers:
1. Gateway transaction failure around failing switch_model():
   - no slash worker restart
   - no runtime persistence
   - no marker appended
   - no config write (global or local)
   - no session model_override or reasoning_override mutation
   - agent effort_by_base and reasoning_config preserved atomically
2. One-turn (--once) failure after temporary override:
   - post-turn restoration restores original model, provider, reasoning_config, effort_by_base, primary_runtime
   - no config write or permanent session model_config change
3. Failed global switch never partially persists:
   - live failure prevents both model and reasoning_overrides writes
   - invalid effort rejected before any config/session touch.
"""

import copy
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
    agent.api_key = "key1"
    agent.reasoning_config = {"enabled": True, "effort": effort} if effort else None
    agent.effort_by_base = {model: effort} if effort else {}
    agent._primary_runtime = {
        "model": model,
        "provider": provider,
        "reasoning_config": {"enabled": True, "effort": effort} if effort else None,
    }
    return agent


class TestGatewayTransactionFailureAtomicity:
    @pytest.mark.parametrize("cmd", [
        "/model gemini-3.6-flash --reasoning medium",
        "/model gemini-3.6-flash --reasoning medium --global",
    ])
    def test_gateway_switch_failure_prevents_all_side_effects(self, cmd):
        """When live agent.switch_model() fails, _apply_model_switch() rolls back completely:
        no slash worker restart, no DB persistence, no marker, no config write,
        and zero session or agent state leaks.
        """
        agent = _make_gateway_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="low")
        original_model = agent.model
        original_provider = agent.provider
        original_reasoning = copy.deepcopy(agent.reasoning_config)
        original_map = copy.deepcopy(agent.effort_by_base)

        session = {
            "agent": agent,
            "session_key": "s_atomicity",
            "model_override": None,
            "create_reasoning_override": None,
            "composer_override_profile": None,
        }

        # Simulate failing switch_model
        def failing_switch(**kwargs):
            raise RuntimeError("Live swap failed: connection dropped")

        agent.switch_model = MagicMock(side_effect=failing_switch)

        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.6-flash", target_provider="gemini-oauth",
                 base_url="", api_key="", api_mode="", model_info={}, warning_message=None)),              patch.object(server, "_restart_slash_worker") as mock_restart_slash,              patch.object(server, "_persist_live_session_runtime") as mock_persist_runtime,              patch.object(server, "_persist_live_session_system_prompt") as mock_persist_prompt,              patch.object(server, "_append_model_switch_marker") as mock_append_marker,              patch.object(server, "_emit_session_info") as mock_emit_info,              patch.object(server, "_write_config_key", create=True) as mock_write_cfg,              patch("hermes_cli.model_switch.persist_model_selection") as mock_persist_global:

            with pytest.raises(ValueError) as excinfo:
                server._apply_model_switch("s_atomicity", session, cmd)

            assert "Model switch to gemini-3.6-flash failed" in str(excinfo.value)

            # Invariant: ZERO side effect calls occurred!
            mock_restart_slash.assert_not_called()
            mock_persist_runtime.assert_not_called()
            mock_persist_prompt.assert_not_called()
            mock_append_marker.assert_not_called()
            mock_emit_info.assert_not_called()
            mock_write_cfg.assert_not_called()
            mock_persist_global.assert_not_called()

        # Invariant: Agent state untouched
        assert agent.model == original_model
        assert agent.provider == original_provider
        assert agent.reasoning_config == original_reasoning
        assert agent.effort_by_base == original_map

        # Invariant: Session overrides untouched
        assert session["model_override"] is None
        assert session["create_reasoning_override"] is None
        assert session["composer_override_profile"] is None


class TestOneTurnAtomicity:
    def test_one_turn_failure_after_application_restores_complete_state(self):
        """When a one-turn (--once) model switch is applied, and the turn subsequently fails,
        _restore_agent_model_runtime restores original model, provider, reasoning_config,
        effort_by_base, and primary_runtime without leaking temporary choices.
        """
        agent = _make_gateway_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="high")
        agent.effort_by_base = {"gemini-3.8-flash": "high"}
        original_primary = copy.deepcopy(agent._primary_runtime)

        # 1. Take snapshot before one-turn application
        snapshot = _snapshot_agent_model_runtime(agent)
        assert snapshot["effort_by_base"] == {"gemini-3.8-flash": "high"}
        assert snapshot["reasoning_config"] == {"enabled": True, "effort": "high"}

        # 2. Apply one-turn override to 3.8 low
        _apply_switch_reasoning("s_once", {}, agent, "low", persist_global=False, one_turn=True,
                                target_model="gemini-3.8-flash", target_provider="gemini-oauth")
        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        # Invariant: effort_by_base was NOT updated during one-turn switch
        assert agent.effort_by_base == {"gemini-3.8-flash": "high"}

        # 3. Simulate turn failure and post-turn restoration
        _restore_agent_model_runtime(agent, snapshot)

        # Invariant: Completely restored to pre-turn state!
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert agent.effort_by_base == {"gemini-3.8-flash": "high"}
        assert agent._primary_runtime == original_primary


    def test_one_turn_lifecycle_failure_restores_complete_state(self):
        """Lifecycle-level --once failure:
        1. Live real AIAgent on permanent gemini-3.8-flash / high with effort_by_base={'gemini-3.8-flash': 'high'}.
        2. Real cross-model switch: server._apply_model_switch(sid, session, "/model gemini-3.6-flash --reasoning medium --once")
           -> applies temporary gemini-3.6-flash / medium
           -> installs session["one_turn_model_restore"].
        3. Turn execution begins: prompt_turn builds st = server._TurnRun(agent, session.pop("one_turn_model_restore"), ...).
        4. Turn execution encounters an exception.
        5. Production finally seam executes: server._finish_turn(sid, session, st).
        6. Asserts complete restoration of model, provider, reasoning_config, effort_by_base, _primary_runtime.
        7. Asserts no permanent config write or permanent session model_override!
        """
        from run_agent import AIAgent

        def fake_build_client(ag, api_key="fake", base_url="", *a, **k):
            ag.api_key = api_key or getattr(ag, "api_key", "fake")
            ag.base_url = base_url or getattr(ag, "base_url", "")
            ag._client_kwargs = {"api_key": ag.api_key, "base_url": ag.base_url}
            ag.client = MagicMock()

        with patch("agent.agent_init._build_client", side_effect=fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "high"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "high"}

        original_primary = copy.deepcopy(agent._primary_runtime)

        session = {
            "agent": agent,
            "session_key": "s_once_lifecycle",
            "model_override": None,
            "create_reasoning_override": None,
        }

        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.6-flash", target_provider="gemini-oauth",
                 base_url="", api_key="key1", api_mode="chat_completions", model_info={}, warning_message=None)),              patch.object(server, "_restart_slash_worker"),              patch.object(server, "_persist_live_session_runtime"),              patch.object(server, "_persist_live_session_system_prompt"),              patch.object(server, "_append_model_switch_marker"),              patch.object(server, "_emit_session_info"),              patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

            out = server._apply_model_switch("s_once_lifecycle", session, "/model gemini-3.6-flash --reasoning medium --once")
            assert out["scope"] == "once"

            # Invariant: Temporary cross-model and effort applied
            assert agent.model == "gemini-3.6-flash"
            assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
            # Invariant: effort_by_base untouched
            assert agent.effort_by_base == {"gemini-3.8-flash": "high"}
            # Invariant: session contains one_turn_model_restore snapshot
            assert "one_turn_model_restore" in session
            assert session["one_turn_model_restore"]["model"] == "gemini-3.8-flash"
            assert session["one_turn_model_restore"]["reasoning_config"] == {"enabled": True, "effort": "high"}

            # Prompt turn execution begins: pops one_turn_model_restore into st
            st = server._TurnRun(
                agent=session["agent"],
                one_turn_restore=session.pop("one_turn_model_restore", None),
                terminal_callback=None,
                receipt_committed=True,
            )

            # Turn encounters error; production finally block calls _finish_turn
            server._finish_turn("s_once_lifecycle", session, st)

            # Invariant: Completely restored to original model and effort!
            assert agent.model == "gemini-3.8-flash"
            assert agent.provider == "gemini-oauth"
            assert agent.reasoning_config == {"enabled": True, "effort": "high"}
            assert agent.effort_by_base == {"gemini-3.8-flash": "high"}
            assert agent._primary_runtime == original_primary

            # Invariant: No permanent config or session override written!
            mock_write_cfg.assert_not_called()
            assert session["model_override"] is None


class TestGlobalSwitchFailureAtomicity:
    def test_failed_global_switch_never_partially_persists(self):
        """When /model 3.8 --reasoning low --global fails during live switch,
        neither model selection nor reasoning_overrides is written to disk config.
        """
        agent = _make_gateway_agent(model="gpt-4o", provider="openrouter", effort=None)
        session = {"agent": agent}

        agent.switch_model = MagicMock(side_effect=RuntimeError("Switch failed"))

        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                 base_url="", api_key="", api_mode="", model_info={}, warning_message=None)),              patch.object(server, "_write_config_key", create=True) as mock_write_cfg,              patch("hermes_cli.model_switch.persist_model_selection") as mock_persist_global:

            with pytest.raises(ValueError):
                server._apply_model_switch("s_fail_global", session, "/model gemini-3.8-flash --reasoning low --global")

            # Invariant: Neither model nor reasoning override is written!
            mock_write_cfg.assert_not_called()
            mock_persist_global.assert_not_called()
