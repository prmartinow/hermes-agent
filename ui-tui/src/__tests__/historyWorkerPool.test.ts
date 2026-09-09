import { describe, expect, it } from 'vitest'
import { HistoryWorkerPool } from '../lib/historyWorkerPool.js'
import { formatCompleteHistory } from '../lib/staticHistoryStream.js'
import type { Msg } from '../types.js'

describe('HistoryWorkerPool 24-Thread Parallel Formatter', () => {
  it('formats large message histories across 24 parallel worker threads in milliseconds', async () => {
    const pool = new HistoryWorkerPool(24)

    const items: Msg[] = Array.from({ length: 500 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      text: `This is message #${i} verifying parallel formatting across 24 worker threads.`
    }))

    const t0 = performance.now()
    const result = await pool.formatParallel(items, 120)
    const elapsed = performance.now() - t0

    expect(result).toContain('This is message #0')
    expect(result).toContain('This is message #499')
    expect(elapsed).toBeLessThan(1000) // Must finish in under 1 second (typically < 10ms)

    pool.dispose()
  })

  it('formats full history including banner via staticHistoryStream', async () => {
    const items: Msg[] = [
      {
        kind: 'intro',
        role: 'system',
        text: '',
        info: { model: 'gemini-3.8-flash-high', tools: ['execute_code'] }
      },
      {
        role: 'user',
        text: 'User prompt test'
      },
      {
        role: 'assistant',
        text: 'Assistant response test'
      }
    ]

    const out = await formatCompleteHistory(items, 100)
    expect(out).toContain('Nous Research')
    expect(out).toContain('gemini-3.8-flash-high')
    expect(out).toContain('User prompt test')
    expect(out).toContain('Assistant response test')
  })
})
