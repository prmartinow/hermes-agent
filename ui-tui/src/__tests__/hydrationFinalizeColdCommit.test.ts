import type { MainScreenStaticOutputLease } from '@hermes/ink'
import * as Ink from '@hermes/ink'
import { createElement, useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import { useSessionLifecycle, type UseSessionLifecycleOptions } from '../app/useSessionLifecycle.js'
import type * as EnvConfig from '../config/env.js'
import type { Msg } from '../types.js'

const acquire = vi.hoisted(() => vi.fn<() => Promise<MainScreenStaticOutputLease>>())

vi.mock('@hermes/ink', async importActual => ({
  ...await importActual<typeof Ink>(),
  acquireMainScreenStaticOutput: acquire
}))

vi.mock('../config/env.js', async importActual => ({
  ...await importActual<typeof EnvConfig>(),
  DASHBOARD_TUI_MODE: true,
  INLINE_MODE: true
}))

type Lifecycle = ReturnType<typeof useSessionLifecycle>

interface HarnessProps {
  gw: UseSessionLifecycleOptions['gw']
  onReady: (value: Lifecycle) => void
  rpc?: UseSessionLifecycleOptions['rpc']
}

function Harness({ gw, onReady, rpc }: HarnessProps) {
  const colsRef = useRef(80)
  const scrollRef = useRef(null)
  const [, setHistoryItems] = useState<Msg[]>([])
  const [, setLastUserMsg] = useState('')
  const [, setSessionStartedAt] = useState(0)
  const [, setStickyPrompt] = useState('')
  const [, setVoiceProcessing] = useState(false)
  const [, setVoiceRecording] = useState(false)

  const lifecycle = useSessionLifecycle({
    colsRef,
    composerActions: { setComposerTokens: () => {} },
    gw,
    panel: () => {},
    rpc: rpc ?? (async () => ({ provider_configured: true }) as any),
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setStickyPrompt,
    setVoiceProcessing,
    setVoiceRecording,
    sys: () => {}
  })

  useEffect(() => onReady(lifecycle), [lifecycle, onReady])

  return null
}

let instance: ReturnType<typeof Ink.renderSync> | undefined

beforeEach(() => {
  acquire.mockReset()
  resetUiState()
  resetTurnState()
  turnController.fullReset()
})

afterEach(() => {
  instance?.unmount()
  instance?.cleanup()
  instance = undefined
  vi.restoreAllMocks()
})

function eventBarriers() {
  const owners = new Map<string, string>()
  return {
    activateEventBarrier: vi.fn((sid: string, owner: string) => {owners.set(sid, owner)}),
    hasEventBarrier: (sid: string, owner: string) => owners.get(sid) === owner,
    cancelEventBarrier: vi.fn((sid: string, owner: string) => {
      if (owners.get(sid) === owner) {owners.delete(sid)}
    }),
    releaseEventBarrier: vi.fn((sid: string, owner: string) => {
      if (owners.get(sid) === owner) {owners.delete(sid)}
    })
  }
}

it.each(['prepare', 'release'] as const)('keeps hydration incomplete when %s fails without a second replay BEGIN', async failure => {
  const writes: string[] = []
  vi.spyOn(process.stdout, 'write').mockImplementation((chunk: any, ...args: any[]) => {
    writes.push(String(chunk))
    args.find(arg => typeof arg === 'function')?.()

    return true
  })

  const lease = {
    token: Symbol('cold-output'),
    beginStaticAppendSurface: vi.fn().mockResolvedValue(undefined),
    write: vi.fn().mockResolvedValue(undefined),
    prepareAppendHandoff: vi.fn(() => {
      if (failure === 'prepare') {throw new Error('handoff rejected')}
    }),
    release: vi.fn().mockRejectedValue(new Error('release rejected')),
    abort: vi.fn().mockResolvedValue({ requiresReconstruction: true }),
    reconstructAndRelease: vi.fn().mockResolvedValue(undefined)
  }

  acquire.mockResolvedValue(lease)

  const gw = {
    ...eventBarriers(),
    request: vi.fn(async (method: string) => method === 'session.resume'
      ? { session_id: 'cold', resumed: 'cold', messages: [] }
      : { has_more: false, messages: Array.from({ length: 125 }, (_, index) => ({
        role: index % 2 ? 'assistant' : 'user', text: `message ${index}`, row_id: index + 1
      })) })
  }

  let lifecycle: Lifecycle | undefined
  instance = Ink.renderSync(createElement(Harness, { gw: gw as any, onReady: value => { lifecycle = value } }))
  await lifecycle!.resumeById('cold')

  expect(lease.prepareAppendHandoff).toHaveBeenCalledOnce()
  expect(lease.release).toHaveBeenCalledTimes(failure === 'release' ? 1 : 0)
  expect(lease.abort).not.toHaveBeenCalled()
  expect(lease.reconstructAndRelease).toHaveBeenCalledOnce()
  expect(gw.cancelEventBarrier).toHaveBeenCalledWith('cold', expect.any(String))
  expect(gw.releaseEventBarrier).not.toHaveBeenCalled()
  expect(lifecycle!.coldHydrationIncompleteRef.current).toBe('cold')
  expect(writes.filter(value => value.includes('hermes-replay;begin;'))).toHaveLength(1)
  expect(writes.some(value => value.includes('hermes-replay;abort;'))).toBe(true)
  expect(writes.some(value => value.includes('hermes-replay;end;'))).toBe(false)
})

it.each(['resume', 'new', 'activate'] as const)('waits for dirty hydration settlement before %s replaces the session', async action => {
  let drainWrite!: () => void
  const writeDrained = new Promise<void>(resolve => { drainWrite = resolve })
  let finishReconstruction!: () => void
  const reconstructed = new Promise<void>(resolve => { finishReconstruction = resolve })

  const firstLease = {
    token: Symbol('first-output'),
    beginStaticAppendSurface: vi.fn().mockResolvedValue(undefined),
    write: vi.fn(() => writeDrained),
    prepareAppendHandoff: vi.fn(),
    release: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue({ requiresReconstruction: true }),
    reconstructAndRelease: vi.fn(() => reconstructed)
  }

  const replacementLease = {
    token: Symbol('replacement-output'),
    beginStaticAppendSurface: vi.fn().mockResolvedValue(undefined),
    write: vi.fn().mockResolvedValue(undefined),
    prepareAppendHandoff: vi.fn(),
    release: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue({ requiresReconstruction: false }),
    reconstructAndRelease: vi.fn().mockResolvedValue(undefined)
  }

  acquire.mockResolvedValueOnce(firstLease).mockResolvedValueOnce(replacementLease)

  const gw = {
    ...eventBarriers(),
    retireSession: vi.fn(),
    request: vi.fn(async (method: string, params: { session_id: string }) => ['session.resume', 'session.activate'].includes(method)
      ? { session_id: params.session_id, resumed: params.session_id, messages: [] }
      : { has_more: false, messages: params.session_id === 'first'
        ? Array.from({ length: 125 }, (_, index) => ({
          role: index % 2 ? 'assistant' : 'user', text: `message ${index}`, row_id: index + 1
        }))
        : [] })
  }

  const rpc = vi.fn(async (method: string) => {
    if (method === 'setup.status') {return { provider_configured: true }}

    if (method === 'session.create') {return { session_id: 'replacement', stored_session_id: 'replacement' }}

    return null
  })

  let lifecycle: Lifecycle | undefined
  instance = Ink.renderSync(createElement(Harness, { gw: gw as any, rpc: rpc as any, onReady: value => { lifecycle = value } }))

  const firstResume = lifecycle!.resumeById('first')
  await vi.waitFor(() => expect(firstLease.write).toHaveBeenCalledOnce())

  const replacementResume = action === 'new' ? lifecycle!.newSession()
    : action === 'activate' ? lifecycle!.activateLiveSession('replacement')
      : lifecycle!.resumeById('replacement')

  expect(acquire).toHaveBeenCalledOnce()
  expect(firstLease.reconstructAndRelease).not.toHaveBeenCalled()
  expect(rpc).not.toHaveBeenCalledWith('session.create', expect.anything())
  expect(gw.request).not.toHaveBeenCalledWith('session.activate', expect.anything())

  drainWrite()
  await vi.waitFor(() => expect(firstLease.reconstructAndRelease).toHaveBeenCalledOnce())
  expect(acquire).toHaveBeenCalledOnce()
  finishReconstruction()
  await Promise.all([firstResume, replacementResume])

  expect(acquire).toHaveBeenCalledTimes(action === 'resume' ? 2 : 1)
  expect(firstLease.abort).not.toHaveBeenCalled()
  expect(gw.releaseEventBarrier).not.toHaveBeenCalledWith('first', expect.any(String))

  if (action === 'resume') {expect(replacementLease.release).toHaveBeenCalledOnce()}

  if (action === 'new') {expect(rpc).toHaveBeenCalledWith('session.create', expect.anything())}

  if (action === 'activate') {expect(gw.request).toHaveBeenCalledWith('session.activate', { session_id: 'replacement' })}
})
