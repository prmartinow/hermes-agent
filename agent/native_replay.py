"""Generic native assistant replay carrier helpers for multi-provider persistence and projection.

Providers that emit cryptographic signatures or ordered native part structures
(such as Google Gemini) attach a structured carrier dictionary inside the
message's ``reasoning_details`` list under ``type: "<provider>.native_assistant"``.

These helpers provide representation-safe operations across both in-memory
``list[dict]`` structures and SQLite-restored JSON text representations.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


GOOGLE_NATIVE_ASSISTANT_TYPE = "google.native_assistant"
CURRENT_NATIVE_CARRIER_VERSION = 1


def normalize_reasoning_details(value: Any) -> List[Dict[str, Any]]:
    """Normalize a reasoning_details value (list, None, or JSON string) to a list of dicts.

    SQLite readers return ``reasoning_details`` as a raw JSON string. This helper
    guarantees callers receive a mutable list of dictionaries without breaking
    non-dict elements.
    """
    if value is None:
        return []
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return []
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, list):
                return [d for d in parsed if isinstance(d, dict)]
            if isinstance(parsed, dict):
                return [parsed]
            return []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, list):
        return [dict(d) for d in value if isinstance(d, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    return []


def find_native_assistant_detail(
    details: Any,
    target_type: str = GOOGLE_NATIVE_ASSISTANT_TYPE,
) -> Optional[Dict[str, Any]]:
    """Find the first native assistant carrier matching target_type."""
    normalized = normalize_reasoning_details(details)
    for d in normalized:
        if d.get("type") == target_type:
            return d
    return None


def filter_native_assistant_details(
    details: Any,
    keep_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter reasoning_details, retaining non-native details plus native details matching keep_type.

    Any carrier whose type ends with ``.native_assistant`` is removed unless its
    type matches keep_type exactly.
    """
    normalized = normalize_reasoning_details(details)
    kept: List[Dict[str, Any]] = []
    for d in normalized:
        dtype = str(d.get("type") or "")
        if dtype.endswith(".native_assistant"):
            if keep_type and dtype == keep_type:
                kept.append(d)
        else:
            kept.append(d)
    return kept


def upsert_native_assistant_detail(
    details: Any,
    carrier: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Insert or update a native assistant carrier in reasoning_details by carrier type."""
    normalized = normalize_reasoning_details(details)
    carrier_type = carrier.get("type")
    if not carrier_type:
        raise ValueError("Native carrier dictionary must contain a non-empty 'type' key.")

    updated = False
    result: List[Dict[str, Any]] = []
    for d in normalized:
        if d.get("type") == carrier_type:
            result.append(dict(carrier))
            updated = True
        else:
            result.append(d)

    if not updated:
        result.append(dict(carrier))
    return result


def build_google_native_carrier(
    parts: List[Dict[str, Any]],
    source_model: str,
    role: str = "model",
    version: int = CURRENT_NATIVE_CARRIER_VERSION,
) -> Dict[str, Any]:
    """Construct an ordered google.native_assistant replay carrier dictionary."""
    return {
        "type": GOOGLE_NATIVE_ASSISTANT_TYPE,
        "version": version,
        "source_model": source_model,
        "content": {
            "role": role,
            "parts": [dict(p) for p in parts if isinstance(p, dict)],
        },
    }
