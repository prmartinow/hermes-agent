import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

interface ListenerEntry {
  callback: (event: any) => void
  once: boolean
}

const { FakeWebSocket } = vi.hoisted(() => {
  class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    static instances: FakeWebSocket[] = []

    readyState = FakeWebSocket.CONNECTING
    sent: string[] = []
    readonly url: string
    private listeners = new Map<string, ListenerEntry[]>()

    constructor(url: string) {
      this.url = url
      FakeWebSocket.instances.push(this)
    }

    static reset() {
      FakeWebSocket.instances = []
    }

    addEventListener(type: string, callback: (event: any) => void, options?: unknown) {
      const once =
        typeof options === 'object' &&
        options !== null &&
        'once' in options &&
        Boolean((options as { once?: unknown }).once)

      const entries = this.listeners.get(type) ?? []

      entries.push({ callback, once })
      this.listeners.set(type, entries)
    }

    removeEventListener(type: string, callback: (event: any) => void) {
      const entries = this.listeners.get(type)

      if (!entries) {
        return
      }

      this.listeners.set(
        type,
        entries.filter(entry => entry.callback !== callback)
      )
    }

    send(payload: string) {
      if (this.readyState !== FakeWebSocket.OPEN) {
        throw new Error('socket not open')
      }

      this.sent.push(payload)
    }

    close(code = 1000, wasClean = false, reason = '') {
      if (this.readyState === FakeWebSocket.CLOSED) {
        return
      }

      this.readyState = FakeWebSocket.CLOSED
      this.emit('close', { code, wasClean, reason })
    }

    open() {
      this.readyState = FakeWebSocket.OPEN
      this.emit('open', {})
    }

    message(data: string) {
      this.emit('message', { data })
    }

    private emit(type: string, event: any) {
      const entries = [...(this.listeners.get(type) ?? [])]

      for (const entry of entries) {
        entry.callback(event)

        if (entry.once) {
          this.removeEventListener(type, entry.callback)
        }
      }
    }
  }

  return { FakeWebSocket }
})

vi.mock('undici', () => ({ WebSocket: FakeWebSocket }))

const lifecycle = vi.hoisted(() => [] as string[])

vi.mock('../lib/parentLog.js', () => ({ recordParentLifecycle: (line: string) => lifecycle.push(line) }))

import {
  RECONNECT_BASE_MS,
  RECONNECT_MAX_MS,
  redactUrl,
  WS_HEARTBEAT_DEAD_MS,
  WS_HEARTBEAT_INTERVAL_MS
} from '@hermes/shared/gateway-client'

import { GatewayClient } from '../gatewayClient.js'
import { applyLocale, resetLocale, t } from '../i18n/runtime.js'

describe('GatewayClient websocket attach mode', () => {
  const originalWebSocket = globalThis.WebSocket
  let originalGatewayUrl: string | undefined
  let originalSidecarUrl: string | undefined

  beforeEach(() => {
    originalGatewayUrl = process.env.HERMES_TUI_GATEWAY_URL
    originalSidecarUrl = process.env.HERMES_TUI_SIDECAR_URL
    FakeWebSocket.reset()
    ;(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    if (originalGatewayUrl === undefined) {
      delete process.env.HERMES_TUI_GATEWAY_URL
    } else {
      process.env.HERMES_TUI_GATEWAY_URL = originalGatewayUrl
    }

    if (originalSidecarUrl === undefined) {
      delete process.env.HERMES_TUI_SIDECAR_URL
    } else {
      process.env.HERMES_TUI_SIDECAR_URL = originalSidecarUrl
    }

    FakeWebSocket.reset()

    if (originalWebSocket) {
      globalThis.WebSocket = originalWebSocket
    } else {
      delete (globalThis as { WebSocket?: unknown }).WebSocket
    }
  })

  it("hands Ink's locale pack and crash-log breadcrumbs to the shared client", async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)

      const request = socket.sent.map(text => JSON.parse(text) as { id: number; method: string })
        .find(frame => frame.method === 'runtime.describe')!

      socket.message(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {
        session_create: { sources: ['tui'], parameters: ['source', 'request_id'] } } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(lifecycle.some(line => line.startsWith('[lifecycle] attached shared gateway '))).toBe(true)

      applyLocale('xx', { lang: 'xx', surface: 'tui', messages: {
        'canonical.launch.unsupportedOptions': 'XX-launch {0}', 'libText.gateway.closed': 'XX-closed' } })
      await expect(gw.request('session.create', { yolo: true })).rejects.toThrow('XX-launch yolo')
      const pending = gw.request('session.list', {})
      await vi.advanceTimersByTimeAsync(0)
      gw.kill()
      await expect(pending).rejects.toThrow('XX-closed')
    } finally { gw.kill(); resetLocale(); vi.useRealTimers() }
  })

  // Pins Ink's launch on the shared client: the exact session.create params (no client name, even to an
  // owner that takes one) and its TUI-worded refusals. Passes unchanged on the tree before the
  // shared client grew its source/client options.
  it('launches Ink sessions with the same frame and refusals', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const clients: GatewayClient[] = []

    const attach = async (sessionCreate: { parameters: string[]; sources: string[] }) => {
      const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
      clients.push(gw)
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances.at(-1)!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)
      const frames = () => socket.sent.map(text => JSON.parse(text) as { id: number; method: string; params: Record<string, unknown> })
      const describe = frames().find(frame => frame.method === 'runtime.describe')!
      socket.message(JSON.stringify({ jsonrpc: '2.0', id: describe.id, result: { session_create: sessionCreate } }))
      await vi.advanceTimersByTimeAsync(0)

      return { frames, gw }
    }

    const message = (promise: Promise<unknown>) => promise.then(() => '', (error: Error) => error.message)

    try {
      const { frames, gw } = await attach({ sources: ['cli', 'tui'], parameters: ['source', 'request_id', 'model', 'client'] })
      void gw.request('session.create', { model: 'm', cols: 80 }).catch(() => undefined)
      await vi.advanceTimersByTimeAsync(0)
      const create = frames().find(frame => frame.method === 'session.create')!
      expect(Object.keys(create.params)).toEqual(['model', 'source', 'request_id'])
      expect(create.params).toMatchObject({ model: 'm', source: 'tui' })
      expect(create.params.request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
      expect(await message(gw.request('session.create', { yolo: true }))).toBe('gateway does not support TUI launch options: yolo')

      const older = await attach({ sources: ['cli'], parameters: ['source', 'request_id'] })
      expect(await message(older.gw.request('session.create', {})))
        .toBe('gateway does not support tui session policy; update/restart the gateway')
    } finally { clients.forEach(gw => gw.kill()); vi.useRealTimers() }
  })

  it('publishes canonical readiness once after discovery and arms the negotiated heartbeat', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
    const events: any[] = []
    gw.on('event', event => events.push(event))

    try {
      gw.start()
      gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      expect(FakeWebSocket.instances).toHaveLength(1)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      socket.message(JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: { heartbeat: true } } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(events.filter(event => event.type === 'gateway.ready')).toHaveLength(0)
      // The wire's ready frame triggers the client.capabilities advertisement; discovery
      // (runtime.describe) is the other frame and the one readiness waits on.
      const sent = socket.sent.map(text => JSON.parse(text) as { id?: number; method: string })
      expect(sent.map(frame => frame.method).sort()).toEqual(['client.capabilities', 'runtime.describe'])
      const request = sent.find(frame => frame.method === 'runtime.describe')!
      socket.message(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {
        session_create: { sources: ['tui'], parameters: ['source', 'request_id'] }
      } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(events.filter(event => event.type === 'gateway.ready')).toHaveLength(1)

      await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_INTERVAL_MS)
      expect(JSON.parse(socket.sent.at(-1) ?? '{}')).toMatchObject({ method: 'gateway.ping' })
    } finally {
      gw.kill()
      expect(vi.getTimerCount()).toBe(0)
      vi.useRealTimers()
    }
  })

  it('/logs carries the attach lifecycle like main\'s sidecar spawn line (row 24)', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL

    const gw = new GatewayClient(async () => ({
      url: 'ws://127.0.0.1:9/api/ws?token=secret', protocols: [], instance_id: 'owner-1', profile_id: '/home/u/.hermes/profiles/work'
    }))

    try {
      gw.start()
      await vi.advanceTimersByTimeAsync(0)
      const tail = gw.getLogTail(20)
      expect(tail).toMatch(/^\[lifecycle\] attached shared gateway /m)
      expect(tail).toContain('profile=/home/u/.hermes/profiles/work')
      expect(tail).toContain('instance=owner-1')
      expect(tail).not.toContain('secret')
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it("boots the owner's profile skin: runtime.describe.skin rides the client-local ready (row 3)", async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
    const events: any[] = []
    gw.on('event', event => events.push(event))

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)

      const request = socket.sent.map(text => JSON.parse(text) as { id: number; method: string })
        .find(frame => frame.method === 'runtime.describe')!

      const skin = { name: 'ares', colors: { banner_title: '#C7A96B' }, branding: { prompt_symbol: '⚔' } }
      socket.message(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {
        session_create: { sources: ['tui'], parameters: ['source', 'request_id'] }, skin } }))
      await vi.advanceTimersByTimeAsync(0)

      const ready = events.filter(event => event.type === 'gateway.ready')
      expect(ready).toHaveLength(1)
      expect(ready[0].payload.skin).toEqual(skin)
    } finally { gw.kill(); vi.useRealTimers() }
  })

  it('remembers clarify-batch-v1 from runtime.describe and forgets it on reconnect to an owner without it', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))

    const open = async () => {
      const socket = FakeWebSocket.instances.at(-1)!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)

      return socket
    }

    const describe = async (socket: FakeWebSocket, capabilities?: string[]) => {
      const request = socket.sent.map(text => JSON.parse(text) as { id: number; method: string })
        .find(frame => frame.method === 'runtime.describe')!

      socket.message(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: {
        session_create: { sources: ['tui'], parameters: ['source', 'request_id'] }, capabilities } }))
      await vi.advanceTimersByTimeAsync(0)

      return socket
    }

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const first = await describe(await open(), ['clarify-batch-v1'])
      expect(gw.clarifyBatch).toBe(true)

      first.close()
      await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS)
      expect(FakeWebSocket.instances.at(-1)).not.toBe(first)
      const second = await open()
      // The previous owner's list must not outlive it while the new owner is still describing.
      expect(gw.clarifyBatch).toBe(false)
      await describe(second)
      expect(gw.clarifyBatch).toBe(false)
    } finally { gw.kill(); vi.useRealTimers() }
  })

  it('treats a failed runtime.describe as a transport failure: redial, re-describe, then ready (B2-21)', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
    const events: any[] = []
    gw.on('event', event => events.push(event))

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)
      const frames = (ws: InstanceType<typeof FakeWebSocket>) => ws.sent.map(text => JSON.parse(text) as { id: number; method: string; params: any })
      const first = frames(socket).find(frame => frame.method === 'runtime.describe')!
      socket.message(JSON.stringify({ jsonrpc: '2.0', id: first.id, error: { code: -32000, message: 'owner busy' } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(socket.readyState).toBe(FakeWebSocket.CLOSED)
      expect(events.some(event => event.type === 'gateway.reconnecting')).toBe(true)

      // The pending create redials too, and is answered once the next socket's describe lands.
      const created = gw.request<{ session_id: string }>('session.create', { model: 'm' })
      await vi.advanceTimersByTimeAsync(0)
      const next = FakeWebSocket.instances.at(-1)!
      expect(next).not.toBe(socket)
      next.open()
      await vi.advanceTimersByTimeAsync(0)

      for (const frame of frames(next).filter(f => f.method === 'runtime.describe')) {
        next.message(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result: {
          session_create: { sources: ['tui'], parameters: ['source', 'request_id', 'model'] } } }))
      }

      await vi.advanceTimersByTimeAsync(0)
      expect(events.filter(event => event.type === 'gateway.ready')).toHaveLength(1)
      const create = frames(next).find(frame => frame.method === 'session.create')!
      expect(create.params).toMatchObject({ source: 'tui', model: 'm' })
      next.message(JSON.stringify({ jsonrpc: '2.0', id: create.id, result: { session_id: 'fresh', info: {} } }))
      await expect(created).resolves.toMatchObject({ session_id: 'fresh' })
    } finally { gw.kill(); vi.useRealTimers() }
  })

  it('keeps discovery-only retries alive and delivers readiness to the mounted subscriber', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    const grant = { url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }
    const bootstrap = vi.fn().mockResolvedValueOnce(grant).mockRejectedValueOnce(new Error('owner stopped')).mockRejectedValueOnce(new Error('owner stopped')).mockResolvedValue(grant)
    const gw = new GatewayClient(bootstrap)
    const events: any[] = []
    gw.on('event', event => events.push(event))

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      FakeWebSocket.instances[0]!.open()
      await vi.advanceTimersByTimeAsync(0)
      FakeWebSocket.instances[0]!.close()
      await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS * 4)
      expect(bootstrap.mock.calls.map(args => args[0])).toEqual([true, false, false, false])
      const socket = FakeWebSocket.instances.at(-1)!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)
      const request = JSON.parse(socket.sent[0]!)
      socket.message(JSON.stringify({ id: request.id, result: { session_create: { sources: ['tui'], parameters: [] } } }))
      await vi.advanceTimersByTimeAsync(0)
      expect(events.some(event => event.type === 'gateway.ready')).toBe(true)
    } finally { gw.kill(); vi.useRealTimers() }
  })

  it('pins the launcher tool-progress mode (hermes --tui -v) on the created session before returning it', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    vi.stubEnv('HERMES_TUI_TOOL_PROGRESS', 'verbose')
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
    gw.on('event', () => undefined)

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)
      const frames = () => socket.sent.map(text => JSON.parse(text) as { id: number; method: string; params: any })

      const reply = (method: string, result: unknown) => {
        const frame = frames().filter(f => f.method === method).at(-1)!

        socket.message(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result }))
      }

      reply('runtime.describe', { session_create: { sources: ['tui'], parameters: ['source', 'request_id'] } })
      await vi.advanceTimersByTimeAsync(0)
      let settled = false

      const created = gw.request<{ session_id: string }>('session.create', {}).then(r => {
        settled = true

        return r
      })


      await vi.advanceTimersByTimeAsync(0)
      reply('session.create', { session_id: 'fresh', info: {} })
      await vi.advanceTimersByTimeAsync(0)
      expect(settled).toBe(false)
      expect(frames().at(-1)).toMatchObject({ method: 'config.set', params: { key: 'verbose', session_id: 'fresh', value: 'verbose' } })
      reply('config.set', { key: 'verbose', value: 'verbose', scope: 'session' })
      await expect(created).resolves.toMatchObject({ session_id: 'fresh' })
    } finally { gw.kill(); vi.unstubAllEnvs(); vi.useRealTimers() }
  })

  it('retries a create whose launch pin failed after the ACK under the same request_id (one session, pin reapplied)', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    vi.stubEnv('HERMES_TUI_TOOL_PROGRESS', 'verbose')
    const gw = new GatewayClient(async () => ({ url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }))
    gw.on('event', () => undefined)

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeWebSocket.instances[0]!
      socket.open()
      await vi.advanceTimersByTimeAsync(0)
      const frames = () => socket.sent.map(text => JSON.parse(text) as { id: number; method: string; params: any })
      const last = (method: string) => frames().filter(f => f.method === method).at(-1)!
      const answer = (method: string, body: object) => socket.message(JSON.stringify({ jsonrpc: '2.0', id: last(method).id, ...body }))
      // The owner keys a local session by request_id, exactly like create_local_session.
      const minted = new Map<string, string>()

      const ack = () => {
        const requestId = last('session.create').params.request_id as string
        minted.set(requestId, minted.get(requestId) ?? `orphan-${minted.size + 1}`)
        answer('session.create', { result: { session_id: minted.get(requestId), info: {} } })
      }

      answer('runtime.describe', { result: { session_create: { sources: ['tui'], parameters: ['source', 'request_id', 'model'] } } })
      await vi.advanceTimersByTimeAsync(0)

      // The caller (useSessionLifecycle) mints a fresh request_id on every attempt.
      const first = gw.request('session.create', { request_id: 'attempt-1', model: 'm' })
      const firstOutcome = first.catch((error: Error) => error)
      await vi.advanceTimersByTimeAsync(0)
      ack()
      await vi.advanceTimersByTimeAsync(0)
      answer('config.set', { error: { code: -32000, message: 'config refused' } })
      expect(await firstOutcome).toMatchObject({ message: 'config refused' })

      const retry = gw.request<{ session_id: string }>('session.create', { request_id: 'attempt-2', model: 'm' })
      await vi.advanceTimersByTimeAsync(0)
      ack()
      await vi.advanceTimersByTimeAsync(0)
      expect(last('config.set').params).toMatchObject({ key: 'verbose', session_id: 'orphan-1', value: 'verbose' })
      answer('config.set', { result: { key: 'verbose', value: 'verbose', scope: 'session' } })
      await expect(retry).resolves.toMatchObject({ session_id: 'orphan-1' })
      expect([...minted.values()]).toEqual(['orphan-1'])
    } finally { gw.kill(); vi.unstubAllEnvs(); vi.useRealTimers() }
  })

  it('re-ensures a crashed owner on a bounded number of reconnects, then stays discovery-only', async () => {
    vi.useFakeTimers()
    delete process.env.HERMES_TUI_GATEWAY_URL
    const grant = { url: 'ws://gateway.test/api/ws', protocols: [], instance_id: 'owner', profile_id: 'fixture' }
    const bootstrap = vi.fn().mockResolvedValueOnce(grant).mockRejectedValue(new Error('gateway absent: not ready'))
    const gw = new GatewayClient(bootstrap)

    try {
      gw.start(); gw.drain()
      await vi.advanceTimersByTimeAsync(0)
      FakeWebSocket.instances[0]!.open()
      await vi.advanceTimersByTimeAsync(0)
      FakeWebSocket.instances[0]!.close()
      // Five reconnects inside one recovery window.
      await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS * (1 + 2 + 4 + 8 + 16))
      expect(bootstrap.mock.calls.map(([start, recover]) => [start, Boolean(recover)])).toEqual([
        [true, false], [false, true], [false, true], [false, true], [false, false], [false, false]])
    } finally { gw.kill(); vi.useRealTimers() }
  })

  it('negotiates the canonical wire on an explicit attach whose listener advertises a session authority', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    const events: any[] = []

    gw.on('event', event => events.push(event))
    gw.start()
    gw.drain()
    const socket = FakeWebSocket.instances[0]!
    const frames = () => socket.sent.map(text => JSON.parse(text) as { id: number; method: string; params: any })

    socket.open()
    socket.message(JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready',
      payload: { skin: { name: 'mono' }, session_authority: true } } }))
    await vi.waitFor(() => expect(frames().some(frame => frame.method === 'runtime.describe')).toBe(true))
    expect(events.some(event => event.type === 'gateway.ready')).toBe(false)
    socket.message(JSON.stringify({ jsonrpc: '2.0', id: frames().find(f => f.method === 'runtime.describe')!.id,
      result: { session_create: { sources: ['tui'], parameters: ['source', 'request_id', 'model'] } } }))
    await vi.waitFor(() => expect(events.filter(event => event.type === 'gateway.ready')).toHaveLength(1))
    expect(events.find(event => event.type === 'gateway.ready').payload.skin).toEqual({ name: 'mono' })
    expect(gw.isCanonical).toBe(true)

    const created = gw.request<{ session_id: string }>('session.create', { cols: 80, model: 'm' })

    await vi.waitFor(() => expect(frames().some(frame => frame.method === 'session.create')).toBe(true))
    const create = frames().find(frame => frame.method === 'session.create')!

    expect(create.params).toMatchObject({ source: 'tui', model: 'm' })
    expect(create.params).not.toHaveProperty('cols')
    socket.message(JSON.stringify({ jsonrpc: '2.0', id: create.id, result: { session_id: 'fresh', info: {} } }))
    await expect(created).resolves.toMatchObject({ session_id: 'fresh' })
    gw.kill()
  })

  it('ignores queued events from a replaced gateway socket', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws'
    delete process.env.HERMES_TUI_SIDECAR_URL
    const gw = new GatewayClient()
    const events: string[] = []
    gw.on('event', ev => events.push(ev.type + ':' + (ev.payload?.text ?? '')))

    try {
      gw.start(); const oldSocket = FakeWebSocket.instances[0]!; oldSocket.open()
      gw.drain(); await Promise.resolve()
      gw.start(); const newSocket = FakeWebSocket.instances.at(-1)!; newSocket.open()
      oldSocket.message(JSON.stringify({jsonrpc:'2.0',method:'event',params:{type:'message.delta',session_id:'fixture',payload:{text:'stale'}}}))
      newSocket.message(JSON.stringify({jsonrpc:'2.0',method:'event',params:{type:'message.delta',session_id:'fixture',payload:{text:'fresh'}}}))
      gw.drain()
      await vi.waitFor(() => expect(events.some(e => e.startsWith('message.delta:'))).toBe(true))
      expect(events.filter(e => e.startsWith('message.delta:'))).toEqual(['message.delta:fresh'])
    } finally { gw.kill() }
  })

  it('waits for websocket open and resolves RPC requests', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!
    const req = gw.request<{ ok: boolean }>('session.create', { cols: 80 })

    expect(gatewaySocket.sent).toHaveLength(0)
    gatewaySocket.open()
    await vi.waitFor(() => expect(gatewaySocket.sent).toHaveLength(1))

    const frame = JSON.parse(gatewaySocket.sent[0] ?? '{}') as { id: string; method: string }
    expect(frame.method).toBe('session.create')

    gatewaySocket.message(JSON.stringify({ id: frame.id, jsonrpc: '2.0', result: { ok: true } }))
    await expect(req).resolves.toEqual({ ok: true })

    gw.kill()
  })

  it('drains buffered events on a later microtask, not synchronously inside drain()', async () => {
    // Regression for #36658: in attach mode the already-running gateway
    // replays `gateway.ready` the instant the socket connects, so it lands in
    // bufferedEvents BEFORE the consumer's mount-time subscribe effect runs.
    // If drain() emitted those synchronously, the gateway.ready handler's
    // setState cascade would run inside React's first commit -> "Too many
    // re-renders" (#301). drain() must defer the buffered flush so the first
    // commit settles first.
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    // Server replays ready BEFORE the consumer subscribes (attach-mode timing):
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
    )

    const order: string[] = []

    gw.on('event', ev => order.push(`event:${ev.type}`))
    gw.drain()
    order.push('after-drain')

    // Buffered event must NOT have fired synchronously inside drain():
    expect(order).toEqual(['after-drain'])

    // ...and must arrive on the next microtask.
    await vi.waitFor(() => expect(order).toContain('event:gateway.ready'))
    expect(order).toEqual(['after-drain', 'event:gateway.ready'])

    gw.kill()
  })

  it('preserves FIFO order when a live event arrives before the deferred flush', async () => {
    // #36658 hardening: `subscribed` must NOT flip synchronously in drain().
    // A live event delivered in the window between drain() returning and the
    // deferred microtask running must still queue BEHIND the chronologically
    // earlier buffered events, not jump ahead of them.
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    // Buffered first (replayed on connect, before subscribe):
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
    )

    const order: string[] = []

    gw.on('event', ev => order.push(ev.type))
    gw.drain()

    // A LIVE event arrives synchronously in the post-drain / pre-microtask gap:
    gatewaySocket.message(
      JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'session.info', payload: {} } })
    )

    // Nothing emitted yet (subscribed stays false until the microtask):
    expect(order).toEqual([])

    await vi.waitFor(() => expect(order.length).toBe(2))
    // FIFO preserved: the earlier-buffered gateway.ready precedes the live one.
    expect(order).toEqual(['gateway.ready', 'session.info'])

    gw.kill()
  })

  it('mirrors event frames to sidecar websocket when configured', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = 'ws://gateway.test/api/pub?token=abc&channel=demo'

    const gw = new GatewayClient()
    const seen: string[] = []

    gw.on('event', ev => seen.push(ev.type))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const sidecarSocket = FakeWebSocket.instances[1]!

    sidecarSocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle so
    // the subsequent live event takes the synchronous publish path.
    await Promise.resolve()

    const eventFrame = JSON.stringify({
      jsonrpc: '2.0',
      method: 'event',
      params: { type: 'tool.start', payload: { tool_id: 't1' } }
    })

    gatewaySocket.message(eventFrame)

    expect(seen).toContain('tool.start')
    expect(sidecarSocket.sent).toContain(eventFrame)

    gw.kill()
  })

  it('publishes local dashboard-control events to the sidecar websocket', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = 'ws://gateway.test/api/pub?token=abc&channel=demo'

    const gw = new GatewayClient()
    const seen: string[] = []

    gw.on('event', ev => seen.push(ev.type))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const sidecarSocket = FakeWebSocket.instances[1]!

    sidecarSocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle.
    await Promise.resolve()

    gw.publishLocalEvent({
      payload: { reason: 'idle_exit_hotkey' },
      session_id: 'sid-old',
      type: 'dashboard.new_session_requested'
    })

    expect(seen).toContain('dashboard.new_session_requested')
    expect(JSON.parse(sidecarSocket.sent.at(-1) ?? '{}')).toEqual({
      jsonrpc: '2.0',
      method: 'event',
      params: {
        payload: { reason: 'idle_exit_hotkey' },
        session_id: 'sid-old',
        type: 'dashboard.new_session_requested'
      }
    })

    gw.kill()
  })

  it('emits exit when attached websocket closes', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    const exits: Array<null | number> = []

    gw.on('exit', code => exits.push(code))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()
    // drain() flips `subscribed` on a microtask now (#36658); let it settle so
    // the close below takes the synchronous exit path.
    await Promise.resolve()
    gatewaySocket.close(1011)

    expect(exits).toEqual([1011])
  })

  it('emits exit with websocket context, close code, and initiator on socket 1006 drop', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://127.0.0.1:9119/api/ws?token=secret123'
    const gw = new GatewayClient()
    const exitEvents: Array<{ code: null | number; context?: any }> = []

    gw.on('exit', (code, context) => exitEvents.push({ code, context }))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    gw.drain()
    await Promise.resolve()

    expect(gw.isAttached()).toBe(true)

    // Simulate abnormal transport close 1006 from network/server side
    gatewaySocket.close(1006, false, '')

    expect(exitEvents).toHaveLength(1)
    expect(exitEvents[0].code).toBe(1006)
    expect(exitEvents[0].context).toEqual({
      code: 1006,
      source: 'websocket',
      reason: 'gateway websocket closed (1006)',
      clean: false,
      initiator: 'remote_or_network'
    })

    const logTail = gw.getLogTail(20)
    expect(logTail).toContain('[lifecycle] websocket close code=1006 clean=false ready=false initiator=remote_or_network')
    expect(logTail).toContain('[lifecycle] transport exit code=1006 reason=gateway websocket closed (1006) source=websocket')
    expect(logTail).toContain('[lifecycle] event-loop delay')
    // Verify private IP and token are not leaked in log tail
    expect(logTail).not.toContain('secret123')
    expect(logTail).not.toContain('127.0.0.1')
    expect(redactUrl('ws://127.0.0.1:9119/api/ws?token=secret123')).toBe('ws://[redacted-ip]:9119/api/ws?***')
    expect(redactUrl('ws://localhost:9119/api/ws?token=secret123')).toBe('ws://[redacted-ip]:9119/api/ws?***')
    const privateHost = [192, 168, 1, 50].join('.')
    expect(redactUrl(`wss://${privateHost}:8000/api/ws`)).toBe('wss://[redacted-ip]:8000/api/ws')

    gw.kill()
  })

  it('records heartbeat_timeout initiator when heartbeat failure forces close', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    const contexts: any[] = []
    gw.on('exit', (_code, context) => contexts.push(context))
    gw.start()

    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    gw.drain()
    await Promise.resolve()

    // Trigger onHeartbeatFailure directly
    ;(gw as any).onHeartbeatFailure()

    expect(contexts).toHaveLength(1)
    expect(contexts[0].source).toBe('websocket')
    expect(contexts[0].initiator).toBe('heartbeat_timeout')
    expect(gw.getLogTail(20)).toContain('initiator=heartbeat_timeout')

    gw.kill()
  })

  it('initializes and cleans up event loop delay monitor without leaking', () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    gw.start()

    const summary = gw.getLoopDelaySummary()
    expect(summary).not.toBeNull()
    expect(summary).toHaveProperty('meanMs')
    expect(summary).toHaveProperty('maxMs')
    expect(summary).toHaveProperty('p99Ms')

    gw.kill()
    expect(gw.getLoopDelaySummary()).toBeNull()
  })

  it('rejects pending RPCs with websocket wording when the attached socket closes', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()

    const req = gw.request('session.create', {})
    await vi.waitFor(() => expect(gatewaySocket.sent.length).toBeGreaterThan(0))

    gatewaySocket.close(1011)

    await expect(req).rejects.toThrow()
  })

  it('rejects pending RPCs when kill() closes the attached websocket', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!

    gatewaySocket.open()
    gw.drain()

    const req = gw.request('session.create', {})
    await vi.waitFor(() => expect(gatewaySocket.sent.length).toBeGreaterThan(0))

    gw.kill('test.shutdown')

    await expect(req).rejects.toThrow()
  })

  it('reattaches when HERMES_TUI_GATEWAY_URL rotates between requests', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway-old.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const firstSocket = FakeWebSocket.instances[0]!

    firstSocket.open()
    gw.drain()

    const stale = gw.request('session.create', {})
    await vi.waitFor(() => expect(firstSocket.sent.length).toBeGreaterThan(0))

    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway-new.test/api/ws?token=xyz'
    const next = gw.request('session.create', {})

    await expect(stale).rejects.toThrow(t('libText.gateway.attachUrlChanged'))
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))

    const secondSocket = FakeWebSocket.instances[1]!
    expect(secondSocket.url).toContain('gateway-new.test')

    secondSocket.open()
    await vi.waitFor(() => expect(secondSocket.sent.length).toBeGreaterThan(0))

    const frame = JSON.parse(secondSocket.sent[0] ?? '{}') as { id: string }
    secondSocket.message(JSON.stringify({ id: frame.id, jsonrpc: '2.0', result: { ok: true } }))

    await expect(next).resolves.toEqual({ ok: true })
    gw.kill()
  })

  it('surfaces JSON-RPC error code and data to callers (shared error mapping)', async () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    gw.start()
    const socket = FakeWebSocket.instances[0]!

    socket.open()
    const req = gw.request('projects.create', {})
    await vi.waitFor(() => expect(socket.sent.length).toBeGreaterThan(0))

    const frame = JSON.parse(socket.sent[0] ?? '{}') as { id: string }
    socket.message(
      JSON.stringify({
        error: { code: -32601, data: { method: 'projects.create' }, message: 'unknown method: projects.create' },
        id: frame.id,
        jsonrpc: '2.0'
      })
    )

    await expect(req).rejects.toMatchObject({
      code: -32601,
      data: { method: 'projects.create' },
      message: 'unknown method: projects.create'
    })
    gw.kill()
  })

  it('uses the undici WebSocket fallback when global WebSocket is unavailable', () => {
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=hunter2&channel=secret'
    delete (globalThis as { WebSocket?: unknown }).WebSocket

    const gw = new GatewayClient()

    gw.start()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0]?.url).toBe('ws://gateway.test/api/ws?token=hunter2&channel=secret')

    gw.kill()
  })

  it('redacts attach URL secrets when the WebSocket constructor throws', () => {
    const secretUrl = 'ws://gateway.test/api/ws?token=hunter2&channel=secret'

    process.env.HERMES_TUI_GATEWAY_URL = secretUrl
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        throw new TypeError(`Invalid URL: ${url}`)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    gw.drain()

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('channel=secret')
    expect(tail).not.toContain(secretUrl)
    expect(tail).toContain('ws://gateway.test/api/ws?***')

    gw.kill()
  })

  it('redacts sidecar URL secrets when the WebSocket constructor throws', async () => {
    const sidecarUrl = 'ws://gateway.test/api/pub?token=hunter2&channel=secret'

    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    process.env.HERMES_TUI_SIDECAR_URL = sidecarUrl
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingSidecarWebSocket extends FakeWebSocket {
      constructor(url: string) {
        if (url.includes('/api/pub')) {
          throw new TypeError(`Invalid URL: ${url}`)
        }

        super(url)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    const gatewaySocket = FakeWebSocket.instances[0]!
    gatewaySocket.open()
    await vi.waitFor(() => expect(gw.getLogTail(20)).toContain('[sidecar] failed to connect'))

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('channel=secret')
    expect(tail).not.toContain(sidecarUrl)
    expect(tail).toContain('ws://gateway.test/api/pub?***')

    gw.kill()
  })

  it('redacts user-info credentials even on URLs the WHATWG parser rejects', () => {
    // Port 99999 is outside the WHATWG URL parser's valid 0–65535
    // range and survives `.trim()`, so the fixture deterministically
    // exercises `redactUrl()`'s fallback branch across Node versions.
    // (An earlier `%zz` user-info fixture did NOT actually throw in
    // recent Node — WHATWG accepts malformed percent escapes there —
    // which silently routed the test through the structured-URL path.)
    const fixture = 'ws://alice:hunter2@gateway.test:99999/api/ws?token=secret'
    expect(() => new URL(fixture)).toThrow()

    process.env.HERMES_TUI_GATEWAY_URL = fixture
    ;(globalThis as { WebSocket?: unknown }).WebSocket = class ThrowingWebSocket extends FakeWebSocket {
      constructor(url: string) {
        throw new TypeError(`Invalid URL: ${url}`)
      }
    } as unknown as typeof WebSocket

    const gw = new GatewayClient()

    gw.start()
    gw.drain()

    const tail = gw.getLogTail(20)
    expect(tail).not.toContain('alice')
    expect(tail).not.toContain('hunter2')
    expect(tail).not.toContain('token=secret')

    gw.kill()
  })

  it('keeps a healthy idle websocket open when heartbeat acknowledgements arrive (issue #32997)', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    try {
      gw.start()
      const socket = FakeWebSocket.instances[0]!

      socket.open()
      socket.message(
        JSON.stringify({
          jsonrpc: '2.0',
          method: 'event',
          params: { type: 'gateway.ready', payload: { heartbeat: true } }
        })
      )
      // A live gateway answers every ping (tui_gateway/ws.py replies inline);
      // the shared channel counts any inbound frame as liveness, so a socket
      // whose pings keep getting acked must never trip the deadline.
      const acked: string[] = []

      const ackPings = () => {
        for (const raw of socket.sent) {
          const frame = JSON.parse(raw) as { id: string; method: string }

          if (frame.method === 'gateway.ping' && !acked.includes(frame.id)) {
            acked.push(frame.id)
            socket.message(JSON.stringify({ id: frame.id, jsonrpc: '2.0', result: { ok: true } }))
          }
        }
      }

      await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_INTERVAL_MS)
      expect(JSON.parse(socket.sent.at(-1) ?? '{}')).toMatchObject({ method: 'gateway.ping' })

      for (let elapsed = 0; elapsed < WS_HEARTBEAT_DEAD_MS * 2; elapsed += WS_HEARTBEAT_INTERVAL_MS) {
        ackPings()
        await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_INTERVAL_MS)
      }

      expect(acked.length).toBeGreaterThan(2)
      expect(socket.readyState).toBe(FakeWebSocket.OPEN)
      expect(FakeWebSocket.instances).toHaveLength(1)
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it('auto-reconnects after a missing heartbeat acknowledgement (issue #32997)', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    try {
      gw.start()
      const first = FakeWebSocket.instances[0]!

      first.open()
      first.message(
        JSON.stringify({
          jsonrpc: '2.0',
          method: 'event',
          params: { type: 'gateway.ready', payload: { heartbeat: true } }
        })
      )
      await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_INTERVAL_MS)
      expect(JSON.parse(first.sent.at(-1) ?? '{}')).toMatchObject({ method: 'gateway.ping' })
      await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_DEAD_MS + WS_HEARTBEAT_INTERVAL_MS)
      await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
      expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2)
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it('does not heartbeat an older backend that omits the capability', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    try {
      gw.start()
      const socket = FakeWebSocket.instances[0]!

      socket.open()
      socket.message(
        JSON.stringify({
          jsonrpc: '2.0',
          method: 'event',
          params: { type: 'gateway.ready', payload: {} }
        })
      )
      await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_DEAD_MS + WS_HEARTBEAT_INTERVAL_MS)
      expect(socket.readyState).toBe(FakeWebSocket.OPEN)
      const methods = socket.sent.map(text => (JSON.parse(text) as { method: string }).method)

      expect(methods).not.toContain('gateway.ping')
      expect(FakeWebSocket.instances).toHaveLength(1)
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it('does not double-reconnect when the exit subscriber restarts immediately', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()

    try {
      gw.on('exit', () => gw.start())
      gw.start()
      const first = FakeWebSocket.instances[0]!

      first.open()
      gw.drain()
      await Promise.resolve()
      first.close(1011)

      expect(FakeWebSocket.instances).toHaveLength(2)
      await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
      expect(FakeWebSocket.instances).toHaveLength(2)
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it('keeps delivering events to the mounted subscriber across reconnects, with growing backoff (#111594)', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    const ready: number[] = []
    const delays: number[] = []

    const readyFrame = JSON.stringify({
      jsonrpc: '2.0',
      method: 'event',
      params: { payload: {}, type: 'gateway.ready' }
    })

    gw.on('event', ev => {
      if (ev.type === 'gateway.ready') {
        ready.push(FakeWebSocket.instances.length)
      }

      if (ev.type === 'gateway.reconnecting') {
        delays.push(ev.payload.delay_ms)
      }
    })

    try {
      gw.start()
      gw.drain()
      await Promise.resolve()
      FakeWebSocket.instances[0]!.open()
      FakeWebSocket.instances[0]!.message(readyFrame)
      expect(ready).toEqual([1])

      // Two failed reconnects: the renderer drain()ed once on mount, so each
      // transport generation must keep emitting live (not re-buffer), and the
      // attempt counter must survive start() so the delay keeps growing.
      FakeWebSocket.instances[0]!.close(1006)
      await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS)
      FakeWebSocket.instances.at(-1)!.close(1006)
      await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS)
      FakeWebSocket.instances.at(-1)!.close(1006)
      await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS)
      expect(delays).toHaveLength(3)
      expect(delays[2]!).toBeGreaterThan(delays[0]!)

      const last = FakeWebSocket.instances.at(-1)!

      last.open()
      last.message(readyFrame)
      expect(ready).toEqual([1, FakeWebSocket.instances.length])
    } finally {
      gw.kill()
      vi.useRealTimers()
    }
  })

  it('does not auto-reconnect after an intentional kill() (issue #32997)', async () => {
    vi.useFakeTimers()
    process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
    const gw = new GatewayClient()
    gw.start()
    FakeWebSocket.instances[0]!.open()
    gw.kill() // sets disposed
    await vi.advanceTimersByTimeAsync(WS_HEARTBEAT_DEAD_MS + RECONNECT_MAX_MS + 1000)
    expect(FakeWebSocket.instances.length).toBe(1) // no reconnect attempted
    vi.useRealTimers()
  })

  describe('lifecycle recovery and consumer readiness', () => {
    it('delivers ready, session completion, and server request exactly once after reconnect without remount', async () => {
      process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
      delete process.env.HERMES_TUI_SIDECAR_URL
      const gw = new GatewayClient()

      const events: string[] = []
      const requests: string[] = []
      const exits: number[] = []

      // 1. Register listeners on mount
      gw.on('event', ev => events.push(ev.type))
      gw.on('request', req => {
        requests.push(req.method)
        req.respond({ ok: true })
      })
      gw.on('exit', code => exits.push(code ?? -1))

      // 2. Drain on mount
      gw.drain()
      await Promise.resolve()

      try {
        // 3. First transport connects and sends ready
        gw.start()
        const socket1 = FakeWebSocket.instances[0]!
        socket1.open()
        socket1.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        )
        await vi.waitFor(() => expect(events).toEqual(['gateway.ready']))

        // 4. Transport disconnects
        socket1.close(1006, false, 'abnormal drop')
        expect(exits).toEqual([1006])

        // 5. Reconnect WITHOUT remount (no new listeners, no gw.drain() call)
        gw.start()
        const socket2 = FakeWebSocket.instances.at(-1)!
        expect(socket2).not.toBe(socket1)
        socket2.open()

        // 6. Second transport sends ready, session completion, and a server request
        socket2.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        )
        socket2.message(
          JSON.stringify({
            jsonrpc: '2.0',
            method: 'event',
            params: { type: 'session.complete', payload: { session_id: 's1' } }
          })
        )
        socket2.message(
          JSON.stringify({
            jsonrpc: '2.0',
            id: 'srq-42',
            method: 'approval',
            params: { command: 'echo recovery' }
          })
        )

        // 7. Ensure delivered to consumer exactly once (not lost or indefinitely buffered)
        await vi.waitFor(() => expect(events).toEqual(['gateway.ready', 'gateway.reconnecting', 'gateway.ready', 'session.complete']))
        expect(requests).toEqual(['approval'])
      } finally {
        gw.kill()
      }
    })

    it('handles synchronous listener-triggered gw.start on exit and scheduled reconnect without duplicate or indefinite buffering', async () => {
      vi.useFakeTimers()
      process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
      const gw = new GatewayClient()

      const events: string[] = []
      let exitCount = 0
      let autoRestartOnExit = true

      gw.on('event', ev => events.push(ev.type))
      gw.on('exit', () => {
        exitCount += 1

        if (autoRestartOnExit) {
          gw.start()
        }
      })
      gw.drain()
      await Promise.resolve()

      try {
        gw.start()
        const s1 = FakeWebSocket.instances[0]!
        s1.open()
        s1.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        )
        expect(events).toEqual(['gateway.ready'])

        // Synchronous start() triggered on exit
        s1.close(1011)
        expect(exitCount).toBe(1)
        expect(FakeWebSocket.instances).toHaveLength(2)

        // Scheduled reconnect timer must be cancelled by the synchronous start()
        await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
        expect(FakeWebSocket.instances).toHaveLength(2)

        const s2 = FakeWebSocket.instances[1]!
        s2.open()
        s2.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        )
        expect(events).toEqual(['gateway.ready', 'gateway.reconnecting', 'gateway.ready'])

        // Second disconnect: test scheduled reconnect path
        autoRestartOnExit = false
        s2.close(1006)
        expect(exitCount).toBe(2)
        expect(FakeWebSocket.instances).toHaveLength(2)

        // Advance timer to trigger scheduled reconnect
        await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
        expect(FakeWebSocket.instances).toHaveLength(3)

        const s3 = FakeWebSocket.instances[2]!
        s3.open()
        s3.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
        )
        s3.message(
          JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'message.delta', payload: { text: 'ok' } } })
        )
        expect(events).toEqual(['gateway.ready', 'gateway.reconnecting', 'gateway.ready', 'gateway.reconnecting', 'gateway.ready', 'message.delta'])
      } finally {
        gw.kill()
        vi.useRealTimers()
      }
    })

    it('excludes stale previous transport events while delivering fresh ones without remount drain', async () => {
      process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
      const gw = new GatewayClient()
      const deltas: string[] = []

      gw.on('event', ev => {
        if (ev.type === 'message.delta') {
          deltas.push((ev.payload as { text?: string })?.text ?? '')
        }
      })
      gw.drain()
      await Promise.resolve()

      try {
        gw.start()
        const s1 = FakeWebSocket.instances[0]!
        s1.open()

        gw.start()
        const s2 = FakeWebSocket.instances.at(-1)!
        expect(s2).not.toBe(s1)
        s2.open()

        // Old socket emits stale frame after replacement
        s1.message(
          JSON.stringify({
            jsonrpc: '2.0',
            method: 'event',
            params: { type: 'message.delta', payload: { text: 'stale-old' } }
          })
        )
        // New socket emits fresh frame
        s2.message(
          JSON.stringify({
            jsonrpc: '2.0',
            method: 'event',
            params: { type: 'message.delta', payload: { text: 'fresh-new' } }
          })
        )

        await vi.waitFor(() => expect(deltas).toEqual(['fresh-new']))
      } finally {
        gw.kill()
      }
    })

    it('preserves initial startup prelistener buffering and pending exit before mount drain', async () => {
      process.env.HERMES_TUI_GATEWAY_URL = 'ws://gateway.test/api/ws?token=abc'
      const gw = new GatewayClient()

      // Start before any listeners or drain
      gw.start()
      const s1 = FakeWebSocket.instances[0]!
      s1.open()

      // Send events and request before listener attached
      s1.message(
        JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
      )
      s1.message(
        JSON.stringify({
          jsonrpc: '2.0',
          id: 'srq-init',
          method: 'clarify',
          params: { query: 'pre-mount question' }
        })
      )
      // Socket drops before listeners attached -> pendingExit recorded
      s1.close(1006, false, 'early drop')

      const events: string[] = []
      const requests: string[] = []
      const exits: number[] = []

      // Now consumer mounts: attaches listeners and calls drain
      gw.on('event', ev => events.push(ev.type))
      gw.on('request', req => {
        requests.push(req.method)
        req.respond({ ok: true })
      })
      gw.on('exit', code => exits.push(code ?? -1))

      // Nothing delivered before drain's microtask
      expect(events).toEqual([])
      expect(requests).toEqual([])
      expect(exits).toEqual([])

      gw.drain()
      await Promise.resolve()

      // Delivered in FIFO order upon mount drain
      expect(events).toEqual(['gateway.ready', 'gateway.reconnecting'])
      expect(requests).toEqual(['clarify'])
      expect(exits).toEqual([1006])

      gw.kill()
    })
  })
})
