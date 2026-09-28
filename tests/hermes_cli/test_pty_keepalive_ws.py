import json

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat


class FakeBridge:
    def __init__(self):
        self.alive = True
        self.accept_input = True
        self.written = bytearray()

    def read(self, timeout):
        return b""        # idle forever

    async def write(self, data):
        if not self.accept_input:
            return False
        self.written.extend(data)
        return True

    def resize(self, cols, rows):
        pass

    def close(self):
        self.alive = False


@pytest.fixture
def pty_keepalive_harness(monkeypatch):
    class Spawned(list):
        pass

    from threading import Event
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
    monkeypatch.setattr(_web_server_chat, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(_web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**kw):
        resume = "child" if kw.get("resume") == "parent" else kw.get("resume")
        env = {"HERMES_TUI_RESUME": resume} if resume else {}
        return (["x", resume or "fresh"], "/tmp", env)

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", fake_argv)

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
        gen = ctrl["generation"]
        ws2.send_bytes(b"again")
    assert len(pty_keepalive_harness) == 1                # reattached, did not respawn
    expected_request = f"\x1b]777;hermes-replay;request;{gen}\x07".encode("ascii")
    written = bytes(pty_keepalive_harness.bridges[0].written)
    assert written == b"hi" + expected_request + b"again"
    assert b"\x0c" not in written


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
    assert written == b"hi" + expected_request + b"again"
    assert b"\x0c" not in written




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


@pytest.mark.anyio
async def test_attach_token_suppresses_redraw_when_buffer_large(pty_keepalive_harness):
    """Reattaching to a session with >256 bytes in the ring buffer emits OSC 777 request and retains userdata, not \x0c."""
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
        gen = ctrl["generation"]
        snap = ws2.receive_bytes()
        assert snap == b"X" * 300
        ws2.send_bytes(b"after")

    expected_request = f"\x1b]777;hermes-replay;request;{gen}\x07".encode("ascii")
    written = bytes(pty_keepalive_harness.bridges[0].written)
    assert written == b"init" + expected_request + b"after"
    assert b"\x0c" not in written


@pytest.mark.anyio
async def test_compaction_tip_reuses_retained_owner(pty_keepalive_harness, monkeypatch):
    from starlette.testclient import TestClient

    tip = ["child"]
    async def argv(**kwargs):
        return ["x", tip[0]], "/tmp", {"HERMES_TUI_RESUME": tip[0]}

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", argv)
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=DEVICE&resume=parent") as ws:
        ws.send_bytes(b"before")
    key = "resume\0\0child\0DEVICE"
    session = _web_server_chat.PTY_REGISTRY._sessions[key]
    file = web_server.app.state.pty_active_session_files[key]
    file.write_text(json.dumps({"session_id": "next-child"}))
    tip[0] = "next-child"
    for resume in ["parent", "child", "next-child"]:
        with client.websocket_connect(f"/api/pty?attach=DEVICE&resume={resume}") as ws:
            assert ws.receive_json()["type"] == "replay-start"
            ws.send_bytes(b"after")
    assert len(pty_keepalive_harness) == 1
    assert _web_server_chat.PTY_REGISTRY._sessions[key] is session
    assert web_server.app.state.pty_active_session_files[key] == file


def test_effective_pty_key_formats():
    from hermes_cli.web_routers.chat_ws import _effective_pty_key

    # None cases
    assert _effective_pty_key(None, None, None) is None
    assert _effective_pty_key(None, "work", None) is None

    # Attach only
    assert _effective_pty_key("TOK1", None, None) == "TOK1"

    # Attach + profile
    assert _effective_pty_key("TOK1", "work", None) == "TOK1\0work\0"

    # Resume only / Attach + resume
    assert _effective_pty_key(None, None, "sess1") == "resume\0\0sess1\0"
    assert _effective_pty_key("TOK1", None, "sess1") == "resume\0\0sess1\0TOK1"
    assert _effective_pty_key("TOK1", "work", "sess1") == "resume\0work\0sess1\0TOK1"


@pytest.mark.anyio
async def test_forced_fresh_with_rotated_token_preserves_prior_work(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws1:
        ws1.send_bytes(b"hi")
    assert len(pty_keepalive_harness) == 1
    bridge0 = pty_keepalive_harness.bridges[0]
    assert bridge0.alive is True

    # The frontend rotates its token for fresh starts; the old owner stays alive.
    with client.websocket_connect("/api/pty?attach=TOK_NEW&fresh=1") as ws2:
        ws2.send_bytes(b"fresh")
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


@pytest.mark.anyio
async def test_canonical_resume_preserves_active_session_file_ownership(
    pty_keepalive_harness,
):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&resume=parent") as ws1:
        ws1.send_bytes(b"hi")

    # The canonical key is resume\0\0child\0TOK1
    files = web_server.app.state.pty_active_session_files
    canonical_key = "resume\0\0child\0TOK1"
    assert canonical_key in files
    assert "resume\0\0parent\0TOK1" not in files
    active_file = files[canonical_key]

    with client.websocket_connect("/api/pty?attach=TOK1&resume=child") as ws2:
        ws2.send_bytes(b"again")

    assert files[canonical_key] == active_file

