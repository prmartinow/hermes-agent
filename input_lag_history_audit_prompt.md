# hermes-agent input lag history audit

You are tasked with performing a deep investigation into past Hermes Agent sessions and Git commit history to understand:
1. Past Session Archaeology: Which past sessions worked on TUI input latency, composer responsiveness, PTY streaming, TranscriptPane rendering, Nanostore migrations, and keystroke batching? What exact changes and hypotheses were made in those sessions?
2. Git History Mapping: Inspect `git log` and commit diffs across `.` (checking branches `local`, `bug-fixes`, `main`, etc.) for:
   - `ui-tui/src/components/appLayout.tsx`
   - `ui-tui/src/app/useMainApp.ts`
   - `ui-tui/src/components/textInput.tsx`
   - `ui-tui/src/lib/inputMetrics.ts`
   - `ui-tui/packages/hermes-ink/src/ink/components/App.tsx`
   - `ui-tui/packages/hermes-ink/src/ink/parse-keypress.ts`
3. Regression Analysis: Identify which commits, merges, or structural refactors inadvertently altered the render boundaries or re-introduced coupling (e.g. why `TranscriptPane` received `composer={composer}`, why stdin coalescing was bypassed, or what caused input field performance to become unstable again).
4. Synthesize Findings: Document a clear, chronological breakdown of:
   - Past sessions and what they implemented.
   - Key commit hashes, authors, dates, and commit messages.
   - The exact regression commits that caused the current 120s runaway queue / 850ms multi-line latency.
