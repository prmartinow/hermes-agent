import { afterEach, describe, expect, it, vi } from "vitest";
import { withPreservedTerminalContext } from "./terminal-state";
import type { Terminal } from "@xterm/xterm";

const originalDocument = globalThis.document;

function setGlobal<K extends keyof typeof globalThis>(
  key: K,
  value: (typeof globalThis)[K] | undefined,
) {
  Object.defineProperty(globalThis, key, {
    configurable: true,
    value,
  });
}

afterEach(() => {
  setGlobal("document", originalDocument);
  vi.restoreAllMocks();
});

describe("withPreservedTerminalContext", () => {
  it("preserves and restores viewportY if action modifies it", async () => {
    let currentViewportY = 120;
    const mockTerm = {
      buffer: {
        active: {
          get viewportY() {
            return currentViewportY;
          },
        },
      },
      focus: vi.fn(),
      scrollToLine: vi.fn((line: number) => {
        currentViewportY = line;
      }),
    } as unknown as Terminal;

    await withPreservedTerminalContext(mockTerm, () => {
      // Simulate viewport jump mid-action
      currentViewportY = 0;
    });

    expect(mockTerm.scrollToLine).toHaveBeenCalledWith(120);
    expect(currentViewportY).toBe(120);
  });

  it("does not invoke scrollToLine if viewportY remained unchanged", async () => {
    const mockTerm = {
      buffer: {
        active: {
          viewportY: 55,
        },
      },
      focus: vi.fn(),
      scrollToLine: vi.fn(),
    } as unknown as Terminal;

    await withPreservedTerminalContext(mockTerm, () => {
      // no viewport change
    });

    expect(mockTerm.scrollToLine).not.toHaveBeenCalled();
  });

  it("restores focus to activeElement if present", async () => {
    const mockActiveElement = {
      focus: vi.fn(),
    } as unknown as HTMLElement;

    setGlobal("document", {
      activeElement: mockActiveElement,
      body: {} as HTMLElement,
      documentElement: {} as HTMLElement,
      contains: vi.fn(() => true),
    } as unknown as Document);

    const mockTerm = {
      buffer: { active: { viewportY: 10 } },
      focus: vi.fn(),
      scrollToLine: vi.fn(),
    } as unknown as Terminal;

    await withPreservedTerminalContext(mockTerm, () => {});

    expect(mockActiveElement.focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it("calls term.focus() if activeElement is body or absent", async () => {
    const mockBody = {} as HTMLElement;
    setGlobal("document", {
      activeElement: mockBody,
      body: mockBody,
      documentElement: {} as HTMLElement,
      contains: vi.fn(() => true),
    } as unknown as Document);

    const mockTerm = {
      buffer: { active: { viewportY: 10 } },
      focus: vi.fn(),
      scrollToLine: vi.fn(),
    } as unknown as Terminal;

    await withPreservedTerminalContext(mockTerm, () => {});

    expect(mockTerm.focus).toHaveBeenCalled();
  });

  it("does not force term.focus() if activeElement is inside .xterm-accessibility-tree", async () => {
    const mockA11yNode = {
      closest: vi.fn((sel) => sel === ".xterm-accessibility-tree"),
      focus: vi.fn(),
    } as unknown as HTMLElement;

    setGlobal("document", {
      activeElement: mockA11yNode,
      body: {} as HTMLElement,
      documentElement: {} as HTMLElement,
      contains: vi.fn(() => true),
    } as unknown as Document);

    const mockTerm = {
      buffer: { active: { viewportY: 10 } },
      focus: vi.fn(),
      scrollToLine: vi.fn(),
    } as unknown as Terminal;

    await withPreservedTerminalContext(mockTerm, () => {});

    expect(mockTerm.focus).not.toHaveBeenCalled();
  });

  it("gracefully handles null terminal", async () => {
    const result = await withPreservedTerminalContext(null, () => "success");
    expect(result).toBe("success");
  });
});
