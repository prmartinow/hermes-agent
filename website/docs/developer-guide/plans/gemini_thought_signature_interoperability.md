# Gemini Thought-Signature Interoperability & Destination-Aware History Projection

**Document Path**: `website/docs/developer-guide/plans/gemini_thought_signature_interoperability.md`  
**Specification Reference**: `agent/native_replay.py`, `agent/gemini_native_adapter.py`, `agent/transports/chat_completions.py`  
**Status**: Implemented & Verified on `dev` (Action Item 2)  

---

## 1. Executive Summary & Problem Resolution

In multi-turn, multi-model agent conversations, Google Gemini Cloud Code PA (`gemini-oauth`) requires cryptographic thought signatures (`thoughtSignature`) on function calls. Missing, mismatched, or corrupted signatures trigger immediate upstream `HTTP 400 (INVALID_ARGUMENT)` rejections.

Historically, systems either:
1. Discarded native signature state when switching models or compacting context, breaking downstream Gemini requests.
2. Injected synthetic bypass sentinels (`skip_thought_signature_validator`) blanketly across all tool calls, polluting durable history and corrupting native parallel tool call topologies.
3. Leaked Google-internal signature fields to third-party partner models (Claude, GPT-OSS) or strict endpoints, causing upstream HTTP 400 errors.

This architecture establishes a strict three-layer separation between **Generic Semantics**, **Durable Provider-Native Provenance**, and **Wire-Only Validation Policy**.

---

## 2. The Three-Layer Architecture

```
[Canonical Hermes History (Durable & Lossless)]
  │
  ├── 1. Generic Semantics:
  │      content (visible text), tool_calls (standard arguments & IDs), reasoning
  │
  ├── 2. Provider-Native Provenance:
  │      ├── reasoning_details: [{"type": "google.native_assistant", "content": {"role": "model", "parts": [...]}}]
  │      └── tool_calls[*].extra_content: {"google": {"thought_signature": "..."}} (Fallback & Compaction Seam)
  │
  └── 3. Wire-Only Validation Policy (Outbound Copy Only):
         └── Synthesized 'skip_thought_signature_validator' (Emitted only for foreign unsigned calls on Gemini wire)
```

### Layer 1: Generic Semantics
- Model-agnostic conversational fields: `role`, `content`, `tool_calls` (OpenAI format), `reasoning`.
- Replayed losslessly across all LLM providers and UI frontends.

### Layer 2: Provider-Native Provenance
- Stored durably in `state.db:messages` without requiring any database schema migration.
- `google.native_assistant`: A structured dictionary inside `reasoning_details` carrying the exact, ordered native `parts` array as returned by Google Cloud Code PA.
- `tool_calls[*].extra_content`: Per-tool-call signature dictionary. Serves as a forward-compatible fallback representation when lossy context compaction removes historical `reasoning_details`.
- Invariant: Durable history is **never mutated** during projection or provider switches.

### Layer 3: Wire-Only Validation Policy
- The dummy validator sentinel `skip_thought_signature_validator` is **never durable cryptographic provenance**.
- Synthesized on outbound wire request copies **only** when translating foreign unsigned tool histories (e.g. traces originating from Claude or GPT-OSS) toward a Gemini thinking model.
- Never persisted into `state.db`, never written into `google.native_assistant`, and never attached to `tool_calls[*].extra_content`.

---

## 3. Verified Empirical Cloud Code PA Laws

Empirically verified on the RPC node through authenticated probes against Google Cloud Code PA (v1internal inference gateway):

| Observed Property | Empirical Behavior | Architectural Implementation |
|---|---|---|
| **Historical Scope** | Google validates signatures across **every historical tool-call turn** in `contents`, not just the latest turn. | Outbound projection sanitizes/repairs all historical assistant tool turns in the request. |
| **Parallel Calls** | In parallel function-call responses, Google signs **only the first call**. Sibling calls are unsigned (`thoughtSignature: None`). | Exact native replay preserves first-signed / second-unsigned topology. Never injects sentinels onto unsigned siblings. |
| **Sequential Calls** | Consecutive model tool-call turns each receive a unique cryptographic signature. | Exact native part ordering and per-turn signatures are preserved across multi-step execution. |
| **Text-Only Signatures** | Non-streaming text responses carry thought signatures; streaming SSE emits standalone signature-only parts (`{"thoughtSignature": "..."}`). | `GoogleNativeStreamAccumulator` captures standalone signature parts; exact replay emits them without artificial merging. |
| **Circulation Domain** | Signatures circulate losslessly across verified models (`gemini-3.8-flash`, `3.7`, `3.6`, `3.1-pro`). | `supports_thought_circulation=True` declared on verified models; unverified models fail closed (`None`). |
| **Account Portability** | Signatures verified by Cloud Code PA are **not bound to individual OAuth account tokens**. | Replay validity depends on model capability, never on OAuth account or credential identity. |
| **Partner Rejection** | Cloud Code partner models (`claude-sonnet-4-6`, `gpt-oss-120b-medium`) reject Google thought signatures with HTTP 400. | Transport projection strictly strips Google metadata before outbound requests to partner destinations. |
| **Corrupted Signatures** | Non-empty corrupted signatures produce `HTTP 400 (Corrupted thought signature)`. | Hermes preserves corrupted signatures verbatim, allowing upstream validation to fail transparently without masking. |
| **Compaction Degradation** | `salvage_grown_transcript` pops `reasoning_details` on historical turns, but preserves `tool_calls[*].extra_content`. | Degraded generic fallback uses per-tool real signatures for native groups, and synthesizes bypass for foreign groups. |

---

## 4. End-to-End Component Lifecycle

### A. Non-Streaming & Streaming Capture (`agent/native_replay.py`, `gemini_native_adapter.py`)
1. **Non-Streaming**: `translate_gemini_response()` packages raw response `parts` into `google.native_assistant` carrier via `build_google_native_carrier()`, taking a deep snapshot. Gated strictly by `is_gemini_model(model)` so partner models never acquire false Google provenance.
2. **Streaming**: `GoogleNativeStreamAccumulator` observes raw SSE events into request-local state, deduplicating incremental frames for the same logical function call and preserving arrival order for text, thought, and standalone signature-only parts. Terminal chunk attaches `[carrier]` upon receiving `finishReason`.
3. **Streaming $	o$ Sync Fallback**: In `GeminiCloudCodeClient`, fallback from streaming HTTP 400 to synchronous HTTP 200 forwards `reasoning_details` onto the synthetic stream chunk delta.

### B. Destination-Aware Projection (`agent/transports/chat_completions.py`)
- Evaluates `_destination_accepts_google_thought_replay(*, model, base_url, provider_profile)`:
  - **Direct Gemini** (`gemini-oauth`, `gemini` on native endpoints): authorizes both `extra_content` and `google.native_assistant`.
  - **OpenRouter / Nous**: authorizes `extra_content` for verified Gemini models; strips private `google.native_assistant` carrier; retains ordinary reasoning details.
  - **Gemini `/openai` Compatibility**: fails closed (strips both `extra_content` and `reasoning_details`).
  - **Partner Models & Strict Routes**: strictly strips all Google replay metadata.
- Representation-safe: handles in-memory lists and SQLite-restored JSON text without mutating source history.

### C. Exact Native Replay & Fallback (`agent/gemini_native_adapter.py`)
- For each assistant message in `_build_gemini_contents()`:
  1. **Carrier Applicability Guard**: `usable_google_native_carrier()` verifies structural validity, source/target circulation capabilities, and semantic equivalence (visible text, function names, deserialized JSON arguments, IDs).
  2. **Authoritative Replay**: If usable, emits exact native `parts` directly into model content. Emits matching `functionResponse.id` only when the native call carried an ID, deliberately omitting `id` if the native call was ID-less.
  3. **Generic Fallback**: If carrier is absent or stale:
     - `group_has_real is True`: emits real signatures, leaves unsigned siblings unsigned (zero bypass on siblings).
     - `group_has_real is False`: synthesizes `skip_thought_signature_validator` across all foreign unsigned tool calls on the wire copy.

---

## 5. Verification Matrix & Test Architecture

The architecture is verified across 8 dedicated test suites comprising 365 passing tests:
1. `tests/agent/test_gemini_native_replay.py` (25 tests): Helper normalization, non-streaming capture, streaming accumulation parity, and deep-copy isolation.
2. `tests/agent/test_gemini_history_projection.py` (16 tests): Destination capabilities, partner model stripping, OpenRouter replay, concrete wire route gating, and non-destructive projection.
3. `tests/agent/test_gemini_exact_replay.py` (17 tests): Authoritative carrier replay, cross-model eligibility, stale carrier rejection, signed group fallback, foreign unsigned fallback, and ID absence preservation.
4. `tests/agent/test_gemini_replay_closure.py` (10 tests): SQLite restart round-trip, Gemini $	o$ partner $	o$ Gemini round-trip, compaction degradation, and attempt isolation.
5. `tests/agent/test_gemini_cloudcode_models.py` (85 tests): Capability registry, circulation matrix, and alias resolution.
6. `tests/agent/test_gemini_cloudcode_adapter.py` (72 tests): Route resolution and wire payload generation.
7. `tests/test_gemini_oauth.py` (63 tests): OAuth lifecycle, catalog discovery, and token accounting.
8. `tests/agent/test_gemini_native_adapter.py` & `schema.py` (77 tests): Native request/response serialization.
