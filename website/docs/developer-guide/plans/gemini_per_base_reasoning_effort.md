# Gemini Per-Base Reasoning-Effort Memory & Model-Switch Semantics

> **Status**: Implemented / Historical Plan
> **Authoritative Runtime Documentation**: [Gemini Cloud Code Runtime Architecture](../gemini-cloud-code-runtime.md)
> *Note: This document records the original Action Item 3 design plan and is retained for historical implementation reference. For current normative architecture, consult the authoritative guide linked above.*

**Document Path**: `website/docs/developer-guide/plans/gemini_per_base_reasoning_effort.md`
**Specification Reference**: `agent/reasoning_selection.py`, `agent/gemini_cloudcode_models.py`, `tui_gateway/model_switch.py`, `tui_gateway/server.py`, `hermes_cli/cli_model_switch_mixin.py`

---

## 1. Executive Summary & Problem Statement

Google Cloud Code PA models adhere to three distinct model/effort wire taxonomies:
1. **Dynamic Tiered Models** (`gemini-3.8-flash`, `gemini-3.7-flash`): Identified upstream during discovery via `supportsThinking: true` and `thinkingBudget: -1`. Outbound requests route to `*-tiered` wire models (`gemini-3.8-flash-tiered`, `gemini-3.7-flash-tiered`) with structured `thinkingConfig: {"thinkingLevel": <level>, "includeThoughts": true}`.
2. **Static Tiered Models** (`gemini-3.6-flash`): Route to separate static wire slugs (`gemini-3.6-flash-low`, `gemini-3.6-flash-medium`, `gemini-3.6-flash-high`) with `thinkingConfig: None`.
3. **Fixed-Tier Models** (`gemini-3.1-pro`): Support `low` and `high` effort via distinct wire slugs (`gemini-3.1-pro-low`, `gemini-3.1-pro-high`), while partner models (`claude-sonnet-4-6`, `gpt-oss-120b-medium`, `gemini-3.1-flash-lite`) reject reasoning effort controls entirely.

Historically, Hermes suffered from four major inconsistencies across model switches:
1. **Effort Bleed Across Model Switches**: Switching from a high-effort model to a different model leaked the previous model's reasoning effort into the destination model, or failed with HTTP 400 when switching to a model without reasoning support.
2. **Global Overwrite on Local Pick**: Selecting an effort on a specific model overwrote the global `agent.reasoning_effort` key in `config.yaml`, corrupting reasoning settings for all other providers.
3. **Lossy Session Resumption**: Resuming a persisted session either wiped out the active reasoning effort or failed to rebuild the runtime effort state.
4. **Surface Divergence**: The Classic CLI, TUI Gateway, and Desktop/Ink UI each maintained distinct reasoning resolution logic, validation gates, and static ladders.

This architecture formalizes a unified, capability-aware **Five-Layer Reasoning Precedence**, active-session **Restart/Resume Carrier Semantics**, and strict **Cross-Surface Equivalence**.

---

## 2. The Five State Layers

Effective reasoning effort for any model is resolved through a strict hierarchical precedence:

```
[User Input / Command]
       │
       ▼
1. Explicit One-Turn Choice (CLI/TUI: --once)
       │  (Fails closed if unsupported; reverts immediately after turn)
       ▼
2. Runtime EffortByBase (Active Session In-Memory State)
       │  (Ephemeral memory: 3.8=low, 3.1=high; persists across switches within active session)
       ▼
3. Durable Per-Base Override (config.yaml: agent.reasoning_overrides[canonical_base])
       │  (Written by --global on Cloud Code routes; alias overrides take precedence over base)
       ▼
4. Global Fallback (config.yaml: agent.reasoning_effort)
       │  (Shared cross-provider default if supported by destination model)
       ▼
5. Model Default (agent/gemini_cloudcode_models.py)
          (Capability registry default, e.g. 3.8 -> 'high', 3.1 -> 'high')
```

### Layer 1: Explicit One-Turn Choice (`--once`)
- Highest precedence; strictly turn-scoped.
- Stored in `one_turn_model_restore` snapshot alongside a deep copy of `effort_by_base`.
- Restored atomically at turn end; does not mutate `effort_by_base`.

### Layer 2: Runtime `effort_by_base`
- Ephemeral in-memory mapping (`dict[str, str]`) on `AIAgent` and `HermesCLI`, derived from Antigravity CLI (AGY v1.2.13) `model.RootModel.effortByBase`.
- Remembers user effort choices per canonical base model (`gemini-3.8-flash`, `gemini-3.1-pro`) during an active session.
- Excursions to partner models (e.g. Claude) or other Gemini bases do not evict remembered efforts.
- Resets on fresh session start or process restart.

### Layer 3: Durable Per-Base Override (`agent.reasoning_overrides`)
- Persisted in `config.yaml` under `agent.reasoning_overrides.<canonical_base>`.
- Emitted when `/model <model> --reasoning <level> --global` is executed on a Cloud Code route.
- Isolates Cloud Code effort preferences without corrupting global configuration.

### Layer 4: Global Fallback (`agent.reasoning_effort`)
- Pre-existing flat setting in `config.yaml`.
- Applied only if destination model supports that effort level.

### Layer 5: Canonical Model Default
- Baseline capability registry defaults defined in `agent/gemini_cloudcode_models.py`.

---

## 3. Active-Session `reasoning_config` as the Restart/Resume Carrier

To prevent unbounded state sprawl in persisted storage, the full `effort_by_base` dictionary is **never serialized to disk**. Instead, Hermes uses the active `reasoning_config` as the authoritative restart carrier:

1. **Active Persistence**:
   - The session row's `model_config` JSON carries the active `reasoning_config` (e.g. `{"enabled": True, "effort": "low"}`).
2. **Cold / Eager / Deferred Resume**:
   - When reconstructing an agent from persisted session state, `_make_agent()` seeds **exactly one** canonical base entry:
     ```python
     agent.effort_by_base = {canonical_base: active_effort}
     ```
   - Previous unvisited or historical model entries are discarded, preserving the clean restart invariant.
3. **Disabled & Unsupported Handling**:
   - Persisted disabled reasoning (`{"enabled": False}`) restores disabled state with `agent.effort_by_base = {}`.
   - Stale or unsupported persisted efforts (e.g. `gemini-3.1-pro` with `medium`) are rejected during seeding, and re-resolve through Layer 3–5 precedence.

---

## 4. Cross-Surface Equivalence

All Hermes interaction surfaces adhere to the exact same behavioral contract:
- **Classic CLI**: Typed `/model`, prompt_toolkit picker modal.
- **TUI Gateway / JSON-RPC**: `slash.exec /model`, `model.options` capability discovery.
- **Desktop / Ink TUI**: `ModelPicker` component rendering exact `reasoning_efforts` arrays.

### Early Validation Invariant (Zero-I/O)
Any typed switch declaring an invalid or unsupported effort (e.g. `3.8 + max`, `3.1 + medium`, `Claude + low`, `gemini-oauth:3.8 + max`) is parsed and rejected locally **before**:
- `switch_model()`
- Credential resolution
- Network or provider catalog requests
- Agent runtime mutation
- Database or configuration persistence

---

## 5. Fallback & Primary Restoration Matrix

When transport or rate-limit failures trigger model fallback:
- **Independent Target Resolution**: The fallback model resolves its own effective effort from its remembered runtime memory, config override, or default.
- **Zero Memory Mutation**: Fallback activation never writes to `effort_by_base`.
- **Clean Primary Restoration**: On the subsequent turn, `restore_primary_runtime()` restores the primary model's exact pre-fallback reasoning configuration.
