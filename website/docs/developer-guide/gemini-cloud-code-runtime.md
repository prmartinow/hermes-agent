# Google Gemini Cloud Code PA (`gemini-oauth`) Runtime Architecture

This document is the authoritative architectural specification and developer reference for Google Gemini Cloud Code PA (`gemini-oauth`) support in Hermes Agent. It documents the contracts for canonical model resolution, wire translation, thought-signature circulation, reasoning effort selection, multi-turn replay, transaction rollback, and cross-surface parity.

---

## 1. Architectural Ownership Map

| Component / Subsystem | Primary Owning Modules | Architectural Responsibilities |
| :--- | :--- | :--- |
| **Model Resolution & Wire Adapters** | `agent/gemini_cloudcode_models.py`<br>`agent/gemini_cloudcode_adapter.py` | Canonical base model definitions, compatibility alias parsing, selectable effort capability envelopes, outbound wire translation (`gemini-3.8-flash-tiered`, structured `thinkingConfig`). |
| **Reasoning Effort Selection** | `agent/reasoning_selection.py` | Canonical base resolution, per-base in-memory effort tracking (`effort_by_base`), 4-layer effective effort precedence resolver (`resolve_effective_reasoning_config`), typed validation. |
| **Native History & Replay** | `agent/gemini_native_adapter.py`<br>`agent/native_replay.py` | Authoritative native assistant carriers (`google.native_assistant`), verbatim function-call replay, destination-aware history projection, `skip_thought_signature_validator` bypass sentinel policy. |
| **Switching & Rollback Atomicity** | `agent/agent_runtime_helpers.py` | In-place atomic model switching (`switch_model`), transactional rollback (`_restore_switch_snapshot`), primary runtime synchronization (`_primary_runtime`), fallback restoration. |
| **Transactional Compressor** | `agent/context_compressor.py` | In-memory threshold and reservation calculation, non-destructive switch snapshotting (`snapshot_switch_runtime`), delayed durable reset commits (`commit_switch_runtime`). |
| **Capability Advertising** | `hermes_cli/inventory.py` | Catalog capability derivation (`build_model_options_payload`), Cloud Code canonical inventory deduplication, `effective_reasoning_effort` calculation. |
| **Classic CLI Interface** | `hermes_cli/cli_model_switch_mixin.py`<br>`hermes_cli/cli_tui_mixin.py` | Classic `/model` slash command parsing, interactive model/effort picker rendering, target preselection and `← current` marking, explicit command effort application (`_apply_reasoning_after_switch`). |
| **Gateway RPC Interface** | `tui_gateway/methods_complete.py`<br>`tui_gateway/methods_complete_helpers.py`<br>`tui_gateway/model_switch.py`<br>`tui_gateway/server.py`<br>`tui_gateway/prompt_turn.py` | OpenRPC `model.options` handler (`methods_complete.py`), picker context composition (`methods_complete_helpers.py`), `/model` typed command dispatch (`model_switch.py`), session-scoped overrides, one-turn (`--once`) turn lifecycle management (`_finish_turn`). |
| **Web TUI / Desktop Ink** | `ui-tui/src/components/modelPicker.tsx` | Capability-aware reasoning stage rendering, interactive keyboard/mouse selection, command emission (`modelPickerCommand`). |
| **Type Contracts & Schemas** | `tui_gateway/contracts/config_free_tier_control.py`<br>`scripts/gen_gateway_contracts.py` | OpenRPC JSON schema (`gateway-contract.openrpc.json`) and generated TypeScript interfaces (`gateway-contract.generated.ts`). |

---

## 2. Logical Models vs. Wire Models Truth Table

Hermes strictly decouples **inbound logical model identities** (used by users, configs, and pickers) from **outbound wire model identities** (sent to Google's upstream Cloud Code PA endpoints):

| Logical Model ID | Selectable Efforts | Model Default | Wire Model Name | Wire Reasoning Configuration |
| :--- | :--- | :--- | :--- | :--- |
| `gemini-3.8-flash` | `low`, `medium`, `high` | `high` | `gemini-3.8-flash-tiered` | `thinkingConfig: { thinkingLevel: "<LEVEL>", includeThoughts: true }` |
| `gemini-3.7-flash` | `low`, `medium`, `high` | `high` | `gemini-3.7-flash-tiered` | `thinkingConfig: { thinkingLevel: "<LEVEL>", includeThoughts: true }` |
| `gemini-3.6-flash` | `low`, `medium`, `high` | `high` | `gemini-3.6-flash-<level>` | Static wire slug (e.g. `gemini-3.6-flash-high`) |
| `gemini-3.5-flash` | `low`, `medium`, `high` | `high` | `gemini-3.5-flash-<level>` | Static wire slug (e.g. `gemini-3.5-flash-high`) |
| `gemini-3.1-pro` | `low`, `high` | `high` | `gemini-3.1-pro-<level>` | Static wire slug (e.g. `gemini-3.1-pro-high`) |
| `gemini-3.1-flash-lite` | *None* (`[]`) | — | `gemini-3.1-flash-lite` | No thinking configuration |
| Claude Partner Routes | *None* (`[]`) | — | Partner route name | No Google thinking configuration |
| GPT-OSS Partner Routes | *None* (`[]`) | — | Partner route name | No Google thinking configuration |

### Canonical Cloud Code Routes:
The Cloud Code PA architectural contract applies across:
- `gemini-oauth`: Primary Google Gemini OAuth provider identity.
- `gemini-1`, `gemini-2`, `gemini-3`, `gemini-4`, `gemini-5`: Multi-account isolated Cloud Code provider routes.
Invalid aliases (e.g. `gemini-0`, `gemini-6`, `gemini-42`) remain strictly outside Cloud Code semantics.

### Core Invariants:
1. **Logical Base Model ≠ Legacy Compatibility Alias ≠ Wire Model**:
   - `gemini-3.8-flash`: Canonical logical base model.
   - `gemini-3.8-flash-high`: Inbound legacy compatibility alias (decomposes to base `gemini-3.8-flash` + effort `high`).
   - `gemini-3.8-flash-tiered`: Outbound wire model sent in Google Cloud Code PA requests.
2. **Dynamic Tiered Models**: Dynamic models (`3.8`, `3.7`) require the wire suffix `-tiered` and must pass explicit outbound `thinkingConfig` with `thinkingLevel: "low" | "medium" | "high"` and `includeThoughts: true`. Outbound requests do not send `thinkingBudget`. (The `thinkingBudget: -1` property belongs exclusively to upstream catalog discovery metadata in `:fetchAvailableModels`, not outbound request bodies).
3. **Static Slugs**: Prior models (`3.6`, `3.1-pro`) require wire routing via distinct static sub-slugs (`-low`, `-medium`, `-high`).

---

## 3. Compatibility Alias Policy

Legacy virtual model aliases (such as `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`, and `gemini-3.1-pro-high`) are preserved for backward compatibility:
- **Accepted as Input**: Users and scripts can specify legacy aliases anywhere a model name is accepted.
- **Canonicalized Internally**: `parse_model_slug()` immediately decomposes the alias into its canonical base model and embedded effort.
- **Not Independent Preference Keys**: Ephemeral memory (`effort_by_base`) and durable config (`reasoning_overrides`) strictly store the canonical base ID (`gemini-3.8-flash`), never the alias.
- **Not Advertised Separately**: `model.options` and CLI pickers canonicalize and deduplicate compatibility aliases; only the base model is displayed.
- **Contradiction Rejection**: Providing an alias alongside a conflicting explicit flag (e.g. `/model gemini-3.8-flash-high --reasoning medium`) raises a validation error before any network or state mutation occurs.

---

## 4. Five-Layer Reasoning Precedence & Lifetime Model

When resolving the effective reasoning effort for a model switch, completion, or UI preselection, Hermes applies a strict 5-layer hierarchy:

```
   ┌────────────────────────────────────────────────────────┐
   │ 1. Explicit Turn / Switch Command                      │  e.g. /model --reasoning low
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 2. Runtime In-Memory Map (effort_by_base)              │  Active session memory
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 3. Durable Per-Base Overrides (reasoning_overrides)    │  config.yaml [canonical_base]
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 4. Durable Global Effort (reasoning_effort)            │  config.yaml [global default]
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 5. Model Architectural Default                         │  Registry default ('high')
   └────────────────────────────────────────────────────────┘
```

### Precedence Ownership:
- **Layers 2–5** are resolved by `resolve_effective_reasoning_config()` in `agent/reasoning_selection.py`.
- **Layer 1** (explicit command choice) is validated against model capability and applied above that resolver by the model switch surfaces (`_apply_switch_reasoning()` in `tui_gateway/model_switch.py` and `_apply_reasoning_after_switch()` in `hermes_cli/cli_model_switch_mixin.py`).

### State Lifetimes & Persistence:

| State Layer | Storage Location | Lifetime | Persisted Across Sessions? |
| :--- | :--- | :--- | :--- |
| **Explicit Turn (`--once`)** | Runtime session restore carrier (`one_turn_model_restore` on Gateway, `_pending_one_turn_model_restore` on CLI) | Single turn | **No** (restored in finally block) |
| **Runtime Effort Memory (`effort_by_base`)** | Agent instance attribute (`agent.effort_by_base`) | Live session / process | **No** (ephemeral; never serialized) |
| **Active Resumed Config** | Session database row (`model_config`) | Session resume | **Yes** (active model only) |
| **Per-Base Overrides (`reasoning_overrides`)** | `config.yaml` | Persistent | **Yes** (keyed by canonical base ID) |
| **Global Default (`reasoning_effort`)** | `config.yaml` | Persistent | **Yes** (generic fallback) |

> **Invariant**: The complete `effort_by_base` map is purely ephemeral and is never serialized to the database or written to disk. Only the active model's resolved `reasoning_config` is stored on session checkpoint.

---

## 5. "None" & Disabled-State Semantics

- **Cloud Code Envelopes Exclude "None"**: User-selectable efforts for Cloud Code routes are strictly bounded by `selectable_reasoning_efforts()` (e.g. `["low", "medium", "high"]`). `"none"` is not an effort level and is rejected if passed to `--reasoning`.
- **Explicit Disabled Configuration**: Disabling reasoning via configuration (`{"enabled": False}`) is a distinct state from an effort level.
- **UI Presentation in Disabled State**:
  - `modelPicker.tsx` does not render a `"none"` row for Cloud Code models.
  - The visual cursor defaults to `high` (the model default).
  - No row is labeled with `← current`.
  - `effective_reasoning_effort` in `model.options` is emitted as `null`.

---

## 6. `model.options` Capability Contract

The OpenRPC `model.options` method exposes model capabilities to frontends with explicit typing:

| Field Name | Type | Value & Semantic Meaning |
| :--- | :--- | :--- |
| `reasoning_efforts` | `Array<string> \| null` | `null` = capability unknown or generic.<br>`[]` = known route with no selectable effort (e.g. Flash-Lite, Claude).<br>`["low", ...]` = exact verified selectable effort set. |
| `effective_reasoning_effort` | `string \| null` | `string` = authoritative currently effective target effort.<br>`null` = disabled, no selectable effort, or unconfigured. |
| `can_disable_reasoning` | `boolean \| null` | `false` = disabling is explicitly unavailable (exact Cloud Code effort models).<br>`true` = disabling is explicitly supported.<br>`null` = capability unknown / not asserted. |

---

## 7. Thought-Signature Provenance & History Circulation

Google Cloud Code endpoints enforce strict server-side thought-signature validation for multi-turn tool interactions. Hermes implements an exact provenance architecture in `agent/gemini_native_adapter.py`:

| History Type | Target Route | Outbound Wire Treatment |
| :--- | :--- | :--- |
| **Native Gemini Tool Call with Valid Signature** | Cloud Code Gemini | Replay exact byte-for-byte signature from carrier. |
| **Native Gemini Parallel Group (1st Call Signed)** | Cloud Code Gemini | Replay signature on first call; sibling calls remain unsigned. |
| **Carrier-Lost Parallel Group with $\ge 1$ REAL Signature** | Cloud Code Gemini | REAL call keeps exact signature; missing siblings remain unsigned. |
| **Carrier-Lost Parallel Group with 0 REAL Signatures (Foreign/Unsigned)** | Cloud Code Gemini | Inject `skip_thought_signature_validator` bypass sentinel. |
| **Corrupted Non-Empty Google Signature** | Cloud Code Gemini | Preserve verbatim as `REAL`; never convert to bypass sentinel. |
| **Gemini Signed Tool Turn** | Partner / Non-Gemini Model | Strip Google-native carrier from wire projection; original history untouched. |
| **Text-Only Native Thought Parts** | Compatible Gemini Models | Circulate native thought parts according to carrier capabilities. |

### Core Replay Rules:
1. **Verbatim Replay**: When a `google.native_assistant` carrier exists, assistant parts are replayed exactly as emitted by Google's API.
2. **Sentinel Classifier Rule**:
   - A mixed carrier-lost group containing at least one REAL Google signature (`group_has_real == True`): REAL calls keep exact signatures, and missing siblings remain unsigned (no bypass sentinel synthesized on siblings).
   - A group containing no REAL Google signatures (`group_has_real == False`): missing signatures receive the `skip_thought_signature_validator` bypass sentinel to satisfy upstream validation.
3. **Non-Destructive Projection**: Wire adaptation operates on deep copies. Original session messages and database records remain immutable.

---

## 8. Switching & Rollback Transaction Contract

Model switches initiated via CLI (`/model`) or Gateway RPC (`_apply_model_switch`) are fully atomic transactions managed by `switch_model()` in `agent/agent_runtime_helpers.py`:

```
   ┌────────────────────────────────────────────────────────┐
   │ 1. Capture Pre-Switch Snapshot (_snapshot_switch_state)│
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 2. Swap Runtime State & Rebuild Switched Client        │  Rollback on Client Construction Error
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 3. Tentative Compressor Update (persist_durable_reset=False)
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 4. Re-resolve Effective Reasoning & Primary Runtime    │
   └───────────────────────────┬────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │ 5. Commit Durable Compressor Resets (_finish_switch)   │  commit_switch_runtime()
   └────────────────────────────────────────────────────────┘
```

### Rollback Guarantees:
- **Complete Reversal**: Any failure prior to completing step 5 triggers `_restore_switch_snapshot()`, restoring `model`, `provider`, `client`, `reasoning_config`, `effort_by_base`, `runtime_capabilities`, prompt cache layout, and compressor state.
- **Probe Failure Policy**: In step 3, failure in context resolution or compressor update triggers rollback. However, post-update feasibility revalidation (`revalidate_compression_feasibility`) is an eager best-effort probe; a network hiccup there is logged and does NOT roll back a good switch.
- **Durable Compressor Isolation**: Step 3 passes `persist_durable_reset=False` to `ContextCompressor.update_model()`. Strikes, cooldowns, and streaks in SQLite are never cleared until step 5 succeeds via `commit_switch_runtime()`.
- **Idempotent Snapshot Consumption**: `_restore_switch_snapshot()` accesses `_compressor_state` non-destructively (via `get()`, not `pop()`), allowing safe execution across nested rollback boundaries without losing state.

---

## 9. Fallback & Restoration Matrix

When a primary Cloud Code model encounters a recoverable provider error (e.g. rate limit or 503), Hermes activates configured fallbacks via `try_activate_fallback()`:

| Primary Model | Active Effort | Fallback Model | Fallback Effective Effort | Restoration on Next Turn |
| :--- | :--- | :--- | :--- | :--- |
| `gemini-3.8-flash` | `low` | `gemini-3.1-pro` | Target's own map/config/default (`high`) | `gemini-3.8-flash` / `low` |
| `gemini-3.8-flash` | `low` | `gemini-3.6-flash` | Target's own map/config/default (`high`) | `gemini-3.8-flash` / `low` |
| `gemini-3.8-flash` | `medium` | `claude-sonnet-4-6` | *None* (no Cloud Code effort) | `gemini-3.8-flash` / `medium` |
| `gpt-4o` (OpenAI) | *None* | `gemini-3.8-flash` | Cloud Code target resolver (`high`) | `gpt-4o` / *None* |

### Fallback Rules:
1. **Target Independence**: Fallback resolves the destination model's own capabilities and does not carry the failed primary model's effort across model boundaries.
2. **Memory Preservation**: Fallback activation does not overwrite or mutate `effort_by_base`.
3. **Primary Restoration**: When the primary runtime is restored, the original `reasoning_config` is recovered intact from `_primary_runtime["reasoning_config"]`.

---

## 10. Surface Equivalence & Scope Semantics

Hermes guarantees complete semantic parity across all interaction surfaces:

| Surface | Exact Validation | Session Memory | Global Persistence | One-Turn (`--once`) | Preselection |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Classic CLI Typed** | Yes | Yes | Yes (`--global`) | Yes (`--once`) | Yes |
| **Classic CLI Picker** | Yes | Yes | Yes (`--global`) | N/A | Yes |
| **Gateway RPC Typed** | Yes | Yes | Yes (`--global`) | Yes (`--once`) | Yes |
| **Desktop / Ink TUI** | Yes | Yes | Yes (`^g` toggle) | N/A (modal emits session/global) | Yes |

### Scope Semantics:
- **Session Scope** (`--tui-session` / default pick):
  - In-memory `effort_by_base` updated.
  - Active `reasoning_config` updated.
  - Active session runtime checkpointed to database.
  - Global `config.yaml` remains untouched.
- **Global Scope** (`--global`):
  - In-memory `effort_by_base` updated.
  - Active `reasoning_config` updated.
  - Canonical `agent.reasoning_overrides[canonical_base]` persisted to `config.yaml`.
  - Generic global `agent.reasoning_effort` left untouched.
- **One-Turn Scope** (`--once`):
  - Temporary destination applied for the current prompt turn only.
  - `effort_by_base` is not permanently updated.
  - Global configuration is untouched.
  - `_finish_turn()` production finally-block consumes the restore carrier and restores previous model, provider, reasoning config, and primary runtime.

---

## 11. Empirical Upstream Caveats

These behaviors reflect verified Google upstream API properties, distinct from Hermes runtime bugs:
1. **`gemini-3.1-pro-high` Upstream Inference 400**:
   - `gemini-3.1-pro-high` is catalog-advertised by Cloud Code PA discovery, but upstream inference requests currently return HTTP 400 (`MODEL_PLACEHOLDER_M37` / "New").
   - Hermes permits the model in discovery and routing envelopes per upstream specification, but surfaces Google's upstream error cleanly if selected.
2. **Partial Parallel Call Signatures**:
   - When Gemini generates parallel function calls, Google's wire response only signs the first function call in the group. Sibling function calls remain unsigned.
   - Hermes preserves this topology on replay. Sibling calls are left unsigned, and bypass sentinels are not injected into signed Gemini call groups.
