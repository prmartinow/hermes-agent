# Architectural Fusion Blueprint: Fusing Antigravity Wire Protocols with Native Hermes Capabilities

By synthesizing the findings from **Subagent 1** (Antigravity CLI binary analysis & Google Cloud Code PA wire protocol), **Subagent 2** (Hermes Agent native & advanced subsystems), and **Subagent 3** (the `gemini` branch capabilities & gap analysis), we can formulate a clear, actionable roadmap to deeply fuse Google Cloud Code PA with Hermes Agent.

---

```
                       HERMES AGENT + ANTIGRAVITY FUSION TOPOLOGY
  ┌───────────────────────────────────────────────────────────────────────────────────────┐
  │                                Hermes Agent Ecosystem                                 │
  │   - CLI (Rich/prompt_toolkit/KawaiiSpinner) | Web Dashboard (:9119) | Ink React TUI    │
  │   - Multi-Platform Gateway (~20 Platforms) | Memory Plugins (Hindsight/Mem0)          │
  │   - Execution Sandboxes (Local/Docker/SSH) | Dynamic Skills & ACP IDE Server          │
  └──────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │
                                             ▼
  ┌───────────────────────────────────────────────────────────────────────────────────────┐
  │                         Hermes Core Engine (run_agent.py)                             │
  │   - Sacred Byte-Stable Prompt Caching & Strict Message Role Alternation               │
  │   - Parallel / Sequential Tool Call Dispatcher (handle_function_call)                 │
  │   - Streaming Thinking Scrubber & Trajectory Persistence (SessionDB SQLite FTS5)      │
  │   - Multi-Account Credential Pool & DOCI Dynamic Urgency Rotation Engine              │
  └──────────────────────────────────────────┬────────────────────────────────────────────┘
                                             │
                       FUSED CAPABILITY BRIDGES (The 6 Pillars)
  ┌──────────────────────────────────────────┼────────────────────────────────────────────┐
  │ 1. Native Multimodal Pipeline (inlineData)│ 4. Hybrid Google Grounding (googleSearch)  │
  │ 2. Thought Signature & Replay Engine     │ 5. Exact Context Budgeting (:countTokens) │
  │ 3. Server-Side Context Caching & KV Lock │ 6. Interactive Challenge (VALIDATION_REQ)  │
  └──────────────────────────────────────────┼────────────────────────────────────────────┘
                                             │
                                             ▼
  ┌───────────────────────────────────────────────────────────────────────────────────────┐
  │             Google Cloud Code PA Internal Gateway (/v1internal RPC Bridge)            │
  │   - Endpoints: :generateContent | :streamGenerateContent?alt=sse | :retrieveQuota     │
  │   - First-Party Consumer Billing Bypass (AntigravityCLI/<version>/auto User-Agent)     │
  │   - Models: Gemini 3.7 Flash Tiered | Gemini 3.6 Flash | Claude 4.6 | GPT-OSS 120B    │
  └───────────────────────────────────────────────────────────────────────────────────────┘
```

---

## The 6 Pillars of Architectural Fusion

### Pillar 1: Native Multimodal & Vision Pipeline
* **AGY Protocol**: Supports `inlineData` (base64 images, PDFs, audio, video) and `mediaResolution: "MEDIA_RESOLUTION_LOW"|"MEDIUM"|"HIGH"`.
* **Hermes Native**: `agent/vision.py` and `tools/vision_tools.py` generate OpenAI-style `image_url` objects and browser CDP screenshots.
* **Fusion Design**:
  - Enhance `gemini_cloudcode_adapter.py`'s `_extract_multimodal_parts` to convert local screenshot paths, filesystem image paths, and remote URLs into base64 `inlineData` parts on the fly.
  - Apply `mediaResolution: "MEDIA_RESOLUTION_LOW"` on browser snapshots and file diffs to slash vision token costs by ~75% (from ~258 tokens to ~65 tokens per tile).
  - Enable native document analysis (direct PDF/text file attachments) without requiring auxiliary OCR services.

---

### Pillar 2: Cryptographic Thought Signatures & Trajectory Preservation
* **AGY Protocol**: Gemini 3 models emit a cryptographic `thoughtSignature` per tool call. Replaying tool calls requires this token (or the `"skip_thought_signature_validator"` sentinel). Non-Gemini partner models reject the field with HTTP 400.
* **Hermes Native**: `agent/think_scrubber.py` and `run_agent.py` extract reasoning into `assistant_msg["reasoning"]`, while `agent/compression/` compacts older turns.
* **Fusion Design**:
  - Persist `thoughtSignature` inside `tool_call.extra_content` in SQLite `hermes_state.py`.
  - When replaying conversation histories that have undergone context compression or external imports, automatically attach `"thoughtSignature": "skip_thought_signature_validator"` to Gemini assistant tool turns.
  - When dispatching to non-Gemini partner models (`claude-sonnet-4-6`, `gpt-oss-120b-medium`), cleanly strip `thoughtSignature` to prevent HTTP 400 schema rejections.

---

### Pillar 3: Server-Side Context Caching & KV Stickiness
* **AGY Protocol**: Google Cloud Code PA implements strict prefix-based KV caching across `systemInstruction` $\to$ `tools` $\to$ `contents`, and supports explicit `cachedContent` handles for static blocks $\ge 32\text{k}$ tokens.
* **Hermes Native**: Sacred prompt caching invariant (byte-stable prefix, no mid-turn tool swapping, strict role alternation). `credential_pool.py` implements session-to-account stickiness.
* **Fusion Design**:
  - Enforce byte-stable JSON serialization of tool parameter schemas in `gemini_schema.py` (sorted keys, stable property ordering).
  - Maintain account stickiness in `credential_pool.py` as long as an account has $\ge 20\%$ 5-hour capacity, guaranteeing ~99% KV cache hit rates on Google servers.

---

### Pillar 4: Hybrid Google Search Grounding (`googleSearch`)
* **AGY Protocol**: Native support for server-side `tools: [{"googleSearch": {}}]`, returning structured `groundingMetadata` with search queries, snippets, web URIs, and confidence scores.
* **Hermes Native**: Relies on client-side tools (`web_search`, `web_extract`) which consume multi-turn iteration budget.
* **Fusion Design**:
  - Introduce an optional native Google Grounding mode for Gemini models: attach `{"googleSearch": {}}` directly in the request payload.
  - When enabled, questions requiring factual search are resolved server-side in a single turn without client tool overhead, streaming grounding source links directly into the user stream.

---

### Pillar 5: Authoritative Token Budgeting (`:countTokens`)
* **AGY Protocol**: Exposes the `/v1internal:countTokens` endpoint returning exact Gemini tokenizer counts for arbitrary payload combinations.
* **Hermes Native**: Context compression and iteration budgeting rely on local tiktoken approximations, which drift on Gemini/Claude tokenizers.
* **Fusion Design**:
  - Wire `:countTokens` into `gemini_cloudcode_adapter.py`.
  - Use exact token counts to trigger `context_compressor.should_compress()` and calculate precise remaining budget before sending multi-turn requests.

---

### Pillar 6: Interactive Challenge Handling (`VALIDATION_REQUIRED`)
* **AGY Protocol**: Google returns HTTP 403 with `google.rpc.ErrorInfo` reason `VALIDATION_REQUIRED` and a challenge URL in `google.rpc.Help`.
* **Hermes Native**: Rich CLI formatting with `KawaiiSpinner` and interactive Web Dashboard modals.
* **Fusion Design**:
  - Catch 403 `VALIDATION_REQUIRED` inside `gemini_cloudcode_adapter.py`.
  - Automatically open or display the challenge URL in the CLI and Web Dashboard (`OAuthLoginModal.tsx`), allowing the user to complete verification in 1 click and automatically retry the turn seamlessly.

---

## Actionable Next Steps for Implementation

| Step | Target Subsystem | Action |
|:---:|---|---|
| **1** | `gemini_cloudcode_adapter.py` | Implement local/remote image & PDF base64 conversion into `inlineData` with `mediaResolution: "MEDIA_RESOLUTION_LOW"`. |
| **2** | `gemini_native_adapter.py` | Wire `skip_thought_signature_validator` fallback on Gemini turns and strip on 3P models (`claude-*`, `gpt-oss-*`). |
| **3** | `gemini_cloudcode_adapter.py` | Implement `:countTokens` RPC helper for authoritative token budgeting. |
| **4** | `auth.py` | Implement interactive `VALIDATION_REQUIRED` 403 challenge URL capture and prompt. |
