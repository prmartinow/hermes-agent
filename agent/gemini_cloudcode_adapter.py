"""OpenAI-SDK-compatible facade over Google's Cloud Code PA (Antigravity OAuth) API.

Provides GeminiCloudCodeClient and AsyncGeminiCloudCodeClient which route
requests through cloudcode-pa.googleapis.com using CaGenerateContentRequest
wrapping, model reasoning level mapping, and Bearer token auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import httpx

from agent.bounded_response import read_streaming_error_body
from agent.gemini_native_adapter import (
    DEFAULT_GEMINI_BASE_URL,
    GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
    _effective_gemini_max_output_tokens,
    GeminiAPIError,
    _GeminiChatNamespace,
    _GeminiStreamChunk,
    _build_gemini_contents,
    _coerce_content_to_text,
    _detect_mime_type,
    _extract_multimodal_parts,
    _iter_sse_events,
    _normalize_media_resolution,
    _resolve_media_to_inline_data,
    _tool_call_extra_signature,
    _translate_tool_call_to_gemini,
    _translate_tools_to_gemini,
    bare_gemini_model_id,
    build_gemini_request,
    gemini_http_error,
    gemini_requires_tool_call_ids,
    is_gemini_model,
    translate_gemini_response,
    translate_stream_event,
)

import platform as _platform
import shutil
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_CLOUDCODE_PA_BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"

_CACHED_USER_AGENT: Optional[str] = None


def get_antigravity_user_agent() -> str:
    """Dynamically resolve User-Agent matching the installed Antigravity CLI version and host platform."""
    global _CACHED_USER_AGENT
    if _CACHED_USER_AGENT:
        return _CACHED_USER_AGENT

    version = "1.1.22"
    agy_cmd = shutil.which("agy")
    if agy_cmd:
        try:
            res = subprocess.run([agy_cmd, "--version"], capture_output=True, text=True, timeout=1.0)
            if res.returncode == 0:
                v = res.stdout.strip()
                if v and len(v) < 20:
                    version = v
        except Exception:
            pass

    os_name = "linux" if _platform.system().lower() == "linux" else _platform.system().lower()
    arch_name = "amd64" if _platform.machine().lower() in ("x86_64", "amd64") else _platform.machine().lower()
    _CACHED_USER_AGENT = f"AntigravityCLI/{version}/auto ({os_name}; {arch_name}; terminal)"
    return _CACHED_USER_AGENT


def is_cloudcode_pa_base_url(base_url: str) -> bool:
    """Return True when the endpoint speaks Cloud Code PA v1internal API."""
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return "cloudcode-pa.googleapis.com" in normalized


def resolve_cloudcode_model_and_effort(model: str, effort: Optional[str] = None) -> str:
    """Resolve model slug and reasoning effort matching canonical Cloud Code resolver."""
    from agent.gemini_cloudcode_models import resolve_model_selection
    bare = bare_gemini_model_id(model).strip() or "gemini-3.7-flash"
    resolved = resolve_model_selection(bare, effort)
    return resolved.wire_model


class GeminiCloudCodeClient:
    """OpenAI-SDK-compatible client routing through Cloud Code PA v1internal."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
        base_url: Optional[str] = None,
        project_id: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
        timeout: Any = None,
        http_client: Optional[httpx.Client] = None,
        **_: Any,
    ) -> None:
        token = (access_token or api_key or "").strip()
        if not token:
            raise RuntimeError(
                "Gemini Cloud Code client requires an OAuth access token. "
                "Run `hermes auth add gemini-oauth` to log in via Google."
            )
        self.access_token = token
        self.api_key = token
        self.project_id = (project_id or "default-cli-project").strip()
        self.base_url = (base_url or DEFAULT_CLOUDCODE_PA_BASE_URL).rstrip("/")
        self._default_headers = dict(default_headers or {})
        self.chat = _GeminiChatNamespace(self)
        self.is_closed = False
        self._http = http_client or httpx.Client(
            timeout=timeout or httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=30.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0),
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
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": get_antigravity_user_agent(),
        }
        headers.update(self._default_headers)
        headers.pop("X-Goog-User-Project", None)
        headers.pop("x-goog-user-project", None)
        return headers

    @staticmethod
    def _advance_stream_iterator(iterator: Iterator[_GeminiStreamChunk]) -> tuple[bool, Optional[_GeminiStreamChunk]]:
        try:
            return False, next(iterator)
        except StopIteration:
            return True, None

    def _extract_cloudcode_reasoning_inputs(
        self,
        extra_body: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Extract normalized effort and optional caller-specified thinking_config."""
        from agent.gemini_cloudcode_models import EffortUnsupportedError

        extra = extra_body if isinstance(extra_body, dict) else {}
        scalar_effort = (
            extra.get("effort")
            or extra.get("reasoning_effort")
            or kwargs.get("effort")
            or kwargs.get("reasoning_effort")
        )
        thinking_config = (
            extra.get("thinking_config")
            or extra.get("thinkingConfig")
            or kwargs.get("thinking_config")
            or kwargs.get("thinkingConfig")
        )

        eff_str = str(scalar_effort).strip().lower() if scalar_effort is not None else None

        # Inspect explicit thinking_config for thinkingLevel and validate against scalar effort
        if isinstance(thinking_config, dict):
            level = thinking_config.get("thinkingLevel")
            if level is not None:
                level_str = str(level).strip().lower()
                if eff_str is not None and eff_str != "none" and eff_str != level_str:
                    raise EffortUnsupportedError(
                        f"Conflicting effort {eff_str!r} and thinkingLevel {level_str!r}"
                    )
                if eff_str is None:
                    eff_str = level_str

        if eff_str == "none":
            return "none", {"includeThoughts": False}

        return eff_str, thinking_config if isinstance(thinking_config, dict) else None

    def _resolve_model_route(
        self,
        model: str,
        extra_body: Optional[Dict[str, Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> tuple[Any, Optional[Dict[str, Any]]]:
        """Resolve model and effort atomically into ResolvedModel and effective thinkingConfig."""
        from agent.gemini_cloudcode_models import resolve_model_selection

        kw = kwargs or {}
        effort, caller_thinking = self._extract_cloudcode_reasoning_inputs(extra_body, kw)
        bare = bare_gemini_model_id(model).strip() or "gemini-3.7-flash"

        if effort == "none":
            resolved = resolve_model_selection(bare, effort=None)
            return resolved, {"includeThoughts": False}

        is_claude = "claude" in bare.lower()
        if is_claude:
            # Preserve existing Claude adapter budget handling until Claude effort registry is formalized
            resolved = resolve_model_selection(bare, effort=None)
        else:
            resolved = resolve_model_selection(bare, effort=effort)

        if caller_thinking is not None:
            effective_thinking = dict(caller_thinking)
            if "thinkingLevel" in effective_thinking and "includeThoughts" not in effective_thinking:
                effective_thinking["includeThoughts"] = True
        else:
            effective_thinking = resolved.thinking_config

        # Claude partner thinking budget fallback if not handled in registry
        bare_mapped = bare_gemini_model_id(resolved.wire_model).strip().lower()
        if "claude" in bare_mapped and effective_thinking is None and ("thinking" in bare_mapped or effort):
            budget_map = {"minimal": 1024, "low": 1024, "medium": 4096, "high": 16384, "max": 24576, "ultra": 32768}
            effective_thinking = {"thinkingBudget": budget_map.get(effort or "high", 4096), "includeThoughts": True}

        return resolved, effective_thinking

    def _map_model_id(
        self,
        model: str,
        extra_body: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """Extract and resolve wire model ID from model string and effort."""
        resolved, _ = self._resolve_model_route(model, extra_body, kwargs)
        return resolved.wire_model

    def _create_chat_completion(
        self,
        *,
        model: str = "gemini-3.6-flash-low",
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
        media_resolution = None
        if isinstance(extra_body, dict):
            media_resolution = extra_body.get("media_resolution") or extra_body.get("mediaResolution")
        if not media_resolution:
            media_resolution = kwargs.get("media_resolution") or kwargs.get("mediaResolution")

        resolved_model, thinking_config = self._resolve_model_route(model, extra_body, kwargs)
        mapped_model = resolved_model.wire_model
        bare_mapped = bare_gemini_model_id(mapped_model).strip().lower()

        effective_max_tokens = _effective_gemini_max_output_tokens(
            max_tokens, thinking_config, model=mapped_model
        )

        if thinking_config and isinstance(thinking_config, dict):
            tb = thinking_config.get("thinkingBudget")
            if isinstance(tb, (int, float)) and tb > 0 and effective_max_tokens is not None:
                if "claude" in bare_mapped:
                    if effective_max_tokens <= 1024:
                        thinking_config = {"includeThoughts": False}
                    else:
                        max_tb = max(1024, effective_max_tokens - 256) if effective_max_tokens >= 1024 + 256 else (effective_max_tokens - 1)
                        if int(tb) > max_tb:
                            thinking_config = {**thinking_config, "thinkingBudget": max_tb}
                elif int(tb) >= effective_max_tokens:
                    thinking_config = {**thinking_config, "thinkingBudget": max(0, effective_max_tokens - 256)}

        request = build_gemini_request(
            messages=messages or [],
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=effective_max_tokens,
            top_p=top_p,
            stop=stop,
            thinking_config=thinking_config,
            media_resolution=media_resolution,
            model=mapped_model,
        )

        cloudcode_body = {
            "model": mapped_model,
            "project": self.project_id or "default-cli-project",
            "user_prompt_id": f"hermes-prompt-{uuid.uuid4()}",
            "request": request,
        }

        if stream:
            return self._stream_completion(
                model=mapped_model,
                cloudcode_body=cloudcode_body,
                timeout=timeout,
            )

        url = f"{self.base_url}:generateContent"
        response = self._http.post(
            url,
            json=cloudcode_body,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code != 200:
            raise gemini_http_error(response)

        try:
            payload = response.json()
            # Unwrap Google CaGenerateContentResponse
            unwrapped = payload.get("response", payload)
        except ValueError as exc:
            raise GeminiAPIError(
                f"Invalid JSON from Cloud Code PA API: {exc}",
                code="cloudcode_invalid_json",
                status_code=response.status_code,
                response=response,
            ) from exc

        return translate_gemini_response(unwrapped, model=mapped_model)

    def count_tokens(
        self,
        contents: Any = None,
        *,
        model: str = "gemini-3.7-flash-tiered",
        messages: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Any = None,
        tools: Any = None,
        timeout: Any = None,
        effort: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        """Call Cloud Code PA :countTokens endpoint to get exact token count."""
        call_kwargs = dict(kwargs)
        extra_body = call_kwargs.pop("extra_body", None)
        if effort is not None:
            call_kwargs["effort"] = effort
        resolved, _ = self._resolve_model_route(
            model,
            extra_body,
            call_kwargs,
        )
        mapped_model = resolved.wire_model
        target_contents = contents if contents is not None else messages
        req_payload: Dict[str, Any] = {}

        if isinstance(target_contents, str):
            req_payload["contents"] = [{"role": "user", "parts": [{"text": target_contents}]}]
        elif isinstance(target_contents, list):
            if target_contents and isinstance(target_contents[0], dict) and ("role" in target_contents[0]) and ("parts" not in target_contents[0]):
                c_list, sys_inst = _build_gemini_contents(
                    target_contents,
                    include_tool_call_ids=gemini_requires_tool_call_ids(mapped_model),
                    model=mapped_model,
                )
                req_payload["contents"] = c_list
                if sys_inst and not system_instruction:
                    req_payload["systemInstruction"] = sys_inst
            else:
                req_payload["contents"] = target_contents
        elif isinstance(target_contents, dict):
            req_payload["contents"] = [target_contents]
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

        if mapped_model:
            req_payload["model"] = mapped_model

        cloudcode_body = {
            "request": req_payload,
        }

        url = f"{self.base_url}:countTokens"
        response = self._http.post(
            url,
            json=cloudcode_body,
            headers=self._headers(),
            timeout=timeout,
        )
        if response.status_code != 200:
            raise gemini_http_error(response)

        try:
            payload = response.json()
            unwrapped = payload.get("response", payload)
            return int(unwrapped.get("totalTokens", 0))
        except (ValueError, TypeError) as exc:
            raise GeminiAPIError(
                f"Invalid JSON from Cloud Code PA countTokens: {exc}",
                code="cloudcode_invalid_json",
                status_code=response.status_code,
                response=response,
            ) from exc

    def _stream_completion(
        self,
        *,
        model: str,
        cloudcode_body: Dict[str, Any],
        timeout: Any = None,
    ) -> Iterator[_GeminiStreamChunk]:
        url = f"{self.base_url}:streamGenerateContent?alt=sse"
        stream_headers = dict(self._headers())
        stream_headers["Accept"] = "text/event-stream"

        def _generator() -> Iterator[_GeminiStreamChunk]:
            try:
                with self._http.stream(
                    "POST",
                    url,
                    json=cloudcode_body,
                    headers=stream_headers,
                    timeout=timeout,
                ) as response:
                    if response.status_code != 200:
                        body_text = read_streaming_error_body(response)
                        if response.status_code == 400:
                            sync_url = f"{self.base_url}:generateContent"
                            sync_resp = self._http.post(
                                sync_url,
                                json=cloudcode_body,
                                headers=self._headers(),
                                timeout=timeout,
                            )
                            if sync_resp.status_code == 200:
                                payload = sync_resp.json()
                                unwrapped = payload.get("response", payload)
                                translated = translate_gemini_response(unwrapped, model=model)
                                msg = translated.choices[0].message if translated.choices else None
                                chunk = _GeminiStreamChunk(
                                    id=getattr(translated, "id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
                                    object="chat.completion.chunk",
                                    created=int(getattr(translated, "created", 0)),
                                    model=model,
                                    choices=[
                                        SimpleNamespace(
                                            index=0,
                                            delta=SimpleNamespace(
                                                role="assistant",
                                                content=getattr(msg, "content", None),
                                                tool_calls=getattr(msg, "tool_calls", None),
                                                reasoning=getattr(msg, "reasoning", None),
                                                reasoning_details=getattr(msg, "reasoning_details", None),
                                            ),
                                            finish_reason="stop",
                                        )
                                    ],
                                    usage=getattr(translated, "usage", None),
                                )
                                yield chunk
                                return
                        raise gemini_http_error(response, body_text=body_text)

                    tool_call_indices: Dict[str, Dict[str, Any]] = {}
                    from agent.native_replay import GoogleNativeStreamAccumulator
                    from agent.gemini_native_adapter import is_gemini_model
                    native_state = GoogleNativeStreamAccumulator() if is_gemini_model(model) else None
                    for raw_event in _iter_sse_events(response):
                        # Unwrap CaGenerateContentResponse container if wrapped
                        event = raw_event
                        if isinstance(raw_event, dict):
                            event = raw_event.get("response", raw_event)
                        for chunk in translate_stream_event(
                            event, model, tool_call_indices, native_stream_state=native_state
                        ):
                            yield chunk
            except httpx.HTTPError as exc:
                raise GeminiAPIError(
                    f"Cloud Code PA streaming request failed: {exc}",
                    code="cloudcode_stream_error",
                ) from exc

        return _generator()


class AsyncGeminiCloudCodeClient:
    """Async wrapper over GeminiCloudCodeClient matching OpenAI SDK AsyncClient signature."""

    def __init__(self, sync_client: GeminiCloudCodeClient):
        self._sync_client = sync_client
        self.chat = _AsyncGeminiCloudCodeChatNamespace(self)

    @property
    def is_closed(self) -> bool:
        return self._sync_client.is_closed

    @property
    def api_key(self) -> str:
        return self._sync_client.api_key

    @property
    def base_url(self) -> str:
        return self._sync_client.base_url

    async def count_tokens(
        self,
        contents: Any = None,
        *,
        model: str = "gemini-3.7-flash-tiered",
        messages: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Any = None,
        tools: Any = None,
        timeout: Any = None,
        effort: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        """Async wrapper over GeminiCloudCodeClient.count_tokens."""
        return await asyncio.to_thread(
            self._sync_client.count_tokens,
            contents=contents,
            model=model,
            messages=messages,
            system_instruction=system_instruction,
            tools=tools,
            timeout=timeout,
            effort=effort,
            **kwargs,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._sync_client.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class _AsyncGeminiCloudCodeChatNamespace:
    def __init__(self, client: AsyncGeminiCloudCodeClient):
        self.completions = _AsyncGeminiCloudCodeCompletions(client)
        self._client = client

    async def count_tokens(
        self,
        contents: Any = None,
        *,
        model: str = "gemini-3.7-flash-tiered",
        messages: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Any = None,
        tools: Any = None,
        timeout: Any = None,
        effort: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        return await self._client.count_tokens(
            contents=contents,
            model=model,
            messages=messages,
            system_instruction=system_instruction,
            tools=tools,
            timeout=timeout,
            effort=effort,
            **kwargs,
        )


class _AsyncGeminiCloudCodeCompletions:
    def __init__(self, client: AsyncGeminiCloudCodeClient):
        self._client = client

    async def create(self, **kwargs: Any) -> Any:
        sync_client = self._client._sync_client
        is_stream = kwargs.get("stream", False)
        if not is_stream:
            return await asyncio.to_thread(sync_client._create_chat_completion, **kwargs)

        stream_iterator = await asyncio.to_thread(sync_client._create_chat_completion, **kwargs)

        async def _async_generator():
            while True:
                done, chunk = await asyncio.to_thread(
                    sync_client._advance_stream_iterator, stream_iterator
                )
                if done:
                    break
                yield chunk

        return _async_generator()
