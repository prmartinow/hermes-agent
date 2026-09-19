"""Focused tests for dashboard PTY reconnect breadcrumbs."""

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest
import hermes_cli.web_server_chat as _web_server_chat


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="PTY bridge is POSIX-only"
)


class _OneFrameBridge:
    def __init__(self):
        self._sent = False
        self.closed = False

    @classmethod
    def spawn(cls, *args, **kwargs):
        return cls()

    def read(self, timeout):
        if not self._sent:
            self._sent = True
            return b"ready"
        return None

    def resize(self, *, cols, rows):
        pass

    def write(self, raw):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def pty_client(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient

    import hermes_cli.web_server as ws

    monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", _OneFrameBridge.spawn)
    ws.app.state.pty_active_session_files = {}

    client = TestClient(ws.app)
    return ws, client, ws._SESSION_TOKEN


def _url(token: str, **params: str) -> str:
    return f"/api/pty?{urlencode({'token': token, **params})}"






def test_fresh_param_ignores_channel_active_session_file(pty_client, monkeypatch):
    """Explicit fresh starts must not resurrect the prior channel session."""
    ws, client, token = pty_client
    channel = "fresh-chan"
    active_file = _web_server_chat._active_session_file_for_channel(ws.app, channel)
    active_file.write_text(json.dumps({"session_id": "sess-old"}), encoding="utf-8")
    captured = {}

    def fake_resolve(resume=None, sidecar_url=None, profile=None, active_session_file=None):
        captured["active_session_file"] = active_session_file
        captured["resume"] = resume
        return (["fake-hermes-tui"], None, None)

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv", fake_resolve)

    with client.websocket_connect(_url(token, channel=channel, fresh="1")) as conn:
        assert conn.receive_bytes() == b"ready"

    assert captured["resume"] is None
    assert captured["active_session_file"] == str(active_file)
    assert not active_file.exists()


def test_active_session_fallback_sends_resume_control_message(pty_client, monkeypatch):
    """Implicit resume (no `?resume=`) must tell the client which session.

    Regression for #93518: the dashboard's stick-to-bottom replay logic only
    fires when the frontend can see a resume id. Without `?resume=` on the URL
    it previously had no way to learn that `pty_ws` fell back to the
    per-channel active-session file, so the viewport stayed pinned at the top
    of the replayed scrollback.
    """
    ws, client, token = pty_client
    channel = "implicit-resume-chan"
    active_file = _web_server_chat._active_session_file_for_channel(ws.app, channel)
    active_file.write_text(json.dumps({"session_id": "sess-old"}), encoding="utf-8")

    monkeypatch.setattr(
        _web_server_chat, "_resolve_chat_argv", lambda **kw: (["fake-hermes-tui"], None, None)
    )

    with client.websocket_connect(_url(token, channel=channel)) as conn:
        assert conn.receive_json() == {"type": "resume", "id": "sess-old"}
        assert conn.receive_bytes() == b"ready"


def test_explicit_resume_sends_no_control_message(pty_client, monkeypatch):
    """An explicit `?resume=` already tells the client via the URL param."""
    ws, client, token = pty_client
    channel = "explicit-resume-chan"

    monkeypatch.setattr(
        _web_server_chat, "_resolve_chat_argv", lambda **kw: (["fake-hermes-tui"], None, None)
    )

    with client.websocket_connect(
        _url(token, channel=channel, resume="sess-explicit")
    ) as conn:
        # The first (and only) frame is PTY output, not a control message.
        assert conn.receive_bytes() == b"ready"


def test_child_eof_closes_socket_and_bridge(pty_client, monkeypatch):
    """Child EOF must close the WS server-side and reap the PTY.

    Regression for the FD leak (#54028): the reader task hits EOF when the
    PTY child exits, but if the browser's socket is half-open (no FIN), the
    writer loop's ``ws.receive()`` would block forever and the PTY fds would
    never be closed. The reader now closes the WebSocket on EOF so the
    handler's ``finally`` runs ``bridge.close()``.
    """
    ws, client, token = pty_client
    bridges = []

    class _RecordingBridge(_OneFrameBridge):
        @classmethod
        def spawn(cls, *args, **kwargs):
            b = cls()
            bridges.append(b)
            return b

    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", _RecordingBridge.spawn)
    monkeypatch.setattr(
        _web_server_chat, "_resolve_chat_argv", lambda **kw: (["fake-hermes-tui"], None, None)
    )

    # The client never sends a disconnect of its own — it only reads the one
    # frame then the server side must tear everything down on child EOF.
    with client.websocket_connect(_url(token, channel="eof-chan")) as conn:
        assert conn.receive_bytes() == b"ready"
        # Server closes the socket after the child EOFs; receiving again
        # surfaces the close rather than hanging.
        with pytest.raises(Exception):
            conn.receive_bytes()

    assert len(bridges) == 1
    # bridge.close() runs in the handler's `finally` via asyncio.to_thread,
    # which can lag the client-side context exit by a tick or two. Poll briefly
    # instead of asserting immediately so the teardown isn't a race.
    import time

    deadline = time.monotonic() + 5.0
    while not bridges[0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridges[0].closed is True


def test_warm_attach_sends_replay_control_before_snapshot_and_matching_request(pty_client, monkeypatch):
    """Warm attach to an existing keep-alive PTY must send an out-of-band JSON
    replay-start control frame carrying the replay generation BEFORE the binary
    snapshot, and write the matching private OSC 777 redraw request to the bridge
    instead of sending Ctrl+L (b"\x0c").
    """
    ws, client, token = pty_client
    channel = "warm-chan"

    bridge_instance = None

    class _WarmBridge(_OneFrameBridge):
        def __init__(self):
            super().__init__()
            nonlocal bridge_instance
            self.written = []
            bridge_instance = self

        @classmethod
        def spawn(cls, *args, **kwargs):
            return cls()

        def read(self, timeout):
            if not self._sent:
                self._sent = True
                return b"ready"
            return b""

        async def write(self, raw):
            self.written.append(bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw.encode("utf-8"))
            return True

    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", _WarmBridge.spawn)
    monkeypatch.setattr(
        _web_server_chat, "_resolve_chat_argv", lambda **kw: (["fake-hermes-tui"], None, None)
    )

    attach_id = "warm-test-device"
    # First connection: fresh PTY is spawned
    with client.websocket_connect(_url(token, channel=channel, attach=attach_id)) as conn1:
        # First frame is raw output, no replay-start control
        assert conn1.receive_bytes() == b"ready"

    assert bridge_instance is not None
    bridge_instance.written.clear()

    # Second connection: warm attach to existing PTY without ?resume=
    with client.websocket_connect(_url(token, channel=channel, attach=attach_id)) as conn2:
        # 1. Control frame MUST arrive BEFORE binary snapshot
        ctrl = conn2.receive_json()
        assert ctrl["type"] == "replay-start"
        assert "generation" in ctrl
        gen = ctrl["generation"]

        # 2. Binary snapshot arrives next
        snap = conn2.receive_bytes()
        assert snap == b"ready"

        # 3. Matching private replay request sent to bridge instead of Ctrl+L
        expected_payload = f"\x1b]777;hermes-replay;request;{gen}\x07".encode("ascii")
        written_combined = b"".join(bridge_instance.written)
        assert expected_payload in written_combined
        assert b"\x0c" not in written_combined
