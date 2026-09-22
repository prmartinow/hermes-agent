import { randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'

import type { ScrollBoxHandle } from '@hermes/ink'
import { evictInkCaches, writeAfterRender } from '@hermes/ink'
import type { InflightTurn, SessionResumeResult, Usage } from '@hermes/shared/gateway-events'
import { type RefObject, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { INLINE_MODE, DASHBOARD_TUI_MODE } from '../config/env.js'

import { buildSetupRequiredSections, SETUP_REQUIRED_TITLE } from '../content/setup.js'
import { introMsg, toTranscriptMessages } from '../domain/messages.js'
import { performColdHistoryHydration, ColdHydrationCancelledError } from './coldHistoryHydration.js'
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

  return [{ role: 'user', text: user }]
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
  rpc: GatewayRpc
  scrollRef: RefObject<null | ScrollBoxHandle>
  setHistoryItems: StateSetter<Msg[]>
  setLastUserMsg: StateSetter<string>
  setSessionStartedAt: StateSetter<number>
  setStickyPrompt: StateSetter<string>
  setVoiceProcessing: StateSetter<boolean>
  setVoiceRecording: StateSetter<boolean>
  sys: (text: string) => void
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

  const resumeAttemptRef = useRef<string | null>(null)
  const replayGeneration = useRef<string | null>(null)
  const [replayCommitted, setReplayCommitted] = useState<string | null>(null)
  const pendingColdCommitRef = useRef<{ attemptId: string; boundaryGeneration: string | null; sid: string } | null>(null)
  const [coldCommitGeneration, setColdCommitGeneration] = useState<string | null>(null)

  useLayoutEffect(() => {
    const pending = pendingColdCommitRef.current
    if (!pending) return
    if (pending.attemptId !== resumeAttemptRef.current) {
      pendingColdCommitRef.current = null
      gw?.cancelEventBarrier(pending.sid)
      return
    }
    pendingColdCommitRef.current = null
    gw?.releaseEventBarrier(pending.sid)
    if (pending.boundaryGeneration) {
      setReplayCommitted(pending.boundaryGeneration)
    }
  }, [coldCommitGeneration, gw])

  useEffect(() => {
    return () => {
      const pending = pendingColdCommitRef.current
      if (pending) {
        pendingColdCommitRef.current = null
        gw?.cancelEventBarrier(pending.sid)
      }
    }
  }, [gw])

  useLayoutEffect(() => {
    if (replayCommitted && replayCommitted === replayGeneration.current) {
      writeAfterRender(`\x1b]777;hermes-replay;end;${replayCommitted}\x07`, process.stdout, true)
    }
  }, [replayCommitted])

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
    patchUiState({ bgTasks: new Set(), info: null, sessionKey: null, sid: null, usage: ZERO })
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

      const info = r.info ?? null
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
    [closeSession, colsRef, onFreshSessionStarted, panel, resetSession, rpc, setHistoryItems, setSessionStartedAt, sys]
  )

  const newSession = useCallback(
    (msg?: string, title?: string) => startNewSession(msg, title, false),
    [startNewSession]
  )

  const newLiveSession = useCallback(
    (msg = 'new live session started', title?: string) => {
      patchOverlayState({ sessions: false })

      return startNewSession(msg, title, true)
    },
    [startNewSession]
  )

  const activateLiveSession = useCallback(
    (id: string) => {
      patchOverlayState({ sessions: false })
      patchUiState({ status: 'switching session…' })

      gw.request<SessionActivateResponse>('session.activate', { session_id: id })
        .then(raw => {
          const r = asRpcResult<SessionActivateResponse>(raw)

          if (!r) {
            sys('error: invalid response: session.activate')

            return patchUiState({ status: 'ready' })
          }

          const info = r.info ?? null
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
            usage: usageFrom(info)
          })
          hydrateLiveSessionInflight(r.inflight)
          cancelResumeScrollRef.current?.()
          cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)
        })
        .catch((e: Error) => {
          sys(`error: ${e.message}`)
          patchUiState({ status: 'ready' })
        })
    },
    [gw, resetSession, scrollRef, setHistoryItems, setSessionStartedAt, sys]
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
    (
      id: string,
      targetRecoveryRef?: { current: string | null },
      retryAttempt = 0,
      options?: { gapReason?: string; mode?: "transport-recovery" | "transport-gap-recovery" | "cold-resume" }
    ) => {
      patchOverlayState({ sessions: false })
      patchUiState({ status: 'resuming…' })
      const attemptId = randomUUID()
      resumeAttemptRef.current = attemptId
      const generation = INLINE_MODE && DASHBOARD_TUI_MODE ? attemptId : null
      replayGeneration.current = generation
      let replayBegun = false

      const startReplay = () => {
        if (generation && !replayBegun && resumeAttemptRef.current === attemptId) {
          replayBegun = true
          process.stdout.write(`\x1b]777;hermes-replay;begin;${generation}\x07`)
        }
      }

      const abortReplay = () => {
        if (generation && resumeAttemptRef.current === attemptId) {
          process.stdout.write(`\x1b]777;hermes-replay;abort;${generation}\x07`)
        }
      }

      rpc<SetupStatusResponse>('setup.status', {}).then(setup => {
        if (resumeAttemptRef.current !== attemptId) return
        if (setup?.provider_configured === false) {
          abortReplay()
          panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections())
          patchUiState({ status: 'setup required' })

          return
        }

        const previousSid = getUiState().sid

        const isTransportRecovery = options?.mode === 'transport-recovery'
        const isGapRecovery = options?.mode === 'transport-gap-recovery'
        const resumeParams: Record<string, unknown> = { cols: colsRef.current, session_id: id }
        if (isTransportRecovery || (!isGapRecovery && INLINE_MODE)) {
          resumeParams.omit_messages = true
        }

        return gw.request<SessionResumeResult & { viewport?: SessionViewportMeta }>('session.resume', resumeParams)
          .then(raw => {
            if (resumeAttemptRef.current !== attemptId) return
            const r = asRpcResult<SessionResumeResult & { viewport?: SessionViewportMeta }>(raw)

            if (!r) {
              abortReplay()
              sys('error: invalid response: session.resume')

              return patchUiState({ status: 'ready' })
            }

            // Valid response acquired: begin replay boundary now
            startReplay()

            const info = r.info ?? null
            const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

            const isColdHydration = !isTransportRecovery && !isGapRecovery && INLINE_MODE

            if (isColdHydration) {
              resetSession()
              setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())

              gw.activateEventBarrier(r.session_id)

              performColdHistoryHydration({
                gateway: gw,
                sessionId: r.session_id,
                cols: colsRef.current,
                theme: getUiState().theme,
                info,
                stdout: process.stdout,
                isCancelled: () => resumeAttemptRef.current !== attemptId
              }).then(hydration => {
                if (resumeAttemptRef.current !== attemptId) {
                  gw.cancelEventBarrier(r.session_id)
                  return
                }
                const resumed = [...hydration.initialLiveMessages, ...liveSessionInflightMessages(r.inflight, hydration.initialLiveMessages)]
                // 1. Commit live tail to React state first
                setHistoryItems(resumed)
                setViewportMeta(r.viewport ?? null)
                // 2. Queue commit acknowledgement: useLayoutEffect releases barrier and finishes replay after React commits this frame
                pendingColdCommitRef.current = { attemptId, boundaryGeneration: generation, sid: r.session_id }
                setColdCommitGeneration(attemptId)
              }).catch(err => {
                if (resumeAttemptRef.current !== attemptId) {
                  gw.cancelEventBarrier(r.session_id)
                  return
                }
                gw.cancelEventBarrier(r.session_id)
                if (err instanceof ColdHydrationCancelledError) {
                  return
                }
                const transcriptMsgs = toTranscriptMessages(r.messages ?? [])
                const resumed = [...transcriptMsgs, ...liveSessionInflightMessages(r.inflight, transcriptMsgs)]
                setHistoryItems(info ? [introMsg(info), ...resumed] : resumed)
                setViewportMeta(r.viewport ?? null)
                if (generation) {
                  setReplayCommitted(generation)
                }
              })
            } else if (!isTransportRecovery) {
              resetSession()
              setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())

              const transcriptMsgs = toTranscriptMessages(r.messages ?? [])
              const resumed = [...transcriptMsgs, ...liveSessionInflightMessages(r.inflight, transcriptMsgs)]

              setHistoryItems(info ? [introMsg(info), ...resumed] : resumed)
              setViewportMeta(r.viewport ?? null)
              setReplayCommitted(generation)
            } else {
              // Transport recovery fast path: historyItems are already preserved!
              // Hydrate any newly arrived inflight state
              if (r.inflight) {
                setHistoryItems(prev => {
                  const inflightMsgs = liveSessionInflightMessages(r.inflight, prev)
                  return inflightMsgs.length > 0 ? [...prev, ...inflightMsgs] : prev
                })
              }
              setReplayCommitted(generation)
            }
            const durableKey = (r as any).resumed ?? (r as any).session_key ?? (r as any).stored_session_id ?? r.session_id
            writeActiveSessionFile(durableKey)
            patchUiState({
              busy: running,
              info,
              sessionKey: durableKey,
              sid: r.session_id,
              status: statusFromLiveSession(r.status ?? undefined, running),
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
            cancelResumeScrollRef.current?.()
            cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)

            if (previousSid && previousSid !== r.session_id) {
              gw?.retireSession(previousSid)
              void closeSession(previousSid)
            }
          })
      }).catch((e: unknown) => {
        if (resumeAttemptRef.current !== attemptId) return
        const failure = classifyResumeFailure(e)
        if (failure.kind === 'retry-same') {
          const isSettling = failure.reason === 'disconnect_interrupt_settling'
          const maxRetries = isSettling ? 15 : 4
          if (retryAttempt < maxRetries) {
            const delay = isSettling ? 1000 : Math.min(2000, 250 * Math.pow(2, retryAttempt))
            setTimeout(() => {
              if (replayGeneration.current === generation) {
                resumeById(id, targetRecoveryRef, retryAttempt + 1, options)
              }
            }, delay)
            return
          }
        }
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
    [closeSession, colsRef, gw, opts.recoverSessionKeyRef, opts.recoverSidRef, panel, recoverSessionKeyRef, resetSession, rpc, scrollRef, setHistoryItems, setSessionStartedAt, sys]
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
