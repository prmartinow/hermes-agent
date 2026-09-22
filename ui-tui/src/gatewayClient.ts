import { type ChildProcess, spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { existsSync } from 'node:fs'
import { delimiter, resolve } from 'node:path'
import { type IntervalHistogram, monitorEventLoopDelay } from 'node:perf_hooks'
import { createInterface } from 'node:readline'

import type { GatewayEvent } from '@hermes/shared/gateway-events'
import {
  DEFAULT_HEARTBEAT_DEADLINE_MS,
  DEFAULT_HEARTBEAT_INTERVAL_MS,
  JsonRpcRequestChannel,
  type ServerRequest,
  wireFrameText
} from '@hermes/shared/json-rpc-channel'
import { reconnectBackoffDelayMs } from '@hermes/shared/reconnect-backoff'
import { WebSocket as UndiciWebSocket } from 'undici'

import type { AnyGatewayEvent } from './gatewayTypes.js'
import { CircularBuffer } from './lib/circularBuffer.js'
import { recordParentLifecycle } from './lib/parentLog.js'

const MAX_GATEWAY_LOG_LINES = 200
const MAX_LOG_LINE_BYTES = 4096
const MAX_BUFFERED_EVENTS = 2000
const MAX_LOG_PREVIEW = 240
const STARTUP_TIMEOUT_MS = Math.max(5000, parseInt(process.env.HERMES_TUI_STARTUP_TIMEOUT_MS ?? '15000', 10) || 15000)
const REQUEST_TIMEOUT_MS = Math.max(30000, parseInt(process.env.HERMES_TUI_RPC_TIMEOUT_MS ?? '120000', 10) || 120000)
const WS_CONNECTING = 0
const WS_OPEN = 1
const WS_CLOSING = 2
const WS_CLOSED = 3

// Keepalive + dead-connection detection (issue #32997) lives in
// @hermes/shared's JsonRpcRequestChannel; these re-exports keep the TUI's
// timing constants readable at their call sites and in tests.
export const WS_HEARTBEAT_INTERVAL_MS = DEFAULT_HEARTBEAT_INTERVAL_MS
export const WS_HEARTBEAT_DEAD_MS = DEFAULT_HEARTBEAT_DEADLINE_MS
// Exponential backoff for reconnect attempts after a transport drop. No
// jitter: a single TUI process has nobody to desynchronize from, and the
// deterministic ladder is what the activity feed reports.
export const RECONNECT_BASE_MS = 1_000
export const RECONNECT_MAX_MS = 30_000

const getWebSocketCtor = (): typeof WebSocket =>
  typeof WebSocket === 'undefined' ? (UndiciWebSocket as unknown as typeof WebSocket) : WebSocket

const truncateLine = (line: string) =>
  line.length > MAX_LOG_LINE_BYTES ? `${line.slice(0, MAX_LOG_LINE_BYTES)}… [truncated ${line.length} bytes]` : line

const describeChild = (proc: ChildProcess | null) => {
  if (!proc) {
    return 'pid=none'
  }

  return `pid=${proc.pid ?? 'unknown'} killed=${proc.killed} exitCode=${proc.exitCode ?? 'null'} signal=${proc.signalCode ?? 'null'}`
}

const resolveGatewayAttachUrl = () => {
  const raw = process.env.HERMES_TUI_GATEWAY_URL?.trim()

  return raw ? raw : null
}

const resolveSidecarUrl = () => {
  const raw = process.env.HERMES_TUI_SIDECAR_URL?.trim()

  return raw ? raw : null
}

const resolvePython = (root: string) => {
  const configured = process.env.HERMES_PYTHON?.trim() || process.env.PYTHON?.trim()

  if (configured) {
    return configured
  }

  const venv = process.env.VIRTUAL_ENV?.trim()

  const hit = [
    venv && resolve(venv, 'bin/python'),
    venv && resolve(venv, 'Scripts/python.exe'),
    resolve(root, '.venv/bin/python'),
    resolve(root, '.venv/bin/python3'),
    resolve(root, 'venv/bin/python'),
    resolve(root, 'venv/bin/python3')
  ].find(p => p && existsSync(p))

  return hit || (process.platform === 'win32' ? 'python' : 'python3')
}

// Matches `<scheme>://user:pass@host…` style user-info segments in
// otherwise-malformed URLs that the WHATWG `URL` parser can't accept.
// Used by the `redactUrl` fallback so embedded credentials are
// scrubbed from log lines even when the URL is unparseable.
const _USERINFO_FALLBACK_RE = /^([a-z][a-z0-9+.-]*:\/\/)[^/?#@]*@/i

const isPrivateHost = (hostname: string): boolean =>
  /^(?:127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|\[?::1\]?|localhost\b)/i.test(hostname)

// Connection URLs (gateway, sidecar) often carry bearer tokens in the query
// string. We surface them in user-facing log lines and the
// `gateway.start_timeout` payload, so always strip the query string and any
// embedded user-info before logging.
export const redactUrl = (raw: string): string => {
  if (!raw) {
    return raw
  }

  try {
    const url = new URL(raw)
    const userInfo = url.username || url.password ? '***@' : ''
    const query = url.search ? '?***' : ''
    const host = isPrivateHost(url.hostname) ? `[redacted-ip]${url.port ? `:${url.port}` : ''}` : url.host

    return `${url.protocol}//${userInfo}${host}${url.pathname}${query}`
  } catch {
    // WHATWG URL rejected the input. Best-effort: strip an embedded
    // `user:pass@` segment AND the query string so a malformed token
    // bearer can never escape into the log tail.
    const noUserInfo = raw.replace(_USERINFO_FALLBACK_RE, '$1***@')
    const queryIdx = noUserInfo.indexOf('?')
    const queryStripped = queryIdx >= 0 ? `${noUserInfo.slice(0, queryIdx)}?***` : noUserInfo

    return queryStripped.replace(
      /^(https?|wss?):\/\/([^/?#@]*@)?(?:127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|localhost|\[?::1\]?)(?::\d+)?/i,
      '$1://$2[redacted-ip]'
    )
  }
}

export type GatewayExitSource = 'process' | 'websocket'

export interface GatewayExitContext {
  code: null | number
  source: GatewayExitSource
  reason?: string
  clean?: boolean
  initiator?: string
}

export class GatewayClient extends EventEmitter {
  private proc: ChildProcess | null = null
  private ws: WebSocket | null = null
  private wsConnectPromise: Promise<void> | null = null
  private sidecarWs: WebSocket | null = null
  private attachUrl: null | string = null
  private sidecarUrl: null | string = null
  private logs = new CircularBuffer<string>(MAX_GATEWAY_LOG_LINES)
  // Request ids, pending map, timeouts, error mapping and the gateway.ping
  // heartbeat are shared with the desktop/web WebSocket client; this class
  // only owns the two transports (child stdio, attached socket) and the
  // buffered-event replay that Ink's mount order needs.
  private readonly channel = new JsonRpcRequestChannel({
    onEvent: ev => this.handleGatewayEvent(ev as AnyGatewayEvent),
    onHeartbeatFailure: () => this.onHeartbeatFailure(),
    onUnhandledRequest: req => this.pushLog(`[protocol] unhandled server request: ${req.method}`),
    requestTimeoutMs: REQUEST_TIMEOUT_MS,
    unrefTimers: true
  })
  private lastSeenSeq = new Map<string, number>()
  private replayEpoch: string | null = null
  private replayInFlight = false
  private replayHold: Map<string, AnyGatewayEvent[]> | null = null
  private eventBarrierOwner = new Map<string, string>()
  private hadGatewayReady = false
  private transportGeneration = 0
  private bufferedEvents = new CircularBuffer<AnyGatewayEvent>(MAX_BUFFERED_EVENTS)
  // Server→client requests (clarify, approval, sudo, …) follow the same
  // mount-order contract as events: an attached session mid-turn can send one
  // the instant the socket opens, before the Ink handler is registered.
  private bufferedRequests: ServerRequest[] = []
  private pendingExit: { code: null | number; context: GatewayExitContext } | undefined
  private ready = false
  private readyTimer: ReturnType<typeof setTimeout> | null = null
  private consumerReady = false
  private subscribed = false
  private drainGeneration = 0
  private stdoutRl: ReturnType<typeof createInterface> | null = null
  private stderrRl: ReturnType<typeof createInterface> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectAttempts = 0
  // Set on kill() so we never auto-reconnect after an intentional shutdown.
  private disposed = false
  private loopDelayMonitor: IntervalHistogram | null = null
  private closeInitiator: 'heartbeat_timeout' | 'client_kill' | 'client_stop' | null = null

  constructor() {
    super()
    // useInput / createGatewayEventHandler can legitimately attach many
    // listeners. Default 10-cap triggers spurious warnings.
    this.setMaxListeners(0)
    this.channel.onRequest(request => {
      if (this.subscribed) {
        this.emit('request', request)
      } else {
        this.bufferedRequests.push(request)
      }
    })
  }

  private publish(ev: AnyGatewayEvent) {
    if (ev.type === 'gateway.ready') {
      this.ready = true

      if (this.readyTimer) {
        clearTimeout(this.readyTimer)
        this.readyTimer = null
      }

      if ((ev as GatewayEvent<'gateway.ready'>).payload?.heartbeat === true && this.ws?.readyState === WS_OPEN) {
        this.channel.startHeartbeat()
      }
    }

    if (this.subscribed) {
      return void this.emit('event', ev)
    }

    this.bufferedEvents.push(ev)
  }

  private clearReadyTimer() {
    if (this.readyTimer) {
      clearTimeout(this.readyTimer)
      this.readyTimer = null
    }
  }

  private initLoopDelayMonitor() {
    this.cleanupLoopDelayMonitor()

    try {
      this.loopDelayMonitor = monitorEventLoopDelay({ resolution: 20 })
      this.loopDelayMonitor.enable()
    } catch {
      this.loopDelayMonitor = null
    }
  }

  private cleanupLoopDelayMonitor() {
    if (this.loopDelayMonitor) {
      try {
        this.loopDelayMonitor.disable()
      } catch {
        // best effort
      }

      this.loopDelayMonitor = null
    }
  }

  getLoopDelaySummary(): { meanMs: number; maxMs: number; p99Ms: number } | null {
    if (!this.loopDelayMonitor) {
      return null
    }

    try {
      const mean = Number.isNaN(this.loopDelayMonitor.mean) ? 0 : this.loopDelayMonitor.mean
      const meanMs = Number((mean / 1e6).toFixed(2))
      const maxMs = Number((this.loopDelayMonitor.max / 1e6).toFixed(2))
      const p99Ms = Number((this.loopDelayMonitor.percentile(99) / 1e6).toFixed(2))

      return { meanMs, maxMs, p99Ms }
    } catch {
      return null
    }
  }

  isAttached(): boolean {
    return Boolean(this.attachUrl)
  }

  private closeSidecarSocket() {
    try {
      this.sidecarWs?.close()
    } catch {
      // best effort
    } finally {
      this.sidecarWs = null
    }
  }

  private closeGatewaySocket() {
    // Null the active reference BEFORE invoking close(): real WebSocket
    // implementations dispatch the 'close' event after a microtask hop,
    // so by the time the handler runs `this.ws` should already be null
    // and the identity guard will correctly classify the close as
    // belonging to a discarded socket. (Test fakes emit synchronously,
    // so doing the swap up front is also what makes the identity guard
    // match real timing in tests.)
    const ws = this.ws
    this.ws = null
    this.wsConnectPromise = null

    if (ws && !this.closeInitiator) {
      this.closeInitiator = 'client_stop'
    }

    try {
      ws?.close()
    } catch {
      // best effort
    }
  }

  // The shared heartbeat found no inbound frame for a full deadline: force the
  // socket closed so the ordinary close path reconnects (issue #32997).
  private onHeartbeatFailure() {
    const ws = this.ws

    if (!ws) {
      return
    }

    this.closeInitiator = 'heartbeat_timeout'
    this.lifecycle('[lifecycle] websocket silent drop detected (heartbeat ack timeout); forcing reconnect')

    try {
      ws.close()
    } catch {
      // ignore
    }
  }

  private scheduleReconnect() {
    if (this.disposed || this.reconnectTimer !== null) {
      return
    }

    const delay = reconnectBackoffDelayMs(this.reconnectAttempts, {
      baseDelayMs: RECONNECT_BASE_MS,
      capMs: RECONNECT_MAX_MS,
      jitter: false
    })

    this.reconnectAttempts += 1
    this.lifecycle(`[lifecycle] scheduling gateway reconnect in ${delay}ms (attempt ${this.reconnectAttempts})`)
    this.publish({ type: 'gateway.reconnecting', payload: { attempt: this.reconnectAttempts, delay_ms: delay } })
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null

      if (this.disposed) {
        return
      }

      this.start()
    }, delay)
    this.reconnectTimer.unref?.()
  }

  private clearReconnect() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    this.reconnectAttempts = 0
  }

  private resetStartupState() {
    // Reject any in-flight RPCs left over from the previous transport
    // before we swap. Otherwise the old transport's stale exit/close
    // handlers (now identity-gated to ignore unrelated transports)
    // never fire `rejectPending`, leaving callers hanging on promises
    // attached to a discarded child / socket.
    this.cleanupLoopDelayMonitor()
    this.channel.detach(new Error('gateway restarting'))
    this.ready = false

    // Invalidate any pending deferred drain() flush from a prior transport so
    // its queued microtask becomes a no-op (it captured the old generation).
    // Always discard per-transport buffers on reset so stale frames are never replayed.
    this.drainGeneration += 1
    this.invalidateTransportGeneration()
    this.bufferedEvents.clear()
    this.bufferedRequests = []
    this.pendingExit = undefined

    if (!this.consumerReady) {
      this.subscribed = false
    } else if (!this.subscribed) {
      // Readiness intent was recorded (drain called), but the deferred
      // microtask from the old generation was invalidated. Re-arm a new-generation
      // deferred drain so fresh frames are delivered once React commits.
      this.scheduleDeferredDrain()
    }

    this.stdoutRl?.close()
    this.stderrRl?.close()
    this.stdoutRl = null
    this.stderrRl = null
    this.clearReadyTimer()
  }

  private startReadyTimer(python: string, cwd: string) {
    this.readyTimer = setTimeout(() => {
      if (this.ready) {
        return
      }

      // Append the most recent gateway stderr/log lines to the timeout
      // event so users can tell apart "wrong python", "missing dep",
      // and "config parse failure" from one glance instead of having
      // to dig through `/logs`.  Capped to keep the activity feed
      // readable on slow boots.
      const stderrTail = this.getLogTail(20)

      this.lifecycle(`[startup] timed out waiting for gateway.ready (python=${python}, cwd=${cwd})`)
      this.publish({
        type: 'gateway.start_timeout',
        payload: { cwd, python, stderr_tail: stderrTail }
      })
    }, STARTUP_TIMEOUT_MS)
  }

  private handleTransportExit(
    code: null | number,
    reason?: string,
    source: GatewayExitSource = 'process',
    clean?: boolean,
    initiator?: string
  ) {
    this.clearReadyTimer()
    this.closeSidecarSocket()
    this.cleanupLoopDelayMonitor()
    this.invalidateTransportGeneration()
    this.lifecycle(`[lifecycle] transport exit code=${code ?? 'null'} reason=${reason ?? 'none'} source=${source}`)
    this.channel.detach(new Error(reason || `gateway exited${code === null ? '' : ` (${code})`}`))

    // Self-heal: a dropped transport (real close OR silent drop caught by the
    // heartbeat) should reconnect instead of stranding the UI on a dead socket
    // (issue #32997). Intentional shutdown sets `disposed` and skips this.
    // Schedule before the synchronous 'exit' emission: useMainApp's existing
    // recovery subscriber may call start() immediately, and start() cancels this
    // timer so there is only one recovery owner.
    this.scheduleReconnect()

    const context: GatewayExitContext = {
      code,
      source,
      reason,
      clean,
      initiator
    }

    if (this.subscribed) {
      this.emit('exit', code, context)
    } else {
      this.pendingExit = { code, context }
    }
  }

  private connectSidecarMirror() {
    this.closeSidecarSocket()

    if (!this.sidecarUrl) {
      return
    }

    const WebSocketCtor = getWebSocketCtor()

    if (typeof WebSocketCtor === 'undefined') {
      this.pushLog(`[sidecar] WebSocket unavailable; skipping mirror to ${redactUrl(this.sidecarUrl)}`)

      return
    }

    try {
      const ws = new WebSocketCtor(this.sidecarUrl)

      this.sidecarWs = ws
      ws.addEventListener('close', () => {
        if (this.sidecarWs === ws) {
          this.sidecarWs = null
        }
      })
      ws.addEventListener('error', () => {
        this.pushLog('[sidecar] mirror connection error')
      })
    } catch (err) {
      this.pushLog(`[sidecar] failed to connect ${redactUrl(this.sidecarUrl)} (constructor error)`)
      this.sidecarWs = null
    }
  }

  private mirrorEventToSidecar(rawFrame: string) {
    const ws = this.sidecarWs

    if (!ws || ws.readyState !== WS_OPEN) {
      return
    }

    try {
      ws.send(rawFrame)
    } catch {
      // best effort
    }
  }


  private handleGatewayEvent(ev: AnyGatewayEvent) {
    if (ev.type === 'gateway.ready') {
      void this.handleGatewayReadyEvent(ev as GatewayEvent<'gateway.ready'>)
      return
    }

    const sid = (ev as any).session_id as string | undefined
    if (this.replayInFlight && sid && this.replayHold?.has(sid)) {
      this.replayHold.get(sid)!.push(ev)
      return
    }

    this.dispatchIfNewer(ev, true)
  }

  private dispatchIfNewer(ev: AnyGatewayEvent, mirrorSidecar = true) {
    const sid = (ev as any).session_id as string | undefined
    const seq = (ev as any).seq as number | undefined

    if (sid && typeof seq === 'number' && Number.isFinite(seq)) {
      const previous = this.lastSeenSeq.get(sid) ?? 0
      if (seq <= previous) {
        return
      }
      this.lastSeenSeq.set(sid, seq)
    }

    if (mirrorSidecar) {
      const frame = JSON.stringify({ jsonrpc: '2.0', method: 'event', params: ev })
      this.mirrorEventToSidecar(frame)
    }

    this.publish(ev)
  }

  private invalidateTransportGeneration(): void {
    this.transportGeneration += 1
    this.replayInFlight = false
    this.replayHold = null
    this.eventBarrierOwner.clear()
  }

  private publishGatewayReady(ev: GatewayEvent<'gateway.ready'>): void {
    const frame = JSON.stringify({ jsonrpc: '2.0', method: 'event', params: ev })
    this.mirrorEventToSidecar(frame)
    this.publish(ev)
  }

  private publishReplayGap(gap: import('@hermes/shared/gateway-events').ClientLocalGatewayEventMap['gateway.replay_gap']): void {
    const ev: AnyGatewayEvent = {
      type: 'gateway.replay_gap',
      payload: gap
    } as AnyGatewayEvent
    const frame = JSON.stringify({ jsonrpc: '2.0', method: 'event', params: ev })
    this.mirrorEventToSidecar(frame)
    this.publish(ev)
  }

  private flushReplayHold(pendingGaps?: Map<string, import('@hermes/shared/gateway-events').ClientLocalGatewayEventMap['gateway.replay_gap']>): void {
    const hold = this.replayHold
    this.replayHold = null
    if (!hold) return

    for (const [sid, parked] of hold.entries()) {
      let expectedNext = (this.lastSeenSeq.get(sid) ?? 0) + 1
      for (const event of parked) {
        const seq = (event as any).seq as number | undefined
        if (typeof seq === 'number' && Number.isFinite(seq)) {
          if (seq > expectedNext) {
            this.pushLog(`[replay-hold] session ${sid} continuity gap: expected ${expectedNext}, got ${seq}`)
            const gap = {
              reason: 'continuity-gap' as const,
              session_id: sid,
              last_seen: expectedNext - 1,
              latest_seq: seq
            }
            if (pendingGaps) {
              pendingGaps.set(sid, gap)
            } else {
              this.publishReplayGap(gap)
            }
          }
          expectedNext = Math.max(expectedNext, seq + 1)
        }
        this.dispatchIfNewer(event, true)
      }
    }
  }

  private async handleGatewayReadyEvent(ev: GatewayEvent<'gateway.ready'>) {
    const generation = ++this.transportGeneration
    const epoch = (ev.payload as any)?.replay_epoch as string | undefined

    if (ev.payload?.heartbeat === true && this.ws?.readyState === WS_OPEN) {
      this.channel.startHeartbeat()
    }

    // First connection or no active sequence watermarks: expose immediately
    if (!this.hadGatewayReady || this.lastSeenSeq.size === 0) {
      this.hadGatewayReady = true
      if (epoch) this.replayEpoch = epoch
      this.publishGatewayReady(ev)
      return
    }

    // Epoch reset (server restarted): invalidate old watermarks
    if (epoch && this.replayEpoch && epoch !== this.replayEpoch) {
      this.pushLog(`[replay] epoch changed from ${this.replayEpoch} to ${epoch} - clearing watermarks`)
      this.lastSeenSeq.clear()
      this.replayEpoch = epoch
      this.publishReplayGap({ reason: 'epoch-reset', epoch })
      this.publishGatewayReady(ev)
      return
    }

    if (epoch && !this.replayEpoch) {
      this.replayEpoch = epoch
    }

    // Establish replay hold for tracked sessions
    this.replayInFlight = true
    const hold = new Map<string, AnyGatewayEvent[]>()
    for (const sid of this.lastSeenSeq.keys()) {
      hold.set(sid, [])
    }
    this.replayHold = hold

    const pendingGaps = new Map<string, import('@hermes/shared/gateway-events').ClientLocalGatewayEventMap['gateway.replay_gap']>()
    const latestSeqBySid = new Map<string, number>()

    try {
      const entries = Array.from(this.lastSeenSeq.entries())
      const results = await Promise.allSettled(
        entries.map(([sid, lastSeen]) =>
          this.channel.request<{
            events?: Array<{ type: string; session_id?: string; seq?: number; payload?: unknown }>
            truncated?: boolean
            epoch?: string
            latest_seq?: number
          }>('session.events.since', { session_id: sid, last_seen: lastSeen }, 10000)
        )
      )

      if (generation !== this.transportGeneration) {
        return
      }

      for (let i = 0; i < entries.length; i++) {
        const [sid, lastSeen] = entries[i]!
        const res = results[i]!

        if (res.status !== 'fulfilled' || !res.value) {
          this.pushLog(`[replay] session ${sid} events request failed`)
          pendingGaps.set(sid, {
            reason: 'request-failed',
            session_id: sid,
            last_seen: lastSeen
          })
          continue
        }

        const val = res.value
        if (typeof val.latest_seq === 'number') {
          latestSeqBySid.set(sid, val.latest_seq)
        }

        if (val.truncated) {
          this.pushLog(`[replay] session ${sid} events truncated (lastSeen=${lastSeen}, latest=${val.latest_seq})`)
          pendingGaps.set(sid, {
            reason: 'truncated',
            session_id: sid,
            last_seen: lastSeen,
            latest_seq: val.latest_seq
          })
        }

        let expectedNext = lastSeen + 1
        if (Array.isArray(val.events)) {
          for (const event of val.events) {
            if (event && event.type) {
              const seq = (event as any).seq as number | undefined
              if (typeof seq === 'number' && Number.isFinite(seq)) {
                if (seq > expectedNext) {
                  this.pushLog(`[replay] session ${sid} continuity gap: expected ${expectedNext}, got ${seq}`)
                  if (!pendingGaps.has(sid)) {
                    pendingGaps.set(sid, {
                      reason: 'continuity-gap',
                      session_id: sid,
                      last_seen: expectedNext - 1,
                      latest_seq: seq
                    })
                  }
                }
                expectedNext = Math.max(expectedNext, seq + 1)
              }
              this.dispatchIfNewer(event as AnyGatewayEvent, true)
            }
          }
        }
      }
    } catch {
      // Replay failure degrades to snapshot reconciliation
    } finally {
      if (generation === this.transportGeneration) {
        this.flushReplayHold(pendingGaps)
        this.replayInFlight = false

        // Verify final latest_seq tail coverage after held events flushed
        for (const [sid, latest] of latestSeqBySid) {
          const seen = this.lastSeenSeq.get(sid) ?? 0
          if (seen < latest) {
            this.pushLog(`[replay] session ${sid} latest_seq tail gap: seen=${seen}, latest=${latest}`)
            if (!pendingGaps.has(sid)) {
              pendingGaps.set(sid, {
                reason: 'continuity-gap',
                session_id: sid,
                last_seen: seen,
                latest_seq: latest
              })
            }
          }
        }

        // Publish coalesced gap events in order just before gateway.ready
        for (const gap of pendingGaps.values()) {
          this.publishReplayGap(gap)
        }

        this.publishGatewayReady(ev)
      }
    }
  }

  getSeqWatermarks(): Record<string, number> {
    return Object.fromEntries(this.lastSeenSeq)
  }

  retireSession(sid: string): void {
    this.lastSeenSeq.delete(sid)
  }

  activateEventBarrier(sid: string, owner?: string): void {
    if (!this.replayHold) {
      this.replayHold = new Map()
    }
    if (!this.replayHold.has(sid)) {
      this.replayHold.set(sid, [])
    }
    if (owner) {
      this.eventBarrierOwner.set(sid, owner)
    }
    this.replayInFlight = true
  }

  releaseEventBarrier(sid: string, owner?: string): AnyGatewayEvent[] {
    if (!this.replayHold) return []
    if (owner && this.eventBarrierOwner.has(sid) && this.eventBarrierOwner.get(sid) !== owner) {
      return []
    }
    this.eventBarrierOwner.delete(sid)
    const parked = this.replayHold.get(sid) ?? []
    this.replayHold.delete(sid)
    if (this.replayHold.size === 0) {
      this.replayHold = null
      this.replayInFlight = false
    }
    for (const ev of parked) {
      this.dispatchIfNewer(ev, true)
    }
    return parked
  }

  cancelEventBarrier(sid: string, owner?: string): void {
    if (!this.replayHold) return
    if (owner && this.eventBarrierOwner.has(sid) && this.eventBarrierOwner.get(sid) !== owner) {
      return
    }
    this.eventBarrierOwner.delete(sid)
    this.replayHold.delete(sid)
    if (this.replayHold.size === 0) {
      this.replayHold = null
      this.replayInFlight = false
    }
  }

  publishLocalEvent(ev: AnyGatewayEvent) {
    const frame = JSON.stringify({ jsonrpc: '2.0', method: 'event', params: ev })

    this.mirrorEventToSidecar(frame)
    this.publish(ev)
  }

  private handleWebSocketFrame(raw: unknown) {
    const text = wireFrameText(raw)

    if (!text) {
      return
    }

    const frame = this.channel.handleFrame(text)

    if (!frame) {
      this.protocolError('malformed websocket frame', text, '(empty frame)')

      return
    }

    // Sidecar mirroring is handled in dispatchIfNewer() to preserve sequence ordering during replay
  }

  private protocolError(what: string, text: string, emptyLabel: string) {
    const preview = text.trim().slice(0, MAX_LOG_PREVIEW) || emptyLabel

    this.pushLog(`[protocol] ${what}: ${preview}`)
    this.publish({ type: 'gateway.protocol_error', payload: { preview } })
  }

  private startSpawnedGateway(root: string) {
    const python = resolvePython(root)
    const cwd = process.env.HERMES_CWD || root
    const env = { ...process.env }
    const pyPath = env.PYTHONPATH?.trim()

    env.PYTHONPATH = pyPath ? `${root}${delimiter}${pyPath}` : root
    // Tell the gateway child where the Hermes source root is so its import
    // guard can force it ahead of any same-named package in the launch cwd.
    env.HERMES_PYTHON_SRC_ROOT = root
    this.startReadyTimer(python, cwd)
    this.proc = spawn(python, ['-m', 'tui_gateway.entry'], { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] })
    this.lifecycle(`[lifecycle] spawned gateway child ${describeChild(this.proc)} python=${python} cwd=${cwd}`)

    const stdin = this.proc.stdin!
    this.channel.attach({ send: text => void stdin.write(text + '\n') })

    this.stdoutRl = createInterface({ input: this.proc.stdout! })
    this.stdoutRl.on('line', raw => {
      if (!this.channel.handleFrame(raw)) {
        this.protocolError('malformed stdout', raw, '(empty line)')
      }
    })

    this.stderrRl = createInterface({ input: this.proc.stderr! })
    this.stderrRl.on('line', raw => {
      const line = truncateLine(raw.trim())

      if (!line) {
        return
      }

      this.pushLog(line)
      this.publish({ type: 'gateway.stderr', payload: { line } })
    })

    const ownedProc = this.proc
    this.proc.on('error', err => {
      // Skip stale errors on an already-replaced child.
      if (this.proc !== ownedProc) {
        this.pushLog(`[lifecycle] stale child error ignored ${describeChild(ownedProc)} message=${err.message}`)

        return
      }

      const line = `[spawn] ${err.message}`

      this.lifecycle(`[lifecycle] child error ${describeChild(ownedProc)} message=${err.message}`)
      this.pushLog(line)
      this.publish({ type: 'gateway.stderr', payload: { line } })
      // Detach the reference up front so the late `exit` event for
      // this same child is identity-skipped (we don't want to emit
      // 'exit' twice). Then run the full teardown — clears the
      // startup timer so we don't fire a misleading
      // `gateway.start_timeout`, rejects pending RPCs, and emits or
      // queues a single `exit`.
      this.proc = null
      this.handleTransportExit(1, `gateway error: ${err.message}`, 'process', false, 'proc_error')
    })
    this.proc.on('exit', (code, signal) => {
      // start() can replace `this.proc` while an old child is still
      // tearing down. Skip stale exits so we don't clear the new
      // startup timer or reject newly-issued pending requests.
      if (this.proc !== ownedProc) {
        this.pushLog(
          `[lifecycle] stale child exit ignored ${describeChild(ownedProc)} code=${code ?? 'null'} signal=${signal ?? 'null'}`
        )

        return
      }

      this.lifecycle(
        `[lifecycle] child exit ${describeChild(ownedProc)} code=${code ?? 'null'} signal=${signal ?? 'null'}`
      )
      this.handleTransportExit(code, signal ? `signal ${signal}` : undefined, 'process', code === 0, signal ? `signal_${signal}` : 'proc_exit')
    })
  }

  private startAttachedGateway(attachUrl: string) {
    const safeAttachUrl = redactUrl(attachUrl)
    this.startReadyTimer('websocket', safeAttachUrl)

    const WebSocketCtor = getWebSocketCtor()

    if (typeof WebSocketCtor === 'undefined') {
      const line = `[startup] WebSocket API unavailable; cannot attach to ${safeAttachUrl}`

      this.pushLog(line)
      this.publish({ type: 'gateway.stderr', payload: { line } })
      this.handleTransportExit(1, 'gateway websocket unavailable', 'websocket', false, 'unavailable')

      return
    }

    try {
      const ws = new WebSocketCtor(attachUrl)
      let settled = false

      this.ws = ws
      // Bind the channel to the socket as soon as it exists (not on open):
      // RPCs issued while CONNECTING await wsConnectPromise and then must
      // reach *this* generation; a stale generation's late frames are
      // already filtered by the `this.ws !== ws` guards below.
      this.channel.attach({ send: text => ws.send(text) })

      const connectPromise = new Promise<void>((resolve, reject) => {
        ws.addEventListener(
          'open',
          () => {
            if (!settled) {
              settled = true
              resolve()
            }

            this.clearReconnect()
            this.connectSidecarMirror()
          },
          { once: true }
        )

        ws.addEventListener(
          'error',
          () => {
            if (!settled) {
              this.pushLog('[startup] gateway websocket connect error')
              settled = true
              reject(new Error('gateway websocket connection failed'))
            }
          },
          { once: true }
        )
        ws.addEventListener(
          'close',
          ev => {
            if (!settled) {
              settled = true
              reject(new Error(`gateway websocket closed (${ev.code}) during connect`))
            }
          },
          { once: true }
        )
      })

      // The connect promise is only awaited by RPCs that arrive while
      // the socket is still connecting. If no request races the open
      // (or a teardown drops the reference before anyone observes it),
      // a connect-error / early-close rejection would surface as an
      // unhandled promise rejection in Node. Attach a no-op handler to
      // ensure the rejection is always observed.
      connectPromise.catch(() => {})
      this.wsConnectPromise = connectPromise

      ws.addEventListener('message', ev => {
        // Old sockets can still have queued events after replacement. Never
        // deliver their ready/delta notifications into the new connection.
        if (this.ws === ws) {this.handleWebSocketFrame(ev.data)}
      })
      ws.addEventListener('close', ev => {
        // Skip close events from sockets that have already been
        // replaced — start() / closeGatewaySocket() can swap `this.ws`
        // before an in-flight close lands, and we must not clear the
        // new ready timer or reject the new pending requests on behalf
        // of a stale socket.
        if (this.ws !== ws) {
          this.pushLog(`[lifecycle] stale websocket close ignored code=${ev.code}`)

          return
        }

        const initiator = this.closeInitiator ?? 'remote_or_network'
        this.closeInitiator = null

        this.lifecycle(
          `[lifecycle] websocket close code=${ev.code} clean=${ev.wasClean} ready=${this.ready} initiator=${initiator}`
        )
        const delaySummary = this.getLoopDelaySummary()

        if (delaySummary) {
          this.lifecycle(
            `[lifecycle] event-loop delay max=${delaySummary.maxMs}ms p99=${delaySummary.p99Ms}ms mean=${delaySummary.meanMs}ms`
          )
        }

        this.cleanupLoopDelayMonitor()
        this.ws = null
        this.wsConnectPromise = null
        this.handleTransportExit(
          ev.code,
          `gateway websocket closed${ev.code ? ` (${ev.code})` : ''}`,
          'websocket',
          ev.wasClean,
          initiator
        )
      })
      ws.addEventListener('error', () => {
        const line = '[gateway] websocket transport error'

        this.pushLog(line)
        this.publish({ type: 'gateway.stderr', payload: { line } })
      })
    } catch (err) {
      this.pushLog(`[startup] failed to connect websocket gateway ${safeAttachUrl} (constructor error)`)
      this.handleTransportExit(1, 'gateway websocket startup failed', 'websocket', false, 'client_error')
    }
  }

  start() {
    this.disposed = false
    this.clearReconnect()

    const root = process.env.HERMES_PYTHON_SRC_ROOT ?? resolve(import.meta.dirname, '../../')
    const attachUrl = resolveGatewayAttachUrl()
    const sidecarUrl = resolveSidecarUrl()

    this.attachUrl = attachUrl
    this.sidecarUrl = sidecarUrl
    this.resetStartupState()
    this.clearReconnect()
    this.initLoopDelayMonitor()

    if (this.proc && !this.proc.killed && this.proc.exitCode === null) {
      this.lifecycle(`[lifecycle] replacing live gateway child ${describeChild(this.proc)}`)
      this.proc.kill()
    }

    this.proc = null
    this.closeGatewaySocket()
    this.closeSidecarSocket()

    if (attachUrl) {
      this.startAttachedGateway(attachUrl)

      return
    }

    this.startSpawnedGateway(root)
  }

  private pushLog(line: string) {
    this.logs.push(truncateLine(line))
  }

  /** Record a client-side diagnostic line in the /logs tail (raw wire text the UI replaced with plain copy). */
  recordLog(line: string) {
    this.pushLog(line)
  }

  // Death-explaining breadcrumbs (spawn / exit / kill / replace) — kept in the
  // in-memory tail for /logs AND persisted to the gateway crash log so the
  // reason survives a parent exit and lands next to the child's SIGTERM panic.
  private lifecycle(line: string) {
    this.pushLog(line)
    recordParentLifecycle(line)
  }

  private scheduleDeferredDrain() {
    const generation = this.drainGeneration

    queueMicrotask(() => {
      if (this.disposed) {
        return
      }

      if (this.drainGeneration !== generation) {
        return
      }

      this.subscribed = true

      // Replay everything buffered up to now, then any events that arrived in
      // the gap before this microtask ran — all in chronological order.
      for (const ev of this.bufferedEvents.drain()) {
        this.emit('event', ev)
      }

      for (const request of this.bufferedRequests.splice(0)) {
        this.emit('request', request)
      }

      if (this.pendingExit !== undefined) {
        const { code, context } = this.pendingExit

        this.pendingExit = undefined
        this.emit('exit', code, context)
      }
    })
  }

  drain() {
    this.consumerReady = true

    if (this.subscribed) {
      return
    }

    // Defer the buffered-event replay to the next microtask, and DO NOT flip
    // `subscribed` until that microtask runs.
    //
    // `drain()` is called from the consumer's mount-time subscribe effect
    // (ui-tui/src/app/useMainApp.ts). In *attach* mode the gateway is already
    // running, so it replays `gateway.ready` / `session.info` the instant the
    // socket connects — those land in `bufferedEvents` *before* the consumer
    // subscribes. If we emitted them synchronously here, the `gateway.ready`
    // handler's `patchUiState` / `setHistoryItems` cascade would run while
    // React is still inside the first commit, tripping "Too many re-renders"
    // (Minified React error #301) — issue #36658. Spawn/inline/sidecar modes
    // don't hit this because `gateway.ready` only arrives after the Python
    // child boots, i.e. on a later async tick.
    //
    // Crucially, `subscribed` stays false until the flush so any LIVE event
    // arriving in the gap between here and the microtask keeps buffering
    // (publish() pushes when !subscribed) instead of emitting synchronously
    // and jumping ahead of the chronologically-earlier replayed events. The
    // flush re-drains the buffer right after flipping `subscribed`, so any
    // in-window arrivals are delivered in FIFO order. A generation token makes
    // the queued microtask a no-op if the transport was reset/killed meanwhile.
    this.scheduleDeferredDrain()
  }

  getLogTail(limit = 20): string {
    return this.logs.tail(Math.max(1, limit)).join('\n')
  }

  private async ensureAttachedWebSocket(method: string): Promise<WebSocket> {
    if (!this.attachUrl) {
      throw new Error('gateway not running')
    }

    if (!this.ws || this.ws.readyState === WS_CLOSED || this.ws.readyState === WS_CLOSING) {
      this.start()
    }

    if (this.ws?.readyState === WS_CONNECTING) {
      try {
        await this.wsConnectPromise
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err))
      }
    }

    if (!this.ws || this.ws.readyState !== WS_OPEN) {
      throw new Error(`gateway not connected: ${method}`)
    }

    return this.ws
  }

  private notConnected = (method: string) => new Error(`gateway not connected: ${method}`)

  request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
    const attachUrl = resolveGatewayAttachUrl()

    if (attachUrl) {
      if (this.attachUrl !== attachUrl) {
        // The env var rotated at runtime — restart the transport so
        // switching from spawned-gateway mode to attach mode also
        // tears down the old Python child. Merely closing `this.ws`
        // would leave a previously spawned gateway process alive.
        this.channel.detach(new Error('gateway attach url changed'))
        this.start()
      }

      return this.ensureAttachedWebSocket(method).then(() =>
        this.channel.request<T>(method, params, timeoutMs, undefined, () => this.notConnected(method))
      )
    }

    if (!this.proc?.stdin || this.proc.killed || this.proc.exitCode !== null) {
      this.start()
    }

    if (!this.proc?.stdin) {
      return Promise.reject(new Error('gateway not running'))
    }

    return this.channel.request<T>(method, params, timeoutMs, undefined, () => this.notConnected(method))
  }

  kill(reason = 'requested') {
    this.disposed = true
    this.closeInitiator = 'client_kill'
    this.clearReconnect()
    this.cleanupLoopDelayMonitor()
    const proc = this.proc
    const killed = proc?.kill()

    this.lifecycle(
      `[lifecycle] GatewayClient.kill reason=${reason} ${describeChild(proc)} killResult=${killed ?? 'none'}`
    )
    this.closeGatewaySocket()
    this.closeSidecarSocket()
    this.clearReadyTimer()
    this.invalidateTransportGeneration()
    // The ws 'close' handler is identity-gated on `this.ws === ws`
    // and we just nulled `this.ws`, so it will short-circuit and
    // skip handleTransportExit. Reject pending RPCs explicitly so
    // attach-mode promises do not hang after an intentional kill.
    this.channel.detach(new Error('gateway closed'))
  }
}
