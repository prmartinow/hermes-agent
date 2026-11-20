// @vitest-environment jsdom
import type { Terminal } from '@xterm/xterm'
import { describe, expect, it, vi } from 'vitest'

import { createPtyKeyHandler } from './pty-key-handler'

function setup() {
  const active = { viewportY: 2, baseY: 10 }
  const term = {
    buffer: { active },
    getSelection: vi.fn(() => ''),
    hasSelection: vi.fn(() => false),
    clearSelection: vi.fn(),
    scrollPages: vi.fn(),
    scrollToTop: vi.fn(),
    scrollToBottom: vi.fn(() => { active.viewportY = active.baseY }),
  }
  const socket = { readyState: WebSocket.OPEN, send: vi.fn() }
  const stickToBottomRef = { current: true }
  const handler = createPtyKeyHandler({
    term: term as unknown as Terminal,
    wsRef: { current: socket as unknown as WebSocket },
    ptyStateRef: { current: 'open' },
    stickToBottomRef,
    isMac: false,
  })
  return { active, term, socket, stickToBottomRef, handler }
}

function key(key: string, options: KeyboardEventInit = {}) {
  return new KeyboardEvent('keydown', { key, cancelable: true, ...options })
}

describe('terminal keyboard navigation', () => {
  it('navigates history while keeping the sticky-bottom state coherent', () => {
    const { active, term, stickToBottomRef, handler } = setup()
    const up = key('PageUp')
    expect(handler(up)).toBe(false)
    expect(up.defaultPrevented).toBe(true)
    expect(term.scrollPages).toHaveBeenCalledWith(-1)
    expect(stickToBottomRef.current).toBe(false)

    const end = key('End')
    expect(handler(end)).toBe(false)
    expect(term.scrollToBottom).toHaveBeenCalledOnce()
    expect(stickToBottomRef.current).toBe(true)
    expect(active.viewportY).toBe(active.baseY)
    expect(handler(key('End'))).toBe(true)
  })

  it('sends the word-deletion shortcut after returning to the prompt', () => {
    const { term, socket, handler } = setup()
    const event = key('Backspace', { ctrlKey: true })
    expect(handler(event)).toBe(false)
    expect(event.defaultPrevented).toBe(true)
    expect(term.scrollToBottom).toHaveBeenCalledOnce()
    expect(socket.send).toHaveBeenCalledWith('\x17')
  })
})
