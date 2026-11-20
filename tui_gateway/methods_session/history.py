"""Session history and hydration snapshots."""

import contextlib
import threading
import time

from ..method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_HYDRATION_SNAPSHOTS: dict[str, dict] = {}
_HYDRATION_SNAPSHOT_LOCK = threading.Lock()
_HYDRATION_SNAPSHOT_TTL = 600.0


def _reap_stale_hydration_snapshots():
    now = time.time()
    stale = [k for k, v in _HYDRATION_SNAPSHOTS.items() if now - v.get("created_at", 0) > _HYDRATION_SNAPSHOT_TTL]
    for k in stale:
        _HYDRATION_SNAPSHOTS.pop(k, None)


def _history_snapshot_page(rid, params, session, stored_id) -> dict:
    snapshot_token = params.get("snapshot_token")
    cursor = params.get("cursor")
    limit_param = params.get("limit")
    with _HYDRATION_SNAPSHOT_LOCK:
        _reap_stale_hydration_snapshots()
        snap = _HYDRATION_SNAPSHOTS.get(snapshot_token)
    owner = (str(params.get("session_id") or ""), str(session.get("profile_home") or ""), stored_id)
    if not snap or snap.get("owner") != owner:
        return _err(rid, 4004, "hydration snapshot expired or not found")

    canonical_messages = snap["messages"]
    total = len(canonical_messages)
    cursor_val = max(0, int(cursor or 0))
    limit_val = min(max(int(limit_param or 100), 1), 500)

    page_slice = canonical_messages[cursor_val : cursor_val + limit_val]
    next_cursor = cursor_val + len(page_slice)
    has_more = next_cursor < total

    return _ok(
        rid,
        {
            "session_id": str(params.get("session_id") or ""),
            "snapshot_token": snapshot_token,
            "messages": page_slice,
            "cursor": cursor_val,
            "next_cursor": next_cursor,
            "total": total,
            "has_more": has_more,
            "count": len(page_slice),
        },
    )


def _read_history(session, stored_id, fallback=None):
    from persistence.conversation.messages import ConversationRequest, TranscriptView

    if stored_id:
        with _session_db(session) as db, _persistence_for(db) as store:
            if store is not None:
                with contextlib.suppress(Exception):
                    request = ConversationRequest(view=TranscriptView.DISPLAY, include_lineage=True, include_row_ids=True)
                    return [m.as_openai_message() for m in store.messages.read_conversation(stored_id, request)]
    return [] if fallback is None else fallback


def _history_tail(rid, params, session, stored_id) -> dict:
    tail_limit = params.get("tail_limit")
    history = _read_history(session, stored_id)
    canonical_messages = _history_to_messages(history, profile_home=session.get("profile_home"))
    try:
        tail_val = min(max(int(tail_limit or 100), 1), 500)
    except (TypeError, ValueError):
        tail_val = 100

    page_slice = canonical_messages[-tail_val:] if tail_val < len(canonical_messages) else canonical_messages
    return _ok(
        rid,
        {
            "session_id": str(params.get("session_id") or ""),
            "messages": page_slice,
            "total": len(canonical_messages),
            "count": len(page_slice),
        },
    )


def _history_snapshot_create(rid, params, session, stored_id) -> dict:
    cursor = params.get("cursor")
    limit_param = params.get("limit")
    import uuid
    history = _read_history(session, stored_id)
    canonical_messages = _history_to_messages(history, profile_home=session.get("profile_home"))
    token = f"snap-{uuid.uuid4().hex[:12]}"
    with _HYDRATION_SNAPSHOT_LOCK:
        _reap_stale_hydration_snapshots()
        _HYDRATION_SNAPSHOTS[token] = {
            "owner": (str(params.get("session_id") or ""), str(session.get("profile_home") or ""), stored_id),
            "session_id": str(params.get("session_id") or ""),
            "messages": canonical_messages,
            "created_at": time.time(),
        }

    cursor_val = max(0, int(cursor or 0))
    limit_val = min(max(int(limit_param or 100), 1), 500)
    page_slice = canonical_messages[cursor_val : cursor_val + limit_val]
    next_cursor = cursor_val + len(page_slice)
    has_more = next_cursor < len(canonical_messages)

    return _ok(
        rid,
        {
            "session_id": str(params.get("session_id") or ""),
            "snapshot_token": token,
            "messages": page_slice,
            "cursor": cursor_val,
            "next_cursor": next_cursor,
            "total": len(canonical_messages),
            "has_more": has_more,
            "count": len(page_slice),
        },
    )


def _history_complete(rid, params, session, stored_id) -> dict:
    history = _read_history(session, stored_id, list(session.get("history", [])))
    return _ok(rid, {"count": len(history), "messages": _history_to_messages(history, profile_home=session.get("profile_home"))})


def _history_after(rid, params, session, stored_id, messages_repo) -> dict:
    after_row_id = params.get("after_row_id")
    limit_param = params.get("limit")
    snapshot_max_row_id = params.get("snapshot_max_row_id")
    try:
        after_id_val = max(0, int(after_row_id))
    except (TypeError, ValueError):
        after_id_val = 0
    try:
        limit_val = min(max(int(limit_param or 100), 1), 500)
    except (TypeError, ValueError):
        limit_val = 100

    try:
        high_watermark = int(snapshot_max_row_id) if snapshot_max_row_id is not None else None
    except (TypeError, ValueError):
        high_watermark = 0
    page = messages_repo.read_hydration_page(stored_id, limit=limit_val, after_row_id=after_id_val,
                                              high_watermark=high_watermark)
    messages = _history_to_messages([m.as_openai_message() for m in page.messages],
                                   profile_home=session.get("profile_home"))
    snapshot_max_val, next_after_row_id = page.high_watermark, page.last_row_id
    has_more = next_after_row_id > after_id_val and next_after_row_id < snapshot_max_val

    return _ok(
        rid,
        {
            "session_id": str(params.get("session_id") or ""),
            "messages": messages,
            "after_row_id": after_id_val,
            "next_after_row_id": next_after_row_id,
            "snapshot_max_row_id": snapshot_max_val,
            "has_more": has_more,
            "count": len(messages),
        },
    )


def _history_before(rid, params, session, stored_id, messages_repo) -> dict:
    before_index = params.get("before_index")
    limit_param = params.get("limit")
    try:
        before_index_val = int(before_index or 0)
    except (TypeError, ValueError):
        before_index_val = 0
    try:
        limit_val = min(max(int(limit_param or 50), 1), 100)
    except (TypeError, ValueError):
        limit_val = 50

    offset = max(0, before_index_val - limit_val)
    fetch_count = before_index_val - offset

    page = messages_repo.read_hydration_page(stored_id, limit=max(fetch_count, 0), offset=offset)
    messages = _history_to_messages([m.as_openai_message() for m in page.messages],
                                   profile_home=session.get("profile_home"))

    return _ok(
        rid,
        {
            "session_id": str(params.get("session_id") or ""),
            "messages": messages,
            "start_index": offset,
            "end_index": before_index_val,
            "has_more_before": offset > 0,
            "count": len(messages),
        },
    )


@method("session.history")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    stored_id = _submit_row_target_key(session) or str(params.get("session_id") or "")
    if params.get("snapshot_token") is not None:
        return _history_snapshot_page(rid, params, session, stored_id)
    if params.get("tail_limit") is not None:
        return _history_tail(rid, params, session, stored_id)
    if params.get("cursor") is not None:
        return _history_snapshot_create(rid, params, session, stored_id)
    if all(params.get(key) is None for key in ("before_index", "limit", "after_row_id")):
        return _history_complete(rid, params, session, stored_id)
    with _session_db(session) as db, _persistence_for(db) as store:
        if store is None:
            return _err(rid, 5000, "database unavailable")
        page = _history_after if params.get("after_row_id") is not None else _history_before
        return page(rid, params, session, stored_id, store.messages)


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
