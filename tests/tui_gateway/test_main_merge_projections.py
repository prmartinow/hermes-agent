"""Extracted discovery and snapshot projections retain main's wire contracts."""
import threading
from contextlib import nullcontext

def test_live_message_count_tracks_serialized_projection(monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(server, '_session_db', lambda session: nullcontext(None))
    monkeypatch.setattr(server, '_fallback_session_info', lambda session: {})
    monkeypatch.setattr(server, '_pending_approval_request_payload', lambda key: None)
    monkeypatch.setattr(server, '_open_requests', lambda sid: [])
    # A display row may expand or disappear in the serializer. The count is the wire size.
    monkeypatch.setattr(server, '_history_to_messages', lambda rows, **_: [{'text': 'visible'}])
    session = {'history_lock': threading.Lock(), 'history': [{}, {}], 'session_key': 'owned'}
    payload = server._live_session_payload('owned', session)
    assert payload['message_count'] == len(payload['messages']) == 1
    omitted = server._live_session_payload('owned', session, omit_messages=True)
    assert omitted['messages'] == [] and omitted['message_count'] == 2


def test_history_to_messages_hydrates_stored_json_tool_calls(tmp_path):
    import json
    from tui_gateway import server

    history = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": json.dumps([
                {
                    "id": "call_sqlite_1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "src/main.ts"}),
                    },
                }
            ]),
        },
        {"role": "tool", "content": "file text", "tool_call_id": "call_sqlite_1", "tool_name": "read_file"},
    ]
    from tests.fixtures.storage_read import conversation
    from tests.fixtures.storage_seed import seed_messages, seed_session
    from tests.gateway.fixtures.storage_handles import storage_handle

    db = storage_handle(tmp_path)
    try:
        seed_session(db, "stored-tool-calls", "tui")
        history[1]["tool_calls"] = json.loads(history[1]["tool_calls"])
        seed_messages(db, "stored-tool-calls", *history)
        replayed = conversation(db, "stored-tool-calls")
        assert replayed[1]["tool_calls"] == history[1]["tool_calls"]
        rows = server._history_to_messages(replayed)
    finally:
        db.close()
    assert rows[1]["args"] == {"path": "src/main.ts"}
    assert rows[1]["context"] == "main.ts"
