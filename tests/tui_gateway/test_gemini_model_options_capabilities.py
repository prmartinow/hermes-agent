"""Tests for Gemini model.options RPC capability contracts (Action Item 3, Milestone 5)."""

import pytest
from unittest.mock import patch
import tui_gateway.server as server


def test_model_options_rpc_carries_exact_reasoning_efforts():
    """Pin RPC-level model.options capability contracts for Cloud Code and generic routes:
    Target              Expected reasoning_efforts
    Gemini 3.8          ['low', 'medium', 'high']
    Gemini 3.7          ['low', 'medium', 'high']
    Gemini 3.6          ['low', 'medium', 'high']
    Gemini 3.1 Pro      ['low', 'high']
    flash-lite          []
    Claude              []
    GPT-OSS             []
    OpenRouter Gemini   None (no Cloud Code exact-effort injection)
    """
    fake_rows = [
        {
            "slug": "gemini-oauth",
            "name": "Google Gemini (OAuth)",
            "models": [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.1-pro",
                "gemini-3.1-flash-lite",
                "claude-sonnet-4-6",
                "gpt-oss-120b-medium",
            ],
            "authenticated": True,
            "is_current": True,
        },
        {
            "slug": "gemini-2",
            "name": "Gemini Account 2",
            "models": ["gemini-3.8-flash", "gemini-3.1-pro"],
            "authenticated": True,
            "is_current": False,
        },
        {
            "slug": "openrouter",
            "name": "OpenRouter",
            "models": ["google/gemini-3.8-flash", "google/gemini-3.1-pro-preview"],
            "authenticated": True,
            "is_current": False,
        }
    ]

    with patch("hermes_cli.model_switch.list_authenticated_providers", return_value=fake_rows):
        res = server._methods["model.options"](1, {"include_unconfigured": True})
        assert "result" in res, f"RPC model.options failed: {res}"
        result = res["result"]
        providers = result.get("providers", [])

        # 1. Cloud Code OAuth provider
        p_gemini = next((p for p in providers if p.get("slug") == "gemini-oauth"), None)
        assert p_gemini is not None, "gemini-oauth provider missing from model.options"
        caps = p_gemini.get("capabilities", {})

        assert caps.get("gemini-3.8-flash", {}).get("reasoning_efforts") == ["low", "medium", "high"]
        assert caps.get("gemini-3.7-flash", {}).get("reasoning_efforts") == ["low", "medium", "high"]
        assert caps.get("gemini-3.6-flash", {}).get("reasoning_efforts") == ["low", "medium", "high"]
        assert caps.get("gemini-3.1-pro", {}).get("reasoning_efforts") == ["low", "high"]
        assert caps.get("gemini-3.1-flash-lite", {}).get("reasoning_efforts") == []
        assert caps.get("claude-sonnet-4-6", {}).get("reasoning_efforts") == []
        assert caps.get("gpt-oss-120b-medium", {}).get("reasoning_efforts") == []

        # 2. Numbered account route (gemini-2)
        p_gemini2 = next((p for p in providers if p.get("slug") == "gemini-2"), None)
        assert p_gemini2 is not None
        caps2 = p_gemini2.get("capabilities", {})
        assert caps2.get("gemini-3.8-flash", {}).get("reasoning_efforts") == ["low", "medium", "high"]
        assert caps2.get("gemini-3.1-pro", {}).get("reasoning_efforts") == ["low", "high"]

        # 3. OpenRouter (generic aggregator route)
        p_openrouter = next((p for p in providers if p.get("slug") == "openrouter"), None)
        assert p_openrouter is not None
        caps_orp = p_openrouter.get("capabilities", {})
        for model_id, model_caps in caps_orp.items():
            if "gemini" in model_id.lower():
                # Invariant: OpenRouter routes must never inject Cloud Code exact-effort ladders!
                assert model_caps.get("reasoning_efforts") is None
