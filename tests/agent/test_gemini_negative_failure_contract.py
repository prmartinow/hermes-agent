"""Negative and failure-path hardening contract tests for Gemini Cloud Code runtime (Action Item 4, Milestone 2).

Verifies fail-closed, atomic, and rollback-safe behavior across:
1. Unsupported reasoning effort selection (CLI & Gateway zero-I/O and no-op invariants).
2. Live model switch rollback across client creation, compressor update, and capability resolution failures.
3. Fallback activation failure memory immutability and fallback index recovery.
4. Signature failure semantics: corrupted signatures vs. missing signatures, and partial parallel groups.
5. Persisted state corruption: stale efforts, malformed reasoning_config, read-time DB immutability.
6. Capability failure and invalid account boundaries (gemini-0, gemini-6, gemini-42, generic routes).
7. Precedence resolver behavior with invalid/stale values.
8. Fallback reasoning config load failure resilience.
"""

import copy
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.gemini_cloudcode_models import (
    selectable_reasoning_efforts,
    resolve_model_selection,
    thought_circulation_support,
)
from agent.reasoning_selection import (
    canonical_reasoning_base,
    resolve_effective_reasoning_config,
    remember_reasoning_effort,
    reasoning_effort_error,
)
from agent.reasoning_selection import (
    resolve_effective_reasoning_config,
    remember_reasoning_effort,
    reasoning_effort_error,
)
from agent.native_replay import (
    build_google_native_carrier,
    find_native_assistant_detail,
    usable_google_native_carrier,
    classify_google_signature,
    GoogleSignatureKind,
)
from agent.gemini_native_adapter import (
    _build_gemini_contents,
)
from agent.chat_completion_helpers import (
    try_activate_fallback,
    _reresolve_fallback_reasoning_config,
)
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


# ==============================================================================
# 1. Unsupported Effort Selection (CLI & Gateway Zero-I/O and No-Op Invariants)
# ==============================================================================

class TestUnsupportedSelection:
    @pytest.mark.parametrize("cmd,expected_err", [
        ("/model gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
        ("/model gemini-3.8-flash --reasoning none", "gemini-3.8-flash has no 'none' effort"),
        ("/model gemini-3.1-pro --reasoning medium", "gemini-3.1-pro has no 'medium' effort"),
        ("/model claude-sonnet-4-6 --reasoning low", "is not supported for model 'claude-sonnet-4-6'"),
        ("/model gemini-3.1-flash-lite --reasoning low", "is not supported for model 'gemini-3.1-flash-lite'"),
        ("/model gpt-oss-120b-medium --reasoning low", "is not supported for model 'gpt-oss-120b-medium'"),
        ("/model gemini-3.8-flash-high --reasoning medium", "Conflicting reasoning effort: alias 'gemini-3.8-flash-high' implies 'high'"),
        ("/model gemini-oauth:gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
        ("/model gemini-2:gemini-3.1-pro --reasoning medium", "gemini-3.1-pro has no 'medium' effort"),
    ])
    def test_unsupported_effort_is_complete_noop_and_zero_io(self, cmd, expected_err):
        """Unsupported effort must fail before any route, network, credential, agent, session, or config mutation."""
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.base_url = ""
        agent.api_mode = ""
        agent.api_key = "key1"
        agent.reasoning_config = {"enabled": True, "effort": "low"}
        agent.effort_by_base = {"gemini-3.8-flash": "low"}
        agent.runtime_capabilities = {"test": True}
        agent._primary_runtime = {"model": "gemini-3.8-flash", "provider": "gemini-oauth"}

        session = {
            "agent": agent,
            "session_key": "s1",
            "model_override": None,
            "create_reasoning_override": None,
        }

        # Snapshot state before rejection
        pre_agent_model = agent.model
        pre_agent_provider = agent.provider
        pre_reasoning_config = copy.deepcopy(agent.reasoning_config)
        pre_effort_by_base = copy.deepcopy(agent.effort_by_base)
        pre_capabilities = copy.deepcopy(agent.runtime_capabilities)
        pre_primary = copy.deepcopy(agent._primary_runtime)

        with patch("hermes_cli.model_switch.switch_model") as mock_switch_model,              patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

            with pytest.raises(ValueError) as excinfo:
                server._apply_model_switch("s1", session, cmd)

            assert expected_err in str(excinfo.value)
            # Invariant: switch_model not called (zero I/O)
            mock_switch_model.assert_not_called()
            mock_write_cfg.assert_not_called()

        # Invariant: Complete state immutability
        assert agent.model == pre_agent_model
        assert agent.provider == pre_agent_provider
        assert agent.reasoning_config == pre_reasoning_config
        assert agent.effort_by_base == pre_effort_by_base
        assert agent.runtime_capabilities == pre_capabilities
        assert agent._primary_runtime == pre_primary
        assert session["model_override"] is None
        assert session["create_reasoning_override"] is None


# ==============================================================================
# 2. Live Model Switch Rollback (Client, Compressor, Capabilities)
# ==============================================================================


    @pytest.mark.parametrize("cmd,expected_err", [
        ("/model gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
        ("/model gemini-3.1-pro --reasoning medium", "gemini-3.1-pro has no 'medium' effort"),
        ("/model claude-sonnet-4-6 --reasoning low", "is not supported for model 'claude-sonnet-4-6'"),
        ("/model gemini-oauth:gemini-3.8-flash --reasoning max", "gemini-3.8-flash has no 'max' effort"),
        ("/model gemini-3.8-flash-high --reasoning medium", "Conflicting reasoning effort: alias 'gemini-3.8-flash-high' implies 'high'"),
    ])
    def test_classic_cli_unsupported_effort_is_complete_noop(self, cmd, expected_err):
        """Classic CLI unsupported effort must fail closed before _switch_model_from and leave all state unchanged."""
        from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
        from hermes_cli.cli_tui_mixin import CLITuiMixin

        class MockCLI(CLITuiMixin, CLIModelSwitchMixin):
            def __init__(self):
                self.model = "gemini-3.8-flash"
                self.provider = "gemini-oauth"
                self.requested_provider = "gemini-oauth"
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

        cli = MockCLI()
        cli.reasoning_config = {"enabled": True, "effort": "low"}
        cli.agent.reasoning_config = {"enabled": True, "effort": "low"}
        pre_model = cli.model
        pre_provider = cli.provider
        pre_effort = copy.deepcopy(cli.effort_by_base)
        pre_agent_effort = copy.deepcopy(cli.agent.effort_by_base)
        pre_reasoning = copy.deepcopy(cli.reasoning_config)
        pre_agent_reasoning = copy.deepcopy(cli.agent.reasoning_config)

        with patch("hermes_cli.cli_model_switch_mixin._switch_model_from") as mock_switch_from,              patch("hermes_cli.model_switch.persist_model_selection") as mock_persist,              patch("cli._cprint") as mock_cprint:

            cli._handle_model_switch(cmd)

            # Invariant: _switch_model_from and persistence never called!
            mock_switch_from.assert_not_called()
            mock_persist.assert_not_called()
            # Invariant: Error surfaced cleanly to user
            assert any(expected_err in str(call) for call in mock_cprint.call_args_list)

        # Invariant: State completely unchanged
        assert cli.model == pre_model
        assert cli.provider == pre_provider
        assert cli.effort_by_base == pre_effort
        assert cli.agent.effort_by_base == pre_agent_effort
        assert cli.reasoning_config == pre_reasoning
        assert cli.agent.reasoning_config == pre_agent_reasoning


class TestSwitchRollback:
    def test_switch_rollback_on_client_construction_failure(self):
        """Phase A: When client construction fails during switch_model(),
        rolls back model, provider, reasoning_config, effort_by_base, and primary runtime.
        """
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "low"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}
            agent.runtime_capabilities = {"circulates_thoughts": True}
            agent._client_kwargs = {"api_key": "key1"}

        pre_model = agent.model
        pre_provider = agent.provider
        pre_reasoning_config = copy.deepcopy(agent.reasoning_config)
        pre_effort_by_base = copy.deepcopy(agent.effort_by_base)
        pre_capabilities = copy.deepcopy(agent.runtime_capabilities)
        pre_primary = copy.deepcopy(agent._primary_runtime)

        def failing_build_client(ag, *a, **k):
            raise ConnectionError("Upstream API unreachable during switch")

        with patch("agent.agent_runtime_helpers._build_switched_client", side_effect=failing_build_client):
            with pytest.raises(ConnectionError):
                agent.switch_model("gemini-3.6-flash", "gemini-oauth")

        # Invariant: Atomic rollback of all runtime fields
        assert agent.model == pre_model
        assert agent.provider == pre_provider
        assert agent.reasoning_config == pre_reasoning_config
        assert agent.effort_by_base == pre_effort_by_base
        assert agent.runtime_capabilities == pre_capabilities
        assert agent._primary_runtime == pre_primary

    def test_switch_rollback_on_compressor_mutation_and_failure(self):
        """Phase B: When compressor mutates and then fails during switch_model(),
        rolls back all state cleanly without calling update_model() resets,
        preserving token counters, strikes, streaks, and cooldowns byte-for-byte.
        Guarantees agent._compressor_state is never leaked on agent.
        """
        from agent.context_compressor import ContextCompressor

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "low"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "low"}
            agent._client_kwargs = {"api_key": "key1", "base_url": ""}
            agent._use_prompt_caching = False
            agent._use_native_cache_layout = False
            agent._custom_providers = [{"name": "old_provider"}]

            compressor = ContextCompressor(
                model="gemini-3.8-flash",
                config_context_length=100000,
                provider="gemini-oauth",
                quiet_mode=True,
            )
            # Seed non-default bookkeeping state
            compressor.last_prompt_tokens = 30000
            compressor.last_completion_tokens = 4500
            compressor.last_total_tokens = 34500
            compressor._prellm_skip_count = 4
            compressor._fallback_compression_streak = 2
            compressor._ineffective_compression_count = 1
            compressor._summary_failure_cooldown_until = 999999.0
            compressor._consecutive_timeout_failures = 3

            agent.context_compressor = compressor

        # Invariant: No leaked _compressor_state attribute before switch
        assert not hasattr(agent, "_compressor_state")

        pre_model = agent.model
        pre_provider = agent.provider
        pre_reasoning = copy.deepcopy(agent.reasoning_config)
        pre_map = copy.deepcopy(agent.effort_by_base)
        pre_caching = agent._use_prompt_caching
        pre_layout = agent._use_native_cache_layout
        pre_custom = copy.deepcopy(agent._custom_providers)
        pre_primary = copy.deepcopy(agent._primary_runtime)

        def mutating_and_failing_compressor(ag, custom_providers, effective_context_length, snapshot):
            # Mutate compressor fields and wipe counters
            ag.context_compressor.model = "mutated_destination_model"
            ag.context_compressor.context_length = 999999
            ag.context_compressor.last_prompt_tokens = 0
            ag.context_compressor._prellm_skip_count = 0
            ag.context_compressor._fallback_compression_streak = 0
            ag.context_compressor._summary_failure_cooldown_until = 0.0
            raise RuntimeError("Compressor internal failure after mutation")

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client),              patch("agent.agent_runtime_helpers._update_switch_compressor", side_effect=mutating_and_failing_compressor):
            with pytest.raises(RuntimeError):
                agent.switch_model("gemini-3.6-flash", "gemini-oauth")

        # Invariant: No leaked _compressor_state attribute after rollback
        assert not hasattr(agent, "_compressor_state")

        # Invariant: Complete rollback of agent and compressor fields!
        assert agent.model == pre_model
        assert agent.provider == pre_provider
        assert agent.reasoning_config == pre_reasoning
        assert agent.effort_by_base == pre_map
        assert agent._use_prompt_caching == pre_caching
        assert agent._use_native_cache_layout == pre_layout
        assert agent._custom_providers == pre_custom
        assert agent._primary_runtime == pre_primary

        # Invariant: Non-default bookkeeping state restored byte-for-byte without reset
        assert agent.context_compressor.model == "gemini-3.8-flash"
        assert agent.context_compressor.last_prompt_tokens == 30000
        assert agent.context_compressor.last_completion_tokens == 4500
        assert agent.context_compressor.last_total_tokens == 34500
        assert agent.context_compressor._prellm_skip_count == 4
        assert agent.context_compressor._fallback_compression_streak == 2
        assert agent.context_compressor._ineffective_compression_count == 1
        assert agent.context_compressor._summary_failure_cooldown_until == 999999.0
        assert agent.context_compressor._consecutive_timeout_failures == 3

    def test_durable_compressor_state_preserved_on_switch_failure_and_cleared_on_success(self, tmp_path):
        """Milestone 2 Sign-Off Closure:
        1. ContextCompressor bound to real SessionDB with non-default strikes, streak, cooldown, runway.
        2. Failed switch preserves both in-memory runtime snapshot and SQLite durable values untouched.
        3. Positive control: successful switch makes resets durable in SQLite.
        """
        import time
        from hermes_state import SessionDB
        from agent.context_compressor import ContextCompressor, PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY

        db_path = tmp_path / "durable_compressor_test.db"
        db = SessionDB(db_path)
        db.create_session("s_durable_test", "cli", model="gemini-3.8-flash")

        future = time.time() + 999999.0
        db.set_compression_ineffective_count("s_durable_test", 1)
        db.set_compression_fallback_streak("s_durable_test", 2)
        db.record_compression_failure_cooldown("s_durable_test", future, "test failure cooldown")
        db.patch_session_model_config("s_durable_test", {PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY: 8192})

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "high"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "high"}
            agent._client_kwargs = {"api_key": "key1", "base_url": ""}

            comp = ContextCompressor(model="gemini-3.8-flash", config_context_length=100000, provider="gemini-oauth", quiet_mode=True)
            comp.bind_session_state(session_db=db, session_id="s_durable_test")
            agent.context_compressor = comp

        orig_snapshot = copy.deepcopy(comp.snapshot_switch_runtime())

        # Negative branch: Switch fails AFTER compressor update
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client),              patch("agent.agent_runtime_helpers._finish_switch", side_effect=RuntimeError("injected post-compressor failure")):
            with pytest.raises(RuntimeError):
                agent.switch_model("gemini-3.6-flash", "gemini-oauth")

        # Invariant 1: In-memory compressor restored exactly
        assert comp.snapshot_switch_runtime() == orig_snapshot
        assert not hasattr(agent, "_compressor_state")

        # Invariant 2: SQLite database retained all original durable values without reset
        assert db.get_compression_ineffective_count("s_durable_test") == 1
        assert db.get_compression_fallback_streak("s_durable_test") == 2
        assert db.get_compression_failure_cooldown("s_durable_test") is not None
        assert db.get_session_model_config_value("s_durable_test", PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY, 0) == 8192

        # Positive control: Successful switch commits durable resets to SQLite
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent.switch_model("gemini-3.6-flash", "gemini-oauth")

        assert agent.model == "gemini-3.6-flash"
        assert db.get_compression_ineffective_count("s_durable_test") == 0
        assert db.get_compression_fallback_streak("s_durable_test") == 0
        assert db.get_compression_failure_cooldown("s_durable_test") is None
        assert db.get_session_model_config_value("s_durable_test", PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY, 0) == 0

    def test_switch_snapshot_restore_is_non_destructive_and_idempotent(self):
        """Milestone 2 Sign-Off Closure:
        _restore_switch_snapshot() must not mutate the snapshot dictionary (no .pop())
        and must remain fully reusable across nested rollbacks.
        """
        from agent.agent_runtime_helpers import _snapshot_switch_state, _restore_switch_snapshot
        from agent.context_compressor import ContextCompressor

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "high"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "high"}
            comp = ContextCompressor(model="gemini-3.8-flash", config_context_length=100000, provider="gemini-oauth", quiet_mode=True)
            comp.last_prompt_tokens = 42000
            agent.context_compressor = comp

        snapshot = _snapshot_switch_state(agent)
        assert "_compressor_state" in snapshot

        # First restore
        _restore_switch_snapshot(agent, snapshot)
        assert "_compressor_state" in snapshot  # not popped!
        assert not hasattr(agent, "_compressor_state")
        assert agent.context_compressor.last_prompt_tokens == 42000

        # Mutate agent and compressor again
        agent.model = "mutated-model"
        agent.context_compressor.last_prompt_tokens = 0

        # Second restore with same snapshot
        _restore_switch_snapshot(agent, snapshot)
        assert not hasattr(agent, "_compressor_state")
        assert agent.model == "gemini-3.8-flash"
        assert agent.context_compressor.last_prompt_tokens == 42000

    def test_compressor_feasibility_probe_failure_does_not_abort_good_switch(self):
        """When compressor update succeeds, a failure in revalidate_compression_feasibility
        probe does NOT abort or roll back the good switch.
        """
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="key1",
                reasoning_config={"enabled": True, "effort": "low"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "low"}
            agent.context_compressor = MagicMock()
            agent.context_compressor.update_model = MagicMock()

        def failing_probe(ag):
            raise ConnectionError("Feasibility probe network hiccup")

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client),              patch("agent.conversation_compression.revalidate_compression_feasibility", side_effect=failing_probe):
            # Must NOT raise!
            agent.switch_model("gemini-3.6-flash", "gemini-oauth")

        # Invariant: Good switch completed successfully despite probe failure!
        assert agent.model == "gemini-3.6-flash"
        assert agent.provider == "gemini-oauth"


# ==============================================================================
# 3. Fallback Activation Failure & Recovery
# ==============================================================================

class TestFallbackFailure:
    def test_fallback_activation_failure_does_not_mutate_reasoning_memory(self):
        """When try_activate_fallback() fails (e.g. resolve_provider_client raises or chain exhausted),
        effort_by_base remains unmutated and no partial destination reasoning config is installed.
        """
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="fake",
                reasoning_config={"enabled": True, "effort": "medium"},
                quiet_mode=True,
            )
            agent.effort_by_base = {"gemini-3.8-flash": "medium", "gemini-3.1-pro": "high"}

        agent._fallback_chain = [{"provider": "gemini-oauth", "model": "claude-sonnet-4-6"}]
        agent._fallback_index = 0
        agent._fallback_activated = False

        pre_effort_by_base = copy.deepcopy(agent.effort_by_base)
        pre_reasoning_config = copy.deepcopy(agent.reasoning_config)

        # Force candidate construction failure
        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=RuntimeError("Provider credentials missing")):
            res = try_activate_fallback(agent)
            assert res is False

        # Invariant: Memory and reasoning config remain intact
        assert agent.effort_by_base == pre_effort_by_base
        assert agent.reasoning_config == pre_reasoning_config

    def test_fallback_index_exhaustion_recovery_on_next_turn(self):
        """When fallback chain is exhausted, restore_primary_runtime resets _fallback_index
        so future turns are not permanently stranded.
        """
        with patch("agent.agent_init._build_client", side_effect=_fake_build_client):
            agent = AIAgent(
                model="gemini-3.8-flash",
                provider="gemini-oauth",
                api_key="fake",
                reasoning_config={"enabled": True, "effort": "medium"},
                quiet_mode=True,
            )
        agent.context_compressor = MagicMock()
        agent._fallback_chain = [{"provider": "gemini-oauth", "model": "broken-model"}]
        agent._fallback_index = 0
        agent._fallback_activated = False
        agent._rate_limited_until = 0

        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)):
            res = try_activate_fallback(agent)
            assert res is False
            assert agent._fallback_index >= len(agent._fallback_chain)

        # Next turn start calls restore_primary_runtime
        restore_primary_runtime(agent)
        # Invariant: fallback index reset to 0!
        assert agent._fallback_index == 0


# ==============================================================================
# 4. Signature Failure Semantics (Corrupted vs. Missing & Parallel Groups)
# ==============================================================================

class TestSignatureFailureSemantics:
    def test_corrupted_signature_is_not_silently_converted_to_bypass(self):
        """A nonempty but corrupted signature must be classified as REAL / nonempty signature,
        and NEVER downgraded to synthesized skip_thought_signature_validator.
        """
        corrupted_sig = "garbage_not_a_valid_base64_signature"

        # Classification level
        kind = classify_google_signature(corrupted_sig)
        assert kind == GoogleSignatureKind.REAL
        assert kind != GoogleSignatureKind.BYPASS
        assert kind != GoogleSignatureKind.MISSING

        # Replay level
        parts = [
            {"functionCall": {"name": "test_tool", "args": {}}, "thoughtSignature": corrupted_sig}
        ]
        carrier = build_google_native_carrier(parts, source_model="gemini-3.8-flash-tiered")
        history = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}}],
                "reasoning_details": [carrier],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        ]

        contents, _ = _build_gemini_contents(history, model="gemini-3.8-flash")
        replayed_part = [p for p in contents[1]["parts"] if "functionCall" in p][0]

        # Invariant: Replayed signature preserves corrupted signature verbatim rather than bypass!
        assert replayed_part["thoughtSignature"] == corrupted_sig
        assert replayed_part["thoughtSignature"] != "skip_thought_signature_validator"

    def test_missing_vs_corrupt_wire_signatures_are_distinct(self):
        """Missing foreign signature -> bypass sentinel.
        Corrupt signature -> preserved real signature classification.
        missing_wire_signature != corrupt_wire_signature.
        """
        # Case A: Missing signature on foreign call
        history_missing = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c_missing", "type": "function", "function": {"name": "f1", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c_missing", "content": "{}"},
        ]
        contents_a, _ = _build_gemini_contents(history_missing, model="gemini-3.8-flash")
        sig_a = [p for p in contents_a[1]["parts"] if "functionCall" in p][0]["thoughtSignature"]
        assert sig_a == "skip_thought_signature_validator"

        # Case B: Corrupted nonempty signature
        parts_b = [{"functionCall": {"name": "f1", "args": {}}, "thoughtSignature": "corrupt_data"}]
        carrier_b = build_google_native_carrier(parts_b, source_model="gemini-3.8-flash-tiered")
        history_corrupt = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c_corrupt", "type": "function", "function": {"name": "f1", "arguments": "{}"}}],
                "reasoning_details": [carrier_b],
            },
            {"role": "tool", "tool_call_id": "c_corrupt", "content": "{}"},
        ]
        contents_b, _ = _build_gemini_contents(history_corrupt, model="gemini-3.8-flash")
        sig_b = [p for p in contents_b[1]["parts"] if "functionCall" in p][0]["thoughtSignature"]
        assert sig_b == "corrupt_data"

        # Invariant: Distinct treatment!
        assert sig_a != sig_b

    def test_partial_gemini_parallel_call_group_carrier_lost_branch(self):
        """In carrier-lost fallback: when tool A has extra_content.thought_signature (REAL),
        and tool B is unsigned (MISSING), group_has_real=True.
        Tool A receives exact REAL signature, and Tool B remains unsigned (NO bypass synthesized).
        Contrast with fully unsigned foreign group where both receive bypass sentinel.
        """
        # Carrier-lost group: NO reasoning_details / native carrier!
        history_native_carrier_lost = [
            {"role": "user", "content": "run parallel"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "ca",
                        "type": "function",
                        "function": {"name": "tool_a", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "sig_real_parallel_a"}},
                    },
                    {
                        "id": "cb",
                        "type": "function",
                        "function": {"name": "tool_b", "arguments": "{}"},
                        # tool_b has NO extra_content
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "ca", "content": "res_a"},
            {"role": "tool", "tool_call_id": "cb", "content": "res_b"},
        ]

        contents_native, _ = _build_gemini_contents(history_native_carrier_lost, model="gemini-3.8-flash")
        fc_native = [p for p in contents_native[1]["parts"] if "functionCall" in p]
        assert len(fc_native) == 2
        # Invariant: Tool A retains REAL signature
        assert fc_native[0]["thoughtSignature"] == "sig_real_parallel_a"
        # Invariant: Tool B remains unsigned (NO bypass synthesized onto sibling!)
        assert "thoughtSignature" not in fc_native[1]

        # Contrast: Fully unsigned foreign parallel group
        history_foreign = [
            {"role": "user", "content": "run parallel"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ]
        contents_foreign, _ = _build_gemini_contents(history_foreign, model="gemini-3.8-flash")
        fc_foreign = [p for p in contents_foreign[1]["parts"] if "functionCall" in p]
        assert len(fc_foreign) == 2
        # Invariant: Fully unsigned foreign group receives bypass on both calls!
        assert fc_foreign[0]["thoughtSignature"] == "skip_thought_signature_validator"
        assert fc_foreign[1]["thoughtSignature"] == "skip_thought_signature_validator"


# ==============================================================================
# 5. Persisted State Corruption (Stale, Malformed & Read-Time Immutability)
# ==============================================================================

class TestPersistedStateCorruption:
    def test_stale_persisted_effort_rejected_and_does_not_rewrite_db_on_read(self, tmp_path: Path):
        """Stored stale effort (e.g. gemini-3.1-pro with medium) is rejected during resume,
        effort_by_base remains empty for that stale model, and reading/resuming DOES NOT rewrite DB.
        """
        db = SessionDB(tmp_path / "state.db")
        sid = "s_stale"
        initial_config = {
            "model": "gemini-3.1-pro",
            "provider": "gemini-oauth",
            "reasoning_config": {"enabled": True, "effort": "medium"}  # medium unsupported on 3.1 Pro
        }
        db.create_session(sid, "Stale Session", model="gemini-3.1-pro", model_config=initial_config)

        row = db.get_session(sid)
        raw_before = row["model_config"]
        overrides = server._stored_session_runtime_overrides(row)

        def _fake_resolve_runtime(model_override, provider_override):
            m = model_override.get('model') if isinstance(model_override, dict) else model_override
            p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
            return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client),              patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),              patch("tui_gateway.server._load_cfg", return_value={}),              patch("tui_gateway.server._startup_system_prompt", return_value=""),              patch("agent.shell_hooks.register_from_config"):

            resumed = server._make_agent(
                sid=sid,
                key=sid,
                session_db=db,
                model_override=overrides.get("model_override"),
                provider_override=overrides.get("provider_override"),
                reasoning_config_override=overrides.get("reasoning_config_override"),
            )

        # Invariant: Stale unsupported effort rejected from seeding
        assert resumed.effort_by_base == {}
        # Invariant: Re-resolved to valid default (high)
        assert resumed.reasoning_config == {"enabled": True, "effort": "high"}

        # Invariant: DB row was NOT rewritten on read/resume!
        row_after = db.get_session(sid)
        assert row_after["model_config"] == raw_before

        if hasattr(db, "close"):
            db.close()

    @pytest.mark.parametrize("malformed_cfg", [
        "low",                                      # string instead of dict
        {"effort": ["low"]},                        # list instead of str
        {"enabled": True},                          # missing effort
        {"enabled": True, "effort": "ultra"},       # unsupported level
    ])
    def test_malformed_persisted_reasoning_config_fails_closed(self, tmp_path: Path, malformed_cfg):
        """Malformed reasoning_config in DB fails closed without unhandled exception,
        does not seed invalid effort into effort_by_base, and resolves through defaults.
        """
        db = SessionDB(tmp_path / "state_malformed.db")
        sid = "s_malformed"
        db.create_session(sid, "Malformed", model="gemini-3.8-flash", model_config={
            "model": "gemini-3.8-flash",
            "provider": "gemini-oauth",
            "reasoning_config": malformed_cfg,
        })

        row = db.get_session(sid)
        overrides = server._stored_session_runtime_overrides(row)

        def _fake_resolve_runtime(model_override, provider_override):
            m = model_override.get('model') if isinstance(model_override, dict) else model_override
            p = (model_override.get('provider') if isinstance(model_override, dict) else None) or provider_override or 'gemini-oauth'
            return m, {'provider': p, 'requested_provider': p, 'base_url': '', 'api_key': 'fake', 'api_mode': ''}

        with patch("agent.agent_init._build_client", side_effect=_fake_build_client),              patch("tui_gateway.server._resolve_agent_model_runtime", side_effect=_fake_resolve_runtime),              patch("tui_gateway.server._load_cfg", return_value={}),              patch("tui_gateway.server._startup_system_prompt", return_value=""),              patch("agent.shell_hooks.register_from_config"):

            resumed = server._make_agent(
                sid=sid,
                key=sid,
                session_db=db,
                model_override=overrides.get("model_override"),
                provider_override=overrides.get("provider_override"),
                reasoning_config_override=overrides.get("reasoning_config_override"),
            )

        # Invariant: No invalid effort seeded into effort_by_base!
        assert "ultra" not in resumed.effort_by_base
        assert not any(isinstance(v, list) for v in resumed.effort_by_base.values())
        # Invariant: Re-resolved to valid default (high)
        assert resumed.reasoning_config == {"enabled": True, "effort": "high"}

        # Invariant: DB read immutability -- reading malformed row does NOT rewrite DB!
        row_after = db.get_session(sid)
        assert row_after["model_config"] == row["model_config"]

        if hasattr(db, "close"):
            db.close()


# ==============================================================================
# 6. Capability & Invalid Account Route Boundaries
# ==============================================================================

class TestCapabilityFailure:
    def test_generic_and_unknown_routes_never_acquire_cloudcode_efforts(self):
        """OpenRouter routes and invalid account numbers (gemini-0, gemini-6, gemini-42)
        must never acquire Cloud Code capability semantics or invent effort ladders.
        """
        assert selectable_reasoning_efforts("openrouter", "google/gemini-3.8-flash") is None
        assert canonical_reasoning_base("openrouter", "google/gemini-3.8-flash") is None

        # Invalid account routes
        for bad_slug in ("gemini-0", "gemini-6", "gemini-42", "gemini-99"):
            assert selectable_reasoning_efforts(bad_slug, "gemini-3.8-flash") is None
            assert canonical_reasoning_base(bad_slug, "gemini-3.8-flash") is None

    def test_precedence_with_invalid_runtime_or_override_values(self):
        """Resolver precedence handles invalid/stale layers:
        1. Invalid runtime effort (3.8: 'ultra') -> falls back to config override 'medium'.
        2. Unsupported override (3.1: 'medium') with global 'low' -> falls back to global 'low'.
        3. Explicit disable override (3.1: false) with global 'low' -> resolves disabled.
        """
        # Case 1: Invalid runtime effort
        cfg1 = {"agent": {"reasoning_overrides": {"gemini-3.8-flash": "medium"}, "reasoning_effort": "low"}}
        r1 = resolve_effective_reasoning_config(config=cfg1, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base={"gemini-3.8-flash": "ultra"})
        assert r1 == {"enabled": True, "effort": "medium"}

        # Case 2: Unsupported config override
        cfg2 = {"agent": {"reasoning_overrides": {"gemini-3.1-pro": "medium"}, "reasoning_effort": "low"}}
        r2 = resolve_effective_reasoning_config(config=cfg2, provider="gemini-oauth", model="gemini-3.1-pro", effort_by_base={})
        assert r2 == {"enabled": True, "effort": "low"}

        # Case 3: Explicit disable override wins over global
        cfg3 = {"agent": {"reasoning_overrides": {"gemini-3.1-pro": "none"}, "reasoning_effort": "low"}}
        r3 = resolve_effective_reasoning_config(config=cfg3, provider="gemini-oauth", model="gemini-3.1-pro", effort_by_base={})
        assert r3 == {"enabled": False}

    def test_fallback_resolver_config_read_failure_resilience(self):
        """_reresolve_fallback_reasoning_config catches load_config() exceptions,
        keeping the existing reasoning_config without crashing.
        """
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.reasoning_config = {"enabled": True, "effort": "high"}
        agent.effort_by_base = {"gemini-3.8-flash": "high"}

        with patch("hermes_cli.config.load_config", side_effect=IOError("Corrupt config.yaml")):
            # Must not raise!
            _reresolve_fallback_reasoning_config(agent)

        # Invariant: Keeps current reasoning_config rather than crashing!
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert agent.effort_by_base == {"gemini-3.8-flash": "high"}
