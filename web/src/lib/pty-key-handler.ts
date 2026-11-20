import type { Terminal } from "@xterm/xterm";
import type { RefObject } from "react";

import { copyTextSync, copyTextToClipboard } from "./clipboard";
import { resolvePtyKeyboardShortcut, sendPtyShortcutSequence } from "./pty-keyboard-shortcuts";
import type { PtyConnectionState } from "./pty-reconnect";
import { isViewportPinnedToBottom } from "./pty-scroll";
import { withPreservedTerminalContext } from "./terminal-state";

interface PtyKeyHandlerOptions {
  term: Terminal;
  wsRef: RefObject<WebSocket | null>;
  ptyStateRef: RefObject<PtyConnectionState>;
  stickToBottomRef: RefObject<boolean>;
  isMac: boolean;
}

function scrollToBottomIfNeeded({ term, stickToBottomRef }: PtyKeyHandlerOptions): boolean {
  if (isViewportPinnedToBottom(term.buffer.active)) return false;
  term.scrollToBottom();
  stickToBottomRef.current = true;
  return true;
}

function copySelection(ev: KeyboardEvent, selection: string, options: PtyKeyHandlerOptions): void {
  const { term, wsRef } = options;
  if (selection) {
    void withPreservedTerminalContext(term, async () => {
      copyTextSync(selection);
      await copyTextToClipboard(selection);
      term.clearSelection();
      if (typeof window !== "undefined") window.getSelection()?.removeAllRanges();
    });
  } else if (ev.ctrlKey && !ev.shiftKey && !ev.metaKey) {
    // Bare Ctrl+C is an intentional interrupt (or Ink copy). Modified copy
    // shortcuts must never interrupt an active turn.
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send("\x03");
  }
  ev.preventDefault();
}

function isModifiedNavigation(ev: KeyboardEvent, isMac: boolean): boolean {
  return ev.shiftKey || ev.ctrlKey || (isMac && ev.metaKey);
}

function isBareEnd(ev: KeyboardEvent): boolean {
  return ev.key === "End" && !ev.shiftKey && !ev.ctrlKey && !ev.metaKey && !ev.altKey;
}

function navigateHistory(ev: KeyboardEvent, options: PtyKeyHandlerOptions): boolean {
  const { term, stickToBottomRef, isMac } = options;
  if (ev.key === "PageUp" || ev.key === "PageDown") {
    term.scrollPages(ev.key === "PageUp" ? -1 : 1);
    stickToBottomRef.current = ev.key === "PageDown" && isViewportPinnedToBottom(term.buffer.active);
  } else if (ev.key === "Home" && isModifiedNavigation(ev, isMac)) {
    term.scrollToTop();
    stickToBottomRef.current = false;
  } else if (ev.key === "End" && isModifiedNavigation(ev, isMac)) {
    term.scrollToBottom();
    stickToBottomRef.current = true;
  } else if (isBareEnd(ev)) {
    if (!scrollToBottomIfNeeded(options)) return false;
  } else {
    return false;
  }
  ev.preventDefault();
  return true;
}

function isCopyKey(ev: KeyboardEvent, isMac: boolean): boolean {
  return ((isMac ? ev.metaKey : ev.ctrlKey) ||
    (ev.shiftKey && (ev.metaKey || ev.ctrlKey))) && !ev.altKey && ev.key.toLowerCase() === "c";
}

function isPasteKey(ev: KeyboardEvent): boolean {
  return (ev.metaKey || ev.ctrlKey) && !ev.altKey && ev.key.toLowerCase() === "v";
}

function isWritingKey(ev: KeyboardEvent): boolean {
  return (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey) ||
    ev.key === "Backspace" || ev.key === "Delete" || ev.key === "Enter";
}

export function createPtyKeyHandler(options: PtyKeyHandlerOptions): (ev: KeyboardEvent) => boolean {
  return (ev) => {
    if (ev.type !== "keydown") return true;
    // Tab escapes the terminal (WCAG 2.1.2).
    if (ev.key === "Tab") return false;

    const { term, wsRef, ptyStateRef, isMac } = options;
    const selection = term.getSelection();
    const shortcut = resolvePtyKeyboardShortcut(ev, isMac, Boolean(selection));
    if (shortcut === "copy" || isCopyKey(ev, isMac)) {
      copySelection(ev, selection, options);
      return false;
    }
    if (shortcut === "delete-word-backward" || shortcut === "delete-word-forward") {
      ev.preventDefault();
      scrollToBottomIfNeeded(options);
      sendPtyShortcutSequence(wsRef.current, ptyStateRef.current,
        shortcut === "delete-word-backward" ? "\x17" : "\x1bd");
      return false;
    }
    if (isPasteKey(ev)) {
      // Skip xterm's Ctrl+V processing without cancelling the browser event.
      scrollToBottomIfNeeded(options);
      return false;
    }
    if (navigateHistory(ev, options)) return false;

    if (isWritingKey(ev)) {
      if (term.hasSelection()) term.clearSelection();
      scrollToBottomIfNeeded(options);
    }
    return true;
  };
}
