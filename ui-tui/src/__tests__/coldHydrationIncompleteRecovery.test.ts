import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React, { useEffect, useRef, useState } from 'react'
import { renderSync } from '@hermes/ink'

import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, resetUiState } from '../app/uiStore.js'
import type { Msg, SessionInfo } from '../types.js'
import {
  useSessionLifecycle,
  type UseSessionLifecycleOptions
} from '../app/useSessionLifecycle.js'
import { ColdHydrationCancelledError, performColdHistoryHydration } from '../app/coldHistoryHydration.js'

// Force INLINE_MODE and DASHBOARD_TUI_MODE to true for replay boundary generation & cold hydration
const envState = { dashboardTuiMode: true, inlineMode: true }
vi.mock('../config/env.js', async importActual => {
  const actual = await importActual<typeof import('../config/env.js')>()
  return {
    ...actual,
    get DASHBOARD_TUI_MODE() {
      return envState.dashboardTuiMode
    },
    get INLINE_MODE() {
      return envState.inlineMode
    }
  }
})

describe('cold hydration incomplete recovery & cancellation consistency', () => {
  let stdoutWrites: string[] = []
  let stdoutSpy: any

  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    stdoutWrites = []
    stdoutSpy = vi.spyOn(process.stdout, 'write').mockImplementation((chunk: any) => {
      stdoutWrites.push(String(chunk))
      return true
    })
  })

  afterEach(() => {
    stdoutSpy?.mockRestore()
  })

  interface HarnessProps {
    onReady: (session: ReturnType<typeof useSessionLifecycle>) => void
    opts?: Partial<UseSessionLifecycleOptions>
    onHistoryCommit?: (items: Msg[]) => void
  }

  function Harness(props: HarnessProps) {
    const colsRef = useRef(80)
    const scrollRef = useRef(null)
    const [historyItems, setHistoryItemsState] = useState<Msg[]>([])

    const setHistoryItems = (updater: any) => {
      setHistoryItemsState((prev: Msg[]) => {
        const next = typeof updater === 'function' ? updater(prev) : updater
        props.onHistoryCommit?.(next)
        return next
      })
    }

    const [lastUserMsg, setLastUserMsg] = useState('')
    const [sessionStartedAt, setSessionStartedAt] = useState(0)
    const [stickyPrompt, setStickyPrompt] = useState('')
    const [voiceProcessing, setVoiceProcessing] = useState(false)
    const [voiceRecording, setVoiceRecording] = useState(false)

    const session = useSessionLifecycle({
      colsRef,
      composerActions: { setComposerTokens: () => {} },
      gw: props.opts?.gw ?? ({} as any),
      panel: () => {},
      rpc: props.opts?.rpc ?? (async () => null),
      scrollRef,
      setHistoryItems,
      setLastUserMsg,
      setSessionStartedAt,
      setStickyPrompt,
      setVoiceProcessing,
      setVoiceRecording,
      sys: () => {},
      ...props.opts
    })

    useEffect(() => {
      props.onReady(session)
    }, [session])

    return null
  }

  it('cold hydration failure does not commit empty history or emit replay END, but emits abort and retains incomplete marker', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    const historyCommits: Msg[][] = []

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    })

    const cancelEventBarrier = vi.fn()

    const mockInfo: SessionInfo = {
      id: 'session-fail-1',
      model: 'test-model',
      profile_name: 'default',
      skills: [],
      tools: [],
      version: '1.0.0'
    }

    const gw = {
      activateEventBarrier: vi.fn(),
      cancelEventBarrier,
      releaseEventBarrier: vi.fn(),
      request: vi.fn(async (method: string) => {
        if (method === 'session.resume') {
          return {
            info: mockInfo,
            messages: [], // backend sends empty messages when omit_messages=true
            resumed: 'session-fail-1',
            session_id: 'session-fail-1'
          }
        }
        if (method === 'session.history') {
          // Cold hydration failure!
          throw new Error('history transport error')
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onHistoryCommit: items => { historyCommits.push(items) },
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any, rpc }
    }))

    await lifecycle!.resumeById('session-fail-1')

    // Wait until cold hydration failure handler finishes
    await vi.waitFor(() => {
      expect(cancelEventBarrier).toHaveBeenCalledWith('session-fail-1', expect.any(String))
    })

    // 1. History commits: resetSession() clears to [], but .catch does NOT commit empty omitted r.messages or fallback intro
    // (Previously, .catch would call setHistoryItems([introMsg(mockInfo)]) which would add an intro commit on failure)
    const commitsAfterReset = historyCommits.filter(c => c.length > 0)
    expect(commitsAfterReset).toEqual([])

    // 2. Replay BEGIN was emitted
    expect(stdoutWrites.some(w => w.includes('\x1b]777;hermes-replay;begin;'))).toBe(true)

    // 3. Replay END was NOT emitted
    expect(stdoutWrites.some(w => w.includes('\x1b]777;hermes-replay;end;'))).toBe(false)

    // 4. Replay ABORT WAS emitted
    expect(stdoutWrites.some(w => w.includes('\x1b]777;hermes-replay;abort;'))).toBe(true)

    // 5. Incomplete marker retains the session id so reconnect recovers via cold hydration
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('session-fail-1')
  })

  it('subsequent transport-recovery request for an incomplete session upgrades to cold-resume', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    const historyCalls: any[] = []

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    })

    let failHistory = true

    const gw = {
      activateEventBarrier: vi.fn(),
      cancelEventBarrier: vi.fn(),
      releaseEventBarrier: vi.fn(),
      request: vi.fn(async (method: string, params?: any) => {
        if (method === 'session.resume') {
          return {
            messages: [],
            resumed: 'session-upgrade-1',
            session_id: 'session-upgrade-1'
          }
        }
        if (method === 'session.history') {
          historyCalls.push(params)
          if (failHistory) {
            throw new Error('cold history failed first time')
          }
          return {
            count: 1,
            has_more: false,
            messages: [{ role: 'user', row_id: 1, text: 'hydrated turn' }]
          }
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any, rpc }
    }))

    // First attempt fails cold hydration
    await lifecycle!.resumeById('session-upgrade-1')
    await vi.waitFor(() => {
      expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('session-upgrade-1')
    })
    expect(historyCalls.length).toBe(1)

    // Allow history to succeed on second attempt
    failHistory = false

    // Subsequent request comes in with mode: 'transport-recovery'
    // Because coldHydrationIncompleteRef matches 'session-upgrade-1', this must UPGRADE to cold-resume!
    await lifecycle!.resumeById('session-upgrade-1', undefined, 0, { mode: 'transport-recovery' })

    // If it had stayed transport-recovery, session.history would NOT have been called again!
    // Since it upgraded to cold-resume, session.history is called again.
    await vi.waitFor(() => {
      expect(historyCalls.length).toBe(2)
    })
  })

  it('incomplete marker clears only after commit acknowledgement', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    let latestHistory: Msg[] = []

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    })

    const releaseEventBarrier = vi.fn()

    const gw = {
      activateEventBarrier: vi.fn(),
      cancelEventBarrier: vi.fn(),
      releaseEventBarrier,
      request: vi.fn(async (method: string) => {
        if (method === 'session.resume') {
          return {
            messages: [],
            resumed: 'session-ack-1',
            session_id: 'session-ack-1'
          }
        }
        if (method === 'session.history') {
          return {
            count: 1,
            has_more: false,
            messages: [{ role: 'user', row_id: 1, text: 'ack hydrated message' }]
          }
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onHistoryCommit: items => { latestHistory = items },
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any, rpc }
    }))

    await lifecycle!.resumeById('session-ack-1')

    // Wait until commit acknowledgement has executed (useLayoutEffect releases event barrier)
    await vi.waitFor(() => {
      expect(releaseEventBarrier).toHaveBeenCalledWith('session-ack-1', expect.any(String))
    })

    // Commit acknowledgement cleared the incomplete marker
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBeNull()

    // History committed successfully
    expect(latestHistory.some(m => m.text === 'ack hydrated message')).toBe(true)

    // Replay END was emitted after commit
    await vi.waitFor(() => {
      expect(stdoutWrites.some(w => w.includes('\x1b]777;hermes-replay;end;'))).toBe(true)
    })
  })

  it('coldHistoryHydration throws ColdHydrationCancelledError during eviction loop when cancelled', async () => {
    const stdout = {
      write: vi.fn(() => true)
    }

    const messages = Array.from({ length: 10 }, (_, i) => ({
      role: 'user',
      row_id: i + 1,
      text: `Message ${i + 1}`
    }))

    const request = vi.fn().mockResolvedValue({
      count: 10,
      has_more: false,
      messages
    })

    // maxMounted: 2 forces deque eviction loop while deque.length > maxMounted
    // isCancelled returns true on eviction check
    const isCancelled = () => true

    await expect(performColdHistoryHydration({
      cols: 80,
      gateway: { request },
      isCancelled,
      maxMounted: 2,
      sessionId: 'sess-cancel',
      stdout: stdout as any,
      theme: { color: {} }
    })).rejects.toThrow(ColdHydrationCancelledError)
  })
})
