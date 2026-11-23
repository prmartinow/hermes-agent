"""Read already-cached output limits for OpenAI-compatible endpoints."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def get_model_max_output_tokens(model: str, provider: str) -> int:
    """Resolve output limits from upstream capability metadata without network access."""
    from agent.model_metadata import strip_codex_context_variant_suffix
    from agent.models_dev import get_model_capabilities

    # The selected model may have its own catalog entry. Only consult the wire model when the
    # selected alias lacks a limit (e.g. an upstream Codex context-window variant).
    selected = (model or "").strip()
    try:
        for candidate in dict.fromkeys((selected, strip_codex_context_variant_suffix(selected))):
            caps = get_model_capabilities(provider, candidate, allow_network=False)
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
