"""Documentation Drift Guard Suite (Action Item 4, Milestone 4).

Validates that critical architectural tables and constants documented in
website/docs/developer-guide/gemini-cloud-code-runtime.md are parsed directly from the Markdown
and remain strictly synchronized with authoritative production registries in agent/gemini_cloudcode_models.py.
"""

from pathlib import Path
import re
import pytest

from agent.gemini_cloudcode_models import (
    selectable_reasoning_efforts,
    resolve_model_selection,
    get_model_capability,
    _CLOUDCODE_ACCOUNT_PROVIDERS,
    _MODEL_CAPABILITIES,
)


DOC_PATH = Path("website/docs/developer-guide/gemini-cloud-code-runtime.md")


def _parse_markdown_truth_table() -> dict[str, dict]:
    """Extract and parse the Truth Table directly from the Markdown guide."""
    assert DOC_PATH.exists(), f"Authoritative documentation file missing at {DOC_PATH}"
    content = DOC_PATH.read_text(encoding="utf-8")

    match = re.search(r"## 2\. Logical Models vs\. Wire Models Truth Table.*?\n(\|.*?)\n\n", content, re.DOTALL)
    assert match, "Truth Table not found in markdown under Section 2"

    table_text = match.group(1)
    rows = [line.strip() for line in table_text.splitlines() if line.strip().startswith("|")]
    data_rows = rows[2:]

    parsed = {}
    for r in data_rows:
        cols = [col.strip() for col in r.split("|")[1:-1]]
        model_id = cols[0].replace("`", "")
        efforts_raw = cols[1]
        default_eff = cols[2].replace("`", "")
        wire_model = cols[3].replace("`", "")
        wire_cfg = cols[4]

        if "None" in efforts_raw:
            efforts = ()
        else:
            efforts = tuple(e.strip().replace("`", "") for e in efforts_raw.split(","))

        parsed[model_id] = {
            "efforts": efforts,
            "default": default_eff,
            "wire_model": wire_model,
            "wire_cfg": wire_cfg,
        }
    return parsed


def test_documentation_file_exists_and_references_authoritative_sections():
    """Verify that the authoritative runtime architecture document exists and has required sections."""
    content = DOC_PATH.read_text(encoding="utf-8")

    assert "# Google Gemini Cloud Code PA (`gemini-oauth`) Runtime Architecture" in content
    assert "1. Architectural Ownership Map" in content
    assert "2. Logical Models vs. Wire Models Truth Table" in content
    assert "3. Compatibility Alias Policy" in content
    assert "4. Five-Layer Reasoning Precedence & Lifetime Model" in content
    assert '5. "None" & Disabled-State Semantics' in content
    assert "6. `model.options` Capability Contract" in content
    assert "7. Thought-Signature Provenance & History Circulation" in content
    assert "8. Switching & Rollback Transaction Contract" in content
    assert "9. Fallback & Restoration Matrix" in content
    assert "10. Surface Equivalence & Scope Semantics" in content
    assert "11. Empirical Upstream Caveats" in content

    # Invariant: Broken math formulas must not appear
    assert "eq$" not in content
    assert "Logical Base Model ≠ Legacy Compatibility Alias ≠ Wire Model" in content


def test_all_registry_models_present_in_markdown_table():
    """Every core canonical Cloud Code model in the registry must appear in the Markdown truth table,
    and every documented model must exist in the production capability registry."""
    parsed_table = _parse_markdown_truth_table()
    # 1. Core Cloud Code canonical models must all be documented
    core_models = (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-pro",
        "gemini-3.1-flash-lite",
    )
    for base_model in core_models:
        assert base_model in parsed_table, (
            f"Documentation drift: canonical model {base_model} missing from Markdown Truth Table"
        )

    # 2. Every documented model must be a valid subset of production registry
    for model_id in parsed_table:
        if "Partner" in model_id:
            continue
        assert model_id in _MODEL_CAPABILITIES, (
            f"Documentation drift: documented model {model_id} not in production capability registry"
        )


def test_markdown_truth_table_matches_production_capabilities():
    """The Markdown Truth Table rows must match selectable_reasoning_efforts and model defaults."""
    parsed_table = _parse_markdown_truth_table()

    for model, parsed in parsed_table.items():
        if "Partner" in model:
            continue
        actual_efforts = selectable_reasoning_efforts("gemini-oauth", model)
        assert parsed["efforts"] == actual_efforts, (
            f"Documentation drift on {model}: Markdown parsed {parsed['efforts']} != registry {actual_efforts}"
        )
        if actual_efforts:
            cap = get_model_capability(model)
            assert parsed["default"] == cap.default_effort, (
                f"Documentation drift on {model} default: Markdown parsed {parsed['default']} != registry {cap.default_effort}"
            )


def test_dynamic_tiered_wire_models_and_outbound_thinking_config():
    """Dynamic models (3.8, 3.7) wire to -tiered and build thinkingConfig without thinkingBudget."""
    parsed_table = _parse_markdown_truth_table()

    for dynamic_model in ("gemini-3.8-flash", "gemini-3.7-flash"):
        assert dynamic_model in parsed_table
        parsed = parsed_table[dynamic_model]

        # Invariant 1: Wire model in Markdown matches resolve_model_selection
        resolved_high = resolve_model_selection(dynamic_model, effort="high")
        assert parsed["wire_model"] == resolved_high.wire_model

        # Invariant 2: Markdown describes thinkingLevel and includeThoughts, NOT thinkingBudget
        assert "thinkingLevel" in parsed["wire_cfg"]
        assert "includeThoughts" in parsed["wire_cfg"]
        assert "thinkingBudget" not in parsed["wire_cfg"]

        # Invariant 3: Production EffortRoute builds outbound thinkingConfig without thinkingBudget
        for effort in ("low", "medium", "high"):
            resolved = resolve_model_selection(dynamic_model, effort=effort)
            assert resolved.thinking_config == {
                "thinkingLevel": effort,
                "includeThoughts": True,
            }


def test_canonical_account_routes_match_production_registry():
    """Documented account routes must match exact set of gemini-oauth + _CLOUDCODE_ACCOUNT_PROVIDERS."""
    content = DOC_PATH.read_text(encoding="utf-8")

    expected_routes = {"gemini-oauth"} | set(_CLOUDCODE_ACCOUNT_PROVIDERS.keys())
    assert expected_routes == {"gemini-oauth", "gemini-1", "gemini-2", "gemini-3", "gemini-4", "gemini-5"}

    for route in expected_routes:
        assert route in content, f"Documentation drift: valid route {route} not documented in guide"


def test_historical_plans_contain_authoritative_status_notice():
    """Historical plan must defer to the new authoritative runtime architecture reference."""
    plan_path = Path("website/docs/developer-guide/plans/gemini_per_base_reasoning_effort.md")
    assert plan_path.exists(), "Historical plan missing"
    content = plan_path.read_text(encoding="utf-8")
    assert "Status**: Implemented / Historical Plan" in content
    assert "../gemini-cloud-code-runtime.md" in content
