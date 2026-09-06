"""Todo snapshots bypass optional tool-progress display settings."""

import json

import tui_gateway.server as server


def test_todo_completion_always_emits_snapshot_and_compat_event(monkeypatch):
    sid = "todo-state-test"
    events = []
    session = {
        "agent": None,
        "edit_snapshots": {},
        "tool_started_at": {},
        "tool_progress_mode": "off",
    }
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, event_sid, payload=None: events.append(
            (event, event_sid, payload)
        ),
    )

    state = {
        "todos": [{"id": "1", "content": "Work", "status": "in_progress"}],
        "revision": 9,
    }
    server._on_tool_complete(sid, "call-1", "todo", {}, json.dumps(state))

    assert [event[0] for event in events] == ["tool.complete", "todo.updated"]
    assert events[-1] == ("todo.updated", sid, state)
    assert session["todo_state"] == state


def test_non_todo_completion_stays_suppressed_when_progress_is_off(monkeypatch):
    sid = "ordinary-tool-test"
    events = []
    monkeypatch.setitem(
        server._sessions,
        sid,
        {
            "agent": None,
            "edit_snapshots": {},
            "tool_started_at": {},
            "tool_progress_mode": "off",
        },
    )
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    server._on_tool_complete(sid, "call-1", "terminal", {}, "ok")

    assert events == []


def test_live_snapshot_prefers_the_highest_revision():
    class Store:
        @staticmethod
        def snapshot():
            return {"todos": [], "revision": 4}

    class Agent:
        _todo_store = Store()

    session = {
        "agent": Agent(),
        "todo_state": {
            "todos": [{"id": "1", "content": "Current", "status": "pending"}],
            "revision": 5,
        },
    }

    payload = server._attach_todo_state({}, session)

    assert payload["todo_state"]["revision"] == 5


def test_unused_store_is_not_attached():
    class Store:
        @staticmethod
        def snapshot():
            return {"todos": [], "revision": 0}

    class Agent:
        _todo_store = Store()

    payload = server._attach_todo_state({}, {"agent": Agent()})

    assert "todo_state" not in payload


def test_empty_list_at_nonzero_revision_is_a_real_clear():
    state = server._normalize_todo_state({"todos": [], "revision": 2})

    assert state == {"todos": [], "revision": 2}

def test_history_to_messages_attaches_structured_todos_for_todo_tool():
    todos = [
        {"id": "1", "content": "Step 1", "status": "completed"},
        {"id": "2", "content": "Step 2", "status": "in_progress"},
    ]
    history = [
        {"role": "user", "content": "solve task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "todo_list",
                        "arguments": json.dumps({"todos": todos}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps({"todos": todos, "revision": 1}),
        },
        {"role": "assistant", "content": "Working on step 2."},
    ]

    messages = server._history_to_messages(history)
    assert len(messages) == 3
    assert messages[0] == {"role": "user", "text": "solve task"}
    assert messages[1]["role"] == "tool"
    assert messages[1]["name"] == "todo_list"
    assert messages[1]["todos"] == todos
    assert messages[2] == {"role": "assistant", "text": "Working on step 2."}

