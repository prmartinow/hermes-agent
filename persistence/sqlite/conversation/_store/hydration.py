"""Bounded reads for older TUI clients using physical transcript row cursors."""

from typing import Any, Optional

from .core import _CONVERSATION_ROW_COLUMNS
from .messages import _resume_lineage_ids, _rows_to_conversation


def read_hydration_page(holder, session_id: str, *, limit: int, offset: int,
                        after_row_id: int | None, high_watermark: int | None):
    if limit < 0 or offset < 0:
        raise ValueError("hydration page bounds must be nonnegative")
    session_ids = _resume_lineage_ids(holder, session_id)
    placeholders = ",".join("?" for _ in session_ids)
    visible = f"session_id IN ({placeholders}) AND (active = 1 OR compacted = 1)"
    after = after_row_id or 0
    if high_watermark is None and after_row_id is not None:
        row = holder._read_one(f"SELECT MAX(id) FROM messages WHERE {visible}", tuple(session_ids))
        high_watermark = int(row[0] or 0)
    upper = " AND id <= ?" if high_watermark is not None else ""
    bounds = (high_watermark,) if high_watermark is not None else ()
    rows = holder._read_all(
        f"SELECT session_id, compacted, {_CONVERSATION_ROW_COLUMNS} FROM messages "
        f"WHERE {visible} AND id > ?{upper} ORDER BY id LIMIT ? OFFSET ?",
        (*session_ids, after, *bounds, limit, offset),
    )
    messages = _rows_to_conversation(holder, rows, session_id=session_id, include_ancestors=True,
                                     repair_alternation=False, include_row_ids=True)
    return messages, high_watermark or 0, int(rows[-1]["id"]) if rows else after


def get_compaction_recovery_messages(holder, session_id: str) -> dict[str, Any]:
    """Fetch detached archived and active conversation messages for one session in a single query.

    Uses one SQL statement to provide an atomic DB boundary:
    WHERE session_id = ? AND (active = 1 OR (active = 0 AND compacted = 1)) ORDER BY id.
    Separates archived (active=0, compacted=1) and active (active=1) rows.
    Does not apply display deduplication.
    Returns:
        {
            "archived_messages": List[Dict[str, Any]],
            "active_messages": List[Dict[str, Any]],
            "observed_watermark": Optional[int],
        }
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    rows = holder._read_all(compaction_recovery_query(), (session_id,))
    return project_compaction_recovery(holder, session_id, rows)


def compaction_recovery_query():
    return (
        f"SELECT compacted, {_CONVERSATION_ROW_COLUMNS} "
        f"FROM messages WHERE session_id = ? "
        f"AND (active = 1 OR (active = 0 AND compacted = 1)) "
        f"ORDER BY id"
    )


def project_compaction_recovery(holder, session_id, rows):
    archived_rows = []
    active_rows = []
    max_id: Optional[int] = None
    for row in rows:
        rid = row["id"]
        if rid is not None:
            rid_int = int(rid)
            if max_id is None or rid_int > max_id:
                max_id = rid_int
        if row["active"] == 1:
            active_rows.append(row)
        elif row["active"] == 0 and row["compacted"] == 1:
            archived_rows.append(row)

    archived_messages = _rows_to_conversation(
        holder,
        archived_rows,
        session_id=session_id,
        include_ancestors=False,
        repair_alternation=False,
        include_row_ids=True,
        include_summary_markers=True,
    )
    active_messages = _rows_to_conversation(
        holder,
        active_rows,
        session_id=session_id,
        include_ancestors=False,
        repair_alternation=False,
        include_row_ids=True,
        include_summary_markers=True,
    )
    return {
        "archived_messages": archived_messages,
        "active_messages": active_messages,
        "observed_watermark": max_id,
    }
