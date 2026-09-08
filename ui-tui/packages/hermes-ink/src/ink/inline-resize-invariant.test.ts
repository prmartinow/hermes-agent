
import { describe, expect, it } from 'vitest'
import { LogUpdate } from './log-update.js'
import { StylePool } from './screen.js'

describe('Inline Resize Invariants (Hermes Web TUI)', () => {
  it('VALIDATION 1: Inline resize emits bounded newlines (<= viewport height) and zero scrollback erase', () => {
    const stylePool = new StylePool()
    const width = 80
    const height = 3500
    const viewportHeight = 40

    const cells = new Uint32Array(width * height * 2)
    const frame = {
      viewport: { width, height: viewportHeight },
      screen: {
        width,
        height,
        cells,
        charPool: new Map(),
        hyperlinkPool: new Map(),
        written: new Uint8Array(width * height)
      },
      cursor: { x: 0, y: height }
    }

    const prevFrame = {
      ...frame,
      viewport: { width: 100, height: viewportHeight } // resize from 100 to 80
    }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(prevFrame, frame, false, false) // altScreen = false

    // Assert zero clearTerminal ([3J) patches in inline mode
    expect(diff.some(p => p.type === 'clearTerminal')).toBe(false)
    expect(diff.some(p => p.type === 'clearScreen')).toBe(true)

    // Count newlines emitted to stdout
    let newlineCount = 0
    for (const p of diff) {
      if (p.type === 'stdout') {
        newlineCount += (p.content.match(/\n/g) || []).length
        expect(p.content).not.toContain('\x1b[3J')
      }
    }

    // Must be exactly viewportHeight - 1 (39 lines with newline, 40th line with \r only)
    expect(newlineCount).toBe(viewportHeight - 1)
  })
})
