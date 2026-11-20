
import React from "react";
import { render } from "./ui-tui/packages/hermes-ink/dist/entry-exports.js";
import { AppLayout } from "./ui-tui/dist/components/appLayout.js";
import { resetOverlayState } from "./ui-tui/dist/app/overlayStore.js";
import { resetTurnState } from "./ui-tui/dist/app/turnStore.js";
import { resetUiState } from "./ui-tui/dist/app/uiStore.js";
import { performance } from "node:perf_hooks";

class Sink {
  columns = 120;
  rows = 42;
  isTTY = true;
  bytes = 0;
  writes = 0;
  listeners = new Map();
  write(chunk) {
    this.bytes += Buffer.byteLength(String(chunk ?? ""));
    this.writes++;
    return true;
  }
  on(event, fn) { this.listeners.set(event, fn); return this; }
  off(event) { this.listeners.delete(event); return this; }
  once(event, fn) { this.listeners.set(event, fn); return this; }
  removeListener(event) { this.listeners.delete(event); return this; }
}

const noop = () => {};
const stdin = { isTTY: true, setRawMode: noop, on: noop, off: noop, resume: noop, pause: noop };

function createProps(historySize, inputText) {
  const historyItems = Array.from({ length: historySize }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    text: "Message " + i + ": Some turn output and thoughts here.\n" + "Code snippet: def foo(): return bar()\n".repeat(4)
  }));

  const scrollRef = { current: {
    getScrollTop: () => 0, getPendingDelta: () => 0, getScrollHeight: () => historySize * 4,
    getViewportHeight: () => 30, getViewportTop: () => 0, isSticky: () => true,
    subscribe: () => () => {}, scrollBy: noop, scrollTo: noop, scrollToBottom: noop,
    setClampBounds: noop, getLastManualScrollAt: () => 0
  }};

  return {
    actions: { answerApproval: noop, answerClarify: noop, answerSecret: noop, answerSudo: noop, onModelSelect: noop, resumeById: noop, setStickyPrompt: noop },
    composer: { cols: 120, compIdx: 0, completions: [], empty: !inputText, handleTextPaste: () => null, input: inputText, inputBuf: [], pagerPageSize: 10, queueEditIdx: null, queuedDisplay: [], submit: noop, updateInput: noop },
    mouseTracking: false,
    progress: { activity: [], outcome: "", reasoning: "", reasoningActive: false, reasoningStreaming: false, reasoningTokens: 0, showProgressArea: false, showStreamingArea: false, streamPendingTools: [], streamSegments: [], streaming: "", subagents: [], toolTokens: 0, tools: [], turnTrail: [], todos: [] },
    status: { cwdLabel: "~/repo", goodVibesTick: 0, sessionStartedAt: Date.now(), showStickyPrompt: false, statusColor: "#98c379", stickyPrompt: "", turnStartedAt: Date.now(), voiceLabel: "voice off" },
    transcript: {
      historyItems,
      scrollRef,
      virtualHistory: { bottomSpacer: 0, end: historyItems.length, measureRef: () => noop, offsets: historyItems.map((_, i) => i * 4), start: Math.max(0, historyItems.length - 30), topSpacer: 0 },
      virtualRows: historyItems.map((msg, index) => ({ index, key: "m" + index, msg }))
    }
  };
}

const testSizes = [5, 25, 50, 100, 250, 500];
const results = [];

for (const size of testSizes) {
  resetUiState();
  resetTurnState();
  resetOverlayState();
  const stdout = new Sink();
  const inst = render(React.createElement(AppLayout, createProps(size, "")), { stdout, stdin, stderr: stdout, debug: false, exitOnCtrlC: false });

  // Warmup
  inst.render(React.createElement(AppLayout, createProps(size, "a")));

  const keystrokes = "Hello, this is a test prompt typed into Hermes composer!".split("");
  const timings = [];

  for (let i = 0; i < keystrokes.length; i++) {
    const text = keystrokes.slice(0, i + 1).join("");
    const t0 = performance.now();
    inst.render(React.createElement(AppLayout, createProps(size, text)));
    const t1 = performance.now();
    timings.push(t1 - t0);
  }

  inst.unmount();

  const avg = timings.reduce((a, b) => a + b, 0) / timings.length;
  const p95 = timings.sort((a, b) => a - b)[Math.floor(timings.length * 0.95)];
  results.push({
    transcript_messages: size,
    avg_keystroke_render_ms: Number(avg.toFixed(3)),
    p95_keystroke_render_ms: Number(p95.toFixed(3)),
    min_ms: Number(Math.min(...timings).toFixed(3)),
    max_ms: Number(Math.max(...timings).toFixed(3))
  });
}

console.log("BENCHMARK_RESULTS_START");
console.log(JSON.stringify(results, null, 2));
console.log("BENCHMARK_RESULTS_END");
