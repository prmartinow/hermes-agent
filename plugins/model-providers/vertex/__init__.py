"""Google Vertex AI provider profile via Google Cloud's OpenAI-compatible endpoint.

Auth is OAuth2 (service-account JSON or ADC), not a static key: ``agent/vertex_adapter.py``
mints ``(token, base_url)`` and the token is passed as ``api_key``. ``auth_type="vertex"``
keeps it out of the api_key provider path so a credentials-file path is never mistaken for a key.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class VertexProfile(ProviderProfile):
    """Vertex AI provider profile."""

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        """Translate partner-model reasoning to the Vertex-compatible request body."""
        model = str(context.get("model") or "").lower()
        reasoning = context.get("reasoning_config")
        if "claude" not in model or not isinstance(reasoning, dict):
            return {}
        effort = str(reasoning.get("effort", "medium") or "medium").strip().lower()
        if reasoning.get("enabled") is False or effort == "none":
            config = {"include_thoughts": False}
        else:
            budgets = {"minimal": 1024, "low": 1024, "medium": 4096, "high": 16384, "max": 24576, "ultra": 32768}
            config = {"include_thoughts": True, "thinking_budget": budgets.get(effort, 4096)}
        return {"extra_body": {"google": {"thinking_config": config}}}


vertex = VertexProfile(
    name="vertex", aliases=("google-vertex", "vertex-ai", "gcp-vertex", "vertexai"), api_mode="chat_completions",
    env_vars=(),  # OAuth2 via service account / ADC — not a static key env var
    base_url="https://aiplatform.googleapis.com",  # real base_url computed at runtime
    auth_type="vertex", default_aux_model="claude-haiku-4-5",
    supports_model_listing=False,  # no /models route on the OpenAI-compat endpoint; setup ships a curated list
)

register_provider(vertex)
