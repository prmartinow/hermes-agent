"""Google Gemini (Antigravity) OAuth provider profile.

gemini-oauth: Google Gemini / Cloud Code PA (OAuth PKCE + Antigravity token bridge)

Uses GeminiCloudCodeClient to route inference through
cloudcode-pa.googleapis.com with CaGenerateContentRequest wrapping,
model reasoning level mapping, and real-time quota telemetry.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class GeminiOAuthProfile(ProviderProfile):
    """Gemini OAuth — Cloud Code PA transport, reasoning model mapping, and telemetry."""

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """Gemini Cloud Code PA routes reasoning level via model ID rather than extra_body."""
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict[str, Any] | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Forward selected reasoning effort into private Cloud Code adapter extra_body."""
        if not reasoning_config or not reasoning_config.get("enabled", True):
            return {}, {}

        from agent.gemini_cloudcode_models import parse_model_slug, get_model_capability

        clean_model = model or ""
        parsed = parse_model_slug(clean_model)
        if parsed.legacy_alias:
            return {}, {}

        cap = get_model_capability(parsed.base_model)
        if cap is None or not cap.efforts:
            return {}, {}

        effort = str(reasoning_config.get("effort") or "").strip().lower()
        if not effort or effort == "none":
            return {}, {}

        return {"effort": effort}, {}


gemini_oauth = GeminiOAuthProfile(
    name="gemini-oauth",
    aliases=("gemini-antigravity", "google-oauth", "antigravity-gemini", "gemini_oauth"),
    display_name="Google Gemini (OAuth / Antigravity)",
    description="Google Gemini via Antigravity OAuth & Cloud Code PA",
    signup_url="https://antigravity.google/",
    api_mode="chat_completions",
    env_vars=(),  # OAuth PKCE / token file — no static API key required
    base_url="https://cloudcode-pa.googleapis.com/v1internal",
    auth_type="oauth_external",
    supports_vision=True,
    supports_health_check=False,
    default_max_tokens=65536,
    default_aux_model="gemini-3.6-flash-low",
    native_reasoning_details_type="google.native_assistant",
    fallback_models=(
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3-flash-agent",
        "gemini-3.5-flash",
        "gemini-3.1-pro",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ),
)

register_provider(gemini_oauth)


def _handle_gs_slash(arg: str, **kwargs: Any) -> str:
    """Dynamic slash command handler for /gs, /gswitch, /gacc."""
    from hermes_cli.auth import handle_gs_command
    return handle_gs_command(arg)


def register(ctx: Any) -> None:
    """Register dynamic slash commands when loaded as a plugin."""
    if hasattr(ctx, "register_command"):
        for cmd_name in ("gs",):
            ctx.register_command(
                cmd_name,
                handler=_handle_gs_slash,
                description="Switch Gemini account for this chat by label",
                args_hint="<label>",
            )
