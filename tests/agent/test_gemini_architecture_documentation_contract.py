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

        labeled_routes = {}
        for part in wire_model.split("/"):
            m = re.search(r"([a-zA-Z0-9_\.-]+)\s*\(([a-z]+)\)", part.strip())
            if m:
                labeled_routes[m.group(2)] = m.group(1)

        parsed[model_id] = {
            "efforts": efforts,
            "default": default_eff,
            "wire_model": wire_model,
            "wire_cfg": wire_cfg,
            "labeled_routes": labeled_routes,
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
    assert "8. History Carrier & Persistence Model" in content
    assert "9. Switching & Rollback Transaction Contract" in content
    assert "10. Fallback & Restoration Matrix" in content
    assert "11. Resume & Session Rebuild Contract" in content
    assert "12. Surface Equivalence & Scope Semantics" in content
    assert "13. Failure Policy Reference" in content
    assert "14. Empirical Upstream Caveats" in content

    # Invariant: Clean typography without broken math formatting
    assert "eq$" not in content
    assert "Logical Base Model ≠ Legacy Compatibility Alias ≠ Wire Model" in content


def test_all_registry_models_present_in_markdown_table():
    """Every model in agent/gemini_cloudcode_models.py must appear in the Markdown truth table,
    and every documented model must exist in the production capability registry (exact set equality)."""
    parsed_table = _parse_markdown_truth_table()
    diff_missing = set(_MODEL_CAPABILITIES) - set(parsed_table)
    diff_extra = set(parsed_table) - set(_MODEL_CAPABILITIES)
    err = f"Documentation drift: missing={diff_missing}, extra={diff_extra}"
    assert set(parsed_table) == set(_MODEL_CAPABILITIES), err


def test_markdown_truth_table_matches_production_capabilities():
    """The Markdown Truth Table rows must match selectable_reasoning_efforts and model defaults."""
    parsed_table = _parse_markdown_truth_table()

    for model, cap in _MODEL_CAPABILITIES.items():
        assert model in parsed_table
        parsed = parsed_table[model]

        actual_efforts = selectable_reasoning_efforts("gemini-oauth", model)
        assert parsed["efforts"] == actual_efforts, (
            f"Documentation drift on {model}: Markdown parsed {parsed['efforts']} != registry {actual_efforts}"
        )
        if actual_efforts:
            assert parsed["default"] == cap.default_effort, (
                f"Documentation drift on {model} default: Markdown parsed {parsed['default']} != registry {cap.default_effort}"
            )


def test_wire_models_and_outbound_thinking_config():
    """Verify that documented wire templates match resolve_model_selection() for every model in registry."""
    parsed_table = _parse_markdown_truth_table()

    for model, cap in _MODEL_CAPABILITIES.items():
        parsed = parsed_table[model]

        is_dynamic = bool(cap.routes and cap.routes.get("high") and cap.routes["high"].thinking_level)

        if is_dynamic:
            # Dynamic tiered models (3.8, 3.7)
            resolved_high = resolve_model_selection(model, effort="high")
            assert parsed["wire_model"] == resolved_high.wire_model

            # Markdown describes thinkingLevel and includeThoughts, NOT thinkingBudget
            assert "thinkingLevel" in parsed["wire_cfg"]
            assert "includeThoughts" in parsed["wire_cfg"]
            assert "thinkingBudget" not in parsed["wire_cfg"]

            # Outbound thinkingConfig has thinkingLevel and includeThoughts without thinkingBudget
            for effort in cap.efforts:
                resolved = resolve_model_selection(model, effort=effort)
                assert resolved.thinking_config == {
                    "thinkingLevel": effort,
                    "includeThoughts": True,
                }
        elif cap.efforts:
            # Static tiered models (3.6, 3.5, 3.1-pro)
            if parsed.get("labeled_routes"):
                expected_routes = {eff: cap.routes[eff].wire_model for eff in cap.efforts}
                assert parsed["labeled_routes"] == expected_routes, (
                    f"Documentation drift on labeled wire routes for {model}: "
                    f"Markdown parsed {parsed['labeled_routes']} != registry {expected_routes}"
                )
            else:
                for effort in cap.efforts:
                    resolved = resolve_model_selection(model, effort=effort)
                    wire_template = parsed["wire_model"].replace("<level>", effort)
                    assert resolved.wire_model == wire_template, (
                        f"Documentation drift on static wire model for {model} (effort {effort}): "
                        f"Markdown template {wire_template} != resolved {resolved.wire_model}"
                    )
        else:
            # Zero-effort models (flash-lite, claude, gpt-oss)
            resolved = resolve_model_selection(model)
            assert resolved.wire_model == parsed["wire_model"], (
                f"Documentation drift on no-effort wire model for {model}: "
                f"Markdown {parsed['wire_model']} != resolved {resolved.wire_model}"
            )


def test_canonical_account_routes_parsed_from_markdown():
    """Extract canonical routes directly from the Markdown subsection and verify exact equality."""
    content = DOC_PATH.read_text(encoding="utf-8")

    routes_match = re.search(r"### Canonical Cloud Code Routes:.*?\n(.*?)\n\n", content, re.DOTALL)
    assert routes_match, "Canonical Cloud Code Routes subsection not found in documentation"

    extracted_routes = set(re.findall(r"`([^`]+)`", routes_match.group(1)))
    expected_routes = {"gemini-oauth"} | set(_CLOUDCODE_ACCOUNT_PROVIDERS.keys())

    err = f"Documentation drift on canonical routes: documented={extracted_routes}, expected={expected_routes}"
    assert extracted_routes == expected_routes, err


def test_historical_plans_contain_authoritative_status_notice():
    """Historical plan must defer to the new authoritative runtime architecture reference."""
    plan_path = Path("website/docs/developer-guide/plans/gemini_per_base_reasoning_effort.md")
    assert plan_path.exists(), "Historical plan missing"
    content = plan_path.read_text(encoding="utf-8")
    assert "Status**: Implemented / Historical Plan" in content
    assert "../gemini-cloud-code-runtime.md" in content
