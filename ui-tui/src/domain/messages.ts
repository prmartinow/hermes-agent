import { LONG_MSG } from '../config/limits.js'
import { isTodoDone } from '../lib/liveProgress.js'
import { buildToolTrailLine, estimateTokensRough } from '../lib/text.js'
import { parseTodos } from '../lib/todo.js'
import type { Msg, SessionInfo, TodoItem } from '../types.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return `${prefix || '(message)'} [long message]`
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

    const { context, display_kind, name, role, text, timestamp } = row as TranscriptRow

    const rawReasoning =
      (row as TranscriptRow).reasoning ??
      (row as TranscriptRow).reasoning_content ??
      (row as TranscriptRow).thinking

    const thinking = typeof rawReasoning === 'string' && rawReasoning.trim() ? rawReasoning.trim() : undefined

    const createdAt =
      typeof timestamp === 'number' && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : undefined

    if (role === 'tool') {
      pending.push(buildToolTrailLine(name ?? 'tool', context ?? ''))

      if (name === 'todo_list' || name === 'todo') {
        const rawTodos = (row as TranscriptRow).todos ?? (row as TranscriptRow).args?.todos
        const parsed = parseTodos(rawTodos)

        if (parsed && parsed.length > 0) {
          pendingTodos = parsed
        }
      }

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

    if (display_kind === 'model_switch') {
      out.push({ kind: 'event', role: 'system', text: 'model changed' })
      pending = []
      pendingTodos = null

      continue
    }

    if (display_kind === 'auto_continue') {
      out.push({ kind: 'event', role: 'system', text: 'resumed interrupted turn' })
      pending = []
      pendingTodos = null

      continue
    }

    if (display_kind === 'personality_switch') {
      out.push({ kind: 'event', role: 'system', text: 'personality changed' })
      pending = []
      pendingTodos = null

      continue
    }

    if (display_kind === 'async_delegation_complete') {
      const meta = (row as TranscriptRow).display_metadata
      const count = meta && typeof meta.task_count === 'number' ? meta.task_count : undefined

      const label =
        count === undefined
          ? 'background agent work finished'
          : `${count} background agent${count === 1 ? '' : 's'} finished`

      out.push({ kind: 'event', role: 'system', text: label })
      pending = []
      pendingTodos = null

      continue
    }

    if (role === 'assistant') {
      const assistantText = typeof text === 'string' ? text : ''

      if (pendingTodos && pendingTodos.length) {
        const done = isTodoDone(pendingTodos)
        out.push({
          kind: 'trail',
          role: 'system',
          text: '',
          todos: pendingTodos,
          ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
        })
        pendingTodos = null
      }

      const msg: Msg = {
        role,
        text: assistantText,
        ...(createdAt !== undefined && { createdAt }),
        ...(pending.length && { tools: pending }),
        ...(thinking && {
          thinking,
          thinkingTokens: estimateTokensRough(thinking)
        })
      }

      if (!assistantText.trim() && (thinking || pending.length)) {
        msg.kind = 'trail'
      }

      out.push(msg)
      pending = []
    } else if (role === 'user' || role === 'system') {
      if (pendingTodos && pendingTodos.length) {
        const done = isTodoDone(pendingTodos)
        out.push({
          kind: 'trail',
          role: 'system',
          text: '',
          todos: pendingTodos,
          ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
        })
        pendingTodos = null
      }

      if (pending.length) {
        out.push({ kind: 'trail', role: 'assistant', text: '', tools: pending })
        pending = []
      }

      if (hasText) {
        out.push({ role, text: text!, ...(createdAt !== undefined && { createdAt }) })
      }
    }
  }

  if (pendingTodos && pendingTodos.length) {
    const done = isTodoDone(pendingTodos)
    out.push({
      kind: 'trail',
      role: 'system',
      text: '',
      todos: pendingTodos,
      ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
    })
  }

  if (pending.length) {
    out.push({ kind: 'trail', role: 'assistant', text: '', tools: pending })
  }

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
  args?: Record<string, unknown>
  context?: string
  display_kind?: string
  display_metadata?: { task_count?: number; [key: string]: unknown }
  name?: string
  reasoning?: string
  reasoning_content?: string
  role?: string
  text?: string
  thinking?: string
  timestamp?: number
  todos?: unknown[]
}
