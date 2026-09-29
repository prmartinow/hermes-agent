"""Tests for Milestone 1: Native Assistant Replay Carrier & Non-Streaming Capture.

Verifies:
1. Representation-safe reasoning_details normalization (list vs JSON string).
2. Carrier extraction, filtering, and upsert helpers in agent/native_replay.py.
3. Non-streaming translate_gemini_response captures complete google.native_assistant.
4. Part ordering and signature preservation across single, parallel, text, and mixed responses.
5. SQLite SessionDB save -> close -> reopen -> restore roundtrip.
6. Byte-for-byte extra_content equality and absence of outbound behavior changes in M1.
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.gemini_native_adapter import translate_gemini_response, _build_gemini_contents
from agent.native_replay import (
    GOOGLE_NATIVE_ASSISTANT_TYPE,
    CURRENT_NATIVE_CARRIER_VERSION,
    build_google_native_carrier,
    find_native_assistant_detail,
    filter_native_assistant_details,
    normalize_reasoning_details,
    upsert_native_assistant_detail,
)
from hermes_state import SessionDB
from providers import get_provider_profile


# ============================================================================
# 1. Native Replay Helper Tests
# ============================================================================

def test_normalize_reasoning_details_variants():
    assert normalize_reasoning_details(None) == []
    assert normalize_reasoning_details("") == []
    assert normalize_reasoning_details([]) == []

    # Valid list
    lst = [{"type": "some_type", "data": "123"}]
    assert normalize_reasoning_details(lst) == lst

    # Valid JSON string (SQLite returned shape)
    json_str = json.dumps(lst)
    assert normalize_reasoning_details(json_str) == lst

    # Malformed JSON
    assert normalize_reasoning_details("not valid json") == []


def test_find_and_filter_native_assistant_details():
    carrier1 = {"type": "google.native_assistant", "version": 1, "data": "g1"}
    carrier2 = {"type": "anthropic.native_assistant", "version": 1, "data": "a1"}
    other = {"type": "redacted_thinking", "data": "r1"}

    details = [carrier1, carrier2, other]

    # Find Google
    found = find_native_assistant_detail(details, "google.native_assistant")
    assert found == carrier1

    # Filter: keep only google
    kept_google = filter_native_assistant_details(details, keep_type="google.native_assistant")
    assert kept_google == [carrier1, other]

    # Filter: keep none of native_assistant
    kept_none = filter_native_assistant_details(details, keep_type=None)
    assert kept_none == [other]

    # JSON string input support
    assert filter_native_assistant_details(json.dumps(details), keep_type="google.native_assistant") == [carrier1, other]


def test_upsert_native_assistant_detail():
    carrier1 = {"type": "google.native_assistant", "version": 1, "data": "old"}
    other = {"type": "other", "data": "x"}
    details = [carrier1, other]

    carrier1_new = {"type": "google.native_assistant", "version": 1, "data": "new"}
    updated = upsert_native_assistant_detail(details, carrier1_new)
    assert len(updated) == 2
    assert find_native_assistant_detail(updated) == carrier1_new

    # Insert brand new
    carrier2 = {"type": "another.native_assistant", "data": "2"}
    inserted = upsert_native_assistant_detail(updated, carrier2)
    assert len(inserted) == 3


def test_gemini_oauth_profile_declares_native_carrier_type():
    profile = get_provider_profile("gemini-oauth")
    assert profile is not None
    assert profile.native_reasoning_details_type == GOOGLE_NATIVE_ASSISTANT_TYPE


# ============================================================================
# 2. Non-Streaming translate_gemini_response Capture Tests
# ============================================================================

def test_translate_gemini_response_captures_single_signed_function_call():
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "I should call get_weather."},
                        {
                            "functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}},
                            "thoughtSignature": "MIIB_sig_tokyo",
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    res = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    msg = res.choices[0].message

    # Generic representations remain intact
    assert msg.role == "assistant"
    assert msg.reasoning == "I should call get_weather."
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == "get_weather"
    assert msg.tool_calls[0].extra_content == {
        "google": {"thought_signature": "MIIB_sig_tokyo"},
        "thought_signature": "MIIB_sig_tokyo",
    }

    # Native carrier captured in reasoning_details
    assert msg.reasoning_details is not None
    assert len(msg.reasoning_details) == 1
    carrier = msg.reasoning_details[0]
    assert carrier["type"] == GOOGLE_NATIVE_ASSISTANT_TYPE
    assert carrier["version"] == CURRENT_NATIVE_CARRIER_VERSION
    assert carrier["source_model"] == "gemini-3.8-flash-tiered"
    assert carrier["content"]["role"] == "model"
    assert carrier["content"]["parts"] == raw_resp["candidates"][0]["content"]["parts"]


def test_translate_gemini_response_captures_parallel_calls_first_signed():
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}},
                            "thoughtSignature": "MIIB_sig_parallel_first",
                        },
                        {
                            "functionCall": {"name": "get_stock_price", "args": {"symbol": "AAPL"}},
                            # Second parallel call has NO signature
                        },
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    res = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    msg = res.choices[0].message

    assert len(msg.tool_calls) == 2
    assert hasattr(msg.tool_calls[0], "extra_content")
    assert not hasattr(msg.tool_calls[1], "extra_content")

    carrier = find_native_assistant_detail(msg.reasoning_details)
    assert carrier is not None
    parts = carrier["content"]["parts"]
    assert len(parts) == 2
    assert parts[0]["functionCall"]["name"] == "get_weather"
    assert parts[0]["thoughtSignature"] == "MIIB_sig_parallel_first"
    assert parts[1]["functionCall"]["name"] == "get_stock_price"
    assert "thoughtSignature" not in parts[1]


def test_translate_gemini_response_captures_text_part_with_signature():
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "Hello world, here is the answer.",
                            "thoughtSignature": "MIIB_text_sig_12345",
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    res = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    msg = res.choices[0].message

    assert msg.content == "Hello world, here is the answer."
    carrier = find_native_assistant_detail(msg.reasoning_details)
    assert carrier is not None
    assert carrier["content"]["parts"][0]["text"] == "Hello world, here is the answer."
    assert carrier["content"]["parts"][0]["thoughtSignature"] == "MIIB_text_sig_12345"


def test_translate_gemini_response_preserves_exact_part_ordering():
    parts_input = [
        {"thought": True, "text": "Step 1 thinking"},
        {"text": "Interim output"},
        {"thought": True, "text": "Step 2 thinking"},
        {"functionCall": {"name": "do_work", "args": {}}, "thoughtSignature": "sig_work"},
        {"text": "Final explanation"},
    ]
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": parts_input,
                },
                "finishReason": "STOP",
            }
        ]
    }

    res = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    carrier = find_native_assistant_detail(res.choices[0].message.reasoning_details)
    assert carrier["content"]["parts"] == parts_input


# ============================================================================
# 3. Persistence & Outbound Invariant Tests
# ============================================================================

def test_session_db_save_close_restore_carrier_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-session-m1"
        db.create_session(session_id, source="cli")

        raw_parts = [
            {"functionCall": {"name": "query_db", "args": {"q": "SELECT 1"}}, "thoughtSignature": "sig_secret_123"}
        ]
        carrier = build_google_native_carrier(raw_parts, source_model="gemini-3.8-flash-tiered")

        msg = {
            "role": "assistant",
            "content": "Queried database.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "query_db", "arguments": '{"q": "SELECT 1"}'},
                    "extra_content": {"google": {"thought_signature": "sig_secret_123"}, "thought_signature": "sig_secret_123"},
                }
            ],
            "reasoning_details": [carrier],
        }

        db.append_message(session_id, **msg)

        # Reopen fresh connection
        db2 = SessionDB(db_path)
        loaded = db2.get_messages(session_id)
        assert len(loaded) == 1
        loaded_msg = loaded[0]

        # Verify tool_calls extra_content byte-for-byte
        assert loaded_msg["tool_calls"][0]["extra_content"] == msg["tool_calls"][0]["extra_content"]

        # Verify carrier can be parsed and matches exact structure
        loaded_carrier = find_native_assistant_detail(loaded_msg["reasoning_details"])
        assert loaded_carrier is not None
        assert loaded_carrier == carrier
        assert loaded_carrier["content"]["parts"] == raw_parts


def test_m1_outbound_build_gemini_contents_unmodified():
    # Verify that in Milestone 1, _build_gemini_contents continues to function
    # identically with its existing fallback behavior.
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "my_tool", "arguments": "{}"},
                    "extra_content": {"thought_signature": "orig_sig_1"},
                }
            ],
        },
    ]

    contents, _ = _build_gemini_contents(messages, model="gemini-3.8-flash-tiered")
    assert len(contents) == 2
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    fc_part = [p for p in model_turn["parts"] if "functionCall" in p][0]
    assert fc_part["functionCall"]["name"] == "my_tool"
    assert fc_part["thoughtSignature"] == "orig_sig_1"
