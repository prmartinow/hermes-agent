"""Read the archived and current messages for a compaction recovery in one snapshot."""

from typing import Any, Optional

from .core import _CONVERSATION_ROW_COLUMNS
from .messages import _rows_to_conversation


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
