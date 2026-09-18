import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";
import { ReplayBoundaryGate } from "./pty-replay-boundary";

const { Terminal } = createRequire(import.meta.url)("@xterm/xterm");
const first = "11111111-1111-1111-1111-111111111111";
const second = "22222222-2222-2222-2222-222222222222";
const osc = (phase: string, id: string) => `\x1b]777;hermes-replay;${phase};${id}\x07`;

describe("explicit replay completion", () => {
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
});
