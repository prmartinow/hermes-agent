"""Tool-schema preparation for Gemini's native API.

Two wire shapes: ``parametersJsonSchema`` (plain JSON Schema, v1beta only) gets a light
normalizer (``prepare_gemini_tool_parameters``); the legacy ``parameters`` field accepts
only the OpenAPI ``Schema`` subset and keeps the lossy translator
(``sanitize_gemini_tool_parameters``) for API versions without the JSON Schema field.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from typing import Any, Dict, List, Optional

from tools.schema_sanitizer import _normalize_type_array

logger = logging.getLogger(__name__)

# Gemini's ``FunctionDeclaration.parameters`` accepts only a subset of OpenAPI 3.0 /
# JSON Schema (the ``Schema`` object); everything else is stripped.
_GEMINI_SCHEMA_ALLOWED_KEYS = {
    "type", "format", "title", "description", "nullable", "enum", "maxItems", "minItems", "properties", "required",
    "minProperties", "maxProperties", "minLength", "maxLength", "pattern", "example", "anyOf", "propertyOrdering",
    "default", "items", "minimum", "maximum",
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


_GEMINI_STRUCTURAL_KEYS = {
    "array": {"items", "minItems", "maxItems"},
    "object": {"properties", "required", "minProperties", "maxProperties", "propertyOrdering"},
}


def _stringify_enum_value(item: Any) -> Any:
    """Gemini-safe string for a scalar enum entry, or None to drop it."""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, (int, float)) and math.isfinite(item):
        return str(item)
    return item if isinstance(item, str) else None


def _normalize_gemini_type_array(type_array: list, cleaned: Dict[str, Any]) -> None:
    """Keep union alternatives and their branch-local structural constraints."""
    derived: Dict[str, Any] = {}
    _normalize_type_array(type_array, derived)
    if "anyOf" in derived:
        constraints = {"anyOf": cleaned["anyOf"]} if "anyOf" in cleaned else {}
        # Gemini requires items/properties on the typed branch itself, not
        # its typeless parent. Keep required paired with those properties.
        structural = {key: cleaned.pop(key) for keys in _GEMINI_STRUCTURAL_KEYS.values()
                      for key in keys if key in cleaned}
        cleaned["anyOf"] = [
            sanitize_gemini_schema({**branch, **constraints, **{
                key: value for key, value in structural.items()
                if key in _GEMINI_STRUCTURAL_KEYS.get(branch["type"], ())
            }}) for branch in derived["anyOf"]
        ]
    else:
        cleaned["type"] = derived["type"]
    if derived.get("nullable"):
        # Derived from "null" in the array. Set AFTER the loop so it beats an input
        # ``nullable: false`` regardless of which key the producer emitted first.
        cleaned["nullable"] = True


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
        if key == "type" and isinstance(value, str):
            cleaned[key] = value.lower()
            continue
        if key == "properties":
            if not isinstance(value, dict):
                continue
            props: Dict[str, Any] = {}
            for prop_name in sorted(k for k in value if isinstance(k, str)):
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

    type_array = cleaned.get("type")
    if isinstance(type_array, list):
        cleaned.pop("type")
        _normalize_gemini_type_array(
            [t.lower() if isinstance(t, str) else t for t in type_array], cleaned
        )

    # Gemini requires every ``enum`` entry to be a string even for
    # integer/number/boolean types; the declared type stays intact and Gemini
    # still emits typed tool arguments at runtime. dict.fromkeys = ordered dedupe.
    enum_val = cleaned.get("enum")
    if isinstance(enum_val, list) and (
        isinstance(type_array, list) or cleaned.get("type") in {"integer", "number", "boolean"}
    ):
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
    if "type" not in cleaned and "anyOf" not in cleaned:
        cleaned["type"] = "object"
    if cleaned.get("type") == "object" and "properties" not in cleaned:
        cleaned["properties"] = {}
    return _order_dict_keys(cleaned)


# ── parametersJsonSchema (full JSON Schema) ─────────────────────────────────
#
# The legacy translator is lossy: anyOf unions without an outer type, bare arrays,
# $ref/$defs and additionalProperties had to be stripped or repaired, and one
# unrepresentable construct 400s the ENTIRE request. Through parametersJsonSchema the
# schema goes as-is; only same-document $refs are inlined (MCP pydantic / zod emit
# them and Google rejects reference indirection) and root ``$schema`` is dropped.

_EMPTY_OBJECT_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}
# Real tool schemas hold a handful of refs; the cap stops circular pydantic models
# from expanding forever.
_MAX_REF_EXPANSIONS = 256


def _resolve_local_ref(root: Dict[str, Any], ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a same-document JSON pointer (``#/$defs/Foo``) against *root*."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _inline_refs(node: Any, root: Dict[str, Any], budget: List[int], stack: tuple = ()) -> Any:
    """Recursively inline same-document ``$ref`` nodes; ``ValueError`` on an unresolvable
    or circular reference or an exhausted budget (the caller then keeps the original)."""
    if isinstance(node, list):
        return [_inline_refs(item, root, budget, stack) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return {key: _inline_refs(value, root, budget, stack) for key, value in node.items()}
    if ref in stack:
        raise ValueError(f"circular $ref {ref!r}")
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("$ref expansion budget exhausted")
    target = _resolve_local_ref(root, ref)
    if target is None:
        raise ValueError(f"unresolvable $ref {ref!r}")
    inlined = _inline_refs(target, root, budget, stack + (ref,))
    # JSON Schema: siblings of $ref (description, default, ...) apply alongside the
    # referenced schema and win over it.
    siblings = {k: v for k, v in node.items() if k != "$ref"}
    return {**inlined, **_inline_refs(siblings, root, budget, stack)} if siblings else inlined


def prepare_gemini_tool_parameters(parameters: Any) -> Dict[str, Any]:
    """Full JSON Schema for ``parametersJsonSchema``: deep-copied, root ``$schema`` dropped,
    same-document ``$ref`` inlined, object root guaranteed. A schema whose references
    cannot all be resolved is sent untouched so the provider names the real problem."""
    if not isinstance(parameters, dict) or not parameters:
        return dict(_EMPTY_OBJECT_SCHEMA)
    schema = copy.deepcopy(parameters)
    schema.pop("$schema", None)
    try:
        schema = _inline_refs(schema, schema, [_MAX_REF_EXPANSIONS])
    except ValueError as exc:
        logger.debug("Gemini tool schema kept as-is ($ref inlining skipped): %s", exc)
        return schema
    schema.pop("$defs", None)
    schema.pop("definitions", None)
    if not schema:
        return dict(_EMPTY_OBJECT_SCHEMA)
    if schema.get("type") == "object" and "properties" not in schema:
        schema["properties"] = {}
    return schema


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
