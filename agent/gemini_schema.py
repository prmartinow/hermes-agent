"""Helpers for translating OpenAI-style tool schemas to Gemini's schema subset."""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

# Gemini's ``FunctionDeclaration.parameters`` field accepts the ``Schema``
# object, which is only a subset of OpenAPI 3.0 / JSON Schema. Strip fields
# outside that subset before sending Hermes tool schemas to Google.
_GEMINI_SCHEMA_ALLOWED_KEYS = {
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "maxItems",
    "minItems",
    "properties",
    "required",
    "minProperties",
    "maxProperties",
    "minLength",
    "maxLength",
    "pattern",
    "example",
    "anyOf",
    "propertyOrdering",
    "default",
    "items",
    "minimum",
    "maximum",
}

# Canonical key order for Gemini Schema objects to enforce deterministic,
# byte-stable JSON serialization across conversation turns.
_CANONICAL_SCHEMA_KEY_ORDER = (
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "properties",
    "required",
    "propertyOrdering",
    "items",
    "anyOf",
    "pattern",
    "example",
    "default",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minProperties",
    "maxProperties",
)

# Google Grounding & Search constants
GOOGLE_SEARCH_TOOL: Dict[str, Any] = {"googleSearch": {}}
GOOGLE_SEARCH_TOOL_NAME: str = "google_search"


def _order_dict_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    """Order dictionary keys canonically for byte-stable JSON serialization."""
    ordered: Dict[str, Any] = {}
    for k in _CANONICAL_SCHEMA_KEY_ORDER:
        if k in d:
            ordered[k] = d[k]
    for k in sorted(d.keys()):
        if k not in ordered:
            ordered[k] = d[k]
    return ordered


def _stringify_enum_value(item: Any) -> Any:
    """Gemini-safe string for a scalar enum entry, or None to drop it."""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, (int, float)) and math.isfinite(item):
        return str(item)
    return item if isinstance(item, str) else None


def sanitize_gemini_schema(schema: Any) -> Dict[str, Any]:
    """Return a Gemini-compatible copy of a tool parameter schema.

    Hermes tool schemas are OpenAI-flavored JSON Schema and may contain keys
    such as ``$schema`` or ``additionalProperties`` that Google's Gemini
    ``Schema`` object rejects. This helper preserves the documented Gemini
    subset and recursively sanitizes nested ``properties`` / ``items`` /
    ``anyOf`` definitions, enforcing byte-stable key ordering and consistent
    type representations.
    """
    if not isinstance(schema, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_ALLOWED_KEYS:
            continue
        if key == "type":
            if isinstance(value, str):
                cleaned[key] = value.lower()
            elif isinstance(value, list):
                # OpenAPI 3.1 multi-type e.g. ["string", "null"]
                non_null_types = [
                    t.lower() for t in value
                    if isinstance(t, str) and t.lower() != "null"
                ]
                if any(isinstance(t, str) and t.lower() == "null" for t in value):
                    cleaned["nullable"] = True
                if non_null_types:
                    cleaned[key] = non_null_types[0]
            else:
                cleaned[key] = str(value).lower()
            continue
        if key == "properties":
            if not isinstance(value, dict):
                continue
            props: Dict[str, Any] = {}
            for prop_name in sorted(value.keys()):
                if not isinstance(prop_name, str):
                    continue
                props[prop_name] = sanitize_gemini_schema(value[prop_name])
            cleaned[key] = props
            continue
        if key == "items":
            cleaned[key] = sanitize_gemini_schema(value)
            continue
        if key == "anyOf":
            if not isinstance(value, list):
                continue
            cleaned[key] = [
                sanitize_gemini_schema(item)
                for item in value
                if isinstance(item, dict)
            ]
            continue
        if key == "propertyOrdering":
            if not isinstance(value, list):
                continue
            cleaned[key] = [
                item for item in value
                if isinstance(item, str)
            ]
            continue
        cleaned[key] = value

    # Gemini requires every ``enum`` entry to be a string even for
    # integer/number/boolean types; the declared type stays intact and Gemini
    # still emits typed tool arguments at runtime. dict.fromkeys = ordered dedupe.
    enum_val = cleaned.get("enum")
    if isinstance(enum_val, list) and cleaned.get("type") in {"integer", "number", "boolean"}:
        if stringified := list(dict.fromkeys(v for v in map(_stringify_enum_value, enum_val) if v is not None)):
            cleaned["enum"] = stringified
        else:
            cleaned.pop("enum", None)
    elif isinstance(enum_val, list):
        # Preserve string enums, deduplicated with stable ordering
        seen_enums = set()
        deduped = []
        for item in enum_val:
            if isinstance(item, str) and item not in seen_enums:
                seen_enums.add(item)
                deduped.append(item)
        if deduped:
            cleaned["enum"] = deduped
        else:
            cleaned.pop("enum", None)

    # Gemini validates ``required`` strictly against the same node's
    # ``properties`` — GenerateContentRequest fails with HTTP 400
    # "...items.required[0]: property is not defined" when a required name
    # has no matching property in that node. Filter ``required`` to
    # names that exist in this node's ``properties`` and sort deterministically.
    required_val = cleaned.get("required")
    if isinstance(required_val, list):
        props_val = cleaned.get("properties")
        prop_names = set(props_val.keys()) if isinstance(props_val, dict) else set()
        valid_required = sorted(set(
            name for name in required_val
            if isinstance(name, str) and name in prop_names
        ))
        if not valid_required:
            cleaned.pop("required", None)
        else:
            cleaned["required"] = valid_required

    # Validate and filter ``propertyOrdering`` to declared properties
    po_val = cleaned.get("propertyOrdering")
    if isinstance(po_val, list):
        props_val = cleaned.get("properties")
        prop_names = set(props_val.keys()) if isinstance(props_val, dict) else set()
        seen_po = set()
        valid_po = []
        for name in po_val:
            if isinstance(name, str) and (not prop_names or name in prop_names) and name not in seen_po:
                seen_po.add(name)
                valid_po.append(name)
        if valid_po:
            cleaned["propertyOrdering"] = valid_po
        else:
            cleaned.pop("propertyOrdering", None)

    return _order_dict_keys(cleaned)


def sanitize_gemini_tool_parameters(parameters: Any) -> Dict[str, Any]:
    """Normalize tool parameters to a valid, deterministic Gemini object schema."""

    cleaned = sanitize_gemini_schema(parameters)
    if not cleaned:
        return {"type": "object", "properties": {}}
    if "type" not in cleaned:
        cleaned["type"] = "object"
    if cleaned.get("type") == "object" and "properties" not in cleaned:
        cleaned["properties"] = {}
    return _order_dict_keys(cleaned)


def serialize_gemini_schema(schema: Any) -> str:
    """Serialize a sanitized Gemini schema into a byte-stable, deterministic JSON string."""
    cleaned = sanitize_gemini_schema(schema)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


serialize_gemini_schema_deterministic = serialize_gemini_schema


def is_google_search_tool(tool: Any) -> bool:
    """Check if a tool declaration or name represents native Google Search Grounding."""
    if tool == "google_search" or tool == "googleSearch":
        return True
    if isinstance(tool, dict):
        if "googleSearch" in tool or "google_search" in tool:
            return True
        if tool.get("type") in {"google_search", "googleSearch"}:
            return True
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name") in {"google_search", "googleSearch"}:
            return True
        if tool.get("name") in {"google_search", "googleSearch"}:
            return True
    return False


def build_gemini_tools(
    tools: Any,
    *,
    enable_grounding: bool = False,
) -> List[Dict[str, Any]]:
    """Translate tool declarations to Gemini API format with optional grounding.

    Returns a list of Gemini tool specifications, e.g.:
    [
        {"functionDeclarations": [...]},
        {"googleSearch": {}},
    ]
    """
    if not isinstance(tools, list):
        tools = []

    declarations: List[Dict[str, Any]] = []
    has_grounding = bool(enable_grounding)

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if is_google_search_tool(tool):
            has_grounding = True
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if is_google_search_tool(name):
                has_grounding = True
                continue
            if not isinstance(name, str) or not name:
                continue
            decl: Dict[str, Any] = {"name": name}
            description = fn.get("description")
            if isinstance(description, str) and description:
                decl["description"] = description
            parameters = fn.get("parameters")
            if isinstance(parameters, dict):
                decl["parameters"] = sanitize_gemini_tool_parameters(parameters)
            declarations.append(decl)
        elif "name" in tool and isinstance(tool["name"], str):
            name = tool["name"]
            if is_google_search_tool(name):
                has_grounding = True
                continue
            decl = {"name": name}
            if "description" in tool and isinstance(tool["description"], str):
                decl["description"] = tool["description"]
            if "parameters" in tool and isinstance(tool["parameters"], dict):
                decl["parameters"] = sanitize_gemini_tool_parameters(tool["parameters"])
            declarations.append(decl)

    # Deterministic sorting of function declarations by name for byte-stable wire payload
    declarations.sort(key=lambda d: str(d.get("name", "")))

    result: List[Dict[str, Any]] = []
    if declarations:
        result.append({"functionDeclarations": declarations})
    if has_grounding:
        result.append(dict(GOOGLE_SEARCH_TOOL))
    return result
