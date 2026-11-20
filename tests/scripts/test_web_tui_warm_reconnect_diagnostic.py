"""Offline contracts for the warm reconnect evidence collector."""

import asyncio
import importlib.util
import json
from pathlib import Path
import time


_spec = importlib.util.spec_from_file_location(
    "warm_reconnect_diagnostic", Path(__file__).parents[2] / "scripts" / "test_web_tui_warm_reconnect_cdp.py"
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


class FakeSocket:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def send(self, raw):
        await self.queue.put(json.dumps({"id": json.loads(raw)["id"], "result": {}}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


def test_collector_correlates_replay_frames_and_redacts_socket_identity():
    async def scenario():
        ws = FakeSocket()
        cdp = audit.CDP(ws, time.monotonic())
        receiver = asyncio.create_task(cdp.receive())
        frames = [
            {"method": "Network.webSocketCreated", "params": {
                "requestId": "socket", "url": "ws://localhost/api/pty?attach=private-token"}},
            {"method": "Network.webSocketFrameReceived", "params": {"requestId": "socket", "response": {
                "opcode": 1, "payloadData": "\x1b]777;hermes-replay;begin;abc-123\x07"}}},
            {"method": "Network.webSocketFrameReceived", "params": {"requestId": "socket", "response": {
                "opcode": 1, "payloadData": json.dumps({"type": "replay-start", "generation": "abc-123"})}}},
            {"method": "Network.webSocketFrameReceived", "params": {"requestId": "socket", "response": {
                "opcode": 1, "payloadData": '"replay-start"'}}},
            {"method": "Network.webSocketFrameReceived", "params": {"requestId": "socket", "response": {
                "opcode": 2, "payloadData": "a"}}},
            {"method": "Network.responseReceived", "params": {"requestId": "asset", "response": {
                "url": "http://localhost/app.js?secret=private", "status": 200, "mimeType": "text/javascript"}}},
        ]
        for frame in frames:
            await ws.queue.put(json.dumps(frame))
        try:
            await cdp.call("Runtime.enable")
            assert cdp.pty_ws_frames_count == 4
            assert len(cdp.pty_replay_start_frames) == 1
            assert [(event["phase"], event["generation"]) for event in cdp.pty_osc_events] == [("begin", "abc-123")]
            assert cdp.pty_replay_start_frames[0]["generation"] == "abc-123"
            assert cdp.socket_identities[0]["attachHash"] == audit.digest_identity("private-token")
            assert cdp.responses == [{"requestId": "asset", "url": "http://localhost/app.js", "status": 200}]
            assert "private-token" not in repr(cdp.socket_identities)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    asyncio.run(scenario())
