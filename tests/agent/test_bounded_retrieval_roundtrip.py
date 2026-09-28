"""Exercise bounded context inheritance and on-demand retrieval through the real child loop."""

import copy
import json
import uuid
from unittest.mock import patch

from openai.types.chat import ChatCompletion

from run_agent import AIAgent
from tools.registry import registry
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


def test_real_child_loop_bounded_inheritance_retrieves_omitted_marker(isolated_hermes_env):
    """Verify real child conversation loop receives bounded seed without marker,

    retrieves the omitted record via session_search / tool_call bridge on its own snapshot,
    and returns the retrieved nonce according to output_schema.
    """
    parent, db = isolated_hermes_env
    parent.enabled_toolsets = ["delegation", "session_search"]

    marker = f"BOUNDED-NONCE-{uuid.uuid4().hex[:8]}"

    # Turn 1: has the secret marker and enough padding so it cannot fit in a tight seed budget
    # Turn 2: assistant ack
    # Turn 3: latest user prompt
    parent._persist_session([
        {"role": "user", "content": f"Historical secret evidence: the authorization nonce is {marker}. " + ("padding details " * 40)},
        {"role": "assistant", "content": "Recorded the authorization nonce."},
        {"role": "user", "content": "Latest user request: execute the assigned task."},
    ])

    before = copy.deepcopy(db.get_messages_as_conversation(parent.session_id))
    requests = []
    child_ids = []
    emitted_tool_names = []

    def inference(child, api_kwargs, **kwargs):
        requests.append(copy.deepcopy(api_kwargs))
        child_ids.append(child.session_id)

        if len(requests) == 1:
            # Turn 1: Inspect the initial user message.
            # CRITICAL ASSERTION: The marker MUST NOT be present in the initial bounded seed!
            user_messages = [m for m in api_kwargs["messages"] if m.get("role") == "user"]
            assert len(user_messages) >= 1
            initial_user_content = str(user_messages[0]["content"])
            assert marker not in initial_user_content, (
                f"Marker {marker} should have been omitted from the bounded initial seed, "
                f"but was found in user turn: {initial_user_content}"
            )

            # Issue retrieval call to recover the omitted record from snapshot
            arguments = {
                "session_id": "snapshot",
                "around_message_id": 1,
                "window": 0,
                "max_chars": 1000,
            }
            exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
            if "session_search" in exposed:
                tool_name = "session_search"
            else:
                assert "tool_call" in exposed
                tool_name = "tool_call"
                arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}

            emitted_tool_names.append(tool_name)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "bounded-retrieval-call",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                }],
            }
            finish_reason = "tool_calls"
        else:
            # Turn 2: Verify tool output contains the retrieved marker from the snapshot
            outputs = [
                m for m in api_kwargs["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "bounded-retrieval-call"
            ]
            assert len(outputs) == 1
            payload = json.loads(outputs[0]["content"])
            assert payload["success"] is True
            assert payload["snapshot_id"] == child._inherited_context_snapshot.manifest.snapshot_id

            # Verify the record content has the marker
            if "messages" in payload:
                found_text = payload["messages"][0]["content"]
            else:
                found_text = payload["results"][0]["snippet"]
            assert marker in found_text, f"Expected marker {marker} in retrieved output, got {found_text}"

            message = {"role": "assistant", "content": json.dumps({"marker": marker})}
            finish_reason = "stop"

        return ChatCompletion(
            id=f"synthetic-bounded-reader-{len(requests)}",
            created=0,
            model="gemini-3.8-flash-high",
            object="chat.completion",
            choices=[{"index": 0, "finish_reason": finish_reason, "message": message}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    with patch("agent.turn_api_call._should_stream", return_value=False), \
            patch.object(AIAgent, "_interruptible_api_call", inference), \
            patch.object(AIAgent, "_get_session_db_for_recall",
                         side_effect=AssertionError("Snapshot route must not acquire recall DB")) as recall:
        result = json.loads(registry.get_entry("delegate_task").handler(
            {"tasks": [{
                "goal": "Retrieve the authorization nonce from earlier history and return as JSON.",
                "inherit_context": True,
                "inherit_context_mode": "bounded",
                "inherit_max_tokens": 220,
                "output_schema": {
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                    "required": ["marker"],
                },
            }]},
            parent_agent=parent,
        ))

    entry = result["results"][0]
    assert entry["status"] == "completed"
    assert entry["schema_valid"] is True
    summary = json.loads(entry["summary"])
    assert summary["marker"] == marker

    # Receipts verification
    manifest = entry["inherited_context"]
    assert manifest["mode"] == "bounded"
    assert manifest["omitted_records_count"] > 0
    assert 1 not in manifest["selected_record_ids"]  # Record 1 was indeed omitted from the seed!
    assert 3 in manifest["selected_record_ids"]  # Latest user prompt was preserved!
    assert manifest["source_records_count"] == 3

    assert len(requests) == 2
    assert len(set(child_ids)) == 1 and child_ids[0] != parent.session_id
    assert recall.call_count == 0
    assert len(emitted_tool_names) == 1
    assert [step["tool"] for step in entry["tool_trace"]] == emitted_tool_names
    assert db.get_messages_as_conversation(parent.session_id) == before


def test_unavailable_reader_rejects_before_inference_and_cleans_children(isolated_hermes_env):
    parent, _ = isolated_hermes_env
    parent.enabled_toolsets = ["file"]
    parent._session_messages = [{"role": "user", "content": "Keep this request."}]
    with patch.object(AIAgent, "_interruptible_api_call") as inference:
        result = json.loads(registry.get_entry("delegate_task").handler(
            {"tasks": [{"goal": "Inspect historical requirements only",
                        "inherit_context": True, "inherit_context_mode": "bounded"}]},
            parent_agent=parent,
        ))
    assert "session_search is not available" in result["error"]
    inference.assert_not_called()
    assert parent._active_children == []
