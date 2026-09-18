import { describe, expect, it } from "vitest";
import { filterPtyMouseData, ptyClickCell } from "./pty-click";
const rect = { left: 20, top: 30, width: 800, height: 400 };

describe("PTY click coordinates", () => {
  it("uses screen bounds and accounts for scrollback", () => {
    expect(ptyClickCell(45, 145, rect, 80, 40, 100, 100)).toEqual({ col: 2, row: 11 });
    expect(ptyClickCell(45, 145, rect, 80, 40, 90, 100)).toEqual({ col: 2, row: 1 });
  });
  it("does not activate live controls when clicking immutable history or outside the screen", () => {
    expect(ptyClickCell(45, 145, rect, 80, 40, 0, 100)).toBeNull();
    expect(ptyClickCell(19, 145, rect, 80, 40, 100, 100)).toBeNull();
    expect(ptyClickCell(820, 145, rect, 80, 40, 100, 100)).toBeNull();
  });
});

it("preserves typed suffixes and bracketed pastes when filtering mouse motion", () => {
  expect(filterPtyMouseData("\x1b[<32;1;1Mtyped")).toBe("typed");
  expect(filterPtyMouseData("\x1b[<0;1;1Mtyped")).toBe("\x1b[<0;1;1Mtyped");
  const paste="\x1b[200~literal\x1b[<32;1;1M\x1b[201~";
  expect(filterPtyMouseData(paste)).toBe(paste);
});
