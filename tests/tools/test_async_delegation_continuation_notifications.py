"""Tests for async delegation notification continuation metadata formatting,
producer-to-formatter forwarding, and receipt-to-follow-up integration.
"""

import json
import queue
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion

from run_agent import AIAgent
from tools import async_delegation as ad
from tools.process_registry_notifications import (
    format_continuation_metadata,
    format_process_notification,
)
from tools.registry import registry
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


# ── 1. Unit Tests: Formatter Function ──────────────────────────────────────────


class TestContinuationMetadataFormatter:
    """Direct tests for format_continuation_metadata."""

    def test_valid_child_session_id_and_continuation_true(self):
        entry = {"child_session_id": "child-sess-001", "continuation_available": True}
        lines = format_continuation_metadata(entry)
        assert lines == [
            "Child session ID: child-sess-001",
            "Continuation available: true",
        ]

    def test_valid_child_session_id_and_continuation_false(self):
        entry = {"child_session_id": "child-sess-002", "continuation_available": False}
        lines = format_continuation_metadata(entry)
        assert lines == [
            "Child session ID: child-sess-002",
            "Continuation available: false",
        ]

    def test_omits_absent_continuation_available_for_legacy(self):
        entry = {"child_session_id": "child-sess-legacy"}
        lines = format_continuation_metadata(entry)
        assert lines == ["Child session ID: child-sess-legacy"]

    def test_omits_absent_child_session_id_when_continuation_present(self):
        entry = {"continuation_available": False}
        lines = format_continuation_metadata(entry)
        assert lines == ["Continuation available: false"]

    def test_omits_both_for_empty_legacy_entry(self):
        assert format_continuation_metadata({}) == []
        assert format_continuation_metadata(None) == []

    def test_malformed_child_session_id_omitted_without_repair(self):
        # Leading/trailing whitespace must be omitted without stripping/repairing
        assert format_continuation_metadata({"child_session_id": "  sess-with-spaces  "}) == []
        assert format_continuation_metadata({"child_session_id": "\tsess-tab\n"}) == []
        # Internal whitespace/newlines
        assert format_continuation_metadata({"child_session_id": "sess with space"}) == []
        assert format_continuation_metadata({"child_session_id": "sess\nnewline"}) == []
        # Non-string types
        assert format_continuation_metadata({"child_session_id": 12345}) == []
        assert format_continuation_metadata({"child_session_id": True}) == []
        assert format_continuation_metadata({"child_session_id": False}) == []
        assert format_continuation_metadata({"child_session_id": ["sess"]}) == []
        assert format_continuation_metadata({"child_session_id": {"id": "sess"}}) == []
        assert format_continuation_metadata({"child_session_id": ""}) == []

    def test_malformed_continuation_available_omitted(self):
        # Non-boolean values must never be coerced or treated as booleans
        assert format_continuation_metadata({"continuation_available": "true"}) == []
        assert format_continuation_metadata({"continuation_available": "false"}) == []
        assert format_continuation_metadata({"continuation_available": 1}) == []
        assert format_continuation_metadata({"continuation_available": 0}) == []
        assert format_continuation_metadata({"continuation_available": None}) == []
        assert format_continuation_metadata({"continuation_available": []}) == []

    def test_never_infers_availability_from_status_or_id(self):
        # Status 'completed' with child_session_id but missing continuation_available
        entry_completed = {"status": "completed", "child_session_id": "sess-xyz"}
        assert format_continuation_metadata(entry_completed) == ["Child session ID: sess-xyz"]

        # Status 'completed' with explicit continuation_available: False must render false
        entry_false = {"status": "completed", "child_session_id": "sess-xyz", "continuation_available": False}
        assert format_continuation_metadata(entry_false) == [
            "Child session ID: sess-xyz",
            "Continuation available: false",
        ]


# ── 2. Batch Delegation Notifications ──────────────────────────────────────────


class TestBatchDelegationNotificationFormatting:
    """Formatting of batch delegation completion events."""

    def test_batch_formatting_with_continuation_metadata_ahead_of_summary(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "batch-100",
            "is_batch": True,
            "goals": ["First task", "Second task"],
            "dispatched_at": 100.0,
            "completed_at": 110.0,
            "role": "leaf",
            "model": "test-model",
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "Summary of task 1 which could be very long indeed.",
                    "child_session_id": "child-sess-0",
                    "continuation_available": True,
                    "duration_seconds": 5.0,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "Summary of task 2.",
                    "child_session_id": "child-sess-1",
                    "continuation_available": False,
                    "duration_seconds": 4.5,
                },
            ],
        }
        text = format_process_notification(evt)
        assert text is not None

        # Verify task 0 has metadata ahead of summary
        sid_0_pos = text.index("Child session ID: child-sess-0")
        avail_0_pos = text.index("Continuation available: true")
        summary_0_pos = text.index("Summary of task 1")
        assert sid_0_pos < summary_0_pos
        assert avail_0_pos < summary_0_pos

        # Verify task 1 has metadata ahead of summary
        sid_1_pos = text.index("Child session ID: child-sess-1")
        avail_1_pos = text.index("Continuation available: false")
        summary_1_pos = text.index("Summary of task 2")
        assert sid_1_pos < summary_1_pos
        assert avail_1_pos < summary_1_pos

    def test_batch_formatting_mixed_children_no_cross_assignment(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "batch-mixed-200",
            "is_batch": True,
            "goals": ["Task A", "Task B", "Task C"],
            "dispatched_at": 100.0,
            "completed_at": 110.0,
            "role": "leaf",
            "model": "test-model",
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "Result A",
                    "child_session_id": "child-a-001",
                    "continuation_available": True,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "Result B (legacy without continuation fields)",
                },
                {
                    "task_index": 2,
                    "status": "failed",
                    "summary": "Result C",
                    "error": "syntax error",
                    "child_session_id": "child-c-003",
                    "continuation_available": False,
                },
            ],
        }
        text = format_process_notification(evt)

        # Split into task blocks to verify strict per-task isolation
        task_a_block = text[text.index("TASK 1/3"):text.index("TASK 2/3")]
        task_b_block = text[text.index("TASK 2/3"):text.index("TASK 3/3")]
        task_c_block = text[text.index("TASK 3/3"):]

        # Task A has child-a-001 and Continuation: true
        assert "Child session ID: child-a-001" in task_a_block
        assert "Continuation available: true" in task_a_block
        assert "child-c-003" not in task_a_block

        # Task B is legacy: must NOT inherit metadata from task A or C
        assert "Child session ID" not in task_b_block
        assert "Continuation available" not in task_b_block

        # Task C has child-c-003 and Continuation: false
        assert "Child session ID: child-c-003" in task_c_block
        assert "Continuation available: false" in task_c_block
        assert "child-a-001" not in task_c_block

    def test_batch_formatting_truncated_task_metadata_ahead_of_warning(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "batch-trunc-300",
            "is_batch": True,
            "goals": ["Exhausted task"],
            "dispatched_at": 100.0,
            "completed_at": 110.0,
            "role": "leaf",
            "model": "test-model",
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "exit_reason": "max_iterations",
                    "truncated": True,
                    "summary": "Incomplete work output.",
                    "child_session_id": "child-trunc-001",
                    "continuation_available": True,
                },
            ],
        }
        text = format_process_notification(evt)
        meta_pos = text.index("Child session ID: child-trunc-001")
        trunc_warn_pos = text.index("[TRUNCATED — subagent hit its iteration cap")
        summary_pos = text.index("Incomplete work output.")
        assert meta_pos < trunc_warn_pos < summary_pos

    def test_batch_formatting_malformed_id_safely_handled_without_repair(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "batch-malformed-400",
            "is_batch": True,
            "goals": ["Malformed metadata task"],
            "dispatched_at": 100.0,
            "completed_at": 110.0,
            "role": "leaf",
            "model": "test-model",
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "Done.",
                    "child_session_id": "   unrepaired-id-with-spaces   ",
                    "continuation_available": True,
                },
            ],
        }
        text = format_process_notification(evt)
        assert "Child session ID" not in text
        assert "unrepaired-id-with-spaces" not in text
        # Valid continuation boolean is still rendered
        assert "Continuation available: true" in text


# ── 3. Single Delegation Notifications ─────────────────────────────────────────


class TestSingleDelegationNotificationFormatting:
    """Formatting of single delegation completion events."""

    def test_single_formatting_with_continuation_true(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "single-100",
            "goal": "Single task goal",
            "dispatched_at": 100.0,
            "completed_at": 105.0,
            "role": "leaf",
            "model": "test-model",
            "status": "completed",
            "summary": "This is the single child summary.",
            "child_session_id": "single-child-001",
            "continuation_available": True,
        }
        text = format_process_notification(evt)
        assert text is not None
        assert "Child session ID: single-child-001" in text
        assert "Continuation available: true" in text

        sid_pos = text.index("Child session ID: single-child-001")
        result_pos = text.index("--- RESULT ---")
        summary_pos = text.index("This is the single child summary.")
        assert sid_pos < result_pos < summary_pos

    def test_single_formatting_with_continuation_false(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "single-200",
            "goal": "Single task goal",
            "dispatched_at": 100.0,
            "completed_at": 105.0,
            "role": "leaf",
            "model": "test-model",
            "status": "completed",
            "summary": "Summary here.",
            "child_session_id": "single-child-002",
            "continuation_available": False,
        }
        text = format_process_notification(evt)
        assert "Child session ID: single-child-002" in text
        assert "Continuation available: false" in text

    def test_single_formatting_legacy_omits_metadata(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "single-legacy-300",
            "goal": "Single task goal",
            "dispatched_at": 100.0,
            "completed_at": 105.0,
            "role": "leaf",
            "model": "test-model",
            "status": "completed",
            "summary": "Legacy summary.",
        }
        text = format_process_notification(evt)
        assert "Child session ID" not in text
        assert "Continuation available" not in text


# ── 4. Task Failure Notices ────────────────────────────────────────────────────


class TestTaskFailureNoticeFormatting:
    """Formatting of async delegation early failure notices."""

    def test_failure_notice_renders_continuation_metadata_from_result_entry(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "fail-deleg-01",
            "task_failure_notice": True,
            "is_batch": True,
            "n_tasks": 3,
            "goals": ["Goal 1", "Goal 2", "Goal 3"],
            "results": [
                {
                    "task_index": 1,
                    "status": "error",
                    "error": "Critical worker failure",
                    "duration_seconds": 12.0,
                    "child_session_id": "failed-child-sess-002",
                    "continuation_available": False,
                    "live_transcript": "/tmp/live/task-1.log",
                }
            ],
        }
        text = format_process_notification(evt)
        assert text.startswith("[ASYNC DELEGATION TASK FAILED — fail-deleg-01, task 2/3]")
        assert "Child session ID: failed-child-sess-002" in text
        assert "Continuation available: false" in text

        # Verify metadata is positioned before live transcript
        meta_pos = text.index("Child session ID: failed-child-sess-002")
        transcript_pos = text.index("Live transcript: /tmp/live/task-1.log")
        assert meta_pos < transcript_pos

    def test_failure_notice_legacy_omits_metadata(self):
        evt = {
            "type": "async_delegation",
            "delegation_id": "fail-legacy-02",
            "task_failure_notice": True,
            "is_batch": True,
            "n_tasks": 2,
            "goals": ["Goal 1", "Goal 2"],
            "results": [
                {
                    "task_index": 0,
                    "status": "error",
                    "error": "Boom",
                }
            ],
        }
        text = format_process_notification(evt)
        assert "Child session ID" not in text
        assert "Continuation available" not in text


# ── 5. Producer -> Formatter Pipeline ──────────────────────────────────────────


class TestProducerToFormatterPipeline:
    """Verify tools/async_delegation.py::_push_completion_event forwards metadata
    from child results to the published event, and formatter consumes it.
    """

    def test_single_producer_forwards_metadata_to_completion_event_and_formatter(self):
        q = queue.Queue()
        record = {
            "delegation_id": "deleg-prod-single-1",
            "session_key": "sk-test",
            "is_batch": False,
            "goal": "Single task goal",
            "role": "leaf",
            "model": "model-x",
            "dispatched_at": 100.0,
            "completed_at": 102.5,
        }
        result = {
            "summary": "Completed successfully.",
            "status": "completed",
            "api_calls": 3,
            "duration_seconds": 2.5,
            "child_session_id": "child-producer-001",
            "continuation_available": True,
        }

        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.async_delegation._persist_completion"):
            mock_reg.completion_queue = q
            ad._push_completion_event(record, result, "completed")

        evt = q.get_nowait()
        assert evt["child_session_id"] == "child-producer-001"
        assert evt["continuation_available"] is True

        text = format_process_notification(evt)
        assert "Child session ID: child-producer-001" in text
        assert "Continuation available: true" in text
        assert text.index("Child session ID: child-producer-001") < text.index("--- RESULT ---")

    def test_single_producer_legacy_result_omits_metadata(self):
        q = queue.Queue()
        record = {
            "delegation_id": "deleg-prod-single-legacy",
            "is_batch": False,
            "goal": "Legacy goal",
            "role": "leaf",
            "model": "model-x",
            "dispatched_at": 100.0,
        }
        result = {
            "summary": "Legacy completed.",
            "status": "completed",
            "api_calls": 1,
        }

        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.async_delegation._persist_completion"):
            mock_reg.completion_queue = q
            ad._push_completion_event(record, result, "completed")

        evt = q.get_nowait()
        assert "child_session_id" not in evt
        assert "continuation_available" not in evt

        text = format_process_notification(evt)
        assert "Child session ID" not in text
        assert "Continuation available" not in text

    def test_batch_producer_preserves_results_and_formatter(self):
        q = queue.Queue()
        record = {
            "delegation_id": "deleg-prod-batch-1",
            "is_batch": True,
            "goals": ["Task A", "Task B"],
            "role": "leaf",
            "model": "model-x",
            "dispatched_at": 100.0,
            "completed_at": 105.0,
        }
        result = {
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "Done A",
                    "child_session_id": "child-b-0",
                    "continuation_available": True,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "Done B",
                    "child_session_id": "child-b-1",
                    "continuation_available": False,
                },
            ],
            "total_duration_seconds": 5.0,
        }

        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.async_delegation._persist_completion"):
            mock_reg.completion_queue = q
            ad._push_completion_event(record, result, "completed")

        evt = q.get_nowait()
        assert len(evt["results"]) == 2
        text = format_process_notification(evt)
        assert "Child session ID: child-b-0" in text
        assert "Continuation available: true" in text
        assert "Child session ID: child-b-1" in text
        assert "Continuation available: false" in text


# ── 6. Integration: Real Persisted Receipt -> Async Notification -> Follow-up ──


class TestRealReceiptToAsyncFollowUpIntegration:
    """Real two-child conversation loop taking a real persisted child receipt,
    formatting async completion, extracting forwarded exact ID, and submitting
    follow-up via native delegate path.
    """

    def test_real_persisted_child_receipt_to_async_notification_to_follow_up(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        parent.valid_tool_names.append("session_search")
        parent.enabled_toolsets.append("session_search")
        marker = "MARKER-ASYNC-FOLLOWUP-7723"

        # Parent initial conversation
        parent._session_messages = [
            {"role": "user", "content": "Initial prompt: discover subsystem secrets."},
            {"role": "assistant", "content": "Starting search."},
        ]

        # Step 1: Child 1 runs via native delegate_task and discovers the marker
        def inference_child_1(*args, **kwargs):
            return ChatCompletion(
                id="child-1-async-comp",
                created=0,
                model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"Investigated subsystem and found secret token: {marker}.",
                    },
                }],
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_1):
            res_1_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{"goal": "Find secret token in subsystem."}]},
                parent_agent=parent,
            )
            res_1 = json.loads(res_1_raw)

        entry_1 = res_1["results"][0]
        assert entry_1["status"] == "completed"
        assert entry_1["continuation_available"] is True
        child_1_sid = entry_1["child_session_id"]
        assert isinstance(child_1_sid, str) and child_1_sid

        # Step 2: Push completion event through async producer into completion queue
        q = queue.Queue()
        record_1 = {
            "delegation_id": "deleg-async-flow-1",
            "session_key": "sk-flow",
            "is_batch": False,
            "goal": "Find secret token in subsystem.",
            "role": "leaf",
            "model": "gemini-3.8-flash-high",
            "dispatched_at": 100.0,
            "completed_at": 102.0,
        }

        with patch("tools.process_registry.process_registry") as mock_reg, \
             patch("tools.async_delegation._persist_completion"):
            mock_reg.completion_queue = q
            ad._push_completion_event(record_1, entry_1, "completed")

        async_evt = q.get_nowait()
        assert async_evt["child_session_id"] == child_1_sid
        assert async_evt["continuation_available"] is True

        # Step 3: Format async completion notice as human-readable model re-injection
        formatted_notice = format_process_notification(async_evt)
        assert formatted_notice is not None

        # Verify metadata is rendered ahead of summary
        expected_sid_line = f"Child session ID: {child_1_sid}"
        expected_avail_line = "Continuation available: true"
        assert expected_sid_line in formatted_notice
        assert expected_avail_line in formatted_notice
        assert formatted_notice.index(expected_sid_line) < formatted_notice.index("--- RESULT ---")

        # Step 4: Extract forwarded exact ID from formatted notification text
        match = re.search(r"Child session ID:\s*(\S+)", formatted_notice)
        assert match is not None, f"Could not find Child session ID in:\n{formatted_notice}"
        extracted_child_sid = match.group(1)
        assert extracted_child_sid == child_1_sid

        # Verify explicit continuation boolean was also extracted correctly
        match_avail = re.search(r"Continuation available:\s*(\S+)", formatted_notice)
        assert match_avail is not None
        assert match_avail.group(1) == "true"

        # Step 5: Parent adds correction turn based on notification
        parent._session_messages.append({
            "role": "user",
            "content": f"Subagent {extracted_child_sid} completed. Follow up: verify the token.",
        })

        # Step 6: Child 2 runs with continue_from=extracted_child_sid
        requests_child_2 = []

        def inference_child_2(agent_self, *args, **kwargs):
            requests_child_2.append((args, kwargs))
            api_kwargs = kwargs.get("api_kwargs") or (args[0] if args and isinstance(args[0], dict) else {})
            msgs = api_kwargs.get("messages") or kwargs.get("messages") or (
                args[0] if args and isinstance(args[0], list) else (
                    args[1] if len(args) > 1 and isinstance(args[1], list) else []
                )
            )

            # Turn 1: Child 2 searches snapshot for marker
            if len(requests_child_2) == 1:
                arguments = {"session_id": "snapshot", "query": marker}
                exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
                tool_name = "session_search" if "session_search" in exposed else "tool_call"
                if tool_name == "tool_call":
                    arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}

                return ChatCompletion(
                    id="child-2-search",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "search-snap-call-1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }],
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )
            else:
                # Turn 2: Verify tool result contains the marker from Child 1 evidence
                tool_msg = next(
                    (m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "search-snap-call-1"),
                    None,
                )
                assert tool_msg is not None
                tool_data = json.loads(tool_msg["content"])
                assert tool_data["success"] is True
                assert marker in str(tool_data)

                # Return final verification
                return ChatCompletion(
                    id="child-2-done",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"verified_token": marker}),
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )

        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_2):
            res_2_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{
                    "goal": "Verify the token discovered by prior worker.",
                    "inherit_context": True,
                    "continue_from": extracted_child_sid,
                    "inherit_context_mode": "bounded",
                    "inherit_max_tokens": 400,
                    "output_schema": {
                        "type": "object",
                        "properties": {"verified_token": {"type": "string"}},
                        "required": ["verified_token"],
                    },
                }]},
                parent_agent=parent,
            )
            res_2 = json.loads(res_2_raw)

        entry_2 = res_2["results"][0]
        assert entry_2["status"] == "completed"
        assert entry_2["schema_valid"] is True
        summary_2 = json.loads(entry_2["summary"])
        assert summary_2["verified_token"] == marker

        # Verify Child 2 receipts prove it continued from the exact forwarded child ID
        manifest_2 = entry_2["inherited_context"]
        assert manifest_2["prior_worker_session_id"] == extracted_child_sid
        assert manifest_2["prior_worker_status"] == "completed"

    def test_actual_async_completion_event_collected_from_background_dispatch_chain(self, isolated_hermes_env):
        parent, db = isolated_hermes_env
        parent.valid_tool_names.append("session_search")
        parent.enabled_toolsets.append("session_search")
        parent._delegate_depth = 0
        parent._interrupt_requested = False
        parent._active_children = []
        parent._active_children_lock = None
        marker = "MARKER-CHAIN-ASYNC-9912"

        parent._session_messages = [
            {"role": "user", "content": "Initial prompt: discover subsystem secrets in background."},
            {"role": "assistant", "content": "Starting search."},
        ]

        def inference_child_1(*args, **kwargs):
            return ChatCompletion(
                id="child-1-bg-comp",
                created=0,
                model="gemini-3.8-flash-high",
                object="chat.completion",
                choices=[{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": f"Investigated subsystem and found secret token: {marker}.",
                    },
                }],
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            )

        q = queue.Queue()
        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_1), \
                patch("tools.process_registry.process_registry") as mock_reg, \
                patch("tools.async_delegation._persist_completion"):
            mock_reg.completion_queue = q
            res_handle_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{"goal": "Find secret token in background."}], "background": True},
                parent_agent=parent,
            )
            res_handle = json.loads(res_handle_raw)
            assert res_handle.get("status") == "dispatched"
            # Collect actual completion event published by _push_completion_event at end of background execution
            evt = q.get(timeout=10.0)

        assert evt["type"] == "async_delegation"
        assert evt["is_batch"] is True
        entry_1 = evt["results"][0]
        assert entry_1["status"] == "completed"
        assert entry_1["continuation_available"] is True
        child_1_sid = entry_1["child_session_id"]
        assert isinstance(child_1_sid, str) and child_1_sid

        # Format notice and verify metadata
        formatted_notice = format_process_notification(evt)
        assert f"Child session ID: {child_1_sid}" in formatted_notice
        assert "Continuation available: true" in formatted_notice

        # Extract ID
        match = re.search(r"Child session ID:\s*(\S+)", formatted_notice)
        assert match is not None
        extracted_child_sid = match.group(1)
        assert extracted_child_sid == child_1_sid

        # Step 2: Parent continues with follow-up Child 2
        parent._session_messages.append({
            "role": "user",
            "content": f"Subagent {extracted_child_sid} completed. Follow up: verify the token.",
        })

        requests_child_2 = []

        def inference_child_2(agent_self, *args, **kwargs):
            requests_child_2.append((args, kwargs))
            api_kwargs = kwargs.get("api_kwargs") or (args[0] if args and isinstance(args[0], dict) else {})
            msgs = api_kwargs.get("messages") or kwargs.get("messages") or (
                args[0] if args and isinstance(args[0], list) else (
                    args[1] if len(args) > 1 and isinstance(args[1], list) else []
                )
            )

            if len(requests_child_2) == 1:
                arguments = {"session_id": "snapshot", "query": marker}
                exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
                tool_name = "session_search" if "session_search" in exposed else "tool_call"
                if tool_name == "tool_call":
                    arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}

                return ChatCompletion(
                    id="child-2-bg-search",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "search-snap-call-2",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }],
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )
            else:
                tool_msg = next(
                    (m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "search-snap-call-2"),
                    None,
                )
                assert tool_msg is not None
                tool_data = json.loads(tool_msg["content"])
                assert tool_data["success"] is True
                assert marker in str(tool_data)

                return ChatCompletion(
                    id="child-2-bg-done",
                    created=0,
                    model="gemini-3.8-flash-high",
                    object="chat.completion",
                    choices=[{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"verified_token": marker}),
                        },
                    }],
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                )

        # Reset parent._delegate_depth to 1 so follow-up child 2 runs synchronously
        parent._delegate_depth = 1
        with patch("agent.turn_api_call._should_stream", return_value=False), \
                patch.object(AIAgent, "_interruptible_api_call", inference_child_2):
            res_2_raw = registry.get_entry("delegate_task").handler(
                {"tasks": [{
                    "goal": "Verify the token discovered by background worker.",
                    "inherit_context": True,
                    "continue_from": extracted_child_sid,
                    "inherit_context_mode": "bounded",
                    "inherit_max_tokens": 400,
                    "output_schema": {
                        "type": "object",
                        "properties": {"verified_token": {"type": "string"}},
                        "required": ["verified_token"],
                    },
                }]},
                parent_agent=parent,
            )
            res_2 = json.loads(res_2_raw)

        entry_2 = res_2["results"][0]
        assert entry_2["status"] == "completed"
        assert entry_2["schema_valid"] is True
        summary_2 = json.loads(entry_2["summary"])
        assert summary_2["verified_token"] == marker
        assert entry_2["inherited_context"]["prior_worker_session_id"] == extracted_child_sid
