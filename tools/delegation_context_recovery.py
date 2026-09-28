"""Opt-in same-session readable compaction recovery for delegation context snapshots."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor
from tools.delegation_context import (
    ContextRecoveryError,
    RequiredContextError,
)


@dataclass(frozen=True)
class RecoveryOutcome:
    """Outcome of attempting same-session compaction recovery at parent dispatch."""

    raw_history: List[Dict[str, Any]]
    source_type: str
    inherit_compacted_history: bool
    compaction_recovery_coverage: Optional[str]  # "available_readable" | "active_only" | "unavailable"
    available_archived_messages_count: int
    observed_db_row_watermark: Optional[int]


def _is_recognized_compaction_summary(msg: Dict[str, Any]) -> bool:
    """Return True if *msg* carries a recognized canonical compaction summary/marker.

    Restricted strictly to user and assistant roles; tool results and generic prefixes
    cannot authorize restoring archives.
    """
    if not isinstance(msg, dict):
        return False
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return False

    if bool(msg.get("_compressed_summary")):
        return True
    if msg.get("display_kind") == "compaction":
        return True

    if ContextCompressor._is_context_summary_message(msg):
        return True
    if ContextCompressor.classify_summary_content(msg.get("content")) is not None:
        return True

    return False


def _content_eq(c1: Any, c2: Any) -> bool:
    """Compare message content, treating None and empty string as equivalent."""
    if c1 == c2:
        return True
    if (c1 is None or c1 == "") and (c2 is None or c2 == ""):
        return True
    return False


def _validate_and_normalize_tool_calls(tcs: Any, label: str, turn: int) -> List[Dict[str, Any]]:
    """Validate tool_calls structure without lossy coercion, failing closed on invalid forms."""
    if tcs is None or tcs == []:
        return []
    if not isinstance(tcs, list):
        raise ContextRecoveryError(
            f"Context recovery failed: active generation mismatch at turn {turn}: "
            f"tool_calls in {label} must be a list or None, got {type(tcs).__name__}."
        )
    norm = []
    for tc_idx, tc in enumerate(tcs):
        if not isinstance(tc, dict):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {turn}: "
                f"tool_call #{tc_idx} in {label} must be a dict, got {type(tc).__name__}."
            )
        cid = tc.get("id")
        if cid is not None and not isinstance(cid, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {turn}: "
                f"tool_call id in {label} must be str or None, got {type(cid).__name__}."
            )
        ttype = tc.get("type")
        if ttype is not None and not isinstance(ttype, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {turn}: "
                f"tool_call type in {label} must be str or None, got {type(ttype).__name__}."
            )
        fn = tc.get("function")
        if fn is not None:
            if not isinstance(fn, dict):
                raise ContextRecoveryError(
                    f"Context recovery failed: active generation mismatch at turn {turn}: "
                    f"tool_call function in {label} must be a dict, got {type(fn).__name__}."
                )
            fname = fn.get("name")
            fargs = fn.get("arguments")
        else:
            fname = tc.get("name")
            fargs = tc.get("arguments")

        if fname is not None and not isinstance(fname, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {turn}: "
                f"tool_call function name in {label} must be str or None, got {type(fname).__name__}."
            )
        if fargs is not None and not isinstance(fargs, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {turn}: "
                f"tool_call function arguments in {label} must be str or None, got {type(fargs).__name__}."
            )
        norm.append({
            "id": cid,
            "type": ttype or "function",
            "name": fname,
            "arguments": fargs,
        })
    return norm


def recover_parent_messages_with_compaction(parent_agent: Any) -> RecoveryOutcome:
    """Recover and stitch locally archived conversation turns before active generation.

    Guards:
    - Capture session_id, attached DB, and chosen source reference before copy/query.
    - Deepcopy live messages before query and verify exact session_id, DB identity,
      source precedence, and deep equality afterward (detects in-place nested edits).
    - Empty authoritative live [] stays empty without querying DB.
    - Uses only active messages returned by the same single-statement
      get_compaction_recovery_messages query when live and conversation_history are absent.
    - Summary recognition restricted strictly to user/assistant roles using canonical helpers.
    - Verifies active generation prefix alignment without lossy coercion (fails closed).
    - Preserves unpersisted live tail when stitching archives.
    - Retains derivative context summaries; honest coverage receipts.
    """
    if parent_agent is None:
        raise RequiredContextError("Parent agent is None; cannot inherit context.")

    # 1. Capture session_id, attached DB, and determine source reference before copy/query
    captured_session_id = getattr(parent_agent, "session_id", None)
    captured_session_db = getattr(parent_agent, "_session_db", None)

    chosen_source: str
    source_ref: Any
    source_type: str

    if hasattr(parent_agent, "_session_messages") and getattr(parent_agent, "_session_messages") is not None:
        chosen_source = "_session_messages"
        source_ref = getattr(parent_agent, "_session_messages")
        source_type = "live_session_messages"
    elif hasattr(parent_agent, "conversation_history") and getattr(parent_agent, "conversation_history") is not None:
        chosen_source = "conversation_history"
        source_ref = getattr(parent_agent, "conversation_history")
        source_type = "conversation_history"
    else:
        chosen_source = "session_db"
        source_ref = None
        source_type = "session_db"

    detached_capture: Optional[List[Dict[str, Any]]] = None
    if chosen_source in ("_session_messages", "conversation_history"):
        if not isinstance(source_ref, list):
            raise RequiredContextError(
                f"Parent agent {chosen_source} is not a list (got {type(source_ref).__name__}); fails closed."
            )
        detached_capture = copy.deepcopy(source_ref)

        # Empty authoritative live [] stays empty; no queries made
        if len(detached_capture) == 0:
            return RecoveryOutcome(
                raw_history=[],
                source_type=source_type,
                inherit_compacted_history=True,
                compaction_recovery_coverage="unavailable",
                available_archived_messages_count=0,
                observed_db_row_watermark=None,
            )

    # 2. Check DB availability
    db_available = (
        captured_session_db is not None
        and bool(captured_session_id)
        and isinstance(captured_session_id, str)
    )

    if not db_available:
        if detached_capture is not None:
            # Live context available, but no DB: retain active context with unavailable coverage
            return RecoveryOutcome(
                raw_history=detached_capture,
                source_type=source_type,
                inherit_compacted_history=True,
                compaction_recovery_coverage="unavailable",
                available_archived_messages_count=0,
                observed_db_row_watermark=None,
            )
        # Neither live messages nor DB available: fails closed
        raise RequiredContextError(
            "Parent agent has no accessible conversation context (no live _session_messages, "
            "conversation_history, or session_db); fails closed."
        )

    # 3. Single-statement atomic read from SessionDB
    try:
        db_data = captured_session_db.get_compaction_recovery_messages(captured_session_id)
    except Exception as exc:
        raise ContextRecoveryError(f"Context recovery failed while querying session database: {exc}") from exc

    # 4. Post-query race & concurrency checks
    current_session_id = getattr(parent_agent, "session_id", None)
    if current_session_id != captured_session_id:
        raise ContextRecoveryError(
            f"Context recovery failed: parent session_id changed concurrently during capture "
            f"({captured_session_id!r} -> {current_session_id!r})."
        )

    current_session_db = getattr(parent_agent, "_session_db", None)
    if current_session_db is not captured_session_db:
        raise ContextRecoveryError(
            "Context recovery failed: parent _session_db changed concurrently during capture (race detected)."
        )

    if chosen_source == "_session_messages":
        current_source_ref = getattr(parent_agent, "_session_messages", None)
        if current_source_ref is not source_ref or source_ref != detached_capture:
            raise ContextRecoveryError(
                "Context recovery failed: parent live messages modified concurrently during capture (race detected)."
            )
        live_messages = detached_capture
    elif chosen_source == "conversation_history":
        if getattr(parent_agent, "_session_messages", None) is not None:
            raise ContextRecoveryError(
                "Context recovery failed: parent live source precedence changed concurrently during capture (race detected)."
            )
        current_source_ref = getattr(parent_agent, "conversation_history", None)
        if current_source_ref is not source_ref or source_ref != detached_capture:
            raise ContextRecoveryError(
                "Context recovery failed: parent live messages modified concurrently during capture (race detected)."
            )
        live_messages = detached_capture
    else:  # chosen_source == "session_db"
        if (
            getattr(parent_agent, "_session_messages", None) is not None
            or getattr(parent_agent, "conversation_history", None) is not None
        ):
            raise ContextRecoveryError(
                "Context recovery failed: parent live source precedence changed concurrently during capture (race detected)."
            )
        live_messages = copy.deepcopy(db_data.get("active_messages", []))
        if not live_messages:
            return RecoveryOutcome(
                raw_history=[],
                source_type=source_type,
                inherit_compacted_history=True,
                compaction_recovery_coverage="unavailable",
                available_archived_messages_count=0,
                observed_db_row_watermark=db_data.get("observed_watermark"),
            )

    archived_messages = db_data.get("archived_messages", [])
    active_messages = db_data.get("active_messages", [])
    observed_watermark = db_data.get("observed_watermark")

    # Case A: No archives in DB (session never compacted)
    if not archived_messages:
        return RecoveryOutcome(
            raw_history=live_messages,
            source_type=source_type,
            inherit_compacted_history=True,
            compaction_recovery_coverage="active_only",
            available_archived_messages_count=0,
            observed_db_row_watermark=observed_watermark,
        )

    # Case B: Archive rows exist, but visible live generation has NO recognized compaction summary marker
    has_marker = any(_is_recognized_compaction_summary(m) for m in live_messages)
    if not has_marker:
        return RecoveryOutcome(
            raw_history=live_messages,
            source_type=source_type,
            inherit_compacted_history=True,
            compaction_recovery_coverage="active_only",
            available_archived_messages_count=len(archived_messages),
            observed_db_row_watermark=observed_watermark,
        )

    # Case C: Visible marker present -> Verify active generation prefix alignment
    if not active_messages:
        raise ContextRecoveryError(
            "Context recovery failed: active generation is absent in session database (cannot verify alignment)."
        )

    if len(active_messages) > len(live_messages):
        raise ContextRecoveryError(
            f"Context recovery failed: active generation in database has {len(active_messages)} messages, "
            f"which exceeds live messages count {len(live_messages)}."
        )

    for i, db_msg in enumerate(active_messages):
        live_msg = live_messages[i]

        # 1. Compare role
        if db_msg.get("role") != live_msg.get("role"):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"role {db_msg.get('role')!r} != {live_msg.get('role')!r}."
            )

        # 2. Compare tool_call_id (no lossy type coercion)
        db_tcid = db_msg.get("tool_call_id")
        live_tcid = live_msg.get("tool_call_id")
        if db_tcid is not None and not isinstance(db_tcid, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool_call_id must be str or None, got {type(db_tcid).__name__} in db message."
            )
        if live_tcid is not None and not isinstance(live_tcid, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool_call_id must be str or None, got {type(live_tcid).__name__} in live message."
            )
        if db_tcid != live_tcid:
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool_call_id {db_tcid!r} != {live_tcid!r}."
            )

        # 3. Compare tool_name / name (known DBname/tool_name alias)
        db_name = db_msg.get("tool_name") if db_msg.get("tool_name") is not None else db_msg.get("name")
        live_name = live_msg.get("name") if live_msg.get("name") is not None else live_msg.get("tool_name")
        if db_name is not None and not isinstance(db_name, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool name must be str or None, got {type(db_name).__name__} in db message."
            )
        if live_name is not None and not isinstance(live_name, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool name must be str or None, got {type(live_name).__name__} in live message."
            )
        if db_name != live_name:
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"tool name {db_name!r} != {live_name!r}."
            )

        # 4. Compare api_content
        db_ac = db_msg.get("api_content")
        live_ac = live_msg.get("api_content")
        if db_ac is not None and not isinstance(db_ac, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"api_content must be str or None, got {type(db_ac).__name__} in db message."
            )
        if live_ac is not None and not isinstance(live_ac, str):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: "
                f"api_content must be str or None, got {type(live_ac).__name__} in live message."
            )
        if (db_ac or None) != (live_ac or None):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: api_content differs."
            )

        # 5. Compare content (legitimate None/empty content equivalence)
        if not _content_eq(db_msg.get("content"), live_msg.get("content")):
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: content differs."
            )

        # 6. Compare tool_calls (strict validation without lossy coercion)
        db_norm_tcs = _validate_and_normalize_tool_calls(db_msg.get("tool_calls"), "db message", i)
        live_norm_tcs = _validate_and_normalize_tool_calls(live_msg.get("tool_calls"), "live message", i)
        if db_norm_tcs != live_norm_tcs:
            raise ContextRecoveryError(
                f"Context recovery failed: active generation mismatch at turn {i}: tool_calls differ."
            )

    # Alignment passed! Stitch available archive rows in id order BEFORE live generation
    stitched_history = list(archived_messages) + list(live_messages)
    return RecoveryOutcome(
        raw_history=stitched_history,
        source_type=source_type,
        inherit_compacted_history=True,
        compaction_recovery_coverage="available_readable",
        available_archived_messages_count=len(archived_messages),
        observed_db_row_watermark=observed_watermark,
    )
