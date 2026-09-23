"""Contract tests for the evidence collector; no live browser required."""
import asyncio
import importlib.util
import json
from pathlib import Path
import time

import pytest

_spec = importlib.util.spec_from_file_location('replay_diagnostic', Path(__file__).parents[1] / 'scripts' / 'diagnose_web_tui_replay.py')
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def test_binary_frames_count_decoded_bytes_and_text_counts_utf8():
    assert audit.frame_bytes({'opcode':2,'payloadData':'AP8='}) == b'\x00\xff'
    assert audit.frame_bytes({'opcode':1,'payloadData':'界'}) == '界'.encode()


def test_wrapping_preserves_cells_until_logical_line_completed():
    assert audit.logical_lines([['ab  ',False],['cd  ',True],['ef ',False]]) == ['ab  cd','ef']


def test_duplicate_candidate_requires_repeated_multiline_block():
    lines=[f'line {n}' for n in range(30)]
    before=audit.block_counts(lines)
    after=audit.block_counts(lines+lines)
    assert all(v == 1 for v in before.values())
    assert any(after[k] == 2 for k in before)


def test_url_redaction_drops_query_and_fragment():
    assert audit.safe_url('ws://localhost/api/pty?token=secret#private') == 'ws://localhost/api/pty'
    assert 'secret' not in audit.redact('ws://localhost/api/pty?token=secret')


class FakeSocket:
    def __init__(self, response):
        self.queue=asyncio.Queue()
        self.response=response
    async def send(self, raw):
        mid=json.loads(raw)['id']
        await self.queue.put(json.dumps({'id':mid,**self.response}))
    def __aiter__(self):return self
    async def __anext__(self):return await self.queue.get()


def test_cdp_error_is_not_empty_success():
    async def run():
        c=audit.CDP(FakeSocket({'error':{'message':'Invalid parameters'}}),time.monotonic())
        task=asyncio.create_task(c.receive())
        try:
            with pytest.raises(RuntimeError,match='Invalid parameters'):
                await c.call('Network.deleteCookies',{})
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_javascript_exception_is_not_missing_terminal():
    async def run():
        c=audit.CDP(FakeSocket({'result':{'exceptionDetails':{'text':'Uncaught'}}}),time.monotonic())
        task=asyncio.create_task(c.receive())
        try:
            with pytest.raises(RuntimeError,match='Uncaught'):await c.evaluate('bad()')
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_exact_websocket_event_is_captured_while_no_command_pending():
    async def run():
        ws=FakeSocket({});c=audit.CDP(ws,time.monotonic());task=asyncio.create_task(c.receive())
        await ws.queue.put(json.dumps({'method':'Network.webSocketFrameReceived','params':{'requestId':'s','timestamp':1,'response':{'opcode':2,'payloadData':'AP8='}}}))
        await asyncio.sleep(0)
        try:
            assert len(c.events)==1
            assert c.events[0]['bytes']==2
            assert 'payloadData' not in c.events[0]
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_late_growth_comparison_separates_repetition_from_scroll():
    hashes=[audit.digest(f'line {i}') for i in range(40)]
    before={'logicalHashes':hashes,'viewportY':10,'visibleHashes':hashes[10:15]}
    after={**before,'logicalHashes':hashes+hashes[12:32]}
    result=audit.compare_snapshots(before,after)
    assert result['unchangedLogicalPrefix']==40
    assert result['longestRepeatedRun']==20
    assert result['viewportUnchanged'] and result['visibleAnchorUnchanged']
    unique={**before,'logicalHashes':hashes+[audit.digest('new content')]}
    assert audit.compare_snapshots(before,unique)['longestRepeatedRun']==0
