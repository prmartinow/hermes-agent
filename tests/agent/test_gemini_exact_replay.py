"""Tests for Action Item 2, Milestone 4: Exact Gemini Replay & Controlled Foreign Bypass.

Verifies:
1. Exact native replay: authoritative carrier parts emitted verbatim without reconstruction.
2. Cross-model carrier eligibility across verified Gemini circulation domain.
3. Stale carrier detection and fail-closed fallback to generic reconstruction.
4. Generic signed fallback: first call real signature, sibling call remains unsigned.
5. Foreign unsigned fallback: foreign tool calls receive bypass sentinel on wire copy only.
6. Signature classification (REAL, BYPASS, MISSING) and non-conversion of corrupted signatures.
7. Non-destructive invariant: durable history is never mutated.
8. Native function call ID association with following functionResponse parts.
9. End-to-end production intake to next-turn native replay.
"""

import copy
import json
import pytest

from agent.native_replay import (
    GOOGLE_NATIVE_ASSISTANT_TYPE,
    GOOGLE_SIGNATURE_BYPASS,
    GoogleSignatureKind,
    build_google_native_carrier,
    classify_google_signature,
    usable_google_native_carrier,
)
from agent.gemini_native_adapter import _build_gemini_contents, _translate_tool_call_to_gemini


# ============================================================================
# 1. Signature Classification Tests (Suite F)
# ============================================================================

def test_classify_google_signature():
    # MISSING
    assert classify_google_signature(None) == GoogleSignatureKind.MISSING
    assert classify_google_signature("") == GoogleSignatureKind.MISSING
    assert classify_google_signature("   ") == GoogleSignatureKind.MISSING
    assert classify_google_signature(123) == GoogleSignatureKind.MISSING

    # BYPASS
    assert classify_google_signature("skip_thought_signature_validator") == GoogleSignatureKind.BYPASS
    assert classify_google_signature("  skip_thought_signature_validator  ") == GoogleSignatureKind.BYPASS

    # REAL
    assert classify_google_signature("MIIB_valid_opaque_sig") == GoogleSignatureKind.REAL
    # Corrupted / garbage signatures are classified REAL (never converted to bypass)
    assert classify_google_signature("corrupted_garbage_signature_123") == GoogleSignatureKind.REAL


# ============================================================================
# 2. Exact Native Replay Tests (Suite A)
# ============================================================================

def test_exact_native_replay_parallel_calls():
    # Part 0: signed, Part 1: unsigned
    carrier_parts = [
        {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}, "thoughtSignature": "sig_tokyo_real"},
        {"functionCall": {"name": "get_stock_price", "args": {"symbol": "AAPL"}}},  # Unsigned
    ]
    carrier = build_google_native_carrier(carrier_parts, source_model="gemini-3.8-flash-tiered")

    messages = [
        {"role": "user", "content": "run parallel"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}},
                {"id": "c2", "type": "function", "function": {"name": "get_stock_price", "arguments": '{"symbol": "AAPL"}'}},
            ],
            "reasoning_details": [carrier],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    assert len(contents) == 2
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    # Authoritative exact native parts emitted
    assert model_turn["parts"] == carrier_parts
    assert model_turn["parts"][0]["thoughtSignature"] == "sig_tokyo_real"
    assert "thoughtSignature" not in model_turn["parts"][1]


def test_exact_native_replay_text_and_signature_only_part():
    carrier_parts = [
        {"text": "Here is the explanation."},
        {"thoughtSignature": "sig_terminal_text_only"},
    ]
    carrier = build_google_native_carrier(carrier_parts, source_model="gemini-3.8-flash-tiered")

    messages = [
        {"role": "user", "content": "explain"},
        {
            "role": "assistant",
            "content": "Here is the explanation.",
            "reasoning_details": [carrier],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    assert len(contents) == 2
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"] == carrier_parts


def test_exact_native_replay_preserves_unknown_metadata_and_ordering():
    carrier_parts = [
        {"thought": True, "text": "thinking step"},
        {"text": "interim output"},
        {"functionCall": {"name": "my_tool", "args": {"k": "v"}, "customMeta": {"foo": "bar"}}, "thoughtSignature": "sig_x"},
        {"customTopLevel": 1234},
    ]
    carrier = build_google_native_carrier(carrier_parts, source_model="gemini-3.8-flash-tiered")

    messages = [
        {"role": "user", "content": "do"},
        {
            "role": "assistant",
            "content": "interim output",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "my_tool", "arguments": '{"k": "v"}'}},
            ],
            "reasoning_details": [carrier],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    assert contents[1]["parts"] == carrier_parts


# ============================================================================
# 3. Cross-Model Carrier Eligibility (Suite B)
# ============================================================================

def test_cross_model_carrier_eligibility():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "tool", "args": {}}, "thoughtSignature": "sig_38"}],
        source_model="gemini-3.8-flash-tiered",
    )
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "tool", "arguments": "{}"}}],
        "reasoning_details": [carrier],
    }

    # Verified Gemini targets: eligible
    for target in ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.1-pro"]:
        assert usable_google_native_carrier(msg, target_model=target) is not None

    # Unverified / partner targets: ineligible
    for target in ["claude-sonnet-4-6", "gpt-oss-120b-medium", "gemini-3.5-flash", "gemini-4.2-flash"]:
        assert usable_google_native_carrier(msg, target_model=target) is None


def test_carrier_from_unverified_source_model_rejected():
    carrier_unverified = build_google_native_carrier(
        [{"functionCall": {"name": "tool", "args": {}}, "thoughtSignature": "sig_35"}],
        source_model="gemini-3.5-flash",  # Unverified circulation
    )
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "tool", "arguments": "{}"}}],
        "reasoning_details": [carrier_unverified],
    }
    assert usable_google_native_carrier(msg, target_model="gemini-3.8-flash") is None


# ============================================================================
# 4. Stale Carrier Rejection & Semantic Applicability (Suite C)
# ============================================================================

def test_stale_carrier_rejection_semantic_mismatch():
    base_carrier = build_google_native_carrier(
        [
            {"text": "Original text"},
            {"functionCall": {"name": "fetch", "args": {"id": 1}, "id": "call_1"}, "thoughtSignature": "sig_1"},
        ],
        source_model="gemini-3.8-flash-tiered",
    )

    base_msg = {
        "role": "assistant",
        "content": "Original text",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "fetch", "arguments": '{"id": 1}'}}],
        "reasoning_details": [base_carrier],
    }

    # 1. Matching -> Usable
    assert usable_google_native_carrier(base_msg, target_model="gemini-3.8-flash") is not None

    # 2. Content changed -> Stale
    msg_content_changed = copy.deepcopy(base_msg)
    msg_content_changed["content"] = "Edited new text"
    assert usable_google_native_carrier(msg_content_changed, target_model="gemini-3.8-flash") is None

    # 3. Tool name changed -> Stale
    msg_tool_changed = copy.deepcopy(base_msg)
    msg_tool_changed["tool_calls"][0]["function"]["name"] = "different_tool"
    assert usable_google_native_carrier(msg_tool_changed, target_model="gemini-3.8-flash") is None

    # 4. Tool arguments changed -> Stale
    msg_args_changed = copy.deepcopy(base_msg)
    msg_args_changed["tool_calls"][0]["function"]["arguments"] = '{"id": 999}'
    assert usable_google_native_carrier(msg_args_changed, target_model="gemini-3.8-flash") is None

    # 5. Tool removed -> Stale
    msg_tool_removed = copy.deepcopy(base_msg)
    msg_tool_removed["tool_calls"] = []
    assert usable_google_native_carrier(msg_tool_removed, target_model="gemini-3.8-flash") is None

    # 6. Tool added -> Stale
    msg_tool_added = copy.deepcopy(base_msg)
    msg_tool_added["tool_calls"].append({"id": "call_2", "type": "function", "function": {"name": "tool2", "arguments": "{}"}})
    assert usable_google_native_carrier(msg_tool_added, target_model="gemini-3.8-flash") is None

    # 7. Non-empty conflicting IDs -> Stale
    msg_id_conflict = copy.deepcopy(base_msg)
    msg_id_conflict["tool_calls"][0]["id"] = "different_call_id"
    assert usable_google_native_carrier(msg_id_conflict, target_model="gemini-3.8-flash") is None


# ============================================================================
# 5. Generic Fallback: Signed Group & Foreign Unsigned (Suites D, E, G)
# ============================================================================

def test_generic_fallback_signed_group_sibling_remains_unsigned():
    # No carrier present (e.g. lost during compaction)
    # Call A has real signature, Call B has missing signature
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "call_a", "arguments": "{}"},
                    "extra_content": {"thought_signature": "real_sig_a"},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "call_b", "arguments": "{}"},
                    # No signature
                },
            ],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    asst_turn = contents[1]
    parts = asst_turn["parts"]
    assert len(parts) == 2
    assert parts[0]["functionCall"]["name"] == "call_a"
    assert parts[0]["thoughtSignature"] == "real_sig_a"
    assert parts[1]["functionCall"]["name"] == "call_b"
    # Mandatory requirement: Call B must remain unsigned; NO bypass injected!
    assert "thoughtSignature" not in parts[1]


def test_generic_fallback_foreign_unsigned_group_synthesizes_bypass():
    # Completely unsigned foreign history (e.g. from Claude)
    messages = [
        {"role": "user", "content": "turn 1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "claude_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "claude_2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ],
        },
        {"role": "user", "content": "turn 2"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "claude_3", "type": "function", "function": {"name": "f3", "arguments": "{}"}},
            ],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    # All historical foreign tool call turns must be repaired with bypass sentinel on wire copy
    asst_turn1 = contents[1]
    assert asst_turn1["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS
    assert asst_turn1["parts"][1]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS

    asst_turn2 = contents[3]
    assert asst_turn2["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS


def test_generic_fallback_corrupted_signature_fails_transparently():
    # Corrupted / garbage signature must be emitted as-is (never converted to bypass)
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "tool", "arguments": "{}"},
                    "extra_content": {"thought_signature": "corrupted_garbage_bytes_xyz"},
                }
            ],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    part = contents[1]["parts"][0]
    assert part["thoughtSignature"] == "corrupted_garbage_bytes_xyz"


def test_bypass_combinations():
    # 1. BYPASS + MISSING -> both get bypass on wire
    msg_bypass_missing = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "t1", "arguments": "{}"}, "extra_content": {"thought_signature": GOOGLE_SIGNATURE_BYPASS}},
                {"id": "2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
            ],
        },
    ]
    contents1, _ = _build_gemini_contents(msg_bypass_missing, model="gemini-3.8-flash-tiered")
    assert contents1[1]["parts"][0]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS
    assert contents1[1]["parts"][1]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS

    # 2. REAL + BYPASS + MISSING -> REAL preserved, BYPASS preserved, MISSING unsigned
    msg_tri = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "t1", "arguments": "{}"}, "extra_content": {"thought_signature": "real_1"}},
                {"id": "2", "type": "function", "function": {"name": "t2", "arguments": "{}"}, "extra_content": {"thought_signature": GOOGLE_SIGNATURE_BYPASS}},
                {"id": "3", "type": "function", "function": {"name": "t3", "arguments": "{}"}},
            ],
        },
    ]
    contents2, _ = _build_gemini_contents(msg_tri, model="gemini-3.8-flash-tiered")
    assert contents2[1]["parts"][0]["thoughtSignature"] == "real_1"
    assert contents2[1]["parts"][1]["thoughtSignature"] == GOOGLE_SIGNATURE_BYPASS
    assert "thoughtSignature" not in contents2[1]["parts"][2]


# ============================================================================
# 6. Non-Destructive Invariant (Suite H)
# ============================================================================

def test_build_gemini_contents_non_destructive_invariant():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "tool", "args": {}}, "thoughtSignature": "sig"}],
        source_model="gemini-3.8-flash-tiered",
    )
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "tool", "arguments": "{}"}}],
            "reasoning_details": json.dumps([carrier]),  # SQLite-restored string
        },
        {"role": "user", "content": "tool result", "tool_call_id": "c1"},
    ]

    orig = copy.deepcopy(messages)
    _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")

    # Messages must remain 100% byte-identical
    assert messages == orig


# ============================================================================
# 7. Native ID Association & Tool Result Coupling (Suite I)
# ============================================================================

def test_native_id_association_with_function_response():
    carrier = build_google_native_carrier(
        [{"functionCall": {"name": "calc", "args": {"x": 1}, "id": "native_fc_id_999"}, "thoughtSignature": "sig"}],
        source_model="gemini-3.8-flash-tiered",
    )
    messages = [
        {"role": "user", "content": "calc"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "native_fc_id_999", "type": "function", "function": {"name": "calc", "arguments": '{"x": 1}'}}],
            "reasoning_details": [carrier],
        },
        {
            "role": "tool",
            "tool_call_id": "native_fc_id_999",
            "name": "calc",
            "content": '{"result": 2}',
        },
    ]

    contents, _ = _build_gemini_contents(messages, include_tool_call_ids=True, model="gemini-3.8-flash-tiered")
    assert len(contents) == 3
    tool_resp_part = contents[2]["parts"][0]
    assert "functionResponse" in tool_resp_part
    assert tool_resp_part["functionResponse"]["name"] == "calc"
    assert tool_resp_part["functionResponse"]["id"] == "native_fc_id_999"


# ============================================================================
# 8. Partner Model Rejection Defense-in-Depth (Suite 13)
# ============================================================================

def test_partner_models_never_receive_thought_signatures_or_sentinels():
    messages = [
        {"role": "user", "content": "call"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
            ],
        },
    ]

    for partner_model in ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]:
        contents, _ = _build_gemini_contents(messages, model=partner_model)
        part = contents[1]["parts"][0]
        assert "thoughtSignature" not in part
# ============================================================================
# 9. Production Intake to Next-Turn Exact Native Replay
# ============================================================================

def test_production_intake_to_next_turn_native_replay():
    from agent.gemini_native_adapter import translate_gemini_response
    from agent.chat_completion_helpers import build_assistant_message
    from unittest.mock import MagicMock

    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "Plan: call tool"},
                        {"functionCall": {"name": "run_op", "args": {"step": 1}}, "thoughtSignature": "sig_turn1"},
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    # Turn 1: translate raw Gemini response
    translated = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    asst_msg = translated.choices[0].message
    assert asst_msg.reasoning_details is not None

    # Turn 1: build canonical assistant message
    mock_agent = MagicMock()
    mock_agent._needs_thinking_reasoning_pad.return_value = False
    mock_agent._extract_reasoning.return_value = None
    mock_agent._split_responses_tool_id.side_effect = lambda rid: (rid, None)
    mock_agent._derive_responses_function_call_id.return_value = None

    persisted_asst = build_assistant_message(mock_agent, asst_msg, finish_reason="tool_calls")

    # Turn 2: next request includes the persisted assistant message + tool result + new user prompt
    turn2_messages = [
        {"role": "user", "content": "start"},
        persisted_asst,
        {"role": "tool", "tool_call_id": persisted_asst["tool_calls"][0]["id"], "name": "run_op", "content": '{"ok": true}'},
        {"role": "user", "content": "continue"},
    ]

    contents, _ = _build_gemini_contents(turn2_messages, model="gemini-3.8-flash-tiered")

    # Model turn in turn2_messages is contents[1]
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    # Authoritative exact native parts emitted from the captured carrier
    assert model_turn["parts"] == raw_resp["candidates"][0]["content"]["parts"]
