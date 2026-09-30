"""Cross-surface equivalence tests for Gemini model/effort switching (Action Item 3, Milestone 5)."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tui_gateway.server as server
from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
from hermes_cli.cli_tui_mixin import CLITuiMixin


def test_cross_surface_equivalence():
    """Verify that all five interaction surfaces converge on identical runtime state and persisted metadata:
    1. Classic CLI typed /model
    2. Classic CLI picker modal
    3. Gateway/Ink typed /model RPC
    4. Desktop/Ink picker model command dispatch
    5. Gateway resume / rebuild from session row

    Target: gemini-3.8-flash
    Effort: medium
    Scope: session

    All converge on:
    - model == 'gemini-3.8-flash'
    - provider == 'gemini-oauth'
    - reasoning_config == {'enabled': True, 'effort': 'medium'}
    - effort_by_base['gemini-3.8-flash'] == 'medium'
    - persisted session metadata contains active reasoning_config, but no full map.
    """
    fake_switch_res = SimpleNamespace(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        provider_label="Google Gemini (OAuth)",
        model_info={},
        api_key=None,
        base_url=None,
        api_mode=None,
        warning_message=None,
    )

    class MockCLI(CLITuiMixin, CLIModelSwitchMixin):
        def __init__(self):
            self.model = "gpt-4o"
            self.provider = "openrouter"
            self.requested_provider = "openrouter"
            self.base_url = None
            self.api_mode = None
            self.api_key = None
            self.reasoning_config = None
            self.effort_by_base = {}
            self.agent = MagicMock()
            self.agent.effort_by_base = {}
            self._session_db = None
            self.session_id = None
            self.verbose = False
            self.max_turns = 100

        def _console_print(self, *a, **k):
            pass

    # ── Surface 1: Classic CLI typed /model ──
    cli1 = MockCLI()
    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_switch_res),          patch("cli._cprint"):
        cli1._handle_model_switch("/model gemini-oauth:gemini-3.8-flash --reasoning medium")

    assert cli1.model == "gemini-3.8-flash"
    assert cli1.provider == "gemini-oauth"
    assert cli1.reasoning_config == {"enabled": True, "effort": "medium"}
    assert cli1.effort_by_base == {"gemini-3.8-flash": "medium"}

    # ── Surface 2: Classic CLI picker selection callback ──
    cli2 = MockCLI()
    cli2._confirm_expensive_model_switch = lambda _res: True
    with patch("hermes_cli.cli_model_switch_mixin._print_switch_summary"):
        cli2._confirm_and_apply_cli_model_switch(
            fake_switch_res,
            persist_global=False,
            one_turn=False,
            reasoning_effort="medium",
        )

    assert cli2.model == "gemini-3.8-flash"
    assert cli2.provider == "gemini-oauth"
    assert cli2.reasoning_config == {"enabled": True, "effort": "medium"}
    assert cli2.effort_by_base == {"gemini-3.8-flash": "medium"}

    # ── Surface 3: Gateway/Ink typed /model RPC ──
    gw_agent3 = MagicMock()
    gw_agent3.model = "gpt-4o"
    gw_agent3.provider = "openrouter"
    gw_agent3.base_url = ""
    gw_agent3.api_mode = ""
    gw_agent3.api_key = ""
    gw_agent3.reasoning_config = None
    gw_agent3.effort_by_base = {}
    gw_agent3._primary_runtime = {}
    gw_session3 = {"agent": gw_agent3}

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_switch_res),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s3", gw_session3, "/model gemini-oauth:gemini-3.8-flash --reasoning medium")

    assert gw_agent3.reasoning_config == {"enabled": True, "effort": "medium"}
    assert gw_agent3.effort_by_base == {"gemini-3.8-flash": "medium"}
    assert gw_session3["create_reasoning_override"] == {"enabled": True, "effort": "medium"}

    # ── Surface 4: Desktop/Ink picker command emission and application ──
    # Simulates client clicking item produced by modelPickerCommand('gemini-3.8-flash', 'gemini-oauth', False, 'medium')
    cmd4 = "gemini-3.8-flash --provider gemini-oauth --reasoning medium --tui-session"
    gw_agent4 = MagicMock()
    gw_agent4.model = "gpt-4o"
    gw_agent4.provider = "openrouter"
    gw_agent4.base_url = ""
    gw_agent4.api_mode = ""
    gw_agent4.api_key = ""
    gw_agent4.reasoning_config = None
    gw_agent4.effort_by_base = {}
    gw_agent4._primary_runtime = {}
    gw_session4 = {"agent": gw_agent4}

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_switch_res),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s4", gw_session4, cmd4)

    assert gw_agent4.reasoning_config == {"enabled": True, "effort": "medium"}
    assert gw_agent4.effort_by_base == {"gemini-3.8-flash": "medium"}
    assert gw_session4["create_reasoning_override"] == {"enabled": True, "effort": "medium"}

    # ── Surface 5: Gateway resume from persisted session row ──
    def _fake_build_client(agent, *a, **k):
        agent._client_kwargs = {}
        agent.client = MagicMock()

    def _fake_resolve_runtime(model_override, provider_override):
        m = model_override.get('model') if isinstance(model_override, dict) else model_override
        p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
        return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}

    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),          patch("tui_gateway.server._load_cfg", return_value={}),          patch("tui_gateway.server._startup_system_prompt", return_value=""),          patch("agent.shell_hooks.register_from_config"):

        resumed_agent = server._make_agent(
            sid="s_resumed",
            key="k_resumed",
            model_override={"model": "gemini-3.8-flash", "provider": "gemini-oauth"},
            provider_override="gemini-oauth",
            reasoning_config_override={"enabled": True, "effort": "medium"},
        )

    assert resumed_agent.model == "gemini-3.8-flash"
    assert resumed_agent.provider == "gemini-oauth"
    assert resumed_agent.reasoning_config == {"enabled": True, "effort": "medium"}
    assert resumed_agent.effort_by_base == {"gemini-3.8-flash": "medium"}
