import { createRequire } from 'node:module'
import { Writable } from 'node:stream'
import { describe, expect, it } from 'vitest'
import { LogUpdate } from './log-update.js'
import { CellWidth, CharPool, createScreen, HyperlinkPool, setCellAt, StylePool } from './screen.js'
import { writeDiffToTerminal } from './terminal.js'

const { Terminal } = createRequire(import.meta.url)('@xterm/xterm')

describe('inline viewport repaint preserves terminal scrollback', () => {
  it.each([100, 10000])('does not append historical rows for a %i-row frame', async height => {
    const cols = 40, rows = 12
    const stylePool = new StylePool()
    const screen = createScreen(cols, height, stylePool, new CharPool(), new HyperlinkPool())
    for (const cursorY of [height - 1, height]) {
      const origin = Math.max(0, Math.max(height, cursorY + 1) - rows)
      for (let y = origin; y < height; y++) {
        const text = `tail-${y}`
        for (let x = 0; x < text.length; x++) setCellAt(screen, x, y, { char: text[x]!, styleId: stylePool.none, width: CellWidth.Narrow, hyperlink: undefined })
      }
      const frame = { screen, viewport: { width: cols, height: rows }, cursor: { x: 0, y: cursorY, visible: true } }
      const prev = { ...frame, viewport: { width: cols + 1, height: rows } }
      const diff = new LogUpdate({ isTTY: true, stylePool }).render(prev, frame, false, false)
      let output = ''
      const stdout = new Writable({ write(chunk, _encoding, done) { output += chunk.toString(); done() } })
      writeDiffToTerminal({ stdout, stderr: stdout }, diff, true)
      expect((output.match(/\n/g) || []).length).toBeLessThan(rows)
      const term = new Terminal({ cols, rows, scrollback: 20000 })
      const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve))
      await write(Array.from({ length: 70 }, (_, i) => `old-${i}\r\n`).join(''))
      const baseY = term.buffer.active.baseY
      const history = Array.from({ length: baseY }, (_, i) => term.buffer.active.getLine(i).translateToString(true))
      await write(output)
      expect(term.buffer.active.baseY).toBe(baseY)
      expect(Array.from({ length: baseY }, (_, i) => term.buffer.active.getLine(i).translateToString(true))).toEqual(history)
      expect(term.buffer.active.getLine(baseY).translateToString(true)).toBe(`tail-${origin}`)
      expect(term.buffer.active.cursorY).toBe(rows - 1)
      term.dispose()
    }
  })
})
