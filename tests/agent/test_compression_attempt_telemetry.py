import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

from agent.conversation_compression import compress_context
from agent.context_compressor import ContextCompressor


class _TodoStore:
    def format_for_injection(self):
        return ""


class _Agent:
    def __init__(self, compressor):
        self.context_compressor = compressor
        self.session_id = "session-telemetry-test"
        self.platform = "cli"
        self.model = "test/main-model"
        self.provider = "test-provider"
        self.tools = []
        self._compression_feasibility_checked = True
        self.compression_in_place = False
        self._memory_manager = None
        self._session_db = None
        self._todo_store = _TodoStore()
        self._cached_system_prompt = None

    def _emit_status(self, _message):
        pass

    def _emit_warning(self, _message):
        pass

    def _invalidate_system_prompt(self):
        self._cached_system_prompt = None

    def _build_system_prompt(self, system_message):
        return system_message

    def commit_memory_session(self, _messages):
        pass


def _messages(secret_text="TOPSECRET_TRANSCRIPT_TEXT"):
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        msgs.append({"role": "user", "content": f"user message {idx} {secret_text}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {idx} {secret_text}"})
    return msgs


def _extract_telemetry(caplog):
    records = [
        record.getMessage()
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    assert len(records) == 1
    return json.loads(records[0].split("context compression attempt telemetry: ", 1)[1])


def test_compression_attempt_telemetry_is_metadata_only(caplog):
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    with patch.object(compressor, "_generate_summary", return_value="SANITIZED SUMMARY"):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, system_prompt = compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    assert system_prompt == "system prompt"
    assert compressed is not None
    payload = _extract_telemetry(caplog)

    assert payload["event"] == "compression_attempt"
    assert payload["attempt_id"]
    assert payload["session_id"] == "session-telemetry-test"
    assert payload["trigger_source"] == "manual"
    assert payload["main_model"] == "test/main-model"
    assert payload["main_context_limit"] == 100_000
    assert payload["current_estimated_tokens"] == 75_000
    assert payload["effective_threshold"] == compressor.threshold_tokens
    assert payload["protected_head_tokens"] is not None
    assert payload["protected_tail_tokens"] is not None
    assert payload["middle_window_tokens"] is not None
    assert payload["chunking"] is False
    assert payload["chunk_count"] in {0, 1}
    assert payload["commit_status"] == "committed"
    assert payload["split_status"] == "not_applicable"
    assert payload["fallback_used"] is False
    assert isinstance(payload["total_duration_ms"], int)
    assert isinstance(payload["commit_ms"], int)
    assert payload["queue_wait_ms"] is None
    assert payload["prompt_build_ms"] is None
    assert payload["time_to_first_progress_ms"] is None
    assert payload["summary_generation_ms"] is None

    raw_log = json.dumps(payload)
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in raw_log
    assert "SANITIZED SUMMARY" not in raw_log
    assert "user message" not in raw_log
    assert "assistant reply" not in raw_log


def test_aux_call_telemetry_records_durations_without_content(caplog):
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SANITIZED SUMMARY"))]
    )

    def _fake_call_llm(**kwargs):
        if kwargs.get("route_info") is not None:
            kwargs["route_info"].update(provider="test-aux-provider", model="test-aux-model")
        return response

    with patch("agent.context_compressor.call_llm", side_effect=_fake_call_llm):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
            )

    payload = _extract_telemetry(caplog)
    assert payload["aux_prompt_tokens"] is not None
    # Current main intentionally omits max_tokens from the aux summary call
    # (the summary budget is prompt-level guidance only), so no output
    # reservation is recorded.
    assert payload["aux_output_reservation"] is None
    assert isinstance(payload["aux_call_duration_ms"], int)
    assert payload["aux_provider"] == "test-aux-provider"
    assert payload["aux_model"] == "test-aux-model"

    raw_log = json.dumps(payload)
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in raw_log
    assert "SANITIZED SUMMARY" not in raw_log


def test_aux_call_telemetry_records_content_free_phase_timings():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor._begin_compression_telemetry(current_tokens=75_000)

    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "TOPSECRET_TRANSCRIPT_TEXT"}],
        max_tokens=1400,
        duration_ms=22,
        aux_provider="ollama",
        aux_model="qwen3:8b",
        phase_timings={
            "queue_wait_ms": 3,
            "prompt_build_ms": 5,
            "time_to_first_progress_ms": 7,
            "summary_generation_ms": 19,
            "commit_ms": 11,
        },
    )

    payload = compressor._last_compression_telemetry
    assert payload is not None
    assert {key: payload[key] for key in (
        "queue_wait_ms",
        "prompt_build_ms",
        "time_to_first_progress_ms",
        "summary_generation_ms",
        "commit_ms",
    )} == {
        "queue_wait_ms": 3,
        "prompt_build_ms": 5,
        "time_to_first_progress_ms": 7,
        "summary_generation_ms": 19,
        "commit_ms": 11,
    }
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in json.dumps(payload)


def _extract_all_telemetry(caplog):
    records = [
        record.getMessage()
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    return [json.loads(r.split("context compression attempt telemetry: ", 1)[1]) for r in records]


def test_in_place_compaction_telemetry_carries_measured_fields(caplog, tmp_path):
    """Exercising real in-place compaction carries measured prompt_build/aux/route/timing telemetry."""
    from pathlib import Path
    from hermes_state import SessionDB
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    db = SessionDB(db_path=tmp_path / "in_place_telemetry.db")
    sid = "20260920_120000_inplace"
    db.create_session(sid, "cli", model="test/main-model")
    for i in range(10):
        db.append_message(session_id=sid, role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)
    agent.compression_in_place = True
    agent.session_id = sid
    agent._session_db = db

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SANITIZED IN-PLACE SUMMARY"))],
        usage=SimpleNamespace(prompt_tokens=1420, completion_tokens=85, total_tokens=1505),
    )

    def _stub_call_llm(**kwargs):
        if kwargs.get("route_info") is not None:
            kwargs["route_info"].update(provider="stub-provider", model="stub-aux-model", reasoning="low")
        lat = kwargs.get("latency_info")
        if lat is not None:
            lat["queue_wait_ms"] = 14
            lat["time_to_first_progress_ms"] = 38
            lat["summary_generation_ms"] = 92
        return response

    messages = [{"role": "system", "content": "system prompt"}]
    for idx in range(30):
        messages.append({"role": "user", "content": f"user turn {idx} SECRET_AUDIT_DATA " * 20})
        messages.append({"role": "assistant", "content": f"assistant turn {idx} SECRET_AUDIT_DATA " * 20})

    with patch("agent.context_compressor.call_llm", side_effect=_stub_call_llm):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, system_prompt = compress_context(
                agent,
                messages,
                "system prompt",
                approx_tokens=75_000,
            )

    assert compressed is not None
    payload = _extract_telemetry(caplog)

    assert payload["commit_status"] == "committed"
    assert payload["split_status"] == "in_place_committed"
    assert payload["fallback_used"] is False
    assert payload["session_id"] == sid
    assert payload["aux_provider"] == "stub-provider"
    assert payload["aux_model"] == "stub-aux-model"
    assert payload["effective_reasoning"] == "low"
    assert isinstance(payload["aux_prompt_tokens"], int) and payload["aux_prompt_tokens"] > 0
    assert payload["estimated_aux_prompt_tokens"] == payload["aux_prompt_tokens"]
    assert isinstance(payload["aux_prompt_chars"], int) and payload["aux_prompt_chars"] > 0
    assert isinstance(payload["aux_prompt_bytes"], int) and payload["aux_prompt_bytes"] > 0
    assert payload["aux_provider_prompt_tokens"] == 1420
    assert payload["aux_provider_completion_tokens"] == 85
    assert payload["aux_provider_total_tokens"] == 1505
    assert isinstance(payload["prompt_build_ms"], int)
    assert payload["queue_wait_ms"] == 14
    assert payload["time_to_first_progress_ms"] == 38
    assert payload["summary_generation_ms"] == 92
    assert isinstance(payload["commit_ms"], int)
    assert isinstance(payload["total_duration_ms"], int)
    assert payload["total_duration_ms"] >= payload["commit_ms"]
    assert payload["chunking"] is False
    assert payload["chunk_count"] == 1

    raw_log = json.dumps(payload)
    assert "SECRET_AUDIT_DATA" not in raw_log
    assert "SANITIZED IN-PLACE SUMMARY" not in raw_log


def test_aborted_attempt_preserves_measured_metrics_and_resets_for_next_attempt(caplog, tmp_path):
    """Aborted attempt preserves measured prompt_build/aux telemetry without leaking into next attempt."""
    from hermes_state import SessionDB
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    db = SessionDB(db_path=tmp_path / "abort_telemetry.db")
    sid = "20260920_120000_abort"
    db.create_session(sid, "cli", model="test/main-model")

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)
    agent.compression_in_place = True
    agent.session_id = sid
    agent._session_db = db

    def _cancelling_call_llm(**kwargs):
        if kwargs.get("route_info") is not None:
            kwargs["route_info"].update(provider="abort-provider", model="abort-model")
        lat = kwargs.get("latency_info")
        if lat is not None:
            lat["queue_wait_ms"] = 7
        raise AuxiliaryExplicitCancellation()

    messages = [{"role": "system", "content": "system prompt"}]
    for idx in range(30):
        messages.append({"role": "user", "content": f"user turn {idx} payload " * 20})
        messages.append({"role": "assistant", "content": f"assistant turn {idx} payload " * 20})

    # Attempt 1: Cancelled mid-call
    with patch("agent.context_compressor.call_llm", side_effect=_cancelling_call_llm):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            res_msgs, _ = compress_context(
                agent,
                messages,
                "system prompt",
                approx_tokens=75_000,
            )

    records = _extract_all_telemetry(caplog)
    assert len(records) == 1
    attempt1 = records[0]
    assert attempt1["commit_status"] == "aborted"
    assert attempt1["split_status"] == "aborted"
    assert attempt1["failure_class"] == "explicit_interrupt"
    assert attempt1["aux_provider"] == "abort-provider"
    assert attempt1["aux_model"] == "abort-model"
    assert isinstance(attempt1["prompt_build_ms"], int)
    assert attempt1["queue_wait_ms"] == 7
    assert isinstance(attempt1["aux_prompt_tokens"], int)
    assert attempt1["commit_ms"] is None

    # Attempt 2: Succeeds on same session; must have fresh attempt_id and isolated metrics
    response2 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SUCCESSFUL RETRY SUMMARY"))],
        usage=SimpleNamespace(prompt_tokens=1100, completion_tokens=50, total_tokens=1150),
    )

    def _success_call_llm(**kwargs):
        if kwargs.get("route_info") is not None:
            kwargs["route_info"].update(provider="success-provider", model="success-model")
        lat = kwargs.get("latency_info")
        if lat is not None:
            lat["queue_wait_ms"] = 3
            lat["time_to_first_progress_ms"] = 15
            lat["summary_generation_ms"] = 55
        return response2

    caplog.clear()
    with patch("agent.context_compressor.call_llm", side_effect=_success_call_llm):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            res_msgs2, _ = compress_context(
                agent,
                messages,
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    records2 = _extract_all_telemetry(caplog)
    assert len(records2) == 1
    attempt2 = records2[0]
    assert attempt2["attempt_id"] != attempt1["attempt_id"]
    assert attempt2["commit_status"] == "committed"
    assert attempt2["split_status"] == "in_place_committed"
    assert attempt2["aux_provider"] == "success-provider"
    assert attempt2["aux_model"] == "success-model"
    assert attempt2["queue_wait_ms"] == 3
    assert attempt2["summary_generation_ms"] == 55
    assert attempt2["aux_provider_total_tokens"] == 1150


def test_early_structural_abort_does_not_claim_default_chunking_measured(caplog):
    """An attempt aborted before window scan represents chunking as unmeasured, not default measured."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    agent = _Agent(compressor)

    # 2 messages is below _min_for_compress (insufficient_messages)
    short_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        res_msgs, _ = compress_context(
            agent,
            short_messages,
            "system prompt",
            approx_tokens=10_000,
            force=True,
        )

    records = _extract_all_telemetry(caplog)
    assert len(records) == 1
    payload = records[0]
    assert payload["commit_status"] == "aborted"
    # Chunking was never evaluated/run
    assert payload["chunking"] is None
    assert payload["chunk_count"] is None



def test_prior_fallback_does_not_contaminate_subsequent_aborted_or_saturated_attempt(caplog):
    """An attempt following a prior fallback attempt does not borrow fallback_used on abort or saturation."""
    from agent.conversation_compression import run_compress_context_with_progress_timeout
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    # Simulate prior attempt having used fallback:
    compressor._last_summary_fallback_used = True
    assert compressor._last_summary_fallback_used is True

    # Sub-case A: Subsequent aborted attempt (e.g. cancellation)
    def _cancelling_call_llm(**kwargs):
        if kwargs.get("route_info") is not None:
            kwargs["route_info"].update(provider="abort-provider", model="abort-model")
        raise AuxiliaryExplicitCancellation()

    with patch("agent.context_compressor.call_llm", side_effect=_cancelling_call_llm):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
            )

    records = _extract_all_telemetry(caplog)
    assert len(records) == 1
    abort_payload = records[0]
    assert abort_payload["commit_status"] == "aborted"
    assert abort_payload["fallback_used"] is False
    # Compressor restored state still has previous fallback flag, but attempt did not borrow it:
    assert compressor._last_summary_fallback_used is True

    # Sub-case B: Saturated attempt (pool refused admission)
    caplog.clear()
    prior_last_telemetry = compressor._last_compression_telemetry
    with patch("agent.conversation_compression._try_admit_compression_job", return_value=False):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            run_compress_context_with_progress_timeout(
                worker=lambda fence: (_messages(), "fallback"),
                messages=_messages(),
                system_prompt_fallback=lambda: "fallback",
                idle_timeout_seconds=1.0,
                total_ceiling_seconds=5.0,
                telemetry_agent=agent,
            )

    sat_records = _extract_all_telemetry(caplog)
    assert len(sat_records) == 1
    sat_payload = sat_records[0]
    assert sat_payload["commit_status"] == "aborted"
    assert sat_payload["failure_class"] == "pool_saturated"
    assert sat_payload["fallback_used"] is False
    # Saturation must not mutate compressor's _last_compression_telemetry or _active_compression_telemetry:
    assert compressor._last_compression_telemetry is prior_last_telemetry
    assert getattr(compressor, "_active_compression_telemetry", None) is prior_last_telemetry


def test_unavailable_route_leaves_provider_and_model_unresolved(caplog):
    """When route_info is not populated by call_llm, aux_provider and aux_model stay None (unresolved)."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SANITIZED SUMMARY"))]
    )

    # call_llm returns without updating route_info (simulating unavailable route)
    with patch("agent.context_compressor.call_llm", return_value=response):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
            )

    payload = _extract_telemetry(caplog)
    assert payload["commit_status"] == "committed"
    assert payload["aux_provider"] is None
    assert payload["aux_model"] is None


def test_two_aux_calls_differing_usage_and_timing_behavior():
    """Per-call usage reflects current call or clears when absent; duration accumulates."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor._begin_compression_telemetry(current_tokens=75_000)

    # Call 1: has usage
    resp1 = SimpleNamespace(usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150})
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "prompt 1"}],
        max_tokens=500,
        duration_ms=25,
        aux_provider="provider-1",
        aux_model="model-1",
        response=resp1,
    )
    t1 = compressor._last_compression_telemetry
    assert t1["aux_call_duration_ms"] == 25
    assert t1["aux_provider"] == "provider-1"
    assert t1["aux_model"] == "model-1"
    assert t1["aux_provider_prompt_tokens"] == 120
    assert t1["aux_provider_completion_tokens"] == 30
    assert t1["aux_provider_total_tokens"] == 150

    # Call 2: differing usage
    resp2 = SimpleNamespace(usage={"prompt_tokens": 280, "completion_tokens": 60, "total_tokens": 340})
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "prompt 2"}],
        max_tokens=600,
        duration_ms=40,
        aux_provider="provider-2",
        aux_model="model-2",
        response=resp2,
    )
    t2 = compressor._last_compression_telemetry
    assert t2["aux_call_duration_ms"] == 65  # 25 + 40 accumulated
    assert t2["aux_provider"] == "provider-2"
    assert t2["aux_model"] == "model-2"
    assert t2["aux_provider_prompt_tokens"] == 280  # per-call usage of Call 2
    assert t2["aux_provider_completion_tokens"] == 60
    assert t2["aux_provider_total_tokens"] == 340

    # Call 3: absent usage (response is None) clears usage fields while accumulating duration
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "prompt 3"}],
        max_tokens=600,
        duration_ms=15,
        aux_provider="provider-3",
        aux_model="model-3",
        response=None,
    )
    t3 = compressor._last_compression_telemetry
    assert t3["aux_call_duration_ms"] == 80  # 65 + 15 accumulated
    assert t3["aux_provider"] == "provider-3"
    assert t3["aux_model"] == "model-3"
    assert t3["aux_provider_prompt_tokens"] is None  # cleared, not preserved
    assert t3["aux_provider_completion_tokens"] is None
    assert t3["aux_provider_total_tokens"] is None


def test_effective_reasoning_rejects_arbitrary_dict_content():
    """Effective reasoning accepts allowlisted effort values but rejects arbitrary dicts/content."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor._begin_compression_telemetry(current_tokens=75_000)

    # Arbitrary dict with sensitive content must not be stringified into telemetry
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "test"}],
        max_tokens=500,
        duration_ms=10,
        reasoning={"secret_key": "private_data_value"},
    )
    payload = compressor._last_compression_telemetry
    assert payload["effective_reasoning"] is None
    assert "private_data_value" not in json.dumps(payload)

    # Allowlisted effort string is preserved
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "test"}],
        max_tokens=500,
        duration_ms=10,
        reasoning="high",
    )
    assert compressor._last_compression_telemetry["effective_reasoning"] == "high"

    # Allowlisted dict with effort is extracted
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "test"}],
        max_tokens=500,
        duration_ms=10,
        reasoning={"effort": "low"},
    )
    assert compressor._last_compression_telemetry["effective_reasoning"] == "low"
