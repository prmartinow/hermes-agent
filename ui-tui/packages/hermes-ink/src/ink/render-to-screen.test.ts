import React from 'react'
import { describe, expect, it } from 'vitest'
import Text from './components/Text.js'
import Box from './components/Box.js'
import { renderNodeToAnsi, serializeScreenToAnsi } from './render-to-screen.js'
import {
  createScreen,
  StylePool,
  CharPool,
  HyperlinkPool,
  setCellAt,
  CellWidth
} from './screen.js'
import { stripAnsi } from '@hermes/shared/ansi'
import { link as oscLink, LINK_END } from './termio/osc.js'

describe('renderNodeToAnsi static Ink serializer', () => {
  it('renders React Ink components directly to styled ANSI lines', () => {
    const element = React.createElement(
      Box,
      { flexDirection: 'column' },
      React.createElement(Text, null, 'Hello Static World'),
      React.createElement(Text, null, 'Second Line')
    )

    const ansi = renderNodeToAnsi(element, 80)
    expect(ansi).toContain('Hello Static World')
    expect(ansi).toContain('Second Line')
    expect(stripAnsi(ansi)).toBe('Hello Static World\nSecond Line')
  })

  it('preserves exact ANSI colors, weights, and resets without redundant transitions', () => {
    const pool = new StylePool()
    const charPool = new CharPool()
    const hyperlinkPool = new HyperlinkPool()
    const screen = createScreen(40, 3, pool, charPool, hyperlinkPool)

    const greenId = pool.intern([{ type: 'ansi', code: '\x1b[32m', endCode: '\x1b[39m' }])
    const redBoldId = pool.intern([
      { type: 'ansi', code: '\x1b[31m', endCode: '\x1b[39m' },
      { type: 'ansi', code: '\x1b[1m', endCode: '\x1b[22m' }
    ])

    // Green span on row 0
    setCellAt(screen, 0, 0, { char: 'A', styleId: greenId, width: CellWidth.Narrow })
    setCellAt(screen, 1, 0, { char: 'B', styleId: greenId, width: CellWidth.Narrow })
    // Red bold on row 0
    setCellAt(screen, 2, 0, { char: 'C', styleId: redBoldId, width: CellWidth.Narrow })

    const ansi = serializeScreenToAnsi(screen, pool)
    const lines = ansi.split('\n')

    expect(lines.length).toBe(3)
    // Row 0 has green applied once before 'A', not repeated before 'B'
    expect(lines[0]).toBe('\x1b[32mAB\x1b[31m\x1b[1mC\x1b[22m\x1b[39m')
    // Row 1 & 2 are empty trimmed lines
    expect(lines[1]).toBe('')
    expect(lines[2]).toBe('')
  })

  it('correctly formats OSC 8 hyperlinks across cells and resets at line end', () => {
    const pool = new StylePool()
    const charPool = new CharPool()
    const hyperlinkPool = new HyperlinkPool()
    const screen = createScreen(30, 2, pool, charPool, hyperlinkPool)

    const linkUrl = 'https://hermes.agent/docs'
    const linkText = 'Doc'
    for (let i = 0; i < linkText.length; i++) {
      setCellAt(screen, i, 0, {
        char: linkText[i]!,
        styleId: pool.none,
        width: CellWidth.Narrow,
        hyperlink: linkUrl
      })
    }
    setCellAt(screen, 3, 0, { char: '!', styleId: pool.none, width: CellWidth.Narrow })

    const ansi = serializeScreenToAnsi(screen, pool)
    const lines = ansi.split('\n')

    // Expect OSC 8 link, link text, OSC 8 end, then plain character
    expect(lines[0]).toBe(oscLink(linkUrl) + 'Doc' + LINK_END + '!')
  })

  it('handles wide characters (emojis, CJK) and SpacerTail skips properly', () => {
    const pool = new StylePool()
    const charPool = new CharPool()
    const hyperlinkPool = new HyperlinkPool()
    const screen = createScreen(40, 2, pool, charPool, hyperlinkPool)

    // Wide emoji
    setCellAt(screen, 0, 0, { char: '🚀', styleId: pool.none, width: CellWidth.Wide })
    // Wide CJK
    setCellAt(screen, 2, 0, { char: '中', styleId: pool.none, width: CellWidth.Wide })
    // Regular char
    setCellAt(screen, 4, 0, { char: 'X', styleId: pool.none, width: CellWidth.Narrow })

    const ansi = serializeScreenToAnsi(screen, pool)
    const lines = ansi.split('\n')

    expect(lines[0]).toBe('🚀中X')
    expect(stripAnsi(lines[0]!)).toBe('🚀中X')
  })

  it('preserves trailing spaces when styled with background color and resets at line end', () => {
    const pool = new StylePool()
    const charPool = new CharPool()
    const hyperlinkPool = new HyperlinkPool()
    const screen = createScreen(10, 2, pool, charPool, hyperlinkPool)

    const bgRedId = pool.intern([{ type: 'ansi', code: '\x1b[41m', endCode: '\x1b[49m' }])
    setCellAt(screen, 0, 0, { char: 'X', styleId: pool.none, width: CellWidth.Narrow })
    setCellAt(screen, 1, 0, { char: ' ', styleId: bgRedId, width: CellWidth.Narrow })
    setCellAt(screen, 2, 0, { char: ' ', styleId: bgRedId, width: CellWidth.Narrow })

    const ansi = serializeScreenToAnsi(screen, pool)
    const lines = ansi.split('\n')

    // Because the line ends with reset '\x1b[49m', trimEnd does not strip the styled spaces
    expect(lines[0]).toBe('X\x1b[41m  \x1b[49m')
  })
})
