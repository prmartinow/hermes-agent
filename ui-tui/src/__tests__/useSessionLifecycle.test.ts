import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React, { useEffect, useRef, useState } from 'react'
import { renderSync } from '@hermes/ink'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { Msg } from '../types.js'
import {
  hydrateLiveSessionInflight,
  liveSessionInflightMessages,
  scheduleResumeScrollToBottom,
  signalFreshSessionBoundary,
  trimTail,
  useSessionLifecycle,
  type UseSessionLifecycleOptions,
  readActiveSessionFile,
  writeActiveSessionFile
} from '../app/useSessionLifecycle.js'

describe('fresh session boundary', () => {
  it('signals only when a live session is replaced by a different session', () => {
    const onFreshSessionStarted = vi.fn()

    expect(signalFreshSessionBoundary('old-session', 'new-session', onFreshSessionStarted)).toBe(true)
    expect(signalFreshSessionBoundary(null, 'first-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('same-session', 'same-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', null, onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', 'new-session')).toBe(false)
    expect(onFreshSessionStarted).toHaveBeenCalledOnce()
    expect(onFreshSessionStarted).toHaveBeenCalledWith('new-session')
  })
})

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })

  it('writes durable sessionKey to active_session_file', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('session-durable-key-xyz', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'session-durable-key-xyz' })
  })

  it('readActiveSessionFile parses session_id and session_key correctly', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    expect(readActiveSessionFile(path)).toBeNull()

    writeActiveSessionFile('session-abc', path)
    expect(readActiveSessionFile(path)).toBe('session-abc')
  })
})

describe('live session activation in-flight state', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ streaming: true })
  })

  it('keeps the in-flight user prompt in history and hydrates partial assistant text', () => {
    const inflight = { assistant: 'partial answer', streaming: true, user: 'write a long answer' }

    expect(liveSessionInflightMessages(inflight)).toEqual([{ role: 'user', text: 'write a long answer' }])

    hydrateLiveSessionInflight(inflight)

    expect(turnController.bufRef).toBe('partial answer')
    expect(getTurnState().streaming).toBe('partial answer')
  })

  it('ignores empty in-flight payloads', () => {
    expect(liveSessionInflightMessages({ assistant: '', streaming: false, user: '   ' })).toEqual([])

    hydrateLiveSessionInflight({ assistant: '', streaming: false, user: '' })

    expect(turnController.bufRef).toBe('')
    expect(getTurnState().streaming).toBe('')
  })

  it('does not duplicate in-flight user prompt if already present in messages', () => {
    const inflight = { assistant: 'partial answer', streaming: true, user: 'write a long answer' }
    const existing = [{ role: 'user' as const, text: 'write a long answer' }]

    expect(liveSessionInflightMessages(inflight, existing)).toEqual([])
  })
})

describe('resume scroll settle', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('re-snaps while sticky and stops when the user scrolls away', () => {
    vi.useFakeTimers()
    let sticky = true
    let lastManualScrollAt = 0
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => lastManualScrollAt,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80, 240]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    sticky = false
    lastManualScrollAt = Date.now() + 1
    vi.advanceTimersByTime(160)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    cancel()
  })

  it('cancels pending resume snaps', () => {
    vi.useFakeTimers()
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => true,
          scrollToBottom
        }
      } as any,
      [20]
    )

    cancel()
    vi.advanceTimersByTime(20)

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the immediate resume snap even before sticky state settles', () => {
    vi.useFakeTimers()
    let sticky = false
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    sticky = true
    cancel()
  })
})

describe('trimTail', () => {
  it('trims the last exchange even when trailing slash and system messages are present', () => {
    const items = [
      { role: 'user', text: 'turn 1' },
      { role: 'assistant', text: 'reply 1' },
      { role: 'user', text: 'turn 2' },
      { role: 'assistant', text: 'reply 2' },
      { kind: 'slash', role: 'system', text: '/undo' }
    ] as any

    const trimmed = trimTail(items, 1)
    expect(trimmed).toEqual([
      { role: 'user', text: 'turn 1' },
      { role: 'assistant', text: 'reply 1' }
    ])
  })

  it('supports multi-turn undo', () => {
    const items = [
      { role: 'user', text: 'turn 1' },
      { role: 'assistant', text: 'reply 1' },
      { role: 'user', text: 'turn 2' },
      { role: 'assistant', text: 'reply 2' },
      { role: 'user', text: 'turn 3' },
      { kind: 'trail', role: 'assistant', text: 'thinking' },
      { kind: 'diff', role: 'assistant', text: 'diff' },
      { role: 'assistant', text: 'reply 3' },
      { kind: 'slash', role: 'system', text: '/undo 2' }
    ] as any

    const trimmed = trimTail(items, 2)
    expect(trimmed).toEqual([
      { role: 'user', text: 'turn 1' },
      { role: 'assistant', text: 'reply 1' }
    ])
  })
})

describe('sessionKey tracking in useSessionLifecycle', () => {
  let dir = ''
  let activeFilePath = ''

  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-lifecycle-'))
    activeFilePath = join(dir, 'active.json')
    process.env.HERMES_TUI_ACTIVE_SESSION_FILE = activeFilePath
  })

  afterEach(() => {
    delete process.env.HERMES_TUI_ACTIVE_SESSION_FILE
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  function Harness(props: {
    onReady: (session: ReturnType<typeof useSessionLifecycle>) => void
    opts?: Partial<UseSessionLifecycleOptions>
  }) {
    const colsRef = useRef(80)
    const scrollRef = useRef(null)
    const [historyItems, setHistoryItems] = useState<Msg[]>([])
    const [lastUserMsg, setLastUserMsg] = useState('')
    const [sessionStartedAt, setSessionStartedAt] = useState(0)
    const [stickyPrompt, setStickyPrompt] = useState('')
    const [voiceProcessing, setVoiceProcessing] = useState(false)
    const [voiceRecording, setVoiceRecording] = useState(false)

    const session = useSessionLifecycle({
      colsRef,
      composerActions: { setComposerTokens: () => {} },
      gw: props.opts?.gw ?? ({} as any),
      panel: () => {},
      rpc: props.opts?.rpc ?? (async () => null),
      scrollRef,
      setHistoryItems,
      setLastUserMsg,
      setSessionStartedAt,
      setStickyPrompt,
      setVoiceProcessing,
      setVoiceRecording,
      sys: () => {},
      ...props.opts
    })

    useEffect(() => {
      props.onReady(session)
    }, [session])

    return null
  }

  it('startNewSession stores sessionKey and writes durable key to active session file', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      if (method === 'session.close') return { ok: true }
      if (method === 'session.create') {
        return {
          session_id: 'runtime-sid-create-1',
          stored_session_id: 'durable-key-create-1',
          info: { version: '1.0.0' }
        }
      }
      return null
    })

    renderSync(React.createElement(Harness, {
      onReady: s => { lifecycle = s },
      opts: { rpc }
    }))

    await lifecycle!.newSession()

    expect(getUiState().sid).toBe('runtime-sid-create-1')
    expect(getUiState().sessionKey).toBe('durable-key-create-1')
    expect(JSON.parse(readFileSync(activeFilePath, 'utf8'))).toEqual({
      session_id: 'durable-key-create-1'
    })
  })

  it('resumeById stores durable sessionKey and writes it to active session file', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    const recoverSessionKeyRef = { current: 'durable-key-resume-2' as string | null }

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    })

    const gw = {
      request: vi.fn(async (method: string) => {
        if (method === 'session.resume') {
          return {
            session_id: 'runtime-sid-resume-2',
            resumed: 'durable-key-resume-2',
            messages: []
          }
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any, recoverSessionKeyRef, rpc }
    }))

    lifecycle!.resumeById('durable-key-resume-2')

    await vi.waitFor(() => expect(getUiState().sid).toBe('runtime-sid-resume-2'))
    expect(getUiState().sessionKey).toBe('durable-key-resume-2')
    expect(recoverSessionKeyRef.current).toBeNull()
    expect(JSON.parse(readFileSync(activeFilePath, 'utf8'))).toEqual({
      session_id: 'durable-key-resume-2'
    })
  })

  it('resumeById does NOT destroy recoverSessionKeyRef on failure', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null
    const recoverSessionKeyRef = { current: 'durable-target-persist' as string | null }

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') return { provider_configured: true }
      return null
    })

    const gw = {
      request: vi.fn(async (method: string) => {
        if (method === 'session.resume') {
          throw new Error('network down')
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any, recoverSessionKeyRef, rpc }
    }))

    lifecycle!.resumeById('durable-target-persist')

    // On transport/network failure, status becomes 'disconnected' and recovery key is preserved
    await vi.waitFor(() => expect(getUiState().status).toBe('disconnected'))
    expect(recoverSessionKeyRef.current).toBe('durable-target-persist')
  })

  it('activateLiveSession stores durable sessionKey and writes it to active session file', async () => {
    let lifecycle: ReturnType<typeof useSessionLifecycle> | null = null

    const gw = {
      request: vi.fn(async (method: string) => {
        if (method === 'session.activate') {
          return {
            session_id: 'runtime-sid-activate-3',
            session_key: 'durable-key-activate-3',
            messages: []
          }
        }
        return null
      })
    }

    renderSync(React.createElement(Harness, {
      onReady: s => { lifecycle = s },
      opts: { gw: gw as any }
    }))

    lifecycle!.activateLiveSession('target-session-id')

    await vi.waitFor(() => expect(getUiState().sid).toBe('runtime-sid-activate-3'))
    expect(getUiState().sessionKey).toBe('durable-key-activate-3')
    expect(JSON.parse(readFileSync(activeFilePath, 'utf8'))).toEqual({
      session_id: 'durable-key-activate-3'
    })
  })
})
