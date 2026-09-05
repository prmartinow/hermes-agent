# Dynamic Model Resolution & Display Name Architecture Plan

**Document Path**: `docs/plans/gemini_dynamic_model_resolution_plan.md`  
**Specification Reference**: `docs/concepts/antigravity-architecture-and-model-resolution.md` & `agy-binaly/docs/MODEL_DISPLAY_RESOLUTION.md`  
**Status**: Approved for Implementation  

---

## 1. Executive Summary & Problem Statement

This implementation plan establishes full behavioral and wire-level alignment between Hermes Agent's Google Gemini OAuth capability (`gemini-oauth`) and Google's production Antigravity CLI Go binary (`agy` v1.1.25, compiled from `google3/third_party/jetski`).

### The Core Problem
1. **Catalog Drop on Null `displayName`**: Google Cloud Code PA returns newly released dynamic models (e.g. `gemini-3.8-flash-tiered`) with `displayName: null`, `supportsThinking: true`, and `thinkingBudget: -1`. Hermes Agent previously discarded any model without an explicit `displayName` (`hermes_cli/auth.py`), preventing newly deployed Google models from appearing in the UI without a manual codebase patch.
2. **Hardcoded Model Version Checks**: Model resolution and wire routing relied on version-specific filters (e.g. `bare.startswith("gemini-3.7-flash")`), which prevented `gemini-3.8-flash` from routing to its required wire target (`gemini-3.8-flash-tiered`) and resulted in upstream HTTP 404 errors.
3. **Missing Dynamic Tier Expansion**: Google returns tiered models as a single endpoint (`-tiered`). The Antigravity CLI expands each dynamic tiered model into three user-facing options (`-high`, `-medium`, `-low`) with corresponding reasoning effort levels. Hermes Agent previously lacked this automated expansion mechanism.

---

## 2. Architecture Comparison

| Architectural Dimension | Google AGY CLI Binary (`v1.1.25`) | Hermes Agent (Prior State) | Target Hermes Architecture |
|---|---|---|---|
| **Missing `displayName`** | Dynamically synthesizes title from slug when `displayName` is `null` | Filtered out models where `displayName` was `null` | Synthesizes human-readable title dynamically for all valid models |
| **Tier Expansion** | Automatically expands `thinkingBudget: -1` into `-high`, `-medium`, `-low` | Relied on static hardcoded model catalogs | Dynamically spawns `-high`, `-medium`, `-low` virtual options |
| **Version Generalization** | Matches all `gemini-3.*` / `supportsThinking` models generically | Hardcoded `startswith("gemini-3.7-flash")` checks | Generic regex matching for all `gemini-3.*` and future tiered versions |
| **Wire Target Mapping** | Maps all effort tiers back to `-tiered` on the wire | Sent unmapped bare slugs, triggering HTTP 404 | Translates virtual tier slugs to `-tiered` in Cloud Code PA requests |
| **Reasoning Configuration** | Maps effort to `thinkingLevel` or `thinkingBudget` (`-1`/`4000`/`1000`) | Manual configuration per turn | Automatically attaches `thinkingLevel` (`high`/`medium`/`low`) from slug |

---

## 3. End-to-End Resolution Pipeline

```
[Google Cloud Code PA API: :fetchAvailableModels]
       │  Returns raw catalog: "gemini-3.8-flash-tiered" (displayName: null, supportsThinking: true, budget: -1)
       ▼
[Dynamic Discovery & Tier Expansion (hermes_cli/auth.py)]
       │  Detects supportsThinking=true & thinkingBudget=-1, spawning 3 virtual tiers (-high, -medium, -low)
       ▼
[Dynamic Slug Parsing & Label Formatting (format_gemini_user_facing_slug)]
       │  Derives: "Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (Medium)", "Gemini 3.8 Flash (Low)"
       ▼
[Frontend Presentation (Web Dashboard /api/model/options & TUI model.options)]
       │  Renders interactive ModelPickerDialog / Bubble Tea model picker
       ▼
[Outbound Wire Resolution (agent/gemini_cloudcode_adapter.py)]
       │  Translates virtual slug "gemini-3.8-flash-high" -> Wire Slug "gemini-3.8-flash-tiered"
       │  Attaches generationConfig.thinkingConfig: {"thinkingLevel": "high", "includeThoughts": true}
       ▼
[Cloud Code PA Inference Gateway: POST /v1internal:streamGenerateContent]
       │  HTTP 200 OK — Real-time token streaming with thought tokens
```

---

## 4. Detailed Component Engineering Specifications

### Component A: Dynamic Discovery & Tier Expansion Engine (`hermes_cli/auth.py`)

* **Target Function**: `fetch_gemini_available_models(account, force, timeout_seconds)`
* **Modifications**:
  1. Remove hardcoded model insertion blocks (e.g. `if "gemini-3.7-flash-tiered" in models_dict:`).
  2. Implement `FetchTieredModels` parity:
     - Scan all models in `models_dict`.
     - Filter out internal non-conversational helpers (`tab_*`, `chat_*`, `models/*`, `*image*`).
     - If `minfo.get("supportsThinking") is True` and `minfo.get("thinkingBudget") == -1` and `mid_str.endswith("-tiered")`:
       - Derive `base_slug = mid_str[:-7]`.
       - Append `f"{base_slug}-high"`, `f"{base_slug}-medium"`, `f"{base_slug}-low"` to `ordered_ids`.
       - Format each tier label via `format_gemini_user_facing_slug(v_slug)`.
     - For static models with explicit `displayName` or general family identifiers:
       - Append `mid_str` to `ordered_ids`.
       - Format display name using `dname or format_gemini_user_facing_slug(mid_str, dname)`.

### Component B: Display Name Synthesizer (`hermes_cli/auth.py`)

* **Target Function**: `format_gemini_user_facing_slug(model_id, raw_display_name)`
* **Modifications**:
  1. If `raw_display_name` is present and does not equal the raw slug, use it as base.
  2. Normalize version substrings (e.g. `3-8` $	o$ `3.8`, `4-6` $	o$ `4.6`).
  3. Extract effort tags (`-high`, `-medium`, `-low`, `-thinking`) into title-cased suffixes (`(High)`, `(Medium)`, `(Low)`, `(Thinking)`).
  4. Ensure output matches standard vendor naming:
     - `gemini-3.8-flash-high` $\longrightarrow$ `Gemini 3.8 Flash (High)`
     - `gemini-3.8-flash-medium` $\longrightarrow$ `Gemini 3.8 Flash (Medium)`
     - `gemini-3.8-flash-low` $\longrightarrow$ `Gemini 3.8 Flash (Low)`
     - `gemini-3.7-flash-high` $\longrightarrow$ `Gemini 3.7 Flash (High)`

### Component C: Cloud Code PA Wire Translation Adapter (`agent/gemini_cloudcode_adapter.py`)

* **Target Functions**:
  1. `resolve_cloudcode_model_and_effort(model, effort)`:
     - Generalize tiered detection using regex `r"^gemini-3\.\d+-flash(?:-(high|medium|low|tiered))?$"`.
     - Ensure any dynamic flash model resolves to `f"gemini-{version}-flash-tiered"` on the wire.
  2. `_create_chat_completion` and `_create_chat_completion_async`:
     - Generalize `thinkingConfig` generation for all `gemini-3.*-flash` models.
     - Automatically map extracted effort (`high`, `medium`, `low`) into `thinkingLevel` (`"high"`, `"medium"`, `"low"`) with `"includeThoughts": True`.

### Component D: Transport Layer Reasoning Config (`agent/transports/chat_completions.py`)

* **Target Function**: `_build_gemini_thinking_config(model, reasoning_config)`
* **Modifications**:
  1. Verify `normalized_model.startswith("gemini-3")` cleanly handles 3.8.
  2. Ensure token ceiling calculations default to 65,536 output tokens for all Gemini 3.x Flash variants.

### Component E: Fallback Provider Roster (`plugins/model-providers/gemini-oauth/__init__.py`)

* **Target Class**: `GeminiOAuthProfile`
* **Modifications**:
  - Update `fallback_models` tuple to prioritize 3.8 dynamic tiers:
    ```python
    fallback_models = (
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    )
    ```

---

## 5. Verification & Test Strategy

### 1. Automated Test Suite
- `tests/test_gemini_oauth.py`:
  - Assert dynamic expansion of `gemini-3.8-flash-tiered` into 3 virtual tiers.
  - Verify formatting of `Gemini 3.8 Flash (High/Medium/Low)`.
- `tests/agent/test_gemini_cloudcode_adapter.py`:
  - Test wire slug translation: `gemini-3.8-flash-high` $	o$ `gemini-3.8-flash-tiered`.
  - Validate generation config: assert `thinkingLevel: "high"`, `includeThoughts: true`.

### 2. Live API Catalog Discovery
- Execute offline/mocked and live discovery probes to verify that `POST /v1internal:fetchAvailableModels` hydrates the options list without error.

### 3. REST & Web UI Validation
- Confirm `GET /api/model/options` contains the full 3.8 roster under provider `gemini-oauth`.
- Verify the Web Dashboard `ModelPickerDialog` and TUI picker render the formatted display names.

---

## 6. Git Branching & Sanitation Protocol

1. **Branch Isolation**:
   - All code edits must be committed directly to consolidated topic branch `dev`.
   - Feature changes live linearly on `dev` and must never be committed directly to `main` or `local`.
2. **Sanitation Verification**:
   - Execute strict regex scans across all staged files prior to commit:
     ```bash
     rg -n --hidden -S "(TOKEN|API_KEY|PRIVATE_KEY|INTERNAL_IP|HOST_PATH)" <staged-files>
     ```
   - Confirm zero host paths, zero private LAN IPs, and zero credentials.
3. **Integration into Serving Branch**:
   - Push topic branch to `origin/dev`.
   - Switch to serving branch `local` and perform `--no-ff` merge of `dev`.
   - Recompile asset bundles (`npm run build:ink && npm run build`).
   - Push updated baseline to `origin/local`.
