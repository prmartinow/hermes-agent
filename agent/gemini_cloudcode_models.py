"""Canonical model effort resolver and capability registry for Gemini Cloud Code PA.

Decouples logical model identity from reasoning effort levels and outbound wire slugs.
Replaces brittle regex suffix parsing with an authoritative capability-driven registry
aligned with Google Antigravity CLI (agy v1.2.13) model resolution semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class EffortUnsupportedError(ValueError):
    """Raised when an unsupported reasoning effort is requested for a base model."""
    pass


@dataclass(frozen=True)
class EffortRoute:
    """Outbound transport destination and thinking configuration for a model tier."""
    wire_model: str
    thinking_level: str | None = None
    thinking_budget: int | None = None
    emit_thinking_config: bool = True

    def build_thinking_config(self) -> dict[str, Any] | None:
        """Construct generationConfig.thinkingConfig payload if applicable."""
        if not self.emit_thinking_config:
            return None
        cfg: dict[str, Any] = {}
        if self.thinking_level is not None:
            cfg["thinkingLevel"] = self.thinking_level
            cfg["includeThoughts"] = True
        elif self.thinking_budget is not None:
            cfg["thinkingBudget"] = self.thinking_budget
            cfg["includeThoughts"] = True
        return cfg if cfg else None


@dataclass(frozen=True)
class ModelCapability:
    """Declared capability envelope and supported reasoning efforts for a base model."""
    base_model: str
    display_name: str
    efforts: tuple[str, ...]
    routes: Mapping[str, EffortRoute]
    default_effort: str | None = None
    supports_thinking: bool = False
    supports_thought_circulation: bool | None = None
    max_tokens: int = 1048576
    max_output_tokens: int = 65536


@dataclass(frozen=True)
class ParsedModelSelection:
    """Result of parsing a user model input or legacy virtual model identifier."""
    base_model: str
    effort: str | None = None
    legacy_alias: bool = False


@dataclass(frozen=True)
class ResolvedModel:
    """Fully resolved logical model, chosen effort, wire slug, and thinking payload."""
    base_model: str
    effort: str | None
    wire_model: str
    thinking_config: dict[str, Any] | None = None


# Authoritative capability registry matching Antigravity v1.2.13 and Cloud Code PA
_MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    # Dynamic Tiered Models (Gemini 3.8 / 3.7 Flash)
    # Cloud Code PA Wire Slug: gemini-X.X-flash-tiered with generationConfig.thinkingLevel
    "gemini-3.8-flash": ModelCapability(
        base_model="gemini-3.8-flash",
        display_name="Gemini 3.8 Flash",
        efforts=("low", "medium", "high"),
        default_effort="high",  # Hermes compatibility default; aligns with thinkingBudget=-1
        supports_thinking=True,
        supports_thought_circulation=None,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "low": EffortRoute(wire_model="gemini-3.8-flash-tiered", thinking_level="low"),
            "medium": EffortRoute(wire_model="gemini-3.8-flash-tiered", thinking_level="medium"),
            "high": EffortRoute(wire_model="gemini-3.8-flash-tiered", thinking_level="high"),
        },
    ),
    "gemini-3.7-flash": ModelCapability(
        base_model="gemini-3.7-flash",
        display_name="Gemini 3.7 Flash",
        efforts=("low", "medium", "high"),
        default_effort="high",
        supports_thinking=True,
        supports_thought_circulation=None,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "low": EffortRoute(wire_model="gemini-3.7-flash-tiered", thinking_level="low"),
            "medium": EffortRoute(wire_model="gemini-3.7-flash-tiered", thinking_level="medium"),
            "high": EffortRoute(wire_model="gemini-3.7-flash-tiered", thinking_level="high"),
        },
    ),
    # Static Tiered Models (Gemini 3.6 Flash / 3.5 Flash)
    # The wire slug itself encodes the tier; thinkingConfig is not emitted on wire
    "gemini-3.6-flash": ModelCapability(
        base_model="gemini-3.6-flash",
        display_name="Gemini 3.6 Flash",
        efforts=("low", "medium", "high"),
        default_effort="high",
        supports_thinking=True,
        supports_thought_circulation=None,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "low": EffortRoute(wire_model="gemini-3.6-flash-low", thinking_budget=1000, emit_thinking_config=False),
            "medium": EffortRoute(wire_model="gemini-3.6-flash-medium", thinking_budget=4000, emit_thinking_config=False),
            "high": EffortRoute(wire_model="gemini-3.6-flash-high", thinking_budget=-1, emit_thinking_config=False),
        },
    ),
    "gemini-3.5-flash": ModelCapability(
        base_model="gemini-3.5-flash",
        display_name="Gemini 3.5 Flash",
        efforts=("low", "medium", "high"),
        default_effort="high",
        supports_thinking=True,
        supports_thought_circulation=None,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "low": EffortRoute(wire_model="gemini-3.5-flash-low", emit_thinking_config=False),
            "medium": EffortRoute(wire_model="gemini-3.5-flash-medium", emit_thinking_config=False),
            "high": EffortRoute(wire_model="gemini-3.5-flash-high", emit_thinking_config=False),
        },
    ),
    # Fixed-Tier Models (Gemini 3.1 Pro)
    # Exposes only low and high tiers
    "gemini-3.1-pro": ModelCapability(
        base_model="gemini-3.1-pro",
        display_name="Gemini 3.1 Pro",
        efforts=("low", "high"),
        default_effort="high",
        supports_thinking=True,
        supports_thought_circulation=None,
        max_tokens=1048576,
        max_output_tokens=65535,
        routes={
            "low": EffortRoute(wire_model="gemini-3.1-pro-low", emit_thinking_config=False),
            "high": EffortRoute(wire_model="gemini-3.1-pro-high", emit_thinking_config=False),
        },
    ),
    # Non-thinking Fast / Auxiliary Models
    "gemini-3.1-flash-lite": ModelCapability(
        base_model="gemini-3.1-flash-lite",
        display_name="Gemini 3.1 Flash Lite",
        efforts=(),
        default_effort=None,
        supports_thinking=False,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "": EffortRoute(wire_model="gemini-3.1-flash-lite", emit_thinking_config=False),
        },
    ),
    # 3P Partner Models (Claude / GPT-OSS) and Specialized Agent Endpoints
    "claude-sonnet-4-6": ModelCapability(
        base_model="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6 (Thinking)",
        efforts=(),
        default_effort=None,
        supports_thinking=True,
        max_tokens=250000,
        max_output_tokens=64000,
        routes={
            "": EffortRoute(wire_model="claude-sonnet-4-6", emit_thinking_config=False),
        },
    ),
    "claude-opus-4-6-thinking": ModelCapability(
        base_model="claude-opus-4-6-thinking",
        display_name="Claude Opus 4.6 (Thinking)",
        efforts=(),
        default_effort=None,
        supports_thinking=True,
        max_tokens=250000,
        max_output_tokens=64000,
        routes={
            "": EffortRoute(wire_model="claude-opus-4-6-thinking", emit_thinking_config=False),
        },
    ),
    "gpt-oss-120b-medium": ModelCapability(
        base_model="gpt-oss-120b-medium",
        display_name="GPT-OSS 120B (Medium)",
        efforts=(),
        default_effort=None,
        # Upstream catalog reports supportsThinking=True, but model has no user-selectable effort levels
        supports_thinking=True,
        max_tokens=131072,
        max_output_tokens=32768,
        routes={
            "": EffortRoute(wire_model="gpt-oss-120b-medium", emit_thinking_config=False),
        },
    ),
    "gemini-3-flash-agent": ModelCapability(
        base_model="gemini-3-flash-agent",
        display_name="Gemini 3 Flash Agent",
        efforts=(),
        default_effort=None,
        supports_thinking=False,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "": EffortRoute(wire_model="gemini-3-flash-agent", emit_thinking_config=False),
        },
    ),
    "gemini-pro-agent": ModelCapability(
        base_model="gemini-pro-agent",
        display_name="Gemini Pro Agent",
        efforts=(),
        default_effort=None,
        supports_thinking=False,
        max_tokens=1048576,
        max_output_tokens=65536,
        routes={
            "": EffortRoute(wire_model="gemini-pro-agent", emit_thinking_config=False),
        },
    ),
}

# Explicit mapping of legacy virtual model identifiers to (base_model, effort)
# Strictly mapped to verified aliases; unverified or arbitrary strings pass through verbatim
LEGACY_MODEL_ALIASES: dict[str, tuple[str, str]] = {
    # Gemini 3.8 Flash virtual tiers
    "gemini-3.8-flash-high": ("gemini-3.8-flash", "high"),
    "gemini-3.8-flash-medium": ("gemini-3.8-flash", "medium"),
    "gemini-3.8-flash-low": ("gemini-3.8-flash", "low"),
    "gemini-3.8-flash-tiered": ("gemini-3.8-flash", "high"),
    "gemini-3.8": ("gemini-3.8-flash", "high"),
    "gemini-3.8-thinking": ("gemini-3.8-flash", "high"),
    # Gemini 3.7 Flash virtual tiers
    "gemini-3.7-flash-high": ("gemini-3.7-flash", "high"),
    "gemini-3.7-flash-medium": ("gemini-3.7-flash", "medium"),
    "gemini-3.7-flash-low": ("gemini-3.7-flash", "low"),
    "gemini-3.7-flash-tiered": ("gemini-3.7-flash", "high"),
    "gemini-3.7": ("gemini-3.7-flash", "high"),
    "gemini-3.7-thinking": ("gemini-3.7-flash", "high"),
    # Gemini 3.6 Flash static tiers
    "gemini-3.6-flash-high": ("gemini-3.6-flash", "high"),
    "gemini-3.6-flash-medium": ("gemini-3.6-flash", "medium"),
    "gemini-3.6-flash-low": ("gemini-3.6-flash", "low"),
    "gemini-3.6-flash-thinking": ("gemini-3.6-flash", "high"),
    "gemini-3.6": ("gemini-3.6-flash", "high"),
    # Gemini 3.5 Flash static tiers
    "gemini-3.5-flash-high": ("gemini-3.5-flash", "high"),
    "gemini-3.5-flash-medium": ("gemini-3.5-flash", "medium"),
    "gemini-3.5-flash-low": ("gemini-3.5-flash", "low"),
    "gemini-3.5": ("gemini-3.5-flash", "high"),
    # Gemini 3.1 Pro tiers
    "gemini-3.1-pro-high": ("gemini-3.1-pro", "high"),
    "gemini-3.1-pro-low": ("gemini-3.1-pro", "low"),
    "gemini-3.1": ("gemini-3.1-pro", "high"),
}

# Known provider namespace prefixes to strip matching bare_gemini_model_id behavior
_KNOWN_PROVIDER_PREFIXES: tuple[str, ...] = (
    "google/", "gemini/", "gemini-oauth/", "gemini_oauth/",
    "gemini-antigravity/", "google-oauth/", "antigravity-gemini/",
    "gemini-1/", "gemini-2/", "gemini-3/", "gemini-4/", "gemini-5/",
    "gemini-oauth-1/", "gemini-oauth-2/", "gemini-oauth-3/", "gemini-oauth-4/", "gemini-oauth-5/",
)


def _strip_model_prefix(model: str) -> str:
    """Strip known Gemini provider namespace prefixes while strictly preserving custom vendor IDs."""
    name = str(model or "").strip()
    lowered = name.lower()
    for prefix in _KNOWN_PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix):].strip() or name
    return name


def parse_model_slug(model: str) -> ParsedModelSelection:
    """Parse user model input or legacy virtual model identifier into base model and effort.

    Exact registered legacy aliases (e.g. 'gemini-3.8-flash-high') decompose to
    (base_model='gemini-3.8-flash', effort='high', legacy_alias=True).
    Unregistered models or custom vendor models (e.g. 'vendor/custom-model') are preserved verbatim.
    """
    clean = _strip_model_prefix(model)
    if not clean:
        return ParsedModelSelection(base_model="", effort=None, legacy_alias=False)

    if clean in LEGACY_MODEL_ALIASES:
        base, eff = LEGACY_MODEL_ALIASES[clean]
        return ParsedModelSelection(base_model=base, effort=eff, legacy_alias=True)

    return ParsedModelSelection(base_model=clean, effort=None, legacy_alias=False)


def get_model_capability(base_model: str) -> ModelCapability | None:
    """Retrieve capability envelope for a canonical base model."""
    clean = _strip_model_prefix(base_model)
    return _MODEL_CAPABILITIES.get(clean)


def efforts_for_base(base_model: str) -> tuple[str, ...]:
    """Return tuple of supported reasoning effort strings for a base model.

    Returns empty tuple for models without effort controls or unknown models.
    """
    cap = get_model_capability(base_model)
    if cap is None:
        return ()
    return cap.efforts


def model_for_base_effort(
    base_model: str,
    effort: str | None = None,
    *,
    explicit: bool = True,
) -> ResolvedModel:
    """Resolve a canonical base model and effort into wire model and thinking config.

    Parameters:
      base_model: Canonical model ID (e.g. 'gemini-3.8-flash').
      effort: Desired reasoning effort ('low', 'medium', 'high').
      explicit: When True, requesting an effort not supported by the model raises
                EffortUnsupportedError. When False, uses model's default effort.

    Raises:
      EffortUnsupportedError: If effort is explicitly requested but not supported.
      RuntimeError: If capability registry has an internal route gap.
    """
    clean_base = _strip_model_prefix(base_model)
    cap = get_model_capability(clean_base)

    # 1. Models registered in capability registry
    if cap is not None:
        normalized_effort = str(effort).strip().lower() if effort is not None else None

        if normalized_effort:
            if normalized_effort not in cap.efforts:
                if explicit:
                    avail_str = ", ".join(cap.efforts) if cap.efforts else "none"
                    raise EffortUnsupportedError(
                        f"{clean_base} has no '{normalized_effort}' effort (available: {avail_str})"
                    )
                effective_effort = cap.default_effort
            else:
                effective_effort = normalized_effort
        else:
            effective_effort = cap.default_effort

        # Look up route
        route_key = effective_effort if (effective_effort and effective_effort in cap.routes) else ""
        if route_key in cap.routes:
            route = cap.routes[route_key]
        elif effective_effort in cap.routes:
            route = cap.routes[effective_effort]
        else:
            raise RuntimeError(
                f"Cloud Code model registry has no route for {clean_base!r} effort {effective_effort!r}"
            )

        return ResolvedModel(
            base_model=clean_base,
            effort=effective_effort,
            wire_model=route.wire_model,
            thinking_config=route.build_thinking_config(),
        )

    # 2. Unregistered / passthrough models (e.g. custom endpoints, vendor/models)
    eff_clean = str(effort).strip().lower() if effort else None
    if eff_clean and explicit:
        raise EffortUnsupportedError(f"{clean_base} has no '{eff_clean}' effort (available: none)")

    return ResolvedModel(
        base_model=clean_base,
        effort=eff_clean,
        wire_model=clean_base,
        thinking_config=None,
    )


def resolve_model_selection(
    model: str,
    effort: str | None = None,
    *,
    explicit_effort: bool | None = None,
) -> ResolvedModel:
    """Convenience entrypoint resolving model inputs (canonical or legacy) and effort.

    Precedence:
      1. Explicit effort argument (if passed).
      2. Legacy alias embedded effort (if model is a legacy alias like 'gemini-3.8-flash-high').
      3. Base model default effort.

    Detects and rejects contradictory inputs where a legacy alias declares one effort
    while explicit effort specifies a different one.
    """
    parsed = parse_model_slug(model)
    norm_effort = str(effort).strip().lower() if effort is not None else None

    # Contradiction check: e.g. model="gemini-3.8-flash-low", effort="high"
    if parsed.legacy_alias and parsed.effort and norm_effort:
        if parsed.effort != norm_effort:
            raise EffortUnsupportedError(
                f"Model alias '{model}' requests effort '{parsed.effort}', but explicit effort is '{norm_effort}'"
            )

    selected_effort = norm_effort or parsed.effort
    is_explicit = explicit_effort if explicit_effort is not None else bool(norm_effort or parsed.legacy_alias)

    return model_for_base_effort(
        parsed.base_model,
        selected_effort,
        explicit=is_explicit,
    )
@dataclass(frozen=True)
class DiscoveredModel:
    """Normalized result of an upstream Google Cloud Code PA catalog discovery record."""
    raw_model: str
    base_model: str
    available_efforts: tuple[str, ...]
    canonicalized: bool


def normalize_discovered_model(
    model_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> DiscoveredModel:
    """Normalize raw upstream Google Cloud Code PA model record into canonical base model and efforts.

    Enforces the closure invariant: discovery must never emit a canonical base that
    model_for_base_effort() cannot resolve.
    """
    minfo = metadata or {}
    mid_clean = _strip_model_prefix(model_id)
    if not mid_clean:
        return DiscoveredModel(raw_model="", base_model="", available_efforts=(), canonicalized=False)

    supports_thinking = bool(minfo.get("supportsThinking", False))
    thinking_budget = minfo.get("thinkingBudget", 0)

    # 1. Dynamic Tiered Models (e.g. gemini-3.8-flash-tiered, gemini-3.7-flash-tiered)
    # Fail-closed future-model rule: do NOT strip -tiered unless the resulting base exists in registry
    if mid_clean.endswith("-tiered") and supports_thinking and thinking_budget == -1:
        base_candidate = mid_clean[:-7]
        cap = get_model_capability(base_candidate)
        if cap is not None:
            return DiscoveredModel(
                raw_model=mid_clean,
                base_model=base_candidate,
                available_efforts=cap.efforts,
                canonicalized=True,
            )
        # Unregistered tiered model (e.g. gemini-4.2-ultra-tiered): fail-closed passthrough
        return DiscoveredModel(
            raw_model=mid_clean,
            base_model=mid_clean,
            available_efforts=(),
            canonicalized=False,
        )

    # 2. Static Tiered Models (e.g. gemini-3.6-flash-high, gemini-3.1-pro-high)
    if mid_clean in LEGACY_MODEL_ALIASES:
        base, eff = LEGACY_MODEL_ALIASES[mid_clean]
        cap = get_model_capability(base)
        if cap is not None and eff in cap.efforts:
            return DiscoveredModel(
                raw_model=mid_clean,
                base_model=base,
                available_efforts=(eff,),
                canonicalized=True,
            )

    # 3. Known Base Models (e.g. claude-sonnet-4-6, gpt-oss-120b-medium, gemini-3.1-flash-lite)
    cap = get_model_capability(mid_clean)
    if cap is not None:
        return DiscoveredModel(
            raw_model=mid_clean,
            base_model=mid_clean,
            available_efforts=cap.efforts,
            canonicalized=False,
        )

    # 4. Unknown, custom vendor models (e.g. vendor/custom-model, unverified legacy strings)
    return DiscoveredModel(
        raw_model=mid_clean,
        base_model=mid_clean,
        available_efforts=(),
        canonicalized=False,
    )
