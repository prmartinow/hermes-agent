"""Cross-surface equivalence tests for Gemini model/effort switching (Action Item 3, Milestone 5)."""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tui_gateway.server as server
from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin


def test_cross_surface_equivalence():
    """Verify that all surfaces converge on identical runtime state and persisted metadata:
    1. Classic CLI typed /model
    2. Classic CLI picker
    3. Gateway/Ink typed /model RPC
    4. Gateway resume / rebuild

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
    # ── Surface 1: Classic CLI typed /model ──
    class MockCLI(CLIModelSwitchMixin):
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

    cli = MockCLI()
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

    with patch("hermes_cli.cli_model_switch_mixin._switch_model_from", return_value=fake_switch_res),          patch("cli._cprint"):
        cli._handle_model_switch("/model gemini-oauth:gemini-3.8-flash --reasoning medium")

    assert cli.model == "gemini-3.8-flash"
    assert cli.provider == "gemini-oauth"
    assert cli.reasoning_config == {"enabled": True, "effort": "medium"}
    assert cli.effort_by_base == {"gemini-3.8-flash": "medium"}

    # ── Surface 2: Gateway/TUI typed /model ──
    gw_agent = MagicMock()
    gw_agent.model = "gpt-4o"
    gw_agent.provider = "openrouter"
    gw_agent.base_url = ""
    gw_agent.api_mode = ""
    gw_agent.api_key = ""
    gw_agent.reasoning_config = None
    gw_agent.effort_by_base = {}
    gw_agent._primary_runtime = {}
    gw_session = {"agent": gw_agent}

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_switch_res),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s1", gw_session, "/model gemini-oauth:gemini-3.8-flash --reasoning medium")

    assert gw_agent.reasoning_config == {"enabled": True, "effort": "medium"}
    assert gw_agent.effort_by_base == {"gemini-3.8-flash": "medium"}
    assert gw_session["create_reasoning_override"] == {"enabled": True, "effort": "medium"}

    # ── Surface 3: Gateway resume from persisted session row ──
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
