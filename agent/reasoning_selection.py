"""Runtime EffortByBase state and shared effective reasoning effort resolution.

Implements the Antigravity (agy v1.2.13) EffortByBase runtime memory model:
  1. Process/session memory keyed strictly by canonical Cloud Code base models.
  2. Strict validation against declared/discovered selectable efforts.
  3. Precedence hierarchy:
       runtime effort_by_base
       > config reasoning_overrides
       > global config reasoning_effort
       > model default
  4. Isolation: non-Cloud-Code routes (OpenRouter, direct Gemini, custom) preserve
     standard generic reasoning resolution without interference.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.gemini_cloudcode_models import (
    _CLOUDCODE_ACCOUNT_PROVIDERS,
    _CLOUDCODE_EFFORT_PROVIDERS,
    get_model_capability,
    parse_model_slug,
    selectable_reasoning_efforts,
)
from hermes_constants import (
    resolve_per_model_reasoning_effort,
    resolve_reasoning_config,
)


def canonical_reasoning_base(provider: str, model: str) -> str | None:
    """Return the canonical base model string if the route is a recognized Cloud Code provider.

    Returns None for non-Cloud-Code routes (e.g. openrouter, openai, anthropic, custom, direct gemini).
    Canonicalizes legacy aliases and vendor prefixes (e.g. 'gemini-3.8-flash-high' -> 'gemini-3.8-flash').
    """
    prov = (provider or "").strip().lower()
    is_cloudcode = (
        prov in _CLOUDCODE_EFFORT_PROVIDERS
        or prov in _CLOUDCODE_ACCOUNT_PROVIDERS
    )
    if not is_cloudcode:
        return None

    parsed = parse_model_slug(model)
    return parsed.base_model


def remember_reasoning_effort(
    effort_by_base: dict[str, str],
    *,
    provider: str,
    model: str,
    effort: str,
) -> bool:
    """Remember a chosen reasoning effort for the route's canonical base model in runtime state.

    Validates effort strictly against selectable_reasoning_efforts(provider, model).
    If valid: stores in effort_by_base and returns True.
    If invalid or non-Cloud-Code route: leaves effort_by_base unmutated and returns False.
    """
    canonical_base = canonical_reasoning_base(provider, model)
    if canonical_base is None:
        return False

    selectable = selectable_reasoning_efforts(provider, model)
    if not selectable or effort not in selectable:
        return False

    effort_by_base[canonical_base] = effort
    return True


def remembered_reasoning_effort(
    effort_by_base: Mapping[str, str] | None,
    *,
    provider: str,
    model: str,
) -> str | None:
    """Retrieve remembered effort for a model's canonical base from runtime state."""
    if not effort_by_base:
        return None
    canonical_base = canonical_reasoning_base(provider, model)
    if canonical_base is None:
        return None
    return effort_by_base.get(canonical_base)


def resolve_effective_reasoning_config(
    *,
    config: dict[str, Any] | None,
    provider: str,
    model: str,
    effort_by_base: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Resolve effective reasoning configuration for a model across runtime, config, and defaults.

    For Cloud Code routes with selectable efforts:
      Precedence:
        1. runtime effort_by_base[canonical_base] (if currently supported)
        2. config agent.reasoning_overrides[canonical_base] (if currently supported)
        3. config agent.reasoning_effort (if currently supported)
        4. model default effort (e.g. 'high')

    For Cloud Code models with no selectable effort (e.g. flash-lite, claude, gpt-oss):
      Returns None (no synthetic Gemini effort config invented).

    For non-Cloud-Code routes (e.g. openrouter, openai, custom):
      Preserves standard resolve_reasoning_config(config, model) semantics.
    """
    canonical_base = canonical_reasoning_base(provider, model)
    if canonical_base is None:
        return resolve_reasoning_config(config, model)

    selectable = selectable_reasoning_efforts(provider, model)
    if not selectable:
        return None

    # 1. Runtime memory
    if effort_by_base and canonical_base in effort_by_base:
        cand_runtime = effort_by_base[canonical_base]
        if cand_runtime in selectable:
            return {"enabled": True, "effort": cand_runtime}

    # 2. Config reasoning_overrides (passed with original model to preserve exact alias priority)
    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else {}
    if isinstance(agent_cfg, dict):
        overrides = agent_cfg.get("reasoning_overrides") or {}
        cand_override = resolve_per_model_reasoning_effort(model, overrides)
        if isinstance(cand_override, dict):
            if cand_override.get("enabled") is False:
                return {"enabled": False}
            cand_eff = cand_override.get("effort")
            if cand_eff in selectable:
                return {"enabled": True, "effort": cand_eff}

    # 3. Global config reasoning_effort (supports string or structured dict config)
    if isinstance(agent_cfg, dict):
        from hermes_constants import parse_reasoning_effort
        parsed_global = parse_reasoning_effort(agent_cfg.get("reasoning_effort"))
        if isinstance(parsed_global, dict):
            if parsed_global.get("enabled") is False:
                return {"enabled": False}
            global_eff = parsed_global.get("effort")
            if global_eff in selectable:
                return {"enabled": True, "effort": global_eff}

    # 4. Model default from capability registry
    cap = get_model_capability(canonical_base)
    if cap and cap.default_effort and cap.default_effort in selectable:
        return {"enabled": True, "effort": cap.default_effort}
    elif selectable:
        return {"enabled": True, "effort": selectable[-1]}

    return None


def reasoning_effort_error(
    provider: str,
    model: str,
    effort: str,
) -> str | None:
    """Validate an explicit reasoning effort for a target route/model.

    Returns an error message string if invalid, or None if valid.
    """
    if not effort:
        return None

    clean_effort = effort.lower().strip()
    canonical_base = canonical_reasoning_base(provider, model)
    if canonical_base is None:
        from hermes_constants import VALID_REASONING_EFFORTS
        valid_generic = set(VALID_REASONING_EFFORTS) | {"none"}
        if clean_effort not in valid_generic:
            return f"Unknown reasoning effort '{effort}' (valid: {', '.join(sorted(valid_generic))})"
        return None

    selectable = selectable_reasoning_efforts(provider, model)
    if selectable is None:
        return f"Reasoning effort is not supported for model '{model}'"

    if not selectable:
        return f"--reasoning is not supported for model '{model}'"

    parsed = parse_model_slug(model)
    if parsed.legacy_alias and parsed.effort and parsed.effort != clean_effort:
        return f"Conflicting reasoning effort: alias '{model}' implies '{parsed.effort}', but --reasoning specifies '{clean_effort}'"

    if clean_effort not in selectable:
        avail_str = ", ".join(selectable)
        return f"{canonical_base} has no '{effort}' effort (available: {avail_str})"

    return None
