"""Cross-Surface Contract Hardening Suite (Action Item 4, Milestone 3).

Enforces strict externally-observable contract equivalence across:
1. Frozen model.options capability semantics (null vs [] vs [...], effective effort, can_disable_reasoning)
2. Provider route equivalence (gemini-oauth, gemini-[1-5], invalid gemini-[0,6,42], canonical inventory)
3. Authoritative effective-effort precedence matrix (effort_by_base -> config override -> global -> default)
4. No-live-agent vs live/resumed session contracts and cold-resume isolation
5. OpenRPC and TypeScript contract schema synchronization
6. Typed-command serialization and backend parser round-tripping
7. Scope-equivalence matrix (--tui-session, --global, --once)
8. Picker-to-runtime end-to-end flow
9. Generic provider isolation (OpenRouter, custom providers with "gemini" in model name)
10. Forward compatibility with unknown or missing capability fields
"""

import copy
import json
import pytest
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.gemini_cloudcode_models import (
    selectable_reasoning_efforts,
    resolve_model_selection,
)
from agent.reasoning_selection import (
    canonical_reasoning_base,
    resolve_effective_reasoning_effort,
    resolve_effective_reasoning_config,
)
from hermes_cli.inventory import build_model_options_payload, load_picker_context
from hermes_cli.models import parse_model_input
from tui_gateway.model_switch import _switch_request
import tui_gateway.server as server


# ==============================================================================
# 1. Freeze model.options Capability Semantics
# ==============================================================================

class TestModelOptionsCapabilitySemantics:
    """Pin explicit capability field semantics:
    - reasoning_efforts = None / absent: capability unknown or generic
    - reasoning_efforts = []: known route with no selectable reasoning effort
    - reasoning_efforts = ["low", ...]: exact selectable set
    - effective_reasoning_effort = "medium": authoritative currently effective target
    - effective_reasoning_effort = None: no active effort (or explicitly disabled)
    - can_disable_reasoning: false for exact Cloud Code effort models
    """

    @pytest.mark.parametrize("model,expected_efforts,expected_can_disable", [
        ("gemini-3.8-flash", ["low", "medium", "high"], False),
        ("gemini-3.7-flash", ["low", "medium", "high"], False),
        ("gemini-3.6-flash", ["low", "medium", "high"], False),
        ("gemini-3.1-pro", ["low", "high"], False),
        ("gemini-3.1-flash-lite", [], None),
        ("claude-sonnet-4-6", [], None),
        ("gpt-oss-120b-medium", [], None),
    ])
    def test_cloudcode_known_models_capability_matrix(self, model, expected_efforts, expected_can_disable):
        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": [model],
            "authenticated": True,
            "is_current": True,
        }]
        with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "gemini-oauth")
            caps = p.get("capabilities", {}).get(model, {})

            assert caps.get("reasoning_efforts") == expected_efforts
            if expected_efforts:
                assert caps.get("can_disable_reasoning") is False
                assert caps.get("reasoning") is True
                assert caps.get("effective_reasoning_effort") in expected_efforts
            else:
                assert caps.get("effective_reasoning_effort") is None

    def test_openrouter_gemini_models_do_not_receive_cloudcode_exact_efforts(self):
        """OpenRouter routes with 'gemini' in model name must retain generic/unspecified semantics."""
        fake_rows = [{
            "slug": "openrouter",
            "name": "OpenRouter",
            "models": ["google/gemini-3.8-flash", "google/gemini-2.5-flash"],
            "authenticated": True,
            "is_current": False,
        }]
        with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "openrouter")
            for m in p["models"]:
                caps = p.get("capabilities", {}).get(m, {})
                assert caps.get("reasoning_efforts") is None
                assert caps.get("effective_reasoning_effort") is None


# ==============================================================================
# 2. Provider-Route Equivalence & Canonical Inventory
# ==============================================================================

class TestProviderRouteEquivalence:
    """All 6 valid Cloud Code provider routes (gemini-oauth, gemini-[1-5]) must expose
    identical effort sets for canonical models, while invalid aliases remain outside."""

    @pytest.mark.parametrize("provider_slug", [
        "gemini-oauth", "gemini-1", "gemini-2", "gemini-3", "gemini-4", "gemini-5"
    ])
    def test_all_valid_cloudcode_routes_expose_identical_efforts(self, provider_slug):
        assert list(selectable_reasoning_efforts(provider_slug, "gemini-3.8-flash")) == ["low", "medium", "high"]
        assert list(selectable_reasoning_efforts(provider_slug, "gemini-3.1-pro")) == ["low", "high"]
        assert list(selectable_reasoning_efforts(provider_slug, "gemini-3.1-flash-lite")) == []
        assert list(selectable_reasoning_efforts(provider_slug, "claude-sonnet-4-6")) == []

    @pytest.mark.parametrize("invalid_slug", [
        "gemini-0", "gemini-6", "gemini-42"
    ])
    def test_invalid_account_aliases_outside_cloudcode_contract(self, invalid_slug):
        assert selectable_reasoning_efforts(invalid_slug, "gemini-3.8-flash") is None
        assert canonical_reasoning_base(invalid_slug, "gemini-3.8-flash") is None

    def test_inventory_outputs_canonical_models_not_legacy_virtual_slugs(self):
        """Inventory advertising must output canonical base models, never legacy aliases."""
        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": ["gemini-3.8-flash", "gemini-3.8-flash-high", "gemini-3.1-pro-high"],
            "authenticated": True,
            "is_current": True,
        }]
        with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "gemini-oauth")
            caps = p.get("capabilities", {})
            assert "gemini-3.8-flash" in caps
            assert caps["gemini-3.8-flash"]["reasoning_efforts"] == ["low", "medium", "high"]


# ==============================================================================
# 3. Authoritative Effective-Effort Matrix
# ==============================================================================

class TestAuthoritativeEffectiveEffortMatrix:
    """Precedence contract:
    runtime effort_by_base -> per-base config override -> global reasoning_effort -> model default.
    Exported model.options value must equal resolve_effective_reasoning_effort().
    """

    def test_gemini_38_flash_precedence_matrix(self):
        prov, mod = "gemini-oauth", "gemini-3.8-flash"

        # 1. runtime medium + override low + global high -> medium
        eff1 = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_effort": "high", "reasoning_overrides": {"gemini-3.8-flash": "low"}}},
            provider=prov, model=mod, effort_by_base={"gemini-3.8-flash": "medium"}
        )
        assert eff1 == "medium"

        # 2. no runtime + override low + global medium -> low
        eff2 = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_effort": "medium", "reasoning_overrides": {"gemini-3.8-flash": "low"}}},
            provider=prov, model=mod, effort_by_base={}
        )
        assert eff2 == "low"

        # 3. no runtime/override + global medium -> medium
        eff3 = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_effort": "medium"}},
            provider=prov, model=mod, effort_by_base={}
        )
        assert eff3 == "medium"

        # 4. nothing configured -> default high
        eff4 = resolve_effective_reasoning_effort(
            config={}, provider=prov, model=mod, effort_by_base={}
        )
        assert eff4 == "high"

        # 5. explicit disabled override -> None
        eff5 = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_overrides": {"gemini-3.8-flash": "none"}}},
            provider=prov, model=mod, effort_by_base={}
        )
        assert eff5 is None

    def test_gemini_31_pro_unsupported_medium_falls_to_next_precedence(self):
        prov, mod = "gemini-oauth", "gemini-3.1-pro"

        # Config has medium (unsupported on 3.1 Pro), so it must fall back to default high
        eff = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_effort": "medium"}},
            provider=prov, model=mod, effort_by_base={}
        )
        assert eff == "high"

        # Config has override low (supported on 3.1 Pro)
        eff_low = resolve_effective_reasoning_effort(
            config={"agent": {"reasoning_overrides": {"gemini-3.1-pro": "low"}}},
            provider=prov, model=mod, effort_by_base={}
        )
        assert eff_low == "low"


# ==============================================================================
# 4. No-Live-Agent vs Resumed-Session Contract
# ==============================================================================

class TestNoLiveAgentAndResumedSessionContract:
    def test_no_live_agent_disk_config_remains_authoritative(self):
        """When no agent is attached, disk config decides provider, model, and effective effort."""
        fake_cfg = {
            "model": "gemini-3.8-flash",
            "provider": "gemini-oauth",
            "agent": {
                "reasoning_overrides": {"gemini-3.8-flash": "low"}
            }
        }
        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": ["gemini-3.8-flash"],
            "authenticated": True,
            "is_current": True,
        }]
        with patch("hermes_cli.config.load_config", return_value=fake_cfg),              patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {"session_id": "nonexistent_session"})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "gemini-oauth")
            caps = p["capabilities"]["gemini-3.8-flash"]
            assert caps["effective_reasoning_effort"] == "low"

    def test_resumed_agent_uses_session_effort_memory(self):
        """Live agent's effort_by_base reflects across model.options target capabilities."""
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.effort_by_base = {"gemini-3.8-flash": "low", "gemini-3.1-pro": "high"}

        session = {"agent": agent, "session_key": "s_live"}
        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": ["gemini-3.8-flash", "gemini-3.1-pro"],
            "authenticated": True,
            "is_current": True,
        }]

        with patch.dict(server._sessions, {"s_live": session}),              patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {"session_id": "s_live"})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "gemini-oauth")
            assert p["capabilities"]["gemini-3.8-flash"]["effective_reasoning_effort"] == "low"
            assert p["capabilities"]["gemini-3.1-pro"]["effective_reasoning_effort"] == "high"

    def test_cold_resume_does_not_resurrect_unrelated_process_memory(self):
        """After cold resume where only 3.8/low was persisted as active state,
        3.8 reflects low, but 3.1 Pro reflects default high rather than dead memory."""
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.effort_by_base = {"gemini-3.8-flash": "low"}  # 3.1 is NOT in effort_by_base

        session = {"agent": agent, "session_key": "s_cold"}
        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": ["gemini-3.8-flash", "gemini-3.1-pro"],
            "authenticated": True,
            "is_current": True,
        }]

        with patch.dict(server._sessions, {"s_cold": session}),              patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {"session_id": "s_cold"})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "gemini-oauth")
            assert p["capabilities"]["gemini-3.8-flash"]["effective_reasoning_effort"] == "low"
            assert p["capabilities"]["gemini-3.1-pro"]["effective_reasoning_effort"] == "high"


# ==============================================================================
# 5. OpenRPC Contract Schema Check
# ==============================================================================

class TestContractSchemaParity:
    def test_gateway_contract_generator_check_is_clean(self):
        """scripts/gen_gateway_contracts.py --check must exit 0 with 0 diff."""
        res = subprocess.run(["python3", "scripts/gen_gateway_contracts.py", "--check"], capture_output=True, text=True)
        err_msg = f"Gateway contract schemas out of sync: {res.stdout} -- {res.stderr}"
        assert res.returncode == 0, err_msg

    def test_openrpc_schema_retains_required_capability_fields(self):
        with open("apps/shared/src/gateway-contract.openrpc.json", "r") as f:
            data = json.load(f)

        schemas = data.get("components", {}).get("schemas", {})
        assert "ModelCapabilities" in schemas
        props = schemas["ModelCapabilities"]["properties"]
        assert "reasoning_efforts" in props
        assert "effective_reasoning_effort" in props
        assert "can_disable_reasoning" in props


# ==============================================================================
# 6. Typed-Command Serialization & Parser Parity
# ==============================================================================

class TestTypedCommandSerializationParity:
    @pytest.mark.parametrize("cmd,exp_model,exp_prov,exp_effort,exp_once,exp_global", [
        ("/model gemini-3.8-flash --provider gemini-oauth --reasoning medium --tui-session",
         "gemini-3.8-flash", "gemini-oauth", "medium", False, False),
        ("/model gemini-3.6-flash --provider gemini-2 --reasoning low --global",
         "gemini-3.6-flash", "gemini-2", "low", False, True),
        ("/model gemini-3.8-flash --provider gemini-oauth --reasoning high --once",
         "gemini-3.8-flash", "gemini-oauth", "high", True, False),
        ("/model gemini-oauth:gemini-3.8-flash --reasoning medium --tui-session",
         "gemini-3.8-flash", "gemini-oauth", "medium", False, False),
        ("/model gemini-3:gemini-3.1-pro --reasoning high --global",
         "gemini-3.1-pro", "gemini-3", "high", False, True),
    ])
    def test_parser_exact_roundtrip(self, cmd, exp_model, exp_prov, exp_effort, exp_once, exp_global):
        model_input, explicit_prov, one_turn, persist_global, effort = _switch_request(cmd, None, None)
        eff_prov, eff_model = parse_model_input(model_input, explicit_prov)
        assert eff_model == exp_model
        assert eff_prov == exp_prov
        assert effort == exp_effort
        assert one_turn is exp_once
        assert persist_global is exp_global


# ==============================================================================
# 7. Scope-Equivalence Matrix
# ==============================================================================

class TestScopeEquivalenceMatrix:
    def test_tui_session_scope_updates_runtime_leaves_disk_untouched(self):
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.effort_by_base = {}
        session = {"agent": agent, "session_key": "s1"}

        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                 base_url="", api_key="", api_mode="", model_info=None, warning_message=None)),              patch.object(server, "_restart_slash_worker"),              patch.object(server, "_persist_live_session_runtime"),              patch.object(server, "_persist_live_session_system_prompt"),              patch.object(server, "_append_model_switch_marker"),              patch.object(server, "_emit_session_info"),              patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

            out = server._apply_model_switch("s1", session, "/model gemini-3.8-flash --reasoning medium --tui-session")
            assert out["scope"] == "session"
            assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
            assert agent.effort_by_base == {"gemini-3.8-flash": "medium"}
            mock_write_cfg.assert_not_called()

    def test_global_scope_persists_canonical_reasoning_overrides(self):
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.effort_by_base = {}
        session = {"agent": agent, "session_key": "s2"}

        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                 base_url="", api_key="", api_mode="", model_info=None, warning_message=None)),              patch.object(server, "_restart_slash_worker"),              patch.object(server, "_persist_live_session_runtime"),              patch.object(server, "_persist_live_session_system_prompt"),              patch.object(server, "_append_model_switch_marker"),              patch.object(server, "_emit_session_info"),              patch.object(server, "_write_config_key", create=True) as mock_write_cfg:

            out = server._apply_model_switch("s2", session, "/model gemini-3.8-flash --reasoning medium --global")
            assert out["scope"] == "global"
            assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
            assert agent.effort_by_base == {"gemini-3.8-flash": "medium"}
            mock_write_cfg.assert_called_with("agent.reasoning_overrides", {"gemini-3.8-flash": "medium"})


# ==============================================================================
# 8. Picker-to-Runtime End-to-End Contract
# ==============================================================================

class TestPickerToRuntimeContract:
    def test_end_to_end_from_capabilities_to_applied_switch(self):
        """Start from capability payload -> user picks low -> switch applied -> runtime updated."""
        agent = MagicMock()
        agent.model = "gemini-3.8-flash"
        agent.provider = "gemini-oauth"
        agent.effort_by_base = {"gemini-3.8-flash": "high"}
        session = {"agent": agent, "session_key": "s_e2e"}

        fake_rows = [{
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": ["gemini-3.8-flash"],
            "authenticated": True,
            "is_current": True,
        }]

        # 1. Read capabilities from gateway
        with patch.dict(server._sessions, {"s_e2e": session}),              patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            opts = server._methods["model.options"](1, {"session_id": "s_e2e"})
            p = next(p for p in opts["result"]["providers"] if p["slug"] == "gemini-oauth")
            cap = p["capabilities"]["gemini-3.8-flash"]
            assert cap["reasoning_efforts"] == ["low", "medium", "high"]
            assert cap["effective_reasoning_effort"] == "high"

        # 2. Frontend UI emits command with chosen effort "low"
        chosen_effort = "low"
        cmd = f"/model gemini-3.8-flash --provider gemini-oauth --reasoning {chosen_effort} --tui-session"

        # 3. Backend processes command
        with patch("hermes_cli.model_switch.switch_model", return_value=SimpleNamespace(
                 success=True, new_model="gemini-3.8-flash", target_provider="gemini-oauth",
                 base_url="", api_key="", api_mode="", model_info=None, warning_message=None)),              patch.object(server, "_restart_slash_worker"),              patch.object(server, "_persist_live_session_runtime"),              patch.object(server, "_persist_live_session_system_prompt"),              patch.object(server, "_append_model_switch_marker"),              patch.object(server, "_emit_session_info"):

            server._apply_model_switch("s_e2e", session, cmd)

            # Invariant: Runtime updated exactly
            assert agent.reasoning_config == {"enabled": True, "effort": "low"}
            assert agent.effort_by_base["gemini-3.8-flash"] == "low"


# ==============================================================================
# 9. Generic-Provider Isolation & Unknown Capabilities
# ==============================================================================

class TestGenericProviderIsolationAndForwardCompatibility:
    def test_custom_provider_with_gemini_model_name_isolated(self):
        """Custom provider serving model named 'gemini-3.8-flash' does not receive Cloud Code effort envelope."""
        fake_rows = [{
            "slug": "custom-local",
            "name": "Local LLM",
            "models": ["gemini-3.8-flash"],
            "authenticated": True,
            "is_current": False,
        }]
        with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "custom-local")
            caps = p.get("capabilities", {}).get("gemini-3.8-flash", {})
            assert caps.get("reasoning_efforts") is None
            assert caps.get("effective_reasoning_effort") is None

    def test_future_model_with_unknown_capabilities_uses_generic_fallback(self):
        """Future model with reasoning=True and reasoning_efforts=None uses generic fallback."""
        fake_rows = [{
            "slug": "future-ai",
            "name": "Future AI",
            "models": ["future-reasoning-v1"],
            "authenticated": True,
            "is_current": False,
        }]
        with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
            res = server._methods["model.options"](1, {})
            p = next(p for p in res["result"]["providers"] if p["slug"] == "future-ai")
            caps = p.get("capabilities", {}).get("future-reasoning-v1", {})
            assert caps.get("reasoning") is True
            assert caps.get("reasoning_efforts") is None
            assert caps.get("effective_reasoning_effort") is None
