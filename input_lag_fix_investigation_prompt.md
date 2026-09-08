Now that you have the complete unified context in your session history — including the live benchmark results, the line-by-line bottleneck analysis, and the historical Git commit archaeology (commits `99d859ce4a`, `3b4dd68326`, `002357a83f`) — proceed with the next phase:

Investigate and architect the exact code fixes and comprehensive implementation plan to permanently resolve the Hermes TUI input field lag and runaway event queue.

Specifically, explore and draft the exact code modifications for:
1. **Phase 1 (Render Decoupling)**: In `ui-tui/src/components/appLayout.tsx`, decouple `<TranscriptPane>` from `composer` by passing only `cols={composer.cols}`. Verify any other props or children that read `composer` unnecessarily.
2. **Phase 2 (LRU Wrap Caching)**: In `ui-tui/src/lib/inputMetrics.ts`, implement an efficient LRU/memoization cache for `wrapAnsi` and `visualLines` to eliminate the 850ms multi-line compute spikes.
3. **Phase 3 & 4 (Hermes-Ink Stdin Coalescing & Keypress Batching)**: In `ui-tui/packages/hermes-ink/src/ink/components/App.tsx`:
   - In `handleReadable` (line 570), drain all available chunks from `stdin.read()` into a unified buffer before invoking `parseMultipleKeypresses`.
   - In `processKeysInBatch` (line 694), coalesce consecutive printable character insertions targeting the active text input within the same tick into a single batched text insertion event.
4. **Verification & Benchmark Strategy**:
   - Verify that all existing unit tests and typechecks pass (`npm run typecheck`, `npm run test` in `ui-tui` and `packages/hermes-ink`).
   - Plan the exact benchmark validation using `scripts/bench-keystroke-latency.mjs` and the live 60-second CDP key-hold test to prove the post-release drain delay drops from >120s to < 100ms.

Deliver a detailed, production-ready implementation plan with exact file paths, before/after code diffs, and verification steps.
