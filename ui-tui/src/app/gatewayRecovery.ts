import type { GatewayExitContext } from '../gatewayClient.js'

import { turnController as defaultTurnController } from './turnController.js'
import { getUiState as defaultGetUiState, patchUiState as defaultPatchUiState } from './uiStore.js'
import {
  lastStderrLine,
  recoveryGaveUpActivity,
  recoveryGaveUpMessage,
  recoveryRestartingActivity,
  recoveryRestartingMessage
} from './userMessages.js'

// Crash-recovery budget for the gateway exit handler. A gateway that
// crash-loops on startup must not let the TUI spawn-storm, so respawn+resume
// attempts are capped to GATEWAY_RECOVERY_LIMIT within a sliding
// GATEWAY_RECOVERY_WINDOW_MS; past the budget the app falls back to the inert
// "gateway exited" state. Kept pure (no refs/UI) so the bound — including the
// crash-loop case — is unit-testable.
export const GATEWAY_RECOVERY_LIMIT = 3
export const GATEWAY_RECOVERY_WINDOW_MS = 60_000

export interface RecoveryPlan {
  // Attempt timestamps to persist (the pruned window, plus `now` iff recovering).
  attempts: number[]
  recover: boolean
  // Session to resume — the live sid, or the not-yet-consumed recovery target
  // when the live sid was already cleared by a prior exit.
  sid: null | string
}

// Decide whether to respawn+resume after a gateway death. `liveSid` is the
// current session (nulled on the first exit); `recoverSid` is a pending
// recovery target carried across a respawn that died before gateway.ready —
// so a startup crash-loop keeps retrying the same session up to the budget
// instead of stranding it after one attempt.
export function planGatewayRecovery(
  liveSid: null | string,
  recoverSid: null | string,
  attempts: number[],
  now: number
): RecoveryPlan {
  const sid = liveSid ?? recoverSid
  const recent = attempts.filter(t => now - t < GATEWAY_RECOVERY_WINDOW_MS)
  const recover = Boolean(sid) && recent.length < GATEWAY_RECOVERY_LIMIT

  return { attempts: recover ? [...recent, now] : recent, recover, sid }
}

export let activeRecoveryTargetRef: { current: string | null } | null = null

export function setActiveRecoveryTargetRef(ref: { current: string | null } | null): void {
  activeRecoveryTargetRef = ref
}

export interface GatewayRecoveryClient {
  isAttached(): boolean
  start(): void
  getLogTail(lines?: number): string
}

export interface GatewayRecoveryTurnController {
  reset(): void
  pushActivity(activity: string, level?: 'info' | 'warn' | 'error'): void
}

export type GatewayExitContextInput = Partial<GatewayExitContext>

export interface HandleGatewayExitOptions {
  code: null | number
  context?: GatewayExitContextInput
  gw: GatewayRecoveryClient
  sys: (text: string) => void
  recoverSessionKeyRef?: { current: string | null }
  recoverSidRef?: { current: string | null }
  recoveryAtRef: { current: number[] }
  gaveUpRef: { current: boolean }
  turnController?: GatewayRecoveryTurnController
  getUiState?: () => { busy: boolean; sessionKey?: null | string; storedSid?: null | string; sid: null | string }
  patchUiState?: (patch: { busy?: boolean; compacting?: boolean; sessionKey?: null | string; sid?: null | string; status?: string }) => void
  now?: () => number
}

export function handleGatewayExit(options: HandleGatewayExitOptions): RecoveryPlan {
  const {
    code,
    context,
    gw,
    sys,
    recoveryAtRef,
    gaveUpRef,
    turnController = defaultTurnController,
    getUiState = defaultGetUiState,
    patchUiState = defaultPatchUiState,
    now = Date.now
  } = options

  const recoverSessionKeyRef = options.recoverSessionKeyRef ?? options.recoverSidRef
  if (!recoverSessionKeyRef) {
    throw new Error('handleGatewayExit requires recoverSessionKeyRef or recoverSidRef')
  }

  setActiveRecoveryTargetRef(recoverSessionKeyRef)

  const source = context?.source ?? (gw.isAttached() ? 'websocket' : 'process')
  const isProcessExit = source === 'process'

  // Only reset active turn state if the backend process itself actually exited.
  // A transport disconnect (e.g. socket 1006) does NOT mean the process crashed
  // or that the turn in progress was lost; preserving state allows seamless
  // continuity once the transport re-attaches and resumes.
  if (isProcessExit) {
    turnController.reset()
  }

  // A still-owned child dying while the TUI is alive is an *unexpected*
  // death — a user /quit exits Node before this fires, and a replaced child
  // is identity-skipped in GatewayClient. Rather than stranding a long
  // session (the user's complaint), respawn the gateway and resume the
  // persisted session via the next gateway.ready, so a single crash / OOM /
  // signal doesn't lose their work. planGatewayRecovery bounds the attempts
  // so a gateway that crash-loops on startup can't spawn-storm, and falls
  // back to recoverSessionKeyRef when sid was already cleared by a prior exit.
  const state = getUiState()
  const durableKey = state.sessionKey ?? state.storedSid ?? state.sid

  if (!isProcessExit) {
    const target = durableKey ?? recoverSessionKeyRef.current
    recoverSessionKeyRef.current = target
    if (options.recoverSidRef && options.recoverSidRef !== recoverSessionKeyRef) {
      options.recoverSidRef.current = target
    }

    patchUiState({
      busy: state.busy,
      compacting: false,
      sid: null,
      status: 'reconnecting…'
    })

    if (state.sid) {
      turnController.pushActivity(recoveryRestartingActivity(source), 'warn')
      sys(recoveryRestartingMessage(source))
    }

    return {
      attempts: recoveryAtRef.current,
      recover: Boolean(target),
      sid: target
    }
  }

  const plan = planGatewayRecovery(durableKey, recoverSessionKeyRef.current, recoveryAtRef.current, now())

  // Clear sid immediately: while the gateway is down, sid-guarded effects
  // (session.active_list poll, queue drain) would otherwise fire RPCs at a
  // dead/respawning gateway. recoverSessionKeyRef carries the session forward, and
  // resumeById restores sid once the fresh gateway is ready.
  recoveryAtRef.current = plan.attempts
  patchUiState({
    busy: false,
    compacting: false,
    sid: null,
    status: 'restarting…'
  })

  if (plan.recover && plan.sid) {
    recoverSessionKeyRef.current = plan.sid
    if (options.recoverSidRef && options.recoverSidRef !== recoverSessionKeyRef) {
      options.recoverSidRef.current = plan.sid
    }
    turnController.pushActivity(recoveryRestartingActivity(source), 'warn')
    sys(recoveryRestartingMessage(source))
    gw.start()

    return plan
  }

  // Budget spent (crash loop) or nothing to recover: GatewayClient keeps
  // retrying on its backoff — say so ONCE, with the exit code and the last
  // stderr line, rather than repeating "gateway exited" every tick. Keep the
  // recovery target: when that background reconnect eventually succeeds,
  // gateway.ready must reopen the SAME chat instead of forging a new one.
  recoverSessionKeyRef.current = plan.sid
  if (options.recoverSidRef && options.recoverSidRef !== recoverSessionKeyRef) {
    options.recoverSidRef.current = plan.sid
  }
  patchUiState({ status: 'stopped' })

  if (!gaveUpRef.current) {
    gaveUpRef.current = true
    turnController.pushActivity(recoveryGaveUpActivity(source), 'error')
    sys(`error: ${recoveryGaveUpMessage(source, code, lastStderrLine(gw.getLogTail(20)))}`)
  }

  return plan
}

export function createGatewayExitHandler(
  options: Omit<HandleGatewayExitOptions, 'code' | 'context'>
): (code: null | number, context?: GatewayExitContextInput) => RecoveryPlan {
  return (code, context) => handleGatewayExit({ ...options, code, context })
}
