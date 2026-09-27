"""Regression tests for compaction stall classification, cooldown escalation, and alias handling.

Pins:
1. Actual TimeoutError stream stall classification (direct, empty-string, chained __cause__,
   type-name matching, and 'stalled: no new output' text).
2. Cooldown escalation ladder (60s -> 300s -> 900s) on consecutive timeout failures.
3. Fallback from auxiliary summary model to main model on stream stall TimeoutError,
   with ladder escalation when both fail.
4. Regression baseline comparison proving old classifier misclassified stalls as streaming_closed/failed.
5. gpt-6-astra-900k alias native compaction eligibility on official Codex OAuth only.
6. Wire canonicalization of gpt-6-astra-900k to gpt-6-astra via transport helper (no live inference).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import _is_connection_error
from agent.context_compressor import (
    ContextCompressor,
    _SummaryFailureKind,
    _TIMEOUT_COOLDOWN_LADDER,
    _TRUNCATED_SUMMARY_MARKER,
    _classify_summary_failure,
    _exc_status_code,
    _next_timeout_cooldown,
)
from agent.error_classifier import FailoverReason, classify_api_error
from agent.native_compaction import (
    DEFAULT_COMPACT_THRESHOLD,
    is_native_compaction_model,
    native_compaction_context_management,
    resolve_native_compaction_capabilities,
)
from agent.transports.codex import ResponsesApiTransport

_CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _old_classify_summary_failure(e: Exception) -> _SummaryFailureKind:
    """Isolated pre-fix classifier for regression baseline verification."""
    status = _exc_status_code(e)
    err = str(e).lower()
    return _SummaryFailureKind(
        model_not_found=status in {404, 503}
        or any(m in err for m in ("model_not_found", "does not exist", "no available channel")),
        timeout=status in {408, 429, 502, 504} or "timeout" in err or "timed out" in err,
        json_decode=isinstance(e, json.JSONDecodeError) or "expecting value" in err,
        streaming_closed=_is_connection_error(e),
        empty_content=isinstance(e, RuntimeError)
        and any(
            m in err
            for m in (
                "empty content",
                "refusal content",
                "llm returned none response",
                "llm returned invalid response",
            )
        ),
        truncated=isinstance(e, RuntimeError) and _TRUNCATED_SUMMARY_MARKER in err,
        overloaded=classify_api_error(e).reason is FailoverReason.overloaded
        or any(marker in err for marker in ("overloaded", "at capacity", "over capacity")),
    )


def _sample_messages():
    return [
        {"role": "user", "content": "analyze data"},
        {"role": "assistant", "content": "processing"},
    ]


class TestCompactionStallRegression:
    def test_timeout_error_stream_stall_classification(self):
        """TimeoutError and stall markers classify as timeout, avoiding streaming_closed misclassification."""
        # Direct TimeoutError with stream stall message
        err1 = TimeoutError("stalled: no new output")
        kind1 = _classify_summary_failure(err1)
        assert kind1.timeout is True
        assert kind1.streaming_closed is False
        assert kind1.fallback_reason() == "timed out"

        # Direct TimeoutError with empty message
        err2 = TimeoutError("")
        kind2 = _classify_summary_failure(err2)
        assert kind2.timeout is True
        assert kind2.streaming_closed is False
        assert kind2.fallback_reason() == "timed out"

        # Chained cause is TimeoutError
        err3 = RuntimeError("stream reader failed")
        err3.__cause__ = TimeoutError("stalled: no new output")
        kind3 = _classify_summary_failure(err3)
        assert kind3.timeout is True
        assert kind3.streaming_closed is False
        assert kind3.fallback_reason() == "timed out"

        # Custom timeout class name
        class StreamReadTimeout(Exception):
            pass

        err4 = StreamReadTimeout("stream dropped without bytes")
        kind4 = _classify_summary_failure(err4)
        assert kind4.timeout is True
        assert kind4.streaming_closed is False
        assert kind4.fallback_reason() == "timed out"

    def test_regression_baseline_evidence_old_vs_new(self):
        """Verifies regression baseline: old classifier failed to identify stream stalls as timeouts."""
        stall_err = TimeoutError("stalled: no new output")
        old_stall = _old_classify_summary_failure(stall_err)
        new_stall = _classify_summary_failure(stall_err)

        # Baseline flaw in old classifier: misclassified as streaming_closed, timeout was False
        assert old_stall.timeout is False
        assert old_stall.streaming_closed is True
        assert old_stall.fallback_reason() == "closed stream prematurely"

        # Fixed behavior:
        assert new_stall.timeout is True
        assert new_stall.streaming_closed is False
        assert new_stall.fallback_reason() == "timed out"

        # Chained cause flaw in old classifier:
        chained_err = RuntimeError("wrapper error")
        chained_err.__cause__ = TimeoutError("stalled: no new output")
        old_chained = _old_classify_summary_failure(chained_err)
        new_chained = _classify_summary_failure(chained_err)

        assert old_chained.timeout is False
        assert old_chained.fallback_reason() == "failed"
        assert new_chained.timeout is True
        assert new_chained.fallback_reason() == "timed out"

    def test_timeout_cooldown_escalation_ladder(self):
        """Repeated timeouts escalate via the ladder 60s -> 300s -> 900s."""
        compressor = SimpleNamespace(_consecutive_timeout_failures=0)

        # Ladder step 1: 60s
        step1 = _next_timeout_cooldown(compressor)
        assert step1 == _TIMEOUT_COOLDOWN_LADDER[0] == 60
        assert compressor._consecutive_timeout_failures == 1

        # Ladder step 2: 300s
        step2 = _next_timeout_cooldown(compressor)
        assert step2 == _TIMEOUT_COOLDOWN_LADDER[1] == 300
        assert compressor._consecutive_timeout_failures == 2

        # Ladder step 3: 900s
        step3 = _next_timeout_cooldown(compressor)
        assert step3 == _TIMEOUT_COOLDOWN_LADDER[2] == 900
        assert compressor._consecutive_timeout_failures == 3

        # Ceiling capped at 900s
        step4 = _next_timeout_cooldown(compressor)
        assert step4 == 900
        assert compressor._consecutive_timeout_failures == 4

    def test_generate_summary_timeout_stall_escalation(self):
        """ContextCompressor._generate_summary properly records timeout ladder and increments counter."""
        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            c = ContextCompressor(model="main-model", quiet_mode=True)

        stall_exc = TimeoutError("stalled: no new output")

        # 1st stall failure: cooldown 60s, counter = 1
        with patch("agent.context_compressor.call_llm", side_effect=stall_exc):
            res1 = c._generate_summary(_sample_messages())
        assert res1 is None
        assert c._consecutive_timeout_failures == 1
        assert c._last_summary_error is not None

        # Reset cooldown deadline to simulate time advancing past 60s
        c._summary_failure_cooldown_until = 0.0

        # 2nd stall failure: cooldown 300s, counter = 2
        with patch("agent.context_compressor.call_llm", side_effect=stall_exc):
            res2 = c._generate_summary(_sample_messages())
        assert res2 is None
        assert c._consecutive_timeout_failures == 2

        # Reset cooldown deadline to simulate time advancing past 300s
        c._summary_failure_cooldown_until = 0.0

        # 3rd stall failure: cooldown 900s, counter = 3
        with patch("agent.context_compressor.call_llm", side_effect=stall_exc):
            res3 = c._generate_summary(_sample_messages())
        assert res3 is None
        assert c._consecutive_timeout_failures == 3

    def test_generate_summary_aux_timeout_falls_back_to_main_and_succeeds(self, caplog):
        """Aux model stream stall falls back to main model with 'timed out' reason."""
        import logging
        mock_ok = MagicMock()
        mock_ok.choices = [MagicMock()]
        mock_ok.choices[0].message.content = "Summary generated by main model"

        stall_exc = TimeoutError("stalled: no new output")

        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            c = ContextCompressor(
                model="main-model",
                summary_model_override="aux-model",
                quiet_mode=True,
            )

        with caplog.at_level(logging.WARNING):
            with patch("agent.context_compressor.call_llm", side_effect=[stall_exc, mock_ok]) as mock_call:
                summary = c._generate_summary(_sample_messages())

        assert mock_call.call_count == 2
        # First call targeted aux-model
        assert mock_call.call_args_list[0].kwargs.get("model") == "aux-model"
        # Retry targeted main model (no model kwarg)
        assert "model" not in mock_call.call_args_list[1].kwargs
        assert summary is not None
        assert "Summary generated by main model" in summary
        assert c._last_aux_model_failure_model == "aux-model"
        assert c._last_aux_model_failure_error == "stalled: no new output"
        # Check that fallback reason passed to logger was 'timed out'
        assert any("timed out" in record.message for record in caplog.records)

    def test_generate_summary_aux_and_main_both_timeout_escalates_ladder(self):
        """When both aux model and main fallback timeout, cooldown escalation activates."""
        stall_exc1 = TimeoutError("stalled: no new output")
        stall_exc2 = TimeoutError("main model also stalled")

        with patch("agent.context_compressor.get_model_context_length", return_value=100000):
            c = ContextCompressor(
                model="main-model",
                summary_model_override="aux-model",
                quiet_mode=True,
            )

        with patch("agent.context_compressor.call_llm", side_effect=[stall_exc1, stall_exc2]) as mock_call:
            summary = c._generate_summary(_sample_messages())

        assert mock_call.call_count == 2
        assert summary is None
        assert c._consecutive_timeout_failures == 1


class TestNativeCompactionAliasAndCanonicalization:
    @pytest.mark.parametrize(
        "model,provider,base_url,eligible",
        [
            ("gpt-6-astra-900k", "openai-codex", _CODEX_URL, True),
            ("GPT-6-ASTRA-900K", "openai-codex", f"{_CODEX_URL}/", True),
            ("gpt-6-astra-900k", "openai-codex", "https://chatgpt.com:443/backend-api/codex", True),
            ("gpt-6-astra-900k", "openai", "https://api.openai.com/v1", False),
            ("gpt-6-astra-900k", "openrouter", "https://openrouter.ai/api/v1", False),
            ("gpt-6-astra-900k", "openai-codex", "https://relay.example/v1", False),
            ("gpt-6-astra-900k", "openai-codex", "http://chatgpt.com/backend-api/codex", False),
            ("gpt-6-astra-900k", "openai-codex", None, False),
        ],
    )
    def test_alias_native_eligibility_official_codex_only(self, model, provider, base_url, eligible):
        """gpt-6-astra-900k is eligible only under official Codex OAuth."""
        assert is_native_compaction_model(model, provider=provider, base_url=base_url) is eligible

        caps = resolve_native_compaction_capabilities(
            model=model,
            provider=provider,
            base_url=base_url,
            is_codex_backend=(provider == "openai-codex"),
        )
        assert caps["native_compaction"] is eligible

        agent = SimpleNamespace(
            model=model,
            provider=provider,
            base_url=base_url,
            codex_responses_native_compaction=True,
            compression_enabled=True,
            capabilities={"openai_native_compaction": True},
            runtime_capabilities=caps,
            codex_responses_compact_threshold=DEFAULT_COMPACT_THRESHOLD,
            context_compressor=None,
        )
        payload = native_compaction_context_management(
            agent, is_codex_backend=(provider == "openai-codex")
        )
        if eligible:
            assert payload == [{"type": "compaction", "compact_threshold": DEFAULT_COMPACT_THRESHOLD}]
        else:
            assert payload is None

    def test_alias_wire_canonicalization_transport_helper(self):
        """Transport helper canonicalizes gpt-6-astra-900k to gpt-6-astra on the wire (no live inference)."""
        route = {
            "base_url": _CODEX_URL,
            "provider": "openai-codex",
        }
        params = dict(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            is_codex_backend=True,
            **route,
        )

        transport = ResponsesApiTransport()
        kwargs_alias = transport.build_kwargs(model="gpt-6-astra-900k", **params)
        kwargs_canonical = transport.build_kwargs(model="gpt-6-astra", **params)

        assert kwargs_alias["model"] == "gpt-6-astra"
        assert kwargs_alias == kwargs_canonical
