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
  blockDrainPattern: RegExp | null = null
  pendingCallbacks: Array<{ seq: number; cb: () => void }> = []

  write(chunk: string | Uint8Array, ...args: any[]): boolean {
    const data = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    this.chunks.push(data)
    this.emit('write-chunk', data)
    const cb = args.find(a => typeof a === 'function')
    const seq = ++globalSeq

    const hasUnfinishedDrain = this.pendingCallbacks.length > 0
    const shouldHold = this.holdDrain || hasUnfinishedDrain || (this.blockDrainPattern !== null && this.blockDrainPattern.test(data))

    if (shouldHold) {
      this.writes.push({ seq, data, drained: false })
      if (cb) this.pendingCallbacks.push({ seq, cb })
      this.emit('drain-held', { seq, data })
      return false
    }

    this.writes.push({ seq, data, drained: true })
    if (cb) cb()
    return true
  }

  releaseDrain() {
    this.holdDrain = false
    this.blockDrainPattern = null
    const pending = [...this.pendingCallbacks]
    this.pendingCallbacks = []
    const seq = ++globalSeq
    for (const item of this.writes) {
      item.drained = true
    }
    for (const p of pending) {
      p.cb()
    }
    this.emit('drain-released', { seq })
    this.emit('drain')
    return seq
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
    const seq = ++globalSeq
    this.log.push({ seq, action: 'barrier-release', sid, attemptId })
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

describe('Cold Hydration Multi-Surface Integration Suite (Step C/D Hardened)', () => {
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

  it('A. Contiguous static prefix + live tail rendering without gap, duplicates, or reordering', async () => {
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

    // Build the matched token sequence from output and assert exact array equality
    const matchedTokens = Array.from(allOutput.matchAll(/TOKEN_\d{3}/g), m => m[0])
    const expectedTokens = Array.from({ length: totalMsgs }, (_, i) => `TOKEN_${String(i + 1).padStart(3, '0')}`)
    expect(matchedTokens).toEqual(expectedTokens)

    // Verify exactly 1 begin and 1 end, 0 abort
    await vi.waitFor(() => {
      const rawOutput = stdout.chunks.join('')
      const endCount = (rawOutput.match(/\x1b\]777;hermes-replay;end;/g) || []).length
      expect(endCount).toBe(1)
    })

    const finalRawOutput = stdout.chunks.join('')
    const beginCount = (finalRawOutput.match(/\x1b\]777;hermes-replay;begin;/g) || []).length
    const abortCount = (finalRawOutput.match(/\x1b\]777;hermes-replay;abort;/g) || []).length

    expect(beginCount).toBe(1)
    expect(abortCount).toBe(0)
  })

  it('B. Physical handoff flush precedes gateway event barrier release and held-event dispatch', async () => {
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

    // Block physical drain strictly on the append handoff frame
    stdout.blockDrainPattern = /PAYLOAD_7/

    let handoffWriteSeq = 0
    stdout.on('drain-held', e => {
      if (stdout.blockDrainPattern?.test(e.data)) {
        handoffWriteSeq = e.seq
      }
    })

    const resumePromise = lifecycle!.resumeById('sess-drain-order')

    // Wait until the handoff write is received and held
    await vi.waitFor(() => {
      expect(handoffWriteSeq).toBeGreaterThan(0)
    })

    // Queue a live gateway event while drain is held
    gw.emitSessionEvent('sess-drain-order', { event: 'turn.delta', payload: { delta: 'held-typing' } })

    // Barrier must NOT be released while drain is held!
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()
    expect(gw.dispatchedEvents.length).toBe(0)

    // Release drain and capture drain completion sequence
    const drainReleaseSeq = stdout.releaseDrain()
    await resumePromise

    // Barrier is released after drain
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalled()
      expect(gw.dispatchedEvents.length).toBe(1)
    })

    const barrierReleaseEntry = gw.log.find(l => l.action === 'barrier-release')
    expect(barrierReleaseEntry).toBeDefined()
    const barrierReleaseSeq = barrierReleaseEntry!.seq
    const eventDispatchSeq = gw.dispatchedEvents[0].seq

    // Strict invariant: handoff write < drain completion <= barrier release < held event dispatch
    expect(handoffWriteSeq).toBeLessThan(drainReleaseSeq)
    expect(drainReleaseSeq).toBeLessThanOrEqual(barrierReleaseSeq)
    expect(barrierReleaseSeq).toBeLessThan(eventDispatchSeq)
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

    // Hold handoff drain
    stdout.blockDrainPattern = /HIST_5/

    let emittedMidReplay = false
    stdout.on('write-chunk', chunk => {
      // Once static bytes for HIST_0 or HIST_1 have been physically written, emit live event
      if (!emittedMidReplay && chunk.includes('HIST_1')) {
        emittedMidReplay = true
        gw.emitSessionEvent('sess-events', { event: 'turn.delta', payload: { delta: 'live-mid-replay' } })
      }
    })

    const p = lifecycle!.resumeById('sess-events')

    // Wait until event is emitted mid-replay and handoff is held
    await vi.waitFor(() => {
      expect(emittedMidReplay).toBe(true)
      expect(stdout.pendingCallbacks.length).toBeGreaterThan(0)
    })

    // Dispatched events must be strictly 0 while drain is held!
    expect(gw.dispatchedEvents.length).toBe(0)
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()

    // Release drain
    stdout.releaseDrain()
    await p

    // Wait for barrier release
    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalled()
    })

    // Dispatched events must now have received the held event exactly once
    expect(gw.dispatchedEvents.length).toBe(1)
    expect(gw.dispatchedEvents[0].event.event).toBe('turn.delta')
    expect(gw.dispatchedEvents[0].event.payload.delta).toBe('live-mid-replay')
  })

  it('D. Clean supersession before first static byte does not emit screen clear', async () => {
    let resolveFirstHistory!: (val: any) => void
    let rejectFirstHistory!: (err: any) => void
    const historyPromiseA = new Promise((res, rej) => {
      resolveFirstHistory = res
      rejectFirstHistory = rej
    })

    gw.requestHandlers.set('session.resume', async (p: any) => ({
      session_id: p.session_id,
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-A') {
        return historyPromiseA
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

    // Begin A (history remains pending)
    const pA = lifecycle!.resumeById('sess-A')

    await vi.waitFor(() => {
      expect(gw.activateEventBarrier).toHaveBeenCalledWith('sess-A', expect.any(String))
    })

    // Supersede with B while A\'s history is genuinely in-flight
    const pB = lifecycle!.resumeById('sess-B')

    // Verify A\'s barrier was cancelled by supersession
    await vi.waitFor(() => {
      expect(gw.cancelEventBarrier).toHaveBeenCalledWith('sess-A', expect.any(String))
    })

    // Now resolve/reject A\'s pending history
    rejectFirstHistory(new Error('Hydration cancelled'))
    await pB

    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-B', expect.any(String))
    })

    // Clean abort of A before static bytes must NOT emit reconstruct screen clear \x1b[2J\x1b[H
    const allOutput = stdout.chunks.join('')
    expect(allOutput).not.toContain('\x1b[2J\x1b[H')
    expect(stripVTControlCharacters(allOutput)).toContain('HELLO_B')
  })

  it('E. Dirty supersession after static output reconstructs terminal dashboard', async () => {
    let unblockHistoryA!: () => void
    const historyGateA = new Promise<void>(res => { unblockHistoryA = res })

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

    // Hold handoff drain for A so A stays in dirty leased state
    stdout.blockDrainPattern = /DIRTY_9/

    let staticBytesSeen = false
    stdout.on('write-chunk', chunk => {
      if (chunk.includes('DIRTY_1')) {
        staticBytesSeen = true
      }
    })

    // Resume A
    const pA = lifecycle!.resumeById('sess-dirty-A')

    // Wait until static bytes have physically materialized on stdout
    await vi.waitFor(() => {
      expect(staticBytesSeen).toBe(true)
    })

    // Supersede with B while A is dirty and holding lease
    const pB = lifecycle!.resumeById('sess-clean-B')

    stdout.releaseDrain()
    await pB

    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-clean-B', expect.any(String))
    })

    // Assert that dirty supersession emitted \x1b[2J\x1b[H (terminal clear) and rendered B
    const fullRawOutput = stdout.chunks.join('')
    expect(fullRawOutput).toContain('\x1b[2J\x1b[H')
    const strippedOutput = stripVTControlCharacters(fullRawOutput)
    console.log('REPR:', JSON.stringify(strippedOutput)); expect(strippedOutput).toContain('CLEAN')
  })

  it('F. Pending-commit ownership race: supersession claims queued transaction without deadlock', async () => {
    gw.requestHandlers.set('session.resume', async (p: any) => ({
      session_id: p.session_id,
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-pending-A') {
        return {
          messages: Array.from({ length: 6 }, (_, i) => ({
            id: `p-${i}`,
            role: 'assistant',
            text: `PENDING_${i}`,
            row_id: i
          })),
          total: 6
        }
      }
      return {
        messages: [{ id: 'succ-b', role: 'user', text: 'SUCCESSOR_B', row_id: 1 }],
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

    // Hold drain when PENDING_5 arrives (the handoff frame queued for commit)
    stdout.blockDrainPattern = /PENDING_5/

    let handoffQueued = false
    stdout.on('write-chunk', chunk => {
      if (chunk.includes('PENDING_5')) {
        handoffQueued = true
      }
    })

    const pA = lifecycle!.resumeById('sess-pending-A')

    await vi.waitFor(() => {
      expect(handoffQueued).toBe(true)
    })

    // Trigger supersession precisely in the pending-commit window
    const pB = lifecycle!.resumeById('sess-successor-B')

    stdout.releaseDrain()

    // Must resolve cleanly without deadlock!
    await pB

    await vi.waitFor(() => {
      expect(gw.releaseEventBarrier).toHaveBeenCalledWith('sess-successor-B', expect.any(String))
    })

    const fullRawOutput = stdout.chunks.join('')
    expect(fullRawOutput).toContain('\x1b[2J\x1b[H')
    const strippedOutput = stripVTControlCharacters(fullRawOutput)
    expect(strippedOutput).toContain('SUCCESSOR_B')
  })
})
