"""Real private IPC: no fake socket, process identity or retirement fence."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile

import pytest

from hermes_cli.pty_retirement import request_idle_retirement, _request

pytestmark = pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer PID required")

CHILD = '''
import os, sys, json
output = sys.stdout
from hermes_cli.pty_retirement import start_retirement_server
from hermes_cli.backend_retirement import retirement
from hermes_cli.web_server_idle_proof import idle_proof
server = start_retirement_server(sys.argv[1])
print(json.dumps(idle_proof()), file=output, flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == 'busy':
        result = retirement.acquire()
    elif command == 'release':
        retirement.release()
        result = True
    else:
        result = retirement.acquire()
        if result:
            retirement.release()
    print(json.dumps(result), file=output, flush=True)
'''


def receive(child):
    assert select.select([child.stdout], [], [], 20)[0], "gateway control child timed out"
    line = child.stdout.readline()
    assert line, "gateway control child exited: " + child.stderr.read()[-2000:]
    return json.loads(line)


@pytest.fixture
def control():
    with tempfile.TemporaryDirectory(prefix="pr-") as directory:
        path = str(Path(directory) / "control.sock")
        env = {**os.environ, "HERMES_HOME": directory, "HERMES_TEST_ISOLATION": "1"}
        child = subprocess.Popen([sys.executable, "-c", CHILD, path], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            assert receive(child)["idle"] is True
            yield child, path
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def command(child, text):
    child.stdin.write(text + "\n")
    child.stdin.flush()
    return receive(child)


def test_busy_rejected_idle_committed_and_admissions_fenced(control):
    child, path = control
    assert command(child, "busy") is True
    assert request_idle_retirement(path, expected_parent_pid=os.getpid()) is False
    assert child.poll() is None
    assert command(child, "release") is True
    assert request_idle_retirement(path, expected_parent_pid=os.getpid()) is True
    assert command(child, "admit") is False
    # Lost replies can be reconciled without opening admission again.
    assert request_idle_retirement(path, expected_parent_pid=os.getpid()) is True
    assert Path(path).stat().st_mode & 0o777 == 0o600


def test_wrong_process_rejected_before_mutation(control):
    child, path = control
    assert request_idle_retirement(path, expected_parent_pid=child.pid) is False
    assert command(child, "admit") is True


def test_prepare_blocks_admission_cancel_reopens(control):
    child, path = control
    prepared = _request(path, os.getpid(), {"action": "prepare"})
    assert prepared["ok"] is True
    assert command(child, "admit") is False
    assert _request(path, os.getpid(), {"action": "cancel", "token": prepared["token"]})["ok"]
    assert command(child, "admit") is True


def test_real_bridge_to_gateway_entry_retirement():
    import time
    from types import SimpleNamespace
    from hermes_cli.pty_bridge import PtyBridge
    from hermes_cli.pty_memory import pty_memory_usage
    with tempfile.TemporaryDirectory(prefix="pg-") as directory:
        env = {**os.environ, "HERMES_HOME": directory, "HERMES_TEST_ISOLATION": "1",
               "HERMES_TUI_DASHBOARD": "1"}
        env.pop("HERMES_TUI_GATEWAY_URL", None)
        wrapper = "import subprocess,sys; subprocess.run([sys.executable, '-m', 'tui_gateway.entry'])"
        bridge = PtyBridge.spawn([sys.executable, "-c", wrapper], env=env)
        path = bridge._retirement_socket
        try:
            output = b""
            deadline = time.monotonic() + 20
            while b"gateway.ready" not in output and time.monotonic() < deadline:
                output += bridge.read(.1) or b""
            assert b"gateway.ready" in output, "real gateway did not reach readiness"
            usage = pty_memory_usage([SimpleNamespace(bridge=bridge)])
            assert isinstance(usage, int) and usage > 0
            assert bridge.retire_if_idle() is True
            assert bridge.is_alive()
        finally:
            bridge.close()
        assert not bridge.is_alive()
        assert not Path(path).exists()


def test_missing_socket_is_unknown_not_idle():
    assert not request_idle_retirement("/nonexistent/retirement.sock", expected_parent_pid=os.getpid())
