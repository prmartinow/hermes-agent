"""Lifecycle, persistence, compaction, and cross-provider closure tests for Action Item 2 (Milestone 5).

Verifies the complete end-to-end invariant:
Provider-native replay state is preserved whenever durable history contains it,
filtered on outbound copies per destination capability, and degrades safely to
genuine per-tool signatures or controlled bypass when lossy compaction removes
the native carrier—without ever fabricating or mutating durable provenance.
"""

import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.chat_completion_helpers import build_assistant_message
from agent.context_compressor import salvage_grown_transcript
from agent.gemini_native_adapter import (
    _build_gemini_contents,
    translate_gemini_response,
)
from agent.native_replay import (
    GOOGLE_NATIVE_ASSISTANT_TYPE,
    GOOGLE_SIGNATURE_BYPASS,
    build_google_native_carrier,
    find_native_assistant_detail,
    usable_google_native_carrier,
)
from agent.session_persistence import _db_flush_row
from agent.transports import get_transport
from hermes_state import SessionDB
from providers import get_provider_profile


def _make_mock_agent():
    mock_agent = MagicMock()
    mock_agent._needs_thinking_reasoning_pad.return_value = False
    mock_agent._extract_reasoning.return_value = None
    mock_agent._split_responses_tool_id.side_effect = lambda rid: (rid, None)
    mock_agent._derive_responses_function_call_id.return_value = None
    return mock_agent


# ============================================================================
# 1. Persistence -> Restart -> Exact Replay Closure
# ============================================================================

def test_persistence_restart_exact_replay_closure():
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "Plan: execute tools"},
                        {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}, "thoughtSignature": "sig_tokyo_live"},
                        {"functionCall": {"name": "get_stock", "args": {"symbol": "AAPL"}}},  # Unsigned sibling
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    # 1. Translate raw Gemini response & capture carrier
    translated = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    asst_msg = translated.choices[0].message
    assert asst_msg.reasoning_details is not None

    # 2. Build canonical Hermes assistant message
    mock_agent = _make_mock_agent()
    persisted_dict = build_assistant_message(mock_agent, asst_msg, finish_reason="tool_calls")

    # 3. Store in SessionDB, close, and restart fresh
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db1 = SessionDB(db_path)
        session_id = "test-restart-closure"
        db1.create_session(session_id, source="cli")
        row = _db_flush_row(mock_agent, persisted_dict, is_current_turn_user=False)
        db1.append_message(session_id, **row)

        # Fresh restart
        db2 = SessionDB(db_path)
        loaded_messages = db2.get_messages(session_id)
        assert len(loaded_messages) == 1
        loaded_asst = loaded_messages[0]

        # In SQLite, reasoning_details is stored as JSON text
        assert isinstance(loaded_asst["reasoning_details"], str)

        # 4. Project through ChatCompletionsTransport
        transport = get_transport("chat_completions")
        profile = get_provider_profile("gemini-oauth")
        turn2_history = [
            {"role": "user", "content": "run parallel"},
            loaded_asst,
            {"role": "tool", "tool_call_id": loaded_asst["tool_calls"][0]["id"], "name": "get_weather", "content": '{"w": "Sunny"}'},
            {"role": "tool", "tool_call_id": loaded_asst["tool_calls"][1]["id"], "name": "get_stock", "content": '{"p": 220}'},
            {"role": "user", "content": "summarize"},
        ]

        orig_turn2 = copy.deepcopy(turn2_history)
        wire_messages = transport.convert_messages(
            turn2_history,
            model="gemini-3.8-flash",
            base_url=profile.base_url,
            provider_profile=profile,
        )

        # Non-destructive invariant: stored turn2_history must remain unchanged
        assert turn2_history == orig_turn2
        assert isinstance(turn2_history[1]["reasoning_details"], str)

        # 5. Build wire Gemini request contents
        contents, _ = _build_gemini_contents(wire_messages, model="gemini-3.8-flash-tiered")
        model_part0 = contents[1]["parts"][1]  # functionCall get_weather
        model_part1 = contents[1]["parts"][2]  # functionCall get_stock

        assert model_part0["functionCall"]["name"] == "get_weather"
        assert model_part0["thoughtSignature"] == "sig_tokyo_live"
        assert model_part1["functionCall"]["name"] == "get_stock"
        assert "thoughtSignature" not in model_part1  # Unsigned sibling remains unsigned!


# ============================================================================
# 2. Gemini -> Partner -> Gemini Round-Trip
# ============================================================================

def test_gemini_partner_gemini_roundtrip_non_destructive():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "tool_x", "args": {"a": 1}}, "thoughtSignature": "sig_gemini_orig"}],
        source_model="gemini-3.8-flash-tiered",
    )
    canonical_history = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "tool_x", "arguments": '{"a": 1}'},
                    "extra_content": {"thought_signature": "sig_gemini_orig"},
                }
            ],
            "reasoning_details": [carrier],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "tool_x", "content": '{"ok": true}'},
        {"role": "user", "content": "continue"},
    ]

    original_canonical = copy.deepcopy(canonical_history)
    transport = get_transport("chat_completions")
    oauth_profile = get_provider_profile("gemini-oauth")

    # Step 1: Project to Gemini 3.8 (Gemini active)
    wire_gemini_1 = transport.convert_messages(
        canonical_history, model="gemini-3.8-flash", base_url=oauth_profile.base_url, provider_profile=oauth_profile
    )
    assert canonical_history == original_canonical
    assert "extra_content" in wire_gemini_1[1]["tool_calls"][0]
    assert find_native_assistant_detail(wire_gemini_1[1]["reasoning_details"]) is not None

    # Step 2: Switch to Claude Sonnet 4.6 under same provider
    wire_claude = transport.convert_messages(
        canonical_history, model="claude-sonnet-4-6", base_url=oauth_profile.base_url, provider_profile=oauth_profile
    )
    assert canonical_history == original_canonical
    # Outbound Claude copy has NO google carrier and NO extra_content
    assert "extra_content" not in wire_claude[1]["tool_calls"][0]
    assert find_native_assistant_detail(wire_claude[1].get("reasoning_details")) is None

    # Step 3: Switch back to Gemini 3.8
    wire_gemini_2 = transport.convert_messages(
        canonical_history, model="gemini-3.8-flash", base_url=oauth_profile.base_url, provider_profile=oauth_profile
    )
    assert canonical_history == original_canonical
    # Carrier and signature fully recovered on Gemini wire copy!
    assert "extra_content" in wire_gemini_2[1]["tool_calls"][0]
    assert find_native_assistant_detail(wire_gemini_2[1]["reasoning_details"]) is not None

    # Step 4: Replay into _build_gemini_contents
    contents, _ = _build_gemini_contents(wire_gemini_2, model="gemini-3.8-flash-tiered")
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig_gemini_orig"


# ============================================================================
# 3. Cross-Model & Cross-Account Closure
# ============================================================================

def test_cross_model_replay_closure():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "run", "args": {}}, "thoughtSignature": "sig_38"}],
        source_model="gemini-3.8-flash-tiered",
    )
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}],
            "reasoning_details": [carrier],
        },
    ]

    # Replay on verified targets
    for target in ["gemini-3.8-flash-tiered", "gemini-3.7-flash-tiered", "gemini-3.6-flash-low", "gemini-3.1-pro-low"]:
        contents, _ = _build_gemini_contents(messages, model=target)
        assert contents[1]["parts"][0]["thoughtSignature"] == "sig_38"


def test_cross_account_provenance_isolation():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "run", "args": {}}, "thoughtSignature": "sig_acc1"}],
        source_model="gemini-3.8-flash-tiered",
    )
    # Stored turn carries Account 1 display metadata
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}],
            "reasoning_details": [carrier],
            "display_metadata": {"gemini_account": "gemini-1", "email": "account1@google.com"},
        },
    ]

    # Target replay through Account 2 context
    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    # Account provenance is display-only; carrier is unaffected
    assert contents[1]["parts"][0]["thoughtSignature"] == "sig_acc1"


# ============================================================================
# 4. Salvage-Compaction Degradation & Fallback (Sections 5, 6, 7)
# ============================================================================

def test_compaction_salvage_carrier_loss_degrades_to_per_tool_signature():
    carrier = build_google_native_carrier(
        [
            {"functionCall": {"name": "tool_a", "args": {}}, "thoughtSignature": "sig_a_real"},
            {"functionCall": {"name": "tool_b", "args": {}}},  # Unsigned
        ],
        source_model="gemini-3.8-flash-tiered",
    )

    # Turn 1: historical turn
    # Turn 2: active recent turn
    candidate = [
        {"role": "user", "content": "historical user"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "tool_a", "arguments": "{}"},
                    "extra_content": {"thought_signature": "sig_a_real"},
                },
                {
                    "id": "2",
                    "type": "function",
                    "function": {"name": "tool_b", "arguments": "{}"},
                },
            ],
            "reasoning_details": [carrier],
        },
        {"role": "tool", "tool_call_id": "1", "name": "tool_a", "content": "res1"},
        {"role": "tool", "tool_call_id": "2", "name": "tool_b", "content": "res2"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent answer"},
    ]

    # Before compaction: exact carrier is authoritative
    contents_before, _ = _build_gemini_contents(candidate[:4], model="gemini-3.8-flash-tiered")
    assert contents_before[1]["parts"][0]["thoughtSignature"] == "sig_a_real"
    assert "thoughtSignature" not in contents_before[1]["parts"][1]

    # Exercise salvage_messages
    compacted = salvage_grown_transcript(candidate, candidate, budget=1000)
    assert compacted is not None
    historical_asst = compacted[1]

    # Invariant: salvage deliberately removed reasoning_details on older assistant turn
    assert "reasoning_details" not in historical_asst
    # Invariant: tool_calls and extra_content survived intact
    assert historical_asst["tool_calls"][0]["extra_content"]["thought_signature"] == "sig_a_real"

    # Now replay compacted history: degraded fallback kicks in
    contents_after, _ = _build_gemini_contents(compacted[:4], model="gemini-3.8-flash-tiered")
    after_parts = contents_after[1]["parts"]
    assert len(after_parts) == 2
    assert after_parts[0]["functionCall"]["name"] == "tool_a"
    assert after_parts[0]["thoughtSignature"] == "sig_a_real"
    assert after_parts[1]["functionCall"]["name"] == "tool_b"
    # Mandatory invariant: unsigned sibling remains unsigned; ZERO bypass on B!
    assert "thoughtSignature" not in after_parts[1]


def test_compaction_salvage_foreign_history_receives_wire_bypass():
    candidate = [
        {"role": "user", "content": "foreign call"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "claude_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "claude_1", "name": "f1", "content": "res1"},
        {"role": "user", "content": "recent user"},
        {"role": "assistant", "content": "recent answer"},
    ]

    compacted = salvage_grown_transcript(candidate, candidate, budget=1000)
    assert compacted is not None

    contents, _ = _build_gemini_contents(compacted[:3], model="gemini-3.8-flash-tiered")
    # Wire copy receives bypass sentinel
    assert contents[1]["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS
    # Compacted history remains unmutated
    assert "extra_content" not in compacted[1]["tool_calls"][0]


def test_text_only_carrier_loss_after_compaction():
    carrier = build_google_native_carrier(
        [{"text": "Visible explanation"}, {"thoughtSignature": "sig_stream_text"}],
        source_model="gemini-3.8-flash-tiered",
    )
    candidate = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "Visible explanation", "reasoning_details": [carrier]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "final"},
    ]

    compacted = salvage_grown_transcript(candidate, candidate, budget=1000)
    assert compacted is not None
    # Older assistant turn has reasoning_details popped
    assert "reasoning_details" not in compacted[1]

    contents, _ = _build_gemini_contents(compacted[:2], model="gemini-3.8-flash-tiered")
    # Text turn without tools contains only visible text; no bypass synthesized onto text!
    assert contents[1]["parts"][0] == {"text": "Visible explanation"}


# ============================================================================
# 5. Stale Carrier & Semantic Divergence (Section 8)
# ============================================================================

def test_stale_carrier_divergence_falls_back_to_generic():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "old_tool", "args": {}}, "thoughtSignature": "sig_old"}],
        source_model="gemini-3.8-flash-tiered",
    )
    # Message was repaired/edited so function name is now "repaired_tool"
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "repaired_tool", "arguments": "{}"}}],
        "reasoning_details": [carrier],
    }

    # Stale carrier rejected
    assert usable_google_native_carrier(msg, target_model="gemini-3.8-flash") is None

    # Replay falls back to generic reconstruction (and synthesizes bypass for the modified unsigned tool)
    contents, _ = _build_gemini_contents([{"role": "user", "content": "hi"}, msg], model="gemini-3.8-flash-tiered")
    assert contents[1]["parts"][0]["functionCall"]["name"] == "repaired_tool"
    assert contents[1]["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS


# ============================================================================
# 6. Retry & Fallback Attempt Isolation (Section 9)
# ============================================================================

def test_fallback_attempt_isolation():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "op", "args": {}}, "thoughtSignature": "sig_att"}],
        source_model="gemini-3.8-flash-tiered",
    )
    canonical_history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "op", "arguments": "{}"},
                    "extra_content": {"thought_signature": "sig_att"},
                }
            ],
            "reasoning_details": [carrier],
        },
    ]

    original = copy.deepcopy(canonical_history)
    transport = get_transport("chat_completions")
    oauth_profile = get_provider_profile("gemini-oauth")

    # Attempt 1: Gemini -> present
    wire1 = transport.convert_messages(canonical_history, model="gemini-3.8-flash", base_url=oauth_profile.base_url, provider_profile=oauth_profile)
    assert canonical_history == original
    assert "extra_content" in wire1[1]["tool_calls"][0]

    # Attempt 2: Claude -> absent
    wire2 = transport.convert_messages(canonical_history, model="claude-sonnet-4-6", base_url=oauth_profile.base_url, provider_profile=oauth_profile)
    assert canonical_history == original
    assert "extra_content" not in wire2[1]["tool_calls"][0]

    # Attempt 3: Gemini again -> present
    wire3 = transport.convert_messages(canonical_history, model="gemini-3.8-flash", base_url=oauth_profile.base_url, provider_profile=oauth_profile)
    assert canonical_history == original
    assert "extra_content" in wire3[1]["tool_calls"][0]


# ============================================================================
# 7. Persistence Never Stores Synthesized Bypass (Section 12)
# ============================================================================

def test_persistence_never_stores_synthesized_bypass():
    foreign_history = [
        {"role": "user", "content": "foreign"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c_foreign", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        },
    ]

    # Outbound projection synthesizes bypass on wire
    wire_contents, _ = _build_gemini_contents(foreign_history, model="gemini-3.8-flash-tiered")
    assert wire_contents[1]["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS

    # Now persist the canonical history into SessionDB
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-no-stored-bypass"
        db.create_session(session_id, source="cli")

        mock_agent = _make_mock_agent()
        row = _db_flush_row(mock_agent, foreign_history[1], is_current_turn_user=False)
        db.append_message(session_id, **row)

        # Restore from fresh DB
        db2 = SessionDB(db_path)
        loaded = db2.get_messages(session_id)
        asst = loaded[0]

        # Invariant: restored message has NO signature and NO bypass sentinel
        tc = asst["tool_calls"][0]
        assert "extra_content" not in tc or not tc.get("extra_content")
        # Invariant: raw SQLite serialized row contains NO skip_thought_signature_validator
        rows = db2._read_all("SELECT tool_calls, reasoning_details FROM messages WHERE session_id = ?", (session_id,))
        for r in rows:
            assert GOOGLE_SIGNATURE_BYPASS not in str(r)
