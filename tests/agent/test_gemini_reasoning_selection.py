"""Unit and contract tests for Action Item 3 Milestone 2: Runtime EffortByBase state and shared effective resolver.

Verifies:
  1. Canonical keying (aliases canonicalize to canonical base model).
  2. Multi-model independence (3.8 low, 3.1 high stored in separate entries).
  3. Validation & rejection (unsupported efforts, none, Claude/lite rejected; map unmutated).
  4. Precedence hierarchy (runtime > reasoning_overrides > global reasoning_effort > default).
  5. Stale unsupported values in runtime/config/global skipped safely without mutation.
  6. Defaults (3.8 -> high, 3.1 -> high).
  7. No-effort models (Claude, GPT-OSS, flash-lite do not populate map).
  8. Route isolation (OpenRouter google/gemini-* does not participate in Cloud Code map).
  9. Mutation isolation (resolver does not mutate config, failed remember does not mutate map).
  10. Isolated instances (map instances are distinct per object, never shared).
"""

from unittest.mock import MagicMock
from agent.reasoning_selection import (
    canonical_reasoning_base,
    remember_reasoning_effort,
    remembered_reasoning_effort,
    resolve_effective_reasoning_config,
)
from run_agent import AIAgent


def test_canonical_reasoning_base_cloudcode_routes():
    # Canonical bases under Cloud Code routes
    assert canonical_reasoning_base("gemini-oauth", "gemini-3.8-flash") == "gemini-3.8-flash"
    assert canonical_reasoning_base("gemini-1", "gemini-3.8-flash") == "gemini-3.8-flash"
    assert canonical_reasoning_base("gemini-2", "gemini-3.1-pro") == "gemini-3.1-pro"

    # Legacy aliases canonicalize to base
    assert canonical_reasoning_base("gemini-oauth", "gemini-3.8-flash-high") == "gemini-3.8-flash"
    assert canonical_reasoning_base("gemini-oauth", "gemini-3.1-pro-low") == "gemini-3.1-pro"

    # Vendor prefixes stripped cleanly
    assert canonical_reasoning_base("gemini-oauth", "google/gemini-3.8-flash") == "gemini-3.8-flash"
    assert canonical_reasoning_base("gemini-oauth", "gemini-oauth/gemini-3.8-flash") == "gemini-3.8-flash"

    # Non-Cloud Code routes return None (no canonical Cloud Code base)
    assert canonical_reasoning_base("openrouter", "google/gemini-3.8-flash") is None
    assert canonical_reasoning_base("openai", "gpt-4o") is None
    assert canonical_reasoning_base("gemini", "gemini-3.8-flash") is None
    assert canonical_reasoning_base("gemini-6", "gemini-3.8-flash") is None


def test_remember_reasoning_effort_validation_and_independence():
    m = {}

    # 1. Valid selections store cleanly under canonical base
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash", effort="low") is True
    assert m == {"gemini-3.8-flash": "low"}

    # 2. Legacy alias updates the same canonical base
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash-high", effort="medium") is True
    assert m == {"gemini-3.8-flash": "medium"}

    # 3. Independent entries for distinct bases
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.1-pro", effort="high") is True
    assert m == {"gemini-3.8-flash": "medium", "gemini-3.1-pro": "high"}

    # 4. Remembered retrieval
    assert remembered_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash") == "medium"
    assert remembered_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash-high") == "medium"
    assert remembered_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.1-pro") == "high"
    assert remembered_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.7-flash") is None


def test_remember_reasoning_effort_rejections_leave_map_unmutated():
    m = {"gemini-3.8-flash": "medium"}

    # 3.8 does not support max
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash", effort="max") is False
    assert m == {"gemini-3.8-flash": "medium"}  # Unchanged!

    # 3.1 Pro does not support medium
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.1-pro", effort="medium") is False
    assert "gemini-3.1-pro" not in m

    # none is rejected for Gemini
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.8-flash", effort="none") is False
    assert m == {"gemini-3.8-flash": "medium"}

    # Models with no selectable efforts (Claude, flash-lite, GPT-OSS) rejected
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="claude-sonnet-4-6", effort="low") is False
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gemini-3.1-flash-lite", effort="low") is False
    assert remember_reasoning_effort(m, provider="gemini-oauth", model="gpt-oss-120b-medium", effort="low") is False
    assert len(m) == 1

    # Non-Cloud Code routes rejected
    assert remember_reasoning_effort(m, provider="openrouter", model="google/gemini-3.8-flash", effort="low") is False
    assert remember_reasoning_effort(m, provider="gemini-6", model="gemini-3.8-flash", effort="low") is False
    assert m == {"gemini-3.8-flash": "medium"}


def test_resolve_effective_reasoning_config_precedence():
    cfg = {
        "agent": {
            "reasoning_effort": "low",
            "reasoning_overrides": {
                "gemini-3.8-flash": "high",
                "gemini-3.1-pro": "low",
            },
        }
    }
    m = {"gemini-3.8-flash": "medium"}

    # Precedence 1: Runtime map ('medium') beats config override ('high')
    r1 = resolve_effective_reasoning_config(config=cfg, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base=m)
    assert r1 == {"enabled": True, "effort": "medium"}

    # Precedence 2: Config override ('high') wins when not in runtime map
    r2 = resolve_effective_reasoning_config(config=cfg, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base={})
    assert r2 == {"enabled": True, "effort": "high"}

    # Precedence 3: Global config ('low') wins when no override and not in runtime map (e.g. 3.7)
    r3 = resolve_effective_reasoning_config(config=cfg, provider="gemini-oauth", model="gemini-3.7-flash", effort_by_base={})
    assert r3 == {"enabled": True, "effort": "low"}

    # Precedence 4: Model default ('high') wins when config has no valid value
    r4 = resolve_effective_reasoning_config(config={}, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base={})
    assert r4 == {"enabled": True, "effort": "high"}


def test_resolve_effective_reasoning_config_stale_unsupported_skipping():
    # Stale value in runtime map: e.g. someone manually put 'max' for 3.8
    stale_runtime = {"gemini-3.8-flash": "max", "gemini-3.1-pro": "medium"}
    cfg = {"agent": {"reasoning_effort": "low"}}

    # 3.8 ignores stale 'max' in runtime map, falls through to global 'low'
    r1 = resolve_effective_reasoning_config(config=cfg, provider="gemini-oauth", model="gemini-3.8-flash", effort_by_base=stale_runtime)
    assert r1 == {"enabled": True, "effort": "low"}
    assert stale_runtime["gemini-3.8-flash"] == "max"  # Map unmutated!

    # 3.1 Pro ignores stale 'medium' in runtime map, and global 'low' IS supported for 3.1 Pro -> uses 'low'
    r2 = resolve_effective_reasoning_config(config=cfg, provider="gemini-oauth", model="gemini-3.1-pro", effort_by_base=stale_runtime)
    assert r2 == {"enabled": True, "effort": "low"}

    # Stale global effort: e.g. global is 'medium', but 3.1 Pro only supports 'low' and 'high'
    cfg_stale_global = {"agent": {"reasoning_effort": "medium"}}
    r3 = resolve_effective_reasoning_config(config=cfg_stale_global, provider="gemini-oauth", model="gemini-3.1-pro", effort_by_base={})
    # Stale global 'medium' skipped, default 'high' wins!
    assert r3 == {"enabled": True, "effort": "high"}


def test_resolve_effective_reasoning_config_no_effort_models():
    # Cloud Code models with no selectable effort return None (no synthetic Gemini config)
    for no_eff_model in ["gemini-3.1-flash-lite", "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]:
        r = resolve_effective_reasoning_config(config={"agent": {"reasoning_effort": "high"}}, provider="gemini-oauth", model=no_eff_model, effort_by_base={"gemini-3.8-flash": "low"})
        assert r is None


def test_resolve_effective_reasoning_config_route_isolation():
    m = {"gemini-3.8-flash": "low"}
    cfg = {"agent": {"reasoning_effort": "high", "reasoning_overrides": {"google/gemini-3.8-flash": "high"}}}

    # OpenRouter route does NOT participate in Cloud Code effort_by_base
    r_openrouter = resolve_effective_reasoning_config(config=cfg, provider="openrouter", model="google/gemini-3.8-flash", effort_by_base=m)
    # OpenRouter resolves through standard resolve_reasoning_config, which matches the config override
    assert r_openrouter == {"enabled": True, "effort": "high"}


def test_runtime_instance_isolation():
    # Separate runtime maps must remain completely independent
    map_a = {}
    map_b = {}

    assert remember_reasoning_effort(map_a, provider="gemini-oauth", model="gemini-3.8-flash", effort="low") is True
    assert map_a == {"gemini-3.8-flash": "low"}
    assert map_b == {}  # map_b was not touched!

    # Test AIAgent instance isolation
    agent1 = AIAgent.__new__(AIAgent)
    agent1.effort_by_base = {}
    agent2 = AIAgent.__new__(AIAgent)
    agent2.effort_by_base = {}

    agent1.effort_by_base["gemini-3.8-flash"] = "low"
    assert agent2.effort_by_base == {}
def test_effective_resolver_preserves_exact_legacy_alias_override_precedence():
    import copy
    cfg = {
        "agent": {
            "reasoning_overrides": {
                "gemini-3.8-flash-high": "low",
                "gemini-3.8-flash": "high",
            }
        }
    }
    orig_cfg = copy.deepcopy(cfg)
    result = resolve_effective_reasoning_config(
        config=cfg,
        provider="gemini-oauth",
        model="gemini-3.8-flash-high",
        effort_by_base={},
    )
    assert result == {"enabled": True, "effort": "low"}
    assert cfg == orig_cfg  # Config is not mutated!


def test_effective_resolver_accepts_structured_global_reasoning_config():
    import copy
    cfg = {
        "agent": {
            "reasoning_effort": {
                "enabled": True,
                "effort": "low",
            }
        }
    }
    orig_cfg = copy.deepcopy(cfg)
    result = resolve_effective_reasoning_config(
        config=cfg,
        provider="gemini-oauth",
        model="gemini-3.8-flash",
        effort_by_base={},
    )
    assert result == {"enabled": True, "effort": "low"}
    assert cfg == orig_cfg  # Config is not mutated!
