# Web TUI behavioral test strategy

Treat the capability matrix as a requirements inventory, not a list of proven fixes. Its current evidence audit covers every capability ID and explicitly marks gaps.

## Three independent measurements for input

1. Browser key or paste event and the exact outgoing PTY payload.
2. Composer state and the value actually submitted.
3. Parsed terminal text and its visibility time. A prompt can acknowledge a key quickly while its display lags behind.

Test with `busy=true`; otherwise TextInput may take its idle fast-echo path, missing bugs that occur during an active response. Include stale parent acknowledgements, bursts spanning PTY reads, Enter immediately after text, bracketed multiline paste, IME composition, wrapping, deletion, and explicit external draft replacement.

The regression in `activeInteraction.test.tsx` deliberately commits an earlier own-value echo between newer keystrokes. The baseline submitted `ac` instead of `abc`; the candidate must retain `abc` and still accept a genuine external replacement. A separate browser test submits exact known text while a deterministic response continues updating.

## Real terminal and browser fixture

- `ui-tui/src/__tests__/activeInteraction.test.tsx` mounts real Ink, MessageLine, TextInput and ModelPicker and parses their output through xterm. It exercises SGR press/release through the input parser, not direct calls to click callbacks.
- History sizes are 100, 1000 and 3800 **mounted** rich messages. The old keystroke benchmark's 30-item window is not equivalent.
- `WEB_TUI_BENCH_REPORT` optionally captures JSONL measurements. `readyCheckMs` includes a deliberate 500 ms wait and is not first-paint time. RSS is process RSS, not incremental memory attributable to one test. Parsed-display latency is not a browser-pixel paint measurement.
- `ui-tui/scripts/active-ui-fixture.tsx` is a deterministic simulated response, with no model requests or real credentials. Build it with `node ui-tui/scripts/build-active-ui-fixture.mjs "$FIXTURE_DIRECTORY"`.
- Launch a temporary loopback dashboard using an isolated `HERMES_HOME`, the candidate web build, and `HERMES_TUI_DIR="$FIXTURE_DIRECTORY"`. Do not reuse the live profile or restart serving for this test.
- Run `python3 scripts/test_web_tui_active_cdp.py --cdp "$CDP_URL" --url "$FIXTURE_URL" --output "$REPORT" --allow-fixture-input` for each display. It refuses to type unless the fixture marker is visible, waits 15 seconds, and uses owned tabs with isolated PTY identities.
- This browser test covers exact submission during streaming, provider/model/reasoning clicks, live reasoning collapse/expand, native drag-selection persistence, and inert historical clicks. It captures console and WebSocket evidence and exits nonzero on failure.

Model output and catalog responses are controlled fixtures. Browser, ChatPage, WebSocket, PTY, Ink components and xterm are real. This does not validate a real provider, live fan-out, or gateway recovery.

## Startup and performance

Record separate timestamps for connection, backend history projection, first Ink commit, frame serialization, PTY drain, xterm parse, first paint, first accepted key, and first visible key. Count resume generations, React commits and full terminal clears independently; three buffer expansions do not prove three complete renders.

Test cold process, warm attach, idle session and active session separately. Use matched baseline and candidate runs. Low aggregate host CPU does not exclude one saturated Node/browser event loop; sample per-process/per-thread CPU, event-loop delay, heap/RSS, GC and queued bytes. Additional cores cannot automatically parallelize React/Yoga reconciliation.

Current code does not contain the claimed history worker pool. RawAnsi covers code blocks/ANSI messages, not all completed history. Profile before adding workers; an immutable formatted-history cache must preserve formatting, resize correctness and all original message content.

## Transport and recovery

Keep container death, Node PTY death, internal gateway WebSocket closure and browser PTY WebSocket closure separate. Logs now include Node PID and close cleanliness/readiness. A 1006 closure alone does not prove a server crash; ping-timeout traces likewise need endpoint/PID correlation.

Test queued messages from replaced sockets, reconnect while streaming, missed heartbeats, server restart, and idle reaping. Do not increase timeouts or add retries as a substitute for finding event-loop stalls. GatewayClient regression tests cover stale-event isolation and heartbeat/reconnect behavior; a live soak remains necessary.

## Acceptance

Use fail-before/pass-after regressions and retain failed captures. Never infer no character loss from a final screenshot alone, no duplication from one banner, or correct clicks from the presence of SGR bytes. Do not report all capabilities as passed when mobile, clipboard, resize/reflow, active-provider recovery or overflow cases have not run.
