import { randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'

import type { ScrollBoxHandle } from '@hermes/ink'
import {
  evictInkCaches,
  writeAfterRender,
  acquireMainScreenStaticOutput,
  type MainScreenStaticOutputLease
} from '@hermes/ink'
import type { InflightTurn, SessionResumeResult, Usage } from '@hermes/shared/gateway-events'
import { type RefObject, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { INLINE_MODE, DASHBOARD_TUI_MODE } from '../config/env.js'

import { buildSetupRequiredSections, SETUP_REQUIRED_TITLE } from '../content/setup.js'
import { introMsg, toTranscriptMessages } from '../domain/messages.js'
import {
  performColdHistoryHydration,
  ColdHydrationCancelledError,
  type ColdHistoryOutput
} from './coldHistoryHydration.js'

export type ColdTransactionPhase =
  | 'hydrating'
  | 'commit-pending'
  | 'finalizing'
  | 'cancelling'
  | 'reconstructing'
  | 'settled'

export type ColdSettlement =
  | { ok: true }
  | { ok: false; error: unknown; terminalDisposed?: boolean }

export const isTerminalDisposed = (err: unknown): boolean => {
  if (!err) return false
  if (typeof err === 'object' && 'terminalDisposed' in err && Boolean((err as any).terminalDisposed)) {
    return true
  }
  const msg = err instanceof Error ? err.message : String(err)
  return (
    msg.includes('Ink instance is unmounted') ||
    msg.includes('terminal disposed') ||
    msg.includes('terminal is unmounted')
  )
}

export interface ReplayGenerationState {
  attemptId: string
  generation: string
  aborted: boolean
  ended: boolean
}

export interface ActiveColdOutputTransaction {
  attemptId: string
  barrierOwner: string
  sid: string
  durableKey: string
  lease: MainScreenStaticOutputLease | null
  acquisitionPromise?: Promise<MainScreenStaticOutputLease>
  boundaryGeneration: string | null
  staticOutputStarted: boolean
  settlementPromise: Promise<ColdSettlement>
  complete: (result: ColdSettlement) => void
  phase: ColdTransactionPhase
  setPhase: (phase: ColdTransactionPhase) => void
  appendedToScrollback?: boolean
  cleanupPromise?: Promise<void>
  replayState?: ReplayGenerationState | null
}
import { ZERO } from '../domain/usage.js'
import { type GatewayClient } from '../gatewayClient.js'
import type {
  SessionActivateResponse,
  SessionCloseResponse,
  SessionCreateResponse,
  SessionHistoryResponse,
  SessionTitleResponse,
  SessionViewportMeta,
  SetupStatusResponse
} from '../gatewayTypes.js'
import { asRpcResult } from '../lib/rpc.js'
import type { Msg, PanelSection, SessionInfo } from '../types.js'

import { applyConnectionRequest, clearConnectionOperation } from './connectionOperationStore.js'
import type { ComposerActions, GatewayRpc, StateSetter } from './interfaces.js'
import { activeRecoveryTargetRef } from './gatewayRecovery.js'
import { classifyResumeFailure } from './sessionRecovery.js'
import { patchOverlayState } from './overlayStore.js'
import { scheduleResumeScrollToBottom } from './sessionResumeView.js'
import { turnController } from './turnController.js'
import { patchTurnState } from './turnStore.js'
import { getUiState, patchUiState } from './uiStore.js'
import { describeCredentialWarning } from './userMessages.js'

export { refreshSessionView, scheduleResumeScrollToBottom } from './sessionResumeView.js'

const usageFrom = (info: null | SessionInfo): Usage => (info?.usage ? { ...ZERO, ...info.usage } : ZERO)

const statusFromLiveSession = (status?: string, running = false) => {
  if (status === 'waiting') {
    return 'waiting for input…'
  }

  if (status === 'starting') {
    return 'starting agent…'
  }

  return running || status === 'working' ? 'running…' : 'ready'
}

export const readActiveSessionFile = (file = process.env.HERMES_TUI_ACTIVE_SESSION_FILE): string | null => {
  if (!file) {
    return null
  }
  try {
    const raw = readFileSync(file, 'utf8')
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') {
      const candidate = parsed.session_id || parsed.session_key
      if (candidate && typeof candidate === 'string') {
        return candidate
      }
    }
    return null
  } catch {
    return null
  }
}

export const writeActiveSessionFile = (sessionId: null | string, file = process.env.HERMES_TUI_ACTIVE_SESSION_FILE) => {
  if (!file || !sessionId) {
    return
  }

  try {
    writeFileSync(file, JSON.stringify({ session_id: sessionId }), { mode: 0o600 })
  } catch {
    // Best-effort shell epilogue hint only; never break live session changes.
  }
}

export const liveSessionInflightMessages = (
  inflight?: null | InflightTurn,
  existingMessages?: Msg[]
): Msg[] => {
  const user = String(inflight?.user ?? '').trim()
  if (!user) {
    return []
  }

  if (existingMessages && existingMessages.length > 0) {
    const lastUser = [...existingMessages].reverse().find(m => m.role === 'user')
    if (lastUser && lastUser.text.trim() === user) {
      return []
    }
  }

  return toTranscriptMessages([
    {
      role: 'user',
      text: user,
      ...(inflight?.display_kind ? { display_kind: inflight.display_kind } : {}),
      ...(inflight?.display_metadata ? { display_metadata: inflight.display_metadata } : {})
    }
  ])
}

export const hydrateLiveSessionInflight = (inflight?: null | InflightTurn) => {
  const assistant = String(inflight?.assistant ?? '')

  if (!assistant && !inflight?.streaming) {
    return
  }

  turnController.hydrateStreamingText(assistant)
}

export const signalFreshSessionBoundary = (
  previousSid: null | string,
  nextSid: null | string,
  onFreshSessionStarted?: (sessionId: string) => void
) => {
  if (!previousSid || !nextSid || previousSid === nextSid || !onFreshSessionStarted) {
    return false
  }

  onFreshSessionStarted(nextSid)

  return true
}

export const trimTail = (items: Msg[], turns = 1) => {
  const q = [...items]

  for (let t = 0; t < turns; t++) {
    while (
      q.length > 0 &&
      (q.at(-1)?.role === 'system' ||
        (q.at(-1) as any)?.kind === 'slash' ||
        (q.at(-1) as any)?.kind === 'system' ||
        (q.at(-1) as any)?.kind === 'panel')
    ) {
      q.pop()
    }

    while (
      q.length > 0 &&
      (q.at(-1)?.role === 'assistant' ||
        q.at(-1)?.role === 'tool' ||
        (q.at(-1) as any)?.kind === 'trail' ||
        (q.at(-1) as any)?.kind === 'diff')
    ) {
      q.pop()
    }

    if (q.length > 0 && q.at(-1)?.role === 'user') {
      q.pop()
    }
  }

  while (
    q.length > 0 &&
    (q.at(-1)?.role === 'system' ||
      (q.at(-1) as any)?.kind === 'slash' ||
      (q.at(-1) as any)?.kind === 'system' ||
      (q.at(-1) as any)?.kind === 'panel')
  ) {
    q.pop()
  }

  return q
}

export interface UseSessionLifecycleOptions {
  colsRef: { current: number }
  composerActions: ComposerActions
  gw: GatewayClient
  onFreshSessionStarted?: (sessionId: string) => void
  panel: (title: string, sections: PanelSection[]) => void
  recoverSessionKeyRef?: { current: string | null }
  recoverSidRef?: { current: string | null }
  coldHydrationIncompleteRef?: { current: string | null }
  rpc: GatewayRpc
  scrollRef: RefObject<null | ScrollBoxHandle>
  setHistoryItems: StateSetter<Msg[]>
  setLastUserMsg: StateSetter<string>
  setSessionStartedAt: StateSetter<number>
  setStickyPrompt: StateSetter<string>
  setVoiceProcessing: StateSetter<boolean>
  setVoiceRecording: StateSetter<boolean>
  sys: (text: string) => void
  stdout?: NodeJS.WriteStream
  coldHydrationMaxMounted?: number
}

// A failed physical cleanup outlives its React hook; reusing that stream requires a PTY restart.
const terminalQuarantine = new WeakMap<NodeJS.WriteStream, Promise<ColdSettlement>>()

export function useSessionLifecycle(opts: UseSessionLifecycleOptions) {
  const {
    colsRef,
    composerActions,
    gw,
    onFreshSessionStarted,
    panel,
    rpc,
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setStickyPrompt,
    setVoiceProcessing,
    setVoiceRecording,
    sys
  } = opts

  const stdout = opts.stdout ?? process.stdout
  const maxMounted = opts.coldHydrationMaxMounted ?? 120

  const recoverSessionKeyRef = opts.recoverSessionKeyRef ?? opts.recoverSidRef

  const closeSession = useCallback(
    (targetSid?: null | string) => {
      if (targetSid) {
        gw?.retireSession(targetSid)
        return rpc<SessionCloseResponse>('session.close', { session_id: targetSid })
      }
      return Promise.resolve(null)
    },
    [gw, rpc]
  )

  const coldRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const resumeAttemptRef = useRef<string | null>(null)
  const replayGeneration = useRef<string | null>(null)
  const activeReplayBoundaryRef = useRef<ReplayGenerationState | null>(null)
  const currentReplayStateRef = useRef<ReplayGenerationState | null>(null)
  const [replayCommitted, setReplayCommitted] = useState<{ generation: string; replaceFrame: boolean } | null>(null)
  const activeColdOutputRef = useRef<ActiveColdOutputTransaction | null>(null)
  const pendingColdCommitRef = useRef<ActiveColdOutputTransaction | null>(null)
  const activeColdBarrierRef = useRef<{ attemptId: string; sid: string } | null>(null)
  const [coldCommitGeneration, setColdCommitGeneration] = useState<string | null>(null)
  const localColdHydrationIncompleteRef = useRef<string | null>(null)
  const coldHydrationIncompleteRef = opts.coldHydrationIncompleteRef ?? localColdHydrationIncompleteRef

  const clearActiveColdBarrier = useCallback((attemptId: string, sid: string) => {
    const active = activeColdBarrierRef.current
    if (active?.attemptId === attemptId && active.sid === sid) {
      activeColdBarrierRef.current = null
    }
  }, [])

  const abortReplayOnce = useCallback(
    (generationOrTx?: string | ActiveColdOutputTransaction | null) => {
      let targetState: ReplayGenerationState | null = null
      let targetGeneration: string | null = null

      if (typeof generationOrTx === 'string') {
        targetGeneration = generationOrTx
        if (currentReplayStateRef.current?.generation === generationOrTx) {
          targetState = currentReplayStateRef.current
        } else if (activeReplayBoundaryRef.current?.generation === generationOrTx) {
          targetState = activeReplayBoundaryRef.current
        }
      } else if (generationOrTx && typeof generationOrTx === 'object') {
        targetGeneration = generationOrTx.boundaryGeneration
        if (generationOrTx.replayState) {
          targetState = generationOrTx.replayState
        } else if (
          activeReplayBoundaryRef.current?.attemptId === generationOrTx.attemptId ||
          activeReplayBoundaryRef.current?.generation === generationOrTx.boundaryGeneration
        ) {
          targetState = activeReplayBoundaryRef.current
        }
      } else if (activeReplayBoundaryRef.current) {
        targetState = activeReplayBoundaryRef.current
        targetGeneration = targetState.generation
      }

      if (!targetGeneration) {
        return
      }

      if (targetState) {
        if (targetState.aborted || targetState.ended) {
          return
        }
        targetState.aborted = true
        if (activeReplayBoundaryRef.current === targetState) {
          activeReplayBoundaryRef.current = null
        }
      }

      stdout.write(`\x1b]777;hermes-replay;abort;${targetGeneration}\x07`)
    },
    [stdout]
  )

  const settleCancelledColdTx = useCallback(
    async (tx: ActiveColdOutputTransaction, preserveBarrier = false) => {
      tx.setPhase('reconstructing')
      if (!preserveBarrier) {
        clearActiveColdBarrier(tx.barrierOwner, tx.sid)
        gw.cancelEventBarrier(tx.sid, tx.barrierOwner)
      }

      if (!tx.cleanupPromise) {
        tx.cleanupPromise = (async () => {
          if (!tx.lease && tx.acquisitionPromise) {
            try {
              tx.lease = await tx.acquisitionPromise
            } catch {
              // Acquisition failed, no physical lease held
            }
          }
          if (tx.lease) {
            if (tx.staticOutputStarted) {
              await tx.lease.reconstructAndRelease()
            } else {
              await tx.lease.abort()
            }
          }
          abortReplayOnce(tx)
        })()
      }

      try {
        await tx.cleanupPromise
        // Settlement describes physical ownership, not the abandoned hydration result.
        tx.complete({ ok: true })
        if (activeColdOutputRef.current === tx) {
          activeColdOutputRef.current = null
        }
      } catch (cleanupErr) {
        clearActiveColdBarrier(tx.barrierOwner, tx.sid)
        gw.cancelEventBarrier(tx.sid, tx.barrierOwner)
        const terminalDisposed = isTerminalDisposed(cleanupErr)
        terminalQuarantine.set(stdout, tx.settlementPromise)
        tx.complete({ ok: false, error: cleanupErr, terminalDisposed })
        // Failed cleanup: do NOT clear activeColdOutputRef.current so successors stay blocked!
        throw cleanupErr
      }
    },
    [abortReplayOnce, clearActiveColdBarrier, gw, stdout]
  )

  const supersedeColdHydration = useCallback(async (preserveBarrier?: { attemptId: string; sid: string }) => {
    if (coldRetryTimerRef.current) {
      clearTimeout(coldRetryTimerRef.current)
      coldRetryTimerRef.current = null
    }
    resumeAttemptRef.current = null
    const quarantine = terminalQuarantine.get(stdout)
    if (quarantine) {
      const result = await quarantine
      if (!result.ok) {
        throw new Error(`Terminal recovery required: ${result.error instanceof Error ? result.error.message : String(result.error)}`, { cause: result.error })
      }
      if (terminalQuarantine.get(stdout) === quarantine) terminalQuarantine.delete(stdout)
    }

    const pending = pendingColdCommitRef.current
    if (pending) {
      // Ownership transferred atomically to superseder
      pendingColdCommitRef.current = null
      await settleCancelledColdTx(pending)
      return
    }

    const active = activeColdBarrierRef.current
    if (active && active !== preserveBarrier) {
      activeColdBarrierRef.current = null
      gw.cancelEventBarrier(active.sid, active.attemptId)
    }

    const tx = activeColdOutputRef.current
    if (tx) {
      tx.setPhase('cancelling')
      const result = await tx.settlementPromise
      if (result.ok) {
        if (activeColdOutputRef.current === tx) {
          activeColdOutputRef.current = null
        }
      } else {
        // Failed cleanup / settlement outcome: keep activeColdOutputRef.current to block successors!
        throw result.error
      }
      return
    }

    const boundary = activeReplayBoundaryRef.current
    if (boundary) {
      abortReplayOnce(boundary.generation)
    }
  }, [abortReplayOnce, clearActiveColdBarrier, gw, settleCancelledColdTx, stdout])

  useLayoutEffect(() => {
    const pending = pendingColdCommitRef.current
    if (!pending) return
    pendingColdCommitRef.current = null

    if (pending.attemptId !== resumeAttemptRef.current || !gw.hasEventBarrier(pending.sid, pending.barrierOwner)) {
      void settleCancelledColdTx(pending).catch(() => {})
      return
    }

    pending.setPhase('finalizing')

    void (async () => {
      try {
        const lease = pending.lease
        if (!lease) throw new Error('Cold commit cannot finalize without an acquired lease')
        if (pending.appendedToScrollback) {
          lease.prepareAppendHandoff()
        }
        await lease.release()
      } catch (err) {
        console.error('Lease release failed in finalizeColdCommit:', err)
        // Fail-closed invariant: if lease release/handoff fails, abort and do not release event barrier or clear incomplete marker!
        await settleCancelledColdTx(pending).catch(() => {})
        return
      }

      if (pending.attemptId !== resumeAttemptRef.current || !gw.hasEventBarrier(pending.sid, pending.barrierOwner)) {
        clearActiveColdBarrier(pending.barrierOwner, pending.sid)
        gw.cancelEventBarrier(pending.sid, pending.barrierOwner)
        abortReplayOnce(pending)
        if (activeColdOutputRef.current === pending) {
          activeColdOutputRef.current = null
        }
        pending.complete({ ok: true })
        return
      }

      if (activeColdOutputRef.current === pending) {
        activeColdOutputRef.current = null
      }

      clearActiveColdBarrier(pending.barrierOwner, pending.sid)
      gw.releaseEventBarrier(pending.sid, pending.barrierOwner)
      coldHydrationIncompleteRef.current = null
      if (pending.boundaryGeneration) {
        setReplayCommitted({ generation: pending.boundaryGeneration, replaceFrame: false })
      }
      pending.complete({ ok: true })
    })()
  }, [abortReplayOnce, clearActiveColdBarrier, coldCommitGeneration, coldHydrationIncompleteRef, gw, settleCancelledColdTx])

  useEffect(() => {
    return () => {
      const tx = activeColdOutputRef.current
      // Begin cancellation before publishing the wait, so this owner never awaits itself.
      const cleanup = supersedeColdHydration()
      if (tx) terminalQuarantine.set(stdout, tx.settlementPromise)
      void cleanup.then(() => {
        if (tx && terminalQuarantine.get(stdout) === tx.settlementPromise) terminalQuarantine.delete(stdout)
      }).catch(error => {
        // Production lifecycle is root-owned; component remounts must not bypass a failed owner.
        console.error('Terminal recovery required; restart the PTY:', error)
      })
    }
  }, [stdout, supersedeColdHydration])

  useLayoutEffect(() => {
    if (replayCommitted && replayCommitted.generation === replayGeneration.current) {
      const state = currentReplayStateRef.current
      if (state && state.generation === replayCommitted.generation) {
        if (!state.aborted && !state.ended) {
          state.ended = true
          activeReplayBoundaryRef.current = null
          writeAfterRender(`\x1b]777;hermes-replay;end;${replayCommitted.generation}\x07`, stdout, replayCommitted.replaceFrame)
        }
      }
    }
  }, [replayCommitted, stdout])

  const cancelResumeScrollRef = useRef<null | (() => void)>(null)
  const [viewportMeta, setViewportMeta] = useState<SessionViewportMeta | null>(null)
  const isFetchingBacklogRef = useRef(false)

  const resetSession = useCallback(() => {
    cancelResumeScrollRef.current?.()
    cancelResumeScrollRef.current = null
    turnController.fullReset()
    setVoiceRecording(false)
    setVoiceProcessing(false)
    setViewportMeta(null)
    isFetchingBacklogRef.current = false
    patchUiState({ bgTasks: new Set(), info: null, sessionKey: null, sid: null, storedSid: null, usage: ZERO })
    setHistoryItems([])
    setLastUserMsg('')
    setStickyPrompt('')
    composerActions.setComposerTokens([])
    // Half-prune: new session has new keys, but keep a warm pool in case
    // the user resumes back to the prior session.
    evictInkCaches('half')
  }, [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt, setVoiceProcessing, setVoiceRecording])

  useEffect(
    () => () => {
      cancelResumeScrollRef.current?.()
      cancelResumeScrollRef.current = null
    },
    []
  )

  const resetVisibleHistory = useCallback(
    (info: null | SessionInfo = null) => {
      turnController.idle()
      turnController.clearReasoning()
      turnController.turnTools = []
      turnController.persistedToolLabels.clear()

      setHistoryItems(info ? [introMsg(info)] : [])
      setStickyPrompt('')
      setLastUserMsg('')
      composerActions.setComposerTokens([])
      patchTurnState({ activity: [] })
      patchUiState({ info, usage: usageFrom(info) })
    },
    [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt]
  )

  const startNewSession = useCallback(
    async (msg?: string, title?: string, keepCurrent = false) => {
      await supersedeColdHydration()
      const setup = await rpc<SetupStatusResponse>('setup.status', {})

      if (setup?.provider_configured === false) {
        panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections())
        patchUiState({ status: 'setup required' })

        return null
      }

      const previousSid = getUiState().sid

      if (!keepCurrent) {
        await closeSession(previousSid)
      }

      const r = await rpc<SessionCreateResponse>('session.create', { cols: colsRef.current })

      if (!r) {
        patchUiState({ status: 'ready' })

        return null
      }

      // The durable id lives on the create result; the lazy-create `info` does
      // not carry it, and session.resume / the exit epilogue need the stored id.
      const storedSid = r.stored_session_id || r.session_id
      const info = r.info ? { ...r.info, stored_session_id: storedSid } : null
      const requestedTitle = title?.trim() ?? ''

      resetSession()
      setSessionStartedAt(Date.now())

      const durableKey = (r as any).stored_session_id ?? r.session_id
      writeActiveSessionFile(durableKey)
      patchUiState({
        info,
        sessionKey: durableKey,
        sid: r.session_id,
        status: info?.version ? 'ready' : 'starting agent…',
        storedSid,
        usage: usageFrom(info)
      })

      if (info) {
        setHistoryItems([introMsg(info)])
      }

      if (info?.credential_warning) {
        sys(`warning: ${describeCredentialWarning(info.credential_warning)}`)
      }

      if (info?.config_warning) {
        sys(`warning: ${info.config_warning}`)
      }

      if (msg) {
        sys(msg)
      }

      if (requestedTitle) {
        rpc<SessionTitleResponse>('session.title', {
          session_id: r.session_id,
          title: requestedTitle
        })
          .then(result => {
            if (!result || getUiState().sid !== r.session_id) {
              return
            }

            const nextTitle = (result.title ?? requestedTitle).trim()
            const suffix = result.pending ? ' (queued while session initializes)' : ''
            patchUiState({ sessionTitle: nextTitle })
            sys(`session title set: ${nextTitle}${suffix}`)
          })
          .catch((err: unknown) => {
            if (getUiState().sid !== r.session_id) {
              return
            }

            const message = err instanceof Error ? err.message : String(err)
            sys(`warning: failed to set session title: ${message}`)
          })
      }

      signalFreshSessionBoundary(previousSid, r.session_id, onFreshSessionStarted)

      return r.session_id
    },
    [closeSession, colsRef, onFreshSessionStarted, panel, resetSession, rpc, setHistoryItems, setSessionStartedAt, supersedeColdHydration, sys]
  )

  const newSession = useCallback(
    (msg?: string, title?: string) => startNewSession(msg, title, false),
    [startNewSession]
  )

  const newLiveSession = useCallback(
    async (msg = 'new live session started', title?: string) => {
      await supersedeColdHydration()
      patchOverlayState({ sessions: false })

      return startNewSession(msg, title, true)
    },
    [startNewSession, supersedeColdHydration]
  )

  const activateLiveSession = useCallback(
    async (id: string) => {
      await supersedeColdHydration()
      patchOverlayState({ sessions: false })
      patchUiState({ status: 'switching session…' })
      // The card belongs to the session being left; the activated one answers with its own.
      clearConnectionOperation()

      return gw.request<SessionActivateResponse>('session.activate', { session_id: id })
        .then(raw => {
          const r = asRpcResult<SessionActivateResponse>(raw)

          if (!r) {
            sys('error: invalid response: session.activate')

            patchUiState({ status: 'ready' })
            return null
          }

          const info = r.info ?? null
          // Agent-less (lazy) activations answer with `_fallback_session_info`, which
          // has no stored_session_id; the durable id is the response's session_key.
          const storedSid = r.session_key || r.session_id
          const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

          resetSession()
          setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())
          const transcriptMsgs = toTranscriptMessages(r.messages)
          const transcript = [...transcriptMsgs, ...liveSessionInflightMessages(r.inflight, transcriptMsgs)]
          setHistoryItems(info ? [introMsg(info), ...transcript] : transcript)
          const durableKey = (r as any).session_key ?? (r as any).resumed ?? r.session_id
          writeActiveSessionFile(durableKey)
          patchUiState({
            busy: running,
            info,
            sessionKey: durableKey,
            sid: r.session_id,
            status: statusFromLiveSession(r.status, running),
            storedSid,
            usage: usageFrom(info)
          })
          hydrateLiveSessionInflight(r.inflight)

          if (r.pending_connection) {
            applyConnectionRequest(r.pending_connection)
          }

          cancelResumeScrollRef.current?.()
          cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)
          return r.session_id
        })
        .catch((e: Error) => {
          sys(`error: ${e.message}`)
          patchUiState({ status: 'ready' })
          return null
        })
    },
    [gw, resetSession, scrollRef, setHistoryItems, setSessionStartedAt, supersedeColdHydration, sys]
  )

  const fetchOlderBacklog = useCallback(async () => {
    const currentSid = getUiState().sid

    if (!currentSid || !viewportMeta?.has_more_before || isFetchingBacklogRef.current) {
      return
    }

    isFetchingBacklogRef.current = true

    try {
      const res = await rpc<SessionHistoryResponse>('session.history', {
        before_index: viewportMeta.start_index,
        limit: 50,
        session_id: currentSid
      })

      if (res && res.messages && res.messages.length > 0 && getUiState().sid === currentSid) {
        const olderMsgs = toTranscriptMessages(res.messages)
        setHistoryItems(prev => {
          const hasIntro = prev.length > 0 && prev[0]?.kind === 'intro'

          if (hasIntro) {
            return [prev[0]!, ...olderMsgs, ...prev.slice(1)]
          }

          return [...olderMsgs, ...prev]
        })
        setViewportMeta({
          end_index: viewportMeta.end_index,
          has_more_before: Boolean(res.has_more_before),
          start_index: typeof res.start_index === 'number' ? res.start_index : 0,
          total: viewportMeta.total
        })
      } else if (res && (!res.messages || res.messages.length === 0)) {
        setViewportMeta(prev => (prev ? { ...prev, has_more_before: false } : null))
      }
    } catch {
      // Non-fatal; can retry on next scroll up
    } finally {
      isFetchingBacklogRef.current = false
    }
  }, [rpc, setHistoryItems, viewportMeta])

  const resumeById = useCallback(
    async (
      id: string,
      targetRecoveryRef?: { current: string | null },
      retryAttempt = 0,
      options?: {
        gapReason?: string
        mode?: "transport-recovery" | "transport-gap-recovery" | "cold-resume"
        durableKey?: string
        historyRetryAttempt?: number
        retryBarrier?: { attemptId: string; sid: string }
      }
    ): Promise<void> => {
      // Transport invalidation clears the gateway queue independently of the React ref.
      // Only gateway.ready may start a fresh cold chain after that ownership is lost.
      const retryBarrierIsCurrent = () => {
        const barrier = options?.retryBarrier
        return !barrier || (activeColdBarrierRef.current === barrier && gw.hasEventBarrier(barrier.sid, barrier.attemptId))
      }
      if (!retryBarrierIsCurrent()) return
      await supersedeColdHydration(options?.retryBarrier)
      if (!retryBarrierIsCurrent()) return
      patchOverlayState({ sessions: false })
      patchUiState({ status: 'resuming…' })
      const attemptId = randomUUID()
      const historyRetryAttempt = options?.historyRetryAttempt ?? 0
      const abandonRetryBarrier = () => {
        const barrier = options?.retryBarrier
        if (!barrier || activeColdBarrierRef.current !== barrier) return
        clearActiveColdBarrier(barrier.attemptId, barrier.sid)
        gw.cancelEventBarrier(barrier.sid, barrier.attemptId)
      }
      resumeAttemptRef.current = attemptId
      const generation = INLINE_MODE && DASHBOARD_TUI_MODE ? attemptId : null
      replayGeneration.current = generation
      let replayBegun = false

      const replayState: ReplayGenerationState | null = generation
        ? { attemptId, generation, aborted: false, ended: false }
        : null
      currentReplayStateRef.current = replayState
      activeReplayBoundaryRef.current = replayState

      const startReplay = () => {
        if (generation && !replayBegun && resumeAttemptRef.current === attemptId) {
          replayBegun = true
          stdout.write(`\x1b]777;hermes-replay;begin;${generation}\x07`)
        }
      }

      const abortReplay = () => {
        if (generation) {
          abortReplayOnce(generation)
        }
      }

      return rpc<SetupStatusResponse>('setup.status', {}).then(setup => {
        if (resumeAttemptRef.current !== attemptId || !retryBarrierIsCurrent()) return
        if (setup?.provider_configured === false) {
          abandonRetryBarrier()
          abortReplay()
          panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections())
          patchUiState({ status: 'setup required' })

          return
        }

        const previousSid = getUiState().sid

        const isColdIncomplete = Boolean(
          coldHydrationIncompleteRef.current && (
            coldHydrationIncompleteRef.current === id ||
            coldHydrationIncompleteRef.current === previousSid ||
            coldHydrationIncompleteRef.current === (options as any)?.durableKey
          )
        )
        const isTransportRecovery = options?.mode === 'transport-recovery' && !isColdIncomplete
        const isGapRecovery = options?.mode === 'transport-gap-recovery'
        const resumeParams: Record<string, unknown> = { cols: colsRef.current, session_id: id }
        if (isTransportRecovery || (!isGapRecovery && INLINE_MODE)) {
          resumeParams.omit_messages = true
        }

        return gw.request<SessionResumeResult & { viewport?: SessionViewportMeta }>('session.resume', resumeParams)
          .then(raw => {
            if (resumeAttemptRef.current !== attemptId || !retryBarrierIsCurrent()) return
            const r = asRpcResult<SessionResumeResult & { viewport?: SessionViewportMeta }>(raw)

            if (!r) {
              abandonRetryBarrier()
              abortReplay()
              sys('error: invalid response: session.resume')

              return patchUiState({ status: 'ready' })
            }

            const isColdHydration = !isTransportRecovery && !isGapRecovery && INLINE_MODE

            // For non-cold paths, begin replay boundary immediately;
            // for cold hydration, startReplay() is deferred until after static lease acquisition.
            if (!isColdHydration) {
              startReplay()
            }

            const storedSid = r.info?.stored_session_id || r.stored_session_id || r.resumed || id
            const info = r.info ? { ...r.info, stored_session_id: storedSid } : null
            const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')
            const durableKey = (r as any).resumed ?? (r as any).session_key ?? (r as any).stored_session_id ?? r.session_id

            if (isColdHydration) {
              const durableKeyStr = String(durableKey || r.session_id || id)
              coldHydrationIncompleteRef.current = durableKeyStr

              const previous = activeColdBarrierRef.current
              const continuingBarrier = previous && previous === options?.retryBarrier && previous.sid === r.session_id
              if (previous && !continuingBarrier) {
                gw.cancelEventBarrier(previous.sid, previous.attemptId)
                activeColdBarrierRef.current = null
              }
              // One event owner spans the retry chain; each physical replay still has a fresh generation.
              const barrier = continuingBarrier ? previous : { attemptId, sid: r.session_id }
              if (!continuingBarrier) gw.activateEventBarrier(r.session_id, barrier.attemptId)
              activeColdBarrierRef.current = barrier

              let currentPhase: ColdTransactionPhase = 'hydrating'
              let settled = false
              let resolveSettlement!: (result: ColdSettlement) => void
              const settlementPromise = new Promise<ColdSettlement>(resolve => {
                resolveSettlement = resolve
              })

              const complete = (result: ColdSettlement) => {
                if (settled) return
                settled = true
                currentPhase = 'settled'
                resolveSettlement(result)
              }

              const tx: ActiveColdOutputTransaction = {
                attemptId,
                barrierOwner: barrier.attemptId,
                sid: r.session_id,
                durableKey: durableKeyStr,
                lease: null,
                boundaryGeneration: generation,
                staticOutputStarted: false,
                settlementPromise,
                complete,
                replayState,
                get phase() {
                  return currentPhase
                },
                setPhase: (phase: ColdTransactionPhase) => {
                  if (!settled) {
                    currentPhase = phase
                  }
                }
              }
              activeColdOutputRef.current = tx

              const acquisitionPromise = acquireMainScreenStaticOutput(stdout)
              tx.acquisitionPromise = acquisitionPromise

              void (async () => {
                let lease: MainScreenStaticOutputLease
                try {
                  lease = await acquisitionPromise
                  tx.lease = lease
                } catch (err) {
                  clearActiveColdBarrier(tx.barrierOwner, r.session_id)
                  gw.cancelEventBarrier(r.session_id, tx.barrierOwner)
                  console.error('Failed to acquire main-screen static output lease:', err)
                  abortReplayOnce(generation)
                  sys(`error: failed to acquire static output lease: ${err instanceof Error ? err.message : String(err)}`)
                  patchUiState({ status: 'ready' })
                  if (activeColdOutputRef.current === tx) {
                    activeColdOutputRef.current = null
                  }
                  tx.complete({ ok: true })
                  return
                }

                if (resumeAttemptRef.current !== attemptId || tx.phase === 'cancelling' || !gw.hasEventBarrier(tx.sid, tx.barrierOwner)) {
                  // Cleanup records failures on settlementPromise for every waiting successor.
                  await settleCancelledColdTx(tx).catch(() => {})
                  return
                }

                startReplay()

                resetSession()
                setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())
                writeActiveSessionFile(durableKey)
                patchUiState({
                  busy: running,
                  info,
                  sessionKey: durableKey,
                  sid: r.session_id,
                  status: statusFromLiveSession(r.status ?? undefined, running),
                  storedSid,
                  usage: usageFrom(info)
                })

                const coldOutput: ColdHistoryOutput = {
                  getColumns: () => colsRef.current,
                  beginStaticAppendSurface: async () => {
                    tx.staticOutputStarted = true
                    await lease.beginStaticAppendSurface()
                  },
                  write: async data => {
                    tx.staticOutputStarted = true
                    await lease.write(data)
                  }
                }

                try {
                  const hydration = await performColdHistoryHydration({
                    gateway: gw,
                    sessionId: r.session_id,
                    theme: getUiState().theme,
                    info,
                    maxMounted,
                    output: coldOutput,
                    isCancelled: () => resumeAttemptRef.current !== attemptId || !gw.hasEventBarrier(tx.sid, tx.barrierOwner)
                  })

                  if (resumeAttemptRef.current !== attemptId || !gw.hasEventBarrier(tx.sid, tx.barrierOwner)) {
                    await settleCancelledColdTx(tx)
                    return
                  }

                  tx.setPhase('commit-pending')
                  tx.appendedToScrollback = hydration.appendedToScrollback
                  const resumed = [...hydration.initialLiveMessages, ...liveSessionInflightMessages(r.inflight, hydration.initialLiveMessages)]
                  // 1. Commit live tail to React state first
                  setHistoryItems(resumed)
                  setViewportMeta(r.viewport ?? null)
                  // 2. Queue commit acknowledgement: useLayoutEffect releases lease and barrier after React commits this frame
                  pendingColdCommitRef.current = tx
                  setColdCommitGeneration(attemptId)
                } catch (err) {
                  const failure = classifyResumeFailure(err)
                  const retryHistory = !(err instanceof ColdHydrationCancelledError) &&
                    resumeAttemptRef.current === attemptId && gw.hasEventBarrier(tx.sid, tx.barrierOwner) &&
                    failure.kind !== 'transport' && failure.kind !== 'identity' && historyRetryAttempt < 3
                  try {
                    await settleCancelledColdTx(tx, retryHistory)
                  } catch {
                    // Physical failure is quarantined; only a fresh PTY can recover ownership.
                    return
                  }
                  if (err instanceof ColdHydrationCancelledError || resumeAttemptRef.current !== attemptId) return
                  if (failure.kind === 'transport') {
                    patchUiState({ status: 'disconnected' })
                    return // gateway.ready consumes the retained incomplete marker.
                  }
                  if (retryHistory) {
                    patchUiState({ status: 'retrying history…' })
                    coldRetryTimerRef.current = setTimeout(() => {
                      coldRetryTimerRef.current = null
                      if (resumeAttemptRef.current !== attemptId) return
                      void resumeById(id, targetRecoveryRef, 0, {
                        mode: 'cold-resume', durableKey: durableKeyStr,
                        historyRetryAttempt: historyRetryAttempt + 1, retryBarrier: barrier
                      }).catch(error => {
                        sys(`error: ${error instanceof Error ? error.message : String(error)}`)
                      })
                    }, Math.min(1000, 250 * 2 ** historyRetryAttempt))
                  } else {
                    sys(`error: history recovery failed: ${err instanceof Error ? err.message : String(err)}`)
                    patchUiState({ status: 'history incomplete' })
                  }
                }
              })()
            } else if (!isTransportRecovery) {
              resetSession()
              setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())

              const transcriptMsgs = toTranscriptMessages(r.messages ?? [])
              const resumed = [...transcriptMsgs, ...liveSessionInflightMessages(r.inflight, transcriptMsgs)]

              setHistoryItems(info ? [introMsg(info), ...resumed] : resumed)
              setViewportMeta(r.viewport ?? null)
              setReplayCommitted(generation ? { generation, replaceFrame: true } : null)
              coldHydrationIncompleteRef.current = null
            } else {
              // Transport recovery fast path: historyItems are already preserved!
              // Hydrate any newly arrived inflight state
              if (r.inflight) {
                setHistoryItems(prev => {
                  const inflightMsgs = liveSessionInflightMessages(r.inflight, prev)
                  return inflightMsgs.length > 0 ? [...prev, ...inflightMsgs] : prev
                })
              }
              setReplayCommitted(generation ? { generation, replaceFrame: true } : null)
            }
            writeActiveSessionFile(durableKey)
            patchUiState({
              busy: running,
              info,
              sessionKey: durableKey,
              sid: r.session_id,
              status: statusFromLiveSession(r.status ?? undefined, running),
              storedSid,
              usage: usageFrom(info)
            })
            const activeRecoveryRef = targetRecoveryRef ?? recoverSessionKeyRef ?? activeRecoveryTargetRef
            if (activeRecoveryRef) {
              activeRecoveryRef.current = null
            }
            if (opts.recoverSidRef && opts.recoverSidRef !== activeRecoveryRef) {
              opts.recoverSidRef.current = null
            }
            if (opts.recoverSessionKeyRef && opts.recoverSessionKeyRef !== activeRecoveryRef) {
              opts.recoverSessionKeyRef.current = null
            }
            hydrateLiveSessionInflight(r.inflight)

            if (r.pending_connection) {
              applyConnectionRequest(r.pending_connection)
            } else {
              clearConnectionOperation()
            }

            cancelResumeScrollRef.current?.()
            cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)

            if (previousSid && previousSid !== r.session_id) {
              gw?.retireSession(previousSid)
              void closeSession(previousSid)
            }
          })
      }).catch((e: unknown) => {
        if (resumeAttemptRef.current !== attemptId || !retryBarrierIsCurrent()) return
        const failure = classifyResumeFailure(e)
        if (failure.kind === 'retry-same') {
          const isSettling = failure.reason === 'disconnect_interrupt_settling'
          const maxRetries = isSettling ? 15 : 4
          if (retryAttempt < maxRetries) {
            const delay = isSettling ? 1000 : Math.min(2000, 250 * Math.pow(2, retryAttempt))
            setTimeout(() => {
              if (resumeAttemptRef.current === attemptId) {
                resumeById(id, targetRecoveryRef, retryAttempt + 1, options)
              }
            }, delay)
            return
          }
        }
        abandonRetryBarrier()
        if (failure.kind === 'identity') {
          const fileFallback = readActiveSessionFile()
          if (fileFallback && fileFallback !== id) {
            return resumeById(fileFallback, targetRecoveryRef, 0, { mode: 'cold-resume' })
          }
        }
        if (failure.kind === 'transport') {
          // Keep recovery target intact across transient transport disconnects
          patchUiState({ status: 'disconnected' })
          return
        }
        abortReplay()
        sys(`error: ${e instanceof Error ? e.message : String(e)}`)
        patchUiState({ status: 'ready' })
      })
    },
    [clearActiveColdBarrier, closeSession, colsRef, gw, opts.recoverSessionKeyRef, opts.recoverSidRef, panel, recoverSessionKeyRef, resetSession, rpc, scrollRef, setHistoryItems, setSessionStartedAt, supersedeColdHydration, sys]
  )

  const guardBusySessionSwitch = useCallback(
    (what = 'switch sessions') => {
      if (!getUiState().busy) {
        return false
      }

      sys(`interrupt the current turn before trying to ${what}`)

      return true
    },
    [sys]
  )

  return useMemo(
    () => ({
      activateLiveSession,
      closeSession,
      coldHydrationIncompleteRef,
      fetchOlderBacklog,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      trimLastExchange: trimTail,
      viewportMeta
    }),
    [
      activateLiveSession,
      closeSession,
      coldHydrationIncompleteRef,
      fetchOlderBacklog,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      trimTail,
      viewportMeta
    ]
  )
}
