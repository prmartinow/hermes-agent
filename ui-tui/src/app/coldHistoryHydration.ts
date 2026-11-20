export class ColdHydrationCancelledError extends Error {
  constructor() {
    super('Cold hydration was cancelled')
    this.name = 'ColdHydrationCancelledError'
  }
}

import { renderNodeToAnsi } from '@hermes/ink'
import React from 'react'

import { TranscriptRowView } from '../components/TranscriptRowView.js'
import { introMsg, toTranscriptMessages } from '../domain/messages.js'
import type { SessionInfo } from '../types.js'
import type { Msg } from '../types.js'

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
  onStaticOutputStarted?: () => void
}

export interface ColdHydrationResult {
  initialLiveMessages: Msg[]
  materializedCount: number
  snapshotMaxRowId: number | null
  appendedToScrollback: boolean
}

function renderStaticMessage(msg: Msg, cols: number, theme: any, sessionId: string): string {
  return renderNodeToAnsi(
    React.createElement(TranscriptRowView, {
      cols, bodyCols: Math.max(1, cols - 2), msg, theme, sid: sessionId
    }),
    cols
  )
}

async function beginStaticOutput(
  output: ColdHistoryOutput, opts: ColdHydrationOptions, getColumns: () => number
): Promise<void> {
  opts.onStaticOutputStarted?.()
  await output.beginStaticAppendSurface()
  if (!opts.info) {return}
  const intro = renderStaticMessage(introMsg(opts.info), getColumns(), opts.theme, opts.sessionId)

  if (intro) {await output.write(normalizeTerminalLines(intro))}
}

function ensureHydrationActive(opts: ColdHydrationOptions): void {
  if (opts.isCancelled?.()) {throw new ColdHydrationCancelledError()}
}

function nextHistoryCursor(page: HistoryPageResult, cursor: number): number | null {
  if (!page.has_more) {return null}
  if (!Number.isSafeInteger(page.next_cursor) || page.next_cursor! <= cursor) {
    throw new Error('Session history returned an invalid continuation cursor')
  }

  return page.next_cursor!
}

function defaultColdOutput(opts: ColdHydrationOptions): ColdHistoryOutput {
  return {
    beginStaticAppendSurface: async () => {},
    write: async (data: string | Uint8Array) => {
      const out = opts.stdout ?? process.stdout

      if (!out.write(data)) {await new Promise(resolve => out.once('drain', resolve))}
    },
    getColumns: () => opts.cols ?? process.stdout.columns ?? 80
  }
}

export async function performColdHistoryHydration(
  opts: ColdHydrationOptions
): Promise<ColdHydrationResult> {
  const {
    gateway,
    sessionId,
    info,
    maxMounted = 120,
    onProgress,
    timeSliceMs = 20
  } = opts

  const output = opts.output ?? defaultColdOutput(opts)

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
    ensureHydrationActive(opts)

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

    ensureHydrationActive(opts)

    if (!res || !Array.isArray(res.messages)) {
      throw new Error('Session history returned an invalid page')
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
      ensureHydrationActive(opts)

      if (!appendedToScrollback) {
        appendedToScrollback = true
        await beginStaticOutput(output, opts, getStaticCols)
      }

      const msg = deque.shift()!
      materializedCount++

      const ansi = renderStaticMessage(msg, getStaticCols(), opts.theme, sessionId)

      ensureHydrationActive(opts)

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

    const nextCursor = nextHistoryCursor(res, cursor)

    if (nextCursor === null) {break}
    cursor = nextCursor
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
