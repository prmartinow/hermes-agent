import React from 'react'
import type { SessionInfo } from '../types.js'
import type { Msg } from '../types.js'
import { toTranscriptMessages, introMsg } from '../domain/messages.js'
import { renderNodeToAnsi } from '@hermes/ink'
import { TranscriptRowView } from '../components/TranscriptRowView.js'

interface HistoryPageResult {
  messages: unknown[]
  count: number
  after_row_id?: number
  next_after_row_id?: number
  snapshot_max_row_id?: number
  has_more?: boolean
}

export interface ColdHydrationOptions {
  gateway: {
    request: <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>
  }
  sessionId: string
  cols: number
  theme: any
  info?: SessionInfo | null
  stdout?: NodeJS.WriteStream
  maxMounted?: number
  onProgress?: (materialized: number, snapshotMax?: number) => void
  timeSliceMs?: number
}

export interface ColdHydrationResult {
  initialLiveMessages: Msg[]
  materializedCount: number
  snapshotMaxRowId: number | null
  appendedToScrollback: boolean
}

async function writeWithBackpressure(out: NodeJS.WriteStream, data: string): Promise<void> {
  if (!out.write(data)) {
    await new Promise(resolve => out.once('drain', resolve))
  }
}

export async function performColdHistoryHydration(
  opts: ColdHydrationOptions
): Promise<ColdHydrationResult> {
  const {
    gateway,
    sessionId,
    cols,
    theme,
    info,
    stdout = process.stdout,
    maxMounted = 120,
    onProgress,
    timeSliceMs = 20
  } = opts

  let afterRowId = 0
  let snapshotMaxRowId: number | null = null
  let materializedCount = 0
  let appendedToScrollback = false
  const deque: Msg[] = []
  const bodyCols = Math.max(1, cols - 2)

  let sliceStart = performance.now()

  while (true) {
    const historyParams: Record<string, unknown> = {
      session_id: sessionId,
      after_row_id: afterRowId,
      limit: 100
    }
    if (snapshotMaxRowId !== null) {
      historyParams.snapshot_max_row_id = snapshotMaxRowId
    }

    const res: HistoryPageResult | null = await gateway.request<HistoryPageResult>(
      'session.history',
      historyParams,
      15000
    )

    if (!res || !Array.isArray(res.messages)) {
      break
    }

    if (snapshotMaxRowId === null && typeof res.snapshot_max_row_id === 'number') {
      snapshotMaxRowId = res.snapshot_max_row_id
    }

    const pageMsgs = toTranscriptMessages(res.messages)
    for (const msg of pageMsgs) {
      deque.push(msg)
    }

    // While deque exceeds maxMounted, pop oldest messages and serialize to stdout
    while (deque.length > maxMounted) {
      if (!appendedToScrollback) {
        appendedToScrollback = true
        process.env.HERMES_TUI_INITIAL_RENDER_MODE = 'append-to-existing-scrollback'

        // Materialize Banner & SessionPanel once at line 0
        if (info) {
          const intro = introMsg(info)
          const introAnsi = renderNodeToAnsi(
            React.createElement(TranscriptRowView, {
              cols,
              bodyCols,
              msg: intro,
              theme,
              sid: sessionId
            }),
            cols
          )
          if (introAnsi) {
            await writeWithBackpressure(stdout, introAnsi + '\n')
          }
        }
      }

      const msg = deque.shift()!
      materializedCount++

      const ansi = renderNodeToAnsi(
        React.createElement(TranscriptRowView, {
          cols,
          bodyCols,
          msg,
          theme,
          sid: sessionId
        }),
        cols
      )

      if (ansi) {
        await writeWithBackpressure(stdout, ansi + '\n')
      }

      onProgress?.(materializedCount, snapshotMaxRowId ?? undefined)

      // Time-budgeted slice: yield event loop every ~20ms
      if (performance.now() - sliceStart > timeSliceMs) {
        await new Promise(resolve => setImmediate(resolve))
        sliceStart = performance.now()
      }
    }

    if (!res.has_more || !res.next_after_row_id || res.next_after_row_id <= afterRowId) {
      break
    }
    afterRowId = res.next_after_row_id
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
