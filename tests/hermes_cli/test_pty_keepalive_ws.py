import asyncio
import json
from threading import Event

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat


class FakeBridge:
    def __init__(self):
        self.alive = True
        self.accept_input = True
        self.written = bytearray()
        self.replay_requests = []
        self.input_written = Event()

    def read(self, timeout):
        return b""        # idle forever

    async def write(self, data):
        if not self.accept_input:
            return False
        if data.startswith(b"\x1b]777;hermes-replay;request;") and data.endswith(b"\x07"):
            self.replay_requests.append(data)
        else:
            self.written.extend(data)
            self.input_written.set()
        return True

    def resize(self, cols, rows):
        pass

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


@pytest.fixture
def pty_keepalive_harness(monkeypatch):
    class Spawned(list):
        pass

    spawned = Spawned()
    spawned.bridges = []
    spawned.ready = Event()
    # Fake bridges have no OS process identity; budget accounting has its own real tests.
    monkeypatch.setattr(_web_server_chat.PTY_REGISTRY, "_memory_usage", lambda sessions: 0)

    def fake_spawn(argv, cwd=None, env=None):
        b = FakeBridge()
        spawned.append(argv)
        spawned.bridges.append(b)
        spawned.ready.set()
        return b

    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(_web_server_chat, "_ws_auth_reason", lambda ws, **_kwargs: (None, "test"))
    monkeypatch.setattr(_web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**kw):
        resume = "child" if kw.get("resume") == "parent" else kw.get("resume")
        env = {"HERMES_TUI_RESUME": resume} if resume else {}
        return (["x", resume or "fresh"], "/tmp", env)

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", fake_argv)
    # TestClient without ``with`` skips the lifespan, which reopens the registry
    # on startup; a test that ran close_all() must not shut it for the next one.
    monkeypatch.setattr(_web_server_chat.PTY_REGISTRY, "_closed", False)

    try:
        yield spawned
    finally:
        _web_server_chat.PTY_REGISTRY._sessions.clear()


@pytest.mark.parametrize("kind", ["memory", "capacity"])
def test_resource_refusal_preserves_resume_for_reload(pty_keepalive_harness, monkeypatch, kind):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from hermes_cli.pty_session import MemoryBudgetFull, RegistryFull

    seen = []

    async def refuse(key, **kwargs):
        seen.append(key)
        raise MemoryBudgetFull() if kind == "memory" else RegistryFull()

    monkeypatch.setattr(_web_server_chat.PTY_REGISTRY, "attach_or_spawn", refuse)
    with TestClient(web_server.app).websocket_connect("/api/pty?attach=same-tab&resume=kept-chat") as ws:
        text = ws.receive_text()
        assert "Start new session" not in text
        assert "same chat" in text
        if kind == "memory":
            assert "memory" in text.lower() and "too many" not in text.lower()
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_text()
        assert error.value.code == 4429
        assert error.value.reason == "terminal-" + kind
    assert len(seen) == 1 and "kept-chat" in seen[0] and "same-tab" in seen[0]
    assert not pty_keepalive_harness.bridges


@pytest.mark.anyio
async def test_attach_token_reuses_same_session(pty_keepalive_harness):
    """Two connects with the same ?attach= token hit one spawned bridge."""
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1") as ws2:
        ctrl = ws2.receive_json()
        assert ctrl["type"] == "replay-start"
        ws2.send_bytes(b"again")
    assert len(pty_keepalive_harness) == 1                # reattached, did not respawn
    assert bytes(pty_keepalive_harness.bridges[0].written) == b"hiagain"
    assert len(pty_keepalive_harness.bridges[0].replay_requests) == 1


@pytest.mark.asyncio
async def test_stalled_input_closes_only_the_keepalive_socket(
    pty_keepalive_harness,
):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws:
        assert pty_keepalive_harness.ready.wait(5)
        bridge = pty_keepalive_harness.bridges[0]
        bridge.accept_input = False
        ws.send_bytes(b"input")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 1013
    assert web_server.PTY_REGISTRY._sessions["TOK1"].alive is True
    assert bridge.alive is True


def test_chat_without_a_pty_bridge_says_what_to_install_as_on_main(pty_keepalive_harness, monkeypatch):
    """On a host whose PTY bridge cannot load, the dashboard's /chat tab prints main's message, which
    says the rest of the dashboard works and where to install Hermes for chat, and closes 1011."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(_web_server_chat, "_PTY_BRIDGE_AVAILABLE", False)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws:
        text = ws.receive_text()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()

    assert "Chat unavailable: the embedded terminal requires a POSIX PTY" in text
    assert "Install Hermes inside WSL2 to use the dashboard's /chat tab" in text
    assert closed.value.code == 1011
    assert pty_keepalive_harness == []


@pytest.mark.asyncio
async def test_a_takeover_during_input_leaves_the_chat_session_to_the_new_tab():
    """Dashboard /chat keep-alive: when another tab takes the session over while this tab's input
    waits on the PTY, that write is cancelled and the session stays alive for the new tab, which
    closes this one as superseded (4409), never as stalled input (1013). Only the attached tab's
    resize reaches the PTY."""
    from hermes_cli.pty_session import WS_CLOSE_SUPERSEDED, PtySession
    from hermes_cli.web_routers.chat_ws import _pump_keepalive_input

    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.resized = []

        async def write(self, data):
            if data == b"old":
                entered.set()
                await release.wait()
            return await super().write(data)

        def resize(self, cols, rows):
            self.resized.append((cols, rows))

    class Tab:
        def __init__(self, *frames):
            self.frames = asyncio.Queue()
            for frame in frames:
                self.frames.put_nowait(frame)
            self.closes = []

        async def receive(self):
            return await self.frames.get()

        async def send_bytes(self, data):
            pass

        async def close(self, code=1000, reason=None):
            self.closes.append(code)

    bridge = BlockingBridge()
    session = PtySession("k", bridge, buffer_cap=64, read_timeout=0.01)
    await session.start()
    old = Tab({"type": "websocket.receive", "bytes": b"old"})
    await session.attach(old)
    pump = asyncio.create_task(_pump_keepalive_input(old, session))
    await entered.wait()
    new = Tab()
    await session.attach(new)
    release.set()
    await asyncio.wait_for(pump, timeout=5)
    session.resize(old, cols=5, rows=6)
    session.resize(new, cols=80, rows=24)

    assert session.alive
    assert old.closes == [WS_CLOSE_SUPERSEDED]
    assert bridge.resized == [(80, 24)]
    assert await session.write(new, b"new")
    assert bytes(bridge.written) == b"new"
    await session.close()


@pytest.mark.asyncio
async def test_attach_token_reuses_same_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&resume=same") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1&resume=same") as ws2:
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "same"]]




@pytest.mark.anyio
async def test_attach_token_reuses_canonical_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&resume=parent") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1&resume=child") as ws2:
        ctrl = ws2.receive_json()
        assert ctrl["type"] == "replay-start"
        gen = ctrl["generation"]
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "child"]]
    expected_request = f"\x1b]777;hermes-replay;request;{gen}\x07".encode("ascii")
    written = bytes(pty_keepalive_harness.bridges[0].written)
    assert written == b"hiagain"
    assert pty_keepalive_harness.bridges[0].replay_requests == [expected_request]
    assert b"\x0c" not in written


@pytest.mark.anyio
async def test_compacted_session_tip_reuses_device_owner(pty_keepalive_harness, monkeypatch):
    from starlette.testclient import TestClient

    tip = ["child"]

    async def argv(**kwargs):
        return ["x", tip[0]], "/tmp", {"HERMES_TUI_RESUME": tip[0]}

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", argv)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=DEVICE&channel=DEVICE&resume=parent") as ws:
        ws.send_bytes(b"before")
    registry = _web_server_chat.PTY_REGISTRY
    key = "DEVICE\0\0child"
    owner = registry._sessions[key]
    owner.active_session_file.write_text(json.dumps({"session_id": "next-child"}))
    tip[0] = "next-child"
    with client.websocket_connect("/api/pty?attach=DEVICE&channel=DEVICE&resume=parent") as ws:
        assert ws.receive_json()["type"] == "replay-start"
        ws.send_bytes(b"after")
    assert len(pty_keepalive_harness) == 1
    assert registry._sessions[key] is owner




@pytest.mark.anyio
async def test_attach_token_reuses_default_chat_after_active_session_fallback(
    pty_keepalive_harness, tmp_path, monkeypatch
):
    from starlette.testclient import TestClient

    active_session_file = tmp_path / "active-session.json"
    monkeypatch.setattr(
        _web_server_chat,
        "_active_session_file_for_channel",
        lambda app, channel: active_session_file,
    )

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&channel=CHAT") as ws1:
        ws1.send_bytes(b"hi")

    active_session_file.write_text(json.dumps({"session_id": "existing"}))

    with client.websocket_connect("/api/pty?attach=TOK1&channel=CHAT") as ws2:
        ws2.send_bytes(b"again")

    assert pty_keepalive_harness == [["x", "fresh"]]


# --- #63553: pty_active_session_files must not grow without bound -----------

def _pty_marker_dict():
    from hermes_cli.web_server import _get_pty_active_session_files

    return _get_pty_active_session_files(web_server.app)


@pytest.mark.asyncio
async def test_channel_marker_leaves_no_dict_entry_after_pty_lifecycle(pty_keepalive_harness):
    """The channel→marker mapping is gone once the keep-alive PTY is closed.

    Regression for #63553: `_active_session_file_for_channel` inserted a
    channel→tempfile entry that nothing ever popped, so every dashboard
    reconnect leaked one dict entry plus a forgotten tempfile+inode.
    """
    from starlette.testclient import TestClient

    markers = _pty_marker_dict()
    markers.clear()
    client = TestClient(web_server.app)

    with client.websocket_connect("/api/pty?attach=TOK63553&channel=CHAN63553") as ws:
        ws.send_bytes(b"hi")
    assert "CHAN63553" in markers  # marker registered while the PTY is live

    # End the session: closing the registry (same path as TTL reap or child
    # EOF) must drop both the tempfile and the dict entry.
    marker_path = markers["CHAN63553"]
    await web_server.PTY_REGISTRY.close_all()
    assert marker_path.exists() is False
    assert "CHAN63553" not in markers


@pytest.mark.asyncio
async def test_channel_marker_reconnect_reuses_one_entry(pty_keepalive_harness):
    """Repeated same-channel reconnects keep one mapping, not one per hop."""
    from starlette.testclient import TestClient

    markers = _pty_marker_dict()
    markers.clear()
    client = TestClient(web_server.app)
    attach = "TOK63553R"
    try:
        for _ in range(3):
            with client.websocket_connect(f"/api/pty?attach={attach}&channel=CHAN63553R") as ws:
                ws.send_bytes(b"hi")
        assert list(markers) == ["CHAN63553R"]
    finally:
        await web_server.PTY_REGISTRY.close_all()
    assert markers == {}


@pytest.mark.asyncio
async def test_legacy_channel_connect_pops_marker_on_disconnect(pty_keepalive_harness):
    """A 1:1 (no attach token) PTY dies with its socket; its marker must too."""
    from starlette.testclient import TestClient

    markers = _pty_marker_dict()
    markers.clear()
    client = TestClient(web_server.app)

    with client.websocket_connect("/api/pty?channel=LEGACY63553") as ws:
        ws.send_bytes(b"hi")
        assert "LEGACY63553" in markers

    # The handler pops the marker after _legacy_pump returns, which can lag the
    # client-side context exit by a tick — poll instead of asserting immediately.
    import time

    deadline = time.monotonic() + 5.0
    while "LEGACY63553" in markers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert "LEGACY63553" not in markers


@pytest.mark.asyncio
async def test_legacy_marker_dropped_when_cancel_lands_mid_close(pty_keepalive_harness, monkeypatch):
    """The disconnect closes the bridge in a worker thread; cancelling the handler while that close is still
    running must not skip the marker discard."""
    import threading
    import time
    from starlette.testclient import TestClient

    close_started = threading.Event()

    def slow_close(self):
        close_started.set()
        time.sleep(0.5)  # still running when the client context exits and cancels the handler
        self.alive = False

    monkeypatch.setattr(FakeBridge, "close", slow_close)
    markers = _pty_marker_dict()
    markers.clear()
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?channel=LEGACYCANCEL") as ws:
        ws.send_bytes(b"hi")
        assert "LEGACYCANCEL" in markers
        ws.close()
        assert close_started.wait(5)  # teardown is inside bridge.close ...
    # ... and leaving the client context cancelled the handler there.
    deadline = time.monotonic() + 5.0
    while "LEGACYCANCEL" in markers and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert "LEGACYCANCEL" not in markers


@pytest.mark.asyncio
async def test_fresh_start_pops_stale_marker_and_reregisters_child_path(
    pty_keepalive_harness, tmp_path, monkeypatch
):
    """`?fresh=1` drops the stale breadcrumb AND keeps the new child's marker.

    Covers the resume regression flagged in #63563 review: popping the stale
    mapping must not leave the fresh child's path unmapped, or the next
    same-channel reconnect allocates a new path and loses the breadcrumb.
    """
    from starlette.testclient import TestClient

    markers = _pty_marker_dict()
    markers.clear()
    client = TestClient(web_server.app)

    with client.websocket_connect("/api/pty?attach=TOKFRESH&channel=CHANFRESH") as ws:
        ws.send_bytes(b"hi")
    stale_path = markers["CHANFRESH"]
    stale_path.write_text(json.dumps({"session_id": "stale"}))

    # Fresh start on the same channel: new PTY, new breadcrumb.
    with client.websocket_connect("/api/pty?attach=TOKFRESH2&channel=CHANFRESH&fresh=1") as ws:
        ws.send_bytes(b"hi")

    assert stale_path.exists() is False            # stale file removed
    assert markers["CHANFRESH"] is stale_path      # same mapping kept for the fresh child
    # The kept mapping is what lets the next same-channel reconnect resume the
    # fresh child instead of allocating a new unmapped path (#63563 review).
    with client.websocket_connect("/api/pty?attach=TOKFRESH2&channel=CHANFRESH") as ws:
        ws.send_bytes(b"hi")
    assert markers["CHANFRESH"] is stale_path
    await web_server.PTY_REGISTRY.close_all()
    assert markers == {}


@pytest.mark.anyio
async def test_attach_token_requests_a_private_replay_with_large_buffer(pty_keepalive_harness):
    """A large retained history still needs a frame, requested outside terminal input."""
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK_BUF") as ws1:
        ws1.send_bytes(b"init")

    # Manually append >256 bytes to the session ring buffer to simulate replayed history
    session = web_server.PTY_REGISTRY._sessions.get("TOK_BUF")
    assert session is not None
    session.buffer.append(b"X" * 300)

    with client.websocket_connect("/api/pty?attach=TOK_BUF") as ws2:
        ctrl = ws2.receive_json()
        assert ctrl["type"] == "replay-start"
        snap = ws2.receive_bytes()
        assert snap == b"X" * 300
        ws2.send_bytes(b"after")

    # The OSC protocol is separate from input; no form-feed enters the composer.
    assert bytes(pty_keepalive_harness.bridges[0].written) == b"initafter"
    assert len(pty_keepalive_harness.bridges[0].replay_requests) == 1


@pytest.mark.anyio
async def test_forced_fresh_with_rotated_token_preserves_prior_work(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws1:
        assert pty_keepalive_harness.ready.wait(5)
        bridge0 = pty_keepalive_harness.bridges[0]
        ws1.send_bytes(b"hi")
        assert bridge0.input_written.wait(5)
    assert len(pty_keepalive_harness) == 1
    assert bridge0.alive is True

    # Wait for the async fork and input dispatch, not merely WebSocket accept.
    # Closing immediately after accept can cancel a fork still in progress.
    pty_keepalive_harness.ready.clear()
    with client.websocket_connect("/api/pty?attach=TOK_NEW&fresh=1") as ws2:
        assert pty_keepalive_harness.ready.wait(5)
        ws2.send_bytes(b"fresh")
        assert pty_keepalive_harness.bridges[1].input_written.wait(5)
    assert len(pty_keepalive_harness) == 2
    assert bridge0.alive is True


@pytest.mark.anyio
async def test_tab_isolation_preserves_per_device_identity(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=DEVICE_A&resume=child") as ws1:
        ws1.send_bytes(b"a")
    with client.websocket_connect("/api/pty?attach=DEVICE_B&resume=child") as ws2:
        ws2.send_bytes(b"b")
    # Separate bridges spawned per device
    assert len(pty_keepalive_harness) == 2


@pytest.mark.anyio
async def test_profile_isolation_preserves_independent_sessions(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&profile=prof_a&resume=child") as ws1:
        ws1.send_bytes(b"a")
    with client.websocket_connect("/api/pty?attach=TOK1&profile=prof_b&resume=child") as ws2:
        ws2.send_bytes(b"b")
    # Separate bridges spawned per profile
    assert len(pty_keepalive_harness) == 2
