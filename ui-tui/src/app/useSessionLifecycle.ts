import { randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'

import type { ScrollBoxHandle } from '@hermes/ink'
import {
  acquireMainScreenStaticOutput,
  evictInkCaches,
  type MainScreenStaticOutputLease,
  writeAfterRender
} from '@hermes/ink'
import type { InflightTurn, SessionResumeResult, Usage } from '@hermes/shared/gateway-events'
import type {
  SessionActivateResponse,
  SessionCloseResponse,
  SessionCreateResponse,
  SessionDetachResponse,
  SessionHistoryResponse,
  SessionInfo,
  SessionTitleResponse,
  SessionViewportMeta,
  SetupStatusResponse
} from '@hermes/shared/gateway-types'
import { type RefObject, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { DASHBOARD_TUI_MODE, INLINE_MODE, STARTUP_WORKSPACE_CWD } from '../config/env.js'
import { buildSetupRequiredSections, setupRequiredTitle } from '../content/setup.js'
import { introMsg, toTranscriptMessages } from '../domain/messages.js'
import { ZERO } from '../domain/usage.js'
import { type GatewayClient } from '../gatewayClient.js'
import { t } from '../i18n/runtime.js'
import { launchRouteServes, localCreationOptions } from '../launchOptions.js'
import { migratePendingInputs } from '../lib/pendingInputs.js'
import { asRpcResult } from '../lib/rpc.js'
import type { Msg, PanelSection } from '../types.js'

import { noteBackgroundHandoff } from './backgroundSessionNotices.js'
import { type ColdHistoryOutput, ColdHydrationCancelledError, performColdHistoryHydration } from './coldHistoryHydration.js'
import { applyConnectionRequest, clearConnectionOperation } from './connectionOperationStore.js'
import { activeRecoveryTargetRef } from './gatewayRecovery.js'
import type { ComposerActions, GatewayRpc, StateSetter } from './interfaces.js'
import { patchOverlayState } from './overlayStore.js'
import { classifyResumeFailure } from './sessionRecovery.js'
import { scheduleResumeScrollToBottom } from './sessionResumeView.js'
import { renameCanonicalSession } from './slash/canonicalSessionCommands.js'
import { captureDestination } from './submissionDestination.js'
import { turnController } from './turnController.js'
import { patchTurnState } from './turnStore.js'
import { getUiState, patchUiState } from './uiStore.js'
import { describeCredentialWarning } from './userMessages.js'

export { refreshSessionView, scheduleResumeScrollToBottom } from './sessionResumeView.js'

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
  if (!err) {return false}
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
  settleTransaction: (result: ColdSettlement) => void
  phase: ColdTransactionPhase
  setPhase: (phase: ColdTransactionPhase) => void
  appendedToScrollback?: boolean
  cleanupPromise?: Promise<void>
  replayState?: ReplayGenerationState | null
}

type PendingColdCommit = ActiveColdOutputTransaction & { settle: (error?: unknown) => void }
type ResumeResult = SessionResumeResult & { viewport?: SessionViewportMeta }

interface ColdBarrier {
  attemptId: string
  sid: string
}

interface ResumeOptions {
  durableKey?: string
  gapReason?: string
  historyRetryAttempt?: number
  mode?: 'transport-recovery' | 'transport-gap-recovery' | 'cold-resume'
  retryBarrier?: ColdBarrier
}

function resumeHistoryMode(
  canonical: boolean, options: ResumeOptions | undefined, incomplete: string | null, id: string, previousSid: string | null
) {
  const needsColdRetry = Boolean(incomplete && [id, previousSid, options?.durableKey].includes(incomplete))
  const isTransportRecovery = !canonical && options?.mode === 'transport-recovery' && !needsColdRetry
  const isColdHydration = !canonical && !isTransportRecovery && options?.mode !== 'transport-gap-recovery' && INLINE_MODE

  return { isColdHydration, isTransportRecovery }
}

function resumedSessionInfo(r: ResumeResult, requestedId: string) {
  const storedSid = r.session_key || r.info?.stored_session_id || r.stored_session_id || r.resumed || requestedId
  const info = r.info ? { ...r.info, stored_session_id: storedSid } : null

  return { storedSid, info }
}

function showResumedSession(r: ResumeResult, storedSid: string, info: SessionInfo | null) {
  const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

  patchUiState({ busy: running, gatewayConnected: true, info, sessionKey: storedSid,
    sid: r.session_id, storedSid, status: statusFromLiveSession(r.status ?? undefined, running), usage: usageFrom(info) })
}

const usageFrom = (info: null | SessionInfo): Usage => (info?.usage ? { ...ZERO, ...info.usage } : ZERO)

const createdSessionStoredId = (result: SessionCreateResponse): string =>
  result.stored_session_id || result.info?.stored_session_id || result.session_id

const statusFromLiveSession = (status?: string, running = false) => {
  if (status === 'waiting') {
    return t('session.status.waitingForInput')
  }

  if (status === 'starting') {
    return t('session.status.startingAgent')
  }

  return running || status === 'working' ? 'running…' : 'ready'
}

/** Session-start notices, in their original order: credential, config, then the caller's message. */
const reportNewSessionNotices = (info: null | SessionInfo, msg: string | undefined, sys: (text: string) => void) => {
  if (info?.credential_warning) {
    sys(`warning: ${describeCredentialWarning(info.credential_warning)}`)
  }

  if (info?.config_warning) {
    sys(`warning: ${info.config_warning}`)
  }

  if (msg) {
    sys(msg)
  }
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

type CanonicalEndReason = 'tui_close' | 'tui_shutdown'

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

/** The subscription a view hands back when it leaves *sid*. A session left mid-turn while it stays
 * live (`/new`, the switcher) keeps working in the background and keeps this view's subscription:
 * the owner asks its questions (clarify, sudo, secret, vault) only while a viewer is attached and
 * settles them as unanswered otherwise, so they must stay routed here until the user answers. */
function releasedOnLeave(sid: null | string, held: Map<string, string>, staysLive: boolean): string | undefined {
  if (!sid) {
    return undefined
  }

  if (staysLive && getUiState().busy) {
    noteBackgroundHandoff(sid)

    return undefined
  }

  return held.get(sid)
}

// Failed physical cleanup remains quarantined across React remounts.
const terminalQuarantine = new WeakMap<NodeJS.WriteStream, Promise<ColdSettlement>>()

function resumeDestination(current: ReturnType<typeof captureDestination>, id: string) {
  return current.sid === id || current.storedSid === id ? current : { ...current, sid: id, storedSid: id }
}

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

  // Plugin on_session_finalize text comes back on the close result and is shown as system lines (never a
  // model turn). `deferMessages`: the caller resets the transcript next and shows them itself afterwards.
  // A canonical session is the gateway's: the view detaches, it never closes the owner's session.
  const closeSession = useCallback(
    async (targetSid?: null | string, deferMessages = false) => {
      if (targetSid && !gw.isCanonical) {gw.retireSession(targetSid)}

      const closed =
        targetSid && !gw.isCanonical ? await rpc<SessionCloseResponse>('session.close', { session_id: targetSid }) : null

      if (!deferMessages) {
        closed?.messages?.forEach(message => sys(message))
      }

      return closed
    },
    [gw, rpc, sys]
  )

  const coldRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const coldRetryWakeRef = useRef<(() => void) | null>(null)
  const resumeAttemptRef = useRef<string | null>(null)
  const replayGeneration = useRef<string | null>(null)
  const activeReplayBoundaryRef = useRef<ReplayGenerationState | null>(null)
  const currentReplayStateRef = useRef<ReplayGenerationState | null>(null)
  const [replayCommitted, setReplayCommitted] = useState<{ generation: string; replaceFrame: boolean } | null>(null)
  const activeColdOutputRef = useRef<ActiveColdOutputTransaction | null>(null)
  const pendingColdCommitRef = useRef<PendingColdCommit | null>(null)
  const activeColdBarrierRef = useRef<ColdBarrier | null>(null)
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
    async (tx: ActiveColdOutputTransaction, preserveBarrier = false, commitError?: unknown) => {
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
        tx.settleTransaction({ ok: true })
        if ('settle' in tx) {(tx as PendingColdCommit).settle(commitError)}
        if (activeColdOutputRef.current === tx) {
          activeColdOutputRef.current = null
        }
      } catch (cleanupErr) {
        clearActiveColdBarrier(tx.barrierOwner, tx.sid)
        gw.cancelEventBarrier(tx.sid, tx.barrierOwner)
        const terminalDisposed = isTerminalDisposed(cleanupErr)
        terminalQuarantine.set(stdout, tx.settlementPromise)
        tx.settleTransaction({ ok: false, error: cleanupErr, terminalDisposed })
        if ('settle' in tx) {(tx as PendingColdCommit).settle(cleanupErr)}
        // Failed cleanup: do NOT clear activeColdOutputRef.current so successors stay blocked!
        throw cleanupErr
      }
    },
    [abortReplayOnce, clearActiveColdBarrier, gw, stdout]
  )

  const supersedeColdHydration = useCallback(async (preserveBarrier?: ColdBarrier) => {
    if (coldRetryTimerRef.current) {
      clearTimeout(coldRetryTimerRef.current)
      coldRetryTimerRef.current = null
      coldRetryWakeRef.current?.()
      coldRetryWakeRef.current = null
    }
    resumeAttemptRef.current = null
    const quarantine = terminalQuarantine.get(stdout)
    if (quarantine) {
      const result = await quarantine
      if (!result.ok) {
        throw new Error(`Terminal recovery required: ${result.error instanceof Error ? result.error.message : String(result.error)}`, { cause: result.error })
      }
      if (terminalQuarantine.get(stdout) === quarantine) {terminalQuarantine.delete(stdout)}
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
    if (!pending) {return}
    pendingColdCommitRef.current = null

    if (pending.attemptId !== resumeAttemptRef.current || !gw.hasEventBarrier(pending.sid, pending.barrierOwner)) {
      void settleCancelledColdTx(pending, false, new ColdHydrationCancelledError()).catch(() => {})
      return
    }

    pending.setPhase('finalizing')

    void (async () => {
      try {
        const lease = pending.lease
        if (!lease) {throw new Error('Cold commit cannot finalize without an acquired lease')}
        if (pending.appendedToScrollback) {
          lease.prepareAppendHandoff()
        }
        await lease.release()
      } catch (err) {
        console.error('Lease release failed in finalizeColdCommit:', err)
        // Fail-closed invariant: if lease release/handoff fails, abort and do not release event barrier or clear incomplete marker!
        await settleCancelledColdTx(pending, false, err).catch(() => {})
        return
      }

      if (pending.attemptId !== resumeAttemptRef.current || !gw.hasEventBarrier(pending.sid, pending.barrierOwner)) {
        clearActiveColdBarrier(pending.barrierOwner, pending.sid)
        gw.cancelEventBarrier(pending.sid, pending.barrierOwner)
        abortReplayOnce(pending)
        if (activeColdOutputRef.current === pending) {
          activeColdOutputRef.current = null
        }
        pending.settleTransaction({ ok: true })
        pending.settle(new ColdHydrationCancelledError())
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
      pending.settleTransaction({ ok: true })
      pending.settle()
    })()
  }, [abortReplayOnce, clearActiveColdBarrier, coldCommitGeneration, coldHydrationIncompleteRef, gw, settleCancelledColdTx])

  useEffect(() => {
    return () => {
      const tx = activeColdOutputRef.current
      // Begin cancellation before publishing the wait, so this owner never awaits itself.
      const cleanup = supersedeColdHydration()
      if (tx) {terminalQuarantine.set(stdout, tx.settlementPromise)}
      void cleanup.then(() => {
        if (tx && terminalQuarantine.get(stdout) === tx.settlementPromise) {terminalQuarantine.delete(stdout)}
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

  const [viewportMeta, setViewportMeta] = useState<SessionViewportMeta | null>(null)
  const isFetchingBacklogRef = useRef(false)

  const cancelResumeScrollRef = useRef<null | (() => void)>(null)
  const attachmentFlight = useRef(0)
  const pendingAttachments = useRef(new Set<number>())
  const canonicalSubscriptions = useRef(new Map<string, string>())
  const canonicalDetachFlights = useRef(new Map<string, Promise<void>>())
  const staleAttachments = useRef(new Map<string, { session_id: string; subscription_id: string }>())

  // `endReason`: main's `session.close` stamp for a view this TUI closes for good (`tui_close` on /new or
  // a resume switch, `tui_shutdown` on exit); the owner applies it once no other viewer remains.
  // The one path that sends `session.detach`: it serializes detaches per session (flight tracking) and
  // forgets the subscription only while it is still the one this view holds. Resolves the owner's
  // reply, or null when nothing was sent; a failed RPC resolves null unless `surfaceErrors` asks for
  // the rejection (the switcher's close reports it; background detaches never do). `onlyIfHeld`: when
  // its turn in the flight comes, send only if this view still holds that subscription (a close that
  // queued behind another close of the same row has nothing left to end).
  const detachCanonical = useCallback(
    (
      sessionId?: null | string,
      subscriptionId?: null | string,
      endReason?: CanonicalEndReason,
      { onlyIfHeld = false, surfaceErrors = false }: { onlyIfHeld?: boolean; surfaceErrors?: boolean } = {}
    ): Promise<null | SessionDetachResponse> => {
      if (!gw.isCanonical || !sessionId || !subscriptionId) {
        return Promise.resolve(null)
      }

      const detach = (): Promise<null | SessionDetachResponse> =>
        onlyIfHeld && canonicalSubscriptions.current.get(sessionId) !== subscriptionId
          ? Promise.resolve(null)
          : rpc<SessionDetachResponse>('session.detach', {
              session_id: sessionId,
              subscription_id: subscriptionId,
              ...(endReason ? { end_reason: endReason } : {})
            }).then(raw => {
              const result = asRpcResult<SessionDetachResponse>(raw)

              if (result?.detached && canonicalSubscriptions.current.get(sessionId) === subscriptionId) {
                canonicalSubscriptions.current.delete(sessionId)
              }

              return result
            })

      const previous = canonicalDetachFlights.current.get(sessionId)
      const run = previous ? previous.then(detach) : detach()

      const pending: Promise<void> = run.then(
        () => undefined,
        () => undefined
      )

      canonicalDetachFlights.current.set(sessionId, pending)
      void pending.then(() => {
        if (canonicalDetachFlights.current.get(sessionId) === pending) {
          canonicalDetachFlights.current.delete(sessionId)
        }
      })

      return surfaceErrors ? run : run.catch(() => null)
    },
    [gw.isCanonical, rpc]
  )

  // Exit (`/quit`, Ctrl+D): main's sidecar stamped every session it held `tui_shutdown` at shutdown.
  const closeForExit = useCallback(async () => {
    const open = [...canonicalSubscriptions.current.entries()]

    await Promise.all(open.map(([sessionId, subscriptionId]) => detachCanonical(sessionId, subscriptionId, 'tui_shutdown')))
  }, [detachCanonical])

  // The Ink session switcher's `^d` close of a live row. On the legacy transport it is main's
  // `session.close`; on a canonical gateway that method is not in the contract (-32601), so the view
  // can only drop its own hold with main's `tui_close` stamp: the owner ends the idle local session
  // once the last viewer leaves, which is what takes the row out of the live feed.
  const closeLiveView = useCallback(
    async (targetSid: string): Promise<null | SessionCloseResponse> => {
      if (!gw.isCanonical) {
        const closed = await rpc<SessionCloseResponse>('session.close', { session_id: targetSid })

        closed?.messages?.forEach(message => sys(message))

        return closed
      }

      // An in-flight detach for this row (a resume switch, /new) wins: this view no longer holds it.
      await canonicalDetachFlights.current.get(targetSid)

      const subscriptionId = canonicalSubscriptions.current.get(targetSid)

      if (!subscriptionId) {
        // A live row this view holds nothing for is not ours to end (another viewer's session, or an
        // ownerless row the gateway kept): nothing is sent, and the caller reports no close at all.
        return null
      }

      const result = await detachCanonical(targetSid, subscriptionId, 'tui_close', {
        onlyIfHeld: true,
        surfaceErrors: true
      })

      if (!result?.detached) {
        // The subscription was already stale: main had dropped this view's hold, so nothing was closed.
        return null
      }

      return { closed: true }
    },
    [detachCanonical, gw.isCanonical, rpc, sys]
  )

  const disposeStaleAttachments = useCallback(() => {
    // In-flight attaches can share a token before either result is adopted.
    // Decide disposal only after their handlers have adopted (or refused) it.
    if (pendingAttachments.current.size) {
      return
    }

    const stale = [...staleAttachments.current.values()]
    staleAttachments.current.clear()

    for (const result of stale) {
      if (canonicalSubscriptions.current.get(result.session_id) !== result.subscription_id) {
        detachCanonical(result.session_id, result.subscription_id)
      }
    }
  }, [detachCanonical])

  const finishAttachment = useCallback(
    (flight: number) => {
      pendingAttachments.current.delete(flight)
      disposeStaleAttachments()
    },
    [disposeStaleAttachments]
  )

  const discardStaleAttachment = useCallback(
    (result?: null | { session_id: string; subscription_id?: string }) => {
      if (result?.subscription_id) {
        staleAttachments.current.set(JSON.stringify([result.session_id, result.subscription_id]), {
          session_id: result.session_id,
          subscription_id: result.subscription_id
        })
      }

      disposeStaleAttachments()
    },
    [disposeStaleAttachments]
  )

  const adoptAttachment = useCallback(
    (
      result: { session_id: string; subscription_id?: string },
      previousSid?: null | string,
      previousSubscription?: null | string,
      closePrevious = false
    ) => {
      if (!gw.isCanonical || !result.subscription_id) {
        return
      }

      canonicalSubscriptions.current.set(result.session_id, result.subscription_id)

      if (
        previousSid &&
        previousSubscription &&
        (previousSid !== result.session_id || previousSubscription !== result.subscription_id)
      ) {
        detachCanonical(previousSid, previousSubscription, closePrevious && previousSid !== result.session_id ? 'tui_close' : undefined)
      }
    },
    [detachCanonical, gw.isCanonical]
  )

  const resetSession = useCallback(() => {
    cancelResumeScrollRef.current?.()
    cancelResumeScrollRef.current = null
    turnController.fullReset()
    setVoiceRecording(false)
    setVoiceProcessing(false)
    patchUiState({ bgTasks: new Set(), info: null, sid: null, storedSid: null, sessionKey: null, usage: ZERO })
    setViewportMeta(null)
    isFetchingBacklogRef.current = false
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
      attachmentFlight.current++
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
      const flight = ++attachmentFlight.current
      const previousSid = getUiState().sid
      const previousSubscription = releasedOnLeave(previousSid, canonicalSubscriptions.current, keepCurrent)
      pendingAttachments.current.add(flight)

      try {
        await supersedeColdHydration()

        if (flight !== attachmentFlight.current) {return null}
        const setup = await rpc<SetupStatusResponse>('setup.status', {})

        const ready =
          setup?.provider_configured !== false || (gw.isCanonical && (await launchRouteServes(rpc)))

        if (flight !== attachmentFlight.current) {
          return null
        }

        if (!ready) {
          panel(setupRequiredTitle(), buildSetupRequiredSections())
          patchUiState({ status: t('session.status.setupRequired') })

          return null
        }

        const closed = keepCurrent ? null : await closeSession(previousSid, true)

        if (flight !== attachmentFlight.current) {
          return null
        }

        // HERMES_TUI_CWD is the dashboard-picked workspace: an explicit cwd on
        // session.create on both transports, so /new stays in that workspace.
        const workspaceCwd = STARTUP_WORKSPACE_CWD ? { cwd: STARTUP_WORKSPACE_CWD } : {}

        const r = await rpc<SessionCreateResponse>(
          'session.create',
          gw.isCanonical
            ? { request_id: randomUUID(), ...localCreationOptions(), ...workspaceCwd }
            : { cols: colsRef.current, ...workspaceCwd }
        )

        if (flight !== attachmentFlight.current) {
          discardStaleAttachment(r)

          return null
        }

        if (!r) {
          patchUiState({ status: 'ready' })

          return null
        }

        // The durable id lives on the create result; the lazy-create `info` does
        // not carry it, and session.resume / the exit epilogue need the stored id.
        const storedSid = createdSessionStoredId(r)
        const info = r.info ? { ...r.info, stored_session_id: storedSid } : null
        const requestedTitle = title?.trim() ?? ''

        resetSession()
        setSessionStartedAt(Date.now())

        writeActiveSessionFile(storedSid)
        patchUiState({
          sessionKey: storedSid,
          gatewayConnected: true,
          info,
          sid: r.session_id,
          status: gw.isCanonical || info?.version ? 'ready' : t('session.status.startingAgent'),
          storedSid,
          usage: usageFrom(info)
        })

        if (info) {
          setHistoryItems([introMsg(info)])
        }

        reportNewSessionNotices(info, msg, sys)

        // After the reset above, so the closed session's plugin messages stay visible.
        closed?.messages?.forEach(message => sys(message))

        if (requestedTitle) {
          ;(gw.isCanonical
            ? renameCanonicalSession(gw, r.session_id, requestedTitle).then(title =>
                title === undefined ? null : ({ title } as SessionTitleResponse)
              )
            : rpc<SessionTitleResponse>('session.title', { session_id: r.session_id, title: requestedTitle })
          )
            .then(result => {
              if (!result || getUiState().sid !== r.session_id) {
                return
              }

              const nextTitle = (result.title ?? requestedTitle).trim()
              const suffix = result.pending ? t('session.lifecycle.titleQueuedSuffix') : ''
              patchUiState({ sessionTitle: nextTitle })
              sys(`${t('session.lifecycle.sessionTitleSet', nextTitle)}${suffix}`)
            })
            .catch((err: unknown) => {
              if (getUiState().sid !== r.session_id) {
                return
              }

              const message = err instanceof Error ? err.message : String(err)
              sys(`warning: ${t('session.lifecycle.failedToSetTitle', message)}`)
            })
        }

        signalFreshSessionBoundary(previousSid, r.session_id, onFreshSessionStarted)
        // Main closed the previous session here (`closeSession` → `tui_close`) unless kept live.
        adoptAttachment(r, previousSid, previousSubscription, !keepCurrent)

        return r.session_id
      } finally {
        finishAttachment(flight)
      }
    },
    [
      adoptAttachment,
      supersedeColdHydration,
      closeSession,
      colsRef,
      discardStaleAttachment,
      finishAttachment,
      gw,
      onFreshSessionStarted,
      panel,
      resetSession,
      rpc,
      setHistoryItems,
      setSessionStartedAt,
      sys
    ]
  )

  const newSession = useCallback(
    (msg?: string, title?: string) => startNewSession(msg, title, false),
    [startNewSession]
  )

  const newLiveSession = useCallback(
    (msg = t('session.lifecycle.newLiveSessionStarted'), title?: string) => {
      patchOverlayState({ sessions: false })

      return startNewSession(msg, title, true)
    },
    [startNewSession]
  )

  const activateLiveSession = useCallback(
    async (id: string) => {
      const flight = ++attachmentFlight.current
      pendingAttachments.current.add(flight)
      await supersedeColdHydration().catch(error => {
        finishAttachment(flight)
        throw error
      })

      if (flight !== attachmentFlight.current) {
        finishAttachment(flight)

        return
      }

      const previousSid = getUiState().sid
      const previousSubscription = releasedOnLeave(previousSid, canonicalSubscriptions.current, previousSid !== id)
      patchOverlayState({ sessions: false })
      patchUiState({ status: t('session.status.switchingSession') })
      // The card belongs to the session being left; the activated one answers with its own.
      clearConnectionOperation()

      const pendingDetach = canonicalDetachFlights.current.get(id)
      const hold = gw.holdAttach?.(id) ?? null

      const request = pendingDetach
        ? pendingDetach.then(() =>
            flight === attachmentFlight.current
              ? gw.request<SessionActivateResponse>('session.activate', { session_id: id })
              : null
          )
        : gw.request<SessionActivateResponse>('session.activate', { session_id: id })

      return request
        .then(raw => {
          const r = asRpcResult<SessionActivateResponse>(raw)

          if (flight !== attachmentFlight.current) {
            discardStaleAttachment(r)

            return
          }

          if (!r) {
            sys(`error: ${t('session.common.invalidResponse', 'session.activate')}`)

            patchUiState({ status: 'ready' })

            return null
          }

          // Agent-less (lazy) activations answer with `_fallback_session_info`, which
          // has no stored_session_id; the durable id is the response's session_key
          // (canonical snapshots carry it as stored_session_id).
          const storedSid = r.session_key || r.stored_session_id || r.info?.stored_session_id || r.session_id
          const info = r.info ? { ...r.info, stored_session_id: storedSid } : null
          const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

          resetSession()
          setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())
          const transcript = [...toTranscriptMessages(r.messages), ...liveSessionInflightMessages(r.inflight)]
          setHistoryItems(info ? [introMsg(info), ...transcript] : transcript)
          writeActiveSessionFile(storedSid)
          patchUiState({
            sessionKey: storedSid,
            busy: running,
            gatewayConnected: true,
            info,
            sid: r.session_id,
            status: statusFromLiveSession(r.status, running),
            storedSid,
            usage: usageFrom(info)
          })
          // resetSession dropped the previous session's controls; the snapshot's
          // still-pending approval/clarify prompts are the only way they come back.
          gw.hydrateSharedPrompts?.(r)
          hydrateLiveSessionInflight(r.inflight)
          gw.settleAttach?.(hold, r)

          if (r.pending_connection) {
            applyConnectionRequest(r.pending_connection)
          }

          cancelResumeScrollRef.current?.()
          cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)
          adoptAttachment(r, previousSid, previousSubscription)

          return r.session_id
        })
        .catch((e: Error) => {
          if (flight !== attachmentFlight.current) {
            return
          }

          sys(`error: ${e.message}`)
          patchUiState({ status: 'ready' })

          return null
        })
        .finally(() => {
          // A failed or superseded attach releases what it held (no-op after a settled snapshot).
          gw.settleAttach?.(hold)
          finishAttachment(flight)
        })
    },
    [
      adoptAttachment,
      discardStaleAttachment,
      finishAttachment,
      gw,
      supersedeColdHydration,
      resetSession,
      scrollRef,
      setHistoryItems,
      setSessionStartedAt,
      sys
    ]
  )

  const fetchOlderBacklog = useCallback(async () => {
    const currentSid = getUiState().sid

    if (gw.isCanonical || !currentSid || !viewportMeta?.has_more_before || isFetchingBacklogRef.current) {
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
  }, [gw.isCanonical, rpc, setHistoryItems, viewportMeta])

  const resumeById = useCallback(
    async (
      id: string,
      targetRecoveryRef?: { current: string | null },
      retryAttempt = 0,
      options?: ResumeOptions
    ): Promise<void> => {
      const retryBarrierIsCurrent = () => {
        const barrier = options?.retryBarrier
        return !barrier || (activeColdBarrierRef.current === barrier && gw.hasEventBarrier(barrier.sid, barrier.attemptId))
      }
      if (!retryBarrierIsCurrent()) {return}
      const flight = ++attachmentFlight.current
      pendingAttachments.current.add(flight)
      await supersedeColdHydration(options?.retryBarrier).catch(error => {
        finishAttachment(flight)
        throw error
      })

      if (flight !== attachmentFlight.current || !retryBarrierIsCurrent()) {
        finishAttachment(flight)

        return
      }

      const abandonRetryBarrier = () => {
        const barrier = options?.retryBarrier
        if (!barrier || activeColdBarrierRef.current !== barrier) {return}
        clearActiveColdBarrier(barrier.attemptId, barrier.sid)
        gw.cancelEventBarrier(barrier.sid, barrier.attemptId)
      }
      const beginResumeAttempt = () => {
        const attemptId = randomUUID()
        resumeAttemptRef.current = attemptId
        const generation = !gw.isCanonical && INLINE_MODE && DASHBOARD_TUI_MODE ? attemptId : null
        replayGeneration.current = generation
        const replayState: ReplayGenerationState | null = generation
          ? { attemptId, generation, aborted: false, ended: false }
          : null
        currentReplayStateRef.current = replayState
        activeReplayBoundaryRef.current = replayState

        return { attemptId, generation, replayState }
      }
      const { attemptId, generation, replayState } = beginResumeAttempt()
      const current = captureDestination()
      const previousSid = current.sid
      const destination = resumeDestination(current, id)
      const subscriptionAtStart = previousSid ? canonicalSubscriptions.current.get(previousSid) : undefined
      const isCurrent = () => flight === attachmentFlight.current && resumeAttemptRef.current === attemptId

      let replayBegun = false
      const startReplay = () => {
        if (generation && !replayBegun && isCurrent()) {
          replayBegun = true
          stdout.write(`\x1b]777;hermes-replay;begin;${generation}\x07`)
        }
      }
      const abortReplay = () => {if (generation) {abortReplayOnce(generation)}}

      let hold: ReturnType<GatewayClient['holdAttach']> | null = null
      const attemptIsCurrent = () => isCurrent() && retryBarrierIsCurrent()
      const resetResumedSession = (r: ResumeResult) => {
        resetSession()
        setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())
      }
      const recoverColdHistory = async (error: unknown, tx: ActiveColdOutputTransaction, barrier: ColdBarrier) => {
        const historyRetryAttempt = options?.historyRetryAttempt ?? 0
        const failure = classifyResumeFailure(error)
        const retryHistory = !(error instanceof ColdHydrationCancelledError) && isCurrent() &&
          gw.hasEventBarrier(tx.sid, tx.barrierOwner) && failure.kind !== 'transport' &&
          failure.kind !== 'identity' && historyRetryAttempt < 3
        try {
          await settleCancelledColdTx(tx, retryHistory)
        } catch {return}
        if (error instanceof ColdHydrationCancelledError || !isCurrent()) {return}
        if (failure.kind === 'transport') {
          patchUiState({ status: 'disconnected' })
          return
        }
        if (retryHistory) {
          patchUiState({ status: 'retrying history…' })
          await new Promise<void>(resolve => {
            coldRetryWakeRef.current = resolve
            coldRetryTimerRef.current = setTimeout(() => {
              coldRetryTimerRef.current = null
              coldRetryWakeRef.current = null
              resolve()
            }, Math.min(1000, 250 * 2 ** historyRetryAttempt))
          })
          if (isCurrent()) {
            await resumeById(id, targetRecoveryRef, 0, {mode: 'cold-resume', durableKey: tx.durableKey,
              historyRetryAttempt: historyRetryAttempt + 1, retryBarrier: barrier})
          }
        } else {
          sys(`error: history recovery failed: ${error instanceof Error ? error.message : String(error)}`)
          patchUiState({ status: 'history incomplete' })
        }
      }
      const hydrateColdResume = async (r: ResumeResult, storedSid: string, info: SessionInfo | null) => {
        coldHydrationIncompleteRef.current = storedSid
        const previous = activeColdBarrierRef.current
        const continuingBarrier = previous && previous === options?.retryBarrier && previous.sid === r.session_id
        if (previous && !continuingBarrier) {gw.cancelEventBarrier(previous.sid, previous.attemptId)}
        const barrier = continuingBarrier ? previous : { attemptId, sid: r.session_id }
        if (!continuingBarrier) {gw.activateEventBarrier(r.session_id, barrier.attemptId)}
        activeColdBarrierRef.current = barrier

        let currentPhase: ColdTransactionPhase = 'hydrating'
        let settled = false
        let resolveSettlement!: (result: ColdSettlement) => void
        const settlementPromise = new Promise<ColdSettlement>(resolve => {resolveSettlement = resolve})
        const tx: ActiveColdOutputTransaction = {
          attemptId, barrierOwner: barrier.attemptId, sid: r.session_id, durableKey: storedSid,
          lease: null, boundaryGeneration: generation, staticOutputStarted: false, settlementPromise,
          replayState,
          settleTransaction: result => {
            if (settled) {return}
            settled = true
            currentPhase = 'settled'
            resolveSettlement(result)
          },
          get phase() {return currentPhase},
          setPhase: phase => {if (!settled) {currentPhase = phase}}
        }
        activeColdOutputRef.current = tx
        tx.acquisitionPromise = acquireMainScreenStaticOutput(stdout)
        let lease: MainScreenStaticOutputLease
        try {
          lease = await tx.acquisitionPromise
          tx.lease = lease
        } catch (error) {
          clearActiveColdBarrier(tx.barrierOwner, tx.sid)
          gw.cancelEventBarrier(tx.sid, tx.barrierOwner)
          abortReplayOnce(tx)
          tx.settleTransaction({ ok: true })
          if (activeColdOutputRef.current === tx) {activeColdOutputRef.current = null}
          if (isCurrent()) {
            sys(`error: failed to acquire static output lease: ${error instanceof Error ? error.message : String(error)}`)
            patchUiState({ status: 'ready' })
          }
          return false
        }

        if (!isCurrent() || tx.phase === 'cancelling' || !gw.hasEventBarrier(tx.sid, tx.barrierOwner)) {
          await settleCancelledColdTx(tx)
          return false
        }
        startReplay()
        resetResumedSession(r)
        showResumedSession(r, storedSid, info)
        const output: ColdHistoryOutput = {
          getColumns: () => colsRef.current,
          beginStaticAppendSurface: async () => {tx.staticOutputStarted = true; await lease.beginStaticAppendSurface()},
          write: async data => {tx.staticOutputStarted = true; await lease.write(data)}
        }
        try {
          const hydration = await performColdHistoryHydration({ gateway: gw, sessionId: r.session_id,
            theme: getUiState().theme, info, maxMounted, output,
            isCancelled: () => !isCurrent() || !gw.hasEventBarrier(tx.sid, tx.barrierOwner) })
          if (!isCurrent() || !gw.hasEventBarrier(tx.sid, tx.barrierOwner)) {
            await settleCancelledColdTx(tx)
            return false
          }
          tx.setPhase('commit-pending')
          tx.appendedToScrollback = hydration.appendedToScrollback
          setHistoryItems([...hydration.initialLiveMessages,
            ...liveSessionInflightMessages(r.inflight, hydration.initialLiveMessages)])
          setViewportMeta(r.viewport ?? null)
          await new Promise<void>((resolve, reject) => {
            // React/physical acknowledgement is independent of transaction cleanup settlement.
            pendingColdCommitRef.current = Object.assign(tx, {
              settle: (error?: unknown) => error === undefined ? resolve() : reject(error)
            })
            setColdCommitGeneration(attemptId)
          })
        } catch (error) {
          await recoverColdHistory(error, tx, barrier)

          return false
        }

        return true
      }
      const completeResume = (r: ResumeResult, storedSid: string) => {
        writeActiveSessionFile(storedSid)
        const recoveryRef = targetRecoveryRef ?? recoverSessionKeyRef ?? activeRecoveryTargetRef

        if (recoveryRef) {recoveryRef.current = null}

        if (opts.recoverSidRef) {opts.recoverSidRef.current = null}

        if (opts.recoverSessionKeyRef) {opts.recoverSessionKeyRef.current = null}
        gw.hydrateSharedPrompts?.(r)
        hydrateLiveSessionInflight(r.inflight)
        gw.settleAttach?.(hold, r)

        if (r.pending_connection) {applyConnectionRequest(r.pending_connection)}
        else {clearConnectionOperation()}

        cancelResumeScrollRef.current?.()
        cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)

        if (previousSid && previousSid !== r.session_id) {void closeSession(previousSid)}
        adoptAttachment(r, previousSid, subscriptionAtStart, true)
      }
      const restoreInlineHistory = (r: ResumeResult, info: SessionInfo | null, transportRecovery: boolean) => {
        const frame = generation ? { generation, replaceFrame: true } : null
        if (transportRecovery) {
          setHistoryItems(prev => [...prev, ...liveSessionInflightMessages(r.inflight, prev)])
          setReplayCommitted(frame)

          return
        }
        const transcript = toTranscriptMessages(r.messages ?? [])
        const resumed = [...transcript, ...liveSessionInflightMessages(r.inflight, transcript)]
        setHistoryItems(info ? [introMsg(info), ...resumed] : resumed)
        setViewportMeta(r.viewport ?? null)
        setReplayCommitted(frame)
        coldHydrationIncompleteRef.current = null
      }
      const handleResumeFailure = async (error: unknown) => {
        if (!attemptIsCurrent()) {return}
        const failure = classifyResumeFailure(error)

        if (!gw.isCanonical && failure.kind === 'retry-same') {
          const settling = failure.reason === 'disconnect_interrupt_settling'

          if (retryAttempt < (settling ? 15 : 4)) {
            await new Promise(resolve => setTimeout(resolve, settling ? 1000 : Math.min(2000, 250 * 2 ** retryAttempt)))

            if (isCurrent()) {await resumeById(id, targetRecoveryRef, retryAttempt + 1, options)}

            return
          }
        }

        abandonRetryBarrier()
        if (!gw.isCanonical && failure.kind === 'identity') {
          const fallback = readActiveSessionFile()

          if (fallback && fallback !== id) {
            await resumeById(fallback, targetRecoveryRef, 0, { mode: 'cold-resume' })

            return
          }
        }

        abortReplay()

        if (failure.kind === 'transport') {
          patchUiState({ status: 'disconnected' })

          return
        }

        sys(`error: ${error instanceof Error ? error.message : String(error)}`)
        if (gw.isCanonical) {
          const setup = await rpc<SetupStatusResponse>('setup.status', {})

          if (!isCurrent()) {return}
          if (setup?.provider_configured === false) {
            panel(setupRequiredTitle(), buildSetupRequiredSections())
            patchUiState({ status: t('session.status.setupRequired') })

            return
          }
        }
        patchUiState({ status: 'ready' })
      }
      const prepareResumedSession = (r: ResumeResult, storedSid: string, info: SessionInfo | null, reset: boolean) => {
        migratePendingInputs(destination, r.session_id, info?.stored_session_id)
        if (reset) {resetResumedSession(r)}
        showResumedSession(r, storedSid, info)
      }

      patchOverlayState({ sessions: false })
      patchUiState({ status: t('session.status.resuming') })

      try {
        const setup = gw.isCanonical ? null : await rpc<SetupStatusResponse>('setup.status', {})

        if (!attemptIsCurrent()) {return}

        if (setup?.provider_configured === false) {
          abandonRetryBarrier()
          abortReplay()
          panel(setupRequiredTitle(), buildSetupRequiredSections())
          patchUiState({ status: t('session.status.setupRequired') })

          return
        }

        hold = gw.holdAttach?.(id) ?? null
        await canonicalDetachFlights.current.get(id)

        if (!attemptIsCurrent()) {return}
        const { isColdHydration, isTransportRecovery } = resumeHistoryMode(
          gw.isCanonical, options, coldHydrationIncompleteRef.current, id, previousSid
        )
        const params = {
          cols: colsRef.current, session_id: id,
          ...(isTransportRecovery || isColdHydration ? { omit_messages: true } : {})
        }

        const r = asRpcResult<ResumeResult>(
          await gw.request<ResumeResult>('session.resume', params)
        )

        if (!attemptIsCurrent()) {
          discardStaleAttachment(r)

          return
        }

        if (!r) {
          abandonRetryBarrier()
          abortReplay()
          sys(`error: ${t('session.common.invalidResponse', 'session.resume')}`)
          patchUiState({ status: 'ready' })

          return
        }

        if (!isColdHydration) {startReplay()}

        const { storedSid, info } = resumedSessionInfo(r, id)
        prepareResumedSession(r, storedSid, info, !isTransportRecovery && !isColdHydration)

        if (isColdHydration) {
          if (!(await hydrateColdResume(r, storedSid, info))) {return}
        } else {
          restoreInlineHistory(r, info, isTransportRecovery)
        }

        if (!isCurrent()) {return}
        completeResume(r, storedSid)
      } catch (error) {
        await handleResumeFailure(error)
      } finally {
        gw.settleAttach?.(hold)
        finishAttachment(flight)
      }
    },
    [abortReplayOnce, adoptAttachment, clearActiveColdBarrier, closeSession, colsRef, discardStaleAttachment, finishAttachment, gw,
      coldHydrationIncompleteRef, opts.recoverSessionKeyRef, opts.recoverSidRef, panel, recoverSessionKeyRef, resetSession, rpc, scrollRef,
      maxMounted, setHistoryItems, setSessionStartedAt, settleCancelledColdTx, stdout, supersedeColdHydration, sys]
  )

  const guardBusySessionSwitch = useCallback(
    (what = t('session.lifecycle.switchSessions')) => {
      if (!getUiState().busy) {
        return false
      }

      sys(t('session.lifecycle.interruptBeforeSwitch', what))

      return true
    },
    [sys]
  )

  return useMemo(
    () => ({
      activateLiveSession,
      closeForExit,
      closeLiveView,
      closeSession,
      coldHydrationIncompleteRef,
      fetchOlderBacklog,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      viewportMeta,
      trimLastExchange: trimTail
    }),
    [
      activateLiveSession,
      closeForExit,
      closeLiveView,
      closeSession,
      coldHydrationIncompleteRef,
      fetchOlderBacklog,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      viewportMeta
    ]
  )
}
