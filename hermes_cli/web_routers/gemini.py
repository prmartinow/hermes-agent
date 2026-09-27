"""Gemini provider web API router.

Handles /api/gemini/quota-timeline, /api/gemini/session-histories, and /api/gemini/account-history.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

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
    offset: int = Query(0, ge=0),
    include_subagents: bool = Query(True),
    scope: str = Query("all"),
):
    from hermes_cli.auth import list_gemini_session_histories
    return list_gemini_session_histories(
        limit=limit,
        offset=offset,
        include_subagents=include_subagents,
        scope=scope,
    )


@router.get("/api/gemini/quota-timeline")
@router.get("/api/gemini/timeline")
@router.get("/api/sessions/quota-timeline")
def get_gemini_quota_timeline(
    timespan: str = Query("24h"),
    model_group: str = Query("gemini"),
):
    from hermes_cli.auth import get_gemini_quota_timeline as _get_timeline
    return _get_timeline(timespan=timespan, model_group=model_group)


@router.get("/api/codex/usage-history")
def get_codex_usage_history(
    days: int = Query(7, ge=1, le=30),
    profile: Optional[str] = None,
):
    """Retrieve sanitized and normalized Codex subscription telemetry."""
    from hermes_cli.codex_usage_dashboard import get_codex_usage_dashboard
    return get_codex_usage_dashboard(days=days, profile=profile)


@router.get("/api/codex/quota-timeline")
def get_codex_quota_timeline(
    days: int = Query(7, ge=1, le=30),
    profile: Optional[str] = None,
    account_id: Optional[str] = Query(None, max_length=128),
    window_id: str = Query(
        "primary",
        max_length=32,
        pattern=r"^(?i:primary|secondary|session|weekly|5h|7d|primary_window|secondary_window)$",
    ),
):
    """Retrieve persisted Codex quota observations, intervals, and matching activity checkpoint deltas."""
    from hermes_cli.codex_usage_dashboard import get_codex_quota_timeline as _get_timeline

    canonical_profile: Optional[str] = None
    if profile is not None and profile.strip():
        from hermes_cli.web_server_profiles import _resolve_profile_dir
        from hermes_cli import profiles as profiles_mod

        norm = profiles_mod.normalize_profile_name(profile)
        _resolve_profile_dir(norm)
        canonical_profile = norm

    try:
        return _get_timeline(
            days=days,
            profile=canonical_profile,
            account_id=account_id,
            window_id=window_id,
        )
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request parameters")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to retrieve Codex quota timeline: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to retrieve quota timeline")
