import { EventEmitter } from 'node:events'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import React, { useState, useEffect, useRef } from 'react'
import { renderSync } from '@hermes/ink'
import * as InkModule from '@hermes/ink'
import { JsonRpcGatewayError } from '@hermes/shared/json-rpc-channel'
import { getUiState } from '../app/uiStore.js'
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
      this.pendingCallbacks.push({ seq, cb: cb ?? (() => {}) })
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
}

class ScriptedGateway {
  barriers = new Map<string, { attemptId: string; released: boolean; cancelled: boolean }>()
  requestHandlers = new Map<string, (params: any) => Promise<any>>()

  hasEventBarrier(sid: string, owner: string) {
    const barrier = this.barriers.get(sid)
    return Boolean(barrier && barrier.attemptId === owner && !barrier.cancelled && !barrier.released)
  }

  activateEventBarrier = vi.fn((sid: string, attemptId: string) => {
    this.barriers.set(sid, { attemptId, released: false, cancelled: false })
  })

  cancelEventBarrier = vi.fn((sid: string, attemptId: string) => {
    const b = this.barriers.get(sid)
    if (b?.attemptId === attemptId) b.cancelled = true
  })

  releaseEventBarrier = vi.fn((sid: string, attemptId: string) => {
    const b = this.barriers.get(sid)
    if (b?.attemptId === attemptId) b.released = true
  })

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

function LifecycleHarness(props: HarnessProps) {
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

describe('Bounded Lifecycle Correctness (Unit 1)', () => {
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
    vi.restoreAllMocks()
  })

  it('1. Acquisition failure cancels barrier, emits ABORT without BEGIN/END, and retains incomplete marker', async () => {
    gw.requestHandlers.set('session.resume', async () => ({
      session_id: 'sess-acq-fail',
      status: 'idle',
      messages: []
    }))

    const acquireSpy = vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockRejectedValueOnce(
      new Error('Terminal lease acquisition failure')
    )

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(LifecycleHarness, {
        gw,
        stdout,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    await lifecycle!.resumeById('sess-acq-fail')

    // Lease acquisition attempted
    expect(acquireSpy).toHaveBeenCalledTimes(1)

    // Barrier must be cancelled
    expect(gw.activateEventBarrier).toHaveBeenCalledWith('sess-acq-fail', expect.any(String))
    expect(gw.cancelEventBarrier).toHaveBeenCalledWith('sess-acq-fail', expect.any(String))
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()

    // Output must contain exactly ABORT, no BEGIN, no END
    const output = stdout.chunks.join('')
    const beginCount = (output.match(/\x1b\]777;hermes-replay;begin;/g) || []).length
    const endCount = (output.match(/\x1b\]777;hermes-replay;end;/g) || []).length
    const abortCount = (output.match(/\x1b\]777;hermes-replay;abort;/g) || []).length

    expect(beginCount).toBe(0)
    expect(endCount).toBe(0)
    expect(abortCount).toBe(1)

    // Cold incomplete marker is retained
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('sess-acq-fail')
  })

  it('2. Reconstruction failure blocks successor session and subsequent successor RPCs', async () => {
    const gwRequestSpy = vi.spyOn(gw, 'request')

    gw.requestHandlers.set('session.resume', async (params: any) => ({
      session_id: params.session_id,
      status: 'idle',
      messages: []
    }))

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-fail-a') {
        return {
          messages: Array.from({ length: 6 }, (_, i) => ({
            id: `msg-${i}`,
            role: 'user',
            text: `BLOCK_PAYLOAD_${i}`,
            row_id: i
          })),
          total: 6
        }
      }
      return { messages: [], total: 0 }
    })

    let reconstructSpy: any = null
    let abortSpy: any = null
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementation(async (out: any) => {
      const lease = await realAcquire(out)
      reconstructSpy = vi.spyOn(lease, 'reconstructAndRelease').mockRejectedValue(
        new Error('Forced reconstruction failure in test')
      )
      abortSpy = vi.spyOn(lease, 'abort')
      return lease
    })

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(LifecycleHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 2, // forces static output
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // Hold drain on BLOCK_PAYLOAD_1 so session A remains active and dirty in static output
    stdout.blockDrainPattern = /BLOCK_PAYLOAD_1/

    let staticSeen = false
    stdout.on('write-chunk', chunk => {
      if (chunk.includes('BLOCK_PAYLOAD_0')) {
        staticSeen = true
      }
    })

    const pA = lifecycle!.resumeById('sess-fail-a')
    await vi.waitFor(() => expect(staticSeen).toBe(true))

    // Superseding session B must fail because reconstructAndRelease throws
    const pB = lifecycle!.resumeById('sess-fail-b')
    stdout.releaseDrain()

    let bError: any = null
    try {
      await pB
    } catch (err) {
      bError = err
    }

    expect(bError).not.toBeNull()
    expect(String(bError)).toContain('Forced reconstruction failure in test')
    expect(reconstructSpy).toHaveBeenCalled()
    expect(abortSpy).not.toHaveBeenCalled()

    // Subsequent successor C must ALSO be blocked before making any RPCs
    let cError: any = null
    try {
      await lifecycle!.resumeById('sess-fail-c')
    } catch (err) {
      cError = err
    }
    expect(cError).not.toBeNull()
    expect(String(cError)).toContain('Forced reconstruction failure in test')

    // Verify that neither successor session B nor C was permitted to issue session.resume RPCs
    expect(gwRequestSpy).not.toHaveBeenCalledWith(
      'session.resume',
      expect.objectContaining({ session_id: 'sess-fail-b' })
    )
    expect(gwRequestSpy).not.toHaveBeenCalledWith(
      'session.resume',
      expect.objectContaining({ session_id: 'sess-fail-c' })
    )

    stdout.releaseDrain()
    await pA.catch(() => {})
  })

  it('3. Replay ABORT strictly follows CSI 3J reconstruction frame and independent drain', async () => {
    gw.requestHandlers.set('session.resume', async (params: any) => ({
      session_id: params.session_id,
      status: 'idle',
      messages: []
    }))

    let historyCallCount = 0
    let resolveSecondHistory: any = null
    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-drain-a') {
        historyCallCount++
        if (historyCallCount === 1) {
          // Page 1 returns 4 items: with maxMounted: 2, 2 items are written to static output and drained
          return {
            messages: Array.from({ length: 4 }, (_, i) => ({
              id: `msg-${i}`,
              role: 'user',
              text: `DRAIN_TOKEN_${i}`,
              row_id: i
            })),
            total: 8,
            has_more: true,
            next_cursor: 4
          }
        }
        // Page 2 stays pending to keep session A mid-hydration without completing into append handoff
        return new Promise(res => {
          resolveSecondHistory = res
        })
      }
      return { messages: [], total: 0 }
    })

    let reconstructSpy: any = null
    let abortSpy: any = null
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementation(async (out: any) => {
      const lease = await realAcquire(out)
      reconstructSpy = vi.spyOn(lease, 'reconstructAndRelease')
      abortSpy = vi.spyOn(lease, 'abort')
      return lease
    })

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(
      React.createElement(LifecycleHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 2,
        onReady: l => { lifecycle = l }
      }),
      { stdout: stdout as any }
    )

    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    const pA = lifecycle!.resumeById('sess-drain-a')

    // Wait until static output is written and drained (DRAIN_TOKEN_1 seen)
    await vi.waitFor(() => {
      expect(stdout.chunks.join('')).toContain('DRAIN_TOKEN_1')
      expect(stdout.pendingCallbacks.length).toBe(0)
    })

    // Now session A has staticOutputStarted === true, is awaiting page 2, and is NOT in append handoff.
    // Configure controlled drain on stdout specifically matching CSI 3J reconstruction clear sequence (\x1b[3J)
    stdout.blockDrainPattern = /\x1b\[3J/

    // Initiate supersession
    const pB = lifecycle!.resumeById('sess-drain-b')

    // Resolve page 2 so cold history hydration loop checks isCancelled and triggers cancellation cleanup
    await vi.waitFor(() => expect(resolveSecondHistory).not.toBeNull())
    resolveSecondHistory({ messages: [], total: 4 })

    // Wait until the CSI 3J reconstruction frame write is emitted and held in pending callbacks
    await vi.waitFor(() => {
      expect(stdout.pendingCallbacks.length).toBeGreaterThan(0)
    })

    // Prove that the held write contains the actual CSI 3J escape sequence (\x1b[3J)
    const heldWrites = stdout.writes.filter(w => !w.drained)
    expect(heldWrites.some(w => w.data.includes('\x1b[3J'))).toBe(true)

    // Prove that replay ABORT was NOT emitted prior to reconstruction drain
    const outputBeforeDrain = stdout.chunks.join('')
    expect(outputBeforeDrain).not.toContain('\x1b]777;hermes-replay;abort;')

    // Prove that superseding session B has NOT completed yet
    let pBSettled = false
    pB.then(() => { pBSettled = true }, () => { pBSettled = true })
    expect(pBSettled).toBe(false)
    expect(reconstructSpy).toHaveBeenCalled()

    // Now independently release the held drain on the reconstruction frame
    stdout.releaseDrain()

    // Await completion of superseding session B
    await pB
    expect(pBSettled).toBe(true)

    // Prove that replay ABORT was emitted strictly after CSI 3J reconstruction frame drain
    const outputAfterDrain = stdout.chunks.join('')
    expect(outputAfterDrain).toContain('\x1b]777;hermes-replay;abort;')

    const csiIndex = outputAfterDrain.indexOf('\x1b[3J')
    const abortIndex = outputAfterDrain.indexOf('\x1b]777;hermes-replay;abort;')
    expect(csiIndex).toBeGreaterThan(-1)
    expect(abortIndex).toBeGreaterThan(csiIndex)

    expect(abortSpy).not.toHaveBeenCalled()
    await pA.catch(() => {})
  })

  it('4. Clean and dirty component unmount while Ink instance alive proves physical cleanup before settlement and ABORT', async () => {
    gw.requestHandlers.set('session.resume', async (params: any) => ({
      session_id: params.session_id,
      status: 'idle',
      messages: []
    }))

    let cleanHistoryResolve: any = null
    let dirtyHistoryPage1Delivered = false
    let resolveDirtyPage2: any = null

    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id === 'sess-clean-unmount') {
        return new Promise(res => { cleanHistoryResolve = res })
      }
      if (p.session_id === 'sess-dirty-unmount') {
        if (!dirtyHistoryPage1Delivered) {
          dirtyHistoryPage1Delivered = true
          return {
            messages: Array.from({ length: 4 }, (_, i) => ({
              id: `dirty-${i}`,
              role: 'user',
              text: `DIRTY_MSG_${i}`,
              row_id: i
            })),
            total: 8,
            has_more: true,
            next_cursor: 4
          }
        }
        return new Promise(res => { resolveDirtyPage2 = res })
      }
      return { messages: [], total: 0 }
    })

    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    let unmountComponent: (() => void) | null = null
    let mountComponent: (() => void) | null = null

    function ParentHarness() {
      const [mounted, setMounted] = useState(true)
      unmountComponent = () => setMounted(false)
      mountComponent = () => setMounted(true)

      if (!mounted) {
        return React.createElement('ink-text', null, 'unmounted')
      }

      return React.createElement(LifecycleHarness, {
        gw,
        stdout,
        coldHydrationMaxMounted: 2,
        onReady: l => { lifecycle = l }
      })
    }

    activeInstance = renderSync(React.createElement(ParentHarness), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    // ==========================================
    // Part A: CLEAN component unmount
    // Static output has NOT started -> lease.abort() must be awaited before settlement/ABORT
    // ==========================================
    let cleanLeaseAbortSpy: any = null
    let cleanLeaseReconstructSpy: any = null
    let cleanAbortResolved = false

    const realAcquire = InkModule.acquireMainScreenStaticOutput
    const acquireSpy = vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementation(async (out: any) => {
      const lease = await realAcquire(out)
      const origAbort = lease.abort.bind(lease)
      cleanLeaseAbortSpy = vi.spyOn(lease, 'abort').mockImplementation(async () => {
        const res = await origAbort()
        cleanAbortResolved = true
        return res
      })
      cleanLeaseReconstructSpy = vi.spyOn(lease, 'reconstructAndRelease')
      return lease
    })

    const cleanResumePromise = lifecycle!.resumeById('sess-clean-unmount')

    await vi.waitFor(() => {
      expect(gw.activateEventBarrier).toHaveBeenCalledWith('sess-clean-unmount', expect.any(String))
      expect(cleanLeaseAbortSpy).not.toBeNull()
      expect(cleanHistoryResolve).not.toBeNull()
    })

    // Component unmounts while history RPC is in flight (clean state)
    unmountComponent!()

    // Resolve history now so performColdHistoryHydration notices cancellation
    cleanHistoryResolve({ messages: [], total: 0 })

    // Physical cleanup was executed
    await vi.waitFor(() => {
      expect(cleanLeaseAbortSpy).toHaveBeenCalledTimes(1)
    })
    expect(cleanAbortResolved).toBe(true)
    expect(cleanLeaseReconstructSpy).not.toHaveBeenCalled()
    expect(gw.cancelEventBarrier).toHaveBeenCalledWith('sess-clean-unmount', expect.any(String))

    await cleanResumePromise.catch(() => {})

    // Replay ABORT was emitted
    expect(stdout.chunks.join('')).toContain('\x1b]777;hermes-replay;abort;')

    // Ink instance remained alive throughout
    expect(activeInstance).not.toBeNull()

    // ==========================================
    // Part B: DIRTY component unmount
    // Static output HAS started -> lease.reconstructAndRelease() must render CSI 3J and drain before settlement/ABORT
    // ==========================================
    lifecycle = null
    mountComponent!()
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())

    let dirtyLeaseAbortSpy: any = null
    let dirtyLeaseReconstructSpy: any = null

    acquireSpy.mockImplementation(async (out: any) => {
      const lease = await realAcquire(out)
      dirtyLeaseAbortSpy = vi.spyOn(lease, 'abort')
      dirtyLeaseReconstructSpy = vi.spyOn(lease, 'reconstructAndRelease')
      return lease
    })

    const dirtyResumePromise = lifecycle!.resumeById('sess-dirty-unmount')

    // Wait until static output is written and drained
    await vi.waitFor(() => {
      expect(stdout.chunks.join('')).toContain('DIRTY_MSG_1')
      expect(stdout.pendingCallbacks.length).toBe(0)
    })

    // Configure controlled drain on stdout matching CSI 3J reconstruction frame
    stdout.blockDrainPattern = /\x1b\[3J/

    // Component unmounts while in dirty leased state
    unmountComponent!()

    // Wait until unmount cleanup fires supersedeColdHydration and cancels the event barrier
    await vi.waitFor(() => {
      expect(gw.cancelEventBarrier).toHaveBeenCalledWith('sess-dirty-unmount', expect.any(String))
    })

    // Release page 2 so cold hydration cancellation triggers
    await vi.waitFor(() => expect(resolveDirtyPage2).not.toBeNull())
    resolveDirtyPage2({ messages: [], total: 4 })

    // Wait for reconstruction frame to be written and held
    await vi.waitFor(() => {
      expect(stdout.pendingCallbacks.length).toBeGreaterThan(0)
    })

    // Prove the held write contains CSI 3J
    const heldDirtyWrites = stdout.writes.filter(w => !w.drained)
    expect(heldDirtyWrites.some(w => w.data.includes('\x1b[3J'))).toBe(true)

    // Prove reconstructAndRelease was called, NOT abort
    expect(dirtyLeaseReconstructSpy).toHaveBeenCalled()
    expect(dirtyLeaseAbortSpy).not.toHaveBeenCalled()

    // Barrier was cancelled immediately on cancellation
    expect(gw.cancelEventBarrier).toHaveBeenCalledWith('sess-dirty-unmount', expect.any(String))

    // Replay ABORT for dirty session must NOT be emitted before CSI 3J frame drains
    const chunksBeforeDrain = stdout.chunks.join('')
    const abortOccurrencesBefore = (chunksBeforeDrain.match(/\x1b\]777;hermes-replay;abort;/g) || []).length

    // Release reconstruction drain
    stdout.releaseDrain()

    // Wait for replay ABORT to be emitted after reconstruction drain
    await vi.waitFor(() => {
      const chunksAfterDrain = stdout.chunks.join('')
      const abortOccurrencesAfter = (chunksAfterDrain.match(/\x1b\]777;hermes-replay;abort;/g) || []).length
      expect(abortOccurrencesAfter).toBe(abortOccurrencesBefore + 1)
    })

    await dirtyResumePromise.catch(() => {})

    // Ink instance remained alive throughout
    expect(activeInstance).not.toBeNull()
  })
  it.each([false, true])('serializes cancellation during acquisition (cleanup failure=%s)', async failCleanup => {
    gw.requestHandlers.set('session.resume', async (p: any) => ({ session_id: p.session_id, status: 'idle', messages: [] }))
    gw.requestHandlers.set('session.history', async () => ({ messages: [], total: 0 }))
    const request = vi.spyOn(gw, 'request')
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    let finishAcquisition!: () => void
    let abort: ReturnType<typeof vi.fn> | undefined
    vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementationOnce(async out => {
      const lease = await realAcquire(out)
      const realAbort = lease.abort.bind(lease)
      abort = vi.fn(async () => {
        if (failCleanup) throw new Error('stale acquisition cleanup failed')
        return realAbort()
      })
      lease.abort = abort
      await new Promise<void>(resolve => { finishAcquisition = resolve })
      return lease
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, { gw, stdout, onReady: l => { lifecycle = l } }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('acquiring-A')
    await vi.waitFor(() => expect(finishAcquisition).toBeTypeOf('function'))
    let settled = false
    const successor = lifecycle!.resumeById('after-acquire-B').then(
      () => { settled = true; return null }, error => { settled = true; return error }
    )
    await new Promise(resolve => setImmediate(resolve))
    expect(settled).toBe(false)
    expect(request).not.toHaveBeenCalledWith('session.resume', expect.objectContaining({ session_id: 'after-acquire-B' }))
    finishAcquisition()
    const error = await successor
    expect(abort).toHaveBeenCalledTimes(1)
    if (failCleanup) {
      expect(String(error)).toContain('stale acquisition cleanup failed')
      await expect(lifecycle!.resumeById('after-acquire-C')).rejects.toThrow('stale acquisition cleanup failed')
      expect(abort).toHaveBeenCalledTimes(1)
      expect(stdout.chunks.join('')).not.toContain('hermes-replay;abort;')
      expect(request).not.toHaveBeenCalledWith('session.resume', expect.objectContaining({ session_id: 'after-acquire-C' }))
    } else {
      expect(error).toBeNull()
      await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledWith('after-acquire-B', expect.any(String)))
      expect((stdout.chunks.join('').match(/hermes-replay;abort;/g) ?? []).length).toBe(1)
    }
  })
  it('successful reconstruction after resize-rejected handoff permits the next session', async () => {
    gw.requestHandlers.set('session.resume', async (p: any) => ({ session_id: p.session_id, status: 'idle', messages: [] }))
    gw.requestHandlers.set('session.history', async (p: any) => ({
      messages: Array.from({ length: p.session_id === 'resize-A' ? 6 : 1 }, (_, i) => ({
        id: `${p.session_id}-${i}`, role: 'user', text: `${p.session_id}_${i}`, row_id: i
      })), total: p.session_id === 'resize-A' ? 6 : 1
    }))
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    let reconstruct: ReturnType<typeof vi.spyOn> | undefined
    vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementationOnce(async out => {
      const lease = await realAcquire(out)
      reconstruct = vi.spyOn(lease, 'reconstructAndRelease')
      const release = lease.release.bind(lease)
      lease.release = async () => {
        stdout.columns = 60
        stdout.emit('resize')
        return release()
      }
      return lease
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, {
      gw, stdout, coldHydrationMaxMounted: 2, onReady: l => { lifecycle = l }
    }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('resize-A')
    await vi.waitFor(() => expect(stdout.chunks.join('')).toContain('hermes-replay;abort;'))
    expect(reconstruct).toHaveBeenCalledTimes(1)
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('resize-A')
    await expect(lifecycle!.resumeById('after-resize-B')).resolves.toBeUndefined()
    await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledWith('after-resize-B', expect.any(String)))
  })

  it('retries healthy history failure only after reconstruction drains, with a fresh snapshot and generation', async () => {
    let attempts = 0
    const historyParams: any[] = []
    gw.requestHandlers.set('session.resume', async (p: any) => {
      attempts++
      return { session_id: p.session_id, status: 'idle', messages: [] }
    })
    gw.requestHandlers.set('session.history', async (p: any) => {
      historyParams.push(p)
      if (attempts === 1 && p.cursor > 0) throw new JsonRpcGatewayError('history backend transient', { code: 5000 })
      return {
        messages: Array.from({ length: 4 }, (_, i) => ({ id: `retry-${i}`, role: 'user', text: `RETRY_${i}`, row_id: i })),
        total: 4, has_more: attempts === 1, next_cursor: 4, snapshot_token: `snapshot-${attempts}`
      }
    })
    stdout.blockDrainPattern = /\x1b\[3J/
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, {
      gw, stdout, coldHydrationMaxMounted: 2, onReady: l => { lifecycle = l }
    }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('retry-A')
    await vi.waitFor(() => expect(stdout.writes.some(w => !w.drained && w.data.includes('\x1b[3J'))).toBe(true))
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(attempts).toBe(1)
    expect(stdout.chunks.join('')).not.toContain('hermes-replay;abort;')
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('retry-A')
    stdout.releaseDrain()
    await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledTimes(1), { timeout: 2000 })
    expect(attempts).toBe(2)
    expect(historyParams.map(p => [p.cursor, p.snapshot_token])).toEqual([[0, undefined], [4, 'snapshot-1'], [0, undefined]])
    expect(gw.activateEventBarrier).toHaveBeenCalledTimes(1)
    expect(gw.cancelEventBarrier).not.toHaveBeenCalled()
    const output = stdout.chunks.join('')
    const generations = [...output.matchAll(/hermes-replay;begin;([^\x07]+)/g)].map(match => match[1])
    expect(new Set(generations).size).toBe(2)
    expect(output.indexOf(`hermes-replay;abort;${generations[0]}`)).toBeLessThan(output.indexOf(`hermes-replay;begin;${generations[1]}`))
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBeNull()
  })

  it.each(['exhausted', 'switch', 'transport', 'unmount'] as const)('history retry is bounded and cancelled: %s', async mode => {
    let attempts = 0
    gw.requestHandlers.set('session.resume', async (p: any) => {
      if (p.session_id === 'retry-A') attempts++
      return { session_id: p.session_id, status: 'idle', messages: [] }
    })
    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id !== 'retry-A') return { messages: [], total: 0 }
      if (mode === 'transport') throw new Error('socket closed')
      throw new JsonRpcGatewayError('backend unavailable', { code: 5000 })
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, { gw, stdout, onReady: l => { lifecycle = l } }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('retry-A')
    await vi.waitFor(() => expect(stdout.chunks.join('')).toContain('hermes-replay;abort;'))
    if (mode === 'switch') await lifecycle!.resumeById('B')
    if (mode === 'unmount') activeInstance.unmount()
    if (mode === 'exhausted') {
      await vi.waitFor(() => expect(getUiState().status).toBe('history incomplete'), { timeout: 3000 })
      expect(attempts).toBe(4)
      expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('retry-A')
    } else {
      await new Promise(resolve => setTimeout(resolve, 400))
      expect(attempts).toBe(1)
      if (mode === 'transport') expect(getUiState().status).toBe('disconnected')
    }
  })

  it.each(['resume-first', 'history-first'] as const)('independent resume and history retry budgets: %s', async order => {
    let resumes = 0
    let histories = 0
    const timers = vi.spyOn(globalThis, 'setTimeout')
    gw.requestHandlers.set('session.resume', async (p: any) => {
      resumes++
      const busy = order === 'resume-first' ? resumes <= 2 : resumes > 1 && resumes <= 5
      if (busy) throw new JsonRpcGatewayError('busy', { code: 4009 })
      return { session_id: p.session_id, status: 'idle', messages: [] }
    })
    gw.requestHandlers.set('session.history', async () => {
      histories++
      if (histories <= (order === 'resume-first' ? 3 : 1)) {
        throw new JsonRpcGatewayError('history backend transient', { code: 5000 })
      }
      return { messages: [], total: 0 }
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, { gw, stdout, onReady: l => { lifecycle = l } }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('retry-budget')
    await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledTimes(1), { timeout: 6500 })
    expect(histories).toBe(order === 'resume-first' ? 4 : 2)
    expect(resumes).toBe(6)
    expect(timers.mock.calls.map(call => call[1]).filter(delay => [250, 500, 1000, 2000].includes(Number(delay))))
      .toEqual(order === 'resume-first' ? [250, 500, 250, 500, 1000] : [250, 250, 500, 1000, 2000])
    expect(gw.activateEventBarrier).toHaveBeenCalledTimes(1)
    expect(gw.cancelEventBarrier).not.toHaveBeenCalled()
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBeNull()
  }, 8000)

  it.each(['backoff', 'resume-in-flight'] as const)('transport invalidation stops stale history retry: %s', async phase => {
    let resumes = 0
    let histories = 0
    let resolveRetry!: (response: any) => void
    gw.requestHandlers.set('session.resume', async (p: any) => {
      resumes++
      if (phase === 'resume-in-flight' && resumes === 2) {
        return new Promise(resolve => { resolveRetry = resolve })
      }
      return { session_id: p.session_id, status: 'idle', messages: [] }
    })
    gw.requestHandlers.set('session.history', async () => {
      histories++
      if (histories === 1) throw new JsonRpcGatewayError('history failed', { code: 5000 })
      return { messages: [], total: 0 }
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, { gw, stdout, onReady: l => { lifecycle = l } }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('transport-retry')
    await vi.waitFor(() => expect(stdout.chunks.join('')).toContain('hermes-replay;abort;'))
    const oldOwner = gw.activateEventBarrier.mock.calls[0]![1]
    if (phase === 'resume-in-flight') await vi.waitFor(() => expect(resolveRetry).toBeTypeOf('function'))
    // GatewayClient clears actual ownership on disconnect without changing the hook's local ref.
    gw.barriers.clear()
    if (phase === 'resume-in-flight') resolveRetry({ session_id: 'transport-retry', status: 'idle', messages: [] })
    await new Promise(resolve => setTimeout(resolve, 400))
    expect(resumes).toBe(phase === 'backoff' ? 1 : 2)
    expect(histories).toBe(1)
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()
    expect(gw.activateEventBarrier).toHaveBeenCalledTimes(1)
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('transport-retry')
    // Invoke the same recovery entry used by gateway.ready, not an internal retry-chain continuation.
    await lifecycle!.resumeById('transport-retry', undefined, 0, { mode: 'transport-recovery' })
    await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledTimes(1))
    const newOwner = gw.activateEventBarrier.mock.calls[1]![1]
    expect(newOwner).not.toBe(oldOwner)
    expect(histories).toBe(2)
    expect(gw.releaseEventBarrier).toHaveBeenCalledWith('transport-retry', newOwner)
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBeNull()
  })

  it.each(['acquire-fresh', 'acquire-retry', 'page', 'handoff'] as const)('actual barrier ownership is required after cold async boundary: %s', async phase => {
    let histories = 0
    let finishAcquisition!: () => void
    let finishPage!: (page: any) => void
    let cleanAbort: ReturnType<typeof vi.spyOn> | undefined
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    let acquisitions = 0
    vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementation(async out => {
      const lease = await realAcquire(out)
      acquisitions++
      if ((phase === 'acquire-fresh' && acquisitions === 1) || (phase === 'acquire-retry' && acquisitions === 2)) {
        cleanAbort = vi.spyOn(lease, 'abort')
        await new Promise<void>(resolve => { finishAcquisition = resolve })
      }
      return lease
    })
    gw.requestHandlers.set('session.resume', async (p: any) => ({ session_id: p.session_id, status: 'idle', messages: [] }))
    gw.requestHandlers.set('session.history', async () => {
      histories++
      if (phase === 'acquire-retry' && histories === 1) throw new JsonRpcGatewayError('history transient', { code: 5000 })
      if (phase === 'page' && histories === 1) return new Promise(resolve => { finishPage = resolve })
      return { messages: [{ id: 'last', role: 'user', text: 'STALE_BOUNDARY_TOKEN', row_id: 0 }], total: 1 }
    })
    if (phase === 'handoff') stdout.blockDrainPattern = /STALE_BOUNDARY_TOKEN/
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    activeInstance = renderSync(React.createElement(LifecycleHarness, { gw, stdout, onReady: l => { lifecycle = l } }), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('boundary-A')
    if (phase.startsWith('acquire')) await vi.waitFor(() => expect(finishAcquisition).toBeTypeOf('function'))
    if (phase === 'page') await vi.waitFor(() => expect(finishPage).toBeTypeOf('function'))
    if (phase === 'handoff') await vi.waitFor(() => expect(stdout.writes.some(w => !w.drained && w.data.includes('STALE_BOUNDARY_TOKEN'))).toBe(true))
    const owner = gw.activateEventBarrier.mock.calls[0]![1]
    expect(gw.hasEventBarrier('boundary-A', owner)).toBe(true)
    const beginCount = (stdout.chunks.join('').match(/hermes-replay;begin;/g) ?? []).length
    const abortCount = (stdout.chunks.join('').match(/hermes-replay;abort;/g) ?? []).length
    const beforeHistories = histories
    gw.barriers.clear()
    if (phase.startsWith('acquire')) finishAcquisition()
    if (phase === 'page') finishPage({ messages: [{ id: 'stale', role: 'user', text: 'MUST_NOT_COMMIT' }], total: 1 })
    if (phase === 'handoff') stdout.releaseDrain()
    await vi.waitFor(() => expect((stdout.chunks.join('').match(/hermes-replay;abort;/g) ?? []).length).toBe(abortCount + 1))
    expect((stdout.chunks.join('').match(/hermes-replay;begin;/g) ?? []).length).toBe(beginCount)
    expect(stdout.chunks.join('')).not.toContain('hermes-replay;end;')
    expect(stdout.chunks.join('')).not.toContain('MUST_NOT_COMMIT')
    expect(histories).toBe(beforeHistories)
    expect(gw.releaseEventBarrier).not.toHaveBeenCalled()
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('boundary-A')
    if (phase.startsWith('acquire')) expect(cleanAbort).toHaveBeenCalledTimes(1)
    await lifecycle!.resumeById('boundary-A', undefined, 0, { mode: 'transport-recovery' })
    await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledTimes(1))
    const newOwner = gw.activateEventBarrier.mock.calls[1]![1]
    expect(newOwner).not.toBe(owner)
    expect(gw.releaseEventBarrier).toHaveBeenCalledWith('boundary-A', newOwner)
    expect(lifecycle!.coldHydrationIncompleteRef.current).toBeNull()
  })

  it.each([false, true])('remount waits for old component cleanup and preserves failure quarantine=%s', async fail => {
    gw.requestHandlers.set('session.resume', async (p: any) => ({ session_id: p.session_id, status: 'idle', messages: [] }))
    let resolvePage!: (page: any) => void
    gw.requestHandlers.set('session.history', async (p: any) => {
      if (p.session_id !== 'old-A') return { messages: [], total: 0 }
      if (p.cursor) return new Promise(resolve => { resolvePage = resolve })
      return { messages: Array.from({ length: 4 }, (_, i) => ({ id: `old-${i}`, role: 'user', text: `OLD_${i}`, row_id: i })), total: 8, has_more: true, next_cursor: 4 }
    })
    const request = vi.spyOn(gw, 'request')
    const realAcquire = InkModule.acquireMainScreenStaticOutput
    let finishCleanup!: () => void
    const acquire = vi.spyOn(InkModule, 'acquireMainScreenStaticOutput').mockImplementationOnce(async out => {
      const lease = await realAcquire(out)
      const reconstruct = lease.reconstructAndRelease.bind(lease)
      lease.reconstructAndRelease = async () => {
        await new Promise<void>(resolve => { finishCleanup = resolve })
        if (fail) throw new Error('orphan reconstruction failed')
        return reconstruct()
      }
      return lease
    })
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    let toggle!: (value: boolean) => void
    function Parent() {
      const [mounted, setMounted] = useState(true)
      toggle = setMounted
      return mounted ? React.createElement(LifecycleHarness, {
        gw, stdout, coldHydrationMaxMounted: 2, onReady: l => { lifecycle = l }
      }) : React.createElement('ink-text', null, 'unmounted')
    }
    activeInstance = renderSync(React.createElement(Parent), { stdout: stdout as any })
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    await lifecycle!.resumeById('old-A')
    await vi.waitFor(() => expect(resolvePage).toBeTypeOf('function'))
    toggle(false)
    await vi.waitFor(() => expect(gw.cancelEventBarrier).toHaveBeenCalled())
    resolvePage({ messages: [], total: 4 })
    await vi.waitFor(() => expect(finishCleanup).toBeTypeOf('function'))
    lifecycle = null
    toggle(true)
    await vi.waitFor(() => expect(lifecycle).not.toBeNull())
    let settled = false
    const successor = lifecycle!.resumeById('new-B').then(() => { settled = true; return null }, err => { settled = true; return err })
    await new Promise(resolve => setImmediate(resolve))
    expect(settled).toBe(false)
    expect(acquire).toHaveBeenCalledTimes(1)
    expect(request).not.toHaveBeenCalledWith('session.resume', expect.objectContaining({ session_id: 'new-B' }))
    finishCleanup()
    const error = await successor
    if (fail) {
      expect(String(error)).toContain('Terminal recovery required: orphan reconstruction failed')
      await expect(lifecycle!.resumeById('new-C')).rejects.toThrow('Terminal recovery required')
      expect(acquire).toHaveBeenCalledTimes(1)
      expect(stdout.chunks.join('')).not.toContain('hermes-replay;abort;')
    } else {
      expect(error).toBeNull()
      await vi.waitFor(() => expect(gw.releaseEventBarrier).toHaveBeenCalledWith('new-B', expect.any(String)))
    }
  })
})
