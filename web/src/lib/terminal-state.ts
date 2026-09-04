import type { Terminal } from "@xterm/xterm";

/**
 * Executes an action while atomically capturing and restoring the active
 * element's DOM focus and the terminal's viewport scroll position.
 */
export async function withPreservedTerminalContext<T>(
  term: Terminal | null | undefined,
  action: () => T | Promise<T>,
): Promise<T> {
  const savedViewportY = term?.buffer?.active?.viewportY;
  const previousActiveElement =
    typeof document !== "undefined" &&
    document.activeElement &&
    document.activeElement !== document.body &&
    document.activeElement !== document.documentElement &&
    typeof (document.activeElement as HTMLElement).focus === "function"
      ? (document.activeElement as HTMLElement)
      : null;

  try {
    return await action();
  } finally {
    // 1. Restore previous active element focus if valid and connected
    if (
      previousActiveElement &&
      (typeof document.contains !== "function" || document.contains(previousActiveElement)) &&
      typeof previousActiveElement.focus === "function"
    ) {
      try {
        previousActiveElement.focus({ preventScroll: true });
      } catch {
        /* ignore focus failures on detached or restricted elements */
      }
    }

    // 2. Ensure terminal component receives focus if no specific active element was tracked
    const isAccessibilityTreeFocused =
      typeof document !== "undefined" &&
      document.activeElement &&
      typeof (document.activeElement as HTMLElement).closest === "function" &&
      Boolean((document.activeElement as HTMLElement).closest(".xterm-accessibility-tree"));

    if (!previousActiveElement && !isAccessibilityTreeFocused && term && typeof term.focus === "function") {
      try {
        term.focus();
      } catch {
        /* ignore */
      }
    }

    // 3. Restore viewport scroll offset if displaced
    if (
      typeof savedViewportY === "number" &&
      term?.buffer?.active?.viewportY !== savedViewportY &&
      typeof term?.scrollToLine === "function"
    ) {
      try {
        term.scrollToLine(savedViewportY);
      } catch {
        /* ignore scroll failures */
      }
    }
  }
}
