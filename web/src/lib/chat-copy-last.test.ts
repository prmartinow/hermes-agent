import type { Terminal } from "@xterm/xterm";
import { afterEach, describe, expect, it, vi } from "vitest";

import { copyLastResponse } from "./chat-copy-last";

const mocks = vi.hoisted(() => ({ getSessionMessages: vi.fn(), copy: vi.fn(async () => true) }));
vi.mock("./api", () => ({ api: { getSessionMessages: mocks.getSessionMessages } }));
vi.mock("./clipboard", () => ({ copyTextToClipboard: mocks.copy }));

afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("copyLastResponse", () => {
  it("fetches each current reply in the selected profile without typing into the composer", async () => {
    vi.useFakeTimers();
    const ws = { send: vi.fn(), readyState: 1 };
    const term = { getSelection: () => "", focus: vi.fn(), buffer: { active: { viewportY: 4 } } };
    const options = {
      wsRef: { current: ws as unknown as WebSocket }, termRef: { current: term as unknown as Terminal },
      copyResetRef: { current: null as ReturnType<typeof setTimeout> | null },
      onCopied: vi.fn(), onCopyReset: vi.fn(), sessionId: "conversation", profile: "secondary",
    };
    mocks.getSessionMessages.mockResolvedValueOnce({ messages: [{ role: "assistant", content: "first reply" }] });
    mocks.getSessionMessages.mockResolvedValueOnce({ messages: [{ role: "assistant", content: "new reply" }] });
    await copyLastResponse(options);
    await copyLastResponse(options);
    expect(mocks.getSessionMessages).toHaveBeenNthCalledWith(2, "conversation", "secondary");
    expect(mocks.copy).toHaveBeenNthCalledWith(1, "first reply");
    expect(mocks.copy).toHaveBeenNthCalledWith(2, "new reply");
    expect(ws.send).not.toHaveBeenCalled();
    expect(options.onCopied).toHaveBeenCalledTimes(2);
    vi.runAllTimers();
  });
});
