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
    fallback_models=(
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3-flash-agent",
        "gemini-3.5-flash-low",
        "gemini-3.5-flash-extra-low",
        "gemini-pro-agent",
        "gemini-3.1-pro-low",
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
