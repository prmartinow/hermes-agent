import { describe, expect, it } from 'vitest'

import { todoTree } from '../lib/todo.js'
import type { TodoItem } from '../types.js'

describe('todoTree hierarchy & DFS indentation', () => {
  it('orders multi-tiered goals, sub-goals, and sub-sub-goals in DFS order with depths', () => {
    const todos: TodoItem[] = [
      { content: 'Main Goal 1', id: 'goal-1', status: 'in_progress' },
      { content: 'Sub-goal 1.1', id: 'sub-1-1', parent: 'goal-1', status: 'completed' },
      { content: 'Sub-sub-goal 1.1.1', id: 'sub-1-1-1', parent: 'sub-1-1', status: 'completed' },
      { content: 'Sub-goal 1.2', id: 'sub-1-2', parent: 'goal-1', status: 'in_progress' },
      { content: 'Main Goal 2', id: 'goal-2', status: 'pending' },
      { content: 'Sub-goal 2.1', id: 'sub-2-1', parent: 'goal-2', status: 'pending' },
    ]

    const tree = todoTree(todos)
    expect(tree.map(([item, depth]) => [item.id, depth])).toEqual([
      ['goal-1', 0],
      ['sub-1-1', 1],
      ['sub-1-1-1', 2],
      ['sub-1-2', 1],
      ['goal-2', 0],
      ['sub-2-1', 1],
    ])
  })

  it('handles flat lists with all depth 0', () => {
    const todos: TodoItem[] = [
      { content: 'A', id: '1', status: 'pending' },
      { content: 'B', id: '2', status: 'pending' },
    ]

    const tree = todoTree(todos)
    expect(tree.map(([item, depth]) => [item.id, depth])).toEqual([
      ['1', 0],
      ['2', 0],
    ])
  })

  it('recovers from dangling parent pointers gracefully by degrading to root depth 0', () => {
    const todos: TodoItem[] = [
      { content: 'Orphan item', id: 'orphan', parent: 'non-existent', status: 'pending' },
      { content: 'Root item', id: 'root', status: 'pending' },
    ]

    const tree = todoTree(todos)
    expect(tree.map(([item, depth]) => [item.id, depth])).toEqual([
      ['orphan', 0],
      ['root', 0],
    ])
  })
})
