"""Contracts for live session event persistence."""

import json
from types import SimpleNamespace

from agent.live_session_stream import LiveSessionStreamWriter, read_live_session_events


def test_turn_start_failure_does_not_interrupt_agent(caplog):
    from agent.live_session_stream import publish_turn_start

    class BrokenWriter:
        def turn_start(self, _message):
            raise OSError("synthetic stream failure")

    publish_turn_start(SimpleNamespace(_live_stream_writer=BrokenWriter()), "hello")
    assert "Could not publish the live session start" in caplog.text


def test_tool_events_remain_visible_to_a_later_viewer(tmp_path):
    from agent.live_session_stream import publish_tool_complete, publish_tool_start

    writer = LiveSessionStreamWriter("test-session", tmp_path)
    agent = SimpleNamespace(_live_stream_writer=writer)
    ref = SimpleNamespace(call_id="tool-1", name="terminal", args={"command": "echo hello"})
    publish_tool_start(agent, ref.call_id, ref.name, ref.args)
    publish_tool_complete(agent, ref, "hello", blocked=False)
    publish_tool_complete(agent, ref, "blocked", blocked=True)

    events, _ = read_live_session_events("test-session", tmp_path)
    assert [event["type"] for event in events] == ["tool.start", "tool.complete"]
    assert [event["payload"]["tool_id"] for event in events] == ["tool-1", "tool-1"]


def test_live_writer_requests_streaming_without_a_viewer(tmp_path, monkeypatch):
    from agent.stream_delivery import StreamDeliveryMixin

    monkeypatch.setattr("agent.plugin_stream_hooks.has_stream_observer_hooks", lambda: False)
    writer = LiveSessionStreamWriter("test-session", tmp_path)
    agent = SimpleNamespace(stream_delta_callback=None, _stream_callback=None, _live_stream_writer=writer)
    assert StreamDeliveryMixin._has_stream_consumers(agent)
    writer._disabled = True
    assert not StreamDeliveryMixin._has_stream_consumers(agent)


def test_live_stream_offsets_advance_and_new_events_are_read_once(tmp_path):
    writer = LiveSessionStreamWriter("test-session", tmp_path)
    writer.write_event("message.delta", {"text": "first"})

    first, offset = read_live_session_events("test-session", tmp_path)
    assert [event["payload"]["text"] for event in first] == ["first"]
    assert offset > 0
    assert read_live_session_events("test-session", tmp_path, offset) == ([], offset)

    writer.write_event("message.delta", {"text": "second"})
    second, next_offset = read_live_session_events("test-session", tmp_path, offset)
    assert [event["payload"]["text"] for event in second] == ["second"]
    assert next_offset > offset


def test_redaction_error_does_not_persist_original_text(tmp_path, monkeypatch):
    def fail_redaction(*args, **kwargs):
        raise ValueError("synthetic redaction error")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", fail_redaction)
    writer = LiveSessionStreamWriter("test-session", tmp_path)
    writer.write_event("message.delta", {"text": "private synthetic text"})

    content = writer.path.read_text(encoding="utf-8-sig")
    assert "private synthetic text" not in content
    assert json.loads(content)["payload"]["text"] == "[redaction unavailable]"


def test_watcher_starts_with_callers_context_and_replays_once(tmp_path):
    import contextvars
    import threading
    from agent.live_session_stream import LiveSessionStreamWatcher

    scope = contextvars.ContextVar("scope", default="missing")
    scope.set("profile-a")
    writer = LiveSessionStreamWriter("test-session", tmp_path)
    writer.write_event("turn.start", {})
    writer.write_event("message.delta", {"text": "hello"})
    writer.write_event("message.complete", {})
    finished = threading.Event()
    seen = []
    watcher = LiveSessionStreamWatcher(
        "test-session", "client", lambda event, sid, payload: seen.append((event, scope.get())),
        profile_home=tmp_path, on_idle=finished.set,
    )

    watcher.start()
    assert finished.wait(timeout=2)
    watcher.stop()
    assert seen == [("message.delta", "profile-a"), ("message.complete", "profile-a")]


def test_watcher_replays_latest_turn_and_tails_new_events_once(tmp_path):
    from agent.live_session_stream import LiveSessionStreamWatcher

    writer = LiveSessionStreamWriter("test-session", tmp_path)
    for event, payload in [("turn.start", {}), ("message.delta", {"text": "old"}),
                           ("turn.start", {}), ("message.delta", {"text": "current"})]:
        writer.write_event(event, payload)
    seen = []
    watcher = LiveSessionStreamWatcher("test-session", "client", lambda event, sid, payload: seen.append(payload),
                                      profile_home=tmp_path)
    offset, pid, complete = watcher._replay()
    assert not complete
    assert [payload["text"] for payload in seen] == ["current"]
    writer.write_event("message.delta", {"text": "new"})
    next_offset, pid, activity, complete = watcher._tail(offset, pid)
    assert activity and not complete and next_offset > offset
    assert [payload["text"] for payload in seen] == ["current", "new"]
    final_offset, _, activity, complete = watcher._tail(next_offset, pid)
    assert final_offset == next_offset and not activity and not complete
