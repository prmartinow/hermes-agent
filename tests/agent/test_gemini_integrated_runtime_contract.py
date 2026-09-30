"""Integrated invariant runtime contract tests spanning Action Items 1, 2, and 3.

Covers:
1. Real agent.switch_model(): 3.8 low + signed tool history -> 3.6 medium
   -> native carrier replay succeeds with preserved signature and static wire slug.
2. Real try_activate_fallback(): 3.8 medium -> Claude fallback/excursion
   -> foreign unsigned call -> real restore_primary_runtime() returns 3.8 medium
   -> foreign call receives skip_thought_signature_validator, original retains signature.
3. Real temporary SessionDB persistence: live agent with effort_by_base (3.8 low, 3.1 high)
   -> _persist_live_session_runtime writes to actual SQLite state.db
   -> actual row verifies reasoning_config is persisted while effort_by_base is NOT serialized
   -> cold agent reconstruction seeds ONLY active 3.8 low (3.1 pruned)
   -> historical signed function call replayed with active model.
4. Real switch transaction: OpenRouter history -> _apply_model_switch to Cloud Code 3.8 high
   -> target effort resolved independently without aggregator bleed
   -> unsigned history projected with skip_thought_signature_validator.

Neither the history projection layer nor the reasoning/wire resolver is mocked away.
"""

import copy
import json
import tempfile
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from agent.gemini_cloudcode_models import (
    resolve_model_selection,
    selectable_reasoning_efforts,
)
from agent.reasoning_selection import (
    canonical_reasoning_base,
    resolve_effective_reasoning_config,
    remember_reasoning_effort,
)
from agent.gemini_native_adapter import (
    _build_gemini_contents,
    translate_gemini_response,
)
from agent.native_replay import (
    build_google_native_carrier,
    find_native_assistant_detail,
    usable_google_native_carrier,
)
from agent.chat_completion_helpers import (
    build_assistant_message,
    try_activate_fallback,
)
from agent.transports import get_transport
from providers import get_provider_profile
from agent.agent_runtime_helpers import (
    restore_primary_runtime,
    switch_model,
)
from run_agent import AIAgent
from hermes_state import SessionDB
import tui_gateway.server as server


def _fake_build_client(ag, api_key="fake", base_url="", *a, **k):
    ag.api_key = api_key or getattr(ag, "api_key", "fake")
    ag.base_url = base_url or getattr(ag, "base_url", "")
    ag._client_kwargs = {}
    ag.client = MagicMock()


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
    Real agent.switch_model(): 3.8 low -> 3.6 medium.
    Outbound contents built for resulting agent.model:
    replays native carrier verbatim with thoughtSignature preserved and static wire slug.
    """
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("agent.agent_init._snapshot_primary_runtime"):
        agent = AIAgent(
            model="gemini-3.8-flash",
            provider="gemini-oauth",
            api_key="fake",
            reasoning_config={"enabled": True, "effort": "low"},
            quiet_mode=True,
        )
        agent.effort_by_base = {"gemini-3.8-flash": "low"}

    route_38 = resolve_model_selection(agent.model, effort="low")
    assert route_38.wire_model == "gemini-3.8-flash-tiered"
    assert route_38.thinking_config.get("thinkingLevel") == "low"

    # Assistant turn with signed tool call
    carrier = _make_dummy_carrier(tool_name="get_weather", tool_args={"city": "Tokyo"})
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
    original_history = copy.deepcopy(history)

    # Real in-place switch to 3.6 medium
    remember_reasoning_effort(agent.effort_by_base, provider="gemini-oauth", model="gemini-3.6-flash", effort="medium")
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("agent.agent_runtime_helpers._update_switch_compressor"):
        agent.switch_model("gemini-3.6-flash", "gemini-oauth")

    assert agent.model == "gemini-3.6-flash"
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.6-flash": "medium"}

    route_36 = resolve_model_selection(agent.model, effort="medium")
    assert route_36.wire_model == "gemini-3.6-flash-medium"
    assert route_36.thinking_config is None

    # Outbound contents built using resulting agent.model
    gemini_contents, _ = _build_gemini_contents(history, model=agent.model)
    assert len(gemini_contents) == 3

    # Invariant: history preserved non-destructively
    assert history == original_history

    # Invariant: Model turn replayed verbatim from carrier with thoughtSignature preserved!
    model_turn = gemini_contents[1]
    assert model_turn["role"] == "model"
    part = [p for p in model_turn["parts"] if "functionCall" in p][0]
    assert part["thoughtSignature"] == "dGVzdF9zaWduYXR1cmVfYnl0ZXM="
    assert part["functionCall"]["name"] == "get_weather"


def test_scenario_2_partner_fallback_excursion_and_foreign_projection():
    """Scenario 2:
    Real try_activate_fallback(): 3.8 medium -> Claude partner model
    -> foreign unsigned call -> real restore_primary_runtime() returns 3.8 medium.
    Outbound contents built using resulting agent.model:
    foreign call receives skip_thought_signature_validator on wire copy,
    while original signed Gemini call retains its native thoughtSignature.
    """
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
        agent = AIAgent(
            model="gemini-3.8-flash",
            provider="gemini-oauth",
            api_key="fake",
            reasoning_config={"enabled": True, "effort": "medium"},
            quiet_mode=True,
        )
        agent.effort_by_base = {"gemini-3.8-flash": "medium"}

    agent.context_compressor = MagicMock()
    agent.context_compressor.context_length = 100000
    agent.context_compressor.update_model = MagicMock()
    agent._fallback_chain = [{"provider": "gemini-oauth", "model": "claude-sonnet-4-6"}]
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._rate_limited_until = 0

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
    original_history = copy.deepcopy(history)

    # 1. Real fallback activation to Claude
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(MagicMock(), "claude-sonnet-4-6")),          patch("agent.fallback_cooldown._arm_rate_limit_cooldown", return_value=0),          patch("hermes_cli.config.load_config", return_value={}):
        fallback_ok = try_activate_fallback(agent)
        assert fallback_ok is True

    # Invariant: Fallback mutates route/provider and destination reasoning becomes None!
    assert agent.model == "claude-sonnet-4-6"
    assert agent.reasoning_config is None
    # Invariant: Fallback activation did NOT mutate effort_by_base!
    assert agent.effort_by_base == {"gemini-3.8-flash": "medium"}

    # Claude turn produces unsigned tool call
    foreign_asst = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_claude2", "type": "function", "function": {"name": "tool_claude", "arguments": "{}"}}],
    }
    history.append(foreign_asst)
    history.append({"role": "tool", "tool_call_id": "call_claude2", "content": '{"result": "claude_done"}'})

    # 2. Next turn: real primary runtime restoration returns agent to 3.8 medium
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"),          patch("agent.agent_runtime_helpers._rebind_primary_credential_pool"),          patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"):
        restored = restore_primary_runtime(agent)
        assert restored is True
        assert agent.model == "gemini-3.8-flash"
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
        assert agent.effort_by_base == {"gemini-3.8-flash": "medium"}

    # 3. Outbound contents built using restored agent.model (gemini-3.8-flash)
    gemini_contents, _ = _build_gemini_contents(history, model=agent.model)
    assert len(gemini_contents) == 5

    # Original Gemini tool call retains its signature
    gem_part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    assert gem_part["thoughtSignature"] == "c2lnX2dlbWluaV9wcmltYXJ5"

    # Foreign Claude tool call receives synthesized bypass sentinel on wire copy!
    claude_part = [p for p in gemini_contents[3]["parts"] if "functionCall" in p][0]
    assert claude_part["thoughtSignature"] == "skip_thought_signature_validator"


def test_scenario_3_persist_cold_resume_replays_signed_call_and_prunes_unrelated_map():
    """Scenario 3:
    Real temporary SessionDB persistence: live agent with effort_by_base (3.8 low, 3.1 high)
    -> _persist_live_session_runtime writes to actual SQLite state.db
    -> actual row verifies reasoning_config is persisted while effort_by_base is NOT serialized
    -> cold agent reconstruction seeds ONLY active 3.8 low (3.1 pruned)
    -> historical signed function call replayed with active model.
    """
    tmp_dir = tempfile.mkdtemp()
    db = SessionDB(Path(tmp_dir) / "state.db")
    sid = "sess_m1_sc3"
    db.create_session(sid, "Test Session", model="gemini-3.8-flash", model_config="{}")

    # 1. Live agent with two memory entries
    agent = MagicMock()
    agent.model = "gemini-3.8-flash"
    agent.provider = "gemini-oauth"
    agent.base_url = ""
    agent.api_mode = ""
    agent.service_tier = None
    agent.reasoning_config = {"enabled": True, "effort": "low"}
    agent.effort_by_base = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
    agent._session_db = db

    session = {"agent": agent, "session_key": sid}

    # Real persistence to SQLite DB!
    server._persist_live_session_runtime(session)

    # 2. Read actual session row from DB
    row = db.get_session(sid)
    assert row is not None
    assert row["model"] == "gemini-3.8-flash"

    # Invariant: stored model_config contains reasoning_config, and does NOT contain effort_by_base!
    stored_cfg = json.loads(row["model_config"])
    assert stored_cfg["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert "effort_by_base" not in stored_cfg

    # 3. Extract overrides from actual row and perform cold agent reconstruction
    overrides = server._stored_session_runtime_overrides(row)

    def _fake_resolve_runtime(model_override, provider_override):
        m = model_override.get('model') if isinstance(model_override, dict) else model_override
        p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
        return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}

    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),          patch("tui_gateway.server._load_cfg", return_value={}),          patch("tui_gateway.server._startup_system_prompt", return_value=""),          patch("agent.shell_hooks.register_from_config"):

        resumed_agent = server._make_agent(
            sid=sid,
            key=sid,
            session_db=db,
            model_override=overrides.get("model_override"),
            provider_override=overrides.get("provider_override"),
            reasoning_config_override=overrides.get("reasoning_config_override"),
        )

    # Invariant: Resumed agent seeds ONLY active canonical base, historical 3.1 Pro entry is pruned!
    assert resumed_agent.model == "gemini-3.8-flash"
    assert resumed_agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert resumed_agent.effort_by_base == {"gemini-3.8-flash": "low"}
    assert "gemini-3.1-pro" not in resumed_agent.effort_by_base

    # 4. Outbound wire request built from history using resumed_agent.model
    carrier = _make_dummy_carrier(thought_sig="c2lnX3Jlc3VtZV90ZXN0", tool_call_id="call_res1", tool_name="get_weather", tool_args={"city": "Tokyo"})
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
    original_history = copy.deepcopy(history)

    gemini_contents, _ = _build_gemini_contents(history, model=resumed_agent.model)
    part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    assert part["thoughtSignature"] == "c2lnX3Jlc3VtZV90ZXN0"
    assert history == original_history

    route = resolve_model_selection(resumed_agent.model, effort=resumed_agent.reasoning_config["effort"])
    assert route.wire_model == "gemini-3.8-flash-tiered"
    assert route.thinking_config.get("thinkingLevel") == "low"


def test_scenario_4_openrouter_history_switch_to_cloudcode():
    """Scenario 4:
    Real switch transaction: OpenRouter history -> _apply_model_switch to Cloud Code 3.8 high
    -> target effort resolved independently without aggregator bleed
    -> unsigned history projected with skip_thought_signature_validator.
    """
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("agent.agent_init._snapshot_primary_runtime"):
        agent = AIAgent(
            model="google/gemini-3.8-flash",
            provider="openrouter",
            api_key="fake",
            reasoning_config=None,
            quiet_mode=True,
        )
        agent.effort_by_base = {}

    session = {"agent": agent}

    history = [
        {"role": "user", "content": "Check docs"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_orp1", "type": "function", "function": {"name": "read_docs", "arguments": '{"topic": "api"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_orp1", "content": "Doc content"},
    ]
    original_history = copy.deepcopy(history)

    validation_verdict = {"accepted": True, "persist": True, "recognized": True, "message": ""}

    # Real switch transaction through gateway
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("agent.agent_runtime_helpers._update_switch_compressor"),          patch("hermes_cli.models_validate.validate_requested_model", return_value=validation_verdict),          patch.object(server, "_restart_slash_worker", return_value=None),          patch.object(server, "_persist_live_session_runtime", return_value=None),          patch.object(server, "_persist_live_session_system_prompt", return_value=None),          patch.object(server, "_append_model_switch_marker", return_value=None),          patch.object(server, "_emit_session_info", return_value=None):

        server._apply_model_switch("s_sw", session, "gemini-3.8-flash --provider gemini-oauth --reasoning high")

    # Invariant: Real switch transaction updated live agent
    assert agent.model == "gemini-3.8-flash"
    assert agent.provider == "gemini-oauth"
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert agent.effort_by_base == {"gemini-3.8-flash": "high"}

    # Outbound wire contents built using resulting agent.model (gemini-3.8-flash)
    gemini_contents, _ = _build_gemini_contents(history, model=agent.model)
    part = [p for p in gemini_contents[1]["parts"] if "functionCall" in p][0]
    # Invariant: OpenRouter unsigned call projected with controlled bypass sentinel!
    assert part["thoughtSignature"] == "skip_thought_signature_validator"
    assert history == original_history

    route = resolve_model_selection(agent.model, effort="high")
    assert route.wire_model == "gemini-3.8-flash-tiered"
    assert route.thinking_config.get("thinkingLevel") == "high"
