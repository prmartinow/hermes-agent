#!/usr/bin/env python3
"""
Context Retrieve Tool - Retrieve full or sliced tool output from Context Memory (context.db).

Allows the agent to query raw outputs that were offloaded to the SQLite FTS5 ContentStore
using an archive_id reference token (e.g. from an offloaded [ARCHIVE_ID: <uuid>] brief).
Supports full output retrieval or line-based pagination (offset, limit).
"""

import logging
import os
import sqlite3
import sys
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

AGENT_MEMORY_TRACES_PATH = os.getenv("AGENT_MEMORY_TRACES_PATH", str(get_hermes_home() / "agent-memory" / "traces"))
DB_PATH = os.getenv("AGENT_MEMORY_CONTEXT_DB_PATH", os.path.join(AGENT_MEMORY_TRACES_PATH, "context.db"))


def _get_retrieve_content_fn():
    """Import retrieve_content from archiver module if available."""
    try:
        if AGENT_MEMORY_TRACES_PATH not in sys.path and os.path.isdir(AGENT_MEMORY_TRACES_PATH):
            sys.path.insert(0, AGENT_MEMORY_TRACES_PATH)
        import importlib
        archiver_mod = importlib.import_module("archiver")
        return getattr(archiver_mod, "retrieve_content", None)
    except Exception as exc:
        logger.debug("Could not import retrieve_content from archiver: %s", exc)
        return None


def _retrieve_from_db_direct(archive_id: str) -> Optional[Dict[str, Any]]:
    """Direct query to context.db if archiver module import fails."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM content_archives WHERE archive_id = ?", (archive_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Direct context.db query failed: %s", exc)
        return None


def context_retrieve(
    archive_id: str,
    offset: Optional[int] = 1,
    limit: Optional[int] = None,
    session_id: str = "system-retrieve",
    tool_caller: str = "hermes-agent",
) -> str:
    """Retrieve full raw output or sliced lines from context.db using archive_id."""
    if not archive_id or not isinstance(archive_id, str):
        return tool_error("archive_id is required and must be a valid string UUID.")

    # 1. Try retrieve_content from archiver (includes audit logging)
    fn = _get_retrieve_content_fn()
    res = None
    if fn is not None:
        try:
            res = fn(archive_id=archive_id, session_id=session_id, tool_caller=tool_caller)
        except Exception as exc:
            logger.warning("retrieve_content call failed: %s", exc)

    # 2. Direct sqlite fallback
    if res is None:
        res = _retrieve_from_db_direct(archive_id)

    if not res:
        return tool_error(f"Archive ID '{archive_id}' not found in context database.")

    raw_content = res.get("raw_content", "")
    tool_name = res.get("tool_name", "tool")

    lines = raw_content.splitlines()
    total_lines = len(lines)

    # Slicing logic (1-indexed start line)
    try:
        start_line = max(1, int(offset)) if offset is not None else 1
    except (TypeError, ValueError):
        start_line = 1
    start_idx = start_line - 1

    if limit is not None:
        try:
            max_count = max(0, int(limit))
            end_idx = min(total_lines, start_idx + max_count)
        except (TypeError, ValueError):
            end_idx = total_lines
    else:
        end_idx = total_lines

    if start_idx >= total_lines and total_lines > 0:
        return f"[Archive {archive_id} ({tool_name}): offset {start_line} exceeds total lines ({total_lines})]"

    sliced_lines = lines[start_idx:end_idx]
    sliced_text = "\n".join(sliced_lines)

    # If full content was requested without slicing
    if start_idx == 0 and end_idx == total_lines:
        return sliced_text

    # Sliced content with header
    header = f"[Archive {archive_id} ({tool_name}) - Lines {start_line} to {end_idx} of {total_lines} total lines]\n"
    return header + sliced_text


def check_context_retrieve_requirements() -> bool:
    """Check if context retrieval is available (context.db exists or archiver module found)."""
    return os.path.exists(DB_PATH) or os.path.isdir(AGENT_MEMORY_TRACES_PATH)


CONTEXT_RETRIEVE_SCHEMA = {
    "name": "context_retrieve",
    "description": (
        "Retrieve full raw tool output or sliced lines from the context database (context.db) "
        "using an archive_id token (e.g. from an offloaded [ARCHIVE_ID: <uuid>] brief). "
        "Use this instead of re-running commands when you need more details from an offloaded output."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "archive_id": {
                "type": "string",
                "description": "The unique archive ID UUID returned in the offloaded output brief (e.g. 'c9a62...').",
            },
            "offset": {
                "type": "integer",
                "description": "Optional 1-indexed start line number for pagination/slicing (default: 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum number of lines to return. Omit to retrieve full remaining content.",
            },
        },
        "required": ["archive_id"],
    },
}


def _handle_context_retrieve(args: Dict[str, Any], **kwargs) -> str:
    archive_id = args.get("archive_id", "")
    offset = args.get("offset", 1)
    limit = args.get("limit")
    session_id = kwargs.get("session_id", "hermes-session")
    return context_retrieve(
        archive_id=archive_id,
        offset=offset,
        limit=limit,
        session_id=session_id,
    )


registry.register(
    name="context_retrieve",
    toolset="context",
    schema=CONTEXT_RETRIEVE_SCHEMA,
    handler=_handle_context_retrieve,
    check_fn=check_context_retrieve_requirements,
    emoji="📦",
    max_result_size_chars=100_000,
)
