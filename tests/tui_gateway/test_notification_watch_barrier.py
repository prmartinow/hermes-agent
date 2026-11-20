"""Completion events must survive while a watch notification owns the turn."""

import threading
import time

from tui_gateway import server
from tests.tui_gateway.test_tui_gateway_server import (
    _RecordingAgent, _configure_immediate_prompt_run, _session,
)


def test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_threading(
    monkeypatch, tmp_path
):
    import queue as _queue_mod

    from tools.process_registry import process_registry

    _configure_immediate_prompt_run(
        monkeypatch, tmp_path, immediate_threads=False
    )
    real_thread_class = threading.Thread
    threads = []
    nested_started = threading.Event()
    release_nested = threading.Event()
    turns = []

    class _RecordingThread(real_thread_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    class _BlockingNotificationAgent(_RecordingAgent):
        def run_conversation(self, prompt, conversation_history=None, stream_callback=None, **_kwargs):
            turns.append(prompt)
            if "proc_batch_1" in prompt:
                nested_started.set()
                if not release_nested.wait(timeout=5):
                    raise TimeoutError("notification turn was not released")
            return {"final_response": "", "messages": []}

    monkeypatch.setattr(server.threading, "Thread", _RecordingThread)
    session = _session(
        session_key="session-a",
        agent=_BlockingNotificationAgent(turns),
        running=True,
    )
    events = [
        {
            "type": "completion",
            "session_id": f"proc_batch_{index}",
            "session_key": "session-a",
            "command": "safe-test-command",
            "exit_code": 0,
            "output": f"owned-{index}",
        }
        for index in range(1, 4)
    ]
    # Consecutive completions share one turn (#104671); a watch_match is a turn
    # barrier, so it is the in-flight turn behind which batch_2/batch_3 must survive.
    events[0].update(type="watch_match", pattern="owned-1")
    isolated_queue: _queue_mod.Queue = _queue_mod.Queue()
    for event in events:
        isolated_queue.put(event)
    monkeypatch.setattr(process_registry, "completion_queue", isolated_queue)
    server._sessions["sid_a"] = session

    try:
        server._run_prompt_submit("rid-a", "sid_a", session, "session-a-turn")

        assert nested_started.wait(timeout=5)
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
        # Membership, not order: the completion_queue is process-global, and
        # notification pollers leaked by earlier session.init tests in this
        # file legitimately steal-and-requeue foreign-session events (see
        # _notification_poller_loop's belongs-elsewhere branch), rotating the
        # queue. The requeue contract is that batch_2 and batch_3 both remain
        # queued (never consumed) while batch_1's turn is in flight — so drain
        # with a deadline (an event may be transiently held by a poller
        # mid-cycle) and assert exactly {batch_2, batch_3} come back.
        queued: dict = {}
        deadline = time.time() + 5.0
        while time.time() < deadline and set(queued) != {
            "proc_batch_2",
            "proc_batch_3",
        }:
            try:
                evt = isolated_queue.get(timeout=0.1)
            except _queue_mod.Empty:
                continue
            queued[evt["session_id"]] = evt
        assert set(queued) == {"proc_batch_2", "proc_batch_3"}
    finally:
        release_nested.set()
        for thread in threads:
            thread.join(timeout=5)
        server._sessions.pop("sid_a", None)
        while not isolated_queue.empty():
            isolated_queue.get_nowait()
        for event in events:
            process_registry._completion_consumed.discard(event["session_id"])
            process_registry._poll_observed.discard(event["session_id"])
