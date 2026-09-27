"""Tests for live session context breakdown."""

from unittest.mock import MagicMock, patch

from agent.context_breakdown import compute_session_context_breakdown


def _make_agent(
    *,
    stable: str = "identity and guidance",
    context: str = "",
    volatile: str = "timestamp line",
    tools: list | None = None,
    context_length: int = 200_000,
    last_prompt_tokens: int = 0,
):
    agent = MagicMock()
    agent.model = "openai/gpt-5.4"
    agent.tools = tools or [
        {"type": "function", "function": {"name": "terminal", "description": "run"}},
        {"type": "function", "function": {"name": "mcp_demo_tool", "description": "mcp"}},
        {"type": "function", "function": {"name": "delegate_task", "description": "spawn"}},
    ]
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent.context_compressor = MagicMock(
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
    )
    return agent, {"stable": stable, "context": context, "volatile": volatile}


def test_breakdown_includes_major_categories():
    stable = (
        "base guidance\n"
        "<available_skills>\n  demo:\n    - hello: hi\n</available_skills>"
    )
    context = "# Project Context\nFollow AGENTS.md"
    volatile = "Current time: now"
    history = [{"role": "user", "content": "hello there"}]
    agent, parts = _make_agent(stable=stable, context=context, volatile=volatile)

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    ids = {item["id"] for item in data["categories"]}
    assert {"system_prompt", "tool_definitions", "rules", "skills", "mcp", "subagent_definitions", "conversation"} <= ids
    assert data["context_max"] == 200_000
    assert data["estimated_total"] > 0



# ── /context renderers (pure functions over the payload) ────────────────────

from agent.context_breakdown import (  # noqa: E402
    compute_context_details,
    render_context_breakdown_lines,
    render_context_category_lines,
    render_context_details_lines,
    render_context_grid,
)


def _payload(**overrides):
    base = {
        "categories": [
            {"id": "system_prompt", "label": "System prompt", "tokens": 10_000},
            {"id": "tool_definitions", "label": "Tool definitions", "tokens": 20_000},
            {"id": "skills", "label": "Skills", "tokens": 5_000},
            {"id": "conversation", "label": "Conversation", "tokens": 15_000},
        ],
        "context_max": 200_000,
        "context_percent": 25,
        "context_used": 50_000,
        "estimated_total": 50_000,
        "model": "openai/gpt-test",
    }
    base.update(overrides)
    return base


def test_grid_is_5x20_and_mostly_free():
    rows = render_context_grid(_payload())
    assert len(rows) == 5
    cells = " ".join(rows).split(" ")
    assert len(cells) == 100
    # 50k / 200k → 25 used cells, 75 free
    assert cells.count("·") == 75
    # Category glyphs proportional: 10k→5, 20k→10, 5k→2-3, 15k→7-8 cells
    assert cells.count("■") == 5
    assert cells.count("▣") == 10










def test_breakdown_lines_grid_toggle():
    with_grid = render_context_breakdown_lines(_payload(), grid=True)
    without = render_context_breakdown_lines(_payload(), grid=False)
    assert any("·" in line for line in with_grid[:5])
    assert not any("·" in line for line in without[:2])
    # Both include the window summary and the expand hint
    for lines in (with_grid, without):
        text = "\n".join(lines)
        assert "Context window: 50,000 / 200,000 tokens (25%)" in text
        assert "/context all" in text




def test_details_lines_caps_listing():
    details = {
        "skills": [
            {"name": f"skill-{i}", "index_tokens": 10, "skill_md_tokens": 100}
            for i in range(20)
        ],
        "toolsets": [],
    }
    lines = render_context_details_lines(details)
    assert any("… and 5 more" in line for line in lines)




from agent.context_breakdown import format_compaction_budget_lines


def test_format_compaction_budget_lines_exempt():
    exempt_report = {
        "context_limit": 1_048_576,
        "output_reservation": 65_536,
        "usable_tokens": 983_040,
        "effective_model_ratio": 0.50,
        "requested_cap": 256_000,
        "effective_cap": None,
        "cap_exempt": True,
        "exemption_reason": "model_family_exemption",
        "actual_trigger": 491_520,
        "limiting_reason": "proportional",
    }
    lines = format_compaction_budget_lines(exempt_report)
    assert len(lines) == 2
    assert lines[0] == "Compaction budget: context limit 1,048,576 · output reservation 65,536 · usable ratio 50% (983,040 tokens)"
    assert lines[1] == "Requested cap: 256,000 · Effective cap: none (exempt: model_family_exemption) · Actual trigger: 491,520 · Limiting reason: proportional"


def test_format_compaction_budget_lines_non_exempt():
    non_exempt_report = {
        "context_limit": 1_000_000,
        "output_reservation": 0,
        "usable_tokens": 1_000_000,
        "effective_model_ratio": 0.50,
        "requested_cap": 256_000,
        "effective_cap": 256_000,
        "cap_exempt": False,
        "exemption_reason": None,
        "actual_trigger": 256_000,
        "limiting_reason": "effective_cap",
    }
    lines = format_compaction_budget_lines(non_exempt_report)
    assert len(lines) == 2
    assert lines[0] == "Compaction budget: context limit 1,000,000 · output reservation 0 · usable ratio 50% (1,000,000 tokens)"
    assert lines[1] == "Requested cap: 256,000 · Effective cap: 256,000 · Actual trigger: 256,000 · Limiting reason: effective_cap"


def test_format_compaction_budget_lines_fallback():
    assert format_compaction_budget_lines({}) == []
    assert format_compaction_budget_lines(None) == []


def test_render_context_breakdown_with_exempt_and_non_exempt_reports():
    exempt_report = {
        "context_limit": 1_048_576,
        "output_reservation": 65_536,
        "usable_tokens": 983_040,
        "effective_model_ratio": 0.50,
        "requested_cap": 256_000,
        "effective_cap": None,
        "cap_exempt": True,
        "exemption_reason": "model_family_exemption",
        "actual_trigger": 491_520,
        "limiting_reason": "proportional",
    }
    lines_exempt = render_context_breakdown_lines(_payload(budget_report=exempt_report))
    text_exempt = "\n".join(lines_exempt)
    assert "Compaction budget: context limit 1,048,576 · output reservation 65,536 · usable ratio 50% (983,040 tokens)" in text_exempt
    assert "Requested cap: 256,000 · Effective cap: none (exempt: model_family_exemption) · Actual trigger: 491,520 · Limiting reason: proportional" in text_exempt

    non_exempt_report = {
        "context_limit": 1_000_000,
        "output_reservation": 0,
        "usable_tokens": 1_000_000,
        "effective_model_ratio": 0.50,
        "requested_cap": 256_000,
        "effective_cap": 256_000,
        "cap_exempt": False,
        "exemption_reason": None,
        "actual_trigger": 256_000,
        "limiting_reason": "effective_cap",
    }
    lines_ne = render_context_breakdown_lines(_payload(), budget_report=non_exempt_report)
    text_ne = "\n".join(lines_ne)
    assert "Compaction budget: context limit 1,000,000 · output reservation 0 · usable ratio 50% (1,000,000 tokens)" in text_ne
    assert "Requested cap: 256,000 · Effective cap: 256,000 · Actual trigger: 256,000 · Limiting reason: effective_cap" in text_ne

    # Legacy / no budget report
    lines_legacy = render_context_breakdown_lines(_payload())
    text_legacy = "\n".join(lines_legacy)
    assert "Compaction budget:" not in text_legacy


def test_compute_session_context_breakdown_attaches_budget():
    exempt_report = {
        "context_limit": 1_048_576,
        "output_reservation": 65_536,
        "usable_tokens": 983_040,
        "effective_model_ratio": 0.50,
        "requested_cap": 256_000,
        "effective_cap": None,
        "cap_exempt": True,
        "exemption_reason": "model_family_exemption",
        "actual_trigger": 491_520,
        "limiting_reason": "proportional",
    }
    agent, parts = _make_agent()
    agent.context_compressor.get_budget_report = lambda: exempt_report
    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, [])
    assert data["budget_report"] == exempt_report
