# Canonical Model Resolution & Discovery Architecture Plan

**Document Path**: `website/docs/developer-guide/plans/gemini_dynamic_model_resolution_plan.md`
**Specification Reference**: `agent/gemini_cloudcode_models.py` & Google AGY Binary (`v1.2.13`)
**Status**: Implemented & Verified on `dev`

---

## 1. Executive Summary & Problem Resolution

This architecture establishes behavioral and wire-level alignment for Cloud Code model identity, effort resolution, discovery normalization, and request routing between Hermes Agent's Google Gemini OAuth provider (`gemini-oauth`) and Google's production Antigravity CLI Go binary (`agy` v1.2.13, reverse-engineered from `google3/third_party/jetski`).

### The Historical Architectural Deficiencies (P0)
1. **Virtual-Slug Leakage**: Previously, tiered Google models were expanded into synthesized virtual slugs (`gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`) directly in discovery. This leaked transport-level concepts into model identity, polluted fallback rosters, and corrupted session-level model persistence.
2. **Dual Reasoning Resolvers**: Model resolution and thinking configuration were computed in separate disconnected functions (`resolve_cloudcode_model_and_effort` and `_build_gemini_thinking_config`), risking configuration drift between generation and token accounting (`countTokens`).
3. **Lossy Discovery**: Dynamic models returning `displayName: null` were either dropped or expanded into fragmented artificial rows rather than aggregating supported effort tiers under the canonical base model.
4. **Catalog Drop on Failure**: If live model fetching failed or returned empty results, the catalog resolver returned an empty list rather than falling through to curated profile fallback models.

---

## 2. Canonical `(base_model, effort)` Architecture

The system enforces strict decoupling between **Model Identity** and **Reasoning Effort**:

```
[Inbound Model Selection]
  │  Accepts: canonical base ('gemini-3.8-flash') OR legacy alias ('gemini-3.8-flash-high')
  ▼
[Canonical Resolver: agent/gemini_cloudcode_models.py]
  │  parse_model_slug(input) ──► (base_model, effort, is_legacy_alias)
  │  Validates effort against model capability envelope (efforts_for_base)
  │  Fail-closed on unsupported efforts (EffortUnsupportedError)
  ▼
[Resolved Model Route: ResolvedModel]
  ├── wire_model: Target Google Cloud Code PA slug (e.g. 'gemini-3.8-flash-tiered')
  └── thinking_config: Structured payload {'thinkingLevel': effort, 'includeThoughts': True}
  ▼
[Transport & Adapter Execution]
  ├── chat.completions.create: Sends wire_model + generationConfig.thinkingConfig
  └── count_tokens: Dispatches exact same resolved wire_model to :countTokens endpoint
```

---

## 3. Inbound vs Outbound Asymmetry & Discovery Isolation

| Dimension | Inbound (User / Client Input) | Outbound (Google Cloud Code PA Wire) | Discovery Catalog (`hermes_cli/auth.py`) |
|---|---|---|---|
| **Model Identity** | Canonical base (`gemini-3.8-flash`) or legacy alias (`gemini-3.8-flash-high`) | Exact Google wire model (`gemini-3.8-flash-tiered`) | Canonical base only (`gemini-3.8-flash`) |
| **Reasoning Effort** | Expressed via session `reasoning_config` or legacy slug suffix | Transmitted in `generationConfig.thinkingConfig.thinkingLevel` | Stored in capability map `get_gemini_model_efforts()[base]` |
| **Virtual Slugs** | Accepted for 100% backward compatibility | **Never emitted** | **Never synthesized or advertised** |
| **Validation** | Unsupported effort raises explicit `EffortUnsupportedError` | Wire models guaranteed to exist in upstream Google catalog | Aggregates dynamic wire tiers into single base model |

---

## 4. Component Implementations

### Component 1: Canonical Capability Registry (`agent/gemini_cloudcode_models.py`)
- Defines `ModelCapability`, `EffortRoute`, `ParsedModelSelection`, and `ResolvedModel`.
- Maintains static capability registry for `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.1-pro`, `gemini-3.5-flash`, `gemini-3-flash-agent`, `gemini-pro-agent`, and partner models (`claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`).
- Provides `model_for_base_effort()` and `efforts_for_base()`.
- Implements `LEGACY_MODEL_ALIASES` mapping all historical `-high`, `-medium`, `-low` virtual slugs to `(base_model, effort)`.

### Component 2: Discovery Normalization (`hermes_cli/auth.py`)
- Refactored `fetch_gemini_available_models()`:
  - Collapses dynamic wire tiers (e.g. `gemini-3.8-flash-tiered` with `thinkingBudget: -1`) into canonical base `gemini-3.8-flash`.
  - Normalizes static tiered models (`gemini-3.6-flash-low`, `medium`, `high`) into base `gemini-3.6-flash`.
  - Stores aggregated effort tuples in global registry accessible via `get_gemini_model_efforts()`.
  - Deprecated and removed legacy `_expand_model_tier_slugs()`.

### Component 3: Adapter & countTokens Cutover (`agent/gemini_cloudcode_adapter.py`)
- Refactored `GeminiCloudCodeClient`:
  - Centralized model resolution in `_resolve_model_route()`.
  - Chat completions and `count_tokens()` consume the identical resolver route.
  - Eliminated duplicate `extra_body` kwarg handling.
  - Generates exact wire models and `thinkingConfig` without version-dependent regular expressions.

### Component 4: Provider Profile & Fallback Roster (`plugins/model-providers/gemini-oauth/__init__.py`)
- `GeminiOAuthProfile.fallback_models` updated to strictly advertise canonical base models:
  ```python
  fallback_models = (
      "gemini-3.8-flash",
      "gemini-3.7-flash",
      "gemini-3.6-flash",
      "gemini-3-flash-agent",
      "gemini-3.5-flash",
      "gemini-pro-agent",
      "gemini-3.1-pro",
      "claude-sonnet-4-6",
      "claude-opus-4-6-thinking",
      "gpt-oss-120b-medium",
  )
  ```
- `build_api_kwargs_extras()` passes `{"effort": effort}` in `extra_body` for canonical base models while bypassing effort injection when legacy aliases or non-thinking configurations are active.

### Component 5: Catalog Fallthrough Resiliency (`hermes_cli/models.py`)
- `_gemini_oauth_catalog()` returns `models or None` instead of `[]`, enabling seamless tri-state fallthrough to `profile.fallback_models` when live OAuth discovery is unauthenticated, empty, or offline.

---

## 5. Verification & Test Architecture

The test suite enforces mathematical closure across all four layers:

1. **Resolver Unit Suite** (`tests/agent/test_gemini_cloudcode_models.py`):
   - Contract verification for `parse_model_slug`, `model_for_base_effort`, and `efforts_for_base`.
   - Backward compatibility for every entry in `LEGACY_MODEL_ALIASES`.
   - Prefix stripping across Gemini vendor namespaces (`gemini/`, `google/`, `gemini-oauth/`), while intentionally preserving unrelated vendor-qualified namespaces (such as `anthropic/`) without mutation.
   - Fallback catalog canonical invariant: asserts no legacy aliases exist in `profile.fallback_models`.
   - Static wire route uniqueness invariant: guarantees injective wire mapping.

2. **Discovery & Provider Suite** (`tests/test_gemini_oauth.py`):
   - Discovery aggregation of dynamic and static wire tiers into base models.
   - Discovery-to-resolver closure property: verifies that every discovered base and effort resolves to a valid upstream wire model.
   - Provider catalog fallthrough on network error and empty discovery responses.

3. **Adapter & Integration Suite** (`tests/agent/test_gemini_cloudcode_adapter.py`):
   - 11-case generation and `count_tokens` route parity matrix (`3.8`, `3.7`, `3.6`, `3.1-pro`).
   - Full wire equivalence closure across all legacy aliases.
   - End-to-end transport seam tests verifying `ChatCompletionsTransport` + `GeminiOAuthProfile` interaction.
   - Strict rejection tests for unsupported efforts (`3.8 + max`, `3.1 + medium`).
### Known Upstream Availability Exception: `gemini-3.1-pro-high`
- **Catalog Discovery**: Exists in Google Cloud Code PA `:fetchAvailableModels` (`displayName: "Gemini 3.1 Pro (High)"`, `tagTitle: "New"`, `model: "MODEL_PLACEHOLDER_M37"`).
- **Token Accounting**: Successfully accepted by Google Cloud Code PA `:countTokens` (HTTP 200 OK).
- **Inference Service**: Upstream Google Cloud Code PA currently rejects `:generateContent` and `:streamGenerateContent` with HTTP 400 (`INVALID_ARGUMENT: Request contains an invalid argument`) across all authenticated accounts.
- **Resolution Architecture Decision**: Hermes correctly preserves the verified canonical route (`gemini-3.1-pro-high` in static capabilities) to maintain catalog/accounting closure and full parity with AGY strings, while documenting that upstream Google has not yet activated inference serving for this placeholder tier.
