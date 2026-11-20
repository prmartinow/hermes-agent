import type { SessionInfo } from '@hermes/shared/gateway-types'

import { storedClarifyRows } from '../app/clarifyTranscript.js'
import { LONG_MSG } from '../config/limits.js'
import { t } from '../i18n/runtime.js'
import { isTodoDone } from '../lib/liveProgress.js'
import { buildToolTrailLine, estimateTokensRough } from '../lib/text.js'
import { parseTodos } from '../lib/todo.js'
import type { Msg, TodoItem } from '../types.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return t('libText.messages.longMessage', prefix || t('libText.messages.messageFallback'))
}

/** A resumed user row's durable message row (backtrack browsing finds its prompt by it). */
const storedRowId = (role: string, rowId: unknown): Pick<Msg, 'rowId'> =>
  role === 'user' && Number.isSafeInteger(rowId) && (rowId as number) > 0 ? { rowId: rowId as number } : {}

/** A resumed tool row joins the next reply's trail; a clarify row shows what the live turn wrote after its trail
 * (the answers bubble, or the abandoned form's system line). Returns the trail lines still pending. */
const pushToolRow = (out: Msg[], pending: string[], { content, context, name }: TranscriptRow): string[] => {
  const clarify = name === 'clarify' && content ? storedClarifyRows(content) : null

  if (!clarify) {
    return [...pending, buildToolTrailLine(name ?? 'tool', context ?? '')]
  }

  out.push({ kind: 'trail', role: 'system', text: '', tools: [...pending, clarify.trail] })
  out.push({ role: clarify.abandoned ? 'system' : 'user', text: clarify.text })

  return []
}

const pushTodoTrail = (out: Msg[], todos: TodoItem[]) => {
  const done = isTodoDone(todos)
  out.push({
    kind: 'trail', role: 'system', text: '', todos,
    ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
  })
}

const updatePendingTodos = (row: TranscriptRow, previous: TodoItem[] | null): TodoItem[] | null => {
  if (row.name !== 'todo_list' && row.name !== 'todo') {return previous}

  const parsed = parseTodos(row.todos ?? row.args?.todos)

  return parsed?.length ? parsed : previous
}

const displayEvent = ({ display_kind, display_metadata: meta }: TranscriptRow): Msg | null => {
  let label: string | undefined

  switch (display_kind) {
    case 'model_switch':
      label = t('libText.messages.modelChanged')

      break

    case 'auto_continue':
      label = t('libText.messages.resumedInterruptedTurn')

      break

    case 'personality_switch':
      label = t('libText.messages.personalityChanged')

      break
  }

  if (display_kind === 'async_delegation_complete' || display_kind === 'process_complete') {
    const count = meta && typeof meta.task_count === 'number' ? meta.task_count : undefined
    label = display_kind === 'process_complete'
      ? t('libText.messages.backgroundProcessFinished')
      : count === undefined
        ? t('libText.messages.backgroundAgentWorkFinished')
        : t(count === 1 ? 'libText.messages.backgroundAgentsFinishedOne' : 'libText.messages.backgroundAgentsFinishedOther', count)

    if (typeof meta?.display_text === 'string') {label = meta.display_text}
  }

  return label === undefined ? null : { kind: 'event', role: 'system', text: label }
}

const pushPending = (out: Msg[], tools: string[], todos: TodoItem[] | null) => {
  if (tools.length) {
    out.push({ kind: 'trail', role: 'assistant', text: '', tools })
  }

  if (todos?.length) {
    pushTodoTrail(out, todos)
  }
}

const pushAssistantRow = (
  out: Msg[], text: unknown, thinking: string | undefined, createdAt: number | undefined,
  pending: string[], todos: TodoItem[] | null
) => {
  const assistantText = typeof text === 'string' ? text : ''

  if (todos?.length) {
    pushPending(out, pending, todos)
    pending = []
  }

  const msg: Msg = {
    role: 'assistant', text: assistantText,
    ...(createdAt !== undefined && { createdAt }),
    ...(pending.length && { tools: pending }),
    ...(thinking && { thinking, thinkingTokens: estimateTokensRough(thinking) })
  }

  if (!assistantText.trim() && (thinking || pending.length)) {
    msg.kind = 'trail'
  }

  out.push(msg)
}

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const out: Msg[] = []
  let pending: string[] = []
  let pendingTodos: TodoItem[] | null = null

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const { content, context, display_kind, name, role, row_id, text, timestamp } = row as TranscriptRow

    const rawReasoning =
      (row as TranscriptRow).reasoning ??
      (row as TranscriptRow).reasoning_content ??
      (row as TranscriptRow).thinking

    const thinking = typeof rawReasoning === 'string' && rawReasoning.trim() ? rawReasoning.trim() : undefined

    const createdAt =
      typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : undefined

    if (role === 'tool') {
      pending = pushToolRow(out, pending, { content, context, name })
      pendingTodos = updatePendingTodos(row as TranscriptRow, pendingTodos)

      continue
    }

    const hasText = typeof text === 'string' && text.trim().length > 0

    if (!hasText && !thinking && !pending.length && !pendingTodos) {
      continue
    }

    // Display-only timeline events: render as dim ◈ markers instead of
    // opaque user messages. Hidden compaction handoffs are skipped entirely.
    if (display_kind === 'hidden') {
      continue
    }

    const event = displayEvent(row as TranscriptRow)

    if (event) {
      out.push(event)
      pending = []
      pendingTodos = null

      continue
    }

    if (role === 'assistant') {
      pushAssistantRow(out, text, thinking, createdAt, pending, pendingTodos)
      pending = []
      pendingTodos = null
    } else if (role === 'user' || role === 'system') {
      pushPending(out, pending, pendingTodos)
      pending = []
      pendingTodos = null

      if (hasText) {
        out.push({ role, text: text!, ...(createdAt !== undefined && { createdAt }), ...storedRowId(role, row_id) })
      }
    }
  }

  pushPending(out, pending, pendingTodos)

  return out
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface TranscriptRow {
  content?: string
  args?: Record<string, unknown>
  context?: string
  display_kind?: string
  display_metadata?: { task_count?: number; [key: string]: unknown }
  name?: string
  reasoning?: string
  reasoning_content?: string
  role?: string
  row_id?: number
  text?: string
  thinking?: string
  timestamp?: number
  todos?: unknown[]
}
