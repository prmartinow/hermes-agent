"""Unit tests for faithful bounded compaction runner.

Tests offline functionality, prompt assembly on the selected window, production compress
lifecycle (normal stop vs length vs oversized/refusal/salvage), artifact persistence,
and fail-closed guards without burning live tokens or requiring host credentials.
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
    """Verifies prompt construction faithful to actual selected production window."""
    prompt, meta = build_production_prompt_and_metadata(
        SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
        "gemini-3.8-flash-high",
        "gemini-oauth",
    )
    assert isinstance(prompt, str)
    assert len(prompt) > 4000
    assert "aurora-pg-prod.vpc-east.internal" in prompt
    c_start, c_end = meta["compress_window"]
    assert c_start < c_end
    assert meta["input_turns_count"] == c_end - c_start
    assert meta["total_transcript_turns"] == len(SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED)


def test_lifecycle_length_truncation_rejection(tmp_path: Path):
    """Verifies finish_reason=length triggers production truncation failure:
    aborts compression, preserves original transcript without augmentation stub.
    """
    mock_trunc_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(
                    role="assistant",
                    content="## Goal\nPartial truncated summary that hit token cap mid-sent...",
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5600,
            completion_tokens=1500,
            total_tokens=7100,
        ),
        model="gemini-3.8-flash-high",
    )

    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_trunc_resp,
            max_output_tokens=1500,
        )

        assert result["lifecycle_status"] == "length_truncated_aborted"
        assert result["metadata"]["aborted_truncated"] is True
        assert result["metadata"]["original_preserved"] is True
        assert result["metadata"]["finish_reason"] == "length"
        assert result["final_accepted_messages"] == SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED

        # Proves no augmentation stub: raw output was rejected
        assert (tmp_path / "original_messages.json").exists()
        assert (tmp_path / "final_accepted_messages.json").exists()
        assert (tmp_path / "raw_summary_output.md").exists()


def test_lifecycle_oversized_growth_guard_refusal(tmp_path: Path):
    """Verifies growth guard refuses commit when candidate exceeds pre-compaction tokens
    and salvage cannot shrink it below budget: rejected_would_grow=True, original preserved.
    """
    mock_resp = SimpleNamespace(
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
        ),
        model="gemini-3.8-flash-high",
    )

    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_resp,
        )

        # Candidate expanded the 1 replaced turn with the full handcrafted summary (5121 > 3388 tokens)
        assert result["lifecycle_status"] == "growth_guard_refused"
        assert result["metadata"]["refused_would_grow"] is True
        assert result["metadata"]["original_preserved"] is True
        assert result["final_accepted_messages"] == SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED

        # Final score evaluates the preserved original transcript
        score = result["eval_score_final"]
        assert score.matched_required_facts == score.total_required_facts
        assert any("Compaction aborted/refused" in n for n in score.diagnostic_notes)

        # Raw output is distinctly scored
        raw_score = result["eval_score_raw"]
        assert raw_score is not None
        assert raw_score.usefulness_score >= 0.90


def test_lifecycle_model_refusal_aborted(tmp_path: Path):
    """Verifies LLM refusal triggers production refusal abort, preserving original transcript."""
    mock_refusal_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content="I cannot fulfill this request as an operations assistant.",
                    refusal="I cannot fulfill this request as an operations assistant.",
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5600,
            completion_tokens=15,
            total_tokens=5615,
        ),
        model="gemini-3.8-flash-high",
    )

    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_refusal_resp,
        )

        assert result["lifecycle_status"] == "refusal_aborted"
        assert result["metadata"]["aborted_refusal"] is True
        assert result["metadata"]["original_preserved"] is True
        assert result["final_accepted_messages"] == SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED


def test_lifecycle_salvage_recovery(tmp_path: Path):
    """Verifies that when candidate is slightly oversized, salvage recovers a shrinking transcript."""
    concise_summary = (
        "## Goal\nMigrate 1,420,500 records from billing_ledger_prod accounts_v2.\n"
        "## Constraints & Preferences\nActive target aurora-pg-prod.vpc-east.internal:5439 user migrator_worker_v2.\n"
        "## Active State\nRocksDB checkpoint /var/data/migration_checkpoint.db ready.\n"
    )
    mock_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content=concise_summary,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=5600,
            completion_tokens=60,
            total_tokens=5660,
        ),
        model="gemini-3.8-flash-high",
    )

    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_resp,
        )

        assert result["lifecycle_status"] == "salvaged_accepted"
        assert result["metadata"]["salvaged"] is True
        assert result["metadata"]["original_preserved"] is False
        assert result["final_accepted_messages"] != SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED
        assert result["metadata"]["tokens"]["final_tokens_rough"] < result["metadata"]["tokens"]["original_tokens_rough"]


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
                allow_live=True,
            )

        mock_call.assert_not_called()


def test_execute_live_compaction_default_offline_fail_closed(tmp_path: Path):
    """Proves default allow_live=False without mock_response fails closed."""
    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        with pytest.raises(RuntimeError, match=r"Live auxiliary inference is not authorized"):
            execute_live_compaction_request(
                transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
                ground_truth=MIGRATION_CASE_GROUND_TRUTH,
                scratch_dir=tmp_path,
                allow_live=False,
                mock_response=None,
            )


def test_execute_live_compaction_physical_calls_bounded_fail_closed(tmp_path: Path):
    """Proves physical calls exceeding max_auxiliary_calls fail closed."""
    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        with pytest.raises(RuntimeError, match=r"Auxiliary physical call bound exceeded"):
            execute_live_compaction_request(
                transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
                ground_truth=MIGRATION_CASE_GROUND_TRUTH,
                scratch_dir=tmp_path,
                allow_live=True,
                max_auxiliary_calls=0,  # 0 budget forces immediate fail-closed
            )


def test_execute_live_compaction_production_default_output_budget_honest(tmp_path: Path):
    """Proves when max_output_tokens=None, no max_tokens parameter is forced (honest production default)."""
    mock_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content="## Goal\nMigrate records."),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=5600, completion_tokens=30, total_tokens=5630),
        model="gemini-3.8-flash-high",
    )

    with patch("agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens", return_value=5630):
        result = execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_resp,
            max_output_tokens=None,
        )

        assert result["metadata"]["max_output_tokens_budget"] == "production_default_honest"


def test_dynamic_scratch_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Proves dynamic scratch directory resolution via TMPDIR or canonical get_scratch_dir()."""
    # 1. With TMPDIR set
    test_tmpdir = tmp_path / "custom_tmp"
    test_tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(test_tmpdir))
    scratch = get_default_scratch_dir()
    assert scratch == test_tmpdir / "compaction-eval-fidelity-fix"

    # 2. With TMPDIR unset -> falls back to canonical hermes scratch/home
    monkeypatch.delenv("TMPDIR", raising=False)
    scratch_canonical = get_default_scratch_dir()
    assert scratch_canonical.name == "compaction-eval-fidelity-fix"


def test_authorization_before_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Proves offline authorization check halts execution BEFORE route resolution, config load, or countTokens.

    Even if the call_llm alias is monkeypatched, execution must still fail closed unless an
    explicit mock fixture (mock_response) is passed.
    """
    # 1. Unset allow_live and mock_response absent -> fails before route resolution or client creation
    with (
        patch(
            "evals.compaction.summary_usefulness.live_runner.resolve_actual_configured_auxiliary",
            side_effect=AssertionError("Route resolution should not be executed prior to authorization!"),
        ),
        patch(
            "evals.compaction.summary_usefulness.live_runner._resolve_call_client",
            side_effect=AssertionError("Client resolution should not be executed prior to authorization!"),
        ),
        patch(
            "agent.gemini_cloudcode_adapter.GeminiCloudCodeClient.count_tokens",
            side_effect=AssertionError("count_tokens network endpoint should not be invoked prior to authorization!"),
        ),
    ):
        with pytest.raises(RuntimeError, match=r"Live auxiliary inference is not authorized"):
            execute_live_compaction_request(
                transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
                ground_truth=MIGRATION_CASE_GROUND_TRUTH,
                scratch_dir=tmp_path,
                allow_live=False,
                mock_response=None,
            )

    # 2. Even if call_llm alias is monkeypatched, absence of mock_response fixture still fails closed
    monkeypatch.setattr("evals.compaction.summary_usefulness.live_runner.call_llm", MagicMock())
    with pytest.raises(RuntimeError, match=r"Live auxiliary inference is not authorized"):
        execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            allow_live=False,
            mock_response=None,
        )


def test_changed_actual_prompt_exceeds_token_limit_refusal(tmp_path: Path):
    """Proves that even when the prebuilt prompt passes the token limit, if the ACTUAL
    prompt dispatched at the transport interceptor exceeds max_input_tokens (8,000),
    the request fails closed with ValueError and halts inference.
    """
    mock_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content="## Goal\nValid summary.",
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        model="gemini-3.8-flash-high",
    )

    # Prebuilt prompt passes normally, but dynamically built prompt at compress-time is oversized
    with patch(
        "agent.context_compressor.ContextCompressor._build_summary_prompt",
        return_value="word " * 9000,  # ~11,250 tokens > 8000
    ):
        with pytest.raises(ValueError, match=r"Input token count .* exceeds maximum allowed limit of 8000 tokens \(fail closed\)"):
            execute_live_compaction_request(
                transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
                ground_truth=MIGRATION_CASE_GROUND_TRUTH,
                scratch_dir=tmp_path,
                mock_response=mock_resp,
            )

    # Also verify with an injected deterministic token counter
    def injected_counter(p: str) -> int:
        if "MIGRATION_EXPANDED_TOKEN_BLOWUP" in p:
            return 8500
        return 1000

    blowup_transcript = list(SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED) + [
        {"role": "user", "content": "MIGRATION_EXPANDED_TOKEN_BLOWUP"}
    ]
    with pytest.raises(ValueError, match=r"Input token count 8500 exceeds maximum allowed limit of 8000 tokens \(fail closed\)"):
        execute_live_compaction_request(
            transcript=blowup_transcript,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=mock_resp,
            token_counter=injected_counter,
        )


def test_internal_retry_attempts_counted_and_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Proves internal retries in call_llm (e.g. transient transport errors) are intercepted,
    counted at the physical provider send layer, and bounded to max_auxiliary_calls without unbounded retries.
    """
    import httpx
    import agent.auxiliary_client as aux_mod

    # Disable retry backoff delay for immediate test execution
    monkeypatch.setattr(aux_mod, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)

    physical_send_attempts = 0

    def flaky_mock_send(*args, **kwargs):
        nonlocal physical_send_attempts
        physical_send_attempts += 1
        raise httpx.ConnectError("connection refused")

    # With max_auxiliary_calls=1, the initial call is attempt 1, and the internal retry attempt
    # triggers physical bound check, failing closed immediately.
    with pytest.raises(RuntimeError, match=r"Auxiliary physical call bound exceeded"):
        execute_live_compaction_request(
            transcript=SYNTHETIC_MIGRATION_TRANSCRIPT_EXPANDED,
            ground_truth=MIGRATION_CASE_GROUND_TRUTH,
            scratch_dir=tmp_path,
            mock_response=flaky_mock_send,
            max_auxiliary_calls=1,
        )

    # First attempt called the mock; internal retry was intercepted and blocked before second mock execution
    assert physical_send_attempts == 1
