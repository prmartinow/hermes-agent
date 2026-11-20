"""Canonical evidence digest and terminal outcomes for delegated worker transcripts."""

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def compute_active_transcript_digest(records: Iterable[Any]) -> str:
    """Compute canonical SHA-256 digest over active conversation messages or raw rows.

    Covers row ID, role, content, tool_call_id, tool_calls, tool_name, and api_content
    to detect any in-place tampering even when row count and max row ID remain unchanged.
    Contains no secrets or plaintext in metadata.
    """
    h = hashlib.sha256()
    for item in records:
        r = dict(item) if not isinstance(item, dict) else item
        row_id = r.get("id")
        role = r.get("role") or ""

        # Canonicalize content
        content = r.get("content")
        if isinstance(content, (dict, list)):
            content_str = json.dumps(content, sort_keys=True, separators=(",", ":"))
        elif content is None:
            content_str = ""
        else:
            content_str = str(content)

        tool_call_id = r.get("tool_call_id") or ""

        # Canonicalize tool_calls
        tool_calls = r.get("tool_calls")
        if isinstance(tool_calls, (dict, list)):
            tool_calls_str = json.dumps(tool_calls, sort_keys=True, separators=(",", ":"))
        elif isinstance(tool_calls, str):
            try:
                parsed = json.loads(tool_calls)
                tool_calls_str = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            except (RecursionError, ValueError):
                tool_calls_str = tool_calls
        elif tool_calls is None:
            tool_calls_str = ""
        else:
            tool_calls_str = str(tool_calls)

        tool_name = r.get("tool_name") or r.get("name") or ""

        # Canonicalize api_content
        api_content = r.get("api_content")
        if isinstance(api_content, (dict, list)):
            api_content_str = json.dumps(api_content, sort_keys=True, separators=(",", ":"))
        elif api_content is None:
            api_content_str = ""
        else:
            api_content_str = str(api_content)

        entry_bytes = json.dumps(
            [
                int(row_id) if row_id is not None and not isinstance(row_id, bool) else None,
                str(role),
                content_str,
                str(tool_call_id),
                tool_calls_str,
                str(tool_name),
                api_content_str,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        h.update(entry_bytes)
        h.update(b"\n")
    return h.hexdigest()


def is_terminal_outcome(status: Any, exit_reason: Any) -> bool:
    """Validate actual child-loop outcome combinations without coercion."""
    allowed = {
        "completed": {"completed", "max_iterations"},
        "failed": {"completed", "max_iterations", "error"},
        "interrupted": {"interrupted"},
    }
    return (isinstance(status, str) and isinstance(exit_reason, str)
            and exit_reason in allowed.get(status, set()))
