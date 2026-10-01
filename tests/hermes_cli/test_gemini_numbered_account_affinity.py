"""Deterministic test for Gemini OAuth numbered provider affinity (Action Item 4 Closure).

Verifies that switching with an explicit numbered provider (`gemini-1` .. `gemini-5`)
resolves the specific account's credentials from the credential pool.
"""

import pytest
from unittest.mock import patch, MagicMock

from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli.model_switch import switch_model


def _make_mock_gemini_pool():
    entries = []
    for i in range(1, 6):
        entries.append(
            PooledCredential(
                provider="gemini-oauth",
                id=f"acc_{i}_id",
                label=f"user{i}@example.com",
                auth_type="oauth",
                priority=0,
                source=f"gemini_account_{i}",
                access_token=f"test_token_account_{i}",
                extra={"account_id": i},
            )
        )
    pool = CredentialPool(provider="gemini-oauth", entries=entries)
    return pool


def test_numbered_provider_affinity_resolves_exact_account_token():
    """Verify that switch_model with explicit_provider=gemini-N selects token N."""
    mock_pool = _make_mock_gemini_pool()

    with patch("hermes_cli.runtime_provider.load_pool", return_value=mock_pool):
        for i in range(1, 6):
            prov = f"gemini-{i}"
            res = switch_model(
                raw_input="gemini-3.8-flash",
                current_provider="gemini-oauth",
                current_model="gemini-3.8-flash",
                explicit_provider=prov,
            )
            assert res.success is True, f"Failed for {prov}: {res.error_message}"
            assert res.target_provider == prov
            assert res.api_key == f"test_token_account_{i}", (
                f"Expected test_token_account_{i} for {prov}, got {res.api_key}"
            )


def test_gemini_oauth_generic_selection_uses_cd_doci():
    """Verify that generic gemini-oauth switch uses normal selection without forcing account affinity."""
    mock_pool = _make_mock_gemini_pool()

    with patch("hermes_cli.runtime_provider.load_pool", return_value=mock_pool):
        res = switch_model(
            raw_input="gemini-3.8-flash",
            current_provider="gemini-oauth",
            current_model="gemini-3.8-flash",
            explicit_provider="gemini-oauth",
        )
        assert res.success is True
        assert res.target_provider == "gemini-oauth"
        assert res.api_key in [f"test_token_account_{i}" for i in range(1, 6)]
