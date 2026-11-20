"""Read already-cached output limits for OpenAI-compatible endpoints."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("agent.model_metadata")


def get_model_max_output_tokens(model: str = "", provider: str = "", config_max_tokens: int | None = None) -> int:
    """Return the explicit, known native, or cached model output limit without network access."""
    if config_max_tokens is not None and int(config_max_tokens) > 0:
        return int(config_max_tokens)

    from agent.model_metadata import strip_codex_context_variant_suffix

    norm_provider = (provider or "").strip().lower()
    bare_model = strip_codex_context_variant_suffix((model or "").strip().lower())
    if "claude" in bare_model or "anthropic" in norm_provider:
        if any(v in bare_model for v in ("opus-4-6", "sonnet-4-6", "3-7", "3.7", "4-")):
            return 64000
        return 8192
    if "gpt-4o" in bare_model:
        return 16384
    try:
        from agent.models_dev import get_model_capabilities

        caps = get_model_capabilities(provider, bare_model, allow_network=False)
        if caps and caps.max_output_tokens:
            return caps.max_output_tokens
    except Exception:
        logger.debug("Could not resolve cached output-token metadata", exc_info=True)
    return 0


def cached_endpoint_output_limit(model: str, base_url: str, api_key: object = "") -> int:
    """Return an exact model's fresh cached output limit, without network or credential minting."""
    from agent.model_metadata import (
        _ENDPOINT_MODEL_CACHE_TTL,
        _endpoint_memo_key,
        _endpoint_model_metadata_cache,
        _endpoint_model_metadata_cache_time,
        _normalize_base_url,
    )

    if not model or not base_url:
        return 0
    key = _endpoint_memo_key(_normalize_base_url(base_url), api_key)
    if time.time() - _endpoint_model_metadata_cache_time.get(key, 0) >= _ENDPOINT_MODEL_CACHE_TTL:
        return 0
    models = _endpoint_model_metadata_cache.get(key, {})
    entry = models.get(model)
    value = entry.get("max_completion_tokens") if isinstance(entry, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
