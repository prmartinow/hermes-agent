import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { createGatewayExitHandler } from '../app/gatewayRecovery.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import {
  BACKEND_GAVE_UP_ACTIVITY,
  BACKEND_RESTARTING,
  BACKEND_RESTARTING_ACTIVITY,
  recoveryGaveUpActivity,
  recoveryGaveUpMessage,
  TRANSPORT_GAVE_UP_ACTIVITY,
  TRANSPORT_RECONNECTING,
  TRANSPORT_RECONNECTING_ACTIVITY
} from '../app/userMessages.js'
import type { Msg } from '../types.js'

describe('Gateway Recovery: WebSocket Transport Loss vs Process Exit', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.reset()
    vi.restoreAllMocks()
  })

  it('preserves live turn/busy and reconnect target with truthful transport copy on socket 1006 loss', () => {
    const sysMock = vi.fn()
    const pushActivityMock = vi.spyOn(turnController, 'pushActivity')
    const resetSpy = vi.spyOn(turnController, 'reset')

    // Simulate an active turn in progress with durable sessionKey
    patchUiState({ busy: true, sessionKey: 'session-durable-123', sid: 'session-turn-123', status: 'working…' })
    expect(getUiState().busy).toBe(true)
    expect(getUiState().sessionKey).toBe('session-durable-123')

    const recoverSidRef = { current: null as string | null }
    const recoveryAtRef = { current: [] as number[] }
    const gaveUpRef = { current: false }
    const startMock = vi.fn()

    const gw = {
      isAttached: () => true,
      start: startMock,
      getLogTail: () => '[lifecycle] websocket close code=1006 clean=false'
    }

    const exitHandler = createGatewayExitHandler({
      gaveUpRef,
      gw,
      recoverSidRef,
      recoveryAtRef,
      sys: sysMock
    })

    // Trigger internal transport loss (WebSocket code 1006)
    exitHandler(1006, {
      code: 1006,
      source: 'websocket',
      reason: 'gateway websocket closed (1006)',
      clean: false,
      initiator: 'remote_or_network'
    })

    // Assert: turnController was NOT reset
    expect(resetSpy).not.toHaveBeenCalled()

    // Assert: UI state kept busy=true and changed status to reconnecting
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('reconnecting…')

    // Assert: sid nulled during disconnect to prevent firing RPCs to dead gateway
    expect(getUiState().sid).toBeNull()

    // Assert: recoverSidRef carries the durable sessionKey (not runtime sid)
    expect(recoverSidRef.current).toBe('session-durable-123')

    // Assert: truthful recovery copy shown (no crash claim, no lost reply claim)
    expect(pushActivityMock).toHaveBeenCalledWith(TRANSPORT_RECONNECTING_ACTIVITY, 'warn')
    expect(sysMock).toHaveBeenCalledWith(TRANSPORT_RECONNECTING)
    expect(sysMock).not.toHaveBeenCalledWith(BACKEND_RESTARTING)

    // Assert: reconnect initiated
    expect(startMock).toHaveBeenCalledTimes(1)
  })

  it('resets active turn and shows crash recovery copy on confirmed process exit', () => {
    const sysMock = vi.fn()
    const pushActivityMock = vi.spyOn(turnController, 'pushActivity')
    const resetSpy = vi.spyOn(turnController, 'reset')

    patchUiState({ busy: true, sessionKey: 'session-durable-456', sid: 'session-proc-456', status: 'working…' })

    const recoverSidRef = { current: null as string | null }
    const recoveryAtRef = { current: [] as number[] }
    const gaveUpRef = { current: false }
    const startMock = vi.fn()

    const gw = {
      isAttached: () => false,
      start: startMock,
      getLogTail: () => '[lifecycle] child exit code=1'
    }

    const exitHandler = createGatewayExitHandler({
      gaveUpRef,
      gw,
      recoverSidRef,
      recoveryAtRef,
      sys: sysMock
    })

    // Trigger process exit (child exit code 1)
    exitHandler(1, {
      code: 1,
      source: 'process',
      reason: 'signal SIGTERM',
      clean: false
    })

    // Assert: turnController WAS reset for real process death
    expect(resetSpy).toHaveBeenCalled()

    // Assert: UI state cleared busy and changed status to restarting
    expect(getUiState().busy).toBe(false)
    expect(getUiState().status).toBe('restarting…')
    expect(getUiState().sid).toBeNull()

    // Assert: recovery target preserved with durable sessionKey
    expect(recoverSidRef.current).toBe('session-durable-456')

    // Assert: process crash copy used
    expect(pushActivityMock).toHaveBeenCalledWith(BACKEND_RESTARTING_ACTIVITY, 'warn')
    expect(sysMock).toHaveBeenCalledWith(BACKEND_RESTARTING)
    expect(sysMock).not.toHaveBeenCalledWith(TRANSPORT_RECONNECTING)
    expect(startMock).toHaveBeenCalledTimes(1)
  })

  it('retains reconnect target across repeated close while sid is null and bounds gave-up notification', () => {
    const sysMock = vi.fn()
    const pushActivityMock = vi.spyOn(turnController, 'pushActivity')
    const startMock = vi.fn()

    const gw = {
      isAttached: () => true,
      start: startMock,
      getLogTail: () => '[lifecycle] websocket close code=1006 clean=false'
    }

    patchUiState({ busy: true, sid: 'session-repeated-789', status: 'working…' })

    const recoverSidRef = { current: null as string | null }
    const recoveryAtRef = { current: [] as number[] }
    const gaveUpRef = { current: false }

    const exitHandler = createGatewayExitHandler({
      gaveUpRef,
      gw,
      recoverSidRef,
      recoveryAtRef,
      sys: sysMock
    })

    // 1st close: live sid present, clears sid, initiates reconnect
    exitHandler(1006, { code: 1006, source: 'websocket' })
    expect(getUiState().sid).toBeNull()
    expect(recoverSidRef.current).toBe('session-repeated-789')
    expect(recoveryAtRef.current).toHaveLength(1)
    expect(startMock).toHaveBeenCalledTimes(1)

    // 2nd close while sid is null (reconnect attempt failed before ready)
    exitHandler(1006, { code: 1006, source: 'websocket' })
    expect(getUiState().sid).toBeNull()
    expect(recoverSidRef.current).toBe('session-repeated-789')
    expect(recoveryAtRef.current).toHaveLength(2)
    expect(startMock).toHaveBeenCalledTimes(2)

    // 3rd close while sid is null
    exitHandler(1006, { code: 1006, source: 'websocket' })
    expect(getUiState().sid).toBeNull()
    expect(recoverSidRef.current).toBe('session-repeated-789')
    expect(recoveryAtRef.current).toHaveLength(3)
    expect(startMock).toHaveBeenCalledTimes(3)

    // 4th close while sid is null: budget exhausted (limit = 3)
    exitHandler(1006, { code: 1006, source: 'websocket' })
    // Reconnect target must STILL be retained for eventual background reconnect!
    expect(recoverSidRef.current).toBe('session-repeated-789')
    expect(getUiState().status).toBe('disconnected')
    expect(gaveUpRef.current).toBe(true)
    expect(pushActivityMock).toHaveBeenCalledWith(TRANSPORT_GAVE_UP_ACTIVITY, 'error')
    expect(sysMock).toHaveBeenCalledWith(expect.stringContaining('Connection to Hermes was lost (code 1006)'))
    // No additional gw.start() call since budget is spent
    expect(startMock).toHaveBeenCalledTimes(3)

    // 5th close: gaveUpRef prevents repeated error spam
    const sysCallCountBefore = sysMock.mock.calls.length
    exitHandler(1006, { code: 1006, source: 'websocket' })
    expect(recoverSidRef.current).toBe('session-repeated-789')
    expect(sysMock.mock.calls.length).toBe(sysCallCountBefore)
  })

  it('uses truthful transport gave-up copy on exhausted reconnect attempts', () => {
    const wsGaveUp = recoveryGaveUpMessage('websocket', 1006)
    expect(wsGaveUp).toContain('Connection to Hermes was lost (code 1006)')
    expect(wsGaveUp).not.toMatch(/Hermes stopped/)

    const procGaveUp = recoveryGaveUpMessage('process', 137)
    expect(procGaveUp).toContain('Hermes stopped (exit code 137)')

    expect(recoveryGaveUpActivity('websocket')).toBe(TRANSPORT_GAVE_UP_ACTIVITY)
    expect(recoveryGaveUpActivity('process')).toBe(BACKEND_GAVE_UP_ACTIVITY)
  })

  it('reconciles busy appropriately after reconnect: backend turn completed while disconnected', async () => {
    const appended: Msg[] = []
    const recoverSidRef = { current: null as string | null }
    const recoveryAtRef = { current: [] as number[] }
    const gaveUpRef = { current: false }
    const sysMock = vi.fn()
    const startMock = vi.fn()

    const gw = {
      isAttached: () => true,
      start: startMock,
      getLogTail: () => ''
    }

    // Step 1: Active turn in progress
    patchUiState({ busy: true, sessionKey: 'session-durable-reconnect', sid: 'session-reconnect-test', status: 'working…' })

    // Step 2: Transport loss occurs via production handler
    const exitHandler = createGatewayExitHandler({
      gaveUpRef,
      gw,
      recoverSidRef,
      recoveryAtRef,
      sys: sysMock
    })

    exitHandler(1006, { code: 1006, source: 'websocket' })

    expect(getUiState().busy).toBe(true)
    expect(recoverSidRef.current).toBe('session-durable-reconnect')

    // Step 3: Reconnect succeeds and gateway.ready arrives
    const resumeByIdMock = vi.fn((durableKey: string) => {
      // Successful resume clears recovery ref and updates sessionKey + runtime sid
      recoverSidRef.current = null
      patchUiState({
        busy: false,
        sessionKey: durableKey,
        sid: 'session-runtime-new',
        status: 'ready'
      })
      appended.push({
        kind: 'text',
        role: 'assistant',
        text: 'Turn completed while disconnected.'
      })
    })

    const ctx = {
      composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: vi.fn(), setInput: vi.fn() },
      gateway: {
        gw: { request: vi.fn() },
        rpc: vi.fn(async (method: string) => {
          if (method === 'commands.catalog') {return { pairs: [] }}

          return null
        })
      },
      session: {
        STARTUP_RESUME_ID: '',
        colsRef: { current: 80 },
        newSession: vi.fn(),
        recoverSidRef,
        resetSession: vi.fn(),
        resumeById: resumeByIdMock,
        setCatalog: vi.fn()
      },
      submission: { submitRef: { current: vi.fn() } },
      system: { bellOnComplete: false, sys: vi.fn() },
      transcript: { appendMessage: (m: Msg) => appended.push(m), panel: vi.fn(), setHistoryItems: vi.fn() },
      voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
    }

    const handler = createGatewayEventHandler(ctx as any)

    // Simulate gateway.ready event received after reconnect
    handler({
      type: 'gateway.ready',
      payload: { heartbeat: true }
    } as any)

    // Assert: resumeById was called with durable sessionKey (not runtime sid)
    await vi.waitFor(() => expect(resumeByIdMock).toHaveBeenCalledWith('session-durable-reconnect', undefined, 0, { mode: 'transport-recovery' }))
    expect(recoverSidRef.current).toBeNull()
    expect(getUiState().sessionKey).toBe('session-durable-reconnect')

    // Assert: busy reconciled to false and completed text received
    expect(getUiState().busy).toBe(false)
    expect(appended).toHaveLength(1)
    expect(appended[0].text).toBe('Turn completed while disconnected.')
  })

  it('reconciles busy appropriately after reconnect: in-progress turn continues streaming then completes', async () => {
    const appended: Msg[] = []
    const recoverSidRef = { current: null as string | null }
    const recoveryAtRef = { current: [] as number[] }
    const gaveUpRef = { current: false }
    const sysMock = vi.fn()
    const startMock = vi.fn()

    const gw = {
      isAttached: () => true,
      start: startMock,
      getLogTail: () => ''
    }

    // Step 1: Active turn in progress
    patchUiState({ busy: true, sessionKey: 'session-durable-live', sid: 'session-live-turn', status: 'working…' })

    // Step 2: Transport loss occurs via production handler
    const exitHandler = createGatewayExitHandler({
      gaveUpRef,
      gw,
      recoverSidRef,
      recoveryAtRef,
      sys: sysMock
    })

    exitHandler(1006, { code: 1006, source: 'websocket' })

    expect(getUiState().busy).toBe(true)
    expect(recoverSidRef.current).toBe('session-durable-live')

    // Step 3: Reconnect succeeds and gateway.ready arrives
    const resumeByIdMock = vi.fn((durableKey: string) => {
      // Successful resume clears recovery ref and updates sessionKey
      recoverSidRef.current = null
      patchUiState({
        busy: true,
        sessionKey: durableKey,
        sid: 'session-live-turn',
        status: 'working…'
      })
    })

    const ctx = {
      composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: vi.fn(), setInput: vi.fn() },
      gateway: {
        gw: { request: vi.fn() },
        rpc: vi.fn(async (method: string) => {
          if (method === 'commands.catalog') {return { pairs: [] }}

          return null
        })
      },
      session: {
        STARTUP_RESUME_ID: '',
        colsRef: { current: 80 },
        newSession: vi.fn(),
        recoverSidRef,
        resetSession: vi.fn(),
        resumeById: resumeByIdMock,
        setCatalog: vi.fn()
      },
      submission: { submitRef: { current: vi.fn() } },
      system: { bellOnComplete: false, sys: vi.fn() },
      transcript: { appendMessage: (m: Msg) => appended.push(m), panel: vi.fn(), setHistoryItems: vi.fn() },
      voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
    }

    const handler = createGatewayEventHandler(ctx as any)

    handler({
      type: 'gateway.ready',
      payload: { heartbeat: true }
    } as any)

    await vi.waitFor(() => expect(resumeByIdMock).toHaveBeenCalledWith('session-durable-live', undefined, 0, { mode: 'transport-recovery' }))
    expect(recoverSidRef.current).toBeNull()
    expect(getUiState().sessionKey).toBe('session-durable-live')
    expect(getUiState().busy).toBe(true)

    // Step 4: Streaming deltas arrive after reconnect
    handler({
      type: 'message.delta',
      session_id: 'session-live-turn',
      payload: { text: 'Continuing seamlessly...' }
    } as any)

    expect(getUiState().busy).toBe(true)

    // Step 5: Turn completion arrives from backend -> busy reconciled to false
    handler({
      type: 'message.complete',
      session_id: 'session-live-turn',
      payload: { session_id: 'session-live-turn', status: 'complete' }
    } as any)

    expect(getUiState().busy).toBe(false)
  })

  it('does not destroy recovery target prematurely if resumeById is interrupted or fails', async () => {
    const recoverSidRef = { current: 'session-durable-persisted' }
    const resumeByIdMock = vi.fn((_key: string) => {
      // Simulate failure or incomplete resume: do NOT clear recoverSidRef
      patchUiState({ status: 'ready' })
    })

    const ctx = {
      composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: vi.fn(), setInput: vi.fn() },
      gateway: {
        gw: { request: vi.fn() },
        rpc: vi.fn(async (method: string) => {
          if (method === 'commands.catalog') return { pairs: [] }
          return null
        })
      },
      session: {
        STARTUP_RESUME_ID: '',
        colsRef: { current: 80 },
        newSession: vi.fn(),
        recoverSidRef,
        resetSession: vi.fn(),
        resumeById: resumeByIdMock,
        setCatalog: vi.fn()
      },
      submission: { submitRef: { current: vi.fn() } },
      system: { bellOnComplete: false, sys: vi.fn() },
      transcript: { appendMessage: vi.fn(), panel: vi.fn(), setHistoryItems: vi.fn() },
      voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
    }

    const handler = createGatewayEventHandler(ctx as any)

    handler({
      type: 'gateway.ready',
      payload: { heartbeat: true }
    } as any)

    await vi.waitFor(() => expect(resumeByIdMock).toHaveBeenCalledWith('session-durable-persisted', undefined, 0, { mode: 'transport-recovery' }))
    // Target was NOT destroyed before or during the failed call
    expect(recoverSidRef.current).toBe('session-durable-persisted')
  })
})
