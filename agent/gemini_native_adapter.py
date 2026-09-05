"""OpenAI-compatible facade over Google AI Studio's native Gemini API.

Hermes keeps ``api_mode='chat_completions'`` for the ``gemini`` provider so the
main agent loop can keep using its existing OpenAI-shaped message flow.
This adapter is the transport shim that converts those OpenAI-style
``messages[]`` / ``tools[]`` requests into Gemini's native
``models/{model}:generateContent`` schema and converts the responses back.

Why this exists
---------------
Google's OpenAI-compatible endpoint has been brittle for Hermes's multi-turn
agent/tool loop (auth churn, tool-call replay quirks, thought-signature
requirements).  The native Gemini API is the canonical path and avoids the
OpenAI-compat layer entirely.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set
from urllib.parse import unquote

import httpx

from agent.bounded_response import read_streaming_error_body
from agent.gemini_schema import (
    build_gemini_tools,
    sanitize_gemini_tool_parameters,
)

logger = logging.getLogger(__name__)

try:
    import hermes_cli as _hermes_cli

    _HERMES_VERSION = str(_hermes_cli.__version__)
except Exception:
    _HERMES_VERSION = "0.0.0"

DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Published max output-token ceiling shared by every current Gemini text model
# (2.5 + 3.x: flash, flash-lite, pro). Used as the default when the caller
# passes max_tokens=None, because Gemini's native API otherwise applies a low
# internal default and truncates output (unlike OpenAI-compat endpoints where
# an omitted limit means full budget).
GEMINI_DEFAULT_MAX_OUTPUT_TOKENS = 65536


def bare_gemini_model_id(model: str) -> str:
    """Strip Gemini's own provider prefix from an aggregator-style model id."""
    name = (model or "").strip()
    lowered = name.lower()
    for prefix in (
        "google/", "gemini/", "gemini-oauth/", "gemini_oauth/",
        "gemini-1/", "gemini-2/", "gemini-3/", "gemini-4/", "gemini-5/",
        "gemini-oauth-1/", "gemini-oauth-2/", "gemini-oauth-3/", "gemini-oauth-4/", "gemini-oauth-5/",
    ):
        if lowered.startswith(prefix):
            return name[len(prefix):].strip() or name
    return name


def _gemini_major_version(model: str) -> Optional[int]:
    """Extract the major version from a Gemini model id (``gemini-3.6-flash`` → 3)."""
    name = bare_gemini_model_id(model).lower()
    match = re.match(r"gemini-(\d+)", name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def gemini_requires_tool_call_ids(model: str) -> bool:
    """Whether functionCall/functionResponse parts must carry explicit ids.

    Gemini 3+ models require explicit tool call IDs in replayed history —
    without them, multi-tool turns can be rejected or mismatched. Older
    Gemini models (2.x) reject unexpected ``id`` fields, so this is gated on
    the major version. Mirrors earendil-works/pi#7494 (their fix for the same
    class of bug in the google-shared converter).
    """
    version = _gemini_major_version(model)
    return version is not None and version >= 3


def is_native_gemini_base_url(base_url: str) -> bool:
    """Return True when the endpoint speaks Gemini's native REST API."""
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if not normalized:
        return False
    if "generativelanguage.googleapis.com" not in normalized:
        return False
    return not normalized.endswith("/openai")


def probe_gemini_tier(
    api_key: str,
    base_url: str = DEFAULT_GEMINI_BASE_URL,
    *,
    model: str = "gemini-3.6-flash",
    timeout: float = 10.0,
) -> str:
    """Probe a Google AI Studio API key and return its tier.

    Returns one of:

    - ``"free"``    -- key is on the free tier (unusable with Hermes)
    - ``"paid"``    -- key is on a paid tier
    - ``"unknown"`` -- probe failed; callers should proceed without blocking.
    """
    key = (api_key or "").strip()
    if not key:
        return "unknown"

    normalized_base = str(base_url or DEFAULT_GEMINI_BASE_URL).strip().rstrip("/")
    if not normalized_base:
        normalized_base = DEFAULT_GEMINI_BASE_URL
    if normalized_base.lower().endswith("/openai"):
        normalized_base = normalized_base[: -len("/openai")]

    url = f"{normalized_base}/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                params={"key": key},
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Client": f"hermes-agent/{_HERMES_VERSION}",
                },
            )
    except Exception as exc:
        logger.debug("probe_gemini_tier: network error: %s", exc)
        return "unknown"

    headers_lower = {k.lower(): v for k, v in resp.headers.items()}
    rpd_header = headers_lower.get("x-ratelimit-limit-requests-per-day")
    if rpd_header:
        try:
            rpd_val = int(rpd_header)
        except (TypeError, ValueError):
            rpd_val = None
        # Published free-tier daily caps (Dec 2025):
        #   gemini-2.5-pro: 100, gemini-2.5-flash: 250, flash-lite: 1000
        # Tier 1 starts at ~1500+ for Flash. We treat <= 1000 as free.
        if rpd_val is not None and rpd_val <= 1000:
            return "free"
        if rpd_val is not None and rpd_val > 1000:
            return "paid"

    if resp.status_code == 429:
        body_text = ""
        try:
            body_text = resp.text or ""
        except Exception:
            body_text = ""
        if "free_tier" in body_text.lower():
            return "free"
        return "paid"

    if 200 <= resp.status_code < 300:
        return "paid"

    return "unknown"


def is_free_tier_quota_error(error_message: str) -> bool:
    """Return True when a Gemini 429 message indicates free-tier exhaustion."""
    if not error_message:
        return False
    return "free_tier" in error_message.lower()


_FREE_TIER_GUIDANCE = (
    "\n\nYour Google API key is on the free tier (a few hundred requests/day "
    "for Gemini Flash models). Hermes typically makes 3-10 API calls per user turn, "
    "so the free tier is exhausted in a handful of messages and cannot sustain "
    "an agent session. Enable billing on your Google Cloud project and "
    "regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey"
)


def is_standard_key_auth_error(
    status: int, error_message: str, reason: str = "", url: str = ""
) -> bool:
    """Return True when a Gemini 401 indicates Google rejected the key TYPE.

    Google began rejecting unrestricted legacy "Standard" Google Cloud API
    keys on the Gemini API on June 19, 2026, and ALL Standard keys stop
    working in September 2026. The rejection surfaces as a misleading 401
    telling the user to supply an OAuth 2 access token ("Request had invalid
    authentication credentials. Expected OAuth 2 access token, login cookie
    or other valid authentication credential."), optionally carrying
    ``google.rpc.ErrorInfo`` reason ``ACCESS_TOKEN_TYPE_UNSUPPORTED``.

    Scoped narrowly so a plain bad key (reason ``API_KEY_INVALID``,
    "API key not valid") keeps its existing message, and OAuth endpoints
    (Cloud Code PA) do not trigger API key advice.
    """
    if status != 401:
        return False
    if "cloudcode-pa.googleapis.com" in str(url):
        return False
    if reason == "ACCESS_TOKEN_TYPE_UNSUPPORTED":
        return True
    return "expected oauth 2 access token" in (error_message or "").lower()


_STANDARD_KEY_GUIDANCE = (
    "\n\nGoogle Gemini rejected this API key's type — you do NOT need OAuth. "
    "Google began rejecting legacy 'Standard' Google Cloud keys for the "
    "Gemini API on June 19, 2026, and all Standard keys stop working in "
    "September 2026. Open https://aistudio.google.com/api-keys, check the "
    "key's type and status, and create a replacement Gemini API key (or, as "
    "a temporary bridge, restrict the Standard key to "
    "generativelanguage.googleapis.com). Then update GEMINI_API_KEY / "
    "GOOGLE_API_KEY in ~/.hermes/.env and restart your session. "
    "Details: https://ai.google.dev/gemini-api/docs/api-key"
)


class GeminiAPIError(Exception):
    """Error shape compatible with Hermes retry/error classification."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "gemini_api_error",
        status_code: Optional[int] = None,
        response: Optional[httpx.Response] = None,
        retry_after: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.response = response
        self.retry_after = retry_after
        self.details = details or {}
        self.challenge_url: Optional[str] = (self.details.get("challenge_url") or None)


def _coerce_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    return str(content)


_EXTENSION_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".pdf": "application/pdf",
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/m4a",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
}


def _detect_mime_type(path_or_url: str, data: Optional[bytes] = None) -> str:
    """Detect MIME type using magic bytes, extension map, or mimetypes module."""
    if data:
        if data.startswith(b"%PDF"):
            return "application/pdf"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"BM"):
            return "image/bmp"

    clean = str(path_or_url or "").split("?")[0].split("#")[0].lower()
    for ext, mime in _EXTENSION_TO_MIME.items():
        if clean.endswith(ext):
            return mime

    guessed, _ = mimetypes.guess_type(clean)
    if guessed:
        return guessed

    return "application/octet-stream"


def _resolve_media_to_inline_data(
    media_ref: str, mime_type: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Convert local file path, file:// URI, remote URL, or data URI to base64 inlineData."""
    if not isinstance(media_ref, str) or not media_ref.strip():
        return None
    ref = media_ref.strip()

    # 1. Data URI (e.g. data:image/png;base64,iVBORw0KGgo...)
    if ref.startswith("data:"):
        try:
            header, encoded = ref.split(",", 1)
            parsed_mime = header.split(":", 1)[1].split(";", 1)[0].strip()
            clean_b64 = re.sub(r"\s+", "", encoded)
            base64.b64decode(clean_b64)
            return {
                "inlineData": {
                    "mimeType": mime_type or parsed_mime or "image/png",
                    "data": clean_b64,
                }
            }
        except Exception as exc:
            logger.debug("_resolve_media_to_inline_data: failed to parse data URI: %s", exc)
            return None

    # 2. Local file path or file:// URI
    file_path = ref
    if file_path.startswith("file://"):
        file_path = unquote(file_path[7:])
    expanded_path = os.path.expanduser(file_path)

    if os.path.isfile(expanded_path):
        try:
            with open(expanded_path, "rb") as f:
                raw_bytes = f.read()
            detected_mime = mime_type or _detect_mime_type(expanded_path, raw_bytes)
            if detected_mime == "application/octet-stream" and not mime_type:
                detected_mime = "image/png"
            return {
                "inlineData": {
                    "mimeType": detected_mime,
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                }
            }
        except Exception as exc:
            logger.warning("_resolve_media_to_inline_data: failed to read file %s: %s", expanded_path, exc)
            return None

    # 3. Remote HTTP/HTTPS URL
    if ref.startswith(("http://", "https://")):
        try:
            resp = httpx.get(
                ref,
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": f"hermes-agent/{_HERMES_VERSION}"},
            )
            if resp.status_code == 200:
                raw_bytes = resp.content
                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                detected_mime = (
                    mime_type
                    or (content_type if content_type and "/" in content_type else None)
                    or _detect_mime_type(ref, raw_bytes)
                )
                if detected_mime == "application/octet-stream" and not mime_type:
                    detected_mime = "image/png"
                return {
                    "inlineData": {
                        "mimeType": detected_mime,
                        "data": base64.b64encode(raw_bytes).decode("ascii"),
                    }
                }
            else:
                logger.warning(
                    "_resolve_media_to_inline_data: HTTP %d downloading media from %s",
                    resp.status_code,
                    ref,
                )
        except Exception as exc:
            logger.warning(
                "_resolve_media_to_inline_data: failed to download remote media from %s: %s",
                ref,
                exc,
            )
            return None

    return None


def _extract_multimodal_parts(content: Any) -> List[Dict[str, Any]]:
    """Extract multimodal parts (text and base64 inlineData) from message content."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        text = _coerce_content_to_text(content)
        return [{"text": text}] if text else []

    parts: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue

        media_res = (
            item.get("mediaResolution")
            or item.get("media_resolution")
            or (item.get("image_url", {}).get("mediaResolution") if isinstance(item.get("image_url"), dict) else None)
            or (item.get("image_url", {}).get("media_resolution") if isinstance(item.get("image_url"), dict) else None)
            or (item.get("image_url", {}).get("detail") if isinstance(item.get("image_url"), dict) else None)
            or item.get("detail")
        )
        res_str = None
        if isinstance(media_res, str):
            res_clean = media_res.strip().upper()
            if res_clean in ("LOW", "MEDIA_RESOLUTION_LOW"):
                res_str = "MEDIA_RESOLUTION_LOW"
            elif res_clean in ("MEDIUM", "MEDIA_RESOLUTION_MEDIUM"):
                res_str = "MEDIA_RESOLUTION_MEDIUM"
            elif res_clean in ("HIGH", "MEDIA_RESOLUTION_HIGH"):
                res_str = "MEDIA_RESOLUTION_HIGH"

        def _attach_res(part_dict: Dict[str, Any]) -> Dict[str, Any]:
            if res_str and isinstance(part_dict, dict) and "inlineData" in part_dict and isinstance(part_dict["inlineData"], dict):
                part_dict["inlineData"]["mediaResolution"] = res_str
            return part_dict

        # Direct inlineData / inline_data objects
        if "inlineData" in item and isinstance(item["inlineData"], dict):
            parts.append(_attach_res({"inlineData": dict(item["inlineData"])}))
            continue
        if "inline_data" in item and isinstance(item["inline_data"], dict):
            idat = item["inline_data"]
            mime = idat.get("mime_type") or idat.get("mimeType") or "image/png"
            data = idat.get("data")
            if data:
                parts.append(_attach_res({"inlineData": {"mimeType": mime, "data": data}}))
                continue

        ptype = str(item.get("type") or "").lower()

        if ptype == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
            continue

        if ptype in ("image_url", "input_image"):
            image_url_obj = item.get("image_url")
            url_str = (
                image_url_obj.get("url")
                if isinstance(image_url_obj, dict)
                else (image_url_obj if isinstance(image_url_obj, str) else "")
            )
            inline_part = _resolve_media_to_inline_data(url_str)
            if inline_part:
                parts.append(_attach_res(inline_part))
            continue

        if ptype in ("image", "document", "file", "input_file", "pdf", "audio", "video"):
            source = item.get("source")
            if isinstance(source, dict):
                stype = str(source.get("type") or "").lower()
                if stype == "base64":
                    mime = source.get("media_type") or source.get("mime_type") or "image/png"
                    data = source.get("data")
                    if isinstance(data, str) and data:
                        parts.append(_attach_res({"inlineData": {"mimeType": mime, "data": data.strip()}}))
                        continue
                elif stype == "url":
                    url = source.get("url")
                    inline_part = _resolve_media_to_inline_data(str(url or ""))
                    if inline_part:
                        parts.append(_attach_res(inline_part))
                        continue

            media_ref = (
                item.get("path")
                or item.get("file_path")
                or item.get("url")
                or item.get("image_url")
            )
            mime = item.get("mime_type") or item.get("mimeType") or item.get("media_type")
            data = item.get("data") or item.get("base64")
            if isinstance(data, str) and data.strip():
                parts.append(_attach_res({"inlineData": {"mimeType": mime or "image/png", "data": data.strip()}}))
                continue
            if media_ref:
                inline_part = _resolve_media_to_inline_data(str(media_ref), mime_type=mime)
                if inline_part:
                    parts.append(_attach_res(inline_part))
                    continue

        if ptype in ("inline_data", "inlinedata"):
            idat = item.get("inline_data") or item.get("inlineData") or item
            if isinstance(idat, dict):
                mime = idat.get("mime_type") or idat.get("mimeType") or "image/png"
                data = idat.get("data")
                if data:
                    parts.append(_attach_res({"inlineData": {"mimeType": mime, "data": data}}))
                    continue

    return parts


def is_gemini_model(model: str) -> bool:
    """Return True if model is a native Google Gemini model (not a 3P/partner model).

    Gemini models require thought signatures for tool call validation on Gemini 3.
    Non-Gemini partner models (e.g. claude-sonnet-4-6, claude-opus-4-6-thinking,
    gpt-oss-120b-medium) reject thoughtSignature with HTTP 400.
    """
    name = bare_gemini_model_id(model or "").lower().strip()
    if not name:
        return True
    if name in (
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ):
        return False
    if any(p in name for p in ("claude", "gpt-oss", "anthropic", "openai", "llama", "mistral", "qwen", "deepseek")):
        return False
    return True


def _tool_call_extra_signature(tool_call: Dict[str, Any]) -> Optional[str]:
    """Extract cryptographic thought signature from a tool call dictionary."""
    if not isinstance(tool_call, dict):
        return None

    # 1. Direct fields on tool_call
    for key in ("thought_signature", "thoughtSignature", "thought_sig"):
        val = tool_call.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 2. extra_content dictionary
    extra = tool_call.get("extra_content")
    if isinstance(extra, dict):
        for key in ("thought_signature", "thoughtSignature", "signature", "thought_sig"):
            val = extra.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        google = extra.get("google")
        if isinstance(google, dict):
            sig = google.get("thought_signature") or google.get("thoughtSignature") or google.get("signature")
            if isinstance(sig, str) and sig.strip():
                return sig.strip()
        elif isinstance(google, str) and google.strip():
            return google.strip()

    # 3. Inside function dict if present
    fn = tool_call.get("function")
    if isinstance(fn, dict):
        for key in ("thought_signature", "thoughtSignature"):
            val = fn.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    return None


# Stands in for a model turn that never arrived (stream failure / interrupt /
# quota fallback) when history leaves a human user text turn directly after a
# tool-result turn. Interposed between the two user contents so the request
# stays alternation-valid while the user's message remains a turn of its own.
# Mirrors gemini-cli's INTERRUPTED_RESPONSE_PLACEHOLDER (gemini-cli#28700).
_INTERRUPTED_RESPONSE_PLACEHOLDER = (
    "[The previous response was interrupted before it completed.]"
)


def _translate_tool_call_to_gemini(
    tool_call: Dict[str, Any],
    include_ids: bool = False,
    model: str = "",
) -> Dict[str, Any]:
    fn = tool_call.get("function") or {}
    args_raw = fn.get("arguments") or {}
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except json.JSONDecodeError:
        args = {"_raw": args_raw}
    if not isinstance(args, dict):
        args = {"_value": args}

    part: Dict[str, Any] = {
        "functionCall": {
            "name": str(fn.get("name") or ""),
            "args": args,
        }
    }
    if include_ids:
        # Gemini 3+ requires explicit tool call IDs so replayed parallel tool
        # calls pair with their functionResponses (earendil-works/pi#7494).
        tool_call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
        if tool_call_id:
            part["functionCall"]["id"] = tool_call_id

    # Cryptographic thought signature handling:
    # 1. Non-Gemini partner models (claude-sonnet-4-6, claude-opus-4-6-thinking, gpt-oss-120b-medium):
    #    Strictly STRIP thoughtSignature to prevent HTTP 400 rejection by Google's Cloud Code PA proxy.
    # 2. Gemini models:
    #    Attach thoughtSignature if present in extra_content/tool_call; if missing or compacted,
    #    attach fallback "skip_thought_signature_validator".
    if is_gemini_model(model):
        thought_signature = _tool_call_extra_signature(tool_call) or "skip_thought_signature_validator"
        part["thoughtSignature"] = thought_signature

    return part


def _translate_tool_result_to_gemini(
    message: Dict[str, Any],
    tool_name_by_call_id: Optional[Dict[str, str]] = None,
    include_ids: bool = False,
) -> Dict[str, Any]:
    tool_name_by_call_id = tool_name_by_call_id or {}
    tool_call_id = str(message.get("tool_call_id") or "")
    # A tool result can carry the unwrapped internal tool name (for example,
    # an MCP tool invoked through the `tool_call` bridge). Gemini requires
    # the functionResponse name to match the functionCall name it originally
    # emitted, so prefer the declaration name mapped from the call ID.
    resolved_name = tool_name_by_call_id.get(tool_call_id)
    if not resolved_name:
        resolved_name = str(message.get("tool_name") or message.get("name") or "")
    name = resolved_name
    content_raw = message.get("content")
    try:
        content_val = (
            json.loads(content_raw)
            if isinstance(content_raw, str)
            else content_raw
        )
    except json.JSONDecodeError:
        content_val = content_raw
    response_payload: Dict[str, Any]
    if isinstance(content_val, dict):
        response_payload = content_val
    else:
        response_payload = {"output": content_val}
    part: Dict[str, Any] = {
        "functionResponse": {
            "name": name,
            "response": response_payload,
        }
    }
    if include_ids and tool_call_id:
        part["functionResponse"]["id"] = tool_call_id
    return part


def _build_gemini_contents(
    messages: List[Dict[str, Any]],
    include_tool_call_ids: bool = False,
    model: str = "",
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    system_text_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    tool_name_by_call_id: Dict[str, str] = {}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")

        if role == "system":
            system_text_parts.append(_coerce_content_to_text(msg.get("content")))
            continue

        if role in {"tool", "function"}:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        _translate_tool_result_to_gemini(
                            msg,
                            tool_name_by_call_id=tool_name_by_call_id,
                            include_ids=include_tool_call_ids,
                        )
                    ],
                }
            )
            continue

        gemini_role = "model" if role == "assistant" else "user"
        parts: List[Dict[str, Any]] = []

        content_parts = _extract_multimodal_parts(msg.get("content"))
        parts.extend(content_parts)

        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    tool_call_id = str(tool_call.get("id") or tool_call.get("call_id") or "")
                    tool_name = str(((tool_call.get("function") or {}).get("name") or ""))
                    if tool_call_id and tool_name:
                        tool_name_by_call_id[tool_call_id] = tool_name
                    parts.append(
                        _translate_tool_call_to_gemini(
                            tool_call, include_ids=include_tool_call_ids, model=model
                        )
                    )

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    # Compatibility contract for native Gemini generateContent:
    # 1) Same-role adjacent contents still merge in general (strict user/model
    #    alternation for ordinary text turns and parallel tool-result grouping;
    #    consecutive same-role contents are rejected with HTTP 400 "Please
    #    ensure that multiturn requests alternate between user and model").
    # 2) Exception: do NOT fuse a human user text turn into a preceding user
    #    content that only carries functionResponse parts (or vice versa).
    #    Gemini 3 accepts that fold with HTTP 200 but then reads the trailing
    #    text as a continuation of the tool result — it returns an empty
    #    candidate or "finishes the user's sentence" instead of answering
    #    (same defect gemini-cli fixed in google-gemini/gemini-cli#28700).
    # 3) Because rule 1's HTTP 400 makes two consecutive user contents unsafe
    #    to emit (#55125 — the reason this merge exists), the split pair is
    #    kept API-valid by interposing a placeholder model turn between the
    #    functionResponse content and the human text content, mirroring
    #    gemini-cli's INTERRUPTED_RESPONSE_PLACEHOLDER repair.
    # 4) Parallel tool results (functionResponse + functionResponse) still
    #    merge into one user content — only mixed functionResponse/text is
    #    kept apart.
    merged_contents: List[Dict[str, Any]] = []
    for content in contents:
        same_role = bool(
            merged_contents and merged_contents[-1]["role"] == content["role"]
        )
        if same_role and content["role"] == "user":
            previous_has_function_response = any(
                isinstance(part, dict) and "functionResponse" in part
                for part in merged_contents[-1].get("parts", [])
            )
            current_has_function_response = any(
                isinstance(part, dict) and "functionResponse" in part
                for part in content.get("parts", [])
            )
            if previous_has_function_response != current_has_function_response:
                same_role = False
                merged_contents.append(
                    {
                        "role": "model",
                        "parts": [{"text": _INTERRUPTED_RESPONSE_PLACEHOLDER}],
                    }
                )

        if same_role:
            merged_contents[-1]["parts"].extend(content["parts"])
        else:
            merged_contents.append(content)
    contents = merged_contents

    # 5) Gemini multi-turn validation requires that `contents` begins with a
    #    `user` turn. If history compaction/slicing or an initial assistant turn
    #    leaves `contents[0]` as a `model` turn (especially with functionCall
    #    parts), prepend a synthetic user turn ("Continue") so that functionCall
    #    turns always immediately follow a user turn and multi-turn alternation
    #    invariants are satisfied.
    if contents and contents[0].get("role") == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "Continue"}]})
    elif not contents:
        contents = [{"role": "user", "parts": [{"text": "Continue"}]}]

    system_instruction = None
    joined_system = "\n".join(part for part in system_text_parts if part).strip()
    if joined_system:
        system_instruction = {"role": "system", "parts": [{"text": joined_system}]}
    return contents, system_instruction


def _translate_tools_to_gemini(tools: Any) -> List[Dict[str, Any]]:
    return build_gemini_tools(tools)


def _translate_tool_choice_to_gemini(tool_choice: Any) -> Optional[Dict[str, Any]]:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        if tool_choice == "required":
            return {"functionCallingConfig": {"mode": "ANY"}}
        if tool_choice == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    return None


def _normalize_thinking_config(config: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(config, dict) or not config:
        return None
    budget = config.get("thinkingBudget", config.get("thinking_budget"))
    include = config.get("includeThoughts", config.get("include_thoughts"))
    level = config.get("thinkingLevel", config.get("thinking_level"))
    normalized: Dict[str, Any] = {}
    if isinstance(budget, (int, float)):
        normalized["thinkingBudget"] = int(budget)
    if isinstance(include, bool):
        normalized["includeThoughts"] = include
    if isinstance(level, str) and level.strip():
        normalized["thinkingLevel"] = level.strip().lower()
    return normalized or None


def _thinking_requests_output_headroom(thinking_config: Any) -> bool:
    """Return True when Gemini will spend output tokens on thinking.

    Gemini bills thought tokens against ``maxOutputTokens``. A global
    Hermes ``max_tokens`` of 4096/16384 is enough for visible text, but
    Ultra/high thinking can consume the entire budget and leave
    ``finishReason=MAX_TOKENS`` with no complete answer. Continuations
    then abort after 4 retries.
    """
    normalized = _normalize_thinking_config(thinking_config)
    if not normalized:
        return False
    if normalized.get("includeThoughts") is False:
        return "thinkingLevel" in normalized or bool(normalized.get("thinkingBudget"))
    budget = normalized.get("thinkingBudget")
    if isinstance(budget, int) and budget <= 0 and "thinkingLevel" not in normalized:
        return False
    return True


def _effective_gemini_max_output_tokens(
    max_tokens: Optional[int],
    thinking_config: Any,
    model: str = "",
) -> int:
    """Resolve native ``maxOutputTokens``.

    Gemini's generateContent API does not treat an omitted cap as
    unlimited — it applies a low internal default and truncates. When
    thinking is enabled, also raise a too-small explicit cap to the
    published ceiling (or thinkingBudget + 8192 headroom) so thought tokens
    do not starve the answer.
    """
    bare = bare_gemini_model_id(model).lower() if model else ""
    default_ceiling = GEMINI_DEFAULT_MAX_OUTPUT_TOKENS
    if "gpt-oss" in bare:
        default_ceiling = 8192
    elif "claude" in bare:
        default_ceiling = 64000

    if max_tokens is None:
        requested = default_ceiling
    else:
        try:
            requested = int(max_tokens)
        except (TypeError, ValueError):
            requested = default_ceiling
    if requested <= 0:
        requested = default_ceiling

    normalized = _normalize_thinking_config(thinking_config)
    if _thinking_requests_output_headroom(thinking_config):
        budget = normalized.get("thinkingBudget", 0) if normalized else 0
        if isinstance(budget, (int, float)) and budget > 0:
            return max(requested, int(budget) + 8192, default_ceiling)
        return max(requested, default_ceiling)
    return min(requested, default_ceiling)


def _normalize_media_resolution(media_resolution: Any) -> Optional[str]:
    """Normalize media resolution string to Gemini generationConfig format."""
    if not media_resolution or not isinstance(media_resolution, str):
        return None
    val = media_resolution.strip().upper()
    if val in ("LOW", "MEDIA_RESOLUTION_LOW"):
        return "MEDIA_RESOLUTION_LOW"
    if val in ("MEDIUM", "MEDIA_RESOLUTION_MEDIUM"):
        return "MEDIA_RESOLUTION_MEDIUM"
    if val in ("HIGH", "MEDIA_RESOLUTION_HIGH"):
        return "MEDIA_RESOLUTION_HIGH"
    if val in ("UNSPECIFIED", "MEDIA_RESOLUTION_UNSPECIFIED"):
        return "MEDIA_RESOLUTION_UNSPECIFIED"
    if val.startswith("MEDIA_RESOLUTION_"):
        return val
    return None


def build_gemini_request(
    *,
    messages: List[Dict[str, Any]],
    tools: Any = None,
    tool_choice: Any = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    top_p: Optional[float] = None,
    stop: Any = None,
    thinking_config: Any = None,
    media_resolution: Optional[str] = None,
    model: str = "",
) -> Dict[str, Any]:
    contents, system_instruction = _build_gemini_contents(
        messages,
        include_tool_call_ids=gemini_requires_tool_call_ids(model),
        model=model,
    )
    request: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        request["systemInstruction"] = system_instruction

    gemini_tools = _translate_tools_to_gemini(tools)
    if gemini_tools:
        request["tools"] = gemini_tools

    tool_config = _translate_tool_choice_to_gemini(tool_choice)
    if tool_config:
        request["toolConfig"] = tool_config

    generation_config: Dict[str, Any] = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    generation_config["maxOutputTokens"] = _effective_gemini_max_output_tokens(
        max_tokens, thinking_config, model=model
    )
    if top_p is not None:
        generation_config["topP"] = top_p
    if stop:
        generation_config["stopSequences"] = stop if isinstance(stop, list) else [str(stop)]
    normalized_thinking = _normalize_thinking_config(thinking_config)
    if normalized_thinking:
        generation_config["thinkingConfig"] = normalized_thinking
    normalized_resolution = _normalize_media_resolution(media_resolution)
    if normalized_resolution:
        generation_config["mediaResolution"] = normalized_resolution
    if generation_config:
        request["generationConfig"] = generation_config

    return request


def _map_gemini_finish_reason(reason: str) -> str:
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "OTHER": "stop",
    }
    return mapping.get(str(reason or "").upper(), "stop")


def _tool_call_extra_from_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sig = part.get("thoughtSignature") or part.get("thought_signature")
    if isinstance(sig, str) and sig.strip():
        return {
            "google": {"thought_signature": sig.strip()},
            "thought_signature": sig.strip(),
        }
    return None


def _empty_response(model: str) -> SimpleNamespace:
    message = SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    usage = SimpleNamespace(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    return SimpleNamespace(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[choice],
        usage=usage,
    )


def translate_gemini_response(resp: Dict[str, Any], model: str) -> SimpleNamespace:
    candidates = resp.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return _empty_response(model)

    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    content_obj = cand.get("content") if isinstance(cand, dict) else {}
    parts = content_obj.get("parts") if isinstance(content_obj, dict) else []

    text_pieces: List[str] = []
    reasoning_pieces: List[str] = []
    tool_calls: List[SimpleNamespace] = []

    for index, part in enumerate(parts or []):
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True and isinstance(part.get("text"), str):
            reasoning_pieces.append(part["text"])
            continue
        if isinstance(part.get("text"), str):
            text_pieces.append(part["text"])
            continue
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            try:
                args_str = json.dumps(fc.get("args") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = "{}"
            tool_call = SimpleNamespace(
                id=(
                    str(fc["id"])
                    if isinstance(fc.get("id"), str) and fc.get("id")
                    else f"call_{uuid.uuid4().hex[:12]}"
                ),
                type="function",
                index=index,
                function=SimpleNamespace(name=str(fc["name"]), arguments=args_str),
            )
            extra_content = _tool_call_extra_from_part(part)
            if extra_content:
                tool_call.extra_content = extra_content
            tool_calls.append(tool_call)

    finish_reason = "tool_calls" if tool_calls else _map_gemini_finish_reason(str(cand.get("finishReason") or ""))
    usage_meta = resp.get("usageMetadata") or {}
    usage = SimpleNamespace(
        prompt_tokens=int(usage_meta.get("promptTokenCount") or 0),
        completion_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
        total_tokens=int(usage_meta.get("totalTokenCount") or 0),
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=int(usage_meta.get("cachedContentTokenCount") or 0),
        ),
    )
    reasoning = "".join(reasoning_pieces) or None
    message = SimpleNamespace(
        role="assistant",
        content="".join(text_pieces) if text_pieces else None,
        tool_calls=tool_calls or None,
        reasoning=reasoning,
        reasoning_content=reasoning,
        reasoning_details=None,
    )
    choice = SimpleNamespace(index=0, message=message, finish_reason=finish_reason)
    return SimpleNamespace(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[choice],
        usage=usage,
    )


class _GeminiStreamChunk(SimpleNamespace):
    pass


def _make_stream_chunk(
    *,
    model: str,
    content: str = "",
    tool_call_delta: Optional[Dict[str, Any]] = None,
    finish_reason: Optional[str] = None,
    reasoning: str = "",
) -> _GeminiStreamChunk:
    delta_kwargs: Dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": None,
        "reasoning": None,
        "reasoning_content": None,
    }
    if content:
        delta_kwargs["content"] = content
    if tool_call_delta is not None:
        tool_delta = SimpleNamespace(
            index=tool_call_delta.get("index", 0),
            id=tool_call_delta.get("id") or f"call_{uuid.uuid4().hex[:12]}",
            type="function",
            function=SimpleNamespace(
                name=tool_call_delta.get("name") or "",
                arguments=tool_call_delta.get("arguments") or "",
            ),
        )
        extra_content = tool_call_delta.get("extra_content")
        if isinstance(extra_content, dict):
            tool_delta.extra_content = extra_content
        delta_kwargs["tool_calls"] = [tool_delta]
    if reasoning:
        delta_kwargs["reasoning"] = reasoning
        delta_kwargs["reasoning_content"] = reasoning
    delta = SimpleNamespace(**delta_kwargs)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return _GeminiStreamChunk(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        object="chat.completion.chunk",
        created=int(time.time()),
        model=model,
        choices=[choice],
        usage=None,
    )


def _iter_sse_events(response: httpx.Response) -> Iterator[Dict[str, Any]]:
    buffer = ""
    for chunk in response.iter_text():
        if not chunk:
            continue
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                return
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("Non-JSON Gemini SSE line: %s", data[:200])
                continue
            if isinstance(payload, dict):
                yield payload


def translate_stream_event(event: Dict[str, Any], model: str, tool_call_indices: Dict[str, Dict[str, Any]]) -> List[_GeminiStreamChunk]:
    candidates = event.get("candidates") or []
    if not candidates:
        return []
    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand, dict) else []
    chunks: List[_GeminiStreamChunk] = []
    seen_slots_in_event: Set[int] = set()

    for part_index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True and isinstance(part.get("text"), str):
            chunks.append(_make_stream_chunk(model=model, reasoning=part["text"]))
            continue
        if isinstance(part.get("text"), str) and part["text"]:
            chunks.append(_make_stream_chunk(model=model, content=part["text"]))
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            name = str(fc["name"])
            try:
                args_str = json.dumps(fc.get("args") or {}, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                args_str = "{}"
            fc_id = str(fc.get("id") or "").strip()

            slot = None
            if fc_id:
                call_key = f"id:{fc_id}"
                slot = tool_call_indices.get(call_key)
                if slot is None:
                    slot = {
                        "index": len(tool_call_indices),
                        "id": fc_id,
                        "name": name,
                        "last_arguments": "",
                    }
                    tool_call_indices[call_key] = slot
            else:
                # Find matching slot by name and argument continuity, excluding slots already matched in this event
                for k, s in tool_call_indices.items():
                    if s.get("index") in seen_slots_in_event:
                        continue
                    if s.get("name") == name:
                        last_args = str(s.get("last_arguments") or "")
                        if last_args == args_str or (last_args and args_str.startswith(last_args)):
                            slot = s
                            break
                if slot is None:
                    slot_key = f"auto_{len(tool_call_indices)}"
                    slot = {
                        "index": len(tool_call_indices),
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "name": name,
                        "last_arguments": "",
                    }
                    tool_call_indices[slot_key] = slot

            seen_slots_in_event.add(slot["index"])

            emitted_arguments = args_str
            last_arguments = str(slot.get("last_arguments") or "")
            if last_arguments:
                if args_str == last_arguments:
                    emitted_arguments = ""
                elif args_str.startswith(last_arguments):
                    emitted_arguments = args_str[len(last_arguments):]
            slot["last_arguments"] = args_str
            chunks.append(
                _make_stream_chunk(
                    model=model,
                    tool_call_delta={
                        "index": slot["index"],
                        "id": slot["id"],
                        "name": name,
                        "arguments": emitted_arguments,
                        "extra_content": _tool_call_extra_from_part(part),
                    },
                )
            )

    finish_reason_raw = str(cand.get("finishReason") or "")
    if finish_reason_raw:
        mapped = "tool_calls" if tool_call_indices else _map_gemini_finish_reason(finish_reason_raw)
        finish_chunk = _make_stream_chunk(model=model, finish_reason=mapped)
        # Attach usage from this event's usageMetadata so the streaming
        # loop in run_agent.py can record token counts (mirrors the
        # non-streaming path in translate_gemini_response).
        usage_meta = event.get("usageMetadata") or {}
        if usage_meta:
            finish_chunk.usage = SimpleNamespace(
                prompt_tokens=int(usage_meta.get("promptTokenCount") or 0),
                completion_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
                total_tokens=int(usage_meta.get("totalTokenCount") or 0),
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=int(usage_meta.get("cachedContentTokenCount") or 0),
                ),
            )
        chunks.append(finish_chunk)
    return chunks


def gemini_http_error(
    response: httpx.Response, *, body_text: Optional[str] = None
) -> GeminiAPIError:
    status = response.status_code
    body_json: Dict[str, Any] = {}
    if body_text is None:
        try:
            body_text = response.text
        except Exception:
            body_text = ""
    body_text = body_text or ""
    if body_text:
        try:
            parsed = json.loads(body_text)
            if isinstance(parsed, dict):
                body_json = parsed
        except (ValueError, TypeError):
            body_json = {}

    err_obj = body_json.get("error") if isinstance(body_json, dict) else None
    if not isinstance(err_obj, dict):
        err_obj = {}
    err_status = str(err_obj.get("status") or "").strip()
    err_message = str(err_obj.get("message") or "").strip()
    _raw_details = err_obj.get("details")
    details_list = _raw_details if isinstance(_raw_details, list) else []

    reason = ""
    retry_after: Optional[float] = None
    metadata: Dict[str, Any] = {}
    violations: List[Dict[str, Any]] = []
    help_links: List[Dict[str, Any]] = []
    for detail in details_list:
        if not isinstance(detail, dict):
            continue
        type_url = str(detail.get("@type") or "")
        if not reason and type_url.endswith("/google.rpc.ErrorInfo"):
            reason_value = detail.get("reason")
            if isinstance(reason_value, str):
                reason = reason_value
            md = detail.get("metadata")
            if isinstance(md, dict):
                metadata = md
        elif type_url.endswith("/google.rpc.Help"):
            links = detail.get("links")
            if isinstance(links, list):
                help_links.extend([lnk for lnk in links if isinstance(lnk, dict)])
        elif type_url.endswith("/google.rpc.RetryInfo"):
            retry_delay_str = str(detail.get("retryDelay") or "").strip()
            if retry_delay_str.endswith("s"):
                try:
                    retry_after = float(retry_delay_str[:-1])
                except ValueError:
                    pass
            elif retry_delay_str:
                try:
                    retry_after = float(retry_delay_str)
                except ValueError:
                    pass
        elif type_url.endswith("/google.rpc.QuotaFailure"):
            v_list = detail.get("violations")
            if isinstance(v_list, list):
                violations.extend([v for v in v_list if isinstance(v, dict)])

    challenge_url: Optional[str] = None
    for link in help_links:
        u = link.get("url")
        if isinstance(u, str) and u.strip():
            challenge_url = u.strip()
            break
    if not challenge_url and metadata:
        for k in ("challenge_url", "validation_url", "url", "verification_url"):
            val = metadata.get(k)
            if isinstance(val, str) and val.strip():
                challenge_url = val.strip()
                break

    header_retry = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header_retry and retry_after is None:
        try:
            retry_after = float(header_retry)
        except (TypeError, ValueError):
            retry_after = None

    reset_at = (time.time() + retry_after) if retry_after is not None else None

    code = f"gemini_http_{status}"
    if status == 401:
        code = "gemini_unauthorized"
    elif status == 403 and (reason == "VALIDATION_REQUIRED" or challenge_url is not None):
        code = "gemini_validation_required"
    elif status == 429:
        code = "gemini_rate_limited"
    elif status == 404:
        code = "gemini_model_not_found"

    if reason == "VALIDATION_REQUIRED" and challenge_url:
        message = f"Gemini HTTP 403 (VALIDATION_REQUIRED): Google verification required. Verify at: {challenge_url}"
    elif err_message:
        message = f"Gemini HTTP {status} ({err_status or 'error'}): {err_message}"
    else:
        message = f"Gemini returned HTTP {status}: {body_text[:500]}"

    # Free-tier quota exhaustion -> append actionable guidance so users who
    # bypassed the setup wizard (direct GOOGLE_API_KEY in .env) still learn
    # that the free tier cannot sustain an agent session.
    if status == 429 and is_free_tier_quota_error(err_message or body_text):
        message = message + _FREE_TIER_GUIDANCE

    # Legacy "Standard" Google Cloud key rejection (June 19, 2026 onward) ->
    # Google's raw 401 misleadingly tells the user to use OAuth. Append the
    # actual fix (mint a new Gemini API key in AI Studio).
    req_url = str(getattr(response, "url", "") or "")
    if is_standard_key_auth_error(status, err_message or body_text, reason, url=req_url):
        message = message + _STANDARD_KEY_GUIDANCE

    return GeminiAPIError(
        message,
        code=code,
        status_code=status,
        response=response,
        retry_after=retry_after,
        details={
            "status": err_status,
            "reason": reason,
            "metadata": metadata,
            "message": err_message,
            "retry_after": retry_after,
            "reset_at": reset_at,
            "violations": violations,
            "challenge_url": challenge_url,
            "help_links": help_links,
        },
    )


class _GeminiChatCompletions:
    def __init__(self, client: "GeminiNativeClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _AsyncGeminiChatCompletions:
    def __init__(self, client: "AsyncGeminiNativeClient"):
        self._client = client

    async def create(self, **kwargs: Any) -> Any:
        return await self._client._create_chat_completion(**kwargs)


class _GeminiChatNamespace:
    def __init__(self, client: Any):
        self.completions = _GeminiChatCompletions(client)
        self._client = client

    def count_tokens(self, **kwargs: Any) -> int:
        if hasattr(self._client, "count_tokens"):
            return self._client.count_tokens(**kwargs)
        raise NotImplementedError("count_tokens is not supported on this client")


class _AsyncGeminiChatNamespace:
    def __init__(self, client: Any):
        self.completions = _AsyncGeminiChatCompletions(client)
        self._client = client

    async def count_tokens(self, **kwargs: Any) -> int:
        if hasattr(self._client, "count_tokens"):
            return await self._client.count_tokens(**kwargs)
        raise NotImplementedError("count_tokens is not supported on this client")


class GeminiNativeClient:
    """Minimal OpenAI-SDK-compatible facade over Gemini's native REST API."""

    # Declared for agent/auxiliary_client.py: already a complete client, so it
    # is never re-dispatched through a wire adapter. (No HERMES_SKIP_ASYNC_WRAP
    # — the async path has a real conversion, AsyncGeminiNativeClient.)
    HERMES_SKIP_TRANSPORT_WRAP = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
        timeout: Any = None,
        http_client: Optional[httpx.Client] = None,
        **_: Any,
    ) -> None:
        if not (api_key or "").strip():
            raise RuntimeError(
                "Gemini native client requires an API key, but none was provided. "
                "Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment / ~/.hermes/.env "
                "(get one at https://aistudio.google.com/app/apikey), or run `hermes setup` "
                "to configure the Google provider."
            )
        self.api_key = api_key
        normalized_base = (base_url or DEFAULT_GEMINI_BASE_URL).rstrip("/")
        if normalized_base.endswith("/openai"):
            normalized_base = normalized_base[: -len("/openai")]
        self.base_url = normalized_base
        self._default_headers = dict(default_headers or {})
        self.chat = _GeminiChatNamespace(self)
        self.is_closed = False
        self._http = http_client or httpx.Client(
            timeout=timeout or httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=30.0)
        )

    def close(self) -> None:
        self.is_closed = True
        try:
            self._http.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": self.api_key,
            # Include Hermes client context following Gemini's partner
            # integration guidance.
            # See https://ai.google.dev/gemini-api/docs/partner-integration
            "User-Agent": f"hermes-agent/{_HERMES_VERSION} (gemini-native)",
            "X-Goog-Api-Client": f"hermes-agent/{_HERMES_VERSION}",
        }
        headers.update(self._default_headers)
        return headers

    @staticmethod
    def _advance_stream_iterator(iterator: Iterator[_GeminiStreamChunk]) -> tuple[bool, Optional[_GeminiStreamChunk]]:
        try:
            return False, next(iterator)
        except StopIteration:
            return True, None

    def _create_chat_completion(
        self,
        *,
        model: str = "gemini-3.6-flash",
        messages: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
        tools: Any = None,
        tool_choice: Any = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        stop: Any = None,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> Any:
        thinking_config = None
        media_resolution = None
        if isinstance(extra_body, dict):
            thinking_config = extra_body.get("thinking_config") or extra_body.get("thinkingConfig")
            media_resolution = extra_body.get("media_resolution") or extra_body.get("mediaResolution")
        if not media_resolution:
            media_resolution = kwargs.get("media_resolution") or kwargs.get("mediaResolution")

        request = build_gemini_request(
            messages=messages or [],
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            thinking_config=thinking_config,
            media_resolution=media_resolution,
            model=model,
        )

        bare_model = bare_gemini_model_id(model)
        if stream:
            return self._stream_completion(model=bare_model, request=request, timeout=timeout)

        url = f"{self.base_url}/models/{bare_model}:generateContent"
        response = self._http.post(url, json=request, headers=self._headers(), timeout=timeout)
        if response.status_code != 200:
            raise gemini_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiAPIError(
                f"Invalid JSON from Gemini native API: {exc}",
                code="gemini_invalid_json",
                status_code=response.status_code,
                response=response,
            ) from exc
        return translate_gemini_response(payload, model=bare_model)

    def count_tokens(
        self,
        contents: Any,
        *,
        model: str = "gemini-3.6-flash",
        system_instruction: Any = None,
        tools: Any = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> int:
        """Call Gemini REST :countTokens endpoint to get exact token count."""
        bare_model = bare_gemini_model_id(model)
        req_payload: Dict[str, Any] = {}

        if isinstance(contents, str):
            req_payload["contents"] = [{"role": "user", "parts": [{"text": contents}]}]
        elif isinstance(contents, list):
            if contents and isinstance(contents[0], dict) and ("role" in contents[0]) and ("parts" not in contents[0]):
                c_list, sys_inst = _build_gemini_contents(
                    contents,
                    include_tool_call_ids=gemini_requires_tool_call_ids(bare_model),
                    model=bare_model,
                )
                req_payload["contents"] = c_list
                if sys_inst and not system_instruction:
                    req_payload["systemInstruction"] = sys_inst
            else:
                req_payload["contents"] = contents
        elif isinstance(contents, dict):
            req_payload["contents"] = [contents]
        else:
            req_payload["contents"] = []

        if system_instruction:
            if isinstance(system_instruction, str):
                req_payload["systemInstruction"] = {
                    "role": "system",
                    "parts": [{"text": system_instruction}],
                }
            elif isinstance(system_instruction, dict):
                req_payload["systemInstruction"] = system_instruction
            elif isinstance(system_instruction, list):
                req_payload["systemInstruction"] = {
                    "role": "system",
                    "parts": [{"text": _coerce_content_to_text(system_instruction)}],
                }

        if tools:
            if isinstance(tools, list) and tools and isinstance(tools[0], dict) and "functionDeclarations" in tools[0]:
                req_payload["tools"] = tools
            else:
                gemini_tools = _translate_tools_to_gemini(tools)
                if gemini_tools:
                    req_payload["tools"] = gemini_tools

        url = f"{self.base_url}/models/{bare_model}:countTokens"
        response = self._http.post(
            url,
            params={"key": self.api_key},
            json=req_payload,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code != 200:
            raise gemini_http_error(response)

        try:
            payload = response.json()
            return int(payload.get("totalTokens", 0))
        except (ValueError, TypeError) as exc:
            raise GeminiAPIError(
                f"Invalid JSON from Gemini countTokens API: {exc}",
                code="gemini_invalid_json",
                status_code=response.status_code,
                response=response,
            ) from exc

    def _stream_completion(self, *, model: str, request: Dict[str, Any], timeout: Any = None) -> Iterator[_GeminiStreamChunk]:
        url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse"
        stream_headers = dict(self._headers())
        stream_headers["Accept"] = "text/event-stream"

        def _generator() -> Iterator[_GeminiStreamChunk]:
            try:
                with self._http.stream("POST", url, json=request, headers=stream_headers, timeout=timeout) as response:
                    if response.status_code != 200:
                        body_text = read_streaming_error_body(response)
                        raise gemini_http_error(response, body_text=body_text)
                    tool_call_indices: Dict[str, Dict[str, Any]] = {}
                    for event in _iter_sse_events(response):
                        for chunk in translate_stream_event(event, model, tool_call_indices):
                            yield chunk
            except httpx.HTTPError as exc:
                raise GeminiAPIError(
                    f"Gemini streaming request failed: {exc}",
                    code="gemini_stream_error",
                ) from exc

        return _generator()


class AsyncGeminiNativeClient:
    """Async wrapper used by auxiliary_client for native Gemini calls."""

    def __init__(self, sync_client: GeminiNativeClient):
        self._sync = sync_client
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self.chat = _AsyncGeminiChatNamespace(self)
        # Expose the underlying sync client as _real_client so the auxiliary
        # cache's eviction-by-leaf-client helper (#23482) can find and drop
        # this async entry when the sync GeminiNativeClient is poisoned.
        # GeminiNativeClient is itself the leaf (no OpenAI client beneath
        # it), so we point at the sync_client directly.
        self._real_client = sync_client

    async def _create_chat_completion(self, **kwargs: Any) -> Any:
        stream = bool(kwargs.get("stream"))
        result = await asyncio.to_thread(self._sync.chat.completions.create, **kwargs)
        if not stream:
            return result

        async def _async_stream() -> Any:
            while True:
                done, chunk = await asyncio.to_thread(self._sync._advance_stream_iterator, result)
                if done:
                    break
                yield chunk

        return _async_stream()

    async def count_tokens(
        self,
        contents: Any,
        *,
        model: str = "gemini-3.6-flash",
        system_instruction: Any = None,
        tools: Any = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> int:
        """Async wrapper over GeminiNativeClient.count_tokens."""
        return await asyncio.to_thread(
            self._sync.count_tokens,
            contents,
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            timeout=timeout,
            **kwargs,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)
