"""Exercise signed ingress through the real dashboard middleware stack."""
import hashlib
import hmac
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from hermes_cli import hindsight_webhook as hook
from hermes_cli import web_server

SECRET = "test-only-hindsight-webhook-secret"
EVENT = {"event": "retain.completed", "bank_id": "test-bank", "operation_id": "test-operation",
         "status": "completed", "timestamp": "2026-09-15T12:00:00Z",
         "data": {"document_id": "test-document", "memory_unit_count": 3}}
BODY = json.dumps(EVENT).encode()


def signed(body=BODY, timestamp=None, secret=SECRET):
    timestamp = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Hindsight-Signature-V2": f"t={timestamp},v1={mac}"}


@pytest.fixture(params=[False, True], ids=["loopback", "oauth"])
def client(request, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HINDSIGHT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(hook, "_inbox_path", lambda: tmp_path / "inbox" / "events.sqlite3")
    monkeypatch.setattr(web_server.app.state, "auth_required", request.param, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    return TestClient(web_server.app, base_url="http://127.0.0.1", follow_redirects=False)


def test_persists_and_deduplicates_retries(client):
    assert client.post(hook.PATH, content=BODY, headers=signed()).json() == {"status": "recorded"}
    assert client.post(hook.PATH, content=BODY, headers=signed(timestamp=int(time.time())+1)).json() == {"status": "duplicate"}
    # New connection / receiver instance sees durable deduplication, not a process cache.
    assert hook._record_event(EVENT, hashlib.sha256(BODY).hexdigest()) is False
    with sqlite3.connect(hook._inbox_path()) as conn:
        rows = conn.execute("SELECT event, bank_id, operation_id, status, payload FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0][:4] == (EVENT["event"], EVENT["bank_id"], EVENT["operation_id"], "completed")
    assert json.loads(rows[0][4]) == EVENT
    assert hook._inbox_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("headers", [
    {}, {"X-Hindsight-Signature": "sha256=" + "0"*64},
    {"X-Hindsight-Signature-V2": "t=not-a-time,v1=oops"},
    {"X-Hindsight-Signature-V2": "t=1,v1=" + "0"*64},
])
def test_unsigned_and_malformed_fail_closed(client, headers):
    assert client.post(hook.PATH, content=BODY, headers=headers).status_code == 401
    assert not hook._inbox_path().exists()


@pytest.mark.parametrize("offset", [-301, 301])
def test_freshness(client, offset):
    assert client.post(hook.PATH, content=BODY, headers=signed(timestamp=int(time.time())+offset)).status_code == 401


def test_tampering_and_wrong_key(client):
    assert client.post(hook.PATH, content=BODY+b" ", headers=signed()).status_code == 401
    assert client.post(hook.PATH, content=BODY, headers=signed(secret="wrong")).status_code == 401
    headers = list(signed().items()) * 2
    assert client.post(hook.PATH, content=BODY, headers=headers).status_code == 401


def test_dashboard_token_is_not_webhook_authority(client):
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    assert client.post(hook.PATH, content=BODY, headers=headers).status_code == 401


def test_unconfigured(client, monkeypatch):
    monkeypatch.delenv("HERMES_HINDSIGHT_WEBHOOK_SECRET")
    assert client.post(hook.PATH, content=BODY, headers=signed()).status_code == 503


@pytest.mark.parametrize("body", [b"bad json", b"[]", b"{}", b'{"event": []}'])
def test_bad_payload(client, body):
    assert client.post(hook.PATH, content=body, headers=signed(body)).status_code == 400
    assert not hook._inbox_path().exists()


def test_body_limit(client):
    body = b"x" * (hook.MAX_BODY_BYTES + 1)
    assert client.post(hook.PATH, content=body, headers=signed(body)).status_code == 413
    assert not hook._inbox_path().exists()


def test_other_routes_and_methods_stay_protected(client):
    for path in ("/api/config", hook.PATH+"/", hook.PATH+"/other", "/api/webhooks/other"):
        assert client.post(path, content=BODY, headers=signed()).status_code == 401
    assert client.get(hook.PATH, headers=signed()).status_code == 401


def test_host_protection_preserved(client):
    assert client.post(hook.PATH, content=BODY, headers={**signed(), "Host": "attacker.example"}).status_code == 400
    assert not hook._inbox_path().exists()


def test_persistence_failure_retries(client, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("test failure")
    monkeypatch.setattr(hook, "_record_event", broken)
    response = client.post(hook.PATH, content=BODY, headers=signed())
    assert response.status_code == 503
    assert "test failure" not in response.text


def test_concurrent_duplicate_is_atomic(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: hook._record_event(EVENT, hashlib.sha256(BODY).hexdigest()), range(8)))
    assert results.count(True) == 1
