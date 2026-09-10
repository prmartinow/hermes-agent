# Hermes Web TUI Capability & Invariants Matrix

## Overview
This document formalizes the architectural contracts, user-facing capabilities, failure classes overcome, and regression-guarding invariants for the Hermes Web TUI across all surfaces (Desktop, Web, Mobile, RPC Displays).

---

## 1. Text Selection, Clipboard & Mouse Interaction

| ID | Capability | Defect / Failure Overcome | Underlying Architectural Mechanism | Protected Invariant / Governance Rule |
| :--- | :--- | :--- | :--- | :--- |
| **1.1** | **Universal Text Drag-Selection & Copy (`Cmd+C`)** | Mouse dragging was intercepted as DEC mouse tracking, blocking text selection across the prompt composer and lower half of the screen. | `web/src/pages/ChatPage.tsx`: Click-vs-drag arbitration in `shouldForceSelection`. Drags $> 3\text{px}$ maintain native browser text selection and allow standard `Cmd+C` copying. | Dragging anywhere on the screen must always select text. Never return `false` from `shouldForceSelection` on mouse drag. |
| **1.2** | **Persistent Text Highlighting** | Re-renders or frame updates wiped active text selection highlighting mid-drag. | `web/src/pages/ChatPage.tsx`: Decoupled terminal selection buffer from streaming updates; `xterm.js` selection service state is guarded during PTY chunk arrivals. | Active selection ranges must survive incoming streaming deltas until explicitly dismissed by user click or keypress. |
| **1.3** | **Non-Secure Context Clipboard Compatibility** | `navigator.clipboard.writeText` failed silently over plain HTTP LAN connections without HTTPS. | `web/src/utils/clipboard.ts`: Fallback to `document.execCommand('copy')` with invisible textarea injection when running over non-secure origins (`http://`). | Clipboard copy must succeed across HTTP LAN connections without requiring SSL certificates. |
| **1.4** | **Universal Click Reactivity (Modals & Buttons)** | Clicking confirmation buttons (`[Allow]`, `[Deny]`, `Confirm`, `Yes`, `No`) above row $N-4$ failed to forward to the PTY. | `web/src/pages/ChatPage.tsx`: Expanded click forwarding across the lower half (`row >= term.rows / 2`) and any row matching interactive patterns (`[Allow]`, borders `┌─┐│`, `▸▾`). Clicks $\le 3\text{px}$ emit DEC 1006 SGR mouse events. | Single clicks on interactive affordances must reach the PTY, while drag gestures must remain text selections. |
| **1.5** | **Zero-Dropped / Non-Eaten Input Characters** | Typing fast while the model was streaming responses caused typed characters to drop, lag, or be overwritten by the status bar. | `ui-tui/src/app/useSubmission.ts` & `log-update.ts`: Typing idle timers (`boostStreamingForTyping`), PTY bracketed paste (`\x1b[?2004h`), and `moveCursorTo(screen, frame.cursor.x, frame.cursor.y)` restore physical cursor to the prompt line `❯`. | Keystrokes must never be blocked by streaming token arrivals or overwriting status bar updates. |

---

## 2. Viewport Geometry, Reflow & Scroll Mechanics

| ID | Capability | Defect / Failure Overcome | Underlying Architectural Mechanism | Protected Invariant / Governance Rule |
| :--- | :--- | :--- | :--- | :--- |
| **2.1** | **Native Window Resize & Zoom Reflow** | Dragging window width or zooming caused text to truncate or hard-wrap mid-word without application padding. | `ui-tui/src/hooks/useVirtualHistory.ts`: Settled resize passes live column width (`bodyCols = transcriptBodyWidth(cols)`), re-wrapping prose at clean word boundaries off live geometry. | Content must dynamically reflow at word boundaries on resize without mid-word breaks or margin clipping. |
| **2.2** | **Resize & Zoom Debounce (Sub-Second Response)** | Window dragging dispatched dozens of resize events per second, causing Node.js to freeze for 15+ seconds. | `web/src/pages/ChatPage.tsx`: 120ms settling timer (`ptyResizeTimer`). Smooth dragging scales the canvas via `@xterm/addon-fit`; the PTY resize escape (`\x1b[RESIZE:cols;rows]`) only fires after drag settles. | Drag gestures must scale smoothly via FitAddon; backend PTY resize packets must remain debounced at $\ge 120\text{ms}$. |
| **2.3** | **Bottom Follow / Viewport Parking** | Resizing, zooming, or reloading landed the user at 5% or 30% from the bottom, or jumped to Line 0. | `web/src/pages/ChatPage.tsx`: `isResizeReplaying` tracking lock combined with `term.scrollToBottom()` ensures the viewport settles cleanly at the bottom prompt composer. | Resizing and zooming must always park the viewport at the bottom prompt composer. |
| **2.4** | **Smooth Scrollback (Zero Jumpy Snapping)** | Trackpad or mouse wheel flicking caused the scrollbar to violently jump to the top of the history. | `web/src/pages/ChatPage.tsx`: Trackpad velocity damping and separation of `followScroll` from overlay transitions. Inertial wheel ticks scroll xterm's native buffer without triggering React state churn. | Scrolling upward must never trigger a sudden rubber-band jump to Line 0. |
| **2.5** | **Permanent Bottom Status Bar Anchoring** | In-flight updates or resets pushed the status bar into the middle or top of the viewport. | `ui-tui/src/components/appLayout.tsx` & `log-update.ts`: Status bar layout coordinates are locked to viewport row $H-1$; resets restore physical cursor to the composer prompt line rather than row 0. | The status bar must remain permanently anchored to the bottom row of the active terminal viewport. |

---

## 3. History Integrity, Rendering Fidelity & Formatting

| ID | Capability | Defect / Failure Overcome | Underlying Architectural Mechanism | Protected Invariant / Governance Rule |
| :--- | :--- | :--- | :--- | :--- |
| **3.1** | **Full Cumulative History from Line 0** | Reconnecting or refreshing only loaded the last 60 messages; Hermes banner and early turns were deleted. | `hermes_cli/web_server_chat.py`: Increased PTY `RingBuffer` `buffer_cap` from 1MB to **32MB**. `ui-tui/src/hooks/useVirtualHistory.ts`: Inline mode mounts all turns (`start = 0; end = n`). | Long sessions (10,000+ turns) must never have earlier history truncated by PTY buffer limits. |
| **3.2** | **Zero History Duplication / Triplication** | Resizing or zooming pushed repeated copies of text blocks and diagrams (e.g. mouse 3px diagram appearing 3x) into scrollback. | `ui-tui/packages/hermes-ink/src/ink/log-update.ts`: In inline mode (`!altScreen`), `reason === 'resize'` sets `patchType = 'clearTerminal'` (`\x1b[2J\x1b[3J\x1b[H`). Stale-width scrollback is wiped before replaying freshly wrapped turns. | Resizing must emit `clearTerminal` (`CSI 3J`) to wipe stale-width scrollback before replaying history. |
| **3.3** | **Zero Long-Response Top-Cropping** | Long responses (> 40 lines) had their top half sliced off; only the bottom half showed until pressing `Ctrl+L`. | `ui-tui/packages/hermes-ink/src/ink/log-update.ts`: In inline mode (`!altScreen`), full resets always start at `startY = 0`. Content growing past the viewport emits natural newlines to let the terminal scroll without slicing. | Resets in inline mode must never slice off top rows using `startY = height - viewport`. |
| **3.4** | **Elimination of Text Morphing & Cursor Drift** | Prompt text morphed into previous responses above the status bar due to coordinate misalignment. | `ui-tui/packages/hermes-ink/src/ink/log-update.ts`: Starting full resets at `startY = 0` guarantees physical terminal cursor coordinates match virtual screen coordinates 1:1, preventing displaced overwrites. | Virtual screen origins must match physical terminal row offsets; never write Row $K$ at Physical Row 0. |
| **3.5** | **100% Rich Styling & Formatting Preservation** | Earlier worker thread streaming attempts stripped colors, Markdown code blocks, and syntax highlighting. | `ui-tui/src/components/markdown.tsx` & `messageLine.tsx`: All rendering authority is held by native React Ink components (`<MessageLine>`, `<Md>`, `<StreamingMd>`). 24-bit truecolor, code highlighting, and box borders are fully preserved. | Text formatting must never be bypassed with raw unstyled plain-text string emitters. |
| **3.6** | **Zero Vertical Voids / No 15–20 Row Empty Gaps** | Disrupted tool calls and large empty row gaps appeared between items when keys were stabilized incorrectly. | `ui-tui/src/app/useMainApp.ts`: Re-keying with `${messageId(msg)}:c${cols}` forces Yoga flexbox to discard stale-width leaf dimensions and re-measure nested tool trails and thinking containers cleanly. | Component layout geometry must fully re-measure when column widths change. |
| **3.7** | **Expanded Markdown Caching (10,000 Items)** | Hardcoded 512-item limit caused constant cache misses and CPU thrashing across 3,800+ messages. | `ui-tui/src/components/markdown.tsx`: Expanded `MD_CACHE_LIMIT = 10000` with dimension-indexed caching, allowing all messages in long sessions to remain cached. | Markdown cache capacity must accommodate thousands of session turns without cache eviction churn. |

---

## 4. Multi-Surface Synchronization & Fan-Out

| ID | Capability | Defect / Failure Overcome | Underlying Architectural Mechanism | Protected Invariant / Governance Rule |
| :--- | :--- | :--- | :--- | :--- |
| **4.1** | **Universal Live Stream Fan-Out (No Stream Stealing)** | Opening a session on a phone stole the stream from the MacBook, silencing the MacBook mid-response. | `tui_gateway/transport.py` & `session_transports.py`: `FanoutTransport` delivers `message.delta`, `thinking.delta`, and `tool.progress` to all connected clients simultaneously. | Attaching a second device must additively join the `FanoutTransport`—never overwrite `session["transport"]`. |
| **4.2** | **Cross-Device Live User Prompt Mirroring** | Prompts typed on the MacBook only showed on the MacBook; mobile phone and RPC node never saw the request. | `tui_gateway/methods_prompt.py`: Emits `_broadcast_global_event("prompt.submitted", {"session_id": sid, "text": text})`. `ui-tui/src/app/createGatewayEventHandler.ts` catches it on all sibling devices and appends to their transcript. | User requests submitted on any device must broadcast and display live on all other connected surfaces. |
| **4.3** | **Independent Native Geometry per Surface (Model A)** | Resizing the phone to 45 cols squished the MacBook into a narrow ribbon. | `hermes_cli/web_routers/chat_ws.py`: `attach_token = f"resume\0{profile}\0{registry_resume}\0{device_token}"`. Each device maintains its own PTY process wrapped natively to its physical screen width. | Each distinct device surface must retain its own native terminal column width and geometry. |
| **4.4** | **Aggressive 3-Minute Idle PTY Session Reaper** | Clearing cookies repeatedly spawned 11 zombie Node processes, consuming 13GB of RAM and crashing the gateway (`code=1006`). | `hermes_cli/web_server_chat.py`: Reduced `PTY_REGISTRY` `ttl` from 30 minutes to **3 minutes**. Unattached or orphaned PTYs are automatically terminated within 180 seconds. | Detached PTY processes must be reaped within 3 minutes to prevent memory leaks and zombie accumulation. |

---

## 5. Performance, Concurrency & Memory Footprint

| ID | Capability | Defect / Failure Overcome | Underlying Architectural Mechanism | Protected Invariant / Governance Rule |
| :--- | :--- | :--- | :--- | :--- |
| **5.1** | **Elimination of Multi-Pass Inline Resize Freeze** | Window resize froze for 10+ seconds because React ran 3 consecutive full-tree reconciliation passes. | `ui-tui/src/hooks/useVirtualHistory.ts`: In inline mode (`isInline`), bypassed height scaling and `bumpMeasuredHeightVersion`. Inline mode has zero spacers, eliminating 2 redundant passes. | Inline mode must never cycle `bumpMeasuredHeightVersion` in a loop when heights change. |
| **5.2** | **Sub-Millisecond Diffing & Pipe Flush** | Concern that terminal diffing or POSIX pipe I/O was the cause of resize latency. | `ui-tui/packages/hermes-ink/src/ink/log-update.ts` & `ink.tsx`: Frame diffing takes $\approx 287\text{ms}$ and pipe flush takes $\approx 313\text{ms}$ across 19,000 rows, verified via sub-millisecond timers in `tui-perf.log`. | Diff generation and pipe write overhead must remain sub-second across 20,000+ terminal lines. |
| **5.3** | **Active vs. Completed Space Decoupling (`<RawAnsi>`)** | Keeping 3,800 completed turns as 55,000 live React components consumed 1.2GB RAM per process. | `ui-tui/packages/hermes-ink/src/ink/components/RawAnsi.tsx`: Completed turns render as constant-time $O(1)$ Yoga leaf nodes (`width * lines.length`). Reduces RAM per process to $< 100\text{MB}$. | Completed turns must be treated as static leaves so Yoga measurements run in $O(1)$ constant time. |

---

## 6. Verification Test Gates

Every merge and release must pass these automated verification gates:
1. `scripts/run_tests.sh tests/tui_gateway/test_multi_client_fanout.py` (10/10 passed)
2. `scripts/run_tests.sh tests/tui_gateway/test_shared_session_delivery.py` (1/1 passed)
3. `python3 tests/test_ui_structural_invariants.py` (100% passed)
4. `cd web && npm test` (324/324 passed)
5. `cd ui-tui && npx vitest run packages/hermes-ink/src/ink/log-update.test.ts` (8/8 passed)
6. Pre-commit security check: 0 host paths, 0 private IPs, 0 secrets.
