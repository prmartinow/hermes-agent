# Google Gemini OAuth & Cloud Code PA — Code Analysis & Optimization Plan

This document synthesizes the code analysis of the `gemini` branch in Hermes Agent, applies the **Karpathy Guidelines** (*Simplicity First, Surgical Changes, Goal-Driven Verification*), and validates design decisions against the **Antigravity CLI (`agy`) Developer Harness** architecture.

---

## 1. Executive Summary & Diff Metrics

The `gemini` branch integrates first-class Google Gemini Antigravity OAuth into Hermes Agent:
- **Net Diff**: 24 files, **+4,052 insertions**, **-72 deletions**.
- **Key Modules**:
  - `hermes_cli/auth.py` — 5-account PKCE OAuth pool, DOCI rotation, 5-step quota ignition, 24/7 background watcher daemon.
  - `agent/gemini_cloudcode_adapter.py` — Cloud Code PA client, SSE unwrapping, dynamic reasoning mapping.
  - `agent/gemini_schema.py` — Strict OpenAPI schema cleaner for Google function declarations.
  - `agent/credential_pool.py` — DOCI account scoring, KV cache stickiness, 429 hot failover.
  - `web/src/components/OAuthProvidersCard.tsx` — 5-account matrix on Web Dashboard with live 4-window quota bars.
  - `tests/test_gemini_oauth.py` — 30 comprehensive unit & integration tests (100% passing).

---

## 2. Antigravity Harness Comparison & Wire Parity

Comparing Hermes's `gemini` implementation with the Antigravity Developer Harness (`agent-harnesses-antigravity` skill and `~/.local/bin/agy` v1.1.22 binary):

| Architectural Dimension | Antigravity CLI (`agy` Go Binary) | Hermes Agent (`gemini` Branch) | Alignment Status |
|---|---|---|---|
| **Base Endpoint** | `https://daily-cloudcode-pa.googleapis.com/v1internal` | `https://daily-cloudcode-pa.googleapis.com/v1internal` | ✅ Exact Match |
| **User-Agent Bypass** | `AntigravityCLI/1.1.22/auto (linux; amd64; terminal)` | Dynamic `AntigravityCLI/<version>/auto (<os>; <arch>; terminal)` | ✅ Exact Match |
| **Request Envelope** | `CaGenerateContentRequest` with `project: "default-cli-project"` | `CaGenerateContentRequest` with `user_prompt_id` UUID | ✅ Exact Match |
| **Model Resolution** | `gemini-3.7-flash` $\to$ `gemini-3.7-flash-tiered` (`thinkingLevel: low/med/high`) | Dynamic mapping to `gemini-3.7-flash-tiered` with `thinkingLevel` | ✅ Exact Match |
| **Partner Models** | `gpt-oss-120b-medium`, `claude-sonnet-4-6` (`thinkingConfig: None`) | `thinkingConfig` bypassed for partner and Lite models | ✅ Exact Match |
| **Tool Schema Sanitization** | Strips `$schema`, `minItems`, `maxItems`, `minimum`, `maximum`, `minLength`, `maxLength` | Recursive sanitizer in `agent/gemini_schema.py` | ✅ Exact Match |
| **Quota Telemetry** | 4 sliding windows (`gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly`) | Full 4-window tracking + live relative countdown formatting | ✅ Exact Match |
| **Multi-Account Rotation** | Single account per profile | Dynamic 5-account DOCI pool with KV cache stickiness | 🚀 Hermes Extension |
| **Background Timer Primer** | Manual on error | 24/7 background daemon (`gemini-quota-watcher`) + 5-step ignition | 🚀 Hermes Extension |

---

## 3. Karpathy Guidelines Review & Optimization Opportunities

### Principle 1: Simplicity First (Eliminate Redundancy & Overhead)
1. **HTTP Connection Pooling & Keep-Alive Reuse**:
   - *Current*: `GeminiCloudCodeClient` initializes `httpx.Client` with default pool settings.
   - *Optimization*: Configure explicit `httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0)` to eliminate TCP/TLS handshake latency on repeated turns (~150–300ms savings).
2. **Lazy User-Agent Resolution**:
   - *Current*: Resolves `agy --version` via subprocess on first import.
   - *Optimization*: Resolve lazily on first network request with a 1.0s timeout and memoize in `_CACHED_USER_AGENT`, preventing subprocess spawns from blocking CLI launch.

### Principle 2: Robustness & Thread Safety
1. **Thread-Safe Quota Caches**:
   - *Current*: `_GEMINI_QUOTA_CACHE` and `_GEMINI_LAST_PRIMED_AT` dictionaries are accessed concurrently by the 60s watcher daemon, 30s refresher daemon, and user threads.
   - *Optimization*: Protect shared dictionary mutations with a lightweight `threading.Lock()` to ensure atomic read-modify-write operations.
2. **Atomic Token Refresh Deduplication**:
   - *Current*: Simultaneous requests on an expiring token can trigger multiple concurrent refresh POSTs to Google OAuth.
   - *Optimization*: Add a per-account lock `_REFRESH_LOCKS[acc_idx]` so only one thread executes the refresh while others wait.
3. **Resilient Streaming Finalization**:
   - *Current*: Streaming SSE generator yields chunks inside a context manager.
   - *Optimization*: Wrap stream iteration in `try...finally` to ensure underlying HTTP connection sockets close cleanly if a client disconnects or an interrupt occurs.

### Principle 3: Surgical Execution & Goal-Driven Verification
1. **Quota Primer Model**:
   - Pinned to **`gemini-3.7-flash-low`** (`thinkingLevel: "low"`, prompt `"Say: Ready"`, 5 tokens total) for Gemini, and **`gpt-oss-120b-medium`** for Claude/GPT.
   - Strictly asserts `remainingFraction < 0.9999999` to guarantee that Google's billing engine recorded consumption and locked the 5-hour rolling timer.
2. **Verification Requirements**:
   - All changes must maintain 100% pass rate across `pytest tests/test_gemini_oauth.py`.
   - Live telemetry must demonstrate that all 5 accounts maintain active, countdown-anchored timers.

---

## 4. Implementation Checklist

- [x] Align Gemini 3.7 Flash wire routing with `gemini-3.7-flash-tiered` and dynamic `thinkingLevel`.
- [x] Strip unsupported schema constraints (`minItems`, `maxItems`, `minimum`, `maximum`, `minLength`, `maxLength`) in `agent/gemini_schema.py`.
- [x] Update quota ignition primer to `gemini-3.7-flash-low` with strict `remainingFraction < 0.9999999` assertion.
- [x] Verify live 5-account anchoring on `gemini-5h` and `3p-5h` quota buckets.
- [x] Add explicit `httpx.Limits` connection pooling to `GeminiCloudCodeClient`.
- [x] Add `threading.Lock()` guard to `_GEMINI_QUOTA_CACHE` and `_GEMINI_LAST_PRIMED_AT`.
- [x] Add per-account refresh deduplication lock in `hermes_cli/auth.py`.
