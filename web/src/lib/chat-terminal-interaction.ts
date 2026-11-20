import type { Terminal } from "@xterm/xterm";
import type { RefObject } from "react";

import { ptyClickCell } from "./pty-click";

interface MouseReport {
  action: number;
  button: number;
}

interface TerminalMouseHooks {
  _selectionService?: {
    _enabled: boolean;
    disable(): void;
    shouldForceSelection(): boolean;
  };
  coreMouseService?: {
    triggerMouseEvent(event: MouseReport): boolean;
  };
}

/** Scroll locally without sending wheel reports into the composer. */
export function installChatTerminalWheelScroll(term: Terminal): void {
  let partialScroll = 0;
  term.attachCustomWheelEventHandler((event: WheelEvent) => {
    const delta = event.deltaY;
    if (!delta) return false;
    let amount = event.deltaMode === WheelEvent.DOM_DELTA_LINE
      ? delta
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
        ? delta * (term.rows || 24)
        : delta / 40;
    if (event.deltaMode === WheelEvent.DOM_DELTA_PIXEL && Math.abs(delta) < 50) amount *= 0.3;
    partialScroll += amount;
    const lines = Math.trunc(partialScroll);
    if (lines !== 0) {
      partialScroll -= lines;
      term.scrollLines(lines);
    }
    event.preventDefault();
    event.stopPropagation();
    return false;
  });
}

/** Preserve natural selection while sending single clicks on the live viewport. */
export function installChatTerminalSelection(
  term: Terminal,
  wsRef: RefObject<WebSocket | null>,
  stickToBottomRef: RefObject<boolean>,
): () => void {
  const core = (term as unknown as { _core?: TerminalMouseHooks })._core;
  const selection = core?._selectionService;
  const element = term.element;
  if (!selection || !element) return () => undefined;

  const originalDisable = selection.disable;
  const originalForceSelection = selection.shouldForceSelection;
  selection.disable = function () { this._enabled = false; };
  selection.shouldForceSelection = () => true;

  let clickStart: { x: number; y: number } | null = null;
  const onMouseDown = (event: MouseEvent) => {
    if (event.button === 0) clickStart = { x: event.clientX, y: event.clientY };
  };
  const onMouseUp = (event: MouseEvent) => {
    if (!clickStart || event.button !== 0) return;
    const distance = Math.hypot(event.clientX - clickStart.x, event.clientY - clickStart.y);
    clickStart = null;
    if (distance > 3 || term.hasSelection()) return;
    const rect = element.querySelector(".xterm-screen")?.getBoundingClientRect();
    if (!rect) return;
    const cell = ptyClickCell(event.clientX, event.clientY, rect, term.cols, term.rows,
      term.buffer.active.viewportY, term.buffer.active.baseY);
    if (cell && wsRef.current?.readyState === WebSocket.OPEN) {
      const { col, row } = cell;
      wsRef.current.send(`\x1b[<0;${col + 1};${row + 1}M\x1b[<0;${col + 1};${row + 1}m`);
    }
  };
  const onMouseMove = (event: MouseEvent) => {
    if (event.buttons === 0 && term.hasSelection()) event.stopImmediatePropagation();
  };
  element.addEventListener("mousedown", onMouseDown, true);
  element.addEventListener("mouseup", onMouseUp, true);
  element.addEventListener("mousemove", onMouseMove, true);
  const selectionChange = term.onSelectionChange(() => {
    if (term.hasSelection()) stickToBottomRef.current = false;
  });
  const mouse = core.coreMouseService;
  const originalMouseEvent = mouse?.triggerMouseEvent;
  if (mouse && originalMouseEvent) {
    mouse.triggerMouseEvent = function (event: MouseReport) {
      if (event.action === 32 && event.button === 3 && term.hasSelection()) return false;
      return originalMouseEvent.call(mouse, event);
    };
  }
  return () => {
    element.removeEventListener("mousedown", onMouseDown, true);
    element.removeEventListener("mouseup", onMouseUp, true);
    element.removeEventListener("mousemove", onMouseMove, true);
    selectionChange.dispose();
    selection.disable = originalDisable;
    selection.shouldForceSelection = originalForceSelection;
    if (mouse && originalMouseEvent) mouse.triggerMouseEvent = originalMouseEvent;
  };
}
