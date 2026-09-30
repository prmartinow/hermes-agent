"""Tests for Gemini reasoning resume and agent rebuild closure on Gateway surface (Action Item 3, Milestone 5)."""

import pytest
from unittest.mock import MagicMock, patch

import tui_gateway.server as server


def _fake_build_client(agent, *a, **k):
    agent._client_kwargs = {}
    agent.client = MagicMock()


def _fake_resolve_runtime(model_override, provider_override):
    m = model_override.get('model') if isinstance(model_override, dict) else model_override
    p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
    return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}


@pytest.fixture(autouse=True)
def _patch_agent_build_env():
    with patch("agent.agent_init._build_client", side_effect=_fake_build_client),          patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),          patch("tui_gateway.server._load_cfg", return_value={}),          patch("tui_gateway.server._startup_system_prompt", return_value=""),          patch("agent.shell_hooks.register_from_config"):
        yield


def test_resume_seeds_only_active_canonical_base():
    """Pin: Cold/eager/deferred resumed Cloud Code session with stored reasoning=low
    starts with reasoning_config={"enabled": True, "effort": "low"}
    and effort_by_base={"gemini-3.8-flash": "low"}.
    """
    model_override = {
        "model": "gemini-3.8-flash",
        "provider": "gemini-oauth",
        "base_url": "",
        "api_mode": "",
    }
    reasoning_override = {"enabled": True, "effort": "low"}

    agent = server._make_agent(
        sid="s1",
        key="k1",
        model_override=model_override,
        provider_override="gemini-oauth",
        reasoning_config_override=reasoning_override,
    )

    assert agent.model == "gemini-3.8-flash"
    assert agent.provider == "gemini-oauth"
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    # Invariant: effort_by_base is seeded with active canonical base!
    assert agent.effort_by_base == {"gemini-3.8-flash": "low"}


def test_resume_disabled_reasoning_does_not_seed_effort_by_base():
    """Pin: Persisted disabled reasoning ({"enabled": False}) restores disabled state,
    but leaves effort_by_base empty (no sentinel inserted).
    """
    model_override = {
        "model": "gemini-3.8-flash",
        "provider": "gemini-oauth",
        "base_url": "",
        "api_mode": "",
    }
    reasoning_override = {"enabled": False}

    agent = server._make_agent(
        sid="s1",
        key="k1",
        model_override=model_override,
        provider_override="gemini-oauth",
        reasoning_config_override=reasoning_override,
    )

    assert agent.reasoning_config == {"enabled": False}
    assert agent.effort_by_base == {}


def test_resume_stale_unsupported_effort_not_seeded_and_falls_back():
    """Pin: Stale unsupported effort (e.g. gemini-3.1-pro with medium)
    is not seeded into effort_by_base and re-resolves via precedence/default.
    """
    model_override = {
        "model": "gemini-3.1-pro",
        "provider": "gemini-oauth",
        "base_url": "",
        "api_mode": "",
    }
    # medium is unsupported on 3.1 Pro (only low, high)
    reasoning_override = {"enabled": True, "effort": "medium"}

    agent = server._make_agent(
        sid="s1",
        key="k1",
        model_override=model_override,
        provider_override="gemini-oauth",
        reasoning_config_override=reasoning_override,
    )

    # Invariant: unsupported effort was NOT seeded!
    assert agent.effort_by_base == {}
    # Invariant: re-resolved to canonical default (high)!
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_restart_invariant_clears_prior_unrelated_entries():
    """Pin: Before restart: 3.8 low, 3.1 high, active = 3.8 low.
    After cold resume:
    effort_by_base contains only active 3.8 low, NOT 3.1 high.
    """
    active_model = "gemini-3.8-flash"
    active_reasoning = {"enabled": True, "effort": "low"}

    # Only active model and reasoning_config are persisted into session row
    model_override = {
        "model": active_model,
        "provider": "gemini-oauth",
        "base_url": "",
        "api_mode": "",
    }

    agent = server._make_agent(
        sid="s1",
        key="k1",
        model_override=model_override,
        provider_override="gemini-oauth",
        reasoning_config_override=active_reasoning,
    )

    # Invariant: Only the active model is seeded, the historical 3.1 entry is gone!
    assert agent.effort_by_base == {"gemini-3.8-flash": "low"}
    assert "gemini-3.1-pro" not in agent.effort_by_base
