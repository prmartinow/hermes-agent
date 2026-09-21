import React from 'react'
import { describe, expect, it } from 'vitest'
import Text from './components/Text.js'
import Box from './components/Box.js'
import { renderNodeToAnsi } from './render-to-screen.js'
import { stripAnsi } from '@hermes/shared/ansi'

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
})
