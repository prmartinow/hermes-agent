import type { Terminal } from "@xterm/xterm";

function activeElementToRestore(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const active = document.activeElement as HTMLElement | null;
  if (!active || active === document.body || active === document.documentElement) return null;
  return typeof active.focus === "function" ? active : null;
}

function restoreFocus(previous: HTMLElement | null, term: Terminal | null | undefined): void {
  if (previous && (typeof document.contains !== "function" || document.contains(previous))) {
    try {
      previous.focus({ preventScroll: true });
    } catch {
      /* focus can fail on detached or restricted elements */
    }
  }

  const active = typeof document === "undefined" ? null : document.activeElement;
  const accessibilityFocused = typeof active?.closest === "function" &&
    Boolean(active.closest(".xterm-accessibility-tree"));
  if (!previous && !accessibilityFocused && typeof term?.focus === "function") {
    try {
      term.focus();
    } catch {
      /* a terminal may have been disposed while the action ran */
    }
  }
}

/**
 * Executes an action while atomically capturing and restoring the active
 * element's DOM focus and the terminal's viewport scroll position.
 */
export async function withPreservedTerminalContext<T>(
  term: Terminal | null | undefined,
  action: () => T | Promise<T>,
): Promise<T> {
  const savedViewportY = term?.buffer?.active?.viewportY;
  const previousActiveElement = activeElementToRestore();

  try {
    return await action();
  } finally {
    restoreFocus(previousActiveElement, term);

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
