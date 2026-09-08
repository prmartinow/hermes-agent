"""Tests for Google Gemini (Antigravity) OAuth provider and adapter.

Covers:
- Plugin discovery and profile definition (GeminiOAuthProfile)
- URL classification (is_cloudcode_pa_base_url)
- Model prefix stripping and reasoning level mapping
- CaGenerateContentRequest wrapping and SSE unwrapping
- Antigravity token read, write, and normalization
- Token expiry detection and OAuth token refresh
- Credential pool auto-seeding and removal
- Unified provider catalog and Web Dashboard status resolution
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.gemini_cloudcode_adapter import (
    DEFAULT_CLOUDCODE_PA_BASE_URL,
    GeminiCloudCodeClient,
    is_cloudcode_pa_base_url,
)
from agent.gemini_native_adapter import bare_gemini_model_id
from hermes_cli.auth import (
    DEFAULT_GEMINI_OAUTH_CLIENT_ID,
    DEFAULT_GEMINI_OAUTH_CLIENT_SECRET,
    PROVIDER_REGISTRY,
    AuthError,
    _extract_gemini_oauth_credentials_from_agy,
    _gemini_access_token_is_expiring,
    _read_antigravity_tokens,
    _refresh_gemini_oauth_tokens,
    _save_antigravity_tokens,
    get_auth_status,
    get_gemini_oauth_auth_status,
    resolve_gemini_oauth_runtime_credentials,
)
from providers import get_provider_profile, list_providers


# ---------------------------------------------------------------------------
# 1. Plugin Registration & Profile Discovery
# ---------------------------------------------------------------------------

def test_gemini_oauth_profile_discovery():
    profile = get_provider_profile("gemini-oauth")
    assert profile is not None
    assert profile.name == "gemini-oauth"
    assert profile.display_name == "Google Gemini (OAuth / Antigravity)"
    assert profile.auth_type == "oauth_external"
    assert "gemini-3.6-flash-low" in profile.fallback_models
    assert profile.supports_vision is True
    assert profile.supports_health_check is False


def test_gemini_oauth_in_provider_registry():
    pconfig = PROVIDER_REGISTRY.get("gemini-oauth")
    assert pconfig is not None
    assert pconfig.auth_type == "oauth_external"
    assert pconfig.inference_base_url == DEFAULT_CLOUDCODE_PA_BASE_URL


# ---------------------------------------------------------------------------
# 2. URL Classification & Model ID Mapping
# ---------------------------------------------------------------------------

def test_is_cloudcode_pa_base_url():
    assert is_cloudcode_pa_base_url("https://cloudcode-pa.googleapis.com/v1internal") is True
    assert is_cloudcode_pa_base_url("https://cloudcode-pa.googleapis.com/v1internal/") is True
    assert is_cloudcode_pa_base_url("https://generativelanguage.googleapis.com/v1beta") is False
    assert is_cloudcode_pa_base_url("https://api.openai.com/v1") is False


def test_bare_gemini_model_id_stripping():
    assert bare_gemini_model_id("gemini-oauth/gemini-3.6-flash-low") == "gemini-3.6-flash-low"
    assert bare_gemini_model_id("gemini_oauth/gemini-3.6-flash-low") == "gemini-3.6-flash-low"
    assert bare_gemini_model_id("google/gemini-2.5-pro") == "gemini-2.5-pro"
    assert bare_gemini_model_id("gemini/gemini-3.6-flash") == "gemini-3.6-flash"
    assert bare_gemini_model_id("gemini-3.6-flash-low") == "gemini-3.6-flash-low"


def test_gemini_cloudcode_client_model_mapping():
    client = GeminiCloudCodeClient(access_token="mock_token")
    assert client._map_model_id("gemini-oauth/gemini-3.6-flash-low") == "gemini-3.6-flash-low"
    assert client._map_model_id("gemini-1/gemini-3.6-flash-high") == "gemini-3.6-flash-high"
    assert client._map_model_id("gemini-3.6-flash-medium") == "gemini-3.6-flash-medium"
    assert client._map_model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 3. Client Construction & Payload Wrapping
# ---------------------------------------------------------------------------

def test_gemini_cloudcode_client_requires_token():
    with pytest.raises(RuntimeError, match="requires an OAuth access token"):
        GeminiCloudCodeClient(access_token="")


def test_gemini_cloudcode_client_generate_content_wrapping():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Hello world!"}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        }
    }
    mock_http.post.return_value = mock_resp

    client = GeminiCloudCodeClient(access_token="test_token_xyz", http_client=mock_http)
    res = client.chat.completions.create(
        model="gemini-oauth/gemini-3.6-flash-low",
        messages=[{"role": "user", "content": "Hi"}],
        stream=False,
    )

    assert len(res.choices) == 1
    assert res.choices[0].message.content == "Hello world!"
    assert res.usage.total_tokens == 15

    # Check request payload wrapping
    call_args = mock_http.post.call_args
    assert call_args is not None
    posted_json = call_args[1]["json"]
    assert posted_json["model"] == "gemini-3.6-flash-low"
    assert "user_prompt_id" in posted_json
    assert "request" in posted_json
    assert "contents" in posted_json["request"]


# ---------------------------------------------------------------------------
# 4. Token Storage, Expiry & Normalization
# ---------------------------------------------------------------------------

def test_read_antigravity_tokens_agy_nested_structure(tmp_path):
    token_file = tmp_path / "antigravity-oauth-token"
    agy_payload = {
        "token": {
            "access_token": "ya29.nested_access_token",
            "refresh_token": "1//nested_refresh_token",
            "token_type": "Bearer",
            "expiry": "2026-08-18T22:53:35Z",
        },
        "email": "test@example.com",
        "name": "Test User",
        "auth_method": "consumer",
    }
    token_file.write_text(json.dumps(agy_payload), encoding="utf-8")

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}):
        normalized = _read_antigravity_tokens()
        assert normalized["access_token"] == "ya29.nested_access_token"
        assert normalized["refresh_token"] == "1//nested_refresh_token"
        assert normalized["email"] == "test@example.com"
        assert normalized["name"] == "Test User"


def test_gemini_access_token_is_expiring():
    # Expired timestamp (ISO string in past)
    assert _gemini_access_token_is_expiring("2020-01-01T00:00:00Z") is True
    # Future timestamp (ISO string 10 hours ahead)
    assert _gemini_access_token_is_expiring("2030-01-01T00:00:00Z") is False
    # None or empty
    assert _gemini_access_token_is_expiring(None) is False


def test_refresh_gemini_oauth_tokens(tmp_path):
    token_file = tmp_path / "antigravity-oauth-token"
    initial = {
        "token": {
            "access_token": "old_access_token",
            "refresh_token": "valid_refresh_token",
            "expiry": "2020-01-01T00:00:00Z",
        }
    }
    token_file.write_text(json.dumps(initial), encoding="utf-8")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "ya29.new_fresh_access_token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}), \
         patch("httpx.post", return_value=mock_resp):
        tokens = _read_antigravity_tokens()
        refreshed = _refresh_gemini_oauth_tokens(tokens)
        assert refreshed["access_token"] == "ya29.new_fresh_access_token"
        assert refreshed["refresh_token"] == "valid_refresh_token"

        # Verify saved back to disk
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved["token"]["access_token"] == "ya29.new_fresh_access_token"


# ---------------------------------------------------------------------------
# 5. Credential Pool & Auth Status
# ---------------------------------------------------------------------------

def test_resolve_gemini_oauth_runtime_credentials(tmp_path):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "token": {
            "access_token": "ya29.valid_token",
            "refresh_token": "1//refresh_token",
            "expiry": "2030-01-01T00:00:00Z",
        },
        "email": "user@google.com",
    }), encoding="utf-8")

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}):
        creds = resolve_gemini_oauth_runtime_credentials(refresh_if_expiring=False)
        assert creds["provider"] == "gemini-oauth"
        assert creds["api_key"] == "ya29.valid_token"
        assert creds["email"] == "user@google.com"
        assert creds["source"] == "antigravity_cli"


def test_get_gemini_oauth_auth_status_logged_in(tmp_path):
    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "token": {
            "access_token": "ya29.valid_token",
            "refresh_token": "1//refresh_token",
            "expiry": "2030-01-01T00:00:00Z",
        },
        "email": "user@google.com",
    }), encoding="utf-8")

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}), \
         patch("hermes_cli.auth.fetch_gemini_quota_summary", return_value={}):
        status = get_auth_status("gemini-oauth")
        assert status["logged_in"] is True
        assert status["email"] == "user@google.com"
        assert status["api_key"] == "ya29.valid_token"
        assert status["has_refresh_token"] is True


def test_credential_pool_seeding_for_gemini_oauth(tmp_path):
    from agent.credential_pool import load_pool

    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "token": {
            "access_token": "ya29.pool_test_token",
            "refresh_token": "1//refresh_token",
            "expiry": "2030-01-01T00:00:00Z",
        },
        "email": "pool_user@google.com",
    }), encoding="utf-8")

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}):
        pool = load_pool("gemini-oauth")
        entries = pool.entries()
        assert len(entries) >= 1
        entry = entries[0]
        assert entry.provider == "gemini-oauth"
        assert entry.access_token == "ya29.pool_test_token"
        assert entry.source in {"antigravity_cli", "gemini_account_1"}
        assert entry.label == "pool_user@google.com"


# ---------------------------------------------------------------------------
# 6. Unified Provider Catalog & Web Server Integration
# ---------------------------------------------------------------------------

def test_provider_catalog_includes_gemini_oauth_on_accounts_tab():
    from hermes_cli.provider_catalog import provider_catalog

    catalog = provider_catalog()
    gemini_entry = next((p for p in catalog if p.slug == "gemini-oauth"), None)
    assert gemini_entry is not None
    assert gemini_entry.tab == "accounts"
    assert gemini_entry.auth_type == "oauth_external"


def test_web_server_oauth_catalog_resolves_gemini_oauth_status(tmp_path):
    from hermes_cli.web_routers.oauth import _build_oauth_catalog, _resolve_provider_status

    token_file = tmp_path / "antigravity-oauth-token"
    token_file.write_text(json.dumps({
        "token": {
            "access_token": "ya29.web_status_token",
            "refresh_token": "1//refresh_token",
            "expiry": "2030-01-01T00:00:00Z",
        },
        "email": "dashboard_user@google.com",
    }), encoding="utf-8")

    mock_auth_file = tmp_path / "auth.json"
    mock_auth_file.write_text(json.dumps({"providers": {}}), encoding="utf-8")

    with patch("hermes_cli.auth._antigravity_token_path", return_value=token_file), \
         patch("hermes_cli.auth._auth_file_path", return_value=mock_auth_file), \
         patch("hermes_cli.auth._load_auth_store", return_value={"providers": {}}):
        rows = _build_oauth_catalog()
        row = next((r for r in rows if r["id"] == "gemini-oauth"), None)
        assert row is not None
        assert row["flow"] == "pkce"

        status = _resolve_provider_status("gemini-oauth", row.get("status_fn"))
        assert status["logged_in"] is True
        assert "accounts" in status
        assert len(status["accounts"]) == 5
        assert status["accounts"][0]["logged_in"] is True
        assert status["accounts"][0]["email"] == "dashboard_user@google.com"


def test_clear_provider_auth_for_gemini_accounts(tmp_path):
    from hermes_cli.auth import clear_provider_auth, _load_auth_store, _save_auth_store

    mock_auth_file = tmp_path / "auth.json"
    initial_store = {
        "active_provider": "gemini-oauth",
        "providers": {
            "gemini-oauth": {"access_token": "tok1", "refresh_token": "ref1"},
            "gemini-oauth-2": {"access_token": "tok2", "refresh_token": "ref2"},
        },
        "credential_pool": {
            "gemini-oauth": [{"access_token": "tok1"}],
            "gemini-oauth-2": [{"access_token": "tok2"}],
        },
    }
    mock_auth_file.write_text(json.dumps(initial_store), encoding="utf-8")

    with patch("hermes_cli.auth._auth_file_path", return_value=mock_auth_file):
        cleared = clear_provider_auth("gemini-1")
        assert cleared is True
        store = _load_auth_store()
        assert "gemini-oauth" not in store.get("providers", {})
        assert "gemini-1" not in store.get("providers", {})
        assert "gemini-oauth" not in store.get("credential_pool", {})
        # gemini-2 should remain intact
        assert "gemini-oauth-2" in store.get("providers", {})


# ---------------------------------------------------------------------------
# 7. Multi-Account (gemini-1 .. gemini-5) & Quota Summary Tests
# ---------------------------------------------------------------------------

def test_multi_account_token_resolution(tmp_path):
    from hermes_cli.auth import _read_gemini_account_tokens, get_auth_status

    mock_auth_store = {
        "providers": {
            "gemini-oauth": {
                "access_token": "ya29.acc1",
                "refresh_token": "1//ref1",
                "email": "acc1@google.com",
            },
            "gemini-oauth-2": {
                "access_token": "ya29.acc2",
                "refresh_token": "1//ref2",
                "email": "acc2@google.com",
            },
            "gemini-oauth-3": {
                "access_token": "ya29.acc3",
                "refresh_token": "1//ref3",
                "email": "acc3@google.com",
            },
        }
    }

    with patch("hermes_cli.auth._load_auth_store", return_value=mock_auth_store):
        tok1 = _read_gemini_account_tokens(1)
        assert tok1["access_token"] == "ya29.acc1"
        assert tok1["email"] == "acc1@google.com"

        tok2 = _read_gemini_account_tokens(2)
        assert tok2["access_token"] == "ya29.acc2"
        assert tok2["email"] == "acc2@google.com"

        tok3 = _read_gemini_account_tokens(3)
        assert tok3["access_token"] == "ya29.acc3"
        assert tok3["email"] == "acc3@google.com"

        st2 = get_auth_status("gemini-2")
        assert st2["logged_in"] is True
        assert st2["email"] == "acc2@google.com"


def test_format_gemini_quota_summary():
    from hermes_cli.auth import format_gemini_quota_summary

    raw_quota = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "window": "weekly",
                        "resetTime": "2026-08-25T13:54:47Z",
                        "remainingFraction": 0.89870787,
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "window": "5h",
                        "resetTime": "2026-08-18T18:54:47Z",
                        "remainingFraction": 0.4008172,
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "window": "weekly",
                        "resetTime": "2026-08-25T15:40:14Z",
                        "remainingFraction": 1.0,
                    },
                    {
                        "bucketId": "3p-5h",
                        "window": "5h",
                        "resetTime": "2026-08-18T20:40:14Z",
                        "remainingFraction": 1.0,
                    },
                ],
            },
        ],
    }

    parsed = format_gemini_quota_summary(raw_quota)
    assert parsed["gemini_5h_percent"] == 40.1
    assert parsed["gemini_5h_reset"] == "2026-08-18T18:54:47Z"
    assert parsed["gemini_weekly_percent"] == 89.9
    assert parsed["gemini_weekly_reset"] == "2026-08-25T13:54:47Z"
    assert parsed["claude_5h_percent"] == 100.0
    assert parsed["claude_weekly_percent"] == 100.0


def test_has_gemini_oauth_credentials_and_resolution(tmp_path):
    from hermes_cli.auth import has_gemini_oauth_credentials, _save_gemini_account_tokens
    from agent.auxiliary_client import resolve_provider_client
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    tokens = {
        "access_token": "ya29.test_token",
        "refresh_token": "1//refresh",
        "expiry": "2030-01-01T00:00:00Z",
        "email": "test@google.com",
    }
    with patch("hermes_cli.auth._auth_file_path", return_value=tmp_path / "auth.json"), \
         patch("agent.auxiliary_client.get_hermes_home", return_value=tmp_path):
        _save_gemini_account_tokens(1, tokens)
        assert has_gemini_oauth_credentials(1) is True
        assert has_gemini_oauth_credentials("gemini-1") is True

        client, model = resolve_provider_client("gemini-1", model="gemini-3.6-flash-low")
        assert client is not None
        assert model == "gemini-3.6-flash-low"
        assert client.api_key == "ya29.test_token"


def test_get_quota_for_gemini_model_association():
    from hermes_cli.auth import get_quota_for_gemini_model

    raw_quota = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "window": "weekly",
                        "resetTime": "2026-08-25T13:54:47Z",
                        "remainingFraction": 0.8991234,
                    },
                    {
                        "window": "5h",
                        "resetTime": "2026-08-18T18:54:47Z",
                        "remainingFraction": 0.4008172,
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "window": "weekly",
                        "resetTime": "2026-08-25T15:40:14Z",
                        "remainingFraction": 1.0,
                    },
                    {
                        "window": "5h",
                        "resetTime": "2026-08-18T20:40:14Z",
                        "remainingFraction": 0.75,
                    },
                ],
            },
        ],
    }

    # Gemini model routes to Gemini Models group
    gemini_q = get_quota_for_gemini_model("gemini-3.6-flash-high", raw_quota)
    assert gemini_q["group_name"] == "Gemini Models"
    assert gemini_q["weekly_pct"] == 89.9
    assert gemini_q["five_hour_pct"] == 40.1

    # Claude and GPT-OSS models route to Claude and GPT models group
    claude_q = get_quota_for_gemini_model("claude-sonnet-4-6", raw_quota)
    assert claude_q["group_name"] == "Claude and GPT models"
    assert claude_q["weekly_pct"] == 100.0
    assert claude_q["five_hour_pct"] == 75.0

    gpt_q = get_quota_for_gemini_model("gpt-oss-120b-medium", raw_quota)
    assert gpt_q["group_name"] == "Claude and GPT models"
    assert gpt_q["weekly_pct"] == 100.0
    assert gpt_q["five_hour_pct"] == 75.0


def test_fetch_gemini_available_models_and_caching():
    from hermes_cli.auth import (
        fetch_gemini_available_models,
        get_gemini_model_display_names,
        _GEMINI_MODELS_CACHE,
    )

    mock_resp = {
        "agentModelSorts": [
            {
                "groups": [
                    {
                        "modelIds": [
                            "gemini-3.6-flash-high",
                            "gemini-3.6-flash-medium",
                            "claude-sonnet-4-6",
                        ]
                    }
                ]
            }
        ],
        "models": {
            "gemini-3.6-flash-high": {"displayName": "Gemini 3.6 Flash (High)"},
            "gemini-3.6-flash-medium": {"displayName": "Gemini 3.6 Flash (Medium)"},
            "claude-sonnet-4-6": {"displayName": "Claude Sonnet 4.6 (Thinking)"},
            "chat_internal": {"displayName": None},
            "tab_helper": {},
        },
    }

    with patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials") as mock_creds, \
         patch("httpx.post") as mock_post:
        mock_creds.return_value = {"access_token": "ya29.test"}
        mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_resp)

        _GEMINI_MODELS_CACHE.clear()
        models = fetch_gemini_available_models(account=1, force=True)
        assert "gemini-3.6-flash-high" in models
        assert "claude-sonnet-4-6" in models
        assert "chat_internal" not in models
        assert "tab_helper" not in models

        # Check display names
        dnames = get_gemini_model_display_names(account=1)
        assert dnames["gemini-3.6-flash-high"] == "Gemini 3.6 Flash (High)"
        assert dnames["claude-sonnet-4-6"] == "Claude Sonnet 4.6 (Thinking)"


# ---------------------------------------------------------------------------
# 10. Dynamic Opportunity-Cost Index (DOCI) & Optimal Rotation Tests
# ---------------------------------------------------------------------------


def test_fetch_gemini_38_flash_tiered_dynamic_expansion():
    """Verify gemini-3.8-flash-tiered with supportsThinking=True is dynamically expanded into 3 virtual tiers."""
    from hermes_cli.auth import (
        fetch_gemini_available_models,
        get_gemini_model_display_names,
        _GEMINI_MODELS_CACHE,
    )

    mock_resp = {
        "agentModelSorts": [
            {
                "groups": [
                    {
                        "modelIds": [
                            "gemini-3.8-flash-tiered",
                            "gemini-3.7-flash-tiered",
                            "claude-opus-4-6-thinking",
                        ]
                    }
                ]
            }
        ],
        "models": {
            "gemini-3.8-flash-tiered": {
                "displayName": None,
                "supportsThinking": True,
                "thinkingBudget": -1,
                "maxOutputTokens": 65536,
            },
            "gemini-3.7-flash-tiered": {
                "displayName": None,
                "supportsThinking": True,
                "thinkingBudget": -1,
                "maxOutputTokens": 65536,
            },
            "claude-opus-4-6-thinking": {
                "displayName": "Claude Opus 4.6 (Thinking)",
                "supportsThinking": True,
                "thinkingBudget": 4096,
            },
            "chat_internal": {"displayName": None},
        },
    }

    with patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials") as mock_creds, \
         patch("httpx.post") as mock_post:
        mock_creds.return_value = {"access_token": "ya29.test"}
        mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_resp)

        _GEMINI_MODELS_CACHE.clear()
        models = fetch_gemini_available_models(account=1, force=True)

        # Assert 3.8 virtual tiers are generated
        assert "gemini-3.8-flash-high" in models
        assert "gemini-3.8-flash-medium" in models
        assert "gemini-3.8-flash-low" in models

        # Assert 3.7 virtual tiers are generated
        assert "gemini-3.7-flash-high" in models
        assert "gemini-3.7-flash-medium" in models
        assert "gemini-3.7-flash-low" in models

        assert "claude-opus-4-6-thinking" in models
        assert "chat_internal" not in models

        # Check display names
        dnames = get_gemini_model_display_names(account=1)
        assert dnames["gemini-3.8-flash-high"] == "Gemini 3.8 Flash (High)"
        assert dnames["gemini-3.8-flash-medium"] == "Gemini 3.8 Flash (Medium)"
        assert dnames["gemini-3.8-flash-low"] == "Gemini 3.8 Flash (Low)"
        assert dnames["gemini-3.7-flash-high"] == "Gemini 3.7 Flash (High)"
        assert dnames["claude-opus-4-6-thinking"] == "Claude Opus 4.6 (Thinking)"


def test_resolve_cloudcode_model_and_effort_gemini_38():
    """Verify resolve_cloudcode_model_and_effort translates 3.8 tiers to wire slug."""
    from agent.gemini_cloudcode_adapter import resolve_cloudcode_model_and_effort

    assert resolve_cloudcode_model_and_effort("gemini-3.8-flash-high") == "gemini-3.8-flash-tiered"
    assert resolve_cloudcode_model_and_effort("gemini-3.8-flash-medium") == "gemini-3.8-flash-tiered"
    assert resolve_cloudcode_model_and_effort("gemini-3.8-flash-low") == "gemini-3.8-flash-tiered"
    assert resolve_cloudcode_model_and_effort("gemini-3.8-flash", effort="high") == "gemini-3.8-flash-tiered"
    assert resolve_cloudcode_model_and_effort("gemini-3.7-flash-high") == "gemini-3.7-flash-tiered"
    assert resolve_cloudcode_model_and_effort("gemini-3.6-flash", effort="low") == "gemini-3.6-flash-low"

def test_calculate_gemini_doci_score_math():
    from hermes_cli.auth import calculate_gemini_doci_score

    # Mock an account with 100% 5h, 68.4% weekly resetting in 16h (~0.67 days)
    mock_status_acc2 = {
        "logged_in": True,
        "email": "account_2@google.com",
        "quota": {
            "gemini_5h_percent": 100.0,
            "gemini_5h_reset": None,
            "gemini_weekly_percent": 68.4,
            "gemini_weekly_reset": (datetime.now(timezone.utc).timestamp() + 16 * 3600),
        },
    }

    # Mock an account with 97.2% 5h resetting in 2h 55m, 99.5% weekly resetting in 6.9 days
    mock_status_acc4 = {
        "logged_in": True,
        "email": "account_4@google.com",
        "quota": {
            "gemini_5h_percent": 97.2,
            "gemini_5h_reset": (datetime.now(timezone.utc).timestamp() + 2.92 * 3600),
            "gemini_weekly_percent": 99.5,
            "gemini_weekly_reset": (datetime.now(timezone.utc).timestamp() + 6.9 * 86400),
        },
    }

    # Mock an exhausted account (0.0% 5h)
    mock_status_exhausted = {
        "logged_in": True,
        "email": "exhausted@google.com",
        "quota": {
            "gemini_5h_percent": 0.0,
            "gemini_weekly_percent": 80.0,
        },
    }

    with patch("hermes_cli.auth.get_gemini_oauth_auth_status") as mock_status:
        # Account 2 test
        mock_status.return_value = mock_status_acc2
        doci2 = calculate_gemini_doci_score(2)
        assert doci2["score"] > 2.5  # High urgency multiplier due to 16h reset
        assert doci2["s_5h"] == 1.0

        # Account 4 test
        mock_status.return_value = mock_status_acc4
        doci4 = calculate_gemini_doci_score(4)
        assert doci4["u_5h"] > 1.2  # Mid-cycle double dip bonus
        assert doci4["score"] > 0.5

        # Exhausted account test
        mock_status.return_value = mock_status_exhausted
        doci_ex = calculate_gemini_doci_score(5)
        assert doci_ex["score"] == 0.0  # Safety gate triggered

        # Concurrency lease dampener test
        mock_status.return_value = mock_status_acc2
        doci_leased = calculate_gemini_doci_score(2, active_leases=2)
        assert doci_leased["active_leases"] == 2
        assert doci_leased["score"] < doci2["score"]
        assert doci_leased["base_score"] == doci2["base_score"]


def test_select_optimal_gemini_account_stickiness():
    from hermes_cli.auth import select_optimal_gemini_account

    # If current account has healthy capacity, stick to it (KV cache preservation)
    with patch("hermes_cli.auth.calculate_gemini_doci_score") as mock_doci:
        mock_doci.side_effect = lambda acc, **kw: {"score": 2.5, "logged_in": True}
        selected = select_optimal_gemini_account(current_account_id=2, candidate_account_ids=[1, 2, 3, 4, 5])
        assert selected == 2  # Sticks to current active account!


def test_select_optimal_gemini_account_failover_ranking():
    from hermes_cli.auth import select_optimal_gemini_account

    # When current account is exhausted (cap_5h 0.0), select highest scored candidate
    def _mock_doci(acc, **kw):
        scores = {
            1: {"score": 0.92, "cap_5h": 0.8, "logged_in": True},
            2: {"score": 0.0, "cap_5h": 0.0, "logged_in": True},   # Exhausted
            3: {"score": 0.81, "cap_5h": 0.7, "logged_in": True},
            4: {"score": 1.37, "cap_5h": 0.9, "logged_in": True},  # Top available candidate (Acc 4)
            5: {"score": 0.78, "cap_5h": 0.6, "logged_in": True},
        }
        return scores.get(acc, {"score": 0.0, "cap_5h": 0.0, "logged_in": False})

    with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci):
        selected = select_optimal_gemini_account(current_account_id=2, candidate_account_ids=[1, 2, 3, 4, 5])
        assert selected == 4  # Successfully fails over to highest scored candidate


def test_gemini_http_error_retry_info_and_reset_at():
    from agent.gemini_native_adapter import gemini_http_error

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {}
    
    error_payload = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "14280s"
                },
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"description": "Quota exceeded for 5h window"}]
                }
            ]
        }
    }

    err = gemini_http_error(mock_resp, body_text=json.dumps(error_payload))
    assert err.retry_after == 14280.0
    assert err.details["retry_after"] == 14280.0
    assert err.details["reset_at"] is not None
    assert err.details["reset_at"] > time.time() + 14000.0
    assert len(err.details["violations"]) == 1


def test_prime_sleeping_gemini_account_timer():
    from hermes_cli.auth import prime_sleeping_gemini_account_timer

    # Mock an account whose 5h timer is sleeping (100% capacity, no active reset countdown)
    mock_status_sleeping = {
        "logged_in": True,
        "email": "prime_test@google.com",
        "quota": {
            "gemini_5h_percent": 100.0,
            "gemini_5h_reset": None,
            "gemini_5h_countdown": None,
        },
    }

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "Ready"
    mock_resp.choices[0].message.reasoning = None
    mock_resp.usage.prompt_tokens = 4
    mock_resp.usage.completion_tokens = 1
    mock_resp.usage.total_tokens = 5
    mock_client._create_chat_completion.return_value = mock_resp

    with patch("hermes_cli.auth._GEMINI_LAST_PRIMED_AT", {}), \
         patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status_sleeping), \
         patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials", return_value={"api_key": "ya29.prime_token"}), \
         patch("hermes_cli.auth.fetch_gemini_quota_summary", return_value={"groups": [{"buckets": [{"bucketId": "gemini-5h", "remainingFraction": 0.9999995}, {"bucketId": "3p-5h", "remainingFraction": 0.9999995}]}]}), \
         patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient", return_value=mock_client):

        result = prime_sleeping_gemini_account_timer(1, model_group="gemini")
        assert result is True
        assert mock_client._create_chat_completion.called
        kwargs = mock_client._create_chat_completion.call_args[1]
        assert kwargs["model"] == "gemini-3.7-flash-low"
        assert kwargs["max_tokens"] == 32
        assert kwargs["messages"] == [{"role": "user", "content": "Say: Ready"}]

    # Test Claude / GPT group uses gpt-oss-120b-medium
    mock_client_3p = MagicMock()
    mock_client_3p._create_chat_completion.return_value = mock_resp
    mock_status_claude = {
        "logged_in": True,
        "email": "prime_test@google.com",
        "quota": {
            "claude_5h_percent": 100.0,
            "claude_5h_reset": None,
            "claude_5h_countdown": None,
        },
    }
    with patch("hermes_cli.auth._GEMINI_LAST_PRIMED_AT", {}), \
         patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status_claude), \
         patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials", return_value={"api_key": "ya29.prime_token"}), \
         patch("hermes_cli.auth.fetch_gemini_quota_summary", return_value={"groups": [{"buckets": [{"bucketId": "gemini-5h", "remainingFraction": 0.9999995}, {"bucketId": "3p-5h", "remainingFraction": 0.9999995}]}]}), \
         patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient", return_value=mock_client_3p):
        result_3p = prime_sleeping_gemini_account_timer(1, model_group="claude/gpt", force=True)
        assert result_3p is True
        assert mock_client_3p._create_chat_completion.called
        assert any(
            c.kwargs.get("model") == "gpt-oss-120b-medium"
            for c in mock_client_3p._create_chat_completion.call_args_list
        )


def test_prime_sleeping_gemini_account_timer_skips_active_timers():
    from hermes_cli.auth import prime_sleeping_gemini_account_timer

    # Mock an account whose 5h timer is actively running (reset countdown in future and <99.9% capacity)
    future_reset = (datetime.now(timezone.utc).timestamp() + 3600)
    mock_status_active = {
        "logged_in": True,
        "email": "prime_test@google.com",
        "quota": {
            "gemini_5h_percent": 90.0,
            "gemini_5h_reset": datetime.fromtimestamp(future_reset, timezone.utc).isoformat(),
            "gemini_5h_countdown": "1h 00m",
        },
    }

    mock_client = MagicMock()

    with patch("hermes_cli.auth.get_gemini_oauth_auth_status", return_value=mock_status_active), \
         patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient", return_value=mock_client):

        result = prime_sleeping_gemini_account_timer(1, model_group="gemini", force=False)
        assert result is False
        mock_client._create_chat_completion.assert_not_called()


def test_gemini_quota_watcher_daemon():
    from hermes_cli.auth import (
        start_gemini_quota_watcher_daemon,
        stop_gemini_quota_watcher_daemon,
        run_gemini_quota_check_cycle,
    )

    with patch("hermes_cli.auth.prime_sleeping_gemini_account_timer") as mock_prime:
        run_gemini_quota_check_cycle()
        assert mock_prime.call_count >= 5

    daemon_thread = start_gemini_quota_watcher_daemon(interval_seconds=3600.0)
    assert daemon_thread.is_alive()
    stop_gemini_quota_watcher_daemon()


def test_unified_gemini_oauth_provider_models_and_aliases():
    from hermes_cli.models_catalog_static import CANONICAL_PROVIDERS, PROVIDER_GROUPS, _PROVIDER_LABELS
    from hermes_cli.providers import HERMES_OVERLAYS, ALIASES

    # 1. Exactly one canonical provider entry for gemini oauth pool
    oauth_entries = [p for p in CANONICAL_PROVIDERS if p.slug.startswith("gemini-")]
    assert len(oauth_entries) == 1
    assert oauth_entries[0].slug == "gemini-oauth"
    assert oauth_entries[0].label == "Google Gemini (OAuth)"

    # 2. Group 'google' has only 'gemini' and 'gemini-oauth'
    assert "google" in PROVIDER_GROUPS
    _, _, members = PROVIDER_GROUPS["google"]
    assert members == ["gemini", "gemini-oauth"]

    # 3. Aliases normalize gemini-1..5 to gemini-oauth
    for i in range(1, 6):
        assert ALIASES.get(f"gemini-{i}") == "gemini-oauth"
        assert ALIASES.get(f"gemini-oauth-{i}") == "gemini-oauth"
        assert _PROVIDER_LABELS.get(f"gemini-{i}") == "Google Gemini (OAuth)"

    # 4. HERMES_OVERLAYS has gemini-oauth overlay
    assert "gemini-oauth" in HERMES_OVERLAYS
    assert HERMES_OVERLAYS["gemini-oauth"].base_url_override == "https://daily-cloudcode-pa.googleapis.com/v1internal"


def test_web_server_gemini_oauth_multi_account_status(tmp_path):
    from hermes_cli.web_routers.oauth import _build_oauth_catalog, _resolve_provider_status

    # Verify oauth catalog has single gemini-oauth row
    rows = _build_oauth_catalog()
    gemini_rows = [r for r in rows if "gemini" in r["id"]]
    assert len(gemini_rows) == 1
    assert gemini_rows[0]["id"] == "gemini-oauth"
    assert gemini_rows[0]["name"] == "Google Gemini (Antigravity OAuth)"

    # Mock multi-account status
    def mock_status(acc_idx):
        if acc_idx in {1, 2}:
            return {
                "logged_in": True,
                "account_id": acc_idx,
                "email": f"account_{acc_idx}@google.com",
                "api_key": f"ya29.acc_{acc_idx}_token",
                "quota": {
                    "gemini_5h_percent": 90.0 - acc_idx * 10,
                    "gemini_weekly_percent": 80.0,
                    "claude_5h_percent": 100.0,
                    "claude_weekly_percent": 100.0,
                },
            }
        return {"logged_in": False, "account_id": acc_idx, "email": "", "api_key": "", "quota": {}}

    with patch("hermes_cli.auth.get_gemini_oauth_auth_status", side_effect=mock_status), \
         patch("hermes_cli.auth.get_all_gemini_accounts_doci_rankings", return_value=[{"account_id": 2, "doci_score": 1.5}, {"account_id": 1, "doci_score": 1.2}]):

        status = _resolve_provider_status("gemini-oauth", None)
        assert status["logged_in"] is True
        assert "accounts" in status
        assert len(status["accounts"]) == 5
        assert status["accounts"][0]["logged_in"] is True
        assert status["accounts"][0]["email"] == "account_1@google.com"
        assert status["accounts"][1]["logged_in"] is True
        assert status["accounts"][1]["email"] == "account_2@google.com"
        assert status["accounts"][2]["logged_in"] is False
        assert status["source_label"] == "Google Gemini OAuth (2/5 Accounts Active)"
        assert len(status["doci_rankings"]) == 2


# ---------------------------------------------------------------------------
# 11. Multimodal inlineData, PDF Conversion & mediaResolution
# ---------------------------------------------------------------------------

def test_multimodal_data_url_and_media_resolution():
    from agent.gemini_native_adapter import _extract_multimodal_parts
    import base64

    # 1. Base64 Image with MEDIA_RESOLUTION_LOW
    raw_img = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsample"
    b64_img = base64.b64encode(raw_img).decode("ascii")
    data_url = f"data:image/png;base64,{b64_img}"

    content = [
        {"type": "text", "text": "Analyze this screenshot"},
        {
            "type": "image_url",
            "image_url": {"url": data_url, "detail": "low"},
        },
    ]

    parts = _extract_multimodal_parts(content)
    assert len(parts) == 2
    assert parts[0] == {"text": "Analyze this screenshot"}
    assert "inlineData" in parts[1]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[1]["inlineData"]["data"] == b64_img
    assert parts[1]["inlineData"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"


def test_multimodal_pdf_conversion_and_filesystem_paths(tmp_path):
    from agent.gemini_native_adapter import _extract_multimodal_parts
    import base64

    # 1. Direct PDF data
    raw_pdf = b"%PDF-1.4 sample pdf content..."
    b64_pdf = base64.b64encode(raw_pdf).decode("ascii")
    pdf_item = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64_pdf,
        },
    }
    parts = _extract_multimodal_parts([pdf_item])
    assert len(parts) == 1
    assert parts[0]["inlineData"]["mimeType"] == "application/pdf"
    assert parts[0]["inlineData"]["data"] == b64_pdf

    # 2. Local filesystem file path
    sample_file = tmp_path / "sample.pdf"
    sample_file.write_bytes(raw_pdf)

    file_content = [
        {"type": "image_url", "image_url": {"url": str(sample_file), "detail": "low"}}
    ]
    parts_fs = _extract_multimodal_parts(file_content)
    assert len(parts_fs) == 1
    assert parts_fs[0]["inlineData"]["mimeType"] == "application/pdf"
    assert parts_fs[0]["inlineData"]["data"] == b64_pdf
    assert parts_fs[0]["inlineData"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"


# ---------------------------------------------------------------------------
# 12. Thought Signatures & Cross-Model Trajectory Preservation
# ---------------------------------------------------------------------------

def test_thought_signature_attachment_and_stripping():
    from agent.gemini_native_adapter import _translate_tool_call_to_gemini

    tool_call = {
        "id": "call_123",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "file.txt"}),
        },
    }

    # Case A: Gemini target model without explicit thought signature -> attaches skip sentinel
    gemini_part = _translate_tool_call_to_gemini(tool_call, model="gemini-3.7-flash-tiered")
    assert gemini_part.get("thoughtSignature") == "skip_thought_signature_validator"
    assert "thoughtSignature" not in gemini_part.get("functionCall", {})

    # Case B: Gemini target with explicit signature in extra_content -> preserves signature
    tool_call_with_sig = dict(tool_call)
    tool_call_with_sig["extra_content"] = {"google": {"thought_signature": "cryptographic_token_abc"}}
    gemini_part_sig = _translate_tool_call_to_gemini(tool_call_with_sig, model="gemini-3.6-flash-low")
    assert gemini_part_sig.get("thoughtSignature") == "cryptographic_token_abc"
    assert "thoughtSignature" not in gemini_part_sig.get("functionCall", {})

    # Case C: Partner models (Claude, GPT-OSS) -> cleanly stripped (no thoughtSignature key)
    claude_part = _translate_tool_call_to_gemini(tool_call_with_sig, model="claude-sonnet-4-6")
    assert "thoughtSignature" not in claude_part
    assert "thoughtSignature" not in claude_part.get("functionCall", {})

    oss_part = _translate_tool_call_to_gemini(tool_call, model="gpt-oss-120b-medium")
    assert "thoughtSignature" not in oss_part
    assert "thoughtSignature" not in oss_part.get("functionCall", {})


# ---------------------------------------------------------------------------
# 13. Authoritative Token Budgeting (:countTokens)
# ---------------------------------------------------------------------------

def test_count_tokens_endpoint_client_helper():
    from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient

    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "totalTokens": 142
    }
    mock_http.post.return_value = mock_resp

    client = GeminiCloudCodeClient(
        access_token="test_token_abc",
        http_client=mock_http,
    )

    tokens = client.count_tokens(
        model="gemini-3.7-flash",
        messages=[{"role": "user", "content": "How many tokens is this?"}],
    )
    assert tokens == 142

    # Verify posted payload
    call_args = mock_http.post.call_args
    assert call_args is not None
    posted_url = call_args[0][0]
    assert posted_url.endswith(":countTokens")
    posted_json = call_args[1]["json"]
    assert "request" in posted_json
    assert posted_json["request"]["model"] == "gemini-3.7-flash-tiered"
    assert "contents" in posted_json["request"]

    # Also test via client.chat.count_tokens namespace
    mock_resp.json.return_value = {"response": {"totalTokens": 256}}
    tokens_ns = client.chat.count_tokens(
        model="gemini-3.6-flash-low",
        contents=[{"role": "user", "parts": [{"text": "Hello"}]}],
    )
    assert tokens_ns == 256


# ---------------------------------------------------------------------------
# 14. Deterministic Schema Serialization (agent/gemini_schema.py)
# ---------------------------------------------------------------------------

def test_deterministic_gemini_schema_serialization():
    from agent.gemini_schema import (
        sanitize_gemini_schema,
        serialize_gemini_schema,
        serialize_gemini_schema_deterministic,
    )

    # Two schemas with keys and properties inserted in different orders
    schema_1 = {
        "type": "object",
        "description": "Tool schema 1",
        "required": ["b", "a"],
        "properties": {
            "z_field": {"type": "string", "description": "Z"},
            "a_field": {"type": "integer", "enum": [1, 2, 3]},
            "m_field": {"type": "boolean"},
        },
    }

    schema_2 = {
        "properties": {
            "m_field": {"type": "boolean"},
            "a_field": {"type": "integer", "enum": [1, 2, 3]},
            "z_field": {"type": "string", "description": "Z"},
        },
        "required": ["a", "b"],
        "description": "Tool schema 1",
        "type": "object",
    }

    # Sanitized dictionaries must have deterministic sorted property ordering
    sanitized_1 = sanitize_gemini_schema(schema_1)
    sanitized_2 = sanitize_gemini_schema(schema_2)

    prop_keys_1 = list(sanitized_1["properties"].keys())
    prop_keys_2 = list(sanitized_2["properties"].keys())
    assert prop_keys_1 == ["a_field", "m_field", "z_field"]
    assert prop_keys_2 == ["a_field", "m_field", "z_field"]

    # Serialized JSON strings must be 100% byte-identical
    json_str_1 = serialize_gemini_schema_deterministic(schema_1)
    json_str_2 = serialize_gemini_schema_deterministic(schema_2)
    assert json_str_1 == json_str_2
    assert serialize_gemini_schema(schema_1) == json_str_1


# ---------------------------------------------------------------------------
# 15. Interactive Challenge Handling (VALIDATION_REQUIRED / 403)
# ---------------------------------------------------------------------------

def test_gemini_validation_required_challenge_url_extraction():
    from hermes_cli.auth import (
        extract_gemini_challenge_url,
        is_gemini_validation_required_error,
        prompt_gemini_interactive_challenge,
        format_auth_error,
        AuthError,
    )
    from agent.gemini_native_adapter import gemini_http_error, GeminiAPIError

    # 1. Google Cloud Code PA 403 response with google.rpc.Help
    challenge_url = "https://accounts.google.com/signin/v2/challenge/selection?token=xyz123"
    rpc_error_body = {
        "error": {
            "code": 403,
            "message": "User verification required.",
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "VALIDATION_REQUIRED",
                    "domain": "googleapis.com",
                    "metadata": {"service": "cloudcode-pa.googleapis.com"},
                },
                {
                    "@type": "type.googleapis.com/google.rpc.Help",
                    "links": [
                        {
                            "description": "Google Cloud Code Verification",
                            "url": challenge_url,
                        }
                    ],
                },
            ],
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {}
    mock_resp.text = json.dumps(rpc_error_body)

    # A. Adapter parses into GeminiAPIError with code='gemini_validation_required'
    api_err = gemini_http_error(mock_resp, body_text=mock_resp.text)
    assert api_err.code == "gemini_validation_required"
    assert api_err.challenge_url == challenge_url
    assert api_err.details["challenge_url"] == challenge_url

    # B. extract_gemini_challenge_url extracts URL from various shapes
    assert extract_gemini_challenge_url(api_err) == challenge_url
    assert extract_gemini_challenge_url(rpc_error_body) == challenge_url
    assert extract_gemini_challenge_url(mock_resp.text) == challenge_url

    # C. is_gemini_validation_required_error detection
    assert is_gemini_validation_required_error(api_err) is True
    assert is_gemini_validation_required_error(rpc_error_body) is True
    assert is_gemini_validation_required_error("VALIDATION_REQUIRED: please verify") is True
    assert is_gemini_validation_required_error("Normal rate limit 429") is False

    # D. format_auth_error surfaces 1-click verification guidance
    auth_err = AuthError("Verification required", code="gemini_validation_required")
    auth_err.challenge_url = challenge_url
    formatted = format_auth_error(auth_err)
    assert "VALIDATION_REQUIRED" in formatted
    assert challenge_url in formatted

    # E. prompt_gemini_interactive_challenge prints prompt and returns True
    prompted = prompt_gemini_interactive_challenge(challenge_url, account=2, auto_open=False)
    assert prompted is True


def test_credential_pool_gemini_oauth_session_stickiness():
    from agent.credential_pool import CredentialPool, PooledCredential

    creds = [
        PooledCredential(
            provider="gemini-oauth",
            id="acc-1",
            label="gemini-1",
            auth_type="oauth",
            priority=0,
            source="gemini_account_1",
            access_token="tok1",
            extra={"account_id": 1},
        ),
        PooledCredential(
            provider="gemini-oauth",
            id="acc-2",
            label="gemini-2",
            auth_type="oauth",
            priority=1,
            source="gemini_account_2",
            access_token="tok2",
            extra={"account_id": 2},
        ),
    ]

    pool = CredentialPool("gemini-oauth", creds)

    # Initial selection: Acc 1 has score 2.0, Acc 2 has score 3.5 -> picks Acc 2 (higher DOCI)
    def _mock_doci_turn1(acc, **kw):
        if acc == 1:
            return {"score": 2.0, "cap_5h": 0.80, "cap_w": 0.90, "logged_in": True}
        return {"score": 3.5, "cap_5h": 0.85, "cap_w": 0.95, "logged_in": True}

    with patch("hermes_cli.auth._read_gemini_account_tokens", return_value=None):
        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci_turn1):
            selected1 = pool.select()
            assert selected1.id == "acc-2"

        # Turn 2: Acc 1 score rises to 4.0, but Acc 2 is still healthy
        # Pool MUST stick to Acc 2 to preserve KV cache!
        def _mock_doci_turn2(acc, **kw):
            if acc == 1:
                return {"score": 4.0, "cap_5h": 0.90, "cap_w": 0.95, "logged_in": True}
            return {"score": 2.5, "cap_5h": 0.75, "cap_w": 0.90, "logged_in": True}

        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci_turn2):
            selected2 = pool.select()
            assert selected2.id == "acc-2"  # Session stickiness preserved!

        # Turn 3: Acc 2 capacity drops below 20% (e.g. cap_5h=15%, DOCI score=0.0)
        # Since NO 429 error was received, pool MUST STILL stick to Acc 2 to avoid pre-filling cache!
        def _mock_doci_turn3(acc, **kw):
            if acc == 1:
                return {"score": 4.0, "cap_5h": 0.90, "cap_w": 0.95, "logged_in": True}
            return {"score": 0.0, "cap_5h": 0.15, "cap_w": 0.90, "logged_in": True}

        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci_turn3):
            selected3 = pool.select()
            assert selected3.id == "acc-2"  # Strictly sticky even below 20% quota!

        # Turn 4: Acc 2 receives a real HTTP 429 rate limit from Google API and is marked exhausted.
        # Now that Acc 2 is exhausted, pool cleanly fails over to Acc 1!
        pool.mark_exhausted_and_rotate(status_code=429, credential_id=selected3.id)

        with patch("hermes_cli.auth.calculate_gemini_doci_score", side_effect=_mock_doci_turn3):
            selected4 = pool.select()
            assert selected4.id == "acc-1"  # Rotates to healthy account on real 429!


def test_gemini_oauth_chat_completions_thinking_config():
    import agent.transports.chat_completions  # noqa: F401
    from agent.transports import get_transport
    cc = get_transport("chat_completions")
    assert cc is not None
    kw = cc.build_kwargs(
        model="gemini-3.7-flash",
        messages=[{"role": "user", "content": "hi"}],
        provider_name="gemini-oauth",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    assert "extra_body" in kw
    assert "thinking_config" in kw["extra_body"]
    assert kw["extra_body"]["thinking_config"] == {"includeThoughts": True, "thinkingLevel": "high"}


def test_gemini_cloudcode_adapter_default_max_output_tokens():
    from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient
    from unittest.mock import MagicMock

    client = GeminiCloudCodeClient(access_token="test_token")
    client._http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "thinking"},
                            {"text": "answer"},
                        ],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    }
    client._http.post.return_value = mock_resp

    res = client._create_chat_completion(
        model="gemini-3.7-flash-high",
        messages=[{"role": "user", "content": "hello"}],
    )
    call_kwargs = client._http.post.call_args
    body = call_kwargs.kwargs["json"]
    req = body["request"]
    assert req["generationConfig"]["maxOutputTokens"] == 65536
    assert getattr(res.choices[0].message, "reasoning") == "thinking"


# ---------------------------------------------------------------------------
# 11. Antigravity Output Ceilings, Thinking Headroom & Invariant Tests
# ---------------------------------------------------------------------------

def test_gemini_output_ceiling_and_model_aware_limits():
    from agent.gemini_native_adapter import (
        GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
        _effective_gemini_max_output_tokens,
    )

    assert GEMINI_DEFAULT_MAX_OUTPUT_TOKENS == 65536

    # Native Gemini model
    assert _effective_gemini_max_output_tokens(None, None, model="gemini-3.7-flash") == 65536
    assert _effective_gemini_max_output_tokens(4096, None, model="gemini-3.7-flash") == 4096

    # Claude partner model
    assert _effective_gemini_max_output_tokens(None, None, model="claude-sonnet-4-6") == 64000
    assert _effective_gemini_max_output_tokens(80000, None, model="claude-sonnet-4-6") == 64000
    assert _effective_gemini_max_output_tokens(4096, None, model="claude-sonnet-4-6") == 4096

    # GPT-OSS partner model
    assert _effective_gemini_max_output_tokens(None, None, model="gpt-oss-120b-medium") == 8192
    assert _effective_gemini_max_output_tokens(16384, None, model="gpt-oss-120b-medium") == 8192


def test_gemini_thinking_headroom_management():
    from agent.gemini_native_adapter import _effective_gemini_max_output_tokens
    from agent.transports.chat_completions import _raise_gemini_thinking_max_tokens

    # Gemini with high thinking level elevates low max_tokens to full ceiling
    thinking_cfg = {"includeThoughts": True, "thinkingLevel": "high"}
    assert _effective_gemini_max_output_tokens(4096, thinking_cfg, model="gemini-3.7-flash") == 65536

    # Claude partner model with explicit thinkingBudget guarantees headroom: max(requested, budget + 8192, 64000)
    claude_thinking = {"includeThoughts": True, "thinkingBudget": 16384}
    assert _effective_gemini_max_output_tokens(4096, claude_thinking, model="claude-sonnet-4-6") == 64000

    # Very large budget extends output ceiling accordingly
    claude_large_thinking = {"includeThoughts": True, "thinkingBudget": 60000}
    assert _effective_gemini_max_output_tokens(4096, claude_large_thinking, model="claude-sonnet-4-6") == 68192

    # Transport helper passes model through
    raised_gemini = _raise_gemini_thinking_max_tokens(
        "gemini-3.7-flash",
        {"enabled": True, "effort": "high"},
        4096,
    )
    assert raised_gemini == 65536

    raised_claude = _raise_gemini_thinking_max_tokens(
        "claude-sonnet-4-6",
        {"enabled": True, "effort": "high"},
        4096,
    )
    assert raised_claude == 64000


def test_gemini_cloudcode_adapter_model_aware_ceilings():
    from agent.gemini_cloudcode_adapter import GeminiCloudCodeClient
    from unittest.mock import MagicMock

    client = GeminiCloudCodeClient(access_token="test_token")
    client._http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]
        }
    }
    client._http.post.return_value = mock_resp

    # Claude model with thinking
    client._create_chat_completion(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
        max_tokens=4096,
    )
    req = client._http.post.call_args.kwargs["json"]["request"]
    assert req["generationConfig"]["maxOutputTokens"] == 64000

    # GPT-OSS model without thinking
    client._create_chat_completion(
        model="gpt-oss-120b-medium",
        messages=[{"role": "user", "content": "hi"}],
    )
    req = client._http.post.call_args.kwargs["json"]["request"]
    assert req["generationConfig"]["maxOutputTokens"] == 8192


def test_context_compressor_gemini_output_headroom_invariant():
    from agent.context_compressor import ContextCompressor

    # For 200k Gemini window with max_tokens=None, output reservation is 65536.
    # Usable input budget = 200000 - 65536 = 134464
    # Hard cap = 134464 - 1024 = 133440
    # Threshold at 50% = 67232 <= 133440
    t_gemini = ContextCompressor._compute_threshold_tokens(
        context_length=200000,
        threshold_percent=0.50,
        max_tokens=None,
        model="gemini-3.7-flash",
        provider="gemini-oauth",
    )
    assert t_gemini == 67232
    assert t_gemini <= 200000 - 65536 - 1024

    # High percentage clamped strictly below hard safety cap
    t_gemini_high = ContextCompressor._compute_threshold_tokens(
        context_length=200000,
        threshold_percent=0.999,
        max_tokens=None,
        model="gemini-3.7-flash",
        provider="gemini-oauth",
    )
    assert t_gemini_high == 133440
    assert t_gemini_high <= 200000 - 65536 - 1024


def test_try_acquire_quota_refresher_lease_claim_and_renew(tmp_path, monkeypatch):
    """Leader can acquire and continuously renew its lease."""
    from hermes_cli.auth import _try_acquire_quota_refresher_lease

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.auth.get_hermes_home", lambda: tmp_path)

    # First acquisition succeeds
    assert _try_acquire_quota_refresher_lease("container-1", ttl_seconds=90.0) is True

    # Same holder renewal succeeds
    assert _try_acquire_quota_refresher_lease("container-1", ttl_seconds=90.0) is True


def test_try_acquire_quota_refresher_lease_yields_to_live_leader(tmp_path, monkeypatch):
    """Secondary container stands down when an active leader holds the lease."""
    from hermes_cli.auth import _try_acquire_quota_refresher_lease

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.auth.get_hermes_home", lambda: tmp_path)

    # Container 1 claims leadership
    assert _try_acquire_quota_refresher_lease("container-1", ttl_seconds=90.0) is True

    # Container 2 checks and stands down (returns False)
    assert _try_acquire_quota_refresher_lease("container-2", ttl_seconds=90.0) is False


def test_try_acquire_quota_refresher_lease_fails_over_when_expired(tmp_path, monkeypatch):
    """Secondary container takes over when the primary leader lease expires."""
    import time
    from hermes_cli.auth import _try_acquire_quota_refresher_lease

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.auth.get_hermes_home", lambda: tmp_path)

    # Container 1 claims with very short TTL
    assert _try_acquire_quota_refresher_lease("container-1", ttl_seconds=0.1) is True

    # Artificially expire the lease
    lease_path = tmp_path / "runtime" / "quota_refresher.lease"
    data = json.loads(lease_path.read_text(encoding="utf-8"))
    data["expires_at"] = time.time() - 10.0
    lease_path.write_text(json.dumps(data), encoding="utf-8")

    # Container 2 now successfully takes over leadership
    assert _try_acquire_quota_refresher_lease("container-2", ttl_seconds=90.0) is True

def test_clear_gemini_oauth_account_disconnect(tmp_path, monkeypatch):
    """Disconnecting a specific Gemini OAuth account removes it from auth.json, pool, and secondary token files."""
    from hermes_cli.auth import clear_provider_auth, _save_gemini_account_tokens, _antigravity_token_path, _read_gemini_account_tokens, _load_auth_store

    auth_file = tmp_path / "auth.json"
    monkeypatch.setattr("hermes_cli.auth._auth_file_path", lambda: auth_file)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.auth.get_hermes_home", lambda: tmp_path)

    # Save tokens for account 1 and account 2
    _save_gemini_account_tokens(1, {"access_token": "ya29.acc1", "refresh_token": "ref1", "email": "acc1@google.com"})
    _save_gemini_account_tokens(2, {"access_token": "ya29.acc2", "refresh_token": "ref2", "email": "acc2@google.com"})

    tok_path_1 = _antigravity_token_path(1)
    tok_path_2 = _antigravity_token_path(2)

    assert tok_path_1.exists()
    assert tok_path_2.exists()

    # Disconnect account 2
    cleared = clear_provider_auth("gemini-oauth-2")
    assert cleared is True

    # Account 2 secondary file is deleted
    assert not tok_path_2.exists()
    # Account 1 remains intact
    assert tok_path_1.exists()

    # Reading account 2 tokens now raises AuthError / returns empty
    from hermes_cli.auth import AuthError
    try:
        tok2 = _read_gemini_account_tokens(2)
        assert not tok2.get("access_token")
    except AuthError:
        pass

