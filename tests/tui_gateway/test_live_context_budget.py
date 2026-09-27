"""Tests for compaction budget reporting in TUI live context output."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from tui_gateway import server

_format_live_context_output = server._format_live_context_output


def _make_tui_session(budget_report=None, *, has_agent=True, explode=False):
    if not has_agent:
        return {"history": []}

    def _get_budget():
        if explode:
            raise RuntimeError("budget calculation exploded")
        return budget_report

    compressor = SimpleNamespace(context_length=1_048_576)
    if budget_report is not None or explode:
        compressor.get_budget_report = _get_budget

    agent = SimpleNamespace(
        context_compressor=compressor,
        skip_context_files=True,
    )
    return {"agent": agent, "history": []}


def test_tui_format_live_context_output_exempt_report():
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
    session = _make_tui_session(exempt_report)
    output = _format_live_context_output("sid-1", session, "")

    assert "Compaction budget: context limit 1,048,576 · output reservation 65,536 · usable ratio 50% (983,040 tokens)" in output
    assert "Requested cap: 256,000 · Effective cap: none (exempt: model_family_exemption) · Actual trigger: 491,520 · Limiting reason: proportional" in output


def test_tui_format_live_context_output_non_exempt_report():
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
    session = _make_tui_session(non_exempt_report)
    output = _format_live_context_output("sid-2", session, "")

    assert "Compaction budget: context limit 1,000,000 · output reservation 0 · usable ratio 50% (1,000,000 tokens)" in output
    assert "Requested cap: 256,000 · Effective cap: 256,000 · Actual trigger: 256,000 · Limiting reason: effective_cap" in output


def test_tui_format_live_context_output_legacy_no_budget():
    session = _make_tui_session(budget_report=None, has_agent=True)
    output = _format_live_context_output("sid-legacy", session, "")

    assert "Compaction budget:" not in output
    assert "Conversation is empty (no messages yet)." in output


def test_tui_format_live_context_output_no_agent():
    session = _make_tui_session(has_agent=False)
    output = _format_live_context_output("sid-no-agent", session, "")

    assert "Compaction budget:" not in output
    assert "Conversation is empty (no messages yet)." in output


def test_tui_format_live_context_output_exploding_budget():
    session = _make_tui_session(explode=True)
    output = _format_live_context_output("sid-explode", session, "")

    assert "Compaction budget:" not in output
    assert "Conversation is empty (no messages yet)." in output
