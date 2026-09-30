"""Tests for Gemini reasoning fallback and primary restoration closure (Action Item 3, Milestone 5)."""

import pytest
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config
from agent.agent_runtime_helpers import restore_primary_runtime


def _make_mock_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="low"):
    agent = MagicMock()
    agent.model = model
    agent.provider = provider
    agent.requested_provider = provider
    agent.base_url = ""
    agent.api_mode = ""
    agent.api_key = ""
    agent._client_kwargs = {}
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = {"enabled": True, "effort": effort} if effort else None
    agent.effort_by_base = {model: effort} if effort else {}
    agent.context_compressor = MagicMock()
    agent._fallback_activated = True
    agent._rate_limited_until = 0
    agent._primary_runtime = {
        "model": model,
        "provider": provider,
        "requested_provider": provider,
        "base_url": "",
        "api_mode": "",
        "api_key": "",
        "client_kwargs": {},
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "compressor_model": model,
        "compressor_context_length": 100000,
        "compressor_base_url": "",
        "compressor_api_key": "",
        "compressor_provider": provider,
        "compressor_api_mode": "",
        "reasoning_config": {"enabled": True, "effort": effort} if effort else None,
    }
    return agent


def test_fallback_case_a_independent_target_memories():
    """Case A: Independent target memories in effort_by_base.
    primary: 3.8 low
    fallback: 3.1 pro
    remembered 3.1 = high
    -> fallback reasoning config = high, map unchanged.
    -> primary restoration: 3.8 low.
    """
    agent = _make_mock_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="low")
    agent.effort_by_base["gemini-3.1-pro"] = "high"

    # Fallback to 3.1 Pro occurs
    agent.model = "gemini-3.1-pro"
    with patch("hermes_cli.config.load_config", return_value={}):
        _reresolve_fallback_reasoning_config(agent)

    # Invariant: Fallback resolves to remembered 3.1 high!
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    # Invariant: Fallback resolution does NOT mutate effort_by_base!
    assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}

    # Primary restoration on next turn
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"),          patch("agent.agent_runtime_helpers._rebind_primary_credential_pool"),          patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"):
        restored = restore_primary_runtime(agent)
        assert restored is True
        assert agent.model == "gemini-3.8-flash"
        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        assert agent.effort_by_base == {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}


def test_fallback_case_b_fallback_without_memory():
    """Case B: Fallback without runtime memory uses config reasoning_overrides.
    primary: 3.8 low
    fallback: 3.6 flash
    config override 3.6 = medium
    -> fallback reasoning config = medium.
    """
    agent = _make_mock_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="low")

    # Fallback to 3.6 occurs
    agent.model = "gemini-3.6-flash"
    cfg = {"agent": {"reasoning_overrides": {"gemini-3.6-flash": "medium"}}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        _reresolve_fallback_reasoning_config(agent)

    # Invariant: Fallback resolves to config override medium!
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    # Invariant: No mutation of effort_by_base!
    assert "gemini-3.6-flash" not in agent.effort_by_base


def test_fallback_case_c_partner_fallback():
    """Case C: Partner fallback to Claude.
    primary: 3.8 medium
    fallback: claude-sonnet-4-6
    -> no Cloud Code reasoning config invented (None).
    -> primary restoration returns to medium.
    """
    agent = _make_mock_agent(model="gemini-3.8-flash", provider="gemini-oauth", effort="medium")

    # Fallback to Claude
    agent.model = "claude-sonnet-4-6"
    with patch("hermes_cli.config.load_config", return_value={}):
        _reresolve_fallback_reasoning_config(agent)

    # Invariant: No synthetic Gemini effort invented for partner model!
    assert agent.reasoning_config is None

    # Primary restoration on next turn
    with patch("agent.agent_runtime_helpers._rebuild_primary_client"),          patch("agent.agent_runtime_helpers._rebind_primary_credential_pool"),          patch("agent.chat_completion_helpers.rewrite_prompt_model_identity"):
        restored = restore_primary_runtime(agent)
        assert restored is True
        assert agent.model == "gemini-3.8-flash"
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}


def test_fallback_case_d_cross_provider_fallback_into_gemini():
    """Case D: Generic primary falling back to Gemini resolves Gemini's own state,
    not inheriting generic provider's reasoning level.
    primary: openrouter gpt-4o with reasoning disabled
    fallback: gemini-oauth gemini-3.8-flash
    -> resolves Gemini's default/remembered effort (high), NOT generic disabled state!
    """
    agent = _make_mock_agent(model="gpt-4o", provider="openrouter", effort=None)
    agent.reasoning_config = {"enabled": False}

    # Fallback to Gemini Cloud Code route
    agent.provider = "gemini-oauth"
    agent.model = "gemini-3.8-flash"
    with patch("hermes_cli.config.load_config", return_value={}):
        _reresolve_fallback_reasoning_config(agent)

    # Invariant: Cloud Code model default (high) resolves cleanly!
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
