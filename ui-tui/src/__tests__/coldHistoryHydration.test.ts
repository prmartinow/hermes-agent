import { describe, expect, it, vi } from 'vitest'
import { PassThrough } from 'stream'
import { performColdHistoryHydration } from '../app/coldHistoryHydration.js'
import type { SessionInfo } from '../types.js'
import { DEFAULT_THEME } from '../theme.js'

describe('Cold History Hydration Pipeline', () => {
  const mockInfo: SessionInfo = {
    id: 'test-sess',
    model: 'test-model',
    version: '0.31.0',
    profile_name: 'default',
    skills: ['web-tui'],
    tools: ['terminal']
  }

  it('keeps all items in live memory for small conversations (<= maxMounted)', async () => {
    const stdout = new PassThrough()
    let written = ''
    stdout.on('data', chunk => { written += chunk.toString() })

    const mockMessages = Array.from({ length: 15 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      text: `Message ${i + 1}`,
      row_id: i + 1
    }))

    const request = vi.fn().mockResolvedValue({
      messages: mockMessages,
      count: 15,
      cursor: 0,
      next_cursor: 15,
      total: 15,
      snapshot_token: 'snap-123',
      has_more: false
    })

    const result = await performColdHistoryHydration({
      gateway: { request },
      sessionId: 'test-sess',
      cols: 80,
      theme: DEFAULT_THEME,
      info: mockInfo,
      stdout: stdout as any,
      maxMounted: 120
    })

    expect(result.appendedToScrollback).toBe(false)
    expect(result.materializedCount).toBe(0)
    expect(written).toBe('')
    // Intro + 15 items
    expect(result.initialLiveMessages.length).toBe(16)
    expect(result.initialLiveMessages[0]?.kind).toBe('intro')
    expect(result.initialLiveMessages[1]?.text).toBe('Message 1')
  })

  it('streams prefix to stdout and bounds live tail to maxMounted for large conversations', async () => {
    const stdout = new PassThrough()
    let written = ''
    stdout.on('data', chunk => { written += chunk.toString() })

    // 150 items across 2 pages (100 in page 1, 50 in page 2)
    const page1Messages = Array.from({ length: 100 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      text: `Message ${i + 1}`,
      row_id: i + 1
    }))

    const page2Messages = Array.from({ length: 50 }, (_, i) => ({
      role: (i + 100) % 2 === 0 ? 'user' : 'assistant',
      text: `Message ${i + 101}`,
      row_id: i + 101
    }))

    const request = vi.fn().mockImplementation((method, params) => {
      if (!params.snapshot_token) {
        return Promise.resolve({
          messages: page1Messages,
          count: 100,
          cursor: 0,
          next_cursor: 100,
          total: 150,
          snapshot_token: 'snap-456',
          has_more: true
        })
      }
      return Promise.resolve({
        messages: page2Messages,
        count: 50,
        cursor: 100,
        next_cursor: 150,
        total: 150,
        snapshot_token: 'snap-456',
        has_more: false
      })
    })

    const result = await performColdHistoryHydration({
      gateway: { request },
      sessionId: 'test-sess',
      cols: 80,
      theme: DEFAULT_THEME,
      info: mockInfo,
      stdout: stdout as any,
      maxMounted: 120
    })

    expect(result.appendedToScrollback).toBe(true)
    expect(result.materializedCount).toBe(30) // 150 total - 120 live = 30 materialized
    expect(result.initialLiveMessages.length).toBe(120)
    expect(result.initialLiveMessages[0]?.text).toBe('Message 31')
    expect(result.initialLiveMessages[119]?.text).toBe('Message 150')

    // Stdout received materialized output
    expect(written).toContain('Message 1')
    expect(written).toContain('Message 30')
    // Message 31 and above are in the live tail, not materialized stdout
    expect(written).not.toContain('Message 31')
  })

  it('aborts immediately and throws ColdHydrationCancelledError when isCancelled is true', async () => {
    const stdout = new PassThrough()
    const request = vi.fn().mockResolvedValue({
      messages: [{ role: 'user', text: 'Msg 1', row_id: 1 }],
      count: 1,
      has_more: false
    })

    await expect(performColdHistoryHydration({
      gateway: { request },
      sessionId: 'test-sess',
      cols: 80,
      theme: DEFAULT_THEME,
      info: mockInfo,
      stdout: stdout as any,
      isCancelled: () => true
    })).rejects.toThrow('Cold hydration was cancelled')

    expect(request).not.toHaveBeenCalled()
  })

  it('aborts and throws ColdHydrationCancelledError if cancelled while page request is awaiting', async () => {
    const stdout = new PassThrough()
    let cancelled = false

    const request = vi.fn().mockImplementation(async () => {
      // simulate cancellation arriving while RPC is in flight
      cancelled = true
      return {
        messages: [{ role: 'user', text: 'Msg 1', row_id: 1 }],
        count: 1,
        has_more: true,
        next_cursor: 1
      }
    })

    await expect(performColdHistoryHydration({
      gateway: { request },
      sessionId: 'test-sess',
      cols: 80,
      theme: DEFAULT_THEME,
      info: mockInfo,
      stdout: stdout as any,
      isCancelled: () => cancelled
    })).rejects.toThrow('Cold hydration was cancelled')

    // Only called once, does not request page 2
    expect(request).toHaveBeenCalledTimes(1)
  })
})
