"""Unit tests for reusable live compaction runner.

Tests offline functionality, prompt assembly, and artifact persistence with mocked LLM calls
to ensure safe testability without burning live tokens.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from evals.compaction.summary_usefulness.fixtures import (
    MIGRATION_CASE_GROUND_TRUTH,
    SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY,
    SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
)
from evals.compaction.summary_usefulness.live_runner import (
    MAX_INPUT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_production_prompt_and_metadata,
    execute_live_compaction_request,
    get_default_scratch_dir,
    resolve_actual_configured_auxiliary,
)


@pytest.fixture(autouse=True)
def _isolate_configured_auxiliary(monkeypatch: pytest.MonkeyPatch):
    """Ensure test isolation for live runner unit tests.

    Under pytest suites with tests/conftest.py, HERMES_HOME is sandboxed and env vars cleared.
    This fixture provides deterministic configuration and mock OAuth credential resolution
    for offline unit tests, preventing contamination from prior test runs or host environment drift.
    """
    from agent.auxiliary_client import _client_cache, _reset_aux_unhealthy_cache

    test_cfg = {
        "model": {
            "default": "gemini-3.8-flash-high",
            "provider": "gemini-oauth",
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: test_cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: test_cfg)
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_gemini_oauth_runtime_credentials",
        lambda *args, **kwargs: {
            "access_token": "ya29.test-mock-token",
            "api_key": "ya29.test-mock-token",
            "base_url": "https://cloudcode-pa.googleapis.com/v1internal",
        },
    )
    _client_cache.clear()
    _reset_aux_unhealthy_cache()
    yield
    _client_cache.clear()
    _reset_aux_unhealthy_cache()


def test_resolve_actual_configured_auxiliary_sanitized():
    aux = resolve_actual_configured_auxiliary()
    assert "resolved_model" in aux
    assert "effective_provider" in aux
    assert "client_class" in aux
    assert aux["resolved_model"] == "gemini-3.8-flash-high"
    assert aux["effective_provider"] == "gemini-oauth"

    # Invariant: Endpoint omitted completely instead of bespoke secret scrubbing
    assert "client_endpoint" not in aux
    assert "endpoint" not in aux
    for v in aux.values():
        val_str = str(v)
        assert "http://" not in val_str and "https://" not in val_str

    # Invariant: No credentials or secret keys present in sanitized dict
    for k in aux:
        assert "key" not in k.lower() or k == "api_key_sanitized" or k == "auth_type"
        assert "secret" not in k.lower()
        assert "token" not in k.lower() or k == "auth_type"


def test_build_production_prompt_and_metadata():
    prompt, meta = build_production_prompt_and_metadata(
        SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
        "gemini-3.8-flash-high",
        "gemini-oauth",
    )
    assert isinstance(prompt, str)
    assert len(prompt) > 10000
    assert "## Completed Actions" in prompt
    assert "## Constraints & Preferences" in prompt
    assert "aurora-pg-prod.vpc-east.internal" in prompt
    assert meta["input_turns_count"] == len(SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED) - 1


def test_execute_live_compaction_request_mocked(tmp_path: Path):
    mock_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content=SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5600,
            completion_tokens=450,
            total_tokens=6050,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
        model="gemini-3.8-flash-high",
    )

    with (
        patch("evals.compaction.summary_usefulness.live_runner.call_llm", return_value=mock_response) as mock_call,
        patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630),
    ):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
        )

        # Proves max_tokens=1500 is passed to call_llm
        assert mock_call.called
        assert mock_call.call_args.kwargs["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
        assert mock_call.call_args.kwargs["max_tokens"] == 1500

        assert result["raw_content"] == SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY
        assert (tmp_path / "raw_summary_output.md").exists()
        assert (tmp_path / "cleaned_summary.md").exists()
        assert (tmp_path / "sanitized_metadata.json").exists()
        assert (tmp_path / "evaluation_score.json").exists()

        # Proves no endpoint in metadata or on-disk metadata file
        assert "client_endpoint" not in result["metadata"]
        assert "endpoint" not in result["metadata"]
        assert result["metadata"]["max_output_tokens_configured"] == 1500

        meta = json.loads((tmp_path / "sanitized_metadata.json").read_text())
        assert meta["model"] == "gemini-3.8-flash-high"
        assert meta["provider"] == "gemini-oauth"
        assert "client_endpoint" not in meta
        assert "endpoint" not in meta
        assert meta["usage"]["provider_prompt_tokens"] == 5600
        assert meta["usage"]["provider_completion_tokens"] == 450

        score = result["eval_score"]
        assert score.usefulness_score >= 0.90
        assert score.matched_required_facts == score.total_required_facts


def test_execute_live_compaction_custom_max_tokens(tmp_path: Path):
    mock_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content=SYNTHETIC_HANDCRAFTED_GOOD_SUMMARY,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5600,
            completion_tokens=300,
            total_tokens=5900,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
        model="gemini-3.8-flash-high",
    )

    with (
        patch("evals.compaction.summary_usefulness.live_runner.call_llm", return_value=mock_response) as mock_call,
        patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630),
    ):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            max_output_tokens=800,
        )

        assert mock_call.called
        assert mock_call.call_args.kwargs["max_tokens"] == 800
        assert result["metadata"]["max_output_tokens_configured"] == 800


def test_execute_live_compaction_input_refusal_fail_closed(tmp_path: Path):
    """Proves input token count > 8000 fails closed and halts before any LLM inference."""
    with (
        patch("evals.compaction.summary_usefulness.live_runner.call_llm") as mock_call,
        patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=8001),
    ):
        with pytest.raises(ValueError, match=r"Input token count 8001 exceeds maximum allowed limit of 8000"):
            execute_live_compaction_request(
                transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
                ground_truth=MIGRATION_CASE_GROUND_TRUTH,
                scratch_dir=tmp_path,
            )

        # Invariant: call_llm was never invoked (strict fail-closed offline refusal)
        mock_call.assert_not_called()


def test_dynamic_scratch_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Proves dynamic scratch directory resolution via TMPDIR or canonical get_hermes_home."""
    # 1. With TMPDIR set
    test_tmpdir = tmp_path / "custom_tmp"
    test_tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(test_tmpdir))
    scratch = get_default_scratch_dir()
    assert scratch == test_tmpdir / "compaction-live-quality"

    # 2. With TMPDIR unset -> falls back to canonical hermes scratch/home without hardcoded /home
    monkeypatch.delenv("TMPDIR", raising=False)
    scratch_canonical = get_default_scratch_dir()
    assert scratch_canonical.name == "compaction-live-quality"
    assert "cache" in scratch_canonical.parts or "scratch" in scratch_canonical.parts
