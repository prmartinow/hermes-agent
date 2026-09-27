"""Targeted tests for usage anchor model/provider scoping, in-place switch clearing,
and restart rejection of mismatched anchors.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_context import _preflight_request_tokens
from agent.usage_anchor import (
    USAGE_ANCHOR_MODEL_CONFIG_KEY,
    capture_usage_anchor,
    persist_usage_anchor,
    restore_usage_anchor,
    set_usage_anchor,
)
from hermes_state import SessionDB


def _create_test_agent(tmp_path: Path, session_id: str, model: str = "gpt-4o", provider: str = "openai"):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
        db = SessionDB(db_path=hermes_home / "state.db")
        db.create_session(session_id, source="cli")
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"),
        ):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key-mock",
                base_url="https://api.openai.com/v1",
                provider=provider,
                api_mode="chat_completions",
                model=model,
                quiet_mode=True,
                session_db=db,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
            )
        agent.client = MagicMock()
        agent.compression_enabled = True
        return db, agent


class TestUsageAnchorModelSwitchAndScoping:
    def test_switch_model_same_model_diff_provider_clears_both_anchors(self, tmp_path: Path):
        """When switching providers for the same model (e.g. openai -> openrouter),
        both usage anchors and DB row must be cleared."""
        db, agent = _create_test_agent(tmp_path, "SESS_SAME_MODEL_DIFF_PROV", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(50_000, 100, msgs)
        set_usage_anchor(agent, anchor, turn_base=True)
        assert agent._usage_anchor["prompt_tokens"] == 50_000
        assert agent._turn_base_usage_anchor["prompt_tokens"] == 50_000
        assert db.get_session_model_config_value("SESS_SAME_MODEL_DIFF_PROV", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is not None

        # Switch provider with same model
        with patch("hermes_cli.config.load_config", return_value={}):
            agent.switch_model("gpt-4o", "openrouter", base_url="https://openrouter.ai/api/v1")

        assert agent.provider == "openrouter"
        assert agent._usage_anchor is None, "_usage_anchor must be cleared on provider switch"
        assert agent._turn_base_usage_anchor is None, "_turn_base_usage_anchor must be cleared on provider switch"
        assert db.get_session_model_config_value("SESS_SAME_MODEL_DIFF_PROV", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None

        # Preflight must be unanchored
        tokens = _preflight_request_tokens(agent, msgs, "sys")
        assert tokens < 50_000
        assert agent._request_pressure_anchored is False

    def test_switch_model_failed_switch_retains_valid_state(self, tmp_path: Path):
        """When switch_model fails midway, pre-switch state and anchors must be retained."""
        db, agent = _create_test_agent(tmp_path, "SESS_FAILED_SWITCH", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(45_000, 50, msgs)
        set_usage_anchor(agent, anchor, turn_base=True)

        # Attempt switch that fails during runtime swap
        with patch("agent.agent_runtime_helpers._build_switched_client", side_effect=RuntimeError("connection failed")):
            with pytest.raises(RuntimeError, match="connection failed"):
                agent.switch_model("claude-3-5-haiku", "anthropic", base_url="https://api.anthropic.com")

        # Must retain original model, provider, and anchors
        assert agent.model == "gpt-4o"
        assert agent.provider == "openai"
        assert agent._usage_anchor is not None
        assert agent._usage_anchor["prompt_tokens"] == 45_000
        assert agent._turn_base_usage_anchor is not None
        assert agent._turn_base_usage_anchor["prompt_tokens"] == 45_000
        persisted = db.get_session_model_config_value("SESS_FAILED_SWITCH", USAGE_ANCHOR_MODEL_CONFIG_KEY, None)
        assert persisted is not None
        assert persisted["prompt_tokens"] == 45_000

    def test_restart_rejects_mismatched_model_anchor_safely(self, tmp_path: Path):
        """When a session restarts with a different model, the persisted anchor is rejected
        and cleared, leaving unanchored estimation without leaking stale counters."""
        db, agent1 = _create_test_agent(tmp_path, "SESS_RESTART_DIFF_MODEL", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(90_000, 200, msgs)
        set_usage_anchor(agent1, anchor)
        assert db.get_session_model_config_value("SESS_RESTART_DIFF_MODEL", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is not None

        # Simulate fresh process restart with a different model
        hermes_home = tmp_path / "hermes_home"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            db2, agent2 = _create_test_agent(tmp_path, "SESS_RESTART_DIFF_MODEL", model="claude-3-5-haiku", provider="anthropic")
            restore_usage_anchor(agent2, msgs)

            assert agent2._usage_anchor is None, "Mismatched model anchor must be rejected"
            # DB must be cleared so stale counters do not linger
            assert db2.get_session_model_config_value("SESS_RESTART_DIFF_MODEL", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None
            tokens = _preflight_request_tokens(agent2, msgs, "sys")
            assert tokens < 90_000
            assert agent2._request_pressure_anchored is False

    def test_restart_rejects_same_model_diff_provider_anchor_safely(self, tmp_path: Path):
        """When a session restarts with the same model but different provider,
        mismatched provider anchor must be rejected and cleared."""
        db, agent1 = _create_test_agent(tmp_path, "SESS_RESTART_DIFF_PROV", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(75_000, 150, msgs)
        set_usage_anchor(agent1, anchor)

        # Simulate fresh process restart with same model on openrouter
        hermes_home = tmp_path / "hermes_home"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            db2, agent2 = _create_test_agent(tmp_path, "SESS_RESTART_DIFF_PROV", model="gpt-4o", provider="openrouter")
            restore_usage_anchor(agent2, msgs)

            assert agent2._usage_anchor is None, "Mismatched provider anchor must be rejected"
            assert db2.get_session_model_config_value("SESS_RESTART_DIFF_PROV", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None
            tokens = _preflight_request_tokens(agent2, msgs, "sys")
            assert tokens < 75_000
            assert agent2._request_pressure_anchored is False

    def test_restart_preserves_same_runtime_continuation(self, tmp_path: Path):
        """When a session restarts with the exact same model and provider,
        the anchor must be safely restored to preserve accurate accounting."""
        db, agent1 = _create_test_agent(tmp_path, "SESS_RESTART_SAME_RUNTIME", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(60_000, 120, msgs)
        set_usage_anchor(agent1, anchor)

        # Simulate fresh process restart with matching runtime
        hermes_home = tmp_path / "hermes_home"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            db2, agent2 = _create_test_agent(tmp_path, "SESS_RESTART_SAME_RUNTIME", model="gpt-4o", provider="openai")
            restore_usage_anchor(agent2, msgs)

            assert agent2._usage_anchor is not None, "Matching runtime anchor must be restored"
            assert agent2._usage_anchor["prompt_tokens"] == 60_000
            assert agent2._usage_anchor["model"] == "gpt-4o"
            assert agent2._usage_anchor["provider"] == "openai"
            tokens = _preflight_request_tokens(agent2, msgs, "sys")
            assert tokens >= 60_000
            assert agent2._request_pressure_anchored is True

    def test_switch_model_same_runtime_reselect_preserves_anchor(self, tmp_path: Path):
        """Re-selecting the current model/provider (e.g. credential reload) must preserve anchor."""
        db, agent = _create_test_agent(tmp_path, "SESS_SAME_RESELECT", model="gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "hello world"}]

        anchor = capture_usage_anchor(30_000, 50, msgs)
        set_usage_anchor(agent, anchor, turn_base=True)

        with patch("hermes_cli.config.load_config", return_value={}):
            agent.switch_model("gpt-4o", "openai", base_url=agent.base_url)

        assert agent._usage_anchor is not None
        assert agent._usage_anchor["prompt_tokens"] == 30_000
        assert agent._turn_base_usage_anchor is not None
        assert agent._turn_base_usage_anchor["prompt_tokens"] == 30_000
