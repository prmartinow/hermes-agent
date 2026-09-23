import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";
import {
  ReplayBoundaryGate,
  parseReplayStartControlMessage,
} from "./pty-replay-boundary";

const { Terminal } = createRequire(import.meta.url)("@xterm/xterm");
const first = "11111111-1111-1111-1111-111111111111";
const second = "22222222-2222-2222-2222-222222222222";
const osc = (phase: string, id: string) => `\x1b]777;hermes-replay;${phase};${id}\x07`;

describe("explicit replay completion", () => {
  it("parses replay-start out-of-band JSON control message", () => {
    expect(parseReplayStartControlMessage(JSON.stringify({ type: "replay-start", generation: first }))).toEqual({
      type: "replay-start",
      generation: first,
    });
    expect(parseReplayStartControlMessage(JSON.stringify({ type: "resume", id: "foo" }))).toBeNull();
    expect(parseReplayStartControlMessage("not json")).toBeNull();
    expect(parseReplayStartControlMessage(JSON.stringify({ type: "replay-start", generation: "invalid-uuid" }))).toBeNull();
  });

  it("rejects stale generations and ignores prompt text", () => {
    const gate = new ReplayBoundaryGate();
    expect(gate.receive("❯ ready │")).toBeNull();
    gate.receive(`hermes-replay;begin;${first}`);
    gate.receive(`hermes-replay;end;${first}`);
    gate.receive(`hermes-replay;begin;${second}`);
    expect(gate.complete(first)).toBe(false);
    expect(gate.receive(`hermes-replay;end;${first}`)).toBeNull();
    expect(gate.complete(second)).toBe(false);
    gate.receive(`hermes-replay;end;${second}`);
    expect(gate.complete(second)).toBe(true);
    expect(gate.complete(second)).toBe(false);
  });

  it("processes split OSC boundaries through real xterm before acknowledging", async () => {
    const term = new Terminal({ cols: 80, rows: 12 });
    const gate = new ReplayBoundaryGate();
    let completed = false;
    let complete!: () => void;
    const completion = new Promise<void>(resolve => { complete = resolve; });
    term.parser.registerOscHandler(777, (data: string) => {
      const boundary = gate.receive(data);
      if (boundary?.phase === "end") {
        term.write("", () => { completed = gate.complete(boundary.generation); complete(); });
      }
      return !!boundary;
    });
    const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve));
    for (const char of osc("begin", first)) await write(char);
    await write("❯ a historical prompt\r\n");
    expect(completed).toBe(false);
    await write("last historical line\r\n");
    for (const char of osc("end", first)) await write(char);
    await completion;
    expect(completed).toBe(true);
    expect(term.buffer.active.getLine(1).translateToString(true)).toBe("last historical line");
    term.dispose();
  });

  it("accepts warm snapshot end boundaries when the begin was trimmed", () => {
    const gate = new ReplayBoundaryGate();
    expect(gate.receive(`hermes-replay;end;${first}`)?.generation).toBe(first);
    expect(gate.complete(first)).toBe(true);
    gate.reset();
    expect(gate.receive(`hermes-replay;abort;${second}`)?.phase).toBe("abort");
  });

  it("ignores stale snapshot markers when pinned until expected frame end completes through real xterm", async () => {
    const term = new Terminal({ cols: 80, rows: 12 });
    const gate = new ReplayBoundaryGate();
    let completed = false;
    let completeResolve!: () => void;
    const completion = new Promise<void>(resolve => { completeResolve = resolve; });

    term.parser.registerOscHandler(777, (data: string) => {
      const boundary = gate.receive(data);
      if (boundary?.phase === "end") {
        term.write("", () => {
          if (gate.complete(boundary.generation)) {
            completed = true;
            completeResolve();
          }
        });
      }
      return !!boundary;
    });

    const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve));

    // Gate is pinned to expected generation (second) upon warm attach replay-start
    gate.pin(second);
    expect(gate.isPinned()).toBe(true);
    expect(gate.getPinnedGeneration()).toBe(second);

    // Stale snapshot bytes arrive with markers from previous generation (first)
    for (const char of osc("begin", first)) await write(char);
    await write("stale prompt\r\n");
    for (const char of osc("end", first)) await write(char);

    // Yield to xterm write callback
    await new Promise<void>(resolve => term.write("", resolve));
    expect(completed).toBe(false);
    expect(gate.isPinned()).toBe(true);

    // Now expected frame redraw arrives with second generation
    for (const char of osc("begin", second)) await write(char);
    await write("fresh prompt\r\n");
    for (const char of osc("end", second)) await write(char);

    await completion;
    expect(completed).toBe(true);
    // Pin is released after successful matching completion
    expect(gate.isPinned()).toBe(false);
    term.dispose();
  });

  it("handles repeated attachment with pin and unpin cycles", async () => {
    const term = new Terminal({ cols: 80, rows: 12 });
    const gate = new ReplayBoundaryGate();
    let completedGen: string | null = null;

    term.parser.registerOscHandler(777, (data: string) => {
      const boundary = gate.receive(data);
      if (boundary?.phase === "end") {
        term.write("", () => {
          if (gate.complete(boundary.generation)) {
            completedGen = boundary.generation;
          }
        });
      }
      return !!boundary;
    });

    const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve));

    // --- Attachment 1 ---
    gate.pin(first);
    expect(gate.isPinned()).toBe(true);
    await write(osc("begin", first) + "turn 1\r\n" + osc("end", first));
    await new Promise<void>(resolve => term.write("", resolve));
    expect(completedGen).toBe(first);
    expect(gate.isPinned()).toBe(false);

    // --- Attachment 2 ---
    completedGen = null;
    gate.pin(second);
    expect(gate.isPinned()).toBe(true);

    // Snapshot contains old turn 1 markers from first attachment
    await write(osc("begin", first) + "turn 1 replay\r\n" + osc("end", first));
    await new Promise<void>(resolve => term.write("", resolve));
    expect(completedGen).toBeNull();
    expect(gate.isPinned()).toBe(true);

    // Redraw delivers second generation markers
    await write(osc("begin", second) + "turn 2 active\r\n" + osc("end", second));
    await new Promise<void>(resolve => term.write("", resolve));
    expect(completedGen).toBe(second);
    expect(gate.isPinned()).toBe(false);

    term.dispose();
  });

  it("accepts marker-erased snapshot where begin marker was wiped from ring buffer", async () => {
    const term = new Terminal({ cols: 80, rows: 12 });
    const gate = new ReplayBoundaryGate();
    let completed = false;
    let completeResolve!: () => void;
    const completion = new Promise<void>(resolve => { completeResolve = resolve; });

    term.parser.registerOscHandler(777, (data: string) => {
      const boundary = gate.receive(data);
      if (boundary?.phase === "end") {
        term.write("", () => {
          if (gate.complete(boundary.generation)) {
            completed = true;
            completeResolve();
          }
        });
      }
      return !!boundary;
    });

    const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve));

    // Pinned to first generation
    gate.pin(first);

    // Snapshot has NO begin marker (it rolled off the ring buffer), only lines and the end marker
    await write("long scrollback line 1\r\n");
    await write("long scrollback line 2\r\n");
    await write(osc("end", first));

    await completion;
    expect(completed).toBe(true);
    expect(gate.isPinned()).toBe(false);

    term.dispose();
  });
});
