import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { GatewayProvider } from '../app/gatewayContext.js'
import type { AppLayoutProps } from '../app/interfaces.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { $uiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import type { GatewayClient } from '../gatewayTypes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const gatewayStub = {
  gw: {
    request: () => new Promise<never>(() => {}),
    send: () => {}
  } as unknown as GatewayClient,
  rpc: (() => new Promise<never>(() => {})) as never
}

const baseLayoutProps: AppLayoutProps = {
  actions: {
    activateLiveSession: () => {},
    answerApproval: () => {},
    answerClarify: () => {},
    answerClarifyQuestion: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: () => Promise.resolve(null),
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  },
  composer: {
    cols: 120,
    compIdx: 0,
    completions: [],
    empty: true,
    handleTextPaste: () => null,
    input: '',
    inputBuf: [],
    pagerPageSize: 10,
    queueEditIdx: null,
    queuedDisplay: [],
    submit: () => {},
    updateInput: () => {},
    voiceRecordKey: 'f8'
  },
  mouseTracking: 'off',
  progress: { showProgressArea: false },
  status: {
    cwdLabel: '~/repo',
    goodVibesTick: 0,
    lastTurnEndedAt: 0,
    sessionStartedAt: 0,
    sessionTitle: 'test-session',
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: '',
    turnStartedAt: null,
    voiceLabel: ''
  },
  transcript: {
    historyItems: [],
    scrollRef: { current: null },
    virtualHistory: {
      bottomSpacer: 0,
      end: 0,
      measureRef: () => () => {},
      offsets: [],
      start: 0,
      topSpacer: 0
    },
    virtualRows: []
  }
}

function renderLayoutCleanText(): string {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let rendered = ''

  Object.assign(stdout, { columns: 120, isTTY: true, rows: 30 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', (chunk: Buffer) => {
    rendered += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(
      GatewayProvider,
      { value: gatewayStub },
      React.createElement(AppLayout, { ...baseLayoutProps })
    ),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  instance.unmount()
  instance.cleanup()

  return stripAnsi(rendered).replace(/\s+/g, '')
}

function buildMockContext() {
  return {
    composer: {
      dequeue: () => undefined,
      queueEditRef: { current: null },
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn(), send: vi.fn() },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: { current: 80 },
      newSession: vi.fn(),
      recoverSidRef: { current: null },
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      bellOnPrompt: false,
      stdout: { write: vi.fn() },
      sys: vi.fn()
    },
    transcript: {
      appendMessage: vi.fn(),
      panel: vi.fn(),
      setHistoryItems: vi.fn()
    },
    voice: {
      cancelVoiceRecording: vi.fn(),
      confirmVoiceDiscardRef: { current: false },
      discardVoiceRecording: vi.fn(),
      openVoiceDiscardModal: vi.fn(),
      pulsePlaybackVoiceWave: vi.fn(),
      resetVoicePlaybackWave: vi.fn(),
      setVoicePlaying: vi.fn(),
      setVoiceRecording: vi.fn(),
      voiceRecordingRef: { current: false },
      voiceStreamRef: { current: null }
    }
  } as any
}

describe('TUI Composer Gemini Account Badge Visibility', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
  })

  it('renders Gemini account badge above the TUI Composer prompt line when gemini_account is in ui.info', () => {
    patchUiState({
      sessionTitle: 'test-session',
      sid: 'sid-1',
      statusBar: 'top',
      status: 'ready',
      info: {
        model: 'gemini-3.8-flash-high',
        provider: 'gemini-oauth',
        gemini_account: 'prm'
      }
    })

    const clean = renderLayoutCleanText()
    // Asserts model and account badge are rendered on the status line directly above the composer input line
    expect(clean).toContain('gemini3.8flashhigh·prm')
    expect(clean).toContain('❯Try"')
  })

  it('renders plain model name without account badge when gemini_account is omitted', () => {
    patchUiState({
      sessionTitle: 'test-session',
      sid: 'sid-1',
      statusBar: 'top',
      status: 'ready',
      info: {
        model: 'gemini-3.8-flash-high',
        provider: 'gemini-oauth'
      }
    })

    const clean = renderLayoutCleanText()
    expect(clean).toContain('gemini3.8flashhigh')
    expect(clean).not.toContain('·prm')
  })

  it('updates the badge reactively above the composer when a session.info gateway event arrives', () => {
    patchUiState({
      sessionTitle: 'test-session',
      sid: 'sid-1',
      statusBar: 'top',
      status: 'ready',
      info: {
        model: 'gemini-3.8-flash-high',
        provider: 'gemini-oauth',
        gemini_account: 'pm'
      }
    })

    let clean = renderLayoutCleanText()
    expect(clean).toContain('gemini3.8flashhigh·pm')

    // Simulate real-time session.info JSON-RPC event arriving over the gateway connection
    const handler = createGatewayEventHandler(buildMockContext())

    handler({
      type: 'session.info',
      payload: {
        model: 'gemini-3.8-flash-high',
        provider: 'gemini-oauth',
        gemini_account: 'tnn'
      }
    } as any)

    expect($uiState.get().info?.gemini_account).toBe('tnn')
    clean = renderLayoutCleanText()
    expect(clean).toContain('gemini3.8flashhigh·tnn')
  })
})
