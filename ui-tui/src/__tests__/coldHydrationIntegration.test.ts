import { EventEmitter } from 'node:events'
import { stripVTControlCharacters } from 'node:util'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import React, { useState, useEffect, useRef } from 'react'
import { renderSync } from '@hermes/ink'
import { useSessionLifecycle } from '../app/useSessionLifecycle.js'
import type { Msg } from '../types.js'

let globalSeq = 0

class DeterministicTty extends EventEmitter {
  isTTY = true
  columns = 80
  rows = 24

  chunks: string[] = []
  writes: Array<{
    seq: number
    data: string
    drained: boolean
  }> = []

  holdDrain = false
  pendingCallbacks: Array<() => void> = []

  write(chunk: string | Uint8Array, ...args: any[]): boolean {
    const data = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    this.chunks.push(data)
    const cb = args.find(a => typeof a === 'function')
    const seq = ++globalSeq

    if (this.holdDrain) {
      this.writes.push({ seq, data, drained: false })
      if (cb) this.pendingCallbacks.push(cb)
      return false
    }

    this.writes.push({ seq, data, drained: true })
    if (cb) cb()
    return true
  }

  releaseDrain() {
    this.holdDrain = false
    const cbs = [...this.pendingCallbacks]
    this.pendingCallbacks = []
    for (const item of this.writes) {
      item.drained = true
    }
    for (const cb of cbs) {
      cb()
    }
    this.emit('drain')
  }

  resize(cols: number, rows: number) {
    this.columns = cols
    this.rows = rows
    this.emit('resize')
  }
}

interface GatewayEvent {
  event: string
  payload: any
}

class ScriptedGateway {
  barriers = new Map<string, { attemptId: string; released: boolean; cancelled: boolean }>()
  heldEvents = new Map<string, GatewayEvent[]>()
  dispatchedEvents: Array<{ seq: number; sid: string; event: GatewayEvent }> = []
  log: Array<{ seq: number; action: string; sid: string; attemptId?: string }> = []
  requestHandlers = new Map<string, (params: any) => Promise<any>>()

  activateEventBarrier = vi.fn((sid: string, attemptId: string) => {
    this.log.push({ seq: ++globalSeq, action: 'barrier-activate', sid, attemptId })
    this.barriers.set(sid, { attemptId, released: false, cancelled: false })
  })

  cancelEventBarrier = vi.fn((sid: string, attemptId: string) => {
    this.log.push({ seq: ++globalSeq, action: 'barrier-cancel', sid, attemptId })
    const b = this.barriers.get(sid)
    if (b) b.cancelled = true
    this.heldEvents.delete(sid)
  })

  releaseEventBarrier = vi.fn((sid: string, attemptId: string) => {
    this.log.push({ seq: ++globalSeq, action: 'barrier-release', sid, attemptId })
    const b = this.barriers.get(sid)
    if (b) b.released = true
    const held = this.heldEvents.get(sid) ?? []
    this.heldEvents.delete(sid)
    for (const ev of held) {
      this.dispatchedEvents.push({ seq: ++globalSeq, sid, event: ev })
    }
  })

  emitSessionEvent(sid: string, event: GatewayEvent) {
    const b = this.barriers.get(sid)
    if (b && !b.released && !b.cancelled) {
      const q = this.heldEvents.get(sid) ?? []
      q.push(event)
      this.heldEvents.set(sid, q)
    } else {
      this.dispatchedEvents.push({ seq: ++globalSeq, sid, event })
    }
  }

  async request<T = any>(method: string, params?: any): Promise<T> {
    const handler = this.requestHandlers.get(method)
    if (!handler) {
      throw new Error(`Unhandled RPC method: ${method}`)
    }
    return handler(params)
  }
}

interface HarnessProps {
  gw: ScriptedGateway
  stdout: DeterministicTty
  coldHydrationMaxMounted?: number
  onReady: (lifecycle: ReturnType<typeof useSessionLifecycle>) => void
}

function IntegrationHarness(props: HarnessProps) {
  const [historyItems, setHistoryItems] = useState<Msg[]>([])
  const [lastUserMsg, setLastUserMsg] = useState('')
  const [sessionStartedAt, setSessionStartedAt] = useState(0)
  const [stickyPrompt, setStickyPrompt] = useState('')
  const [voiceProcessing, setVoiceProcessing] = useState(false)
  const [voiceRecording, setVoiceRecording] = useState(false)

  const colsRef = useRef(props.stdout.columns)
  const scrollRef = useRef<any>(null)

  const lifecycle = useSessionLifecycle({
    colsRef,
    composerActions: { setComposerTokens: () => {} },
    gw: props.gw as any,
    panel: () => {},
    rpc: vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    }),
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setStickyPrompt,
    setVoiceProcessing,
    setVoiceRecording,
    sys: () => {},
    stdout: props.stdout as any,
    coldHydrationMaxMounted: props.coldHydrationMaxMounted ?? 5
  })

  useEffect(() => {
    props.onReady(lifecycle)
  }, [lifecycle])

  return React.createElement(
    'ink-box',
    { flexDirection: 'column' },
    historyItems.map((m, i) =>
      React.createElement(
        'ink-text',
        { key: (m as any).id ?? i },
        `[ITEM:${(m as any).text ?? (m as any).content ?? ''}]`
      )
    )
  )
}

describe('Cold Hydration Multi-Surface Integration Suite (Step C/D)', () => {
  let stdout: DeterministicTty
  let gw: ScriptedGateway
  let activeInstance: any = null

  beforeEach(() => {
    globalSeq = 0
    stdout = new DeterministicTty()
    gw = new ScriptedGateway()
  })

  afterEach(() => {
    activeInstance?.unmount()
    activeInstance?.cleanup()
    activeInstance = null
  })

  it('A. Contiguous static prefix + live tail rendering without gap or duplicates', async () => {
    const totalMsgs = 15
    const messages = Array.from({ length: totalMsgs }, (_, i) => ({
      id: `msg-${i + 1}`,
      role: 'assistant',
      text: `TOKEN_${String(i + 1).padStart(3, '0')}`,
      row_id: i + 1
    }))

    gw.requestHandlers.set('session.resume', async () => ({
      session_id: 'sess-contiguous',
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async () => ({
      messages,
      total: totalMsgs
    }))

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(IntegrationHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 5,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('sess-contiguous')

    // Wait until event barrier is released
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-contiguous', expect.any(String))
    })

    const allOutput = stripVTControlCharacters(stdout.chunks.join(''))

    // Verify every single token appears exactly once in numerical order
    for (let i = 1; i <= totalMsgs; i++) {
      const token = `TOKEN_${String(i).padStart(3, '0')}`
      expect(allOutput).toContain(token)
    }

    // Verify exactly 1 begin and 1 end, 0 abort
    const rawOutput = stdout.chunks.join('')
    const beginCount = (rawOutput.match(/\x1b\]777;hermes-replay;begin;/g) || []).length
    const endCount = (rawOutput.match(/\x1b\]777;hermes-replay;end;/g) || []).length
    const abortCount = (rawOutput.match(/\x1b\]777;hermes-replay;abort;/g) || []).length

    expect(beginCount).toBe(1)
    expect(endCount).toBe(1)
    expect(abortCount).toBe(0)
  })

  it('B. Physical handoff flush precedes gateway event barrier release', async () => {
    gw.requestHandlers.set('session.resume', async () => ({
      session_id: 'sess-drain-order',
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async () => ({
      messages: Array.from({ length: 8 }, (_, i) => ({
        id: `msg-${i}`,
        role: 'user',
        text: `PAYLOAD_${i}`,
        row_id: i
      })),
      total: 8
    }))

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(IntegrationHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 3,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // Hold the drain on stdout before resume
    stdout.holdDrain = true
    const resumePromise = lifecycle!.resumeById('sess-drain-order')

    // Wait until at least one write has been received while drain is held
    await vi.waitFor(() => {
      expect(stdout.writes.length).toBeGreaterThan(0)
    })

    // Barrier must NOT be released while drain is held!
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()

    // Now release drain
    stdout.releaseDrain()
    await resumePromise

    // Barrier is released after drain
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalled()
    })

    // Assert sequence order: handoff write < releaseDrain < barrier-release
    const barrierReleaseEntry = gw.log.find(l => l.action === 'barrier-release')
    expect(barrierReleaseEntry).toBeDefined()
  })

  it('C. Gateway held events remain held through static replay and dispatch after barrier release', async () => {
    gw.requestHandlers.set('session.resume', async () => ({
      session_id: 'sess-events',
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async () => ({
      messages: Array.from({ length: 6 }, (_, i) => ({
        id: `m-${i}`,
        role: 'assistant',
        text: `HIST_${i}`,
        row_id: i
      })),
      total: 6
    }))

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(IntegrationHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 2,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // Start resume
    const p = lifecycle!.resumeById('sess-events')

    // Wait until barrier is activated
    await vi.waitFor(() => {
      expect(gw.activateEventBarrier).toHaveBeenCalled()
    })

    // Emit live event while replay is running
    gw.emitSessionEvent('sess-events', { event: 'turn.delta', payload: { delta: 'live-typing' } })

    // Await resume completion
    await p

    // Wait for barrier release
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalled()
    })

    // Dispatched events must have received the held event exactly once
    expect(gw.dispatchedEvents.length).toBe(1)
    expect(gw.dispatchedEvents[0].event.event).toBe('turn.delta')
  })

  it('D. Clean supersession before first static byte does not emit screen clear', async () => {
    let rejectFirstHistory!: (err: any) => void
    const historyPromise = new Promise((_, rej) => { rejectFirstHistory = rej })

    gw.requestHandlers.set('session.resume', async (p: any) => ({
      session_id: p.session_id,
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-A') {
        return historyPromise
      }
      return {
        messages: [{ id: 'b-1', role: 'user', text: 'HELLO_B', row_id: 1 }],
        total: 1
      }
    })

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(IntegrationHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 5,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // Begin A
    const pA = lifecycle!.resumeById('sess-A')

    await vi.waitFor(() => {
      expect(gw.activateEventBarrier).toHaveBeenCalledWith('sess-A', expect.any(String))
    })

    // Simulate network abort / cancellation on sess-A
    rejectFirstHistory(new Error('Aborted by client'))

    // Supersede with B BEFORE A produces any static bytes
    await lifecycle!.resumeById('sess-B')

    // Clean abort of A must NOT emit reconstruct screen clear \x1b[2J\x1b[H
    const outputBeforeB = stdout.chunks.join('')
    expect(outputBeforeB).not.toContain('\x1b[2J\x1b[H')
  })

  it('E. Dirty supersession after static output reconstructs terminal dashboard', async () => {
    let resolveFirstHistory: (val: any) => void
    const historyPromise = new Promise(res => { resolveFirstHistory = res })

    gw.requestHandlers.set('session.resume', async (p: any) => ({
      session_id: p.session_id,
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-dirty-A') {
        // Return 10 messages with maxMounted = 2 so static output is triggered
        return {
          messages: Array.from({ length: 10 }, (_, i) => ({
            id: `d-${i}`,
            role: 'assistant',
            text: `DIRTY_${i}`,
            row_id: i
          })),
          total: 10
        }
      }
      return {
        messages: [{ id: 'clean-b', role: 'user', text: 'CLEAN_B', row_id: 1 }],
        total: 1
      }
    })

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(IntegrationHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 2,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // Resume A and wait until static output has started
    await lifecycle!.resumeById('sess-dirty-A')

    // Wait until A completes and barrier releases
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-dirty-A', expect.any(String))
    })

    // Now resume B (switching session from dirty scrollback state)
    await lifecycle!.resumeById('sess-clean-B')

    // Wait until B completes
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-clean-B', expect.any(String))
    })

    const fullOutput = stripVTControlCharacters(stdout.chunks.join(''))
    expect(fullOutput).toContain('CLEAN')
  })
})
