"""Exercise snapshot retrieval through the real child loop, with inference alone stubbed."""
import copy
import json
from unittest.mock import patch

from openai.types.chat import ChatCompletion

from run_agent import AIAgent
from tools.registry import registry
from tests.tools.test_delegation_context import isolated_hermes_env  # noqa: F401


def test_real_child_loop_reads_own_snapshot_without_recall_db(isolated_hermes_env):
    parent, db = isolated_hermes_env
    parent.enabled_toolsets = ["delegation", "session_search"]
    parent._persist_session([
        {"role": "user", "content": "Snapshot-only evidence: original nonce is READER-LOOP-72."},
        {"role": "assistant", "content": "Noted the historical evidence."},
        {"role": "user", "content": "Retrieve the earlier evidence before reporting."},
    ])
    before = copy.deepcopy(db.get_messages_as_conversation(parent.session_id))
    requests = []
    child_ids = []
    emitted_tool_names = []

    def inference(child, api_kwargs, **kwargs):
        requests.append(copy.deepcopy(api_kwargs))
        child_ids.append(child.session_id)
        if len(requests) == 1:
            arguments = {"session_id": "snapshot", "around_message_id": 1, "window": 0,
                         "max_chars": 1000}
            exposed = {t["function"]["name"] for t in api_kwargs.get("tools", [])}
            if "session_search" in exposed:
                tool_name = "session_search"
            else:
                assert "tool_call" in exposed
                tool_name = "tool_call"
                arguments = {"calls": [{"name": "session_search", "arguments": arguments}]}
            emitted_tool_names.append(tool_name)
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "snapshot-read-call", "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments)},
            }]}
            finish_reason = "tool_calls"
        else:
            outputs = [m for m in api_kwargs["messages"]
                       if m.get("role") == "tool" and m.get("tool_call_id") == "snapshot-read-call"]
            assert len(outputs) == 1
            payload = json.loads(outputs[0]["content"])
            assert payload["success"] is True
            assert payload["snapshot_id"] == child._inherited_context_snapshot.manifest.snapshot_id
            assert "READER-LOOP-72" in payload["messages"][0]["content"]
            assert payload["messages"][0]["id"] == 1
            message = {"role": "assistant", "content": '{"nonce":"READER-LOOP-72"}'}
            finish_reason = "stop"
        return ChatCompletion(
            id=f"synthetic-reader-{len(requests)}", created=0, model="gemini-3.8-flash-high",
            object="chat.completion",
            choices=[{"index": 0, "finish_reason": finish_reason, "message": message}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    with patch("agent.turn_api_call._should_stream", return_value=False), \
            patch.object(AIAgent, "_interruptible_api_call", inference), \
            patch.object(AIAgent, "_get_session_db_for_recall",
                         side_effect=AssertionError("Snapshot route must not acquire a recall database")) as recall:
        result = json.loads(registry.get_entry("delegate_task").handler(
            {"tasks": [{
                "goal": "Call session_search on your own snapshot, then return the original nonce as JSON.",
                "inherit_context": True,
                "output_schema": {"type": "object", "properties": {"nonce": {"type": "string"}},
                                  "required": ["nonce"]},
            }]}, parent_agent=parent,
        ))
    entry = result["results"][0]
    assert entry["status"] == "completed"
    assert entry["schema_valid"] is True
    assert json.loads(entry["summary"])["nonce"] == "READER-LOOP-72"
    assert len(requests) == 2
    assert len(set(child_ids)) == 1 and child_ids[0] != parent.session_id
    assert recall.call_count == 0
    assert len(emitted_tool_names) == 1
    assert [step["tool"] for step in entry["tool_trace"]] == emitted_tool_names
    assert db.get_messages_as_conversation(parent.session_id) == before
