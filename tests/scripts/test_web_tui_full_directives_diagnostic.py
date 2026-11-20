"""Offline orchestration checks for the browser diagnostic."""

import asyncio
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "full_directives_diagnostic", Path(__file__).parents[2] / "scripts" / "test_web_tui_full_directives_cdp.py"
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def test_audit_encodes_the_session_and_runs_phases_in_order(monkeypatch):
    calls = []

    class Response:
        def read(self):
            return json.dumps([{
                "url": "http://127.0.0.1:9119/chat",
                "title": "test",
                "webSocketDebuggerUrl": "ws://127.0.0.1/fake",
            }]).encode()

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, message):
            self.request = json.loads(message)
            calls.append((self.request["method"], self.request["params"]))

        async def recv(self):
            return json.dumps({"id": self.request["id"], "result": {}})

    async def no_sleep(_seconds):
        pass

    async def history(_cdp_call):
        calls.append(("history", {}))

    async def interactions(_cdp_call):
        calls.append(("interactions", {}))

    monkeypatch.setattr(audit.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(audit.websockets, "connect", lambda *_args, **_kwargs: Socket())
    monkeypatch.setattr(audit.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(audit, "check_history_and_click", history)
    monkeypatch.setattr(audit, "check_mouse_and_clipboard", interactions)

    assert asyncio.run(audit.run_full_directives_audit("test&resume=other")) is True
    assert calls == [
        ("Page.enable", {}),
        ("Runtime.enable", {}),
        ("DOM.enable", {}),
        ("Page.navigate", {"url": "http://127.0.0.1:9119/chat?resume=test%26resume%3Dother"}),
        ("history", {}),
        ("interactions", {}),
    ]
