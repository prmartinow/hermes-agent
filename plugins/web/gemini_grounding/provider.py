"""Gemini Grounding (Google Search) web search provider.

Provides first-party Google Search Grounding via Google Cloud Code PA v1internal API,
reusing Hermes's authenticated Gemini OAuth credential pool.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_DEFAULT_GROUNDING_TIMEOUT_SECS = 20.0


class GeminiGroundingWebSearchProvider(WebSearchProvider):
    """First-party Google Search Grounding provider via Gemini OAuth."""

    @property
    def name(self) -> str:
        return "gemini-grounding"

    @property
    def display_name(self) -> str:
        return "Google Search Grounding (Gemini OAuth)"

    def is_available(self) -> bool:
        """Return True when at least one Gemini OAuth account is authenticated."""
        try:
            from hermes_cli.auth import has_gemini_oauth_credentials
            for acc in range(1, 6):
                if has_gemini_oauth_credentials(acc):
                    return True
            return False
        except Exception:
            return False

    def is_keyless_available(self) -> bool:
        """Return True if Gemini OAuth credentials exist (zero external search API keys needed)."""
        return self.is_available()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Google Search Grounding query via Cloud Code PA.

        Returns standard Hermes search output:
        {"success": True, "data": {"web": [{title, url, description, position}, ...]}}
        """
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return {"success": False, "error": "Interrupted"}
        except Exception:
            pass

        cleaned_query = (query or "").strip()
        if not cleaned_query:
            return {"success": True, "data": {"web": []}}

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = min(max(limit, 1), 50)

        # Resolve credentials from Hermes Gemini OAuth pool
        try:
            from hermes_cli.auth import (
                DEFAULT_GEMINI_OAUTH_BASE_URL,
                resolve_gemini_oauth_runtime_credentials,
            )
            from agent.gemini_cloudcode_adapter import get_antigravity_user_agent
        except ImportError as exc:
            return {"success": False, "error": f"Hermes Gemini OAuth modules unavailable: {exc}"}

        # Attempt primary account with fallback across available accounts on 429
        last_error = "No Gemini OAuth credentials available"
        for acc_idx in range(1, 6):
            try:
                creds = resolve_gemini_oauth_runtime_credentials(acc_idx, refresh_if_expiring=True)
            except Exception:
                continue

            token = creds.get("api_key") or creds.get("access_token")
            if not token:
                continue

            base_url = creds.get("base_url") or DEFAULT_GEMINI_OAUTH_BASE_URL
            project_id = creds.get("project_id") or "default-cli-project"

            prompt = (
                f"Search query: \"{cleaned_query}\".\n"
                f"Provide the top {limit} relevant search results. For each result, include title, URL, and a brief description."
            )

            payload = {
                "model": "gemini-3.7-flash-tiered",
                "project": project_id,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "tools": [{"googleSearch": {}}],
                    "generationConfig": {
                        "maxOutputTokens": 1024,
                        "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": False},
                    },
                },
            }

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": get_antigravity_user_agent(),
            }

            try:
                with httpx.Client(timeout=_DEFAULT_GROUNDING_TIMEOUT_SECS) as client:
                    resp = client.post(f"{base_url}:generateContent", headers=headers, json=payload)

                if resp.status_code == 429:
                    last_error = f"Account {acc_idx} rate limited (HTTP 429)"
                    logger.warning("Gemini Grounding account %d hit rate limit, trying next account", acc_idx)
                    continue

                if resp.status_code != 200:
                    last_error = f"Cloud Code PA error (HTTP {resp.status_code}): {resp.text[:200]}"
                    continue

                data = resp.json()
                res = data.get("response", data)
                candidates = res.get("candidates", [])
                if not candidates:
                    return {"success": True, "data": {"web": []}}

                cand = candidates[0]
                gm = cand.get("groundingMetadata", {})
                chunks = gm.get("groundingChunks", [])
                supports = gm.get("groundingSupports", [])

                # Map chunk index to supporting snippet text
                chunk_snippets: Dict[int, str] = {}
                for sup in supports:
                    indices = sup.get("groundingChunkIndices", [])
                    seg = sup.get("segment", {})
                    text = (seg.get("text") or "").strip()
                    if text:
                        for c_idx in indices:
                            if c_idx not in chunk_snippets:
                                chunk_snippets[c_idx] = text
                            elif len(chunk_snippets[c_idx]) < 300:
                                chunk_snippets[c_idx] += " " + text

                web_results: List[Dict[str, Any]] = []
                seen_urls = set()

                for idx, chunk in enumerate(chunks):
                    web = chunk.get("web", {})
                    uri = (web.get("uri") or "").strip()
                    title = (web.get("title") or "").strip()
                    if not uri or uri in seen_urls:
                        continue
                    seen_urls.add(uri)

                    desc = chunk_snippets.get(idx, "").strip()
                    if not desc:
                        desc = title

                    web_results.append({
                        "title": title or uri,
                        "url": uri,
                        "description": desc[:500],
                        "position": len(web_results) + 1,
                    })

                    if len(web_results) >= limit:
                        break

                logger.info(
                    "Gemini Grounding search %s: retrieved %d results via account %d",
                    cleaned_query, len(web_results), acc_idx,
                )
                return {"success": True, "data": {"web": web_results}}

            except httpx.TimeoutException:
                last_error = f"Gemini Grounding request timed out after {_DEFAULT_GROUNDING_TIMEOUT_SECS}s"
                continue
            except Exception as exc:
                last_error = f"Gemini Grounding request error: {exc}"
                continue

        return {"success": False, "error": last_error}


class GoogleGroundingWebSearchProvider(GeminiGroundingWebSearchProvider):
    """Alias for GeminiGroundingWebSearchProvider under google-grounding."""

    @property
    def name(self) -> str:
        return "google-grounding"
