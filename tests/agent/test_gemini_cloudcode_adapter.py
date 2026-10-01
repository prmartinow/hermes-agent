"""Tests for Gemini Cloud Code adapter routing through canonical resolver.

Verifies:
- Canonical routing: Gemini 3.8/3.7 -> -tiered wire model with thinkingLevel; Gemini 3.6/3.1 -> static wire slugs.
- Strict negative validation before HTTP request (3.8 + max, 3.1 + medium).
- Input source parity (extra_body.effort, extra_body.reasoning_effort, kwargs.effort, kwargs.reasoning_effort, thinkingLevel).
- Conflict rejection between scalar effort and thinkingLevel.
- Legacy equivalence (gemini-3.8-flash-high == gemini-3.8-flash + effort=high).
- Pass-through preservation for unverified/future models without regex guessing.
- Real provider-profile forwarding path via GeminiOAuthProfile.build_api_kwargs_extras.
- countTokens routing through the same canonical resolver.
"""

from unittest.mock import MagicMock, patch
import pytest

from agent.gemini_cloudcode_adapter import (
    GeminiCloudCodeClient,
    resolve_cloudcode_model_and_effort,
)
from agent.gemini_cloudcode_models import EffortUnsupportedError
from providers import get_provider_profile


def _make_mock_client():
    mock_http = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "response": {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
            "totalTokens": 42,
        }
    }
    mock_http.post.return_value = mock_resp
    client = GeminiCloudCodeClient(
        access_token="ya29.test_token",
        http_client=mock_http,
    )
    return client, mock_http


# ============================================================================
# 1. Canonical Routing Tests
# ============================================================================

@pytest.mark.parametrize("version", ["3.7", "3.8"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_cloudcode_client_dynamic_tier_routing(version, effort):
    client, mock_http = _make_mock_client()
    base_model = f"gemini-{version}-flash"

    client.chat.completions.create(
        model=base_model,
        messages=[{"role": "user", "content": "Hello"}],
        extra_body={"effort": effort},
    )

    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == f"gemini-{version}-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": effort,
        "includeThoughts": True,
    }


@pytest.mark.parametrize(
    "effort, expected_wire",
    [
        ("low", "gemini-3.6-flash-low"),
        ("medium", "gemini-3.6-flash-medium"),
        ("high", "gemini-3.6-flash-high"),
    ],
)
def test_cloudcode_client_static_tier_36_routing(effort, expected_wire):
    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model="gemini-3.6-flash",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body={"effort": effort},
    )

    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == expected_wire
    assert "thinkingConfig" not in sent_body["request"]["generationConfig"]


@pytest.mark.parametrize(
    "effort, expected_wire",
    [
        ("low", "gemini-3.1-pro-low"),
        ("high", "gemini-pro-agent"),
    ],
)
def test_cloudcode_client_static_tier_31_pro_routing(effort, expected_wire):
    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model="gemini-3.1-pro",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body={"effort": effort},
    )

    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == expected_wire
    assert "thinkingConfig" not in sent_body["request"]["generationConfig"]


# ============================================================================
# 2. Strict Negative Cases (Fail before HTTP)
# ============================================================================

def test_cloudcode_client_rejects_max_effort_locally():
    client, mock_http = _make_mock_client()
    with pytest.raises(EffortUnsupportedError) as exc_info:
        client.chat.completions.create(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "Hello"}],
            extra_body={"effort": "max"},
        )
    assert "gemini-3.8-flash has no 'max' effort" in str(exc_info.value)
    mock_http.post.assert_not_called()


def test_cloudcode_client_rejects_31_pro_medium_effort_locally():
    client, mock_http = _make_mock_client()
    with pytest.raises(EffortUnsupportedError) as exc_info:
        client.chat.completions.create(
            model="gemini-3.1-pro",
            messages=[{"role": "user", "content": "Hello"}],
            extra_body={"effort": "medium"},
        )
    assert "gemini-3.1-pro has no 'medium' effort" in str(exc_info.value)
    mock_http.post.assert_not_called()


# ============================================================================
# 3. Input Source Parity Tests
# ============================================================================

@pytest.mark.parametrize(
    "call_kwargs",
    [
        {"extra_body": {"effort": "medium"}},
        {"extra_body": {"reasoning_effort": "medium"}},
        {"effort": "medium"},
        {"reasoning_effort": "medium"},
        {"extra_body": {"thinking_config": {"thinkingLevel": "medium"}}},
    ],
)
def test_cloudcode_client_input_source_parity(call_kwargs):
    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Hello"}],
        **call_kwargs,
    )

    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == "gemini-3.8-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "medium",
        "includeThoughts": True,
    }


def test_cloudcode_client_rejects_conflicting_effort_and_thinking_level():
    client, mock_http = _make_mock_client()
    with pytest.raises(EffortUnsupportedError) as exc_info:
        client.chat.completions.create(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "Hello"}],
            extra_body={
                "effort": "low",
                "thinking_config": {"thinkingLevel": "high"},
            },
        )
    assert "Conflicting effort 'low' and thinkingLevel 'high'" in str(exc_info.value)
    mock_http.post.assert_not_called()


# ============================================================================
# 4. Legacy Equivalence Tests
# ============================================================================

def test_cloudcode_client_legacy_alias_payload_equivalence():
    client_alias, mock_http_alias = _make_mock_client()
    client_canon, mock_http_canon = _make_mock_client()

    client_alias.chat.completions.create(
        model="gemini-3.8-flash-high",
        messages=[{"role": "user", "content": "Hello"}],
    )
    client_canon.chat.completions.create(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body={"effort": "high"},
    )

    body_alias = mock_http_alias.post.call_args[1]["json"]
    body_canon = mock_http_canon.post.call_args[1]["json"]

    assert body_alias["model"] == body_canon["model"] == "gemini-3.8-flash-tiered"
    assert (
        body_alias["request"]["generationConfig"]["thinkingConfig"]
        == body_canon["request"]["generationConfig"]["thinkingConfig"]
        == {"thinkingLevel": "high", "includeThoughts": True}
    )


# ============================================================================
# 5. No Future Regex Guessing (Passthrough Verification)
# ============================================================================

@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-4.2-ultra-tiered",
        "gemini-10.0-flash-high",
        "gemini-3.8.1-flash-high",
        "gemini-3.8-flash-preview-high",
        "gemini-3.5-flash-extra-low",
    ],
)
def test_cloudcode_client_future_models_passthrough_verbatim(model_id):
    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model=model_id,
        messages=[{"role": "user", "content": "Hello"}],
    )

    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == model_id


# ============================================================================
# 6. Real Provider-Profile Forwarding Path
# ============================================================================

def test_gemini_oauth_profile_build_api_kwargs_extras():
    profile = get_provider_profile("gemini-oauth")
    assert profile is not None

    # Canonical base model forwards selected effort into extra_body
    extra_body, _ = profile.build_api_kwargs_extras(
        model="gemini-3.8-flash",
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    assert extra_body == {"effort": "medium"}

    # End-to-end routing with client
    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body=extra_body,
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == "gemini-3.8-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "medium"


def test_gemini_oauth_profile_preserves_legacy_alias_embedded_effort():
    profile = get_provider_profile("gemini-oauth")
    # Legacy alias like gemini-3.8-flash-low must NOT be overridden by global reasoning_config
    extra_body, _ = profile.build_api_kwargs_extras(
        model="gemini-3.8-flash-low",
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    assert extra_body == {}

    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model="gemini-3.8-flash-low",
        messages=[{"role": "user", "content": "Hello"}],
        extra_body=extra_body,
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == "gemini-3.8-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"


# ============================================================================
# 7. countTokens Routing Tests
# ============================================================================

def test_count_tokens_routes_wire_model_with_effort():
    client, mock_http = _make_mock_client()

    tokens = client.count_tokens(
        model="gemini-3.6-flash",
        contents="Hello world",
        effort="medium",
    )
    assert tokens == 42
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["request"]["model"] == "gemini-3.6-flash-medium"


def test_count_tokens_dynamic_tier():
    client, mock_http = _make_mock_client()

    client.count_tokens(
        model="gemini-3.8-flash",
        contents="Hello world",
        effort="low",
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["request"]["model"] == "gemini-3.8-flash-tiered"


def test_count_tokens_unsupported_effort_fails_before_http():
    client, mock_http = _make_mock_client()
    with pytest.raises(EffortUnsupportedError):
        client.count_tokens(
            model="gemini-3.1-pro",
            contents="Hello world",
            effort="medium",
        )
    mock_http.post.assert_not_called()
# ============================================================================
# 8. Milestone 3 Amendment: countTokens Input Parity & Production Transport Seam
# ============================================================================

def test_count_tokens_accepts_extra_body_effort():
    client, mock_http = _make_mock_client()
    client.count_tokens(
        model="gemini-3.6-flash",
        contents="hello",
        extra_body={"effort": "medium"},
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["request"]["model"] == "gemini-3.6-flash-medium"


def test_count_tokens_accepts_extra_body_reasoning_effort():
    client, mock_http = _make_mock_client()
    client.count_tokens(
        model="gemini-3.6-flash",
        contents="hello",
        extra_body={"reasoning_effort": "medium"},
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["request"]["model"] == "gemini-3.6-flash-medium"


def test_count_tokens_accepts_thinking_level_in_thinking_config():
    client, mock_http = _make_mock_client()
    client.count_tokens(
        model="gemini-3.6-flash",
        contents="hello",
        extra_body={
            "thinking_config": {
                "thinkingLevel": "medium",
            }
        },
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["request"]["model"] == "gemini-3.6-flash-medium"


def test_production_transport_profile_build_kwargs_and_client_routing():
    from agent.transports import get_transport

    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini-oauth")
    assert profile is not None

    kw = transport.build_kwargs(
        provider_profile=profile,
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        base_url=profile.base_url,
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    assert kw["extra_body"]["effort"] == "medium"

    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model=kw["model"],
        messages=kw["messages"],
        extra_body=kw["extra_body"],
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == "gemini-3.8-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "medium"


def test_production_transport_legacy_alias_not_overridden_by_reasoning_config():
    from agent.transports import get_transport

    transport = get_transport("chat_completions")
    profile = get_provider_profile("gemini-oauth")

    kw_legacy = transport.build_kwargs(
        provider_profile=profile,
        model="gemini-3.8-flash-low",
        messages=[{"role": "user", "content": "hi"}],
        base_url=profile.base_url,
        reasoning_config={"enabled": True, "effort": "medium"},
    )
    # Suffix alias embedded effort must NOT be overridden by global reasoning_config
    assert "effort" not in kw_legacy.get("extra_body", {})

    client, mock_http = _make_mock_client()
    client.chat.completions.create(
        model=kw_legacy["model"],
        messages=kw_legacy["messages"],
        extra_body=kw_legacy.get("extra_body"),
    )
    sent_body = mock_http.post.call_args[1]["json"]
    assert sent_body["model"] == "gemini-3.8-flash-tiered"
    assert sent_body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
# ============================================================================
# 9. Milestone 4: Generation/countTokens Parity Matrix & Legacy Equivalence Closure
# ============================================================================

from agent.gemini_cloudcode_models import LEGACY_MODEL_ALIASES, model_for_base_effort


@pytest.mark.parametrize(
    "base, effort",
    [
        ("gemini-3.8-flash", "low"),
        ("gemini-3.8-flash", "medium"),
        ("gemini-3.8-flash", "high"),
        ("gemini-3.7-flash", "low"),
        ("gemini-3.7-flash", "medium"),
        ("gemini-3.7-flash", "high"),
        ("gemini-3.6-flash", "low"),
        ("gemini-3.6-flash", "medium"),
        ("gemini-3.6-flash", "high"),
        ("gemini-3.1-pro", "low"),
        ("gemini-3.1-pro", "high"),
    ],
)
def test_generation_and_count_tokens_route_parity_matrix(base, effort):
    client_gen, mock_http_gen = _make_mock_client()
    client_count, mock_http_count = _make_mock_client()

    client_gen.chat.completions.create(
        model=base,
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"effort": effort},
    )
    client_count.count_tokens(
        model=base,
        contents="hi",
        extra_body={"effort": effort},
    )

    gen_body = mock_http_gen.post.call_args[1]["json"]
    count_body = mock_http_count.post.call_args[1]["json"]
    expected_resolved = model_for_base_effort(base, effort)

    assert gen_body["model"] == count_body["request"]["model"] == expected_resolved.wire_model
    if expected_resolved.thinking_config:
        assert gen_body["request"]["generationConfig"]["thinkingConfig"] == expected_resolved.thinking_config
    else:
        assert "thinkingConfig" not in gen_body["request"]["generationConfig"]


@pytest.mark.parametrize("alias", list(LEGACY_MODEL_ALIASES.keys()))
def test_legacy_alias_full_wire_equivalence_closure(alias):
    base, effort = LEGACY_MODEL_ALIASES[alias]

    client_alias, mock_http_alias = _make_mock_client()
    client_canon, mock_http_canon = _make_mock_client()

    client_alias.chat.completions.create(
        model=alias,
        messages=[{"role": "user", "content": "hi"}],
    )
    client_canon.chat.completions.create(
        model=base,
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"effort": effort},
    )

    alias_body = mock_http_alias.post.call_args[1]["json"]
    canon_body = mock_http_canon.post.call_args[1]["json"]

    assert alias_body["model"] == canon_body["model"]
    assert alias_body["request"]["generationConfig"].get("thinkingConfig") == canon_body["request"]["generationConfig"].get("thinkingConfig")


@pytest.mark.parametrize(
    "reasoning_cfg",
    [
        {"enabled": False},
        {"enabled": True, "effort": "none"},
    ],
)
def test_reasoning_disabled_does_not_inject_extra_body_effort(reasoning_cfg):
    profile = get_provider_profile("gemini-oauth")
    extra_body, _ = profile.build_api_kwargs_extras(
        model="gemini-3.8-flash",
        reasoning_config=reasoning_cfg,
    )
    assert "effort" not in extra_body
