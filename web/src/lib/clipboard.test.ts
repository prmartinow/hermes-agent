import { afterEach, describe, expect, it, vi } from "vitest";
import { copyTextSync, copyTextToClipboard } from "./clipboard";

const originalNavigator = globalThis.navigator;
const originalDocument = globalThis.document;
const originalWindow = globalThis.window;

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
  setGlobal("navigator", originalNavigator);
  setGlobal("document", originalDocument);
  setGlobal("window", originalWindow);
  vi.restoreAllMocks();
});

describe("copyTextToClipboard", () => {
  it("uses navigator.clipboard when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setGlobal(
      "navigator",
      { clipboard: { writeText } } as unknown as Navigator,
    );
    setGlobal("document", undefined);

    await expect(copyTextToClipboard("CODEX-1234")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("CODEX-1234");
  });

  it("falls back to selection copy when Clipboard API fails", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("not allowed"));
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    setGlobal(
      "navigator",
      { clipboard: { writeText } } as unknown as Navigator,
    );
    setGlobal("document", {
      body: { appendChild, removeChild },
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => null),
    } as unknown as Document);

    await expect(copyTextToClipboard("CODEX-1234")).resolves.toBe(true);

    expect(writeText).toHaveBeenCalledWith("CODEX-1234");
    expect(textarea.value).toBe("CODEX-1234");
    expect(appendChild).toHaveBeenCalledWith(textarea);
    expect(textarea.select).toHaveBeenCalled();
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(removeChild).toHaveBeenCalledWith(textarea);
  });

  it("uses selection copy directly in insecure browser contexts", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    setGlobal(
      "navigator",
      { clipboard: { writeText } } as unknown as Navigator,
    );
    setGlobal(
      "window",
      { isSecureContext: false } as unknown as Window & typeof globalThis,
    );
    setGlobal("document", {
      body: { appendChild, removeChild },
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => null),
    } as unknown as Document);

    await expect(copyTextToClipboard("CODEX-1234")).resolves.toBe(true);

    expect(writeText).not.toHaveBeenCalled();
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("uses ClipboardItem for rich text when available in secure contexts", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    setGlobal(
      "navigator",
      { clipboard: { write } } as unknown as Navigator,
    );
    setGlobal("window", { isSecureContext: true } as unknown as Window & typeof globalThis);
    (globalThis as unknown as { ClipboardItem: unknown }).ClipboardItem = class MockClipboardItem {
      data: unknown;
      constructor(data: unknown) {
        this.data = data;
      }
    };

    await expect(
      copyTextToClipboard({ text: "plain", html: "<b>plain</b>" }),
    ).resolves.toBe(true);

    expect(write).toHaveBeenCalled();
  });

  it("returns false when no copy mechanism is available", async () => {
    setGlobal("navigator", {} as Navigator);
    setGlobal("document", undefined);

    await expect(copyTextToClipboard("CODEX-1234")).resolves.toBe(false);
  });

  it("gracefully handles null or undefined payloads", async () => {
    await expect(copyTextToClipboard(null)).resolves.toBe(true);
    await expect(copyTextToClipboard(undefined)).resolves.toBe(true);
    await expect(copyTextToClipboard({ text: "" })).resolves.toBe(true);
  });
});

describe("copyTextSync", () => {
  it("executes document.execCommand synchronously", () => {
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    setGlobal("document", {
      body: { appendChild, removeChild },
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => null),
    } as unknown as Document);

    expect(copyTextSync("SYNC-TEST")).toBe(true);
    expect(textarea.value).toBe("SYNC-TEST");
    expect(appendChild).toHaveBeenCalledWith(textarea);
    expect(textarea.select).toHaveBeenCalled();
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(removeChild).toHaveBeenCalledWith(textarea);
  });

  it("returns false for empty text or missing document", () => {
    expect(copyTextSync("")).toBe(false);
    setGlobal("document", undefined);
    expect(copyTextSync("text")).toBe(false);
  });

  it("restores focus to previous active element after copying", () => {
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    const mockActiveElement = {
      focus: vi.fn(),
    } as unknown as HTMLElement;

    const contains = vi.fn((el) => el === mockActiveElement);

    setGlobal("document", {
      activeElement: mockActiveElement,
      body: { appendChild, removeChild },
      contains,
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => null),
    } as unknown as Document);

    expect(copyTextSync("SYNC-TEST")).toBe(true);
    expect(mockActiveElement.focus).toHaveBeenCalledWith({
      preventScroll: true,
    });
  });

  it("handles missing or disconnected active element gracefully", () => {
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    const disconnectedElement = {
      focus: vi.fn(),
    } as unknown as HTMLElement;

    setGlobal("document", {
      activeElement: disconnectedElement,
      body: { appendChild, removeChild },
      contains: vi.fn(() => false),
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => null),
    } as unknown as Document);

    expect(copyTextSync("SYNC-TEST")).toBe(true);
    expect(disconnectedElement.focus).not.toHaveBeenCalled();
  });

  it("does not mutate or re-add selection ranges to avoid scrolling virtual viewports", () => {
    const appendChild = vi.fn();
    const removeChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    const textarea = {
      focus: vi.fn(),
      select: vi.fn(),
      setAttribute: vi.fn(),
      setSelectionRange: vi.fn(),
      style: {},
      value: "",
    } as unknown as HTMLTextAreaElement;

    const mockSelection = {
      addRange: vi.fn(),
      getRangeAt: vi.fn(),
      rangeCount: 1,
      removeAllRanges: vi.fn(),
    };

    setGlobal("document", {
      body: { appendChild, removeChild },
      createElement: vi.fn(() => textarea),
      execCommand,
      getSelection: vi.fn(() => mockSelection as unknown as Selection),
    } as unknown as Document);

    expect(copyTextSync("SYNC-TEST")).toBe(true);
    expect(mockSelection.addRange).not.toHaveBeenCalled();
    expect(mockSelection.removeAllRanges).not.toHaveBeenCalled();
  });
});
