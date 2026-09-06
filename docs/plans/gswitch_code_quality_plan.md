# Code Quality & Verification Plan: `/gswitch` Feature & Dynamic CD-DOCI Rotation

**Document Path**: `docs/plans/gswitch_code_quality_plan.md`  
**Primary Goal**: Verify, audit, and harden the `/gswitch` feature and dynamic CD-DOCI rotation integration across all layers of the Hermes Agent codebase against architectural invariants, type safety, lint standards, concurrency safety, and edge-case resilience.

---

## Sub-Goal 1: Static Analysis, Type Safety & Lint Cleanliness (`qa-static-analysis`)
- **Python Audit**: Run static analysis across `hermes_cli/auth.py`, `hermes_cli/commands.py`, `cli.py`, `tui_gateway/server.py`, `tui_gateway/methods_tools.py`, `agent/credential_pool.py`, and `tools/delegate_tool.py`.
  - Assert zero unresolved imports, type mismatches, or missing return type annotations.
  - Verify compliance with strict PEP 8 formatting, no trailing whitespace, and no stray debug statements.
- **TypeScript Audit**: Run `tsc --noEmit` and check `apps/desktop/src/lib/desktop-slash-commands.ts` for type definition alignment.

---

## Sub-Goal 2: Architectural Seams & Invariant Audit (`qa-seams-invariants`)
- **Zero-Hardcoded-Labels Invariant**: Audit every touched file to ensure that no account aliases are hardcoded as static constants or fallback literals. All alias-to-index resolutions must flow strictly through `display.account_aliases` in `config.yaml` and live `get_gemini_oauth_auth_status()` discovery.
- **Surface Isolation**: Ensure `/gswitch` operates identically across the interactive CLI REPL, the TUI JSON-RPC gateway, the Web Dashboard, and Electron Desktop.
- **Prompt Caching & Role Alternation**: Verify that `/gswitch` execution does not alter past messages, inject synthetic prompts, or invalidate prefix prompt caching.

---

## Sub-Goal 3: State Persistence & Cache Stickiness Invariant Audit (`qa-state-persistence`)
- **Database Serialization**: Audit `state.db` SQLite update paths (`update_session_meta`, `model_config` JSON encoding/decoding) for schema safety, handling `NULL` configs, invalid JSON recovery, and multi-threaded write locking.
- **KV-Cache Stickiness**: Confirm that pinning an account with `/gswitch <label>` remains durable across session restarts, while `/gswitch auto` and `/gswitch clear` cleanly remove the key and allow unpinned CD-DOCI rotation.
- **Turn Context Synchronization**: Verify that `turn_context.py` picks up dynamic manual switches mid-session and invokes `agent._swap_credential()` with zero dropped turns.

---

## Sub-Goal 4: Edge Cases & Error Resilience Audit (`qa-error-resilience`)
- **Input Sanitization**: Test case insensitivity (`/gswitch ALIAS2`, `/gswitch alias2`), extra whitespace (`/gswitch   alias2  `), and unicode safety.
- **Numeric Argument Rejection**: Confirm `/gswitch 1` through `/gswitch 5` are strictly rejected with guidance to use account labels.
- **Unauthenticated Account Guard**: Test behavior when `/gswitch` targets an unauthenticated account in `auth.json`—must fail gracefully with a descriptive error message without corrupting session state.
- **Exhaustion Fallback**: Verify that if a manually pinned account reaches 0% capacity, the credential pool temporarily fails over to a healthy account rather than hanging or crashing.

---

## Sub-Goal 5: Full Test Suite Execution & Regression Validation (`qa-test-verification`)
- **Hermetic Test Runner**: Run the full suite via `scripts/run_tests.sh` across all affected test modules.
- **Coverage Check**: Verify 100% code branch coverage of `handle_gswitch_command()`, `get_gemini_account_label_map()`, `_gswitch_completions()`, and `_handle_gswitch()`.
- **Zero Flakiness Assurance**: Confirm zero timing or subprocess isolation flakes.
