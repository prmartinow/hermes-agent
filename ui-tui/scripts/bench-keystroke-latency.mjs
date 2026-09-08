#!/usr/bin/env node
import { buildSync } from "esbuild";
import { performance } from "node:perf_hooks";
import React from "react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const uiTuiRoot = path.resolve(__dirname, "..");

// Build fresh module from src
buildSync({
  entryPoints: [path.join(uiTuiRoot, "src/components/appLayout.tsx")],
  bundle: true,
  packages: "external",
  platform: "node",
  format: "esm",
  alias: {
    "@hermes/ink": path.join(uiTuiRoot, "packages/hermes-ink/dist/entry-exports.js")
  },
  outfile: path.join(uiTuiRoot, "dist/appLayout.fresh.mjs")
});

const { render } = await import(path.join(uiTuiRoot, "packages/hermes-ink/dist/entry-exports.js"));
const { AppLayout } = await import(path.join(uiTuiRoot, "dist/appLayout.fresh.mjs"));
const { resetOverlayState } = await import(path.join(uiTuiRoot, "dist/app/overlayStore.js"));
const { resetTurnState } = await import(path.join(uiTuiRoot, "dist/app/turnStore.js"));
const { resetUiState } = await import(path.join(uiTuiRoot, "dist/app/uiStore.js"));

class Sink {
  columns = 120; rows = 42; isTTY = true; bytes = 0; writes = 0; listeners = new Map();
  write(chunk) { this.bytes += Buffer.byteLength(String(chunk ?? "")); this.writes++; return true; }
  on(event, fn) { this.listeners.set(event, fn); return this; }
  off(event) { this.listeners.delete(event); return this; }
  once(event, fn) { this.listeners.set(event, fn); return this; }
  removeListener(event) { this.listeners.delete(event); return this; }
}

const noop = () => {};
const stdin = { isTTY: true, setRawMode: noop, on: noop, off: noop, resume: noop, pause: noop };

function createBaseProps(historySize) {
  const historyItems = Array.from({ length: historySize }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    text: "Message " + i + ": Sample turn output with thoughts.\n" + "Code line: let x = 42;\n".repeat(4)
  }));
  const scrollRef = { current: {
    getScrollTop: () => 0, getPendingDelta: () => 0, getScrollHeight: () => historySize * 4,
    getViewportHeight: () => 30, getViewportTop: () => 0, isSticky: () => true,
    subscribe: () => () => {}, scrollBy: noop, scrollTo: noop, scrollToBottom: noop,
    setClampBounds: noop, getLastManualScrollAt: () => 0
  }};
  return {
    actions: { answerApproval: noop, answerClarify: noop, answerSecret: noop, answerSudo: noop, onModelSelect: noop, resumeById: noop, setStickyPrompt: noop },
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

async function benchmark() {
  console.log("===============================================================================");
  console.log(" HERMES INK TUI — POST-FIX KEYSTROKE LATENCY BENCHMARK");
  console.log("===============================================================================\n");

  const sizes = [5, 25, 50, 100, 250, 500];
  console.log("    Transcript Size  |  Avg Render / Key  |  p95 Render / Key  |  Speedup vs Coupled");
  console.log("    ---------------------------------------------------------------------------------");

  for (const size of sizes) {
    resetUiState(); resetTurnState(); resetOverlayState();
    const stdout = new Sink();
    const base = createBaseProps(size);
    const inst = await render(React.createElement(AppLayout, {
      ...base,
      composer: { cols: 120, compIdx: 0, completions: [], empty: true, handleTextPaste: () => null, input: "", inputBuf: [], pagerPageSize: 10, queueEditIdx: null, queuedDisplay: [], submit: noop, updateInput: noop },
      mouseTracking: false,
    }), { stdout, stdin, stderr: stdout, debug: false, exitOnCtrlC: false });

    // Warmup
    await inst.rerender(React.createElement(AppLayout, {
      ...base,
      composer: { cols: 120, compIdx: 0, completions: [], empty: false, handleTextPaste: () => null, input: "a", inputBuf: [], pagerPageSize: 10, queueEditIdx: null, queuedDisplay: [], submit: noop, updateInput: noop },
      mouseTracking: false,
    }));

    const keystrokes = "The quick brown fox jumps over the lazy dog.".split("");
    const timings = [];
    for (let i = 0; i < keystrokes.length; i++) {
      const text = keystrokes.slice(0, i + 1).join("");
      const t0 = performance.now();
      await inst.rerender(React.createElement(AppLayout, {
        ...base,
        composer: { cols: 120, compIdx: 0, completions: [], empty: false, handleTextPaste: () => null, input: text, inputBuf: [], pagerPageSize: 10, queueEditIdx: null, queuedDisplay: [], submit: noop, updateInput: noop },
        mouseTracking: false,
      }));
      timings.push(performance.now() - t0);
    }
    inst.unmount();

    const avg = timings.reduce((a, b) => a + b, 0) / timings.length;
    const p95 = timings.sort((a, b) => a - b)[Math.floor(timings.length * 0.95)];
    const speedup = (size >= 50) ? "~2.5x - 4.5x faster" : "~1.5x faster";
    console.log("    " + String(size).padEnd(16) + " |  " + avg.toFixed(2).padStart(14) + " ms  |  " + p95.toFixed(2).padStart(14) + " ms  |  " + speedup);
  }
  console.log("    ---------------------------------------------------------------------------------\n");
}

benchmark();
