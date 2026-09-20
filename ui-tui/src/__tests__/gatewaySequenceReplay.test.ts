import { type AddressInfo } from "node:net"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { WebSocketServer, type WebSocket as WsServerClient } from "ws"

import { GatewayClient } from "../gatewayClient.js"
import type { AnyGatewayEvent } from "../gatewayTypes.js"

interface TestServer {
  server: WebSocketServer
  port: number
  url: string
  waitForNextConnection: (timeoutMs?: number) => Promise<WsServerClient>
  getConnections: () => WsServerClient[]
  close: () => Promise<void>
}

async function createLocalGatewayServer(): Promise<TestServer> {
  const server = new WebSocketServer({ host: "127.0.0.1", port: 0 })
  await new Promise<void>((resolve, reject) => {
    server.once("listening", resolve)
    server.once("error", reject)
  })

  const port = (server.address() as AddressInfo).port
  const url = `ws://127.0.0.1:${port}`

  const connections: WsServerClient[] = []
  const unconsumed: WsServerClient[] = []
  const waiters: ((ws: WsServerClient) => void)[] = []

  server.on("connection", ws => {
    connections.push(ws)
    const waiter = waiters.shift()

    if (waiter) {
      waiter(ws)
    } else {
      unconsumed.push(ws)
    }
  })

  const waitForNextConnection = (timeoutMs = 5000): Promise<WsServerClient> => {
    if (unconsumed.length > 0) {
      return Promise.resolve(unconsumed.shift()!)
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const idx = waiters.indexOf(resolve)
        if (idx >= 0) waiters.splice(idx, 1)
        reject(new Error(`Timed out after ${timeoutMs}ms waiting for WebSocket connection`))
      }, timeoutMs)

      waiters.push((ws: WsServerClient) => {
        clearTimeout(timer)
        resolve(ws)
      })
    })
  }

  return {
    close: async () => {
      for (const client of connections) {
        try {
          client.terminate()
        } catch {}
      }
      await new Promise<void>(resolve => server.close(() => resolve()))
    },
    getConnections: () => [...connections],
    port,
    server,
    url,
    waitForNextConnection
  }
}

describe("Gateway Sequence Replay & Gap Recovery (P0)", () => {
  let server: TestServer
  let client: GatewayClient | null = null
  let origUrl: string | undefined

  beforeEach(async () => {
    origUrl = process.env.HERMES_TUI_GATEWAY_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
    server = await createLocalGatewayServer()
    process.env.HERMES_TUI_GATEWAY_URL = server.url
  })

  afterEach(async () => {
    if (origUrl !== undefined) {
      process.env.HERMES_TUI_GATEWAY_URL = origUrl
    } else {
      delete process.env.HERMES_TUI_GATEWAY_URL
    }
    if (client) {
      try {
        client.kill()
      } catch {}
      client = null
    }
    if (server) {
      await server.close()
    }
  })

  it("deduplicates events based on monotonic seq counter", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws = await server.waitForNextConnection()

    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 1, payload: { msg: "first" } } }))
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 2, payload: { msg: "second" } } }))
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 2, payload: { msg: "duplicate second" } } }))
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 1, payload: { msg: "older first" } } }))
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 3, payload: { msg: "third" } } }))

    await vi.waitFor(() => {
      const s1Events = received.filter(e => (e as any).session_id === "s1")
      expect(s1Events.length).toBe(3)
    })

    const s1Events = received.filter(e => (e as any).session_id === "s1")
    expect((s1Events[0] as any).seq).toBe(1)
    expect((s1Events[1] as any).seq).toBe(2)
    expect((s1Events[2] as any).seq).toBe(3)
    expect(client.getSeqWatermarks()).toEqual({ s1: 3 })
  })

  it("replays missing events on reconnect before surfacing gateway.ready", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    let ws = await server.waitForNextConnection()

    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 10, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 10 }))

    ws.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()

    let requestedMethod: string | null = null
    let requestedParams: any = null

    ws2.on("message", raw => {
      try {
        const data = JSON.parse(raw.toString())
        if (data.method === "session.events.since") {
          requestedMethod = data.method
          requestedParams = data.params

          const response = {
            jsonrpc: "2.0",
            id: data.id,
            result: {
              epoch: "epoch-1",
              events: [
                { type: "session.event", session_id: "s1", seq: 11, payload: { replayed: true, seq: 11 } },
                { type: "session.event", session_id: "s1", seq: 12, payload: { replayed: true, seq: 12 } }
              ],
              latest_seq: 12,
              truncated: false
            }
          }
          ws2.send(JSON.stringify(response))
        }
      } catch {}
    })

    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    await vi.waitFor(() => {
      expect(requestedMethod).toBe("session.events.since")
      expect(requestedParams).toEqual({ session_id: "s1", last_seen: 10 })
    })

    await vi.waitFor(() => {
      expect(client?.getSeqWatermarks()).toEqual({ s1: 12 })
    })

    const eventOrder = received.map(e => {
      if (e.type === "gateway.ready") return "gateway.ready"
      if ((e as any).seq) return `seq-${(e as any).seq}`
      return e.type
    })

    const idx11 = eventOrder.indexOf("seq-11")
    const idx12 = eventOrder.indexOf("seq-12")
    const secondReady = eventOrder.lastIndexOf("gateway.ready")

    expect(idx11).toBeGreaterThan(-1)
    expect(idx12).toBeGreaterThan(idx11)
    expect(secondReady).toBeGreaterThan(idx12)
  })

  it("parks live events arriving during replay and flushes them in monotonic order", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 5, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 5 }))

    ws1.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()

    ws2.on("message", raw => {
      try {
        const data = JSON.parse(raw.toString())
        if (data.method === "session.events.since") {
          ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 8, payload: { live: 8 } } }))
          ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 9, payload: { live: 9 } } }))

          setTimeout(() => {
            ws2.send(JSON.stringify({
              jsonrpc: "2.0",
              id: data.id,
              result: {
                epoch: "epoch-1",
                events: [
                  { type: "session.event", session_id: "s1", seq: 6, payload: { rep: 6 } },
                  { type: "session.event", session_id: "s1", seq: 7, payload: { rep: 7 } }
                ],
                latest_seq: 9,
                truncated: false
              }
            }))
          }, 30)
        }
      } catch {}
    })

    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    await vi.waitFor(() => {
      const seqs = received.filter(e => (e as any).session_id === "s1").map(e => (e as any).seq)
      expect(seqs).toEqual([5, 6, 7, 8, 9])
    })

    expect(client.getSeqWatermarks()).toEqual({ s1: 9 })
  })

  it("handles epoch change by clearing watermarks and emitting gateway.replay_gap", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-A" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 50, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 50 }))

    ws1.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()
    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-B" } } }))

    await vi.waitFor(() => {
      const gap = received.find(e => e.type === "gateway.replay_gap")
      expect(gap).toBeDefined()
      expect((gap as any).payload.reason).toBe("epoch-reset")
    })

    expect(client.getSeqWatermarks()).toEqual({})
  })

  it("discards stale replay when socket terminates during replay RPC and does not emit old gateway.ready", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.filter(e => e.type === "gateway.ready").length).toBe(1))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 20, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 20 }))

    ws1.terminate()

    // Second connection starts replay
    client.start()
    const ws2 = await server.waitForNextConnection()
    ws2.on("message", () => {
      // While replay request is in flight, ws2 suddenly drops!
      ws2.terminate()
    })
    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    // Give time for ws2 drop and rejected promise
    await new Promise(r => setTimeout(r, 80))

    // Reconnection 3 occurs
    client.start()
    const ws3 = await server.waitForNextConnection()

    // Verify gateway.ready was NOT emitted by the dead ws2!
    expect(received.filter(e => e.type === "gateway.ready").length).toBe(1)
  })

  it("emits gateway.replay_gap with request-failed when session.events.since rejects", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 10, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 10 }))

    ws1.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()
    ws2.on("message", raw => {
      const data = JSON.parse(raw.toString())
      if (data.method === "session.events.since") {
        ws2.send(JSON.stringify({
          jsonrpc: "2.0",
          id: data.id,
          error: { code: -32000, message: "backend replay failure" }
        }))
      }
    })
    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    await vi.waitFor(() => {
      const gap = received.find(e => e.type === "gateway.replay_gap")
      expect(gap).toBeDefined()
      expect((gap as any).payload.reason).toBe("request-failed")
      expect((gap as any).payload.session_id).toBe("s1")
    })
  })

  it("detects continuity gap when replay response skips sequence numbers", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 10, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 10 }))

    ws1.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()
    ws2.on("message", raw => {
      const data = JSON.parse(raw.toString())
      if (data.method === "session.events.since") {
        // Skips seq 12! Sends 11 then 13!
        ws2.send(JSON.stringify({
          jsonrpc: "2.0",
          id: data.id,
          result: {
            epoch: "epoch-1",
            events: [
              { type: "session.event", session_id: "s1", seq: 11, payload: {} },
              { type: "session.event", session_id: "s1", seq: 13, payload: {} }
            ],
            latest_seq: 13,
            truncated: false
          }
        }))
      }
    })
    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    await vi.waitFor(() => {
      const gap = received.find(e => e.type === "gateway.replay_gap")
      expect(gap).toBeDefined()
      expect((gap as any).payload.reason).toBe("continuity-gap")
      expect((gap as any).payload.session_id).toBe("s1")
    })
  })

  it("emits gateway.replay_gap with truncated when backend ring has rolled over", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws1 = await server.waitForNextConnection()
    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 5, payload: {} } }))
    await vi.waitFor(() => expect(client?.getSeqWatermarks()).toEqual({ s1: 5 }))

    ws1.terminate()

    client.start()
    const ws2 = await server.waitForNextConnection()
    ws2.on("message", raw => {
      const data = JSON.parse(raw.toString())
      if (data.method === "session.events.since") {
        ws2.send(JSON.stringify({
          jsonrpc: "2.0",
          id: data.id,
          result: {
            epoch: "epoch-1",
            events: [{ type: "session.event", session_id: "s1", seq: 600, payload: {} }],
            latest_seq: 600,
            truncated: true
          }
        }))
      }
    })
    ws2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))

    await vi.waitFor(() => {
      const gap = received.find(e => e.type === "gateway.replay_gap")
      expect(gap).toBeDefined()
      expect((gap as any).payload.reason).toBe("truncated")
      expect((gap as any).payload.session_id).toBe("s1")
    })
  })

  it("retires session watermarks via retireSession", async () => {
    client = new GatewayClient()
    const received: AnyGatewayEvent[] = []
    client.on("event", ev => received.push(ev))
    client.drain()
    await Promise.resolve()

    client.start()
    const ws = await server.waitForNextConnection()
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { replay_epoch: "epoch-1" } } }))
    await vi.waitFor(() => expect(received.some(e => e.type === "gateway.ready")).toBe(true))

    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s1", seq: 10, payload: {} } }))
    ws.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.event", session_id: "s2", seq: 25, payload: {} } }))

    await vi.waitFor(() => {
      expect(client?.getSeqWatermarks()).toEqual({ s1: 10, s2: 25 })
    })

    client.retireSession("s1")
    expect(client.getSeqWatermarks()).toEqual({ s2: 25 })
  })
})
