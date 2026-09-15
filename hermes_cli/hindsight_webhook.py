"""Signed Hindsight ingress and durable, idempotent event inbox.

Only timestamped V2 signatures are accepted; dashboard credentials never grant
webhook authority. No payload is interpreted as an agent instruction.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from contextlib import closing

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

PATH = "/api/webhooks/hindsight"
MAX_BODY_BYTES = 256 * 1024
TOLERANCE_SECONDS = 300
RETENTION_SECONDS = 30 * 86400
_SIGNATURE = re.compile(r"t=([0-9]{1,12}),v1=([0-9a-f]{64})", re.ASCII)
_EVENTS = {"retain.completed", "consolidation.completed", "memory_defense.triggered"}


def is_hindsight_webhook(request: Request) -> bool:
    return request.method == "POST" and request.url.path == PATH


def is_authenticated_hindsight_webhook(request: Request) -> bool:
    return is_hindsight_webhook(request) and getattr(request.state, "hindsight_authenticated", False) is True


async def authenticate(request: Request) -> None:
    """Verify raw wire bytes, signed attempt time, and the event envelope."""
    secret = os.environ.get("HERMES_HINDSIGHT_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Hindsight webhook is not configured")
    signatures = request.headers.getlist("x-hindsight-signature-v2")
    match = _SIGNATURE.fullmatch(signatures[0]) if len(signatures) == 1 else None
    if match is None or abs(time.time() - int(match[1])) > TOLERANCE_SECONDS:
        raise HTTPException(401, "Invalid webhook signature")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Webhook body too large")
        body.extend(chunk)
    raw = bytes(body)
    expected = hmac.new(secret.encode(), match[1].encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match[2]) or abs(time.time() - int(match[1])) > TOLERANCE_SECONDS:
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid webhook JSON") from None
    if not isinstance(event, dict) or any(
        not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 1024
        for key in ("event", "bank_id", "operation_id", "status", "timestamp")
    ) or event["event"] not in _EVENTS or not isinstance(event.get("data"), dict):
        raise HTTPException(400, "Invalid webhook event")
    # Identity is the signed body, NOT the unsigned event header or attempt time.
    request.state.hindsight_event = event
    request.state.hindsight_digest = hashlib.sha256(raw).hexdigest()
    request.state.hindsight_authenticated = True


def _inbox_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "hindsight-webhooks" / "inbox.sqlite3"


def _record_event(event: dict, digest: str) -> bool:
    """Commit before ACK; SQLite uniqueness also covers concurrent processes/restarts."""
    path = _inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    now = time.time()
    with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            digest TEXT PRIMARY KEY, received_at REAL NOT NULL,
            event TEXT NOT NULL, bank_id TEXT NOT NULL,
            operation_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
        )""")
        conn.execute("DELETE FROM events WHERE received_at < ?", (now - RETENTION_SECONDS,))
        cursor = conn.execute(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (digest, now, event["event"], event["bank_id"], event["operation_id"],
             event["status"], json.dumps(event, ensure_ascii=True)),
        )
        return cursor.rowcount == 1


async def receive(request: Request) -> dict:
    if not is_authenticated_hindsight_webhook(request):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        inserted = await run_in_threadpool(
            _record_event, request.state.hindsight_event, request.state.hindsight_digest
        )
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "Webhook inbox unavailable") from None
    return {"status": "recorded" if inserted else "duplicate"}
