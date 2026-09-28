"""Regression and continuity tests for detached and background turns.

Verifies the user contract:
- Actively running agents must NEVER be interrupted by tab focus / disconnect / autocleanup.
- Real running threads make progress and complete while clientless.
- close_on_disconnect only tears down immediately when actually idle (protects running,
  queued, agent build, async delegation, and pending tool/human input).
- Reattach cancels pending orphan timers.
- Sessions are cleanly reaped once work finishes and session becomes idle.
"""

import threading

import pytest

from tui_gateway import server


class _LiveTransport:
    def __init__(self):
        self._closed = False
        self.sent = []

    def write(self, msg, *_args, **_kwargs):
        self.sent.append(msg)
        return True

    def send(self, msg, *_args, **_kwargs):
        self.sent.append(msg)
        return True


def _session(sid, **extra):
    s = dict(
        agent=None,
        session_key=sid,
        _sid=sid,
        history=[],
        history_lock=threading.Lock(),
        history_version=0,
        running=False,
        transport=server._detached_ws_transport,
        attached_images=[],
        image_counter=0,
        cols=80,
        source="desktop",
    )
    s.update(extra)
    return s


def test_real_running_thread_completes_while_clientless(monkeypatch):
    """A real background thread makes progress and completes while disconnected;
    orphan reap timer defers while thread is running, and reaps only when idle."""
    sid = "real-thread-session"
    thread_started = threading.Event()
    release_thread = threading.Event()
    thread_completed = threading.Event()
    timers = []

    class _Timer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    torn_down = []
    monkeypatch.setattr(server.threading, "Timer", _Timer)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20.0)
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, *, end_reason: torn_down.append((session["_sid"], end_reason)) or True,
    )

    def _worker():
        thread_started.set()
        # Thread continues working in background
        while not release_thread.wait(0.02):
            pass
        with session["history_lock"]:
            session["history"].append({"role": "assistant", "content": "background work done"})
            session["running"] = False
        thread_completed.set()

    worker_thread = threading.Thread(target=_worker, daemon=True)
    live_transport = _LiveTransport()
    session = _session(
        sid,
        running=True,
        transport=live_transport,
        _run_thread=worker_thread,
        close_on_disconnect=True,  # Even with close_on_disconnect!
    )
    server._sessions[sid] = session
    server._pending_ws_reaps.clear()

    try:
        worker_thread.start()
        assert thread_started.wait(2.0), "worker thread failed to start"

        # Client disconnects while thread is actively running
        reaped, detached = server._close_sessions_for_transport(live_transport)
        assert (reaped, detached) == (0, 1), "running thread must not be reaped on disconnect"
        assert session["transport"] is server._detached_ws_transport
        assert sid in server._pending_ws_reaps
        reap_timer = timers[-1]

        # Orphan reap fires while thread is still working: must defer and not interrupt
        reap_timer.callback()
        assert sid in server._sessions, "session must remain live while thread runs"
        assert torn_down == []
        assert not session.get("_client_gone_interrupt_requested")
        assert not session.get("_turn_cancel_requested")
        assert timers[-1].delay == 20.0

        # Let the real thread complete its work
        release_thread.set()
        assert thread_completed.wait(2.0), "worker thread failed to finish"
        worker_thread.join(timeout=2.0)
        assert not session["running"]
        assert session["history"] == [{"role": "assistant", "content": "background work done"}]

        # Next reap callback reaps the finished/idle session
        timers[-1].callback()
        assert sid not in server._sessions
        assert torn_down == [(sid, "ws_orphan_reap")]
    finally:
        release_thread.set()
        worker_thread.join(timeout=2.0)
        server._sessions.pop(sid, None)
        server._pending_ws_reaps.pop(sid, None)


@pytest.mark.parametrize("probe", ["input", "approval"])
def test_unreadable_work_state_is_not_permission_to_reap(monkeypatch, probe):
    sid = "unknown-work-state"
    session = _session(sid)
    def unavailable(*_args):
        raise RuntimeError("state unavailable")
    if probe == "input":
        monkeypatch.setattr("tui_gateway.server_requests.pending_kind", unavailable)
    else:
        monkeypatch.setattr("tools.approval.has_blocking_approval", unavailable)
    assert server._session_work_in_flight(sid, session)
    assert not server._session_is_lru_evictable(sid, session)


def test_same_owner_reattach_cancels_old_timer(monkeypatch):
    """Reattaching a live transport to a detached running session cancels the reap timer."""
    sid = "reattach-session"
    timers = []

    class _Timer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(server.threading, "Timer", _Timer)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20.0)

    old_transport = _LiveTransport()
    session = _session(sid, running=True, transport=old_transport)
    server._sessions[sid] = session
    server._pending_ws_reaps.clear()

    try:
        # Client disconnects
        server._close_sessions_for_transport(old_transport)
        assert session["transport"] is server._detached_ws_transport
        assert sid in server._pending_ws_reaps
        timer = timers[-1]
        assert not timer.cancelled

        # Client reconnects with new transport
        new_transport = _LiveTransport()
        with session["history_lock"]:
            server._rebind_live_transport(sid, session, new_transport)

        assert session["transport"] is new_transport
        assert sid not in server._pending_ws_reaps
        assert timer.cancelled, "rebind must cancel the pending orphan reap timer"
    finally:
        server._sessions.pop(sid, None)
        server._pending_ws_reaps.pop(sid, None)


@pytest.mark.parametrize(
    "busy_key,busy_val",
    [
        ("running", True),
        ("queued_prompt", {"text": "next prompt"}),
        ("queued_prompts", [{"text": "queued prompt"}]),
        ("_auto_continue_scheduled", True),
        ("agent_build_started", True),  # with unset agent_ready
        ("pending_human_input", True),
        ("pending_approval", True),
        ("active_delegation", True),
    ],
)
def test_close_on_disconnect_protects_every_busy_state(monkeypatch, busy_key, busy_val):
    """close_on_disconnect only immediately tears down when ACTUALLY idle.
    Any work in flight (running, queued, build, human input, approval, delegations)
    keeps the session alive and detaches instead."""
    sid = f"busy-{busy_key}"
    scheduled = []
    closed = []
    transport = _LiveTransport()

    session = _session(sid, transport=transport, close_on_disconnect=True)
    server._sessions[sid] = session
    server._pending_ws_reaps.clear()

    if busy_key == "agent_build_started":
        session["agent_build_started"] = True
        session["agent_ready"] = threading.Event()  # unset
    elif busy_key == "pending_human_input":
        monkeypatch.setattr("tui_gateway.server_requests.pending_kind", lambda s: "request_input" if s == sid else "")
    elif busy_key == "pending_approval":
        monkeypatch.setattr("tools.approval.has_blocking_approval", lambda key: key == sid)
    elif busy_key == "active_delegation":
        monkeypatch.setattr(server, "_session_has_active_delegations", lambda s, sess: s == sid)
    else:
        session[busy_key] = busy_val

    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda sess, *, end_reason: closed.append((sess["_sid"], end_reason)) or True,
    )
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", lambda s: scheduled.append(s))

    try:
        reaped, detached = server._close_sessions_for_transport(transport)
        assert (reaped, detached) == (0, 1), f"state {busy_key} must NOT be immediately reaped on disconnect"
        assert scheduled == [sid], f"state {busy_key} must schedule orphan reap for graceful backgrounding"
        assert closed == []
        assert session["transport"] is server._detached_ws_transport
        assert not server._session_is_lru_evictable(sid, session)
    finally:
        server._sessions.pop(sid, None)
        server._pending_ws_reaps.pop(sid, None)
