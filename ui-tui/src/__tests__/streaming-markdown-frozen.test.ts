
import { describe, expect, it } from 'vitest'
import { createScanState, advanceScan } from '../components/streamingMarkdown.js'

describe('Streaming Markdown Frozen Block Invariants', () => {
  it('freezes markdown horizontal rules and headings immediately so dynamic tail never cascades', () => {
    const state = createScanState()
    const chunk1 = '---\n'
    advanceScan(chunk1, state)
    expect(state.blocks.length).toBe(1)
    expect(state.blocks[0]).toBe('---\n')

    const chunk2 = chunk1 + '### C. Automatic PTY Worker Reconnection (ChatPage.tsx)\n'
    advanceScan(chunk2, state)
    expect(state.blocks.length).toBe(2)
    expect(state.blocks[1]).toBe('### C. Automatic PTY Worker Reconnection (ChatPage.tsx)\n')

    // Stream progressive bullet point tokens in the tail
    const chunk3 = chunk2 + '• The Root Cause: When a child PTY worker exited (code 4410) during an active'
    advanceScan(chunk3, state)
    expect(state.blocks.length).toBe(2)
    const tail3 = chunk3.slice(state.settledLen)
    expect(tail3).not.toContain('### C.')
    expect(tail3).toBe('• The Root Cause: When a child PTY worker exited (code 4410) during an active')

    const chunk4 = chunk2 + '• The Root Cause: When a child PTY worker exited (code 4410) during an active ongoing session, Chat'
    advanceScan(chunk4, state)
    expect(state.blocks.length).toBe(2)
    const tail4 = chunk4.slice(state.settledLen)
    expect(tail4).not.toContain('### C.')
    expect(tail4).toBe('• The Root Cause: When a child PTY worker exited (code 4410) during an active ongoing session, Chat')
  })
})
