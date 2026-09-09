import { describe, expect, it } from 'vitest'
import { LogUpdate } from './log-update.js'
import { StylePool } from './screen.js'

describe('Inline Resize Invariants (Hermes Web TUI)', () => {
  it('VALIDATION 1: Inline resize emits clearTerminal to wipe stale scrollback before reload', () => {
    const stylePool = new StylePool()
    const width = 80
    const height = 100
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
      viewport: { width: 100, height: viewportHeight }
    }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(prevFrame, frame, false, false) // altScreen = false

    // Assert clearTerminal in inline mode so stale scrollback is erased before reload
    expect(diff.some(p => p.type === 'clearTerminal')).toBe(true)
  })
})
