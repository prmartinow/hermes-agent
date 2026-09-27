export class ColdHydrationCancelledError extends Error {
  constructor() {
    super('Cold hydration was cancelled')
    this.name = 'ColdHydrationCancelledError'
  }
}

import React from 'react'
import type { SessionInfo } from '../types.js'
import type { Msg } from '../types.js'
import { toTranscriptMessages, introMsg } from '../domain/messages.js'
import { renderNodeToAnsi } from '@hermes/ink'
import { TranscriptRowView } from '../components/TranscriptRowView.js'

interface HistoryPageResult {
  messages: unknown[]
  count: number
  cursor?: number
  next_cursor?: number
  snapshot_token?: string
  total?: number
  after_row_id?: number
  next_after_row_id?: number
  snapshot_max_row_id?: number
  has_more?: boolean
}

export interface ColdHistoryOutput {
  beginStaticAppendSurface(): Promise<void>
  write(data: string | Uint8Array): Promise<void>
  getColumns(): number
}

export function normalizeTerminalLines(value: string): string {
  const normalized = value.replace(/\r?\n/g, '\r\n')
  return normalized.endsWith('\r\n') ? normalized : `${normalized}\r\n`
}

export interface ColdHydrationOptions {
  gateway: {
    request: <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>
  }
  sessionId: string
  theme: any
  info?: SessionInfo | null
  output?: ColdHistoryOutput
  cols?: number
  stdout?: NodeJS.WriteStream
  maxMounted?: number
  onProgress?: (materialized: number, snapshotMax?: number) => void
  timeSliceMs?: number
  isCancelled?: () => boolean
}

export interface ColdHydrationResult {
  initialLiveMessages: Msg[]
  materializedCount: number
  snapshotMaxRowId: number | null
  appendedToScrollback: boolean
}

export async function performColdHistoryHydration(
  opts: ColdHydrationOptions
): Promise<ColdHydrationResult> {
  const {
    gateway,
    sessionId,
    theme,
    info,
    maxMounted = 120,
    onProgress,
    timeSliceMs = 20
  } = opts

  const output: ColdHistoryOutput = opts.output ?? {
    beginStaticAppendSurface: async () => {},
    write: async (data: string | Uint8Array) => {
      const out = opts.stdout ?? process.stdout
      if (!out.write(data)) {
        await new Promise(resolve => out.once('drain', resolve))
      }
    },
    getColumns: () => opts.cols ?? process.stdout.columns ?? 80
  }

  let cursor = 0
  let snapshotToken: string | null = null
  let totalMessages: number | null = null
  let snapshotMaxRowId: number | null = null
  let materializedCount = 0
  let appendedToScrollback = false
  const deque: Msg[] = []

  let staticCols: number | null = null
  const getStaticCols = () => (staticCols ??= output.getColumns())

  let sliceStart = performance.now()

  while (true) {
    if (opts.isCancelled?.()) {
      throw new ColdHydrationCancelledError()
    }
    const historyParams: Record<string, unknown> = {
      session_id: sessionId,
      cursor,
      limit: 100
    }
    if (snapshotToken !== null) {
      historyParams.snapshot_token = snapshotToken
    }

    const res: HistoryPageResult | null = await gateway.request<HistoryPageResult>(
      'session.history',
      historyParams,
      15000
    )

    if (opts.isCancelled?.()) {
      throw new ColdHydrationCancelledError()
    }

    if (!res || !Array.isArray(res.messages)) {
      break
    }

    if (snapshotToken === null && typeof res.snapshot_token === 'string') {
      snapshotToken = res.snapshot_token
      totalMessages = res.total ?? null
    }

    const pageMsgs = toTranscriptMessages(res.messages)
    for (const msg of pageMsgs) {
      deque.push(msg)
      if (typeof (msg as any).row_id === 'number') {
        snapshotMaxRowId = Math.max(snapshotMaxRowId ?? 0, (msg as any).row_id)
      }
    }

    // While deque exceeds maxMounted, pop oldest messages and serialize to static output
    while (deque.length > maxMounted) {
      if (opts.isCancelled?.()) throw new ColdHydrationCancelledError()
      if (!appendedToScrollback) {
        appendedToScrollback = true
        await output.beginStaticAppendSurface()

        // Materialize Banner & SessionPanel once at line 0
        if (info) {
          const lockedCols = getStaticCols()
          const lockedBodyCols = Math.max(1, lockedCols - 2)
          const intro = introMsg(info)
          const introAnsi = renderNodeToAnsi(
            React.createElement(TranscriptRowView, {
              cols: lockedCols,
              bodyCols: lockedBodyCols,
              msg: intro,
              theme,
              sid: sessionId
            }),
            lockedCols
          )
          if (introAnsi) {
            await output.write(normalizeTerminalLines(introAnsi))
          }
        }
      }

      const msg = deque.shift()!
      materializedCount++

      const lockedCols = getStaticCols()
      const lockedBodyCols = Math.max(1, lockedCols - 2)
      const ansi = renderNodeToAnsi(
        React.createElement(TranscriptRowView, {
          cols: lockedCols,
          bodyCols: lockedBodyCols,
          msg,
          theme,
          sid: sessionId
        }),
        lockedCols
      )

      if (opts.isCancelled?.()) {
        throw new ColdHydrationCancelledError()
      }
      if (ansi) {
        await output.write(normalizeTerminalLines(ansi))
      }

      onProgress?.(materializedCount, totalMessages ?? undefined)

      // Time-budgeted slice: yield event loop every ~20ms
      if (performance.now() - sliceStart > timeSliceMs) {
        await new Promise(resolve => setImmediate(resolve))
        sliceStart = performance.now()
      }
    }

    if (!res.has_more || res.next_cursor === undefined || res.next_cursor <= cursor) {
      break
    }
    cursor = res.next_cursor
  }

  // Final live messages for the React tree
  const initialLiveMessages = appendedToScrollback
    ? deque
    : (info ? [introMsg(info), ...deque] : deque)

  return {
    initialLiveMessages,
    materializedCount,
    snapshotMaxRowId,
    appendedToScrollback
  }
}
