"""Gemini provider web API router.

Handles /api/gemini/quota-timeline, /api/gemini/session-histories, and /api/gemini/account-history.
"""

from typing import Optional
from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/api/gemini/account-history")
@router.get("/api/sessions/history")
async def get_gemini_account_history(
    session_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    scope: str = Query("all"),
):
    from hermes_cli.auth import list_account_events
    return list_account_events(session_id=session_id, limit=limit, offset=offset, scope=scope)


@router.get("/api/gemini/session-histories")
def get_gemini_session_histories(
    limit: int = Query(100, ge=1, le=500),
):
    from hermes_cli.auth import list_gemini_session_histories
    return list_gemini_session_histories(limit=limit)


@router.get("/api/gemini/quota-timeline")
@router.get("/api/gemini/timeline")
@router.get("/api/sessions/quota-timeline")
def get_gemini_quota_timeline(
    timespan: str = Query("24h"),
    model_group: str = Query("gemini"),
):
    from hermes_cli.auth import get_gemini_quota_timeline as _get_timeline
    return _get_timeline(timespan=timespan, model_group=model_group)
