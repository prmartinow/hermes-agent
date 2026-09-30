"""Documentation Drift Guard Suite (Action Item 4, Milestone 4).

Validates that critical architectural tables and constants documented in
website/docs/developer-guide/gemini-cloud-code-runtime.md remain strictly synchronized
with authoritative production registries in agent/gemini_cloudcode_models.py and hermes_cli.
"""

from pathlib import Path
import pytest

from agent.gemini_cloudcode_models import (
    resolve_model_selection,
    selectable_reasoning_efforts,
    efforts_for_base,
    get_model_capability,
    _CLOUDCODE_EFFORT_PROVIDERS,
    _CLOUDCODE_ACCOUNT_PROVIDERS,
)


DOCUMENTED_EFFORT_TABLE = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("low", "medium", "high"),
    "gemini-3.1-pro": ("low", "high"),
    "gemini-3.1-flash-lite": (),
    "claude-sonnet-4-6": (),
    "gpt-oss-120b-medium": (),
}

DOCUMENTED_WIRE_MODELS = {
    "gemini-3.8-flash": "gemini-3.8-flash-tiered",
    "gemini-3.7-flash": "gemini-3.7-flash-tiered",
}

DOCUMENTED_ACCOUNT_ROUTES = {
    "gemini-oauth",
    "gemini-1",
    "gemini-2",
    "gemini-3",
    "gemini-4",
    "gemini-5",
}


def test_documentation_file_exists_and_references_authoritative_sections():
    """Verify that the authoritative runtime architecture document exists and has required sections."""
    doc_path = Path("website/docs/developer-guide/gemini-cloud-code-runtime.md")
    assert doc_path.exists(), "Authoritative documentation file missing"
    content = doc_path.read_text(encoding="utf-8")

    assert "# Google Gemini Cloud Code PA (`gemini-oauth`) Runtime Architecture" in content
    assert "Architectural Ownership Map" in content
    assert "Logical Models vs. Wire Models Truth Table" in content
    assert "Five-Layer Reasoning Precedence & Lifetime Model" in content
    assert "Thought-Signature Provenance & History Circulation" in content
    assert "Switching & Rollback Transaction Contract" in content
    assert "Surface Equivalence & Scope Semantics" in content
    assert "Empirical Upstream Caveats" in content


def test_documented_selectable_efforts_match_production_registry():
    """Ensure documented selectable effort tuples exactly match selectable_reasoning_efforts()."""
    for model, expected_efforts in DOCUMENTED_EFFORT_TABLE.items():
        actual = selectable_reasoning_efforts("gemini-oauth", model)
        assert actual == expected_efforts, (
            f"Documentation drift on {model}: documented {expected_efforts} != registry {actual}"
        )


def test_documented_dynamic_tiered_wire_models_match_registry():
    """Ensure documented dynamic tiered wire models match capability registry."""
    for base_model, expected_wire in DOCUMENTED_WIRE_MODELS.items():
        resolved = resolve_model_selection(base_model, effort="high")
        assert resolved.wire_model == expected_wire, (
            f"Documentation drift on wire model for {base_model}: {resolved.wire_model} != {expected_wire}"
        )


def test_documented_account_routes_match_cloudcode_providers():
    """Ensure documented account routes match valid Cloud Code providers in registry."""
    valid_providers = set(_CLOUDCODE_EFFORT_PROVIDERS) | set(_CLOUDCODE_ACCOUNT_PROVIDERS)
    for route in DOCUMENTED_ACCOUNT_ROUTES:
        assert route in valid_providers, f"Documented route {route} not in valid Cloud Code providers"


def test_historical_plans_contain_authoritative_status_notice():
    """Historical plan must defer to the new authoritative runtime architecture reference."""
    plan_path = Path("website/docs/developer-guide/plans/gemini_per_base_reasoning_effort.md")
    assert plan_path.exists(), "Historical plan missing"
    content = plan_path.read_text(encoding="utf-8")
    assert "Status**: Implemented / Historical Plan" in content
    assert "../gemini-cloud-code-runtime.md" in content
