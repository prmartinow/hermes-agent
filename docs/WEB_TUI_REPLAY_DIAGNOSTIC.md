# Web TUI replay evidence capture

Use `scripts/diagnose_web_tui_replay.py` for startup replay investigations. Legacy banner-count/DOM-only audit scripts are not regression oracles for history integrity.

```sh
python3 scripts/diagnose_web_tui_replay.py \
  --cdp "$CDP_URL" --url "$SESSION_URL" \
  --output "$EVIDENCE_DIRECTORY" --duration 45
scripts/run_tests.sh tests/test_web_tui_replay_diagnostic.py
```

The tool runs control and early-scroll sequentially in owned temporary tabs, closing each afterward. It reuses the browser's existing authentication without reading or deleting cookies. If login is required, it stops that capture; it never supplies credentials.

## Isolation

A fresh tab normally shares `hermes.pty.token.chat` through localStorage. Consequently, it can reattach to the same PTY as an existing user tab. The diagnostic overrides only this key's get/set methods in the **test document's JavaScript realm**, using a fresh random identity per capture. Actual shared storage is not changed. The report verifies the outgoing attach identity by hash and checks that the shared key stayed unchanged. This is controlled instrumentation, not a production code change. Other preferences and authentication are shared, so this is not an isolated browser context.

A unique request identity is evidence of a fresh attachment key, but server creation/PID is not independently instrumented; the report states that limit. Test PTYs are detached when their tabs close and left to normal idle reaping. No user PTY is killed.

## Evidence

- Continuous CDP reader: protocol errors and evaluation exceptions fail explicitly.
- Browser console, exceptions, WebSocket lifecycle, decoded frame byte counts/hashes.
- Loaded JavaScript asset URLs without queries and exact response hashes.
- Full xterm physical rows normalized by `isWrapped`, stored as per-line hashes.
- Viewport/base coordinates, actual scrollbar track/slider rectangles, visible buffer and DOM hashes.
- Parse/render/scroll counters attached when the terminal is discovered; earlier events are not observed by those counters.
- Real wheel events at measured terminal bounds, starting after the requested delay and retried until a scroll away from bottom is observed. Browser stalls can delay input: use actual dispatch/ack and sample timestamps, not nominal delay labels.
- Late-growth comparisons retain unchanged prefix length, repeated multiline content, and visible anchor equality. Repetition is not automatically an unintended duplicate: historical quotes and tool output can repeat legitimately.

Startup sampling/input starts immediately; settled assertions are not eligible before 15 seconds. Capture duration must be at least 30 seconds. Full-buffer sampling is nonzero overhead and its cadence is recorded rather than asserted to be exact. No pixels are captured: parsed text/render events do not alone prove pixel-perfect painting.

Artifacts contain no raw terminal transcript or frame payload. Console messages are redacted on a best-effort basis; keep artifacts private and review before sharing. Output directory permissions are 0700 and reports 0600. ANSI clear counts currently detect whole sequences within individual frames; zero counts do not exclude a sequence split across frame boundaries.

Exit 0 means capture completed, **not that history rendering passed**. Exit 2 means missing terminal, missing effective scroll, authentication/isolation failure, or capture error. Source-transcript comparison and backend resume-generation telemetry are still needed to prove an unintended duplicate and locate its producer.

## Replay boundary protocol

Dashboard inline resumes emit OSC 777 with `hermes-replay;begin;<generation>`,
then `hermes-replay;end;<generation>` after the committed full history frame.
Failure emits `abort` instead. The generation is a UUID scoped to the resume
attempt. Ink queues completion in the same output write after the frame; stdout
backpressure delays both. Inline history does not defer a partial mounted range.
The browser acknowledges completion only after its ordered xterm parse callback,
rejecting stale generations. It accepts an end-only warm snapshot when the begin
marker was trimmed by a scrollback wipe. A timeout warns; it never means success.
Deploy matching frontend and TUI bundles: an older producer has no completion
marker and the new consumer deliberately reports unconfirmed loading.

The viewport-only resize path translates transcript rows to viewport coordinates
and avoids newline on the bottom physical row. The replay ring-buffer clear
recognizer preserves escape-prefix state across read boundaries. Tests cover
actual terminal parsing rather than checking only the kind of clear command.

A copied inactive session is useful integration evidence, not a replacement for
an active-turn reproduction. Run the baseline against the same copied snapshot;
if it does not reproduce the live anomaly, do not credit the candidate with
fixing that anomaly merely because its snapshot test passes.
