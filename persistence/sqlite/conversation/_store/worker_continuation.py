"""Atomic capture and terminal stamping of a delegated worker's transcript."""

import sqlite3
import time
from typing import Any, Optional

from session_rules.worker_continuation import compute_active_transcript_digest, is_terminal_outcome

from .core import _CONVERSATION_ROW_COLUMNS
from .messages import _rows_to_conversation
from .sessions import _MODEL_CONFIG_ROW_MISSING, _merge_model_config_json


def stamp_child_terminal_session(holder, session_id: str, status: str, exit_reason: str) -> Optional[dict[str, Any]]:
    """Atomically capture active transcript watermark, compute canonical digest, and merge _delegate_terminal marker."""
    if not isinstance(session_id, str) or not session_id or not is_terminal_outcome(status, exit_reason):
        return None

    return holder._execute_write(lambda conn: stamp_terminal_on_connection(holder, conn, session_id, status, exit_reason))


def stamp_terminal_on_connection(holder, conn, session_id, status, exit_reason):
    rows = conn.execute(
        f"SELECT {_CONVERSATION_ROW_COLUMNS} FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
        (session_id,),
    ).fetchall()
    active_rows = [dict(r) for r in rows]
    count = len(active_rows)
    if count == 0:
        return None
    max_id = max((int(r["id"]) for r in active_rows if r.get("id") is not None), default=None)
    digest = compute_active_transcript_digest(active_rows)
    terminal_marker = {
        "version": 1,
        "status": str(status),
        "exit_reason": str(exit_reason),
        "message_count": count,
        "max_row_id": max_id,
        "active_transcript_digest": digest,
        "completed_at": time.time(),
    }
    merged = _merge_model_config_json(holder, conn, session_id, {"_delegate_terminal": terminal_marker})
    if merged is _MODEL_CONFIG_ROW_MISSING:
        return None
    conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (merged, session_id))
    return terminal_marker


def capture_terminal_child_session(
    holder, session_id: str, expected_parent_id: str, *, include_compacted: bool = False,
) -> dict[str, Any]:
    """Capture owned child metadata, lease and transcript in ONE read-only SQL statement.

    No explicit transaction is opened on a pooled connection. Ownership gates
    the message join, so foreign/non-delegated transcripts are never loaded.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(expected_parent_id, str) or not expected_parent_id:
        raise ValueError("expected_parent_id must be a non-empty string")
    if type(include_compacted) is not bool:
        raise ValueError("include_compacted must be a boolean")
    with holder._read_ctx() as conn:
        if conn.in_transaction:
            raise sqlite3.OperationalError("Cannot capture child session inside an existing transaction")
        return capture_terminal_on_connection(holder, conn, session_id, expected_parent_id,
                                              include_compacted=include_compacted)


def capture_terminal_on_connection(holder, conn, session_id, expected_parent_id, *, include_compacted,
                                   require_closed_owner=False):
    columns = ", ".join(f"m.{col.strip()}" for col in _CONVERSATION_ROW_COLUMNS.split(","))
    archived = " OR (m.active = 0 AND m.compacted = 1)" if include_compacted else ""
    owner = (" AND s.parent_session_id = ? AND s.source = 'subagent' AND s.ended_at > 0 "
             "AND CASE WHEN json_valid(s.model_config) THEN "
             "json_extract(s.model_config, '$._delegate_from') = ? "
             "AND json_type(s.model_config, '$._delegate_terminal') = 'object' ELSE 0 END "
             "AND (l.holder IS NULL OR l.expires_at <= ?)" if require_closed_owner else "")
    sql = (
        "SELECT s.id AS _source_id, s.parent_session_id AS _source_parent, "
        "s.source AS _source_kind, s.model_config AS _source_config, "
        "s.ended_at AS _source_ended, s.end_reason AS _source_end_reason, "
        "l.holder AS _lease_holder, l.expires_at AS _lease_expiry, "
        f"m.compacted, {columns} FROM sessions s "
        "LEFT JOIN session_turn_leases l ON l.conversation_id = s.id "
        "LEFT JOIN messages m ON m.session_id = s.id "
        f"AND (m.active = 1{archived}) AND s.parent_session_id = ? "
        "AND CASE WHEN json_valid(s.model_config) THEN "
        "json_type(s.model_config, '$._delegate_from') = 'text' "
        "AND json_extract(s.model_config, '$._delegate_from') = ? ELSE 0 END "
        f"WHERE s.id = ?{owner} ORDER BY m.id"
    )
    args = (expected_parent_id, expected_parent_id, session_id)
    if require_closed_owner:
        args += (expected_parent_id, expected_parent_id, time.time())
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return {"found": False, "error": f"Session {session_id!r} not found"}
    first = rows[0]
    session = {
        "id": first["_source_id"], "parent_session_id": first["_source_parent"],
        "source": first["_source_kind"], "model_config": first["_source_config"],
        "ended_at": first["_source_ended"], "end_reason": first["_source_end_reason"],
    }
    lease = None
    if first["_lease_holder"] is not None or first["_lease_expiry"] is not None:
        lease = {"holder": first["_lease_holder"], "expires_at": first["_lease_expiry"]}
    active_rows = [row for row in rows if row["id"] is not None and row["active"] == 1]
    archived_rows = [row for row in rows if row["id"] is not None
                     and row["active"] == 0 and row["compacted"] == 1]
    decode = dict(session_id=session_id, include_ancestors=False, repair_alternation=False,
                  include_row_ids=True, include_summary_markers=True)
    return {
        "found": True, "session": session, "lease": lease,
        "active_messages": _rows_to_conversation(holder, active_rows, **decode),
        "archived_messages": _rows_to_conversation(holder, archived_rows, **decode),
        "active_count": len(active_rows),
        "max_active_row_id": max((row["id"] for row in active_rows), default=None),
        "active_transcript_digest": compute_active_transcript_digest(active_rows),
    }
