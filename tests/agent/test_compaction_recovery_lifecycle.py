"""Compaction recovery lifecycle regressions: restart, no-reduction, and oversized unanchored resume.

This suite reproduces and guards the compaction recovery lifecycle:
1. Restore between compaction commit and next real usage:
   - Verifies transcript state, tool-pair integrity, and usage anchor clearing upon compaction commit.
   - Reproduces and tests model switch usage leakage: switching models must not leak stale usage anchors
     from the previous model, nor persist stale anchors reflexively across model boundaries.
2. Equal-size / no-reduction repeated summaries loop bounded:
   - When summary candidate produces no progress (equal size / no reduction), structural backoff is armed.
   - In provider overflow recovery, repeated failing requests are strictly bounded by max_compression_attempts;
     no infinite retry loops or repeated identical failing requests.
3. Oversized unanchored resume -> overflow -> compression -> successful next request:
   - Tests end-to-end production turn orchestration with mocked provider:
     Cold resume with unanchored oversized history defers preflight to real usage, hits provider context overflow,
     triggers reactive overflow recovery with cooldown bypass, compacts history preserving tool-call / tool-result pairs,
     and successfully completes on retry with authoritative usage anchoring.

LABELLING:
- Mocked Provider: Mocked LLM responses/errors at the client / call_llm boundary (OpenAI ChatCompletion mocks).
- Real Production Orchestration: Real AIAgent, real SessionDB (SQLite), real turn_preflight, real turn_overflow,
  real conversation_compression, real usage_anchor, and real context_compressor algorithms.
"""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import openai

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import (
    compress_context,
    _candidate_rejected,
    compression_blocked_transiently,
)
from agent.error_classifier import ClassifiedError, FailoverReason
from agent.turn_overflow import recover_from_overflow
from agent.turn_preflight import PreflightGateVerdict, run_preflight_compression
from agent.turn_retry_state import TurnRetryState
from agent.usage_anchor import (
    capture_usage_anchor,
    set_usage_anchor,
    restore_usage_anchor,
    anchored_context_tokens,
    USAGE_ANCHOR_MODEL_CONFIG_KEY,
)
from agent.turn_context import _preflight_request_tokens
from hermes_state import SessionDB
from tests.agent.test_run_agent import _mock_response


def _create_test_agent(tmp_path: Path, session_id: str, model: str = "gpt-4o", provider: str = "openai"):
    """Helper to build an AIAgent with an isolated SQLite SessionDB and synthetic HERMES_HOME."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
        db = SessionDB(db_path=hermes_home / "state.db")
        db.create_session(session_id, source="cli")
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"),
        ):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key-mock",
                base_url="https://api.openai.com/v1",
                provider=provider,
                api_mode="chat_completions",
                model=model,
                quiet_mode=True,
                session_db=db,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
            )
        agent.client = MagicMock()
        agent.compression_enabled = True
        agent.context_compressor.protect_first_n = 1
        agent.context_compressor.protect_last_n = 2
        agent.context_compressor.tail_token_budget = 500
        return db, agent


def _build_history_with_tool_pairs(num_turns: int = 20):
    """Build bulky conversation history with valid assistant(tool_calls) and tool results."""
    history = []
    for i in range(num_turns):
        history.append({"role": "user", "content": f"User query {i}: " + "massive data block " * 100})
        history.append({"role": "assistant", "content": f"Assistant response {i}: " + "massive info payload " * 100})
    # Add a paired tool call and tool result in the sequence
    call_id = "call_test_123"
    history.append({
        "role": "assistant",
        "content": "Calling tool",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "test_search", "arguments": "{\"q\": \"hermes\"}"},
        }],
    })
    history.append({
        "role": "tool",
        "tool_call_id": call_id,
        "content": "Tool output payload: " + "data payload " * 100,
    })
    return history


class TestCompactionCommitRestoreLifecycle:
    """Lifecycle tests: restore between compaction commit and next real usage,
    and stale usage anchor isolation across model switch."""

    def test_restore_between_compaction_commit_and_next_real_usage(self, tmp_path: Path):
        """REAL PRODUCTION ORCHESTRATION with MOCKED PROVIDER LLM summary:
        Between compaction commit and the arrival of next real usage:
        - Compaction commits rewritten transcript to SQLite.
        - Usage anchor is cleared (set_usage_anchor(agent, None)).
        - When a fresh agent restores the session before next usage, it must not adopt
          a stale anchor or leave orphaned tool calls / results."""
        db, agent = _create_test_agent(tmp_path, "SESS_RESTORE_LIFECYCLE")
        history = _build_history_with_tool_pairs(20)

        # Mocked Provider: summary generation returns concise summary
        mock_summary = "## Goal\nConsolidated prior tasks.\n## State\nTool completed."
        with (
            patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(MagicMock(), "aux-model")),
            patch("agent.context_compressor.call_llm", return_value=_mock_response(mock_summary)),
        ):
            compacted_msgs, _ = compress_context(agent, history, "sys_prompt", approx_tokens=60_000)

        # Verify compaction occurred and usage anchor was cleared at commit
        assert len(compacted_msgs) < len(history)
        assert agent._usage_anchor is None
        assert db.get_session_model_config_value("SESS_RESTORE_LIFECYCLE", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None

        # Verify tool pairing preserved in compacted messages: no orphaned tool calls or results
        tool_call_ids = {
            tc["id"]
            for m in compacted_msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        tool_result_ids = {
            m["tool_call_id"]
            for m in compacted_msgs
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        assert tool_call_ids == tool_result_ids, "Compaction must not orphan tool calls or tool results"

        # Now simulate session restore in a fresh process / agent
        hermes_home = tmp_path / "hermes_home"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            db2, agent2 = _create_test_agent(tmp_path, "SESS_RESTORE_LIFECYCLE")
            restore_usage_anchor(agent2, compacted_msgs)
            assert agent2._usage_anchor is None, "Restored agent must be unanchored before next real usage"

            # Preflight token estimate on restored unanchored session must be unanchored
            preflight_est = _preflight_request_tokens(agent2, compacted_msgs, "sys_prompt")
            assert agent2._request_pressure_anchored is False
            assert preflight_est > 0

    def test_no_stale_usage_across_model_switch_regression(self, tmp_path: Path):
        """REGRESSION TEST:
        When an agent switches model via switch_model(), stale usage anchors from the old
        model must NOT contaminate preflight or persist across the model boundary."""
        db, agent = _create_test_agent(tmp_path, "SESS_MODEL_SWITCH", model="openai/gpt-4o", provider="openai")
        msgs = [{"role": "user", "content": "Initial short message"}]

        # Provider reported 180,000 prompt tokens on Model A (e.g. gpt-4o with large context)
        anchor_a = capture_usage_anchor(180_000, 50, msgs)
        set_usage_anchor(agent, anchor_a)
        assert agent._usage_anchor["prompt_tokens"] == 180_000

        # Switch to Model B (e.g. a smaller model with 128k context)
        with patch("hermes_cli.config.load_config", return_value={}):
            agent.switch_model(
                "anthropic/claude-3-5-haiku",
                "openrouter",
                base_url="https://openrouter.ai/api/v1",
            )

        # switch_model must invalidate / clear both usage anchors and session db anchor
        assert getattr(agent, "_usage_anchor", None) is None, (
            "switch_model must clear agent._usage_anchor on model switch"
        )
        assert getattr(agent, "_turn_base_usage_anchor", None) is None, (
            "switch_model must clear agent._turn_base_usage_anchor on model switch"
        )
        assert db.get_session_model_config_value("SESS_MODEL_SWITCH", USAGE_ANCHOR_MODEL_CONFIG_KEY, None) is None, (
            "switch_model must clear persisted usage anchor in session db on model switch"
        )

        # Preflight on Model B must NOT use Model A's anchor:
        tokens = _preflight_request_tokens(agent, msgs, "sys_prompt")
        assert tokens < 180_000, "Model B preflight pressure must not use stale anchor from Model A"
        assert agent._request_pressure_anchored is False


class TestEqualSizeAndNoReductionRepeatedSummariesLoop:
    """Verifies that equal-size or no-reduction summaries do not trigger infinite retry loops."""

    def test_equal_size_summary_rejected_arms_structural_backoff(self, tmp_path: Path):
        """REAL PRODUCTION ORCHESTRATION:
        When compression produces an identical / equal-size candidate, _candidate_rejected
        arms structural backoff and skips boundary rewrite, preventing repeated identical passes."""
        db, agent = _create_test_agent(tmp_path, "SESS_EQUAL_SIZE")
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]

        # Mock compressor to return unchanged messages (equal size / no reduction)
        agent.context_compressor.compress = lambda m, **k: list(m)

        out, _ = compress_context(agent, msgs, "sys", approx_tokens=50_000)
        assert out == msgs, "Unchanged messages must be returned as-is"
        assert agent.context_compressor._structural_no_op_backoff_until > 0, "Structural backoff must be armed"

        # On the next turn, should_compress_info must report blocked by structural backoff
        should_compress, reason = agent.context_compressor.should_compress_info(100_000)
        assert should_compress is False
        assert reason is not None and reason.startswith("structural_backoff")

    def test_no_reduction_overflow_loop_bounded_by_max_attempts(self, tmp_path: Path):
        """REAL PRODUCTION ORCHESTRATION:
        In recover_from_overflow, if compression candidates repeatedly fail to reduce tokens,
        the loop is strictly bounded by max_compression_attempts and terminates with failed=True."""
        db, agent = _create_test_agent(tmp_path, "SESS_OVERFLOW_BOUNDED")
        msgs = [{"role": "user", "content": "x" * 500} for _ in range(10)]

        # Mock compression to return 1 message of equal total size (no reduction in tokens)
        agent._compress_context = lambda m, s, **k: ([{"role": "user", "content": "x" * 5000}], s)

        classified = ClassifiedError(reason=FailoverReason.context_overflow)
        attempts = 0
        max_attempts = 3
        verdict_history = []

        while attempts < 5:
            _retry = TurnRetryState()
            verdict = recover_from_overflow(
                agent,
                RuntimeError("prompt is too long: 130000 > 100000"),
                classified,
                _retry,
                status_code=400,
                error_msg="prompt is too long: 130000 > 100000",
                wrapped_output_cap_budget=None,
                messages=list(msgs),
                api_messages=list(msgs),
                system_message="sys",
                active_system_prompt="sys",
                conversation_history=list(msgs),
                approx_tokens=130_000,
                compression_attempts=attempts,
                max_compression_attempts=max_attempts,
                api_call_count=1,
                effective_task_id=None,
            )
            verdict_history.append(verdict)
            if verdict.action == "break":
                attempts = verdict.compression_attempts
            else:
                break

        # Must exit within max_attempts and fail the turn gracefully
        assert len(verdict_history) == max_attempts + 1
        final_verdict = verdict_history[-1]
        assert final_verdict.action == "return"
        assert final_verdict.result is not None
        assert final_verdict.result.get("failed") is True
        assert final_verdict.result.get("completed") is False


class TestOversizedUnanchoredResumeAndRecovery:
    """End-to-end turn orchestration testing cold unanchored resume, provider overflow,
    reactive compaction with cooldown bypass, and subsequent successful retry."""

    def test_oversized_unanchored_resume_overflow_then_compression_then_success(self, tmp_path: Path):
        """REAL PRODUCTION ORCHESTRATION with MOCKED PROVIDER:
        1. Agent resumes oversized session with no usage anchor (unanchored).
        2. Preflight defers compression to await authoritative provider usage.
        3. Provider call #1 returns 400 Context Overflow (mocked).
        4. recover_from_overflow catches error and forces compression (bypass_cooldown=True).
        5. Messages are compacted into bounds, preserving tool pairs.
        6. Provider call #2 returns 200 OK + assistant message + valid usage (mocked).
        7. Turn succeeds with final_response, exactly 2 calls, and clean usage anchor."""
        db, agent = _create_test_agent(tmp_path, "SESS_E2E_OVERFLOW", model="gpt-4o", provider="openai")

        history = []
        for i in range(25):
            history.append({"role": "user", "content": f"User query {i}: " + "massive data block " * 300})
            history.append({"role": "assistant", "content": f"Assistant response {i}: " + "massive info payload " * 300})
        history.append({
            "role": "assistant",
            "content": "Calling tool",
            "tool_calls": [{"id": "call_abc", "type": "function", "function": {"name": "read_data", "arguments": "{}"}}]
        })
        history.append({
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "Result payload: " + "data payload " * 300
        })

        # Mocked Provider client staging:
        # Call 1: 400 BadRequestError (context overflow)
        # Call 2: 200 OK ChatCompletion response with usage
        calls = []

        def mock_create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                resp = MagicMock()
                resp.status_code = 400
                resp.headers = {}
                raise openai.BadRequestError(
                    message="prompt is too long: 130000 > 128000 maximum",
                    response=resp,
                    body={"error": {"message": "prompt is too long: 130000 > 128000 maximum", "code": 400}},
                )
            return _mock_response(
                "Successfully analyzed data after compaction recovery.",
                usage={"prompt_tokens": 15000, "completion_tokens": 40, "total_tokens": 15040},
            )

        agent.client.chat.completions.create.side_effect = mock_create

        # Mock summary LLM for compaction
        summary_resp = _mock_response("## Goal\nPreserved historical research.\n## State\nTool completed.")
        with (
            patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(MagicMock(), "aux-model")),
            patch("agent.context_compressor.call_llm", return_value=summary_resp),
        ):
            result = agent.run_conversation("Please synthesize findings", conversation_history=history)

        # Assertions for successful completion and invariants:
        assert result.get("completed") is True, f"Turn failed: {result.get(error)}"
        assert result.get("failed") is False
        assert len(calls) == 2, "Expected exactly 1 overflow call + 1 successful retried call"
        assert "Successfully analyzed data after compaction recovery" in result.get("final_response", "")

        # Assert usage anchor captured authoritative provider usage from the successful call
        assert agent._usage_anchor is not None
        assert agent._usage_anchor["prompt_tokens"] == 15000
        assert agent._usage_anchor["completion_tokens"] == 40
        assert agent._request_pressure_anchored is False  # Was initially unanchored on cold resume

        # Assert persisted transcript in SQLite has no orphan tool pairs
        persisted_msgs = db.get_messages("SESS_E2E_OVERFLOW")
        tool_call_ids = {
            tc["id"]
            for m in persisted_msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        tool_result_ids = {
            m["tool_call_id"]
            for m in persisted_msgs
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        assert tool_call_ids == tool_result_ids, "Persisted messages must not have orphaned tool pairs"
