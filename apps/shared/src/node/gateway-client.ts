/** The renderer-free `hermes-gateway-v1` client (Node): ticketed WebSocket dial, `runtime.describe`, the
 *  `session.create` contract, reconnect with bounded owner recovery, typed requests and events. Where the
 *  URL and ticket come from is the caller's `connect` (Ink: `gateway_bootstrap.py`; any other local
 *  client: `hermes gateway attach --json`). */
import { EventEmitter } from 'node:events'
import { type IntervalHistogram, monitorEventLoopDelay } from 'node:perf_hooks'

import { CircularBuffer } from '../circular-buffer.js'
import {
  canonicalEvent, canonicalRequest, canonicalResult, type ClientIdentity, type CreationContract, type LaunchSource
} from '../gateway-canonical.js'
import type { ClientLocalGatewayEventMap, GatewayEvent } from '../gateway-events.js'
import { type GatewayText, gatewayText } from '../gateway-text.js'
import type { AnyGatewayEvent } from '../gateway-types.js'
import {
  DEFAULT_HEARTBEAT_DEADLINE_MS,
  DEFAULT_HEARTBEAT_INTERVAL_MS,
  JsonRpcGatewayError,
  JsonRpcRequestChannel,
  type ServerRequest,
  wireFrameText
} from '../json-rpc-channel.js'
import { reconnectBackoffDelayMs } from '../reconnect-backoff.js'

const MAX_GATEWAY_LOG_LINES = 200
const MAX_LOG_LINE_BYTES = 4096
const MAX_BUFFERED_EVENTS = 2000
const MAX_LOG_PREVIEW = 240
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

/** One attach grant: the owner's WebSocket URL and the subprotocols (with the single-use ticket) to
 *  dial it with. From `hermes gateway attach --json`: its `url`, `protocols`, and `endpoint.profile_id`
 *  / `endpoint.instance_id`. */
export interface GatewayConnection { url: string; protocols: string[]; profile_id: string; instance_id: string }

/** `start`: the client's first attach (ensure the owner); `recover`: a bounded re-ensure of an owner
 *  that is not ready; neither: discover only. Called again for every reconnect (tickets are single-use). */
export type ConnectGateway = (start: boolean, recover: boolean) => Promise<GatewayConnection>

export interface GatewayClientOptions {
  connect: ConnectGateway
  /** For a `connect` failure no retry can fix, the line to report; the client then stops retrying. */
  terminalConnectFailure?: (error: Error) => string | undefined
  /** An explicit listener to dial instead of `connect` (Ink: `HERMES_TUI_GATEWAY_URL`); read per request. */
  attachUrl?: () => null | string
  /** A socket every event frame is mirrored to (Ink: `HERMES_TUI_SIDECAR_URL`). */
  sidecarUrl?: () => null | string
  /** How long a dial may take to reach `gateway.ready` before `gateway.start_timeout` (default 15 s). */
  startupTimeoutMs?: number
  /** Default per-request deadline (default 120 s). */
  requestTimeoutMs?: number
  /** Tool-progress mode pinned on each new session right after `session.create` (may throw to refuse). */
  toolProgress?: () => string | undefined
  /** Lifecycle breadcrumbs worth persisting (they also land in the `/logs` tail). */
  onLifecycle?: (line: string) => void
  /** User-facing text by Ink catalog key (default: `gatewayText`, English). */
  text?: GatewayText
  /** The WebSocket implementation when the runtime has no global `WebSocket` (Node < 22). */
  fallbackWebSocket?: typeof WebSocket
  /** The `session.create` policy label (default `'tui'`, the terminal-client policy). The gateway must
   *  list it in `runtime.describe` `session_create.sources`, or session.create is refused. Canonical
   *  gateways only: a non-canonical `attachUrl` listener gets session.create without it. */
  source?: LaunchSource
  /** The client's own name on `session.create`, sent only when the gateway lists `client` in
   *  `session_create.parameters` (never to a non-canonical `attachUrl` listener). A label for
   *  attribution: the gateway never grants anything on it. */
  client?: ClientIdentity
}

const truncateLine = (line: string) =>
  line.length > MAX_LOG_LINE_BYTES ? `${line.slice(0, MAX_LOG_LINE_BYTES)}… [truncated ${line.length} bytes]` : line

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

// Reconnects after the first launch are discovery-only, except that a bounded number per window
// re-ensure an owner that is not ready (gateway_bootstrap.py --recover): crashed, idle-exited,
// stopped with `hermes gateway stop` while this TUI is open, or an older Hermes's gateway. Main's
// TUI owned its sidecar, so an open TUI comes back on its own; the budget keeps a crash-looping
// owner from being respawned without pause.
export const OWNER_RECOVERY_LIMIT = 3
export const OWNER_RECOVERY_WINDOW_MS = 60_000


export interface AttachHold { sid: string; frames: AnyGatewayEvent[] }
interface ReplayResult {
  events?: Array<{ type: string; session_id?: string; seq?: number; payload?: unknown }>
  truncated?: boolean
  epoch?: string
  latest_seq?: number
}

export class GatewayClient extends EventEmitter {
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
  private readonly channel: JsonRpcRequestChannel
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
  // Legacy listeners replay process-wide sequence cursors. Canonical owners instead use
  // runtime.describe plus holdAttach/settleAttach and their session-specific snapshot fence.
  private lastSeenSeq = new Map<string, number>()
  private replayEpoch: string | null = null
  private replayInFlight = false
  private replayHold: Map<string, AnyGatewayEvent[]> | null = null
  private eventBarrierOwner = new Map<string, string>()
  private hadGatewayReady = false
  private transportGeneration = 0
  private loopDelayMonitor: IntervalHistogram | null = null
  private closeInitiator: 'heartbeat_timeout' | 'client_kill' | null = null
  private drainGeneration = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectAttempts = 0
  // Set on kill() so we never auto-reconnect after an intentional shutdown.
  private disposed = false

  private bootstrapFlight: Promise<void> | null = null
  private bootstrapError: Error | null = null
  private localStarted = false
  private localGeneration = 0
  private recoveryAttempts: number[] = []
  isCanonical = false
  private creationContract?: CreationContract
  private describedSkin?: Record<string, unknown>
  private runtimeCapabilities: string[] = []
  private describeFlight?: Promise<void>
  private attachHold: AttachHold | null = null
  // The last session.create whose outcome is in doubt (see createCanonical).
  private unsettledCreate?: { key: string; requestId: unknown }

  private readonly options: GatewayClientOptions
  private readonly text: GatewayText

  constructor(options: GatewayClientOptions) {
    super()
    this.options = options
    this.text = options.text ?? gatewayText
    this.channel = new JsonRpcRequestChannel({
      // A mid-turn socket streams deltas every second; killing the only
      // transport that carried live traffic split sessions that completed
      // server-side (#115251). Count any inbound frame as liveness, exactly
      // like the desktop/web client; a silent drop still trips the deadline.
      heartbeatLiveness: 'any-inbound',
      onEvent: ev => this.publishWire(ev as AnyGatewayEvent),
      onHeartbeatFailure: () => this.onHeartbeatFailure(),
      onRequestHandlerError: (error, req) =>
        this.pushLog(`[protocol] server request handler crashed: ${req.method} (${error.message})`),
      onUncorrelatedError: error => this.pushLog(`[protocol] error frame with no request id: ${error.code ?? 'none'} ${error.message}`),
      onUnhandledRequest: req => this.pushLog(`[protocol] unhandled server request: ${req.method}`),
      requestTimeoutMs: options.requestTimeoutMs ?? 120_000,
      unrefTimers: true
    })
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

  get attached(): boolean {
    return this.attachUrl !== null
  }

  /** Frames off the socket. On a canonical gateway the owner's execution stamp rides on
   * the params (`authority_epoch`/`execution_generation`); fold it into the payload the
   * pre-cutover handlers read, and let this client own readiness (it publishes
   * `gateway.ready` itself after `runtime.describe`, so the listener's ready would be a
   * second one) while still negotiating the transport's heartbeat capability. */
  private publishWire(ev: AnyGatewayEvent) {
    if (!this.isCanonical && ev.type === 'gateway.ready' && this.attachUrl !== null &&
        (ev as { payload?: { session_authority?: boolean } }).payload?.session_authority === true) {
      // An explicit HERMES_TUI_GATEWAY_URL endpoint whose session verbs belong to an authority:
      // negotiate the canonical wire exactly like a local grant, keeping the listener's skin.
      return this.adoptCanonicalAttach(ev as GatewayEvent<'gateway.ready'>)
    }

    if (this.isCanonical && ev.type === 'gateway.ready') {
      if ((ev as GatewayEvent<'gateway.ready'>).payload?.heartbeat && this.ws?.readyState === WS_OPEN) {
        this.channel.startHeartbeat()
      }

      return
    }

    if (this.isCanonical) {
      // An idle frame (session.info from _publish_pending) carries its stamp in the payload only
      // (B2-18): fall back to it, and never stamp an absent value over it.
      const shared = ev as AnyGatewayEvent & { authority_epoch?: number; execution_generation?: number }
      const payload = (canonicalEvent(ev).payload ?? {}) as { authority_epoch?: number; execution_generation?: number }
      const epoch = shared.authority_epoch ?? payload.authority_epoch
      const generation = shared.execution_generation ?? payload.execution_generation
      ev.payload = { ...payload, ...(epoch !== undefined ? { execution_epoch: String(epoch) } : {}),
        ...(generation !== undefined ? { execution_generation: generation } : {}) } as never

      if (this.attachHold && (ev as { session_id?: unknown }).session_id === this.attachHold.sid) {
        this.attachHold.frames.push(ev)

        return
      }
    }

    if (this.isCanonical) {
      this.publish(ev)
    } else {
      this.handleGatewayEvent(ev)
    }
  }

  private adoptCanonicalAttach(ev: GatewayEvent<'gateway.ready'>) {
    const ws = this.ws

    this.isCanonical = true

    if (ev.payload?.heartbeat && ws?.readyState === WS_OPEN) {
      this.channel.startHeartbeat()
    }

    this.describeThenReady(ws, { skin: ev.payload?.skin })
  }

  /** Canonical readiness is runtime.describe on this socket. A failed describe is a transport
   * failure (B2-21): drop the socket onto the backoff path, which redials and re-describes. */
  private describeThenReady(ws: WebSocket | null, payload: Record<string, unknown>) {
    this.describeFlight = undefined
    this.runtimeCapabilities = []
    void this.describeRuntime().then(() => {
      // The owner's resolved profile skin (`runtime.describe.skin`, main's `gateway.ready.skin`)
      // unless the listener's own ready already carried one.
      const ready = payload.skin === undefined && this.describedSkin ? { ...payload, skin: this.describedSkin } : payload

      if (this.ws === ws) { this.publish({ type: 'gateway.ready', payload: ready } as unknown as AnyGatewayEvent) }
    }).catch(error => {
      this.publish({ type: 'gateway.start_timeout', payload: {
        python: 'runtime.describe', cwd: '', stderr_tail: String(error)
      } })

      if (this.ws === ws) {
        this.closeGatewaySocket()
        this.handleTransportExit(1, String(error))
      }
    })
  }

  private publish(ev: AnyGatewayEvent) {
    if (ev.type === 'gateway.ready') {
      this.ready = true
      this.clearReconnect()
      this.reconnectAttempts = 0

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
      this.drain()
    }, delay)
    this.reconnectTimer.unref?.()
  }

  private clearReconnect() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private resetStartupState() {
    // Reject any in-flight RPCs left over from the previous transport
    // before we swap. Otherwise the old transport's stale exit/close
    // handlers (now identity-gated to ignore unrelated transports)
    // never settle the channel's pending RPCs, leaving callers hanging on promises
    // attached to a discarded child / socket.
    this.cleanupLoopDelayMonitor()
    this.channel.detach(new Error(this.text('libText.gateway.restarting')))
    this.ready = false
    // `subscribed` is NOT reset here: the renderer drain()s once on mount, so a
    // reset would strand every post-reconnect event (gateway.ready included) in
    // the buffer forever (#111594).
    // Invalidate any pending deferred drain() flush from a prior transport so
    // its queued microtask becomes a no-op (it captured the old generation).
    this.drainGeneration += 1
    this.invalidateTransportGeneration()
    this.bufferedEvents.clear()
    this.bufferedRequests = []
    this.pendingExit = undefined

    if (this.consumerReady && !this.subscribed) {
      this.scheduleDeferredDrain()
    }

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
    }, this.options.startupTimeoutMs ?? 15_000)
  }

  private handleTransportExit(
    code: null | number,
    reason?: string,
    source: GatewayExitSource = 'websocket',
    clean?: boolean,
    initiator?: string
  ) {
    this.clearReadyTimer()
    this.ready = false
    this.closeSidecarSocket()
    this.cleanupLoopDelayMonitor()
    this.invalidateTransportGeneration()
    this.lifecycle(`[lifecycle] transport exit code=${code ?? 'null'} reason=${reason ?? 'none'} source=${source}`)
    this.channel.detach(
      new Error(reason || (code === null ? this.text('libText.gateway.exited') : this.text('libText.gateway.exitedWithCode', code)))
    )

    // Self-heal: a dropped transport (real close OR silent drop caught by the
    // heartbeat) should reconnect instead of stranding the UI on a dead socket
    // (issue #32997). Intentional shutdown sets `disposed` and skips this.
    // Schedule before the synchronous 'exit' emission: in spawn mode useMainApp's
    // recovery subscriber may call start() immediately, and start() cancels this
    // timer so there is only one recovery owner; the attempt counter survives
    // until gateway.ready so backoff keeps growing across failed restarts.
    this.scheduleReconnect()

    const context: GatewayExitContext = { code, source, reason, clean, initiator }

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

    const WebSocketCtor = this.webSocketCtor()

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

  /** B2-19: hold this session's canonical frames while its resume/activate is in flight. Fanout
   * frames can beat the reply; the caller applies the snapshot, then settleAttach replays only the
   * held frames after the snapshot (same replay_epoch, seq > last_sequence). */
  holdAttach(sessionId: string): AttachHold | null {
    if (!this.isCanonical) { return null }

    return (this.attachHold = { sid: sessionId, frames: [] })
  }

  /** End a hold: with the applied snapshot, replay only frames it does not contain; without one
   * (failed or superseded attach), deliver what was held. A replaced hold is a no-op. */
  settleAttach(hold: AttachHold | null, snapshot?: object | null) {
    if (!hold || this.attachHold !== hold) { return }
    this.attachHold = null

    const fence = snapshot as { last_sequence?: unknown; replay_epoch?: unknown } | null | undefined

    for (const ev of hold.frames) {
      const { replay_epoch: epoch, seq } = ev as { replay_epoch?: unknown; seq?: unknown }

      if (!fence || (epoch === fence.replay_epoch && typeof seq === 'number' && seq > Number(fence.last_sequence ?? -1))) {
        this.publish(ev)
      }
    }
  }

  hydrateSharedPrompts(snapshot: unknown) {
    const result = snapshot as { session_id: string; authority_epoch: number; prompts?: Array<Record<string, unknown>>;
      open_requests?: Array<{ id: string; method: string; params?: Record<string, unknown> }> | null }

    for (const prompt of this.isCanonical ? result.prompts ?? [] : []) {
      this.publishLocalEvent({ type: `${prompt.kind}.request`, session_id: result.session_id,
        payload: { ...prompt, execution_epoch: String(result.authority_epoch) } } as unknown as AnyGatewayEvent)
    }

    // The channel delivered these before the reply resolved, while the view still showed the session
    // it was leaving, and the caller's reset has since cleared them (a batch clarify form). Both
    // transports: deliver again under the same id, before held frames are released.
    for (const req of result.open_requests ?? []) {
      this.channel.deliverRequest(req.id, req.method, req.params ?? {}, true)
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

  private publishReplayGap(gap: ClientLocalGatewayEventMap['gateway.replay_gap']): void {
    const ev: AnyGatewayEvent = {
      type: 'gateway.replay_gap',
      payload: gap
    } as AnyGatewayEvent

    const frame = JSON.stringify({ jsonrpc: '2.0', method: 'event', params: ev })
    this.mirrorEventToSidecar(frame)
    this.publish(ev)
  }

  private flushReplayHold(pendingGaps?: Map<string, ClientLocalGatewayEventMap['gateway.replay_gap']>): void {
    const hold = this.replayHold
    this.replayHold = null

    if (!hold) {return}

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

  private processReplayResult(
    sid: string, lastSeen: number, result: PromiseSettledResult<ReplayResult>,
    pendingGaps: Map<string, ClientLocalGatewayEventMap['gateway.replay_gap']>,
    latestSeqBySid: Map<string, number>
  ): void {
    if (result.status !== 'fulfilled' || !result.value) {
      this.pushLog(`[replay] session ${sid} events request failed`)
      pendingGaps.set(sid, { reason: 'request-failed', session_id: sid, last_seen: lastSeen })

      return
    }

    const value = result.value

    if (typeof value.latest_seq === 'number') {latestSeqBySid.set(sid, value.latest_seq)}
    if (value.truncated) {
      this.pushLog(`[replay] session ${sid} events truncated (lastSeen=${lastSeen}, latest=${value.latest_seq})`)
      pendingGaps.set(sid, { reason: 'truncated', session_id: sid, last_seen: lastSeen, latest_seq: value.latest_seq })
    }

    let expectedNext = lastSeen + 1

    for (const event of Array.isArray(value.events) ? value.events : []) {
      if (!event || !event.type) {continue}
      const seq = event.seq

      if (typeof seq === 'number' && Number.isFinite(seq)) {
        if (seq > expectedNext) {
          this.pushLog(`[replay] session ${sid} continuity gap: expected ${expectedNext}, got ${seq}`)
          if (!pendingGaps.has(sid)) {
            pendingGaps.set(sid, { reason: 'continuity-gap', session_id: sid, last_seen: expectedNext - 1, latest_seq: seq })
          }
        }

        expectedNext = Math.max(expectedNext, seq + 1)
      }

      this.dispatchIfNewer(event as AnyGatewayEvent, true)
    }
  }

  private finishReplay(
    ev: GatewayEvent<'gateway.ready'>, generation: number,
    pendingGaps: Map<string, ClientLocalGatewayEventMap['gateway.replay_gap']>,
    latestSeqBySid: Map<string, number>
  ): void {
    if (generation !== this.transportGeneration) {return}
    this.flushReplayHold(pendingGaps)
    this.replayInFlight = false

    for (const [sid, latest] of latestSeqBySid) {
      const seen = this.lastSeenSeq.get(sid) ?? 0

      if (seen < latest) {
        this.pushLog(`[replay] session ${sid} latest_seq tail gap: seen=${seen}, latest=${latest}`)
        if (!pendingGaps.has(sid)) {
          pendingGaps.set(sid, { reason: 'continuity-gap', session_id: sid, last_seen: seen, latest_seq: latest })
        }
      }
    }

    // Reconcile all gaps before notifying consumers that the gateway is ready.
    for (const gap of pendingGaps.values()) {this.publishReplayGap(gap)}
    this.publishGatewayReady(ev)
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

      if (epoch) {this.replayEpoch = epoch}
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

    const pendingGaps = new Map<string, ClientLocalGatewayEventMap['gateway.replay_gap']>()
    const latestSeqBySid = new Map<string, number>()

    try {
      const entries = Array.from(this.lastSeenSeq.entries())

      const results = await Promise.allSettled(
        entries.map(([sid, lastSeen]) =>
          this.channel.request<ReplayResult>('session.events.since', { session_id: sid, last_seen: lastSeen }, 10000)
        )
      )

      if (generation !== this.transportGeneration) {
        return
      }

      for (let i = 0; i < entries.length; i++) {
        const [sid, lastSeen] = entries[i]!
        this.processReplayResult(sid, lastSeen, results[i]!, pendingGaps, latestSeqBySid)
      }
    } catch {
      // Replay failure degrades to snapshot reconciliation
    } finally {
      this.finishReplay(ev, generation, pendingGaps, latestSeqBySid)
    }
  }

  getSeqWatermarks(): Record<string, number> {
    return Object.fromEntries(this.lastSeenSeq)
  }

  retireSession(sid: string): void {
    this.lastSeenSeq.delete(sid)
  }

  activateEventBarrier(sid: string, owner: string): void {
    if (this.isCanonical) {return}

    if (!this.replayHold) {
      this.replayHold = new Map()
    }

    if (!this.replayHold.has(sid)) {
      this.replayHold.set(sid, [])
    }

    this.eventBarrierOwner.set(sid, owner)
    this.replayInFlight = true
  }

  hasEventBarrier(sid: string, owner: string): boolean {
    return !this.isCanonical && this.eventBarrierOwner.get(sid) === owner && Boolean(this.replayHold?.has(sid))
  }

  releaseEventBarrier(sid: string, owner: string): AnyGatewayEvent[] {
    if (this.isCanonical) {return []}

    if (!this.replayHold) {return []}

    if (this.eventBarrierOwner.get(sid) !== owner) {
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

  cancelEventBarrier(sid: string, owner: string): void {
    if (this.isCanonical) {return}

    if (!this.replayHold) {return}

    if (this.eventBarrierOwner.get(sid) !== owner) {
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

    if (frame.method === 'event' && this.isCanonical) {
      this.mirrorEventToSidecar(text)
    }
  }

  private protocolError(what: string, text: string, emptyLabel: string) {
    const preview = text.trim().slice(0, MAX_LOG_PREVIEW) || emptyLabel

    this.pushLog(`[protocol] ${what}: ${preview}`)
    this.publish({ type: 'gateway.protocol_error', payload: { preview } })
  }

  private startLocalGateway() {
    const generation = ++this.localGeneration
    const start = !this.localStarted
    const recover = !start && this.claimOwnerRecovery()
    this.localStarted = true
    this.bootstrapError = null
    this.bootstrapFlight = this.options.connect(start, recover).then(grant => {
      if (this.disposed || generation !== this.localGeneration) { return }
      this.attachUrl = grant.url
      this.isCanonical = true
      // Main's /logs opened with the sidecar's spawn line; the shared owner's equivalent is the attach.
      this.lifecycle(`[lifecycle] attached shared gateway ${start ? 'start' : recover ? 'recover' : 'discover'} ` +
        `url=${redactUrl(grant.url)} profile=${grant.profile_id} instance=${grant.instance_id}`)
      this.startAttachedGateway(grant.url, grant.protocols)
    }).catch(error => {
      if (this.disposed || generation !== this.localGeneration) { return }
      this.bootstrapError = error instanceof Error ? error : new Error(String(error))
      this.clearReadyTimer()
      this.publish({ type: 'gateway.start_timeout', payload: {
        python: 'gateway ensure', cwd: '', stderr_tail: this.bootstrapError.message
      } })
      this.channel.detach(this.bootstrapError)

      // No reconnect fixes this (Ink: the helper's interpreter cannot load Hermes, gateway_bootstrap.py
      // `gateway unavailable:`), so report it once instead of retrying every 30 s forever (#138878).
      const terminal = this.options.terminalConnectFailure?.(this.bootstrapError)

      if (terminal !== undefined) {
        this.lifecycle(`[lifecycle] gateway bootstrap cannot start; not retrying: ${terminal}`)

        return
      }

      this.scheduleReconnect()
    })
  }

  /** One slot of the sliding owner re-ensure budget, so a crash-looping owner is not respawned forever. */
  private claimOwnerRecovery(now = Date.now()) {
    this.recoveryAttempts = this.recoveryAttempts.filter(at => now - at < OWNER_RECOVERY_WINDOW_MS)

    if (this.recoveryAttempts.length >= OWNER_RECOVERY_LIMIT) {
      return false
    }

    this.recoveryAttempts.push(now)

    return true
  }

  private startAttachedGateway(attachUrl: string, protocols?: string[]) {
    const safeAttachUrl = redactUrl(attachUrl)
    this.startReadyTimer('websocket', safeAttachUrl)

    const WebSocketCtor = this.webSocketCtor()

    if (typeof WebSocketCtor === 'undefined') {
      const line = `[startup] WebSocket API unavailable; cannot attach to ${safeAttachUrl}`

      this.pushLog(line)
      this.publish({ type: 'gateway.stderr', payload: { line } })
      this.handleTransportExit(1, this.text('libText.gateway.websocketUnavailable'), 'websocket', false, 'unavailable')

      return
    }

    try {
      const ws = new WebSocketCtor(attachUrl, protocols)
      let settled = false

      this.ws = ws
      this.closeInitiator = null
      // Bind the channel to the socket as soon as it exists (not on open):
      // RPCs issued while CONNECTING await wsConnectPromise and then must
      // reach *this* generation; a stale generation's late frames are
      // already filtered by the `this.ws !== ws` guards below.
      this.channel.attach({ send: text => ws.send(text) })

      const connectPromise = new Promise<void>((resolve, reject) => {
        ws.addEventListener(
          'open',
          () => {
            if (this.ws !== ws) {
              return
            }

            if (!settled) {
              settled = true
              resolve()
            }

            this.connectSidecarMirror()

            // The canonical gateway has no ready frame (readiness is the discovery grant +
            // runtime.describe); the client-local ready carries the skin runtime.describe resolved.
            if (this.isCanonical) { this.describeThenReady(ws, {}) }
          },
          { once: true }
        )

        ws.addEventListener(
          'error',
          () => {
            if (!settled) {
              this.pushLog('[startup] gateway websocket connect error')
              settled = true
              reject(new Error(this.text('libText.gateway.websocketConnectionFailed')))
            }
          },
          { once: true }
        )
        ws.addEventListener(
          'close',
          ev => {
            if (!settled) {
              settled = true
              reject(new Error(this.text('libText.gateway.websocketClosedDuringConnect', ev.code)))
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
        if (this.ws === ws) {
          this.handleWebSocketFrame(ev.data)
        }
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
        this.lifecycle(`[lifecycle] websocket close code=${ev.code} clean=${ev.wasClean} ready=${this.ready} initiator=${initiator}`)
        const delaySummary = this.getLoopDelaySummary()

        if (delaySummary) {
          this.lifecycle(`[lifecycle] event-loop delay max=${delaySummary.maxMs}ms p99=${delaySummary.p99Ms}ms mean=${delaySummary.meanMs}ms`)
        }

        this.ws = null
        this.wsConnectPromise = null
        this.handleTransportExit(
          ev.code,
          ev.code ? this.text('libText.gateway.websocketClosedWithCode', ev.code) : this.text('libText.gateway.websocketClosed'),
          'websocket', ev.wasClean, initiator
        )
      })
      ws.addEventListener('error', () => {
        const line = '[gateway] websocket transport error'

        this.pushLog(line)
        this.publish({ type: 'gateway.stderr', payload: { line } })
      })
    } catch (err) {
      this.pushLog(`[startup] failed to connect websocket gateway ${safeAttachUrl} (constructor error)`)
      this.handleTransportExit(1, this.text('libText.gateway.websocketStartupFailed'), 'websocket', false, 'client_error')
    }
  }

  start() {
    if (this.disposed) {
      // kill() is terminal: every caller (die / dieWithCode /
      // graceful-exit-cleanup / dead-output-stream) exits the Node process
      // right after, so there is no legitimate kill-then-start flow. A
      // start() arriving here is a recovery subscriber reacting to the
      // killed child's late `exit` — respawning now would recreate the
      // gateway on a PTY that is already gone.
      this.pushLog('[lifecycle] start() ignored after kill()')

      return
    }

    this.disposed = false
    this.clearReconnect()

    const attachUrl = this.options.attachUrl?.() ?? null
    const sidecarUrl = this.options.sidecarUrl?.() ?? null

    this.attachUrl = attachUrl
    this.sidecarUrl = sidecarUrl
    this.resetStartupState()
    this.initLoopDelayMonitor()

    this.closeGatewaySocket()
    this.closeSidecarSocket()

    if (attachUrl) {
      // Each explicit connection negotiates its wire from the listener's ready frame.
      this.isCanonical = false
      this.creationContract = undefined
      this.runtimeCapabilities = []
      this.startAttachedGateway(attachUrl)

      return
    }

    this.startLocalGateway()
  }

  private pushLog(line: string) {
    this.logs.push(truncateLine(line))
  }

  /** Record a client-side diagnostic line in the /logs tail (raw wire text the UI replaced with plain copy). */
  recordLog(line: string) {
    this.pushLog(line)
  }

  // Death-explaining breadcrumbs (spawn / exit / kill / replace) — kept in the
  // in-memory tail for /logs AND handed to `onLifecycle` (Ink persists them to the
  // gateway crash log so the reason survives a parent exit).
  private lifecycle(line: string) {
    this.pushLog(line)
    this.options.onLifecycle?.(line)
  }

  private webSocketCtor(): typeof WebSocket | undefined {
    return typeof WebSocket === 'undefined' ? this.options.fallbackWebSocket : WebSocket
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

      if (!this.options.attachUrl?.()) { await this.bootstrapFlight }
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

  /** Only an owner advertising `clarify-batch-v1` knows `clarify_batch`: an older owner's
   * closed submit params would refuse every prompt carrying it. */
  get clarifyBatch(): boolean {
    return this.isCanonical && this.runtimeCapabilities.includes('clarify-batch-v1')
  }

  /** The owner's session.create contract. A failed read is not cached: the next
   * session.create asks again instead of reporting an outdated gateway forever. */
  private describeRuntime(): Promise<void> {
    if (!this.describeFlight) {
      const flight: Promise<void> = this.requestOverWebSocket<{ capabilities?: unknown; session_create: CreationContract; skin?: Record<string, unknown> | null }>('runtime.describe').then(
        description => {
          this.creationContract = description.session_create
          this.runtimeCapabilities = Array.isArray(description.capabilities)
            ? description.capabilities.filter((c): c is string => typeof c === 'string')
            : []
          this.describedSkin = description.skin && typeof description.skin === 'object' ? description.skin : undefined
        },
        error => {
          if (this.describeFlight === flight) { this.describeFlight = undefined }

          throw error
        })

      this.describeFlight = flight
    }

    return this.describeFlight
  }

  private requestOverWebSocket<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
    return this.ensureAttachedWebSocket(method).then(() =>
      this.channel.request<T>(method, params, timeoutMs, undefined, () => this.notConnected(method)))
  }

  request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
    const attachUrl = this.options.attachUrl?.() ?? null

    if (attachUrl) {
      if (this.attachUrl !== attachUrl) {
        // The env var rotated at runtime — restart the transport so
        // switching from spawned-gateway mode to attach mode also
        // tears down the old Python child. Merely closing `this.ws`
        // would leave a previously spawned gateway process alive.
        this.channel.detach(new Error(this.text('libText.gateway.attachUrlChanged')))
        this.start()
      }

      return this.isCanonical
        ? this.requestCanonical<T>(method, params, timeoutMs)
        : this.requestOverWebSocket<T>(method, params, timeoutMs)
    }

    if (!this.bootstrapFlight) { this.start() }

    return this.bootstrapFlight!.then(() => {
      if (this.bootstrapError) { throw this.bootstrapError }

      return this.requestCanonical<T>(method, params, timeoutMs)
    })
  }

  /** One request through the canonical wire adapter (local grant or negotiated explicit attach). */
  private requestCanonical<T>(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<T> {
    return (async () => {
      if (method === 'session.create' && !this.creationContract) { await this.describeRuntime() }

      const request = canonicalRequest(method, params, this.creationContract, this.text,
        { client: this.options.client, source: this.options.source })

      if (request.method === 'session.create') { return this.createCanonical<T>(request.params, timeoutMs) }
      const value = await this.requestOverWebSocket<T>(request.method, request.params, timeoutMs)

      return canonicalResult(method, value, request.params)
    })()
  }

  /** session.create plus the launch tool-progress pin, as one outcome for the caller. The owner keys a
   * local session by request_id, so a create that failed after it may have committed (lost ACK, or the
   * pin refused after the ACK) is retried with the SAME request_id when the options match: the owner
   * answers with the session it already minted and the pin is reapplied, never a second session. */
  private async createCanonical<T>(prepared: Record<string, unknown>, timeoutMs?: number): Promise<T> {
    const toolProgress = this.options.toolProgress?.()
    const { request_id: _fresh, ...options } = prepared
    const key = JSON.stringify(Object.keys(options).sort().map(name => [name, options[name]]))
    const retry = this.unsettledCreate?.key === key ? this.unsettledCreate : undefined
    // Taken, not peeked: a concurrent create never shares an identity still in flight.
    this.unsettledCreate = undefined
    const params = retry ? { ...prepared, request_id: retry.requestId } : prepared
    let acknowledged = false

    try {
      const value = await this.requestOverWebSocket<T>('session.create', params, timeoutMs)
      acknowledged = true

      if (toolProgress) {
        // Launch pin, before the caller can submit: a refusal fails this create visibly.
        const sid = (value as { session_id?: string } | null)?.session_id

        await this.requestOverWebSocket('config.set', { key: 'verbose', session_id: sid, value: toolProgress })
      }

      return canonicalResult('session.create', value, params)
    } catch (error) {
      // An owner's error reply to the create itself is a definitive refusal (nothing to recover);
      // an ACKed create, a timeout or a dropped socket may have committed the session.
      if (acknowledged || !(error instanceof JsonRpcGatewayError)) {
        this.unsettledCreate = { key, requestId: params.request_id }
      }

      throw error
    }
  }

  kill(reason = 'requested') {
    this.disposed = true
    this.closeInitiator = 'client_kill'
    this.cleanupLoopDelayMonitor()
    this.invalidateTransportGeneration()
    this.localGeneration++
    this.clearReconnect()
    this.reconnectAttempts = 0
    this.channel.stopHeartbeat()
    this.lifecycle(`[lifecycle] GatewayClient.kill reason=${reason} (detach only)`)
    this.closeGatewaySocket()
    this.closeSidecarSocket()
    this.clearReadyTimer()
    // The ws 'close' handler is identity-gated on `this.ws === ws`
    // and we just nulled `this.ws`, so it will short-circuit and
    // skip handleTransportExit. Reject pending RPCs explicitly so
    // attach-mode promises do not hang after an intentional kill.
    this.channel.detach(new Error(this.text('libText.gateway.closed')))
  }
}
