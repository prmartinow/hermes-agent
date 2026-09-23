interface ScreenRect { left: number; top: number; width: number; height: number }

/** Convert visible pixels to the live PTY viewport; old scrollback is inert. */
export function ptyClickCell(
  x: number, y: number, rect: ScreenRect, cols: number, rows: number,
  viewportY: number, baseY: number,
): { col: number; row: number } | null {
  if (rect.width <= 0 || rect.height <= 0 || cols <= 0 || rows <= 0) return null;
  const col = Math.floor((x - rect.left) * cols / rect.width);
  const visibleRow = Math.floor((y - rect.top) * rows / rect.height);
  if (col < 0 || col >= cols || visibleRow < 0 || visibleRow >= rows) return null;
  const row = viewportY + visibleRow - baseY;
  return row >= 0 && row < rows ? { col, row } : null;
}

/** Filter mouse motion, never the ordinary input following a mouse report. */
export function filterPtyMouseData(data: string): string {
  if (!data.startsWith("\x1b[<")) return data;
  // eslint-disable-next-line no-control-regex -- SGR mouse protocol
  return data.replace(/\x1b\[<(\d+);\d+;\d+[Mm]/g,
    (report, button: string) => Number(button) === 0 ? report : "");
}
