// The shared client with only a `connect` (a non-Ink client): it dials the grant, reads the owner's
// session.create contract before readiness, creates a session, and redials through `connect` with
// the recovery flag when the socket drops.
import { afterEach, expect, it, vi } from 'vitest'

import { GatewayClient, RECONNECT_BASE_MS } from './gateway-client'

class FakeSocket {
  static instances: FakeSocket[] = []
  readyState = 0
  sent: { id: number; method: string; params: Record<string, unknown> }[] = []
  private listeners = new Map<string, ((event: any) => void)[]>()

  constructor(readonly url: string, readonly protocols?: string[]) {
    FakeSocket.instances.push(this)
  }

  addEventListener(type: string, callback: (event: any) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback])
  }

  send(text: string) {
    this.sent.push(JSON.parse(text))
  }

  close(code = 1000) {
    this.readyState = 3
    this.emit('close', { code })
  }

  emit(type: string, event: unknown) {
    for (const callback of this.listeners.get(type) ?? []) {
      callback(event)
    }
  }

  open() {
    this.readyState = 1
    this.emit('open', {})
  }

  reply(method: string, result: unknown) {
    const request = this.sent.find(frame => frame.method === method)!

    this.emit('message', { data: JSON.stringify({ jsonrpc: '2.0', id: request.id, result }) })
  }
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  FakeSocket.instances = []
})

it('dials the grant, describes the owner, creates a session and redials through connect', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeSocket)

  const connect = vi.fn(async () => ({
    url: 'ws://127.0.0.1:9/api/ws', protocols: ['hermes-gateway-v1', 'hermes-gateway-ticket.t'],
    profile_id: '/home/p', instance_id: 'owner'
  }))

  const gw = new GatewayClient({ connect })
  const events: string[] = []

  gw.on('event', (event: { type: string }) => events.push(event.type))
  gw.start()
  gw.drain()
  await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(1))
  const socket = FakeSocket.instances[0]!

  expect(connect).toHaveBeenLastCalledWith(true, false)
  expect([socket.url, socket.protocols]).toEqual(['ws://127.0.0.1:9/api/ws', ['hermes-gateway-v1', 'hermes-gateway-ticket.t']])
  socket.open()
  await vi.waitFor(() => expect(socket.sent.some(frame => frame.method === 'runtime.describe')).toBe(true))
  socket.reply('runtime.describe', { session_create: { sources: ['tui'], parameters: ['request_id', 'source'] } })
  await vi.waitFor(() => expect(events).toContain('gateway.ready'))

  const created = gw.request<{ session_id: string }>('session.create', {})

  await vi.waitFor(() => expect(socket.sent.some(frame => frame.method === 'session.create')).toBe(true))
  expect(Object.keys(socket.sent.find(frame => frame.method === 'session.create')!.params)).toEqual(['source', 'request_id'])
  expect(socket.sent.find(frame => frame.method === 'session.create')!.params).toMatchObject({ source: 'tui' })
  socket.reply('session.create', { session_id: 'S1', messages: [], authority_epoch: 1 })
  expect((await created).session_id).toBe('S1')

  const pending = gw.request('session.list', {})

  await vi.waitFor(() => expect(socket.sent.some(frame => frame.method === 'session.list')).toBe(true))
  socket.close(1006)
  await expect(pending).rejects.toThrow('gateway websocket closed (1006)')
  await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
  expect(connect).toHaveBeenLastCalledWith(false, true)
  gw.kill()
})

it('replays a disconnected session before publishing held live events and readiness', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeSocket)
  const gw = new GatewayClient({
    connect: async () => { throw new Error('legacy listener does not use a ticket') },
    attachUrl: () => 'ws://127.0.0.1:9/api/ws'
  })
  const seen: { type: string; seq?: number }[] = []
  const frame = (socket: FakeSocket, params: unknown) =>
    socket.emit('message', { data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params }) })

  gw.on('event', (event: { type: string; seq?: number }) => seen.push(event))
  gw.start()
  gw.drain()
  await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(1))
  const first = FakeSocket.instances[0]!

  first.open()
  frame(first, { type: 'gateway.ready', payload: { replay_epoch: 'one' } })
  frame(first, { type: 'message.delta', session_id: 's', seq: 1, payload: { text: 'one' } })
  await vi.waitFor(() => expect(seen.some(event => event.seq === 1)).toBe(true))
  first.close(1006)
  await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
  await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(2))
  const second = FakeSocket.instances[1]!

  second.open()
  frame(second, { type: 'gateway.ready', payload: { replay_epoch: 'one' } })
  await vi.waitFor(() => expect(second.sent.some(request => request.method === 'session.events.since')).toBe(true))
  frame(second, { type: 'message.delta', session_id: 's', seq: 3, payload: { text: 'three' } })
  second.reply('session.events.since', {
    events: [{ type: 'message.delta', session_id: 's', seq: 2, payload: { text: 'two' } }], latest_seq: 3
  })
  await vi.waitFor(() => expect(seen.filter(event => event.type === 'gateway.ready')).toHaveLength(2))
  expect(seen.filter(event => event.seq !== undefined).map(event => event.seq)).toEqual([1, 2, 3])
  expect(seen.at(-1)?.type).toBe('gateway.ready')
  gw.kill()
})

it('reports a truncated replay before readiness so the viewer can recover history', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeSocket)
  const gw = new GatewayClient({
    connect: async () => { throw new Error('legacy listener does not use a ticket') },
    attachUrl: () => 'ws://127.0.0.1:9/api/ws'
  })
  const seen: { type: string; payload?: { reason?: string } }[] = []
  const frame = (socket: FakeSocket, params: unknown) =>
    socket.emit('message', { data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params }) })

  gw.on('event', (event: { type: string; payload?: { reason?: string } }) => seen.push(event))
  gw.start()
  gw.drain()
  await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(1))
  const first = FakeSocket.instances[0]!

  first.open()
  frame(first, { type: 'gateway.ready', payload: { replay_epoch: 'one' } })
  frame(first, { type: 'message.delta', session_id: 's', seq: 1, payload: { text: 'one' } })
  await vi.waitFor(() => expect(gw.getSeqWatermarks().s).toBe(1))
  first.close(1006)
  await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS)
  const second = FakeSocket.instances[1]!

  second.open()
  frame(second, { type: 'gateway.ready', payload: { replay_epoch: 'one' } })
  await vi.waitFor(() => expect(second.sent.some(request => request.method === 'session.events.since')).toBe(true))
  second.reply('session.events.since', { events: [], truncated: true, latest_seq: 10 })
  await vi.waitFor(() => expect(seen.filter(event => event.type === 'gateway.ready')).toHaveLength(2))
  expect(seen.slice(-2)).toMatchObject([
    { type: 'gateway.replay_gap', payload: { reason: 'truncated' } }, { type: 'gateway.ready' }
  ])
  gw.kill()
})

// A third-party client launches with its own source and name; the name rides only when the owner lists it.
async function createdFrame(options: { source?: 'cli'; client?: { name: string } }, parameters: string[]) {
  vi.stubGlobal('WebSocket', FakeSocket)
  const connect = async () => ({ url: 'ws://127.0.0.1:9/api/ws', protocols: [], profile_id: '/home/p', instance_id: 'owner' })
  const gw = new GatewayClient({ connect, ...options })

  gw.start()
  gw.drain()
  await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(1))
  const socket = FakeSocket.instances[0]!

  socket.open()
  await vi.waitFor(() => expect(socket.sent.some(frame => frame.method === 'runtime.describe')).toBe(true))
  socket.reply('runtime.describe', { session_create: { sources: ['cli', 'tui'], parameters } })
  void gw.request('session.create', {}).catch(() => undefined)
  await vi.waitFor(() => expect(socket.sent.some(frame => frame.method === 'session.create')).toBe(true))
  const { request_id: _id, ...params } = socket.sent.find(frame => frame.method === 'session.create')!.params
  gw.kill()

  return params
}

it('puts the source and client options on the session.create frame', async () => {
  const client = { name: 'example-client' }

  expect(await createdFrame({ source: 'cli', client }, ['source', 'request_id', 'client'])).toEqual({ source: 'cli', client })
  FakeSocket.instances = []
  expect(await createdFrame({ source: 'cli', client }, ['source', 'request_id'])).toEqual({ source: 'cli' })
})
