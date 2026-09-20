import { type AddressInfo } from "node:net"

import type { ServerRequest } from "@hermes/shared/json-rpc-channel"
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

        if (idx >= 0) {waiters.splice(idx, 1)}
        reject(new Error(`Timed out after ${timeoutMs}ms waiting for WebSocket connection`))
      }, timeoutMs)

      waiters.push((ws: WsServerClient) => {
        clearTimeout(timer)
        resolve(ws)
      })
    })
  }

  const close = async () => {
    for (const ws of connections) {
      try {
        ws.terminate()
      } catch {
        // best effort
      }
    }

    await new Promise<void>(resolve => server.close(() => resolve()))
  }

  return {
    server,
    port,
    url,
    waitForNextConnection,
    getConnections: () => [...connections],
    close
  }
}

function waitForSocketMessage(ws: WsServerClient, timeoutMs = 5000): Promise<string> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`Timed out after ${timeoutMs}ms waiting for socket message`))
    }, timeoutMs)

    ws.once("message", data => {
      clearTimeout(timer)
      resolve(data.toString("utf-8"))
    })
  })
}

describe("GatewayClient real WebSocket lifecycle integration regression", () => {
  let originalGatewayUrl: string | undefined
  let originalSidecarUrl: string | undefined
  let testServer: TestServer | null = null
  let client: GatewayClient | null = null

  beforeEach(() => {
    originalGatewayUrl = process.env.HERMES_TUI_GATEWAY_URL
    originalSidecarUrl = process.env.HERMES_TUI_SIDECAR_URL
    delete process.env.HERMES_TUI_SIDECAR_URL
  })

  afterEach(async () => {
    if (originalGatewayUrl !== undefined) {
      process.env.HERMES_TUI_GATEWAY_URL = originalGatewayUrl
    } else {
      delete process.env.HERMES_TUI_GATEWAY_URL
    }

    if (originalSidecarUrl !== undefined) {
      process.env.HERMES_TUI_SIDECAR_URL = originalSidecarUrl
    } else {
      delete process.env.HERMES_TUI_SIDECAR_URL
    }

    if (client) {
      try {
        client.kill()
      } catch {
        // best effort
      }

      client = null
    }

    if (testServer) {
      await testServer.close()
      testServer = null
    }
  })

  it("delivers ready, session completion, and server request exactly once after abnormal drop and reconnect without remount/drain", async () => {
    testServer = await createLocalGatewayServer()
    process.env.HERMES_TUI_GATEWAY_URL = testServer.url

    client = new GatewayClient()

    const events: AnyGatewayEvent[] = []
    const requests: ServerRequest[] = []
    const exits: { code: number | null; context: unknown }[] = []

    // 1. Mount: client registers listeners and calls drain ONCE
    client.on("event", ev => events.push(ev))
    client.on("request", req => {
      requests.push(req)
      req.respond({ approved: true, receivedId: req.id })
    })
    client.on("exit", (code, context) => exits.push({ code, context }))

    client.drain()
    await Promise.resolve()

    // 2. Connect to real ephemeral localhost WebSocket server
    client.start()
    const s1 = await testServer.waitForNextConnection()

    // Server sends initial gateway.ready
    s1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { heartbeat: false } } }))
    await vi.waitFor(() => expect(events.map(e => e.type)).toEqual(["gateway.ready"]))

    // 3. Terminate first server socket abnormally (raw TCP drop without clean WS close)
    s1.terminate()

    // Client detects exit and schedules reconnect
    await vi.waitFor(() => expect(exits.length).toBe(1))
    expect(exits[0]!.code).toBe(1006)
    await vi.waitFor(() => expect(events.map(e => e.type)).toContain("gateway.reconnecting"))

    // 4. Production reconnect path (recovery handler calls client.start() without remounting or re-draining)
    client.start()
    const s2 = await testServer.waitForNextConnection()
    expect(s2).not.toBe(s1)

    // Wait for response promise from s2
    const s2ResponsePromise = waitForSocketMessage(s2)

    // 5. Second server sends ready, session completion, and a server request
    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { heartbeat: false } } }))
    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.complete", payload: { session_id: "sess-recovery-1" } } }))
    s2.send(JSON.stringify({ jsonrpc: "2.0", id: "srq-approve-1", method: "approval", params: { command: "deploy-check" } }))

    // 6. Assert exactly-once delivery to consumer without drain/remount
    await vi.waitFor(() => {
      expect(events.filter(e => e.type !== "gateway.stderr").map(e => e.type)).toEqual([
        "gateway.ready",
        "gateway.reconnecting",
        "gateway.ready",
        "session.complete"
      ])
    })
    expect(events.some(e => e.type === "gateway.stderr")).toBe(true)
    expect(requests.map(r => r.method)).toEqual(["approval"])
    expect(requests[0]!.id).toBe("srq-approve-1")

    // 7. Verify bidirectional response was transmitted back over real reconnected socket s2
    const rawResponse = await s2ResponsePromise
    const parsedResponse = JSON.parse(rawResponse)
    expect(parsedResponse).toMatchObject({
      jsonrpc: "2.0",
      id: "srq-approve-1",
      result: { approved: true, receivedId: "srq-approve-1" }
    })

    // 8. Repeat cycle: terminate s2 abnormally and reconnect to s3
    s2.terminate()
    await vi.waitFor(() => expect(exits.length).toBe(2))
    expect(exits[1]!.code).toBe(1006)

    client.start()
    const s3 = await testServer.waitForNextConnection()
    expect(s3).not.toBe(s2)

    const s3ResponsePromise = waitForSocketMessage(s3)

    s3.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: { heartbeat: false } } }))
    s3.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.complete", payload: { session_id: "sess-recovery-2" } } }))
    s3.send(JSON.stringify({ jsonrpc: "2.0", id: "srq-clarify-2", method: "clarify", params: { question: "proceed?" } }))

    await vi.waitFor(() => {
      expect(events.filter(e => e.type !== "gateway.stderr").map(e => e.type)).toEqual([
        "gateway.ready",
        "gateway.reconnecting",
        "gateway.ready",
        "session.complete",
        "gateway.reconnecting",
        "gateway.ready",
        "session.complete"
      ])
    })
    expect(requests.map(r => r.method)).toEqual(["approval", "clarify"])
    expect(requests[1]!.id).toBe("srq-clarify-2")

    const rawResponse3 = await s3ResponsePromise
    const parsedResponse3 = JSON.parse(rawResponse3)
    expect(parsedResponse3).toMatchObject({
      jsonrpc: "2.0",
      id: "srq-clarify-2",
      result: { approved: true, receivedId: "srq-clarify-2" }
    })
  })

  it("reconnects via scheduled backoff timer path and delivers events without remount", async () => {
    testServer = await createLocalGatewayServer()
    process.env.HERMES_TUI_GATEWAY_URL = testServer.url

    client = new GatewayClient()
    const events: AnyGatewayEvent[] = []

    client.on("event", ev => {
      // console.log("Test2 event:", ev.type)
      events.push(ev)
    })
    client.drain()
    await Promise.resolve()

    client.start()
    const s1 = await testServer.waitForNextConnection()
    s1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: {} } }))
    await vi.waitFor(() => expect(events.map(e => e.type)).toContain("gateway.ready"))

    // Terminate abnormally and do NOT manually call client.start()
    // Let GatewayClient internal scheduleReconnect timer fire (~1000ms base delay)
    s1.terminate()

    await vi.waitFor(() => expect(events.map(e => e.type)).toContain("gateway.reconnecting"), { timeout: 3000 })

    // Next connection will be initiated by GatewayClient internal timer
    const s2 = await testServer.waitForNextConnection(8000)
    expect(s2).not.toBe(s1)

    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: {} } }))
    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "session.complete", payload: { session_id: "s2" } } }))

    await vi.waitFor(() => {
      expect(events.map(e => e.type)).toEqual([
        "gateway.ready",
        "gateway.stderr",
        "gateway.reconnecting",
        "gateway.ready",
        "session.complete"
      ])
    }, { timeout: 4000 })
  }, 12000)

  it("rejects stale frames and stale close events from a replaced socket", async () => {
    testServer = await createLocalGatewayServer()
    process.env.HERMES_TUI_GATEWAY_URL = testServer.url

    client = new GatewayClient()
    const events: AnyGatewayEvent[] = []
    const exits: number[] = []

    client.on("event", ev => events.push(ev))
    client.on("exit", code => exits.push(code ?? -1))
    client.drain()
    await Promise.resolve()

    client.start()
    const s1 = await testServer.waitForNextConnection()

    // Reconnect / replace transport before s1 is closed
    client.start()
    const s2 = await testServer.waitForNextConnection()
    expect(s2).not.toBe(s1)

    // Stale frame sent over s1 must be ignored by GatewayClient
    s1.send(JSON.stringify({
      jsonrpc: "2.0",
      method: "event",
      params: { type: "message.delta", payload: { text: "stale-s1" } }
    }))

    // Fresh frame sent over s2 must be accepted
    s2.send(JSON.stringify({
      jsonrpc: "2.0",
      method: "event",
      params: { type: "message.delta", payload: { text: "fresh-s2" } }
    }))

    await vi.waitFor(() => {
      const deltas = events.filter(e => e.type === "message.delta")
      expect(deltas).toHaveLength(1)
      expect((deltas[0]!.payload as { text: string }).text).toBe("fresh-s2")
    })

    // Stale close event on s1 must not trigger transport exit on client
    s1.close(1000, "normal replacement")
    // Give event loop time to process s1 close
    await new Promise(r => setTimeout(r, 50))
    expect(exits).toEqual([])
  })

  it("behavior when reconnect occurs after drain scheduled but before microtask flush (requires no old ready/event/request and exactly one fresh ready)", async () => {
    testServer = await createLocalGatewayServer()
    process.env.HERMES_TUI_GATEWAY_URL = testServer.url

    client = new GatewayClient()
    const events: AnyGatewayEvent[] = []
    const requests: ServerRequest[] = []

    client.on("event", ev => events.push(ev))
    client.on("request", req => {
      requests.push(req)
      req.respond({ approved: true, receivedId: req.id })
    })

    // Start client BEFORE drain so events are initially buffered
    client.start()
    const s1 = await testServer.waitForNextConnection()

    // s1 sends ready, event, and request into pretransport buffer
    s1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: {} } }))
    s1.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", payload: { text: "from-s1" } } }))
    s1.send(JSON.stringify({ jsonrpc: "2.0", id: "srq-s1", method: "approval", params: { command: "stale" } }))

    // Give a brief tick for s1 frames to land in client bufferedEvents
    await new Promise(r => setTimeout(r, 30))
    expect(events).toEqual([]) // Not delivered yet (subscribed is false)
    expect(requests).toEqual([])

    // Schedule drain (sets consumerReady = true, queues microtask)
    client.drain()

    // BEFORE microtask flush, reconnect occurs (triggering resetStartupState)
    client.start()

    // Microtask flushes now
    await Promise.resolve()

    // Connect to second socket
    const s2 = await testServer.waitForNextConnection()
    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: {} } }))
    s2.send(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", payload: { text: "from-s2" } } }))
    s2.send(JSON.stringify({ jsonrpc: "2.0", id: "srq-s2", method: "approval", params: { command: "fresh" } }))

    await vi.waitFor(() => {
      // Must deliver exactly one fresh ready, and only fresh events and requests from s2
      expect(events.filter(e => e.type !== "gateway.stderr").map(e => e.type)).toEqual([
        "gateway.ready",
        "message.delta"
      ])
      const delta = events.find(e => e.type === "message.delta")
      expect((delta?.payload as { text?: string })?.text).toBe("from-s2")
      expect(requests.map(r => r.id)).toEqual(["srq-s2"])
    })
  })
})
