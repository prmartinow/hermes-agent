"""Generic native assistant replay carrier helpers for multi-provider persistence and projection.

Providers that emit cryptographic signatures or ordered native part structures
(such as Google Gemini) attach a structured carrier dictionary inside the
message's ``reasoning_details`` list under ``type: "<provider>.native_assistant"``.

These helpers provide representation-safe operations across both in-memory
``list`` structures and SQLite-restored JSON text representations.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional


GOOGLE_NATIVE_ASSISTANT_TYPE = "google.native_assistant"
CURRENT_NATIVE_CARRIER_VERSION = 1


def normalize_reasoning_details(value: Any) -> List[Any]:
    """Normalize a reasoning_details value (list, None, or JSON string) to a list.

    SQLite readers return ``reasoning_details`` as a raw JSON string. This helper
    guarantees callers receive a mutable list without dropping or altering unknown
    or non-dict provider entries.
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
                return copy.deepcopy(parsed)
            if isinstance(parsed, dict):
                return [copy.deepcopy(parsed)]
            return [parsed]
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, dict):
        return [copy.deepcopy(value)]
    return [copy.deepcopy(value)]


def find_native_assistant_detail(
    details: Any,
    target_type: str = GOOGLE_NATIVE_ASSISTANT_TYPE,
) -> Optional[Dict[str, Any]]:
    """Find the first native assistant carrier matching target_type."""
    normalized = normalize_reasoning_details(details)
    for d in normalized:
        if isinstance(d, dict) and d.get("type") == target_type:
            return d
    return None


def filter_native_assistant_details(
    details: Any,
    keep_type: Optional[str] = None,
) -> List[Any]:
    """Filter reasoning_details, retaining non-native entries plus native details matching keep_type.

    Only dictionary entries whose type ends with ``.native_assistant`` are evaluated;
    they are removed unless their type matches keep_type exactly. Non-dict entries
    and non-native dictionaries are preserved untouched.
    """
    normalized = normalize_reasoning_details(details)
    kept: List[Any] = []
    for d in normalized:
        if isinstance(d, dict):
            dtype = str(d.get("type") or "")
            if dtype.endswith(".native_assistant"):
                if keep_type and dtype == keep_type:
                    kept.append(d)
            else:
                kept.append(d)
        else:
            kept.append(d)
    return kept


def upsert_native_assistant_detail(
    details: Any,
    carrier: Dict[str, Any],
) -> List[Any]:
    """Insert or update a native assistant carrier in reasoning_details by carrier type."""
    normalized = normalize_reasoning_details(details)
    carrier_type = carrier.get("type")
    if not carrier_type:
        raise ValueError("Native carrier dictionary must contain a non-empty 'type' key.")

    updated = False
    result: List[Any] = []
    carrier_copy = copy.deepcopy(carrier)
    for d in normalized:
        if isinstance(d, dict) and d.get("type") == carrier_type:
            result.append(carrier_copy)
            updated = True
        else:
            result.append(d)

    if not updated:
        result.append(carrier_copy)
    return result


def build_google_native_carrier(
    parts: List[Dict[str, Any]],
    source_model: str,
    role: str = "model",
    version: int = CURRENT_NATIVE_CARRIER_VERSION,
) -> Dict[str, Any]:
    """Construct an ordered google.native_assistant replay carrier dictionary with a deep snapshot."""
    valid_parts = [copy.deepcopy(p) for p in parts if isinstance(p, dict)]
    return {
        "type": GOOGLE_NATIVE_ASSISTANT_TYPE,
        "version": version,
        "source_model": source_model,
        "content": {
            "role": role,
            "parts": valid_parts,
        },
    }
class GoogleNativeStreamAccumulator:
    """Accumulates streamed raw Gemini response parts into an ordered native assistant Content structure."""

    def __init__(self, role: str = "model"):
        self.role = role
        self.parts: List[Dict[str, Any]] = []
        self._slot_to_part_index: Dict[int, int] = {}
        self._call_id_to_part_index: Dict[str, int] = {}

    def observe_part(
        self,
        part: Dict[str, Any],
        *,
        function_slot: Optional[int] = None,
    ) -> None:
        """Observe one raw Gemini response Part dictionary from an SSE event."""
        if not isinstance(part, dict):
            return

        fc = part.get("functionCall")
        if isinstance(fc, dict):
            call_id = str(fc.get("id") or "")
            target_idx = None
            if call_id and call_id in self._call_id_to_part_index:
                target_idx = self._call_id_to_part_index[call_id]
            elif function_slot is not None and function_slot in self._slot_to_part_index:
                target_idx = self._slot_to_part_index[function_slot]

            if target_idx is not None:
                # Generic lossless merge into existing part for this logical function call
                existing = self.parts[target_idx]

                incoming_fc = part.get("functionCall")
                if isinstance(incoming_fc, dict):
                    existing_fc = existing.setdefault("functionCall", {})
                    for k, v in incoming_fc.items():
                        existing_fc[k] = copy.deepcopy(v)
                    if call_id:
                        self._call_id_to_part_index[call_id] = target_idx

                for k, v in part.items():
                    if k == "functionCall":
                        continue
                    if k in {"thoughtSignature", "thought_signature"}:
                        if v and not (existing.get("thoughtSignature") or existing.get("thought_signature")):
                            existing[k] = copy.deepcopy(v)
                        continue
                    existing[k] = copy.deepcopy(v)
                return

            # First time observing this function call
            part_copy = copy.deepcopy(part)
            new_idx = len(self.parts)
            self.parts.append(part_copy)
            if function_slot is not None:
                self._slot_to_part_index[function_slot] = new_idx
            if call_id:
                self._call_id_to_part_index[call_id] = new_idx
            return

        # Non-function-call part (text, thought, signature-only, unknown)
        self.parts.append(copy.deepcopy(part))

    def build_carrier(self, source_model: str) -> Optional[Dict[str, Any]]:
        """Construct the completed google.native_assistant replay carrier dictionary."""
        if not self.parts:
            return None
        return build_google_native_carrier(
            parts=self.parts,
            source_model=source_model,
            role=self.role,
        )
