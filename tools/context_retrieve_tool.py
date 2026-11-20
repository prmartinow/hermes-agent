"""Read historic tool outputs referenced by archive ID in Hermes conversations."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agent.secret_scope import UnscopedSecretError, get_secret
from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error


def _db_path() -> Path:
    database = get_secret("AGENT_MEMORY_CONTEXT_DB_PATH", "")
    if database:
        return Path(database).absolute()
    traces = get_secret("AGENT_MEMORY_TRACES_PATH", "")
    directory = Path(traces) if traces else get_hermes_home() / "agent-memory" / "traces"
    return (directory / "context.db").absolute()


def context_retrieve(
    archive_id: str,
    offset: int | None = 1,
    limit: int | None = None,
) -> str:
    """Retrieve full raw output or sliced lines from context.db using archive_id."""
    if not archive_id or not isinstance(archive_id, str):
        return tool_error("archive_id is required and must be a valid string UUID.")

    try:
        with closing(sqlite3.connect(_db_path().as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT raw_content, tool_name FROM content_archives WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
    except (UnscopedSecretError, sqlite3.OperationalError):
        row = None

    if row is None:
        return tool_error(f"Archive ID '{archive_id}' not found in context database.")

    raw_content, tool_name = row

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

    if start_idx >= total_lines > 0:
        return f"[Archive {archive_id} ({tool_name}): offset {start_line} exceeds total lines ({total_lines})]"

    sliced_lines = lines[start_idx:end_idx]
    sliced_text = "\n".join(sliced_lines)

    # If full content was requested without slicing
    if start_idx == 0 and end_idx == total_lines:
        return raw_content

    # Sliced content with header
    header = f"[Archive {archive_id} ({tool_name}) - Lines {start_line} to {end_idx} of {total_lines} total lines]\n"
    return header + sliced_text


def check_context_retrieve_requirements() -> bool:
    """Offer legacy retrieval only to profiles with an archive on disk."""
    try:
        return _db_path().is_file()
    except UnscopedSecretError:
        return False


CONTEXT_RETRIEVE_SCHEMA = {
    "name": "context_retrieve",
    "description": (
        "Read an archived tool result or a range of its lines "
        "using an [ARCHIVE_ID: <uuid>] reference in an older conversation."
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


def _handle_context_retrieve(args: dict[str, Any], **kwargs) -> str:
    archive_id = args.get("archive_id", "")
    offset = args.get("offset", 1)
    limit = args.get("limit")
    return context_retrieve(archive_id=archive_id, offset=offset, limit=limit)


registry.register(
    name="context_retrieve",
    toolset="file",
    schema=CONTEXT_RETRIEVE_SCHEMA,
    handler=_handle_context_retrieve,
    check_fn=check_context_retrieve_requirements,
    emoji="📦",
    max_result_size_chars=100_000,
)
