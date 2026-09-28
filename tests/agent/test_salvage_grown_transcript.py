"""Mechanical salvage for compression candidates that would grow."""

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    HISTORICAL_TASK_HEADING,
    _LEAN_USER_MESSAGES_HEADING,
    _SUMMARY_END_MARKER,
    _salvage_trim_summary,
    salvage_grown_transcript,
)
from agent.model_metadata import estimate_messages_tokens_rough


def test_salvage_stubs_old_tools_and_keeps_todo_when_stubbing_suffices():
    """Tool stubbing alone gets under budget → the todo snapshot survives.

    The snapshot is the only in-transcript todo re-injection at the boundary
    (and may carry the pruned-skill reload notice), so it is last-resort only.
    """
    original = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "tool_call_id": "a", "content": "A" * 4000},
        {"role": "tool", "tool_call_id": "b", "content": "B" * 4000},
        {"role": "tool", "tool_call_id": "c", "content": "keep-latest"},
    ]
    grown = original + [
        {
            "role": "user",
            "content": "Current todos:\n- [ ] x",
            "_todo_snapshot_synthetic": True,
        }
    ]
    assert estimate_messages_tokens_rough(grown) > estimate_messages_tokens_rough(original)

    out = salvage_grown_transcript(original, grown)

    assert out is not None
    assert estimate_messages_tokens_rough(out) < estimate_messages_tokens_rough(original)
    assert any(m.get("_todo_snapshot_synthetic") for m in out)
    tools = [m["content"] for m in out if m.get("role") == "tool"]
    assert tools[-1] == "keep-latest"
    assert any("cleared to save context space" in t for t in tools)


def test_salvage_drops_todo_only_as_last_resort():
    """When cheaper ops cannot get under budget, the snapshot is dropped."""
    original = [
        {"role": "user", "content": "please do the thing " + ("o" * 600)},
        {"role": "assistant", "content": "ok"},
    ]
    grown = [
        {"role": "user", "content": "summary of the ask"},
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": "Current todos:\n- [ ] " + ("t" * 800),
            "_todo_snapshot_synthetic": True,
        },
    ]
    assert estimate_messages_tokens_rough(grown) > estimate_messages_tokens_rough(original)

    out = salvage_grown_transcript(original, grown)

    assert out is not None
    assert estimate_messages_tokens_rough(out) < estimate_messages_tokens_rough(original)
    assert not any(m.get("_todo_snapshot_synthetic") for m in out)


def test_salvage_last_resort_preserves_pruned_skill_reload_notice():
    """7a16840add couples the reload notice into the snapshot — it survives."""
    from agent.conversation_compression import _PRUNED_SKILL_RELOAD_NOTICE_HEADER

    notice = (
        f"{_PRUNED_SKILL_RELOAD_NOTICE_HEADER}\n"
        "Reload with skill_view(name='example-skill') before acting."
    )
    original = [
        {"role": "user", "content": "please do the thing " + ("o" * 3000)},
        {"role": "assistant", "content": "ok"},
    ]
    grown = [
        {"role": "user", "content": "summary of the ask"},
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": "Current todos:\n- [ ] " + ("t" * 4000) + f"\n\n{notice}",
            "_todo_snapshot_synthetic": True,
        },
    ]
    assert estimate_messages_tokens_rough(grown) > estimate_messages_tokens_rough(original)

    out = salvage_grown_transcript(original, grown)

    assert out is not None
    assert estimate_messages_tokens_rough(out) < estimate_messages_tokens_rough(original)
    snapshot_rows = [m for m in out if m.get("_todo_snapshot_synthetic")]
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["content"].startswith(_PRUNED_SKILL_RELOAD_NOTICE_HEADER)
    assert "Current todos" not in snapshot_rows[0]["content"]


def test_salvage_returns_none_when_nothing_can_shrink():
    original = [{"role": "user", "content": "tiny"}]
    huge = [{"role": "user", "content": "X" * 200_000}]

    assert salvage_grown_transcript(original, huge) is None


def test_salvage_caps_oversized_summary():
    """Structured oversized summary with droppable sections gets capped under budget while preserving protected sections."""
    original = [
        {"role": "user", "content": "ask " + ("o" * 12_000)},
        {"role": "assistant", "content": "short reply"},
    ]
    summary_text = (
        "[CONTEXT COMPACTION]\n\n"
        + HISTORICAL_TASK_HEADING + "\nInitial task instructions.\n\n"
        + "## Detailed Session Log (oldest first)\n"
        + ("- verbose session log entry\n" * 600)
        + "\n"
        + _SUMMARY_END_MARKER
    )
    grown = [
        {
            "role": "user",
            "content": summary_text,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "short reply"},
    ]
    assert estimate_messages_tokens_rough(grown) > estimate_messages_tokens_rough(original)

    out = salvage_grown_transcript(original, grown)

    assert out is not None
    assert len(out[0]["content"]) <= 8000
    assert HISTORICAL_TASK_HEADING in out[0]["content"]
    assert "truncated so compaction can shrink" in out[0]["content"]
    assert out[0]["content"].endswith(_SUMMARY_END_MARKER)


def test_salvage_refuses_unstructured_oversized_summary():
    """Unstructured oversized summaries cannot establish a safe slice and must be refused rather than blindly truncated."""
    original = [
        {"role": "user", "content": "ask " + ("o" * 12_000)},
        {"role": "assistant", "content": "short reply"},
    ]
    grown = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION] "
                + ("S" * 20_000)
                + "\n\n"
                + _SUMMARY_END_MARKER
            ),
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "short reply"},
    ]
    assert estimate_messages_tokens_rough(grown) > estimate_messages_tokens_rough(original)
    assert salvage_grown_transcript(original, grown) is None


def test_salvage_never_truncates_merged_summary_with_live_user_tail():
    original = [{"role": "user", "content": "O" * 12_000}]
    merged = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION] "
                + ("S" * 20_000)
                + "\n\n"
                + _SUMMARY_END_MARKER
                + "\n\nLIVE USER REQUEST"
            ),
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        }
    ]

    assert salvage_grown_transcript(original, merged) is None
    assert merged[0]["content"].endswith("LIVE USER REQUEST")


def test_salvage_does_not_cap_plain_user_text_quoting_summary_marker():
    original = [{"role": "user", "content": "O" * 20_000}]
    quoted = "ordinary user text " + ("Q" * 12_000) + "\n\n" + _SUMMARY_END_MARKER
    candidate = [{"role": "user", "content": quoted}]

    out = salvage_grown_transcript(original, candidate)

    assert out is not None
    assert out[0]["content"] == quoted
    assert "truncated so compaction can shrink" not in out[0]["content"]


def test_salvage_never_caps_unmarked_summary_shaped_live_user_text():
    original = [{"role": "user", "content": "O" * 20_000}]
    live_user_text = (
        "[CONTEXT COMPACTION] "
        + ("U" * 12_000)
        + "\n\n"
        + _SUMMARY_END_MARKER
    )
    candidate = [{"role": "user", "content": live_user_text}]

    out = salvage_grown_transcript(original, candidate)

    assert out is not None
    assert out[0]["content"] == live_user_text
    assert "truncated so compaction can shrink" not in out[0]["content"]


def test_salvage_preserves_protected_intent_and_verbatim_user_section():
    original = [
        {"role": "user", "content": "task " + ("o" * 8000)},
        {"role": "assistant", "content": "reply"},
    ]
    budget = estimate_messages_tokens_rough(original)

    head = (
        "[CONTEXT COMPACTION] Earlier context.\n"
        + HISTORICAL_TASK_HEADING + "\nUser asked to perform migration.\n\n"
        + "## Constraints & Preferences\n- MUST use verify-full\n- NEVER use --no-owner\n\n"
        + "## Key Decisions\n- Decision: RocksDB cursor checkpointing\n\n"
    )
    middle = "## Detailed Session Log (oldest first)\n" + ("- verbose log line\n" * 400)
    tail = (
        _LEAN_USER_MESSAGES_HEADING + "\n"
        + "> Please execute phase 1 with verify-full and RocksDB\n\n"
        + _SUMMARY_END_MARKER
    )
    full_summary = head + middle + tail

    candidate = [
        {
            "role": "user",
            "content": full_summary,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "reply"},
    ]

    out = salvage_grown_transcript(original, candidate, budget=budget)

    assert out is not None
    salvaged_text = out[0]["content"]
    assert len(salvaged_text) <= 8000
    assert HISTORICAL_TASK_HEADING in salvaged_text
    assert "## Constraints & Preferences" in salvaged_text
    assert "## Key Decisions" in salvaged_text
    assert _LEAN_USER_MESSAGES_HEADING in salvaged_text
    assert "Please execute phase 1" in salvaged_text
    assert salvaged_text.endswith(_SUMMARY_END_MARKER)
    assert "summary truncated so compaction can shrink" in salvaged_text


def test_salvage_refuses_when_protected_intent_cannot_fit():
    original = [
        {"role": "user", "content": "small " + ("o" * 2000)},
        {"role": "assistant", "content": "reply"},
    ]
    budget = estimate_messages_tokens_rough(original)

    head = (
        "[CONTEXT COMPACTION] Earlier context.\n"
        + HISTORICAL_TASK_HEADING + "\nUser asked to perform migration.\n\n"
        + "## Constraints & Preferences\n" + ("- Critical constraint detail\n" * 250) + "\n"
        + "## Key Decisions\n- Decision: RocksDB cursor checkpointing\n\n"
    )
    tail = (
        _LEAN_USER_MESSAGES_HEADING + "\n"
        + ("> Verbatim user ask with critical parameters\n" * 100)
        + "\n" + _SUMMARY_END_MARKER
    )
    full_summary = head + tail

    candidate = [
        {
            "role": "user",
            "content": full_summary,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "reply"},
    ]

    out = salvage_grown_transcript(original, candidate, budget=budget)
    assert out is None


def test_salvage_tool_integrity_and_pairing():
    original = [
        {"role": "user", "content": "task " + ("o" * 4000)},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_1", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "T" * 8000},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_2", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": "keep_2"},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_3", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_3", "content": "keep_3"},
    ]
    grown = original + [{"role": "user", "content": "extra " + ("x" * 2000)}]
    budget = estimate_messages_tokens_rough(original)

    out = salvage_grown_transcript(original, grown, budget=budget)
    assert out is not None

    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 3
    assert tools[0]["tool_call_id"] == "call_1"
    assert "cleared to save context space" in tools[0]["content"]
    assert tools[1]["tool_call_id"] == "call_2"
    assert tools[1]["content"] == "keep_2"
    assert tools[2]["tool_call_id"] == "call_3"
    assert tools[2]["content"] == "keep_3"


def test_salvage_bounded_no_reduction_behavior():
    original = [
        {"role": "user", "content": "small"},
        {"role": "assistant", "content": "reply"},
    ]
    budget = estimate_messages_tokens_rough(original)

    candidate = [
        {"role": "user", "content": "small"},
        {"role": "assistant", "content": "reply"},
    ]
    assert salvage_grown_transcript(original, candidate, budget=budget) is None


def test_salvage_trim_summary_refuses_final_protected_section_over_cap_no_user():
    """Regression: when final protected section (Constraints/Key Decisions) has no following heading
    and exceeds cap without user messages, refuse trim completely rather than cutting the section body."""
    content_key_decisions = (
        "[CONTEXT COMPACTION]\n\n"
        + HISTORICAL_TASK_HEADING + "\nInitial task instructions.\n\n"
        + "## Key Decisions\n"
        + ("- Crucial decision with architectural justification and invariant details\n" * 200)
        + "\n"
        + _SUMMARY_END_MARKER
    )
    assert len(content_key_decisions) > 8000
    # Must refuse (return None), never cut the body of Key Decisions to fit under 8000
    assert _salvage_trim_summary(content_key_decisions) is None

    content_constraints = (
        "[CONTEXT COMPACTION]\n\n"
        + HISTORICAL_TASK_HEADING + "\nInitial task instructions.\n\n"
        + "## Constraints & Preferences\n"
        + ("- Invariant rule: strict parameter validation must never be bypassed\n" * 200)
        + "\n"
        + _SUMMARY_END_MARKER
    )
    assert len(content_constraints) > 8000
    # Must refuse (return None), never cut the body of Constraints
    assert _salvage_trim_summary(content_constraints) is None


def test_salvage_preserves_active_state_blocked_critical_context():
    """Active/pending work headings Active State, Blocked, and Critical Context must be preserved
    in original order when trimming non-protected sections."""
    content = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Preamble handoff wrapper.\n\n"
        + HISTORICAL_TASK_HEADING + "\nTask migration in progress.\n\n"
        + "## Completed Actions\n" + ("1. Tool run completed successfully\n" * 200) + "\n"
        + "## Active State\nWorking directory /workspace/project, clean branch.\n\n"
        + "## Blocked\nBlocked on Postgres port 5432 connection timeout.\n\n"
        + "## Key Decisions\nDecision: checkpoint via atomic swap.\n\n"
        + "## Errors & Fixes\n" + ("- Handled transient socket error\n" * 150) + "\n"
        + "## Critical Context\nServer binding: 0.0.0.0:8080 with auth token required.\n\n"
        + _LEAN_USER_MESSAGES_HEADING + "\n> user directive: verify active state before proceeding\n\n"
        + _SUMMARY_END_MARKER
    )
    assert len(content) > 8000
    trimmed = _salvage_trim_summary(content)
    assert trimmed is not None
    assert len(trimmed) <= 8000
    assert "Completed Actions" not in trimmed
    assert "Errors & Fixes" not in trimmed
    assert HISTORICAL_TASK_HEADING in trimmed
    assert "## Active State\nWorking directory /workspace/project, clean branch." in trimmed
    assert "## Blocked\nBlocked on Postgres port 5432 connection timeout." in trimmed
    assert "## Key Decisions\nDecision: checkpoint via atomic swap." in trimmed
    assert "## Critical Context\nServer binding: 0.0.0.0:8080 with auth token required." in trimmed
    assert _LEAN_USER_MESSAGES_HEADING in trimmed
    assert "user directive: verify active state" in trimmed
    assert "summary truncated so compaction can shrink" in trimmed
    assert trimmed.endswith(_SUMMARY_END_MARKER)

    # Verify original order preserved
    pos_hist = trimmed.find(HISTORICAL_TASK_HEADING)
    pos_active = trimmed.find("## Active State")
    pos_blocked = trimmed.find("## Blocked")
    pos_dec = trimmed.find("## Key Decisions")
    pos_crit = trimmed.find("## Critical Context")
    pos_user = trimmed.find(_LEAN_USER_MESSAGES_HEADING)
    assert 0 <= pos_hist < pos_active < pos_blocked < pos_dec < pos_crit < pos_user


def test_salvage_no_user_branch_never_exceeds_max_chars_accounting_for_markers():
    """No-user branch must account for trunc_marker and _SUMMARY_END_MARKER lengths; if total > max_chars, refuse."""
    # When protected text leaves enough room for markers, total assembled <= max_chars
    content_fits = (
        HISTORICAL_TASK_HEADING + "\nTask.\n\n"
        + "## Completed Actions\n" + ("1. Tool\n" * 100) + "\n"
        + "## Key Decisions\n" + ("D" * 7700) + "\n\n"
        + _SUMMARY_END_MARKER
    )
    trimmed = _salvage_trim_summary(content_fits, max_chars=8000)
    assert trimmed is not None
    assert len(trimmed) <= 8000
    assert trimmed.endswith(_SUMMARY_END_MARKER)

    # When protected sections plus markers would exceed max_chars, must refuse (return None)
    content_over = (
        HISTORICAL_TASK_HEADING + "\nTask.\n\n"
        + "## Completed Actions\n" + ("1. Tool\n" * 100) + "\n"
        + "## Key Decisions\n" + ("D" * 7900) + "\n\n"
        + _SUMMARY_END_MARKER
    )
    assert _salvage_trim_summary(content_over, max_chars=8000) is None


def test_salvage_caller_trimmed_none_preserves_original_summary_when_tools_pruned():
    """When _salvage_trim_summary returns None (refuses lossy trim), salvage_grown_transcript
    must preserve the original summary message intact if other safe pruning (tool stubbing) gets under budget."""
    original = [
        {"role": "user", "content": "task " + ("o" * 3000)},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_1", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "T" * 8000},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_2", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": "T" * 8000},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_3", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_3", "content": "keep_3"},
    ]
    budget = estimate_messages_tokens_rough(original)

    # Oversized summary whose protected section alone is over cap with no user messages
    oversized_summary = (
        "[CONTEXT COMPACTION]\n\n"
        + "## Historical Task Snapshot\nTask ask.\n\n"
        + "## Key Decisions\n" + ("- Decision note\n" * 600)
        + "\n" + _SUMMARY_END_MARKER
    )
    assert len(oversized_summary) > 8000

    grown = [
        {
            "role": "user",
            "content": oversized_summary,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_1", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "T" * 8000},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_2", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": "T" * 8000},
        {"role": "assistant", "content": "calling", "tool_calls": [{"id": "call_3", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "call_3", "content": "keep_3"},
    ]

    out = salvage_grown_transcript(original, grown, budget=budget)
    assert out is not None
    assert estimate_messages_tokens_rough(out) < budget
    # The summary was NOT trimmed or corrupted; original summary is preserved intact
    assert out[0]["content"] == oversized_summary
    # Older tool call_1 stubbed out; recent 2 (call_2 and call_3) kept
    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 3
    assert tools[0]["tool_call_id"] == "call_1"
    assert "cleared to save context space" in tools[0]["content"]
    assert tools[1]["tool_call_id"] == "call_2"
    assert tools[1]["content"] == "T" * 8000
    assert tools[2]["tool_call_id"] == "call_3"
    assert tools[2]["content"] == "keep_3"


def test_salvage_caller_trimmed_none_refuses_when_budget_cannot_be_met():
    """When _salvage_trim_summary returns None and other pruning cannot bring transcript under budget, refuse candidate."""
    original = [
        {"role": "user", "content": "small " + ("o" * 200)},
        {"role": "assistant", "content": "reply"},
    ]
    budget = estimate_messages_tokens_rough(original)

    oversized_summary = (
        "[CONTEXT COMPACTION]\n\n"
        + "## Historical Task Snapshot\nTask ask.\n\n"
        + "## Key Decisions\n" + ("- Decision note\n" * 600)
        + "\n" + _SUMMARY_END_MARKER
    )
    grown = [
        {
            "role": "user",
            "content": oversized_summary,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        {"role": "assistant", "content": "reply"},
    ]
    # No tools to stub, summary cannot shrink -> refuses candidate
    assert salvage_grown_transcript(original, grown, budget=budget) is None
