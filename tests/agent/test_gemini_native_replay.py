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
# ============================================================================
# 4. Milestone 1 Amendment Tests: Partner Exclusion & Boundary Hardening
# ============================================================================

def test_translate_gemini_response_excludes_partner_models():
    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    # Claude partner model: must NOT receive google.native_assistant
    res_claude = translate_gemini_response(raw_resp, model="claude-sonnet-4-6")
    assert res_claude.choices[0].message.reasoning_details is None
    assert len(res_claude.choices[0].message.tool_calls) == 1

    # GPT-OSS partner model: must NOT receive google.native_assistant
    res_gpt = translate_gemini_response(raw_resp, model="gpt-oss-120b-medium")
    assert res_gpt.choices[0].message.reasoning_details is None
    assert len(res_gpt.choices[0].message.tool_calls) == 1


def test_filter_native_assistant_details_preserves_non_dicts():
    details = [
        {"type": "google.native_assistant", "version": 1},
        "opaque-provider-detail",
        17,
        {"type": "ordinary_reasoning", "content": "text"},
    ]

    filtered = filter_native_assistant_details(details, keep_type=None)
    assert "opaque-provider-detail" in filtered
    assert 17 in filtered
    assert {"type": "ordinary_reasoning", "content": "text"} in filtered
    assert not any(isinstance(d, dict) and d.get("type") == "google.native_assistant" for d in filtered)


def test_build_google_native_carrier_takes_deep_snapshot():
    parts = [
        {
            "functionCall": {
                "name": "tool",
                "args": {"nested": {"x": 1}},
            },
            "thoughtSignature": "sig",
        }
    ]

    carrier = build_google_native_carrier(parts, "gemini-3.8-flash-tiered")

    # Mutate the source part
    parts[0]["functionCall"]["args"]["nested"]["x"] = 99
    parts[0]["thoughtSignature"] = "mutated"

    # Carrier must remain unchanged
    carrier_part = carrier["content"]["parts"][0]
    assert carrier_part["functionCall"]["args"]["nested"]["x"] == 1
    assert carrier_part["thoughtSignature"] == "sig"


def test_production_intake_pipeline_gemini_response_to_session_db():
    from agent.chat_completion_helpers import build_assistant_message
    from unittest.mock import MagicMock

    raw_resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"thought": True, "text": "Analyzing request"},
                        {"functionCall": {"name": "query_metrics", "args": {"id": 42}}, "thoughtSignature": "sig_metric_42"},
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }

    # 1. Translate raw Gemini response
    translated = translate_gemini_response(raw_resp, model="gemini-3.8-flash-tiered")
    assistant_msg = translated.choices[0].message
    assert assistant_msg.reasoning_details is not None

    # 2. Intake via build_assistant_message
    mock_agent = MagicMock()
    mock_agent._needs_thinking_reasoning_pad.return_value = False
    mock_agent._extract_reasoning.return_value = None
    mock_agent._split_responses_tool_id.side_effect = lambda rid: (rid, None)
    mock_agent._derive_responses_function_call_id.return_value = None
    persisted_dict = build_assistant_message(mock_agent, assistant_msg, finish_reason="tool_calls")

    assert "reasoning_details" in persisted_dict
    carrier = find_native_assistant_detail(persisted_dict["reasoning_details"])
    assert carrier is not None
    assert carrier["content"]["parts"] == raw_resp["candidates"][0]["content"]["parts"]

    # 3. Store in SessionDB, close, restore, and verify
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-prod-intake"
        db.create_session(session_id, source="cli")
        from agent.session_persistence import _db_flush_row
        row = _db_flush_row(mock_agent, persisted_dict, is_current_turn_user=False)
        db.append_message(session_id, **row)

        # Restore from fresh DB
        db2 = SessionDB(db_path)
        loaded = db2.get_messages(session_id)
        assert len(loaded) == 1
        loaded_msg = loaded[0]

        loaded_carrier = find_native_assistant_detail(loaded_msg["reasoning_details"])
        assert loaded_carrier is not None
        assert loaded_carrier == carrier
# ============================================================================
# 5. Milestone 2: Streaming Native Carrier Accumulation & Parity Tests
# ============================================================================

from agent.native_replay import GoogleNativeStreamAccumulator
from agent.gemini_native_adapter import translate_stream_event


def test_stream_accumulator_preserves_text_thought_and_signature_only():
    acc = GoogleNativeStreamAccumulator(role="model")

    # Frame 1: thought text
    acc.observe_part({"thought": True, "text": "Analyzing..."})
    # Frame 2: visible text
    acc.observe_part({"text": "Hello "})
    acc.observe_part({"text": "world!"})
    # Frame 3: standalone signature-only part (R0 discovery)
    acc.observe_part({"thoughtSignature": "sig_standalone_123"})
    # Frame 4: unknown provider part
    acc.observe_part({"customMeta": {"foo": "bar"}})

    carrier = acc.build_carrier("gemini-3.8-flash-tiered")
    assert carrier is not None
    assert carrier["type"] == GOOGLE_NATIVE_ASSISTANT_TYPE
    assert carrier["source_model"] == "gemini-3.8-flash-tiered"
    parts = carrier["content"]["parts"]
    assert len(parts) == 5
    assert parts[0] == {"thought": True, "text": "Analyzing..."}
    assert parts[1] == {"text": "Hello "}
    assert parts[2] == {"text": "world!"}
    assert parts[3] == {"thoughtSignature": "sig_standalone_123"}
    assert parts[4] == {"customMeta": {"foo": "bar"}}


def test_stream_accumulator_function_call_frame_deduplication_and_signature_retention():
    acc = GoogleNativeStreamAccumulator(role="model")

    # Frame 1: initial function call with signature and partial args
    acc.observe_part(
        {"functionCall": {"name": "query_db", "args": {"q": "SEL"}, "id": "call_1"}, "thoughtSignature": "sig_early"},
        function_slot=0,
    )
    # Frame 2: incremental args, signature omitted in later SSE frame
    acc.observe_part(
        {"functionCall": {"name": "query_db", "args": {"q": "SELECT * FROM users"}, "id": "call_1"}},
        function_slot=0,
    )

    carrier = acc.build_carrier("gemini-3.8-flash-tiered")
    assert carrier is not None
    parts = carrier["content"]["parts"]
    # Must resolve into exactly ONE Part, not two!
    assert len(parts) == 1
    fc_part = parts[0]
    assert fc_part["functionCall"]["name"] == "query_db"
    assert fc_part["functionCall"]["args"] == {"q": "SELECT * FROM users"}
    assert fc_part["functionCall"]["id"] == "call_1"
    # Signature observed early must NOT be lost
    assert fc_part["thoughtSignature"] == "sig_early"


def test_stream_accumulator_late_arriving_signature_recorded():
    acc = GoogleNativeStreamAccumulator(role="model")

    # Frame 1: call without signature
    acc.observe_part({"functionCall": {"name": "calc", "args": {"x": 1}}}, function_slot=0)
    # Frame 2: signature arrives
    acc.observe_part({"functionCall": {"name": "calc", "args": {"x": 1}}, "thoughtSignature": "sig_late"}, function_slot=0)

    carrier = acc.build_carrier("gemini-3.8-flash-tiered")
    parts = carrier["content"]["parts"]
    assert len(parts) == 1
    assert parts[0]["thoughtSignature"] == "sig_late"


def test_streaming_single_signed_function_call_events():
    events = [
        {"candidates": [{"content": {"role": "model", "parts": [{"thought": True, "text": "Calling tool"}]}}]},
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}, "thoughtSignature": "sig_stream_tokyo"}
                        ],
                    }
                }
            ]
        },
        {"candidates": [{"finishReason": "STOP"}]},
    ]

    tool_call_indices = {}
    native_state = GoogleNativeStreamAccumulator()
    all_chunks = []
    for ev in events:
        chunks = translate_stream_event(ev, "gemini-3.8-flash-tiered", tool_call_indices, native_stream_state=native_state)
        all_chunks.extend(chunks)

    # Generic output verification
    tool_chunks = [c for c in all_chunks if getattr(c.choices[0].delta, "tool_calls", None)]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].choices[0].delta.tool_calls[0].function.name == "get_weather"

    # Terminal chunk verification: carries native carrier
    finish_chunks = [c for c in all_chunks if getattr(c.choices[0], "finish_reason", None) == "tool_calls"]
    assert len(finish_chunks) == 1
    terminal = finish_chunks[0]
    assert hasattr(terminal.choices[0].delta, "reasoning_details")
    carrier = terminal.choices[0].delta.reasoning_details[0]
    assert carrier["type"] == GOOGLE_NATIVE_ASSISTANT_TYPE
    assert len(carrier["content"]["parts"]) == 2
    assert carrier["content"]["parts"][1]["thoughtSignature"] == "sig_stream_tokyo"


def test_streaming_parallel_calls_first_signed_second_unsigned():
    events = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}, "thoughtSignature": "sig_first"},
                            {"functionCall": {"name": "get_stock_price", "args": {"symbol": "AAPL"}}},  # Unsigned sibling
                        ],
                    }
                }
            ]
        },
        {"candidates": [{"finishReason": "STOP"}]},
    ]

    tool_call_indices = {}
    native_state = GoogleNativeStreamAccumulator()
    all_chunks = []
    for ev in events:
        chunks = translate_stream_event(ev, "gemini-3.8-flash-tiered", tool_call_indices, native_stream_state=native_state)
        all_chunks.extend(chunks)

    terminal = [c for c in all_chunks if c.choices[0].finish_reason][0]
    carrier = terminal.choices[0].delta.reasoning_details[0]
    parts = carrier["content"]["parts"]
    assert len(parts) == 2
    assert parts[0]["functionCall"]["name"] == "get_weather"
    assert parts[0]["thoughtSignature"] == "sig_first"
    assert parts[1]["functionCall"]["name"] == "get_stock_price"
    assert "thoughtSignature" not in parts[1]


def test_streaming_text_plus_signature_only_r0_fixture():
    events = [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Philosophical answer."}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"thoughtSignature": "sig_stream_c4_only"}]}}]},
        {"candidates": [{"finishReason": "STOP"}]},
    ]

    tool_call_indices = {}
    native_state = GoogleNativeStreamAccumulator()
    all_chunks = []
    for ev in events:
        chunks = translate_stream_event(ev, "gemini-3.8-flash-tiered", tool_call_indices, native_stream_state=native_state)
        all_chunks.extend(chunks)

    terminal = [c for c in all_chunks if c.choices[0].finish_reason][0]
    carrier = terminal.choices[0].delta.reasoning_details[0]
    parts = carrier["content"]["parts"]
    assert len(parts) == 2
    assert parts[0] == {"text": "Philosophical answer."}
    assert parts[1] == {"thoughtSignature": "sig_stream_c4_only"}


def test_streaming_partner_models_never_emit_native_carrier():
    events = [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello from Claude"}]}}]},
        {"candidates": [{"finishReason": "STOP"}]},
    ]

    for partner_model in ["claude-sonnet-4-6", "gpt-oss-120b-medium"]:
        tool_call_indices = {}
        native_state = GoogleNativeStreamAccumulator()
        all_chunks = []
        for ev in events:
            chunks = translate_stream_event(ev, partner_model, tool_call_indices, native_stream_state=native_state)
            all_chunks.extend(chunks)

        terminal = [c for c in all_chunks if c.choices[0].finish_reason][0]
        assert getattr(terminal.choices[0].delta, "reasoning_details", None) is None


def test_streaming_sync_fallback_forwards_carrier():
    from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient
    from unittest.mock import MagicMock, patch

    mock_client = GeminiCloudCodeClient(access_token="test_token")
    mock_http = MagicMock()

    # Mock 1: streamGenerateContent returns 400
    mock_resp_400 = MagicMock()
    mock_resp_400.status_code = 400
    mock_resp_400.text = "Streaming unsupported"
    mock_resp_400.headers = {}
    mock_resp_400.content = b"Streaming unsupported"
    mock_resp_400.iter_lines.return_value = []
    mock_http.stream.return_value.__enter__.return_value = mock_resp_400

    # Mock 2: fallback generateContent returns 200 with signed Gemini response
    raw_resp_200 = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "Fallback answer", "thoughtSignature": "sig_fallback_200"}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    }
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = raw_resp_200
    mock_http.post.return_value = mock_resp_200

    mock_client._http = mock_http

    chunks = list(mock_client.chat.completions.create(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    ))

    assert len(chunks) == 1
    chunk = chunks[0]
    delta = chunk.choices[0].delta
    assert delta.content == "Fallback answer"
    assert delta.reasoning_details is not None
    carrier = find_native_assistant_detail(delta.reasoning_details)
    assert carrier is not None
    assert carrier["content"]["parts"][0]["thoughtSignature"] == "sig_fallback_200"


def test_generic_stream_assembler_preserves_carrier_in_assistant_message():
    from agent.reasoning_summaries import append_streamed_reasoning_detail
    from agent.chat_completion_helpers import build_assistant_message
    from unittest.mock import MagicMock

    carrier = build_google_native_carrier(
        parts=[{"thoughtSignature": "sig_sse_1"}],
        source_model="gemini-3.8-flash-tiered",
    )

    # 1. Accumulate via production append_streamed_reasoning_detail
    reasoning_details = []
    append_streamed_reasoning_detail(reasoning_details, carrier)
    assert len(reasoning_details) == 1
    assert reasoning_details[0] == carrier

    # 2. Attach to final streaming message and pass to build_assistant_message
    assistant_msg = SimpleNamespace(
        role="assistant",
        content="Streaming completed",
        tool_calls=None,
        reasoning="thought",
        reasoning_content="thought",
        reasoning_details=reasoning_details,
    )

    mock_agent = MagicMock()
    mock_agent._needs_thinking_reasoning_pad.return_value = False
    mock_agent._extract_reasoning.return_value = None

    persisted = build_assistant_message(mock_agent, assistant_msg, finish_reason="stop")
    assert persisted["reasoning_details"] == [carrier]


def test_streaming_intake_to_session_db_persistence_roundtrip():
    from agent.reasoning_summaries import append_streamed_reasoning_detail
    from agent.chat_completion_helpers import build_assistant_message
    from agent.session_persistence import _db_flush_row
    from unittest.mock import MagicMock

    events = [
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "query", "args": {"id": 1}}, "thoughtSignature": "sig_stream_db"}]}}]},
        {"candidates": [{"finishReason": "tool_calls"}]},
    ]

    tool_call_indices = {}
    native_state = GoogleNativeStreamAccumulator()
    all_chunks = []
    for ev in events:
        chunks = translate_stream_event(ev, "gemini-3.8-flash-tiered", tool_call_indices, native_stream_state=native_state)
        all_chunks.extend(chunks)

    terminal = [c for c in all_chunks if c.choices[0].finish_reason][0]
    reasoning_details = []
    for d in terminal.choices[0].delta.reasoning_details:
        append_streamed_reasoning_detail(reasoning_details, d)

    assistant_msg = SimpleNamespace(
        role="assistant",
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_db_1",
                type="function",
                function=SimpleNamespace(name="query", arguments='{"id": 1}'),
                extra_content={"google": {"thought_signature": "sig_stream_db"}},
            )
        ],
        reasoning=None,
        reasoning_content=None,
        reasoning_details=reasoning_details,
    )

    mock_agent = MagicMock()
    mock_agent._needs_thinking_reasoning_pad.return_value = False
    mock_agent._extract_reasoning.return_value = None
    mock_agent._split_responses_tool_id.side_effect = lambda rid: (rid, None)
    mock_agent._derive_responses_function_call_id.return_value = None

    persisted_dict = build_assistant_message(mock_agent, assistant_msg, finish_reason="tool_calls")

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db = SessionDB(db_path)
        session_id = "test-stream-persist"
        db.create_session(session_id, source="cli")
        row = _db_flush_row(mock_agent, persisted_dict, is_current_turn_user=False)
        db.append_message(session_id, **row)

        db2 = SessionDB(db_path)
        loaded = db2.get_messages(session_id)
        assert len(loaded) == 1
        loaded_carrier = find_native_assistant_detail(loaded[0]["reasoning_details"])
        assert loaded_carrier is not None
        assert loaded_carrier["content"]["parts"][0]["thoughtSignature"] == "sig_stream_db"
def test_stream_accumulator_preserves_unknown_fields_across_function_snapshots():
    acc = GoogleNativeStreamAccumulator()

    acc.observe_part(
        {
            "functionCall": {
                "name": "tool",
                "args": {"x": 1},
                "id": "c1",
                "nativeCallMeta": {"v": 1},
            },
            "thoughtSignature": "sig",
            "nativePartMeta": {"stage": 1},
            "earlyOnlyField": {"init": True},
        },
        function_slot=0,
    )

    acc.observe_part(
        {
            "functionCall": {
                "name": "tool",
                "args": {"x": 2},
                "id": "c1",
                "nativeCallMeta": {"v": 2},
            },
            "nativePartMeta": {"stage": 2},
            "lateNativeField": {"ok": True},
            # earlyOnlyField omitted in frame 2
        },
        function_slot=0,
    )

    part = acc.build_carrier("gemini-3.8-flash-tiered")["content"]["parts"][0]

    assert part["functionCall"]["args"] == {"x": 2}
    assert part["functionCall"]["nativeCallMeta"] == {"v": 2}
    assert part["nativePartMeta"] == {"stage": 2}
    assert part["lateNativeField"] == {"ok": True}
    assert part["earlyOnlyField"] == {"init": True}  # Preserved across omission!
    assert part["thoughtSignature"] == "sig"  # Preserved across omission!
