"""Keep offline evaluation tests away from user state and external providers."""
import ipaddress
import os
from pathlib import Path
import socket
import tempfile

import pytest

_sandbox = Path(tempfile.mkdtemp(prefix="hermes-compaction-eval-"))
os.environ["HOME"] = str(_sandbox)
os.environ["HERMES_HOME"] = str(_sandbox / "hermes")
os.environ["OPENAI_API_KEY"] = "synthetic-test-key"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
_home = Path(os.environ["HERMES_HOME"])
_home.mkdir()
(_home / "config.yaml").write_text(
    'model:\n  provider: openai-api\n  default: gpt-4o\n'
    'auxiliary:\n  compression:\n    provider: openai-api\n    model: gpt-4o\n',
    encoding="utf-8",
)

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
_blocked_network_attempts = []


def _require_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host in (None, "localhost"):
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    _blocked_network_attempts.append(str(host))
    raise AssertionError("External network disabled for offline evaluation tests")


def _local_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _require_loopback(address[0])
    return _connect(sock, address)


def _local_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _require_loopback(address[0])
    return _connect_ex(sock, address)


def _local_getaddrinfo(host, *args, **kwargs):
    _require_loopback(host)
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _local_connect
socket.socket.connect_ex = _local_connect_ex
socket.getaddrinfo = _local_getaddrinfo


@pytest.fixture(autouse=True)
def _offline_model_metadata(monkeypatch):
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *args, **kwargs: 1_000_000)
    yield
    assert not _blocked_network_attempts, "A test attempted external network access."
