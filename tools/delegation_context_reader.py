"""Reader and bounded pagination foundation for snapshot-scoped session search.

Provides safe, in-memory, literal search and complete paginated retrieval over
immutable ContextSnapshot records without touching SQLite session databases.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.delegation_context import ContextSnapshot, SnapshotRecord
from tools.registry import tool_error

logger = logging.getLogger("tools.delegation_context_reader")

MAX_QUERY_LENGTH = 1000
MAX_MAX_CHARS = 32000
DEFAULT_MAX_CHARS = 8000
MAX_WINDOW = 20
DEFAULT_WINDOW = 5
MAX_LIMIT = 20
DEFAULT_LIMIT = 3


def is_snapshot_session_id(session_id: Any) -> bool:
    """Return True if session_id belongs to the reserved snapshot namespace.

    Includes whitespace-wrapped reserved refs so they route to snapshot validation
    and explicit errors rather than falling back to SQLite SessionDB.
    """
    if not isinstance(session_id, str):
        return False
    s = session_id.strip()
    return s == "snapshot" or s.startswith("snapshot:")


def extract_snapshot_id(session_id: str) -> Optional[str]:
    """Extract requested snapshot ID from 'snapshot:<snapshot_id>' or 'snapshot' alias.

    Uses exact identifier equality without stripping or silently repairing IDs.
    """
    if not isinstance(session_id, str):
        return None
    if session_id == "snapshot":
        return "snapshot"
    if session_id.startswith("snapshot:"):
        req_id = session_id[len("snapshot:"):]
        return req_id if req_id else None
    return None


def validate_snapshot_route(
    session_id: str,
    snapshot: Optional[Any],
    *,
    profile: Optional[str] = None,
    sort: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    exclude_session_ids: Optional[Any] = None,
) -> Tuple[Optional[ContextSnapshot], Optional[str]]:
    """Validate snapshot session request. Fails closed before any DB access.

    Returns (validated_snapshot, error_message).
    """
    # Reject unsupported non-null / malformed options cleanly
    if profile is not None:
        if not isinstance(profile, str) or profile.strip():
            return None, "profile parameter is not supported for snapshot sessions"

    if sort is not None:
        if not isinstance(sort, str) or sort.strip():
            return None, "sort parameter is not supported for snapshot sessions"

    if after is not None:
        if not isinstance(after, str) or after.strip():
            return None, "after parameter is not supported for snapshot sessions"

    if before is not None:
        if not isinstance(before, str) or before.strip():
            return None, "before parameter is not supported for snapshot sessions"

    if exclude_session_ids is not None:
        if not isinstance(exclude_session_ids, (list, tuple, set)) or len(exclude_session_ids) > 0:
            return None, "exclude_session_ids parameter is not supported for snapshot sessions"

    if not isinstance(session_id, str):
        return None, "session_id must be a string"

    # Whitespace-wrapped reserved refs route here; reject with explicit error
    if session_id != session_id.strip():
        return None, "Snapshot session_id must not contain leading or trailing whitespace"

    req_id = extract_snapshot_id(session_id)
    if not req_id:
        return None, "Snapshot session_id must specify an ID in the format 'snapshot:<snapshot_id>' or 'snapshot'"

    if snapshot is None:
        target = "attached snapshot" if session_id == "snapshot" else f"requested: '{req_id[:64]}'"
        return None, f"No inherited snapshot is attached to this agent ({target})"

    if not isinstance(snapshot, ContextSnapshot):
        return None, "Attached snapshot must be a valid ContextSnapshot instance"

    manifest = getattr(snapshot, "manifest", None)
    attached_id = getattr(manifest, "snapshot_id", None) if manifest else None
    if not attached_id or not isinstance(attached_id, str):
        return None, "Attached snapshot is missing a valid manifest snapshot_id"

    records = getattr(snapshot, "records", None)
    if not isinstance(records, tuple):
        return None, "Attached snapshot records must be a tuple"

    # Exact identifier equality: session_id='snapshot' selects own attached snapshot;
    # explicit 'snapshot:<id>' must match attached snapshot_id exactly.
    if session_id != "snapshot" and req_id != attached_id:
        return None, f"Snapshot ID '{req_id[:64]}' does not match the attached snapshot ID"

    return snapshot, None


def _validate_numeric_and_type_args(
    content_offset: Optional[Any],
    max_chars: Optional[Any],
    around_message_id: Optional[Any],
    window: Optional[Any],
    limit: Optional[Any],
    role_filter: Optional[Any],
    query: Optional[Any],
    start_message_id: Optional[Any] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Strict type and bound validation. Requires actual ints (not bools/floats/strings)."""
    # content_offset
    offset_val = 0
    if content_offset is not None:
        if isinstance(content_offset, bool) or not isinstance(content_offset, int):
            msg = "content_offset must be an integer, not boolean" if isinstance(content_offset, bool) else "content_offset must be an integer"
            return None, msg
        if content_offset < 0:
            return None, "content_offset must be greater than or equal to 0"
        offset_val = content_offset

    # max_chars
    max_chars_val = DEFAULT_MAX_CHARS
    if max_chars is not None:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            msg = "max_chars must be an integer, not boolean" if isinstance(max_chars, bool) else "max_chars must be an integer"
            return None, msg
        if max_chars <= 0:
            return None, "max_chars must be a positive integer"
        max_chars_val = min(MAX_MAX_CHARS, max(1, max_chars))

    # around_message_id
    anchor_id: Optional[int] = None
    if around_message_id is not None:
        if isinstance(around_message_id, bool) or not isinstance(around_message_id, int):
            msg = "around_message_id must be an integer, not boolean" if isinstance(around_message_id, bool) else "around_message_id must be an integer"
            return None, msg
        if around_message_id < 1:
            return None, "around_message_id must be greater than or equal to 1"
        anchor_id = around_message_id

    # window
    window_val = DEFAULT_WINDOW
    if window is not None:
        if isinstance(window, bool) or not isinstance(window, int):
            msg = "window must be an integer, not boolean" if isinstance(window, bool) else "window must be an integer"
            return None, msg
        if window < 0:
            return None, "window must be greater than or equal to 0"
        window_val = min(MAX_WINDOW, window)

    # limit
    limit_val = DEFAULT_LIMIT
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            msg = "limit must be an integer, not boolean" if isinstance(limit, bool) else "limit must be an integer"
            return None, msg
        if limit < 1:
            return None, "limit must be greater than or equal to 1"
        limit_val = min(MAX_LIMIT, limit)

    # role_filter
    allowed_roles: Optional[Set[str]] = None
    if role_filter is not None:
        if not isinstance(role_filter, str):
            return None, "role_filter must be a string"
        allowed_roles = {r.strip().lower() for r in role_filter.split(",") if r.strip()}

    # query
    query_str = ""
    if query is not None:
        if not isinstance(query, str):
            return None, "query must be a string"
        if len(query) > MAX_QUERY_LENGTH:
            return None, f"query exceeds maximum length of {MAX_QUERY_LENGTH} characters"
        query_str = query

    if start_message_id is not None:
        if type(start_message_id) is not int or start_message_id < 1:
            return None, "start_message_id must be a positive integer"
        if anchor_id is not None or query_str:
            return None, "start_message_id cannot be combined with around_message_id or query"
    if offset_val > 0 and query_str:
        return None, "content_offset is not valid for search"
    # Sequential pages carry their own start record; exact record reads use window=0.
    if offset_val > 0 and start_message_id is None:
        if anchor_id is None or window_val != 0:
            return None, "content_offset > 0 is only allowed when window=0 for single-record retrieval"

    parsed = {
        "offset_val": offset_val,
        "max_chars_val": max_chars_val,
        "anchor_id": anchor_id,
        "window_val": window_val,
        "limit_val": limit_val,
        "allowed_roles": allowed_roles,
        "query_str": query_str,
        "start_id": start_message_id or 1,
    }
    return parsed, None


def _format_record_entry(
    rec: SnapshotRecord,
    is_anchor: bool,
    offset: int,
    chunk: str,
    total_chars: int,
    truncated: bool,
    next_offset: Optional[int] = None,
) -> Dict[str, Any]:
    """Format a message entry consistently across modes."""
    entry: Dict[str, Any] = {
        "id": rec.record_id,
        "role": rec.role,
        "content": chunk,
        "anchor": is_anchor,
        "content_offset": offset,
        "content_length": len(chunk),
        "total_chars": total_chars,
        "truncated": truncated,
    }
    if rec.tool_name:
        entry["tool_name"] = rec.tool_name
    if rec.tool_call_id:
        entry["tool_call_id"] = rec.tool_call_id
    if next_offset is not None:
        entry["next_content_offset"] = next_offset
    return entry


def _extract_snippet(text: str, start: int, end: int, max_window: int = 40) -> Tuple[str, int]:
    """Extract snippet surrounding a match in original text, returning (snippet, snip_start)."""
    rec_len = len(text)
    snip_start = max(0, start - max_window)
    snip_end = min(rec_len, end + max_window)
    return text[snip_start:snip_end], snip_start


def _paginate_record(
    snap: ContextSnapshot,
    session_id: str,
    anchor_id: int,
    offset_val: int,
    max_chars_val: int,
    allowed_roles: Optional[Set[str]],
    role_filter: Optional[str],
) -> str:
    """Mode 1: Exact single-record retrieval (window=0)."""
    records = snap.records
    total_records = len(records)
    manifest = snap.manifest

    if anchor_id > total_records or anchor_id < 1:
        return tool_error(
            f"Record ID {anchor_id} not found in snapshot (contains {total_records} records, IDs 1..{total_records})",
            success=False,
        )
    rec = records[anchor_id - 1]
    if allowed_roles and rec.role.lower() not in allowed_roles:
        return tool_error(
            f"Record ID {anchor_id} has role '{rec.role}', which does not match role_filter '{role_filter}'",
            success=False,
        )

    text_total = len(rec.text)
    if offset_val > text_total:
        return tool_error(
            f"content_offset {offset_val} exceeds message length {text_total}",
            success=False,
        )

    chunk = rec.text[offset_val : offset_val + max_chars_val]
    truncated = (offset_val + len(chunk) < text_total)
    next_offset = (offset_val + len(chunk)) if truncated else None

    msg_entry = _format_record_entry(
        rec,
        is_anchor=True,
        offset=offset_val,
        chunk=chunk,
        total_chars=text_total,
        truncated=truncated,
        next_offset=next_offset,
    )

    payload: Dict[str, Any] = {
        "success": True,
        "mode": "snapshot_record",
        "session_id": session_id,
        "snapshot_id": manifest.snapshot_id,
        "snapshot_digest": f"sha256:{manifest.content_hash_sha256[:16]}",
        "source": manifest.source_type,
        "around_message_id": rec.record_id,
        "window": 0,
        "content_char_budget": max_chars_val,
        "content_chars_returned": len(chunk),
        "messages": [msg_entry],
        "total_records": total_records,
        "has_more": truncated,
    }
    if truncated and next_offset is not None:
        payload["next_around_message_id"] = rec.record_id
        payload["next_content_offset"] = next_offset
        payload["next_window"] = 0
        payload["next_call"] = {
            "session_id": session_id,
            "around_message_id": rec.record_id,
            "window": 0,
            "content_offset": next_offset,
        }
    return json.dumps(payload, ensure_ascii=False)


def _paginate_scroll(
    snap: ContextSnapshot,
    session_id: str,
    anchor_id: int,
    window_val: int,
    max_chars_val: int,
    allowed_roles: Optional[Set[str]],
) -> str:
    """Mode 2: Window / Scroll retrieval (around_message_id set, window > 0, no query)."""
    records = snap.records
    total_records = len(records)
    manifest = snap.manifest

    if anchor_id > total_records or anchor_id < 1:
        return tool_error(
            f"around_message_id {anchor_id} not in snapshot (valid range: 1..{total_records})",
            success=False,
        )

    start_idx = max(1, anchor_id - window_val)
    end_idx = min(total_records, anchor_id + window_val)
    messages_before = start_idx - 1
    messages_after = total_records - end_idx

    candidates = [records[i - 1] for i in range(start_idx, end_idx + 1)]
    if allowed_roles:
        candidates = [r for r in candidates if r.role.lower() in allowed_roles]

    returned_messages: List[Dict[str, Any]] = []
    budget_remaining = max_chars_val
    truncated_record = False
    next_offset_for_last: Optional[int] = None
    last_rec_id: Optional[int] = None
    unconsumed_idx = len(candidates)

    for i, rec in enumerate(candidates):
        rec_len = len(rec.text)
        is_anchor = (rec.record_id == anchor_id)

        # Exact zero budget: do not add empty chunk or advance past unconsumed records
        if budget_remaining <= 0:
            unconsumed_idx = i
            break

        if rec_len <= budget_remaining:
            entry = _format_record_entry(
                rec,
                is_anchor=is_anchor,
                offset=0,
                chunk=rec.text,
                total_chars=rec_len,
                truncated=False,
            )
            returned_messages.append(entry)
            budget_remaining -= rec_len
            last_rec_id = rec.record_id
        else:
            chunk = rec.text[:budget_remaining]
            trunc = (len(chunk) < rec_len)
            next_off = len(chunk) if trunc else None
            entry = _format_record_entry(
                rec,
                is_anchor=is_anchor,
                offset=0,
                chunk=chunk,
                total_chars=rec_len,
                truncated=trunc,
                next_offset=next_off,
            )
            returned_messages.append(entry)
            truncated_record = trunc
            last_rec_id = rec.record_id
            next_offset_for_last = next_off
            budget_remaining = 0
            unconsumed_idx = i + 1
            break

    # Determine continuation cleanly
    has_more = False
    next_anchor: Optional[int] = None
    next_off: Optional[int] = None
    next_win: Optional[int] = None

    if truncated_record and next_offset_for_last is not None:
        has_more = True
        next_anchor = last_rec_id
        next_off = next_offset_for_last
        next_win = 0  # content_offset > 0 requires window=0
    elif unconsumed_idx < len(candidates):
        has_more = True
        next_anchor = candidates[unconsumed_idx].record_id
        next_win = 0  # Sequential unreturned record retrieval
    elif end_idx < total_records:
        next_anchor = next((r.record_id for r in records[end_idx:]
                            if not allowed_roles or r.role.lower() in allowed_roles), None)
        has_more = next_anchor is not None
        next_win = 0

    content_chars_returned = sum(len(m["content"]) for m in returned_messages)
    payload: Dict[str, Any] = {
        "success": True,
        "mode": "snapshot_scroll",
        "session_id": session_id,
        "snapshot_id": manifest.snapshot_id,
        "snapshot_digest": f"sha256:{manifest.content_hash_sha256[:16]}",
        "source": manifest.source_type,
        "around_message_id": anchor_id,
        "window": window_val,
        "content_char_budget": max_chars_val,
        "content_chars_returned": content_chars_returned,
        "messages": returned_messages,
        "messages_before": messages_before,
        "messages_after": messages_after,
        "total_records": total_records,
        "has_more": has_more,
    }
    if next_anchor is not None:
        payload["next_around_message_id"] = next_anchor
    if next_off is not None:
        payload["next_content_offset"] = next_off
    if next_win is not None:
        payload["next_window"] = next_win
    if has_more and next_anchor is not None:
        call_params: Dict[str, Any] = {
            "session_id": session_id,
            "start_message_id": next_anchor,
        }
        if next_off is not None:
            call_params["content_offset"] = next_off
        payload["next_call"] = call_params

    return json.dumps(payload, ensure_ascii=False)


def _literal_match(text: str, folded_query: str) -> Optional[Tuple[int, int]]:
    """Literal Unicode caseless search with offsets in the original text."""
    if not folded_query:
        return None
    folded = text.casefold()
    start = folded.find(folded_query)
    if start < 0:
        return None
    end = start + len(folded_query)
    if len(folded) == len(text):
        return start, end
    # Only a matching record with expanding casefold characters needs remapping.
    position = 0
    original_start = None
    for index, char in enumerate(text):
        following = position + len(char.casefold())
        if original_start is None and following > start:
            original_start = index
        if following >= end:
            return original_start, index + 1
        position = following
    return None


def _paginate_search(
    snap: ContextSnapshot,
    session_id: str,
    query_str: str,
    anchor_id: Optional[int],
    limit_val: int,
    max_chars_val: int,
    allowed_roles: Optional[Set[str]],
) -> str:
    """Mode 3: Literal search with inclusive start cursor and aggregate snippet budgeting."""
    records = snap.records
    manifest = snap.manifest

    folded_query = query_str.casefold()
    all_matches: List[Tuple[SnapshotRecord, int, int]] = []
    for rec in records:
        if allowed_roles and rec.role.lower() not in allowed_roles:
            continue
        match = _literal_match(rec.text, folded_query)
        if match is not None:
            all_matches.append((rec, *match))

    total_matches = len(all_matches)

    # Inclusive search start cursor: query + around_message_id filters record_id >= anchor_id
    start_cursor = anchor_id if anchor_id is not None else 1
    matches_from_cursor = [m for m in all_matches if m[0].record_id >= start_cursor]

    results: List[Dict[str, Any]] = []
    budget_remaining = max_chars_val

    for rec, start, end in matches_from_cursor:
        if len(results) >= limit_val or budget_remaining <= 0:
            break

        snip_text, snip_start = _extract_snippet(rec.text, start, end, max_window=40)
        snip_len = len(snip_text)

        # Bound returned snippet text to aggregate content character budget
        if snip_len > budget_remaining:
            if not results and budget_remaining > 0:
                snip_start = start
                snip_text = rec.text[start:start + budget_remaining]
                snip_len = len(snip_text)
            else:
                break

        budget_remaining = max(0, budget_remaining - snip_len)
        entry: Dict[str, Any] = {
            "id": rec.record_id,
            "role": rec.role,
            "total_chars": len(rec.text),
            "match_position": start,
            "snippet_offset": snip_start,
            "snippet": snip_text,
            "snippet_truncated": snip_start > 0 or snip_start + len(snip_text) < len(rec.text),
        }
        if rec.tool_name:
            entry["tool_name"] = rec.tool_name
        if rec.tool_call_id:
            entry["tool_call_id"] = rec.tool_call_id
        results.append(entry)

    # Determine continuation across matches
    has_more = False
    next_anchor: Optional[int] = None
    if results:
        last_id = results[-1]["id"]
        remaining_matches = [m for m in all_matches if m[0].record_id > last_id]
        if remaining_matches:
            has_more = True
            next_anchor = remaining_matches[0][0].record_id
    elif matches_from_cursor:
        has_more = True
        next_anchor = matches_from_cursor[0][0].record_id

    content_chars_returned = sum(len(r["snippet"]) for r in results)
    payload: Dict[str, Any] = {
        "success": True,
        "mode": "snapshot_search",
        "session_id": session_id,
        "snapshot_id": manifest.snapshot_id,
        "snapshot_digest": f"sha256:{manifest.content_hash_sha256[:16]}",
        "source": manifest.source_type,
        "query": query_str,
        "total_matches": total_matches,
        "count": len(results),
        "content_char_budget": max_chars_val,
        "content_chars_returned": content_chars_returned,
        "results": results,
        "has_more": has_more,
    }
    if next_anchor is not None:
        payload["next_around_message_id"] = next_anchor
        payload["next_call"] = {
            "session_id": session_id,
            "query": query_str,
            "around_message_id": next_anchor,
            "limit": limit_val,
        }

    return json.dumps(payload, ensure_ascii=False)


def _paginate_browse(
    snap: ContextSnapshot, session_id: str, limit_val: int, max_chars_val: int,
    allowed_roles: Optional[Set[str]], start_id: int = 1, offset_val: int = 0,
) -> str:
    """Sequential pages retain a record+character cursor across partial records."""
    records = snap.records
    eligible = [r for r in records if not allowed_roles or r.role.lower() in allowed_roles]
    candidates = [r for r in eligible if r.record_id >= start_id]
    if offset_val and (not candidates or candidates[0].record_id != start_id):
        return tool_error("content_offset requires an existing matching start record", success=False)
    if candidates and candidates[0].record_id == start_id and offset_val > len(candidates[0].text):
        return tool_error("content_offset exceeds message length", success=False)
    messages = []
    remaining = max_chars_val
    cursor = None
    for i, rec in enumerate(candidates[:limit_val]):
        offset = offset_val if rec.record_id == start_id else 0
        if remaining <= 0:
            cursor = (rec.record_id, offset)
            break
        chunk = rec.text[offset:offset + remaining]
        partial = offset + len(chunk) < len(rec.text)
        next_offset = offset + len(chunk) if partial else None
        messages.append(_format_record_entry(rec, False, offset, chunk, len(rec.text), partial, next_offset))
        remaining -= len(chunk)
        if partial:
            cursor = (rec.record_id, next_offset)
            break
    else:
        if len(candidates) > len(messages):
            cursor = (candidates[len(messages)].record_id, 0)
    payload = {
        "success": True, "mode": "snapshot_browse", "session_id": session_id,
        "snapshot_id": snap.manifest.snapshot_id,
        "snapshot_digest": f"sha256:{snap.manifest.content_hash_sha256[:16]}",
        "source": snap.manifest.source_type, "total_records": len(records),
        "matching_records": len(eligible), "count": len(messages),
        "content_char_budget": max_chars_val,
        "content_chars_returned": sum(len(m["content"]) for m in messages),
        "messages": messages, "has_more": cursor is not None,
    }
    if cursor is not None:
        record_id, offset = cursor
        payload.update(next_around_message_id=record_id, next_window=0,
                       next_call={"session_id": session_id, "start_message_id": record_id,
                                  "content_offset": offset})
        if offset:
            payload["next_content_offset"] = offset
    return json.dumps(payload, ensure_ascii=False)


def dispatch_snapshot_search(
    snapshot: Optional[Any],
    session_id: str,
    *,
    query: str = "",
    around_message_id: Optional[int] = None,
    window: Optional[int] = 5,
    role_filter: Optional[str] = None,
    limit: Optional[int] = 3,
    content_offset: Optional[int] = None,
    max_chars: Optional[int] = None,
    sort: Optional[str] = None,
    profile: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    exclude_session_ids: Optional[Any] = None,
    start_message_id: Optional[int] = None,
) -> str:
    """Execute snapshot search, scroll, record retrieval, or browse.

    Invariants:
    - Never accesses SQLite session DB or external profiles.
    - Missing, forged, or sibling-foreign snapshot references fail closed.
    - Literal search only (never regex); search text is untrusted historical data.
    - Zero/positive integer validation; bool/float/string numeric inputs rejected.
    - window=0 allows single-record recovery; content_offset > 0 permitted only when window=0.
    - max_chars clamped to [1, 32000], default 8000.
    - query + around_message_id provides inclusive search cursor continuation.
    - next_call provides unambiguous next-step tool invocation parameters.
    """
    snap, err = validate_snapshot_route(
        session_id=session_id,
        snapshot=snapshot,
        profile=profile,
        sort=sort,
        after=after,
        before=before,
        exclude_session_ids=exclude_session_ids,
    )
    if err:
        return tool_error(err, success=False)

    parsed, val_err = _validate_numeric_and_type_args(
        content_offset=content_offset,
        max_chars=max_chars,
        around_message_id=around_message_id,
        window=window,
        limit=limit,
        role_filter=role_filter,
        query=query,
        start_message_id=start_message_id,
    )
    if val_err:
        return tool_error(val_err, success=False)

    assert parsed is not None
    offset_val = parsed["offset_val"]
    max_chars_val = parsed["max_chars_val"]
    anchor_id = parsed["anchor_id"]
    window_val = parsed["window_val"]
    limit_val = parsed["limit_val"]
    allowed_roles = parsed["allowed_roles"]
    query_str = parsed["query_str"]

    def finish(raw: str) -> str:
        payload = json.loads(raw)
        if isinstance(payload.get("next_call"), dict):
            payload["next_call"].update(max_chars=max_chars_val, limit=limit_val)
            if role_filter is not None:
                payload["next_call"]["role_filter"] = role_filter
        return json.dumps(payload, ensure_ascii=False)

    if query_str:
        return finish(_paginate_search(snap, session_id, query_str, anchor_id,
                                       limit_val, max_chars_val, allowed_roles))
    if anchor_id is not None and window_val == 0:
        return finish(_paginate_record(snap, session_id, anchor_id, offset_val,
                                       max_chars_val, allowed_roles, role_filter))
    if anchor_id is not None:
        return finish(_paginate_scroll(snap, session_id, anchor_id, window_val,
                                       max_chars_val, allowed_roles))
    return finish(_paginate_browse(snap, session_id, limit_val, max_chars_val,
                                   allowed_roles, parsed["start_id"], offset_val))
