import type { Terminal } from "@xterm/xterm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PtyOutputStream } from "./pty-output-stream";

const generation = "12345678-1234-1234-1234-123456789abc";

function fixture(initialResume: string | null = null) {
  let frame: FrameRequestCallback | null = null;
  let osc: (data: string) => boolean = () => false;
  const callbacks: Array<() => void> = [];
  const boundaryDispose = vi.fn();
  const term = {
    parser: { registerOscHandler: vi.fn((_id: number, handler: (data: string) => boolean) => { osc = handler; return { dispose: boundaryDispose }; }) },
    reset: vi.fn(), clear: vi.fn(), hasSelection: vi.fn(() => false), scrollToBottom: vi.fn(),
    write: vi.fn((_text: string, callback?: () => void) => { if (callback) callbacks.push(callback); }),
  };
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frame = callback; return 1; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn(() => { frame = null; }));
  const setHydrating = vi.fn();
  const setBanner = vi.fn();
  const stickToBottomRef = { current: true };
  const stream = new PtyOutputStream({
    term: term as unknown as Terminal, initialResume, stickToBottomRef, setHydrating, setBanner, onText: vi.fn(),
  });
  return { stream, term, callbacks, boundaryDispose, setHydrating, setBanner, stickToBottomRef,
    osc: (data: string) => osc(data), flush: () => frame?.(0) };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("PtyOutputStream", () => {
  it("preserves ANSI and chunked UTF-8 in order", () => {
    const { stream, term, flush } = fixture();
    stream.receive(new Uint8Array([0xe2]).buffer);
    stream.receive(new Uint8Array([0x82, 0xac]).buffer);
    stream.receive("\x1b[2J\x1b[3J\r\n\r\ntext");
    flush();
    expect(term.write.mock.calls[0][0]).toBe("€\x1b[2J\x1b[3J\r\n\r\ntext");
    stream.dispose();
  });

  it("finishes only after the pinned end marker has passed the terminal parser", () => {
    const { stream, term, setHydrating, osc, flush, callbacks } = fixture("session");
    stream.receive(JSON.stringify({ type: "replay-start", generation }));
    stream.receive("visible transcript");
    flush();
    expect(setHydrating).not.toHaveBeenCalledWith(false);
    expect(osc("hermes-replay;end;ffffffff-ffff-ffff-ffff-ffffffffffff")).toBe(false);
    osc(`hermes-replay;begin;${generation}`);
    osc(`hermes-replay;end;${generation}`);
    expect(term.write.mock.calls.at(-1)?.[0]).toBe("");
    expect(setHydrating).not.toHaveBeenCalledWith(false);
    callbacks.at(-1)?.();
    expect(setHydrating).toHaveBeenLastCalledWith(false);
    stream.dispose();
  });

  it("lets a later user selection suppress the queued follow-scroll callback", () => {
    const { stream, term, flush, callbacks } = fixture("session");
    stream.receive("transcript");
    flush();
    term.hasSelection.mockReturnValue(true);
    callbacks.at(-1)?.();
    expect(term.scrollToBottom).not.toHaveBeenCalled();
    stream.dispose();
  });

  it("cleans pending writes, replay warnings and parser handlers on disposal", () => {
    const { stream, term, flush, boundaryDispose, setBanner } = fixture("session");
    stream.receive("pending");
    stream.dispose();
    flush();
    vi.runAllTimers();
    stream.receive("late output");
    expect(term.write).not.toHaveBeenCalled();
    expect(setBanner).not.toHaveBeenCalled();
    expect(boundaryDispose).toHaveBeenCalledOnce();
  });
});
