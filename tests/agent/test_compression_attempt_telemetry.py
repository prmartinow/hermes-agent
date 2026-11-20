import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.conversation_compression import _emit_aborted_attempt_telemetry, compress_context
from agent.context_compressor import ContextCompressor
from tests.fixtures.storage_seed import seed_messages, seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


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


def test_automatic_compaction_counts_once_in_shared_metrics(monkeypatch):
    from hermes_cli.observability import shared_metrics_events
    from hermes_cli.observability.shared_metrics_fields import compression_fields

    calls = []
    monkeypatch.setattr(shared_metrics_events, "record_compression", lambda **kw: calls.append(kw))
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    with patch.object(compressor, "_generate_summary", return_value="SANITIZED SUMMARY"):
        compress_context(agent, _messages(), "system prompt", approx_tokens=80_000)

    assert [compression_fields(**kw) for kw in calls] == [
        {"trigger": "auto", "outcome": "success", "context_fill_bucket": "75_to_90", "failure_class": "none"}
    ]


def test_structural_no_op_is_counted_skipped_with_the_compressors_class(caplog, monkeypatch):
    """A transcript that fits inside the protected tail makes no summary call: the attempt log names
    ``no_compressible_window`` (not the caller's generic ``no_progress``) and the shared metric counts
    it ``skipped``, not ``failed`` (#131412)."""
    from hermes_cli.observability import shared_metrics_events
    from hermes_cli.observability.shared_metrics_fields import compression_fields

    recorded = []
    monkeypatch.setattr(shared_metrics_events, "record_compression", lambda **kw: recorded.append(kw))
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    agent = _Agent(compressor)
    messages = _messages()

    with patch.object(compressor, "_find_tail_cut_by_tokens", return_value=0):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, _ = compress_context(agent, messages, "system prompt", approx_tokens=75_000)

    assert compressed == messages
    payload = _extract_telemetry(caplog)
    assert (payload["commit_status"], payload["failure_class"]) == ("aborted", "no_compressible_window")
    assert [(f["outcome"], f["failure_class"]) for f in (compression_fields(**kw) for kw in recorded)] == [
        ("skipped", "no_compressible_window")
    ]


@pytest.mark.parametrize(
    ("recorded", "caller", "expected"),
    [
        # Nothing recorded: the caller's verdict is the only information there is.
        (None, "no_progress", "no_progress"),
        # Sibling generic verdict: a terminal summary failure keeps its documented class.
        ("summary_auth_failure", "summary_generation_aborted", "summary_auth_failure"),
        # A caller-side event the compressor cannot see still outranks a recorded class.
        ("no_compressible_window", "attempt_superseded", "attempt_superseded"),
        # A snapshot restore put the PREVIOUS attempt's telemetry back: its class is not this attempt's.
        (("no_compressible_window", "previous-attempt"), "no_progress", "no_progress"),
    ],
)
def test_generic_caller_verdicts_defer_to_the_recorded_failure_class(caplog, monkeypatch, recorded, caller, expected):
    from hermes_cli.observability import shared_metrics_events

    recorded, attempt_id = recorded if isinstance(recorded, tuple) else (recorded, "this-attempt")
    monkeypatch.setattr(shared_metrics_events, "finish_compression_attempt", lambda *_args, **_kwargs: None)
    compressor = SimpleNamespace(
        _last_compression_telemetry={"failure_class": recorded, "attempt_id": attempt_id}, context_length=100_000,
    )
    agent = SimpleNamespace(
        context_compressor=compressor, session_id="session-telemetry-test", _compression_attempt_id="this-attempt",
    )

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        _emit_aborted_attempt_telemetry(agent, time.monotonic(), caller)

    assert _extract_telemetry(caplog)["failure_class"] == expected


def test_in_place_commit_preserves_seeded_telemetry_fields(caplog):
    """#118580: the memory-flush session-end inside the commit must not wipe attempt telemetry."""
    import contextlib
    import os
    import tempfile
    from pathlib import Path

    from agent.conversation_compression import CompressionCommitFence, run_compress_context_with_progress_timeout

    with tempfile.TemporaryDirectory() as tmp:
        from persistence_assembly import open_persistence, profile_scope
        from run_agent import AIAgent

        db = open_persistence(profile_scope(home=Path(tmp)))
        sid = "20260920_234956_0c74e527"
        seed_session(db, sid, "cli", model="test/model")
        for idx in range(8):
            seed_messages(
                db, sid, {"role": "user" if idx % 2 == 0 else "assistant", "content": f"msg {idx}"},
            )
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                quiet_mode=True, persistence=db, session_id=sid,
                skip_context_files=True, skip_memory=True,
            )
        agent.compression_in_place = True
        agent._compression_feasibility_checked = True
        messages = [
            {"role": "user" if idx % 2 == 0 else "assistant",
             "content": f"turn message {idx} padding " * 20}
            for idx in range(30)
        ]

        def worker(fence):
            return compress_context(
                agent, messages, "system prompt",
                approx_tokens=75_000, force=True, commit_fence=fence,
            )

        with patch.object(agent.context_compressor, "_generate_summary", return_value="SUMMARY"):
            with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
                run_compress_context_with_progress_timeout(
                    worker=worker, messages=messages, system_prompt_fallback="system prompt",
                    idle_timeout_seconds=30, total_ceiling_seconds=120, fence=CompressionCommitFence(),
                    telemetry_agent=agent, stall_fallback=False,
                )
        with contextlib.suppress(Exception):
            db.close()
    payload = _extract_telemetry(caplog)
    assert payload["commit_status"] == "committed"
    assert payload["split_status"] == "in_place_committed"
    for key in (
        "trigger_source", "main_model", "middle_window_tokens",
        "protected_head_tokens", "protected_tail_tokens",
    ):
        assert key in payload, f"seeded field lost: {key}"
    assert payload["middle_window_tokens"] is not None


def test_an_overlapping_blocked_call_keeps_the_running_attempts_telemetry():
    """A call that stops at the automatic gate must not erase the record of an attempt still summarizing: the
    previous-attempt clear (#118580) happens only once an attempt owns the lease and dispatches its summary."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    running = {"event": "compression_attempt", "attempt_id": "running-attempt", "method": "llm_summary"}
    compressor._last_compression_telemetry = compressor._active_compression_telemetry = running
    compressor._compression_telemetry_seed = {"attempt_id": "running-attempt"}
    compressor._summary_failure_cooldown_until = time.monotonic() + 600
    agent = _Agent(compressor)

    from agent.conversation_compression import compress_context

    with patch.object(type(compressor), "_refresh_durable_guards"):
        compress_context(agent, [{"role": "user", "content": "hi"}] * 4, "sys", approx_tokens=80_000)

    assert compressor._last_compression_telemetry is running
    assert compressor._active_compression_telemetry is running
    assert compressor._compression_telemetry_seed == {"attempt_id": "running-attempt"}


def test_a_lock_contended_call_keeps_the_running_attempts_telemetry(tmp_path):
    """The lock sit-out shares the gate's rule: the holder's record on a shared compressor must survive, and the
    contender logs its own ``lock_contended`` record from its seed."""
    import os

    db = storage_handle(tmp_path)
    seed_session(db, "S", "cli")
    assert db.coordination.try_acquire_compression_lock(session_id="S", holder="winner", ttl_seconds=60)
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
            persistence=db, session_id="S", skip_context_files=True, skip_memory=True,
        )
    compressor = agent.context_compressor
    running = {"event": "compression_attempt", "attempt_id": "running-attempt", "method": "llm_summary"}
    compressor._last_compression_telemetry = compressor._active_compression_telemetry = running
    try:
        agent._compress_context([{"role": "user", "content": f"m{i}"} for i in range(20)], "sys", force=True)
        assert agent._compression_skipped_due_to_lock
        assert compressor._last_compression_telemetry is running
        assert compressor._active_compression_telemetry is running
    finally:
        agent.close()


def test_pool_saturation_emit_does_not_inherit_previous_attempt_telemetry(caplog):
    """The pool-refusal emit fires before any attempt begins: no previous attempt's numbers ride along."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    compressor._last_compression_telemetry = {
        "event": "compression_attempt", "attempt_id": "prior-attempt", "session_id": "prior-session",
        "commit_ms": 73, "middle_window_tokens": 2407,
    }
    compressor._active_compression_telemetry = compressor._last_compression_telemetry
    agent = _Agent(compressor)

    from agent.conversation_compression import CompressionCommitFence, run_compress_context_with_progress_timeout

    with patch("agent.conversation_compression._try_admit_compression_job", return_value=False):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            run_compress_context_with_progress_timeout(
                worker=lambda fence: ([], "system prompt"), messages=[], system_prompt_fallback="system prompt",
                idle_timeout_seconds=30, total_ceiling_seconds=120, fence=CompressionCommitFence(),
                telemetry_agent=agent, stall_fallback=False,
            )

    payload = _extract_telemetry(caplog)
    assert payload["failure_class"] == "pool_saturated"
    assert payload["session_id"] == "session-telemetry-test"
    assert payload.get("commit_ms") is None
    assert payload.get("middle_window_tokens") is None


def test_late_unwinding_attempt_keeps_its_own_identity_after_a_newer_attempt_starts(caplog):
    """A stalled attempt that unwinds after its stall fallback began must log its own attempt id, session
    and trigger, not the newer attempt's (they overwrite the agent's current-attempt fields)."""
    import threading

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)
    agent.session_id = "session-A"
    stalled, release = threading.Event(), threading.Event()
    calls = []

    def _summary(*_args, **_kwargs):
        calls.append(None)
        if len(calls) == 1:
            stalled.set()
            release.wait(10)
        return "SANITIZED SUMMARY"

    with patch.object(compressor, "_generate_summary", side_effect=_summary):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            first = threading.Thread(
                target=compress_context, args=(agent, _messages(), "system prompt"),
                kwargs={"approx_tokens": 80_000, "trigger": "pre_api"},
            )
            first.start()
            assert stalled.wait(10)
            agent.session_id = "session-B"
            compress_context(agent, _messages(), "system prompt", approx_tokens=80_000, trigger="post_tool")
            release.set()
            first.join(10)

    records = [
        json.loads(record.getMessage().split("context compression attempt telemetry: ", 1)[1])
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    by_trigger = {payload["trigger_source"]: payload for payload in records}
    assert len(records) == 2 and set(by_trigger) == {"pre_api", "post_tool"}
    assert by_trigger["pre_api"]["session_id"] == "session-A"
    assert by_trigger["post_tool"]["session_id"] == "session-B"
    assert by_trigger["pre_api"]["attempt_id"] != by_trigger["post_tool"]["attempt_id"]


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
    from tests.fixtures.storage_seed import seed_messages, seed_session
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    db = storage_handle(tmp_path)
    sid = "20260920_120000_inplace"
    seed_session(db, sid, "cli", model="test/main-model")
    for i in range(10):
        seed_messages(db, sid, {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"})

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
    agent._session_db = agent.persistence = db

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
            compressed, _system_prompt = compress_context(
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
    from tests.fixtures.storage_seed import seed_session
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    db = storage_handle(tmp_path)
    sid = "20260920_120000_abort"
    seed_session(db, sid, "cli", model="test/main-model")

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
    agent._session_db = agent.persistence = db

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
            _res_msgs, _ = compress_context(
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
            _res_msgs2, _ = compress_context(
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
        _res_msgs, _ = compress_context(
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
