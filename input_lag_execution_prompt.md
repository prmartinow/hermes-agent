# Execute Implementation Plan: Hermes TUI Input Latency & Runaway Queue Resolution

Proceed immediately with implementing and verifying the 4-phase resolution plan you developed:

## 1. Code Changes to Apply:
- **Phase 1 (Render Decoupling)**:
  In `ui-tui/src/components/appLayout.tsx`, change `<TranscriptPane>` props to accept `cols={composer.cols}` instead of `composer={composer}`. Update the `TranscriptPane` component signature and props interface accordingly. Ensure `TranscriptPane` does NOT receive any other props that change on individual keystrokes.
- **Phase 2 (LRU Wrap Caching & Metric Memoization)**:
  In `ui-tui/src/lib/inputMetrics.ts`, implement a bounded LRU cache (e.g. 256 entries) for `visualLines` / `wrapAnsi` so multi-line text wrapping does not recompute on every keystroke.
- **Phase 3 & 4 (Hermes-Ink Stdin Draining & Keypress Coalescing)**:
  In `ui-tui/packages/hermes-ink/src/ink/components/App.tsx`:
  - In `handleReadable`, drain all available chunks from `this.props.stdin.read()` in a while loop before invoking `parseMultipleKeypresses`.
  - In `processKeysInBatch`, coalesce consecutive printable single-character keypress events targeting active text inputs into a single text insertion.

## 2. Build & Verification Steps:
1. Rebuild hermes-ink: `npm run build:ink` in `ui-tui`.
2. Run test suites and typechecks: `npm run typecheck` and `npm run test` in `ui-tui`.
3. Run the benchmark: `node scripts/bench-keystroke-latency.mjs` and record the before/after performance comparison.
4. Build the final TUI bundle: `npm run build` in `ui-tui`.

Ensure all changes are clean, strictly typechecked, and fully verified with passing unit tests. Deliver a summary of modified files, test results, and final benchmark numbers upon completion.
