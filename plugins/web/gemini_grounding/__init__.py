"""Gemini Grounding (Google Search) web search plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from plugins.web.gemini_grounding.provider import (
    GeminiGroundingWebSearchProvider,
    GoogleGroundingWebSearchProvider,
)

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext


def register(ctx: PluginContext) -> None:
    """Register the Gemini Grounding providers with the web search registry."""
    ctx.register_web_search_provider(GeminiGroundingWebSearchProvider())
    ctx.register_web_search_provider(GoogleGroundingWebSearchProvider())
