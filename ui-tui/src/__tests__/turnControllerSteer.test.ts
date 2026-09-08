import { beforeEach, describe, expect, it } from 'vitest'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'

describe('turnController.recordSteer — live mid-turn steer prompt ordering', () => {
  beforeEach(() => {
    resetTurnState()
    turnController.fullReset()
  })

  it('inserts steer prompt into streamSegments', () => {
    turnController.recordSteer('please also check auth.log')
    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(1)
    expect(segments[0]).toEqual({ role: 'user', text: 'please also check auth.log' })
  })

  it('seals in-flight reasoning before injecting steer prompt', () => {
    turnController.recordReasoningAvailable('analyzing current configuration', true)
    turnController.recordSteer('skip to error logs')

    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(2)
    expect(segments[0]?.thinking).toBe('analyzing current configuration')
    expect(segments[1]).toEqual({ role: 'user', text: 'skip to error logs' })
  })

  it('places steer prompt chronologically after preceding completed tool', () => {
    turnController.recordToolStart('call_1', 'read_file', 'path: config.json')
    turnController.recordToolComplete('call_1', 'read_file', undefined, 'read 50 lines')

    turnController.recordSteer('now inspect database.py')

    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(2)
    expect(segments[0]?.tools?.length).toBe(1)
    expect(segments[1]).toEqual({ role: 'user', text: 'now inspect database.py' })
  })

  it('deduplicates identical steer prompt arriving twice (optimistic + gateway event)', () => {
    turnController.recordSteer('deploy once')
    turnController.recordSteer('deploy once')

    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(1)
    expect(segments[0]).toEqual({ role: 'user', text: 'deploy once' })
  })

  it('appends subsequent tool calls after the steer prompt without backward merging', () => {
    // 1. Tool 1 completes
    turnController.recordToolStart('call_1', 'read_file', 'path: a.txt')
    turnController.recordToolComplete('call_1', 'read_file', undefined, 'ok')

    // 2. User steers
    turnController.recordSteer('focus on b.txt')

    // 3. Tool 2 starts and completes
    turnController.recordToolStart('call_2', 'read_file', 'path: b.txt')
    turnController.recordToolComplete('call_2', 'read_file', undefined, 'ok')

    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(3)
    // First segment is Tool 1
    expect(segments[0]?.tools?.[0]).toContain('Read File')
    // Second segment is the user steer prompt
    expect(segments[1]).toEqual({ role: 'user', text: 'focus on b.txt' })
    // Third segment is Tool 2
    expect(segments[2]?.tools?.[0]).toContain('Read File')
  })

  it('preserves the steer prompt in finalMessages upon message.complete', () => {
    turnController.recordToolStart('call_1', 'read_file', 'path: a.txt')
    turnController.recordToolComplete('call_1', 'read_file', undefined, 'ok')

    turnController.recordSteer('focus on b.txt')

    const { finalMessages, finalText } = turnController.recordMessageComplete({ text: 'All done!' })

    expect(finalText).toBe('All done!')
    expect(finalMessages).toHaveLength(3)
    // Tool 1
    expect(finalMessages[0]?.tools?.length).toBe(1)
    // User steer prompt
    expect(finalMessages[1]).toEqual({ role: 'user', text: 'focus on b.txt' })
    // Assistant final text
    expect(finalMessages[2]).toEqual({ role: 'assistant', text: 'All done!' })
  })
})
