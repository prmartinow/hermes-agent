"""Tests for Action Item 2, Milestone 3: Destination Capabilities & Non-Destructive Projection.

Verifies:
1. Destination-aware authority based on route and model capability (thought_circulation_support).
2. Verified Gemini models under gemini-oauth and gemini retain extra_content and google.native_assistant.
3. Partner models under gemini-oauth (Claude, GPT-OSS) strip Google replay state.
4. OpenRouter/Nous routes retain ordinary reasoning and extra_content but strip private google.native_assistant.
5. Strict and unrelated routes strip all Google replay state regardless of model name substrings.
6. SQLite-restored JSON string reasoning_details representations are parsed and projected safely.
7. Strict non-destructive invariant: source messages are never mutated.
"""

import copy
import json
import pytest

from agent.native_replay import build_google_native_carrier, find_native_assistant_detail
from agent.transports import get_transport
from providers import get_provider_profile


def _make_fixture_messages(reasoning_details_as_string: bool = False):
    carrier = build_google_native_carrier(
        parts=[
            {"functionCall": {"name": "query", "args": {"id": 1}}, "thoughtSignature": "sig_real_123"},
        ],
        source_model="gemini-3.8-flash-tiered",
    )
    ordinary_detail = {"type": "ordinary_reasoning", "content": "thought text"}
    foreign_carrier = {"type": "anthropic.native_assistant", "version": 1, "data": "foreign"}

    details_list = [carrier, ordinary_detail, foreign_carrier]
    details = json.dumps(details_list) if reasoning_details_as_string else details_list

    return [
        {"role": "user", "content": "run query"},
        {
            "role": "assistant",
            "content": "running tool",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "query", "arguments": '{"id": 1}'},
                    "extra_content": {
                        "thought_signature": "sig_real_123",
                        "google": {"thought_signature": "sig_real_123"},
                    },
                }
            ],
            "reasoning_details": details,
        },
    ]


# ============================================================================
# 1. Verified Gemini via gemini-oauth
# ============================================================================

@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.1-pro",
        "gemini-3.8-flash-high",  # legacy alias
    ],
)
def test_projection_verified_gemini_via_gemini_oauth(model):
    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini-oauth")

    messages = _make_fixture_messages()
    orig = copy.deepcopy(messages)

    wire = transport.convert_messages(
        messages,
        model=model,
        base_url=profile.base_url,
        provider_profile=profile,
    )

    # Source message must remain untouched
    assert messages == orig

    asst = wire[1]
    # Tool call extra_content retained
    assert "extra_content" in asst["tool_calls"][0]
    assert asst["tool_calls"][0]["extra_content"]["thought_signature"] == "sig_real_123"

    # reasoning_details: google.native_assistant retained, foreign carrier removed, ordinary retained
    assert "reasoning_details" in asst
    assert isinstance(asst["reasoning_details"], list)
    carrier = find_native_assistant_detail(asst["reasoning_details"])
    assert carrier is not None
    assert carrier["content"]["parts"][0]["thoughtSignature"] == "sig_real_123"
    assert any(d.get("type") == "ordinary_reasoning" for d in asst["reasoning_details"])
    assert not any(d.get("type") == "anthropic.native_assistant" for d in asst["reasoning_details"])


# ============================================================================
# 2. Cloud Code Partner Models under gemini-oauth
# ============================================================================

@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ],
)
def test_projection_cloud_code_partner_models_strip_google_replay(model):
    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini-oauth")

    messages = _make_fixture_messages()
    orig = copy.deepcopy(messages)

    wire = transport.convert_messages(
        messages,
        model=model,
        base_url=profile.base_url,
        provider_profile=profile,
    )

    assert messages == orig

    asst = wire[1]
    # Tool call extra_content strictly stripped
    assert "extra_content" not in asst["tool_calls"][0]

    # google.native_assistant carrier strictly stripped
    rds = asst.get("reasoning_details")
    if rds is not None:
        assert find_native_assistant_detail(rds) is None


# ============================================================================
# 3. Direct Gemini Provider (gemini)
# ============================================================================

def test_projection_direct_gemini_provider():
    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini")

    messages = _make_fixture_messages()

    # 1. Verified model (3.8) -> retained
    wire_38 = transport.convert_messages(
        messages,
        model="gemini-3.8-flash",
        base_url=profile.base_url,
        provider_profile=profile,
    )
    asst_38 = wire_38[1]
    assert "extra_content" in asst_38["tool_calls"][0]
    assert find_native_assistant_detail(asst_38["reasoning_details"]) is not None

    # 2. Unverified model (3.5) -> stripped conservatively
    wire_35 = transport.convert_messages(
        messages,
        model="gemini-3.5-flash",
        base_url=profile.base_url,
        provider_profile=profile,
    )
    asst_35 = wire_35[1]
    assert "extra_content" not in asst_35["tool_calls"][0]
    assert find_native_assistant_detail(asst_35.get("reasoning_details") or []) is None

    # 3. Synthetic future model (4.2) -> stripped conservatively
    wire_42 = transport.convert_messages(
        messages,
        model="gemini-4.2-flash",
        base_url=profile.base_url,
        provider_profile=profile,
    )
    asst_42 = wire_42[1]
    assert "extra_content" not in asst_42["tool_calls"][0]
    assert find_native_assistant_detail(asst_42.get("reasoning_details") or []) is None


# ============================================================================
# 4. OpenRouter / Nous Replay Behavior
# ============================================================================

def test_projection_openrouter_nous_behavior():
    transport = get_transport("chat_completions")

    messages = _make_fixture_messages()
    orig = copy.deepcopy(messages)

    # OpenRouter + google/gemini-3.8-flash:
    # Retains ordinary reasoning & extra_content; strips private google.native_assistant
    wire_or_gemini = transport.convert_messages(
        messages,
        model="google/gemini-3.8-flash",
        base_url="https://openrouter.ai/api/v1",
    )
    assert messages == orig
    asst_or_gemini = wire_or_gemini[1]
    assert "extra_content" in asst_or_gemini["tool_calls"][0]
    assert "reasoning_details" in asst_or_gemini
    assert any(d.get("type") == "ordinary_reasoning" for d in asst_or_gemini["reasoning_details"])
    assert find_native_assistant_detail(asst_or_gemini["reasoning_details"]) is None

    # OpenRouter + anthropic/claude-3.5-sonnet:
    # Strips extra_content & google carrier; retains ordinary reasoning
    wire_or_claude = transport.convert_messages(
        messages,
        model="anthropic/claude-3.5-sonnet",
        base_url="https://openrouter.ai/api/v1",
    )
    asst_or_claude = wire_or_claude[1]
    assert "extra_content" not in asst_or_claude["tool_calls"][0]
    assert find_native_assistant_detail(asst_or_claude.get("reasoning_details") or []) is None
    assert any(d.get("type") == "ordinary_reasoning" for d in asst_or_claude.get("reasoning_details") or [])


# ============================================================================
# 5. Strict Route & Substring False-Positive Guard
# ============================================================================

@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-flash",  # Verified model on unverified/strict endpoint
        "acme-gemini-proxy", # Model with 'gemini' substring
        "my-gemini-model",
        "gemma-foo",
    ],
)
def test_projection_strict_route_false_positive_guard(model):
    transport = get_transport("chat_completions")

    messages = _make_fixture_messages()
    wire = transport.convert_messages(
        messages,
        model=model,
        base_url="https://strict.example.invalid/v1",
        provider_profile=None,
    )

    asst = wire[1]
    assert "extra_content" not in asst["tool_calls"][0]
    # Strict route drops reasoning_details entirely
    assert "reasoning_details" not in asst


# ============================================================================
# 6. SQLite-Restored JSON String Representation & Non-Destructive Invariant
# ============================================================================

def test_projection_sqlite_restored_json_string_representation():
    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini-oauth")

    # Stored in SQLite as raw JSON text
    messages = _make_fixture_messages(reasoning_details_as_string=True)
    orig = copy.deepcopy(messages)

    # 1. Project to Gemini OAuth
    wire_gemini = transport.convert_messages(
        messages,
        model="gemini-3.8-flash",
        base_url=profile.base_url,
        provider_profile=profile,
    )
    # Output must be a list containing google carrier
    asst_g = wire_gemini[1]
    assert isinstance(asst_g["reasoning_details"], list)
    assert find_native_assistant_detail(asst_g["reasoning_details"]) is not None
    assert "extra_content" in asst_g["tool_calls"][0]

    # 2. Project to OpenRouter
    wire_or = transport.convert_messages(
        messages,
        model="google/gemini-3.8-flash",
        base_url="https://openrouter.ai/api/v1",
    )
    asst_or = wire_or[1]
    assert isinstance(asst_or["reasoning_details"], list)
    assert find_native_assistant_detail(asst_or["reasoning_details"]) is None
    assert "extra_content" in asst_or["tool_calls"][0]

    # 3. Project to strict route
    wire_strict = transport.convert_messages(
        messages,
        model="claude-sonnet-4-6",
        base_url="https://strict.example.invalid/v1",
    )
    asst_s = wire_strict[1]
    assert "reasoning_details" not in asst_s
    assert "extra_content" not in asst_s["tool_calls"][0]

    # CRITICAL INVARIANT: source message must remain 100% byte-identical
    assert messages == orig
    assert isinstance(messages[1]["reasoning_details"], str)  # Untouched JSON string
def test_projection_gemini_openai_compat_does_not_receive_native_replay():
    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini")
    messages = _make_fixture_messages()
    original = copy.deepcopy(messages)

    wire = transport.convert_messages(
        messages,
        model="gemini-3.8-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_profile=profile,
    )

    assistant = wire[1]
    assert "extra_content" not in assistant["tool_calls"][0]
    assert "reasoning_details" not in assistant
    assert messages == original
