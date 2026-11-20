import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import type { Msg } from '../types.js'

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn(async () => null) },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn().mockResolvedValue(undefined),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      sys: vi.fn()
    },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: (title: string, sections: any[]) =>
        appended.push({ kind: 'panel', panelData: { sections, title }, role: 'system', text: '' }),
      setHistoryItems: vi.fn()
    },
    voice: {
      setProcessing: vi.fn(),
      setRecording: vi.fn(),
      setVoiceEnabled: vi.fn()
    }
  }) as any

describe('turn.steer event handling', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ showReasoning: true })
  })
  it('appends steer prompt to streamSegments and preserves it upon turn completion', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: {}, type: 'message.start' } as any)
    onEvent({ payload: { text: 'analyzing files' }, type: 'reasoning.delta' } as any)
    onEvent({ payload: { text: 'please steer to auth.log' }, type: 'turn.steer' } as any)

    const segments = getTurnState().streamSegments
    expect(segments).toHaveLength(2)
    expect(segments[0]?.thinking).toBe('analyzing files')
    expect(segments[1]).toEqual({ role: 'user', text: 'please steer to auth.log' })

    onEvent({ payload: { text: 'Done checking auth.log' }, type: 'message.complete' } as any)
    expect(appended).toHaveLength(3)
    expect(appended[0]?.thinking).toBe('analyzing files')
    expect(appended[1]).toEqual({ role: 'user', text: 'please steer to auth.log' })
    expect(appended[2]).toMatchObject({ role: 'assistant', text: 'Done checking auth.log' })
  })
})
