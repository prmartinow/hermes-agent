import type { TodoItem } from '../types.js'

export type TodoTone = 'active' | 'body' | 'dim'

export const todoGlyph = (status: TodoItem['status']) =>
  status === 'completed' ? '[x]' : status === 'cancelled' ? '[-]' : status === 'in_progress' ? '[>]' : '[ ]'

export const todoTone = (status: TodoItem['status']): TodoTone =>
  status === 'in_progress' ? 'active' : status === 'pending' ? 'body' : 'dim'

/** DFS order of a (possibly nested) todo list: [item, depth] pairs, parents
 *  before children. Dangling/cyclic parents degrade to depth 0. Mirrors
 *  apps/desktop/src/lib/todos.ts's todoTree() so both surfaces render the
 *  same hierarchy from the same `parent` field. */
export function todoTree(todos: readonly TodoItem[]): [TodoItem, number][] {
  const ids = new Set(todos.map(t => t.id))
  const kids = new Map<string, TodoItem[]>()
  const roots: TodoItem[] = []

  for (const t of todos) {
    if (t.parent && ids.has(t.parent) && t.parent !== t.id) {
      const list = kids.get(t.parent) ?? []
      list.push(t)
      kids.set(t.parent, list)
    } else {
      roots.push(t)
    }
  }

  const out: [TodoItem, number][] = []
  const seen = new Set<string>()

  const walk = (item: TodoItem, depth: number) => {
    if (seen.has(item.id)) {
      return
    }

    seen.add(item.id)
    out.push([item, depth])

    for (const kid of kids.get(item.id) ?? []) {
      walk(kid, depth + 1)
    }
  }

  for (const root of roots) {
    walk(root, 0)
  }

  // Cycle members never reach a root — append them flat so nothing is lost.
  for (const t of todos) {
    if (!seen.has(t.id)) {
      seen.add(t.id)
      out.push([t, 0])
    }
  }

  return out
}

export const isTodoStatus = (status: unknown): status is TodoItem['status'] =>
  status === 'pending' || status === 'in_progress' || status === 'completed' || status === 'cancelled'

export const parseTodos = (value: unknown): null | TodoItem[] => {
  if (!Array.isArray(value)) {
    return null
  }

  return value
    .map(item => {
      if (!item || typeof item !== 'object') {
        return null
      }

      const row = item as Record<string, unknown>
      const status = row.status

      if (!isTodoStatus(status)) {
        return null
      }

      const id = String(row.id ?? '').trim()
      const parent = String(row.parent ?? '').trim()

      return {
        content: String(row.content ?? '').trim(),
        id,
        status,
        ...(parent && parent !== id ? { parent } : {})
      }
    })
    .filter((item): item is TodoItem => Boolean(item?.id && item.content))
}
