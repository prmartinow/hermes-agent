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
from enum import Enum


class GoogleSignatureKind(str, Enum):
    REAL = "real"
    BYPASS = "bypass"
    MISSING = "missing"


GOOGLE_SIGNATURE_BYPASS = "skip_thought_signature_validator"


def classify_google_signature(sig: Any) -> GoogleSignatureKind:
    """Classify a thought signature string into REAL, BYPASS, or MISSING.

    None, empty string, or whitespace-only is MISSING.
    The exact literal 'skip_thought_signature_validator' is BYPASS.
    Any other non-empty opaque string is REAL.
    """
    if not isinstance(sig, str):
        return GoogleSignatureKind.MISSING
    s = sig.strip()
    if not s:
        return GoogleSignatureKind.MISSING
    if s == GOOGLE_SIGNATURE_BYPASS:
        return GoogleSignatureKind.BYPASS
    return GoogleSignatureKind.REAL


def usable_google_native_carrier(
    message: Dict[str, Any],
    *,
    target_model: str,
) -> Optional[Dict[str, Any]]:
    """Determine whether an assistant message carries an applicable Google native carrier.

    Fails closed (returns None) unless:
    1. A carrier with type='google.native_assistant' and version=1 exists.
    2. content is a dict with role='model' and parts is a list of dicts.
    3. The carrier's source_model has thought_circulation_support == True.
    4. The target_model has thought_circulation_support == True.
    5. Semantic applicability guard passes:
       - Visible text in carrier parts (ignoring thought=True, functionCall, signature-only)
         matches message['content'].
       - Function calls in carrier parts match message['tool_calls'] in count, order,
         function name, deserialized JSON arguments, and non-empty IDs.
    """
    if not isinstance(message, dict):
        return None

    from agent.gemini_cloudcode_models import thought_circulation_support

    # Target model must support thought circulation
    if thought_circulation_support(target_model) is not True:
        return None

    raw_details = message.get("reasoning_details")
    carrier = find_native_assistant_detail(raw_details, GOOGLE_NATIVE_ASSISTANT_TYPE)
    if not carrier or not isinstance(carrier, dict):
        return None

    if carrier.get("version") != CURRENT_NATIVE_CARRIER_VERSION:
        return None

    source_model = carrier.get("source_model")
    if not source_model or thought_circulation_support(str(source_model)) is not True:
        return None

    content_obj = carrier.get("content")
    if not isinstance(content_obj, dict) or content_obj.get("role") != "model":
        return None

    parts = content_obj.get("parts")
    if not isinstance(parts, list):
        return None

    # Semantic applicability guard:
    # 1. Compare visible text
    carrier_visible_pieces = []
    carrier_fc_parts = []
    for p in parts:
        if not isinstance(p, dict):
            return None
        if "functionCall" in p:
            carrier_fc_parts.append(p["functionCall"])
            continue
        if p.get("thought") is True:
            continue
        text_val = p.get("text")
        if isinstance(text_val, str) and text_val:
            carrier_visible_pieces.append(text_val)

    carrier_visible_text = "".join(carrier_visible_pieces)
    msg_content = message.get("content")
    msg_visible_text = str(msg_content or "") if msg_content is not None else ""

    if carrier_visible_text.strip() != msg_visible_text.strip():
        # Visible content mismatch -> carrier is stale
        return None

    # 2. Compare tool calls
    msg_tool_calls = message.get("tool_calls") or []
    if not isinstance(msg_tool_calls, list):
        msg_tool_calls = []

    if len(carrier_fc_parts) != len(msg_tool_calls):
        return None

    for idx, (carrier_fc, generic_tc) in enumerate(zip(carrier_fc_parts, msg_tool_calls)):
        if not isinstance(carrier_fc, dict) or not isinstance(generic_tc, dict):
            return None

        # Compare function name
        c_name = str(carrier_fc.get("name") or "")
        g_fn = generic_tc.get("function") if isinstance(generic_tc.get("function"), dict) else {}
        g_name = str(g_fn.get("name") or generic_tc.get("name") or "")
        if c_name != g_name:
            return None

        # Compare arguments structurally (deserialized JSON)
        c_args = carrier_fc.get("args") or {}
        g_args_raw = g_fn.get("arguments") or generic_tc.get("arguments") or "{}"
        if isinstance(g_args_raw, str):
            try:
                g_args = json.loads(g_args_raw)
            except Exception:
                g_args = g_args_raw
        else:
            g_args = g_args_raw

        if c_args != g_args:
            return None

        # Compare non-empty IDs if both present
        c_id = str(carrier_fc.get("id") or "").strip()
        g_id = str(generic_tc.get("id") or generic_tc.get("call_id") or "").strip()
        if c_id and g_id and c_id != g_id:
            return None

    return carrier
