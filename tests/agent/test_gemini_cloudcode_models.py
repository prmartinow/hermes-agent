"""Tests for canonical Gemini Cloud Code model resolver and capability registry.

Covers:
- Canonical parsing of base models, legacy aliases, partner models, and unknown IDs.
- Prefix stripping restricted strictly to known Gemini provider namespaces.
- Capability envelope querying (efforts_for_base) and separation from upstream supportsThinking.
- Dynamic tier wire translation (Gemini 3.8 and 3.7 Flash -> -tiered with thinkingLevel).
- Static tier wire translation (Gemini 3.6 Flash and 3.1 Pro).
- Strict negative validation (unsupported efforts like max, minimal, xhigh, ultra).
- Legacy equivalence (resolving via alias produces identical ResolvedModel as base+effort).
- Contradiction detection between alias effort and explicit effort.
- Backward compatibility passthrough for non-aliased legacy strings (3.5 extra-low, 3.1 pro low-thinking).
- Registry closure and invariant verification across all capabilities.
"""

import pytest

from agent.gemini_cloudcode_models import (
    LEGACY_MODEL_ALIASES,
    _MODEL_CAPABILITIES,
    EffortRoute,
    EffortUnsupportedError,
    ModelCapability,
    ParsedModelSelection,
    ResolvedModel,
    efforts_for_base,
    get_model_capability,
    model_for_base_effort,
    parse_model_slug,
    resolve_model_selection,
)


# ============================================================================
# A. Canonical Parsing & Prefix Stripping Tests
# ============================================================================

def test_parse_model_slug_canonical_base():
    parsed = parse_model_slug("gemini-3.8-flash")
    assert parsed == ParsedModelSelection(base_model="gemini-3.8-flash", effort=None, legacy_alias=False)


def test_parse_model_slug_known_prefix_stripping():
    # Only known Gemini provider prefixes are stripped
    assert parse_model_slug("google/gemini-3.8-flash").base_model == "gemini-3.8-flash"
    assert parse_model_slug("gemini-oauth/gemini-3.8-flash").base_model == "gemini-3.8-flash"
    assert parse_model_slug("gemini_oauth/gemini-3.8-flash").base_model == "gemini-3.8-flash"
    assert parse_model_slug("gemini-1/gemini-3.8-flash").base_model == "gemini-3.8-flash"
    assert parse_model_slug("gemini-oauth-2/gemini-3.7-flash").base_model == "gemini-3.7-flash"


def test_parse_model_slug_preserves_custom_vendor_prefixes():
    # Slashes in non-Gemini vendor namespaces must be preserved verbatim
    assert parse_model_slug("vendor/custom-model").base_model == "vendor/custom-model"
    assert parse_model_slug("foo/bar/baz").base_model == "foo/bar/baz"
    assert parse_model_slug("openrouter/auto").base_model == "openrouter/auto"


@pytest.mark.parametrize(
    "alias, expected_base, expected_effort",
    [
        ("gemini-3.8-flash-high", "gemini-3.8-flash", "high"),
        ("gemini-3.8-flash-medium", "gemini-3.8-flash", "medium"),
        ("gemini-3.8-flash-low", "gemini-3.8-flash", "low"),
        ("gemini-3.8-flash-tiered", "gemini-3.8-flash", "high"),
        ("gemini-3.7-flash-high", "gemini-3.7-flash", "high"),
        ("gemini-3.7-flash-medium", "gemini-3.7-flash", "medium"),
        ("gemini-3.7-flash-low", "gemini-3.7-flash", "low"),
        ("gemini-3.6-flash-high", "gemini-3.6-flash", "high"),
        ("gemini-3.6-flash-medium", "gemini-3.6-flash", "medium"),
        ("gemini-3.6-flash-low", "gemini-3.6-flash", "low"),
        ("gemini-3.1-pro-high", "gemini-3.1-pro", "high"),
        ("gemini-3.1-pro-low", "gemini-3.1-pro", "low"),
    ],
)
def test_parse_model_slug_legacy_aliases(alias, expected_base, expected_effort):
    parsed = parse_model_slug(alias)
    assert parsed == ParsedModelSelection(base_model=expected_base, effort=expected_effort, legacy_alias=True)


def test_parse_model_slug_partner_and_unknown_models():
    assert parse_model_slug("claude-sonnet-4-6") == ParsedModelSelection(
        base_model="claude-sonnet-4-6", effort=None, legacy_alias=False
    )
    assert parse_model_slug("gpt-oss-120b-medium") == ParsedModelSelection(
        base_model="gpt-oss-120b-medium", effort=None, legacy_alias=False
    )
    assert parse_model_slug("unknown-model-high") == ParsedModelSelection(
        base_model="unknown-model-high", effort=None, legacy_alias=False
    )


# ============================================================================
# B. Capability Envelope & Upstream Thinking Tests
# ============================================================================

def test_efforts_for_base_capabilities():
    assert efforts_for_base("gemini-3.8-flash") == ("low", "medium", "high")
    assert efforts_for_base("gemini-3.7-flash") == ("low", "medium", "high")
    assert efforts_for_base("gemini-3.6-flash") == ("low", "medium", "high")
    assert efforts_for_base("gemini-3.5-flash") == ("low", "medium", "high")
    assert efforts_for_base("gemini-3.1-pro") == ("low", "high")
    assert efforts_for_base("gpt-oss-120b-medium") == ()
    assert efforts_for_base("gemini-3.1-flash-lite") == ()
    assert efforts_for_base("unregistered-future-model") == ()


def test_gpt_oss_supports_thinking_distinct_from_efforts():
    # gpt-oss-120b-medium has upstream supportsThinking=True, but zero selectable efforts
    cap = get_model_capability("gpt-oss-120b-medium")
    assert cap is not None
    assert cap.supports_thinking is True
    assert cap.efforts == ()


# ============================================================================
# C. Dynamic Tier Wire Resolution (Gemini 3.8 / 3.7 Flash)
# ============================================================================

@pytest.mark.parametrize("version", ["3.7", "3.8"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_dynamic_tier_resolution_wire_model_and_thinking(version, effort):
    base = f"gemini-{version}-flash"
    resolved = model_for_base_effort(base, effort)

    assert resolved.base_model == base
    assert resolved.effort == effort
    assert resolved.wire_model == f"gemini-{version}-flash-tiered"
    assert resolved.thinking_config == {
        "thinkingLevel": effort,
        "includeThoughts": True,
    }


def test_dynamic_tier_default_effort():
    resolved = model_for_base_effort("gemini-3.8-flash")
    assert resolved.effort == "high"
    assert resolved.wire_model == "gemini-3.8-flash-tiered"
    assert resolved.thinking_config == {"thinkingLevel": "high", "includeThoughts": True}


# ============================================================================
# D. Static Tier Wire Resolution (Gemini 3.6 Flash / 3.1 Pro)
# ============================================================================

@pytest.mark.parametrize(
    "effort, expected_wire",
    [
        ("low", "gemini-3.6-flash-low"),
        ("medium", "gemini-3.6-flash-medium"),
        ("high", "gemini-3.6-flash-high"),
    ],
)
def test_static_tier_gemini_36_resolution(effort, expected_wire):
    resolved = model_for_base_effort("gemini-3.6-flash", effort)
    assert resolved.base_model == "gemini-3.6-flash"
    assert resolved.effort == effort
    assert resolved.wire_model == expected_wire
    assert resolved.thinking_config is None


@pytest.mark.parametrize(
    "effort, expected_wire",
    [
        ("low", "gemini-3.1-pro-low"),
        ("high", "gemini-3.1-pro-high"),
    ],
)
def test_static_tier_gemini_31_pro_resolution(effort, expected_wire):
    resolved = model_for_base_effort("gemini-3.1-pro", effort)
    assert resolved.base_model == "gemini-3.1-pro"
    assert resolved.effort == effort
    assert resolved.wire_model == expected_wire
    assert resolved.thinking_config is None


# ============================================================================
# E. Strict Negative Tests (Unsupported Effort Validation)
# ============================================================================

@pytest.mark.parametrize("invalid_effort", ["max", "minimal", "xhigh", "ultra", "extra_high", "0"])
def test_gemini_38_flash_rejects_unsupported_efforts(invalid_effort):
    with pytest.raises(EffortUnsupportedError) as exc_info:
        model_for_base_effort("gemini-3.8-flash", invalid_effort)
    msg = str(exc_info.value)
    assert "gemini-3.8-flash has no" in msg
    assert invalid_effort in msg
    assert "available: low, medium, high" in msg


def test_gemini_31_pro_rejects_medium_effort():
    with pytest.raises(EffortUnsupportedError) as exc_info:
        model_for_base_effort("gemini-3.1-pro", "medium")
    msg = str(exc_info.value)
    assert "gemini-3.1-pro has no 'medium' effort" in msg
    assert "available: low, high" in msg


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_non_thinking_model_rejects_any_effort(effort):
    with pytest.raises(EffortUnsupportedError) as exc_info:
        model_for_base_effort("gpt-oss-120b-medium", effort)
    assert "gpt-oss-120b-medium has no" in str(exc_info.value)
    assert "available: none" in str(exc_info.value)


# ============================================================================
# F. Legacy Equivalence & Passthrough Tests
# ============================================================================

@pytest.mark.parametrize("alias", list(LEGACY_MODEL_ALIASES.keys()))
def test_legacy_alias_produces_identical_resolution_as_canonical(alias):
    base, eff = LEGACY_MODEL_ALIASES[alias]

    resolved_from_alias = resolve_model_selection(alias)
    resolved_from_canonical = resolve_model_selection(base, eff)

    assert resolved_from_alias.base_model == resolved_from_canonical.base_model
    assert resolved_from_alias.effort == resolved_from_canonical.effort
    assert resolved_from_alias.wire_model == resolved_from_canonical.wire_model
    assert resolved_from_alias.thinking_config == resolved_from_canonical.thinking_config


def test_non_aliased_legacy_strings_passthrough_verbatim():
    # Unverified legacy strings must pass through without mutation to preserve compatibility
    r1 = resolve_model_selection("gemini-3.5-flash-extra-low")
    assert r1.wire_model == "gemini-3.5-flash-extra-low"

    r2 = resolve_model_selection("gemini-3.1-pro-low-thinking")
    assert r2.wire_model == "gemini-3.1-pro-low-thinking"


# ============================================================================
# G. Contradiction Detection
# ============================================================================

def test_resolve_model_selection_rejects_contradictory_alias_and_effort():
    with pytest.raises(EffortUnsupportedError) as exc_info:
        resolve_model_selection("gemini-3.8-flash-low", effort="high")
    assert "Model alias 'gemini-3.8-flash-low' requests effort 'low', but explicit effort is 'high'" in str(exc_info.value)


def test_resolve_model_selection_accepts_matching_alias_and_effort():
    resolved = resolve_model_selection("gemini-3.8-flash-high", effort="high")
    assert resolved.wire_model == "gemini-3.8-flash-tiered"
    assert resolved.effort == "high"


# ============================================================================
# H. Registry Invariants & Closure Verification
# ============================================================================

def test_registry_invariants_across_all_capabilities():
    for base, cap in _MODEL_CAPABILITIES.items():
        assert cap.base_model == base
        for eff in cap.efforts:
            res = model_for_base_effort(base, eff)
            assert res.base_model == base
            assert res.effort == eff
            assert res.wire_model != ""

            if base in ("gemini-3.8-flash", "gemini-3.7-flash"):
                assert res.wire_model.endswith("-tiered")
                assert res.thinking_config is not None
                assert res.thinking_config["thinkingLevel"] == eff
                assert res.thinking_config["includeThoughts"] is True


def test_legacy_aliases_map_strictly_to_registered_capabilities():
    for alias, (base, eff) in LEGACY_MODEL_ALIASES.items():
        cap = get_model_capability(base)
        assert cap is not None, f"Legacy alias '{alias}' points to unregistered base '{base}'"
        assert eff in cap.efforts, f"Legacy alias '{alias}' specifies effort '{eff}' not supported by base '{base}'"


def test_model_for_base_effort_fails_closed_on_missing_route():
    # If a registered model has an effort without a route, fail closed
    broken_cap = ModelCapability(
        base_model="broken-model",
        display_name="Broken Model",
        efforts=("high",),
        routes={},  # missing route
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(_MODEL_CAPABILITIES, "broken-model", broken_cap)
        with pytest.raises(RuntimeError) as exc_info:
            model_for_base_effort("broken-model", "high")
        assert "Cloud Code model registry has no route for 'broken-model' effort 'high'" in str(exc_info.value)
# ============================================================================
# I. Milestone 2: Catalog Discovery Normalization Tests
# ============================================================================

from agent.gemini_cloudcode_models import DiscoveredModel, normalize_discovered_model


def test_normalize_discovered_model_dynamic_tiered():
    meta = {"supportsThinking": True, "thinkingBudget": -1}
    res_38 = normalize_discovered_model("gemini-3.8-flash-tiered", meta)
    assert res_38 == DiscoveredModel(
        raw_model="gemini-3.8-flash-tiered",
        base_model="gemini-3.8-flash",
        available_efforts=("low", "medium", "high"),
        canonicalized=True,
    )

    res_37 = normalize_discovered_model("gemini-3.7-flash-tiered", meta)
    assert res_37 == DiscoveredModel(
        raw_model="gemini-3.7-flash-tiered",
        base_model="gemini-3.7-flash",
        available_efforts=("low", "medium", "high"),
        canonicalized=True,
    )


@pytest.mark.parametrize(
    "model_id, expected_base, expected_eff",
    [
        ("gemini-3.6-flash-high", "gemini-3.6-flash", "high"),
        ("gemini-3.6-flash-medium", "gemini-3.6-flash", "medium"),
        ("gemini-3.6-flash-low", "gemini-3.6-flash", "low"),
        ("gemini-3.1-pro-high", "gemini-3.1-pro", "high"),
        ("gemini-3.1-pro-low", "gemini-3.1-pro", "low"),
    ],
)
def test_normalize_discovered_model_static_tiers(model_id, expected_base, expected_eff):
    res = normalize_discovered_model(model_id, {})
    assert res == DiscoveredModel(
        raw_model=model_id,
        base_model=expected_base,
        available_efforts=(expected_eff,),
        canonicalized=True,
    )


def test_normalize_discovered_model_passthroughs_and_future_models():
    # Known partner models remain uncanonicalized
    res_claude = normalize_discovered_model("claude-sonnet-4-6", {})
    assert res_claude == DiscoveredModel(
        raw_model="claude-sonnet-4-6",
        base_model="claude-sonnet-4-6",
        available_efforts=(),
        canonicalized=False,
    )

    res_gpt = normalize_discovered_model("gpt-oss-120b-medium", {"supportsThinking": True})
    assert res_gpt == DiscoveredModel(
        raw_model="gpt-oss-120b-medium",
        base_model="gpt-oss-120b-medium",
        available_efforts=(),
        canonicalized=False,
    )

    # Unverified legacy strings pass through verbatim
    res_35 = normalize_discovered_model("gemini-3.5-flash-extra-low", {})
    assert res_35 == DiscoveredModel(
        raw_model="gemini-3.5-flash-extra-low",
        base_model="gemini-3.5-flash-extra-low",
        available_efforts=(),
        canonicalized=False,
    )

    # Vendor models with slashes pass through verbatim
    res_vendor = normalize_discovered_model("vendor/model-high", {})
    assert res_vendor == DiscoveredModel(
        raw_model="vendor/model-high",
        base_model="vendor/model-high",
        available_efforts=(),
        canonicalized=False,
    )

    # Fail-closed future-tiered model: must NOT strip -tiered if base is not in capability registry
    res_future = normalize_discovered_model("gemini-4.2-ultra-tiered", {"supportsThinking": True, "thinkingBudget": -1})
    assert res_future == DiscoveredModel(
        raw_model="gemini-4.2-ultra-tiered",
        base_model="gemini-4.2-ultra-tiered",
        available_efforts=(),
        canonicalized=False,
    )
def test_dynamic_tier_requires_verified_metadata():
    # If Google returns gemini-3.8-flash-tiered with unverified metadata, do not canonicalize
    result = normalize_discovered_model(
        "gemini-3.8-flash-tiered",
        {"supportsThinking": False, "thinkingBudget": 0},
    )
    assert result.base_model == "gemini-3.8-flash-tiered"
    assert result.available_efforts == ()
    assert result.canonicalized is False


def test_shorthand_aliases_not_canonicalized_in_discovery():
    # Suffix/user shorthands are not wire models and must pass through unchanged if in catalog
    assert normalize_discovered_model("gemini-3.8", {}).base_model == "gemini-3.8"
    assert normalize_discovered_model("gemini-3.8-thinking", {}).base_model == "gemini-3.8-thinking"
    assert normalize_discovered_model("gemini-3.6", {}).base_model == "gemini-3.6"


def test_bare_registered_model_reports_empty_efforts_unless_wire_established():
    # A bare base model reported by upstream does not establish effort capabilities by itself
    result = normalize_discovered_model("gemini-3.8-flash", {})
    assert result.base_model == "gemini-3.8-flash"
    assert result.available_efforts == ()
    assert result.canonicalized is False
# ============================================================================
# J. Milestone 4: Fallback Catalog Invariants & Wire Route Uniqueness
# ============================================================================

def test_gemini_oauth_fallback_models_are_canonical_bases():
    from providers import get_provider_profile
    profile = get_provider_profile("gemini-oauth")
    assert profile is not None
    # All fallback models must be canonical base identities, never legacy aliases
    for model in profile.fallback_models:
        assert not parse_model_slug(model).legacy_alias, f"Fallback model '{model}' is a legacy alias!"

    assert "gemini-3.8-flash" in profile.fallback_models
    assert "gemini-3.7-flash" in profile.fallback_models
    assert "gemini-3.6-flash" in profile.fallback_models
    assert "gemini-3.1-pro" in profile.fallback_models

    for virtual_suffix in ("-high", "-medium", "-low", "-tiered"):
        for base in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.1-pro"):
            assert f"{base}{virtual_suffix}" not in profile.fallback_models


def test_static_wire_model_routes_uniqueness():
    from agent.gemini_cloudcode_models import _STATIC_WIRE_MODEL_ROUTES
    seen = {}
    for base, cap in _MODEL_CAPABILITIES.items():
        if base in ("gemini-3.8-flash", "gemini-3.7-flash"):
            continue
        for effort, route in cap.routes.items():
            if route.wire_model == base or not effort:
                continue
            if route.wire_model in seen:
                assert seen[route.wire_model] == (base, effort), f"Collision on static wire model {route.wire_model}"
            seen[route.wire_model] = (base, effort)
    assert seen == _STATIC_WIRE_MODEL_ROUTES
# ============================================================================
# K. Action Item 2, Milestone 3: Thought Circulation Capability Matrix
# ============================================================================

from agent.gemini_cloudcode_models import thought_circulation_support


def test_thought_circulation_support_verified_models():
    # Verified True
    assert thought_circulation_support("gemini-3.8-flash") is True
    assert thought_circulation_support("gemini-3.7-flash") is True
    assert thought_circulation_support("gemini-3.6-flash") is True
    assert thought_circulation_support("gemini-3.1-pro") is True

    # Verified False for Cloud Code partner models that reject Google signatures
    assert thought_circulation_support("claude-sonnet-4-6") is False
    assert thought_circulation_support("claude-opus-4-6-thinking") is False
    assert thought_circulation_support("gpt-oss-120b-medium") is False

    # Unverified / Unknown: None (never inferred from supports_thinking)
    assert thought_circulation_support("gemini-3.5-flash") is None
    assert thought_circulation_support("gemini-3.1-flash-lite") is None
    assert thought_circulation_support("gemini-3-flash-agent") is None
    assert thought_circulation_support("gemini-pro-agent") is None
    assert thought_circulation_support("gemini-4.2-flash") is None
    assert thought_circulation_support("unknown-model-xyz") is None


def test_thought_circulation_support_aliases_and_prefixes():
    # Legacy virtual aliases resolve to base capability
    assert thought_circulation_support("gemini-3.8-flash-high") is True
    assert thought_circulation_support("gemini-3.6-flash-low") is True

    # Standard Gemini vendor prefixes stripped cleanly
    assert thought_circulation_support("google/gemini-3.8-flash") is True
    assert thought_circulation_support("gemini/gemini-3.8-flash") is True
    assert thought_circulation_support("gemini-oauth/gemini-3.8-flash") is True

    # Unrelated vendor prefixes preserved without false positive matching
    assert thought_circulation_support("acme/gemini-3.8-flash") is None
    assert thought_circulation_support("my-gemini-proxy") is None
