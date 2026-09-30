"""Integrated invariant runtime contract tests spanning Action Items 1, 2, and 3.

Covers:
1. 3.8 low + signed tool history -> switch 3.6 medium -> replay succeeds with correct projection and static wire slug.
2. 3.8 medium -> Claude fallback/excursion -> unsigned/foreign projection rules remain valid -> return 3.8 medium.
3. 3.8 low -> persist session -> cold resume -> replay historical signed function call -> map contains only 3.8 low -> inference succeeds.
4. OpenRouter Gemini history -> Cloud Code Gemini switch -> sentinel projection applied where required -> target effort resolved independently.

Neither the history projection layer nor the reasoning/wire resolver is mocked away.
"""

import copy
import json
import pytest
from unittest.mock import MagicMock, patch

from agent.gemini_cloudcode_models import (
    resolve_model_selection,
    selectable_reasoning_efforts,
)
from agent.reasoning_selection import canonical_reasoning_base
from agent.gemini_native_adapter import (
    _build_gemini_contents,
    translate_gemini_response,
)
from agent.native_replay import (
    build_google_native_carrier,
    find_native_assistant_detail,
    usable_google_native_carrier,
)
from agent.reasoning_selection import (
    resolve_effective_reasoning_config,
    remember_reasoning_effort,
)
from agent.chat_completion_helpers import (
    build_assistant_message,
    _reresolve_fallback_reasoning_config,
)
from agent.transports import get_transport
from providers import get_provider_profile
from agent.agent_runtime_helpers import (
    restore_primary_runtime,
    switch_model,
)
import tui_gateway.server as server


def _make_dummy_carrier(
    thought_sig: str = "dGVzdF9zaWduYXR1cmVfYnl0ZXM=",
    tool_call_id: str = "call_abc123",
    tool_name: str = "get_weather",
    tool_args: dict = None,
):
    """Helper creating a genuine Google native model part list with thought signature."""
    args = tool_args if tool_args is not None else {"city": "Tokyo"}
    native_parts = [
        {"thought": True, "text": "Analyzing the user query."},
        {
            "functionCall": {
                "name": tool_name,
                "args": args,
            },
            "thoughtSignature": thought_sig,
        }
    ]
    return build_google_native_carrier(native_parts, source_model="gemini-3.8-flash-tiered")


def test_scenario_1_dynamic_switch_to_static_with_signed_history():
    """Scenario 1:
    3.8 low + signed tool history
    -> switch to 3.6 medium
    -> replay succeeds with exact native carrier projection and static wire slug.
    """
    # 1. Start agent on 3.8 flash with effort 'low'
    agent = MagicMock()
    agent.model = "gemini-3.8-flash"
    agent.provider = "gemini-oauth"
    agent.requested_provider = "gemini-oauth"
    agent.base_url = ""
    agent.api_mode = ""
    agent.api_key = "fake_key"
    agent._client_kwargs = {}
    agent.effort_by_base = {"gemini-3.8-flash": "low"}
    agent.reasoning_config = {"enabled": True, "effort": "low"}

    route_38 = resolve_model_selection(agent.model, effort="low")
    assert route_38.wire_model == "gemini-3.8-flash-tiered"
    assert route_38.thinking_config.get("thinkingLevel") == "low"

    # 2. Assistant turn with signed tool call
    carrier = _make_dummy_carrier()
    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
            }
        ],
        "reasoning_details": [carrier],
    }
    tool_resp_msg = {
        "role": "tool",
        "tool_call_id": "call_abc123",
        "content": '{"temperature": 22}',
    }
    history = [
        {"role": "user", "content": "What is the weather in Tokyo?"},
        assistant_msg,
        tool_resp_msg,
    ]

    # 3. Switch to 3.6 flash with effort 'medium'
    remember_reasoning_effort(agent.effort_by_base, provider="gemini-oauth", model="gemini-3.6-flash", effort="medium")
    agent.model = "gemini-3.6-flash"
    agent.reasoning_config = resolve_effective_reasoning_config(
        config={}, provider="gemini-oauth", model="gemini-3.6-flash", effort_by_base=agent.effort_by_base
    )
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}

    route_36 = resolve_model_selection(agent.model, effort="medium")
    # Invariant: 3.6 medium resolves to static wire model!
    assert route_36.wire_model == "gemini-3.6-flash-medium"
    assert route_36.thinking_config is None

    # 4. Outbound contents built for 3.6
    gemini_contents, _ = _build_gemini_contents(history, model="gemini-3.6-flash")
    assert len(gemini_contents) == 3

    # Invariant: Model turn replayed verbatim from carrier with thoughtSignature preserved!
    model_turn = gemini_contents[1]
    assert model_turn["role"] == "model"
    part = [p for p in model_turn["parts"] if "functionCall" in p][0]
    assert part["thoughtSignature"] == "dGVzdF9zaWduYXR1cmVfYnl0ZXM="
    assert part["functionCall"]["name"] == "get_weather"

    # Invariant: Function response matches
    user_tool_turn = gemini_contents[2]
    assert user_tool_turn["role"] == "user"
    fr_part = [p for p in user_tool_turn["parts"] if "functionResponse" in p][0]["functionResponse"]
    assert fr_part["name"] == "get_weather"


def test_scenario_2_partner_fallback_excursion_and_foreign_projection():
    """Scenario 2:
    3.8 medium -> Claude fallback/excursion -> unsigned foreign tool call -> primary restore 3.8 medium.
    Foreign Claude tool call receives skip_thought_signature_validator sentinel on Gemini wire copy,
    while original signed Gemini call retains its native thoughtSignature.
    """
    # 1. Primary agent 3.8 medium
    agent = MagicMock()
    agent.model = "gemini-3.8-flash"
    agent.provider = "gemini-oauth"
    agent.requested_provider = "gemini-oauth"
    agent.base_url = ""
    agent.api_mode = ""
    agent.api_key = "fake_key"
    agent._client_kwargs = {}
    agent.effort_by_base = {"gemini-3.8-flash": "medium"}
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    agent.context_compressor = MagicMock()
    agent._fallback_activated = True
    agent._rate_limited_until = 0
    agent._primary_runtime = {
        "model": "gemini-3.8-flash",
        "provider": "gemini-oauth",
        "requested_provider": "gemini-oauth",
        "base_url": "",
        "api_mode": "",
        "api_key": "fake_key",
        "client_kwargs": {},
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "compressor_model": "gemini-3.8-flash",
        "compressor_context_length": 100000,
        "compressor_base_url": "",
        "compressor_api_key": "fake_key",
        "compressor_provider": "gemini-oauth",
        "compressor_api_mode": "",
        "reasoning_config": {"enabled": True, "effort": "medium"},
    }

    # Native Gemini signed turn
    signed_carrier = _make_dummy_carrier(thought_sig="c2lnX2dlbWluaV9wcmltYXJ5", tool_call_id="call_gem1", tool_name="tool_gem", tool_args={})
    history = [
        {"role": "user", "content": "Query 1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_gem1", "type": "function", "function": {"name": "tool_gem", "arguments": "{}"}}],
            "reasoning_details": [signed_carrier],
        },
        {"role": "tool", "tool_call_id": "call_gem1", "content": '{"ok": 1}'},
    ]

    # 2. Fallback to Claude (partner model)
    agent.model = "claude-sonnet-4-6"
    with patch("hermes_cli.config.load_config", return_value={}):
        _reresolve_fallback_reasoning_config(agent)
    assert agent.reasoning_config is None

    # History projected for Claude: strips google native details non-destructively on wire copy
    transport = get_transport("chat_completions")
    claude_profile = get_provider_profile("gemini-oauth")
    claude_projected = transport.convert_messages(
        copy.deepcopy(history),
        model="claude-sonnet-4-6",
        base_url=claude_profile.base_url,
        provider_profile=claude_profile,
    )
    claude_asst = claude_projected[1]
    assert find_native_assistant_detail(claude_asst.get("reasoning_details")) is None

    # Claude responds with unsigned tool call
    foreign_asst = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_claude2", "type": "function", "function": {"name": "tool_claude", "arguments": "{}"}}],
        # Notice: no google native carrier attached!
    }
    history.append(foreign_asst)
    history.append({"role": "tool", "tool_call_id": "call_claude2", "content": '{"result": "claude_done"}'})

    # 3. Next turn: primary restoration returns agent to 3.8 medium
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"),          patch("agent.agent_runtime_helpers._rebind_primary_credential_pool"),          patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"):
        restored = restore_primary_runtime(agent)
        assert restored is True
        assert agent.model == "gemini-3.8-flash"
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
        assert agent.effort_by_base == {"gemini-3.8-flash": "medium"}

    # 4. Outbound contents to Gemini
    gemini_contents, _ = _build_gemini_contents(history, model="gemini-3.6-flash")
    assert len(gemini_contents) == 5

    # Original Gemini tool call retains its signature
    gem_part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    assert gem_part["thoughtSignature"] == "c2lnX2dlbWluaV9wcmltYXJ5"

    # Foreign Claude tool call receives synthesized bypass sentinel on wire copy!
    claude_part = [p for p in gemini_contents[3]["parts"] if "functionCall" in p][0]
    assert claude_part["thoughtSignature"] == "skip_thought_signature_validator"


def test_scenario_3_persist_cold_resume_replays_signed_call_and_prunes_unrelated_map():
    """Scenario 3:
    3.8 low (with prior 3.1 high in memory)
    -> persisted to DB
    -> cold resume
    -> effort_by_base contains only 3.8 low (historical 3.1 entry pruned)
    -> replay of historical signed function call succeeds with valid signature.
    """
    # 1. Pre-restart state
    pre_restart_map = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
    carrier = _make_dummy_carrier(thought_sig="c2lnX3Jlc3VtZV90ZXN0", tool_call_id="call_res1")
    history = [
        {"role": "user", "content": "User prompt"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_res1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}}],
            "reasoning_details": [carrier],
        },
        {"role": "tool", "tool_call_id": "call_res1", "content": '{"temp": 20}'},
    ]

    # Stored row in DB
    stored_row = {
        "model": "gemini-3.8-flash",
        "provider": "gemini-oauth",
        "model_config": json.dumps({
            "model": "gemini-3.8-flash",
            "provider": "gemini-oauth",
            "reasoning_config": {"enabled": True, "effort": "low"}
        })
    }
    overrides = server._stored_session_runtime_overrides(stored_row)

    # 2. Cold resume agent construction
    def _fake_build_client(agent, *a, **k):
        agent._client_kwargs = {}
        agent.client = MagicMock()

    def _fake_resolve_runtime(model_override, provider_override):
        m = model_override.get('model') if isinstance(model_override, dict) else model_override
        p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
        return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}

    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),          patch("tui_gateway.server._load_cfg", return_value={}),          patch("tui_gateway.server._startup_system_prompt", return_value=""),          patch("agent.shell_hooks.register_from_config"):

        resumed_agent = server._make_agent(
            sid="s_cold",
            key="k_cold",
            model_override=overrides.get("model_override"),
            provider_override=overrides.get("provider_override"),
            reasoning_config_override=overrides.get("reasoning_config_override"),
        )

    # Invariant: Resumed agent seeded with ONLY active canonical base
    assert resumed_agent.model == "gemini-3.8-flash"
    assert resumed_agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert resumed_agent.effort_by_base == {"gemini-3.8-flash": "low"}
    assert "gemini-3.1-pro" not in resumed_agent.effort_by_base

    # 3. Outbound wire request built from history
    gemini_contents, _ = _build_gemini_contents(history, model="gemini-3.6-flash")
    part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    assert part["thoughtSignature"] == "c2lnX3Jlc3VtZV90ZXN0"

    route = resolve_model_selection(resumed_agent.model, effort=resumed_agent.reasoning_config["effort"])
    assert route.wire_model == "gemini-3.8-flash-tiered"
    assert route.thinking_config.get("thinkingLevel") == "low"


def test_scenario_4_openrouter_history_switch_to_cloudcode():
    """Scenario 4:
    OpenRouter generic Gemini history
    -> switch to Cloud Code route
    -> unsigned history projected with skip_thought_signature_validator
    -> target Cloud Code reasoning resolved independently without aggregator bleed.
    """
    # 1. History created on OpenRouter (unsigned tool call)
    history = [
        {"role": "user", "content": "Check docs"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_orp1", "type": "function", "function": {"name": "read_docs", "arguments": '{"topic": "api"}'}}],
            # No Google carrier
        },
        {"role": "tool", "tool_call_id": "call_orp1", "content": "Doc content"},
    ]

    # 2. Switch to Cloud Code gemini-oauth:gemini-3.8-flash with effort 'high'
    gw_agent = MagicMock()
    gw_agent.model = "google/gemini-3.8-flash"
    gw_agent.provider = "openrouter"
    gw_agent.base_url = ""
    gw_agent.api_mode = ""
    gw_agent.api_key = "orp_key"
    gw_agent.reasoning_config = None
    gw_agent.effort_by_base = {}
    gw_agent._primary_runtime = {}
    gw_session = {"agent": gw_agent}

    fake_switch_res = MagicMock(
        success=True,
        new_model="gemini-3.8-flash",
        target_provider="gemini-oauth",
        base_url="",
        api_key="",
        api_mode="",
        model_info={},
        warning_message=None,
    )

    with patch("hermes_cli.model_switch.switch_model", return_value=fake_switch_res),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s_sw", gw_session, "/model gemini-oauth:gemini-3.8-flash --reasoning high")

    # Invariant: Cloud Code effort resolved independently
    assert gw_agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert gw_agent.effort_by_base == {"gemini-3.8-flash": "high"}

    # 3. Build outbound wire contents for next turn to Gemini Cloud Code PA
    gemini_contents, _ = _build_gemini_contents(history, model="gemini-3.6-flash")
    part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    # Invariant: OpenRouter unsigned call projected with controlled bypass sentinel!
    assert part["thoughtSignature"] == "skip_thought_signature_validator"

    route = resolve_model_selection("gemini-3.8-flash", effort="high")
    assert route.wire_model == "gemini-3.8-flash-tiered"
    assert route.thinking_config.get("thinkingLevel") == "high"
