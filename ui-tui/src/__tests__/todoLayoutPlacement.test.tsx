import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { GatewayProvider } from '../app/gatewayContext.js'
import type { AppLayoutProps } from '../app/interfaces.js'
import { turnController } from '../app/turnController.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import { toTranscriptMessages } from '../domain/messages.js'
import type { GatewayClient } from '../gatewayTypes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'
import type { TodoItem } from '../types.js'

const gatewayStub = {
  gw: {
    request: () => new Promise<never>(() => {}),
    send: () => {}
  } as unknown as GatewayClient,
  rpc: (() => new Promise<never>(() => {})) as never
}

const layoutProps: AppLayoutProps = {
  actions: {
    activateLiveSession: () => {},
    answerApproval: () => {},
    answerClarify: () => {},
    answerClarifyQuestion: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: () => Promise.resolve(null),
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  },
  composer: {
    cols: 100,
    compIdx: 0,
    completions: [],
    empty: true,
    handleTextPaste: () => null,
    input: '',
    inputBuf: [],
    pagerPageSize: 10,
    queueEditIdx: null,
    queuedDisplay: [],
    submit: () => {},
    updateInput: () => {},
    voiceRecordKey: 'f8'
  },
  mouseTracking: 'off',
  progress: { showProgressArea: false },
  status: {
    cwdLabel: '~/repo',
    goodVibesTick: 0,
    lastTurnEndedAt: 1000,
    sessionStartedAt: 1000,
    sessionTitle: '',
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: '',
    turnStartedAt: null,
    voiceLabel: ''
  },
  transcript: {
    historyItems: [],
    scrollRef: { current: null },
    virtualHistory: {
      bottomSpacer: 0,
      end: 1,
      measureRef: () => () => {},
      offsets: [0],
      start: 0,
      topSpacer: 0
    },
    virtualRows: [
      {
        index: 0,
        key: 'user-0',
        msg: { role: 'user', text: 'solve this task with todos' }
      }
    ]
  }
}

describe('Todo Layout Placement & Trailing Position', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
  })

  it('places archived todos trailing after thinking and tools in recordMessageComplete', () => {
    const todos: TodoItem[] = [
      { content: 'Inspect repo', id: '1', status: 'completed' },
      { content: 'Fix bug', id: '2', status: 'in_progress' }
    ]

    patchTurnState({ todos })
    turnController.pendingSegmentTools = ['Read File("test.ts") (0.5s) ✓']
    turnController.reasoningText = 'Thinking about the fix...'

    const { finalMessages } = turnController.recordMessageComplete({ text: 'Fixed the bug.' })

    // Expected order: thinking -> tool details -> archived todos trailing -> final assistant text
    expect(finalMessages).toHaveLength(4)
    expect(finalMessages[0]?.thinking).toBe('Thinking about the fix...')
    expect(finalMessages[1]?.tools).toBeDefined()
    expect(finalMessages[2]?.todos).toEqual(todos)
    expect(finalMessages[2]?.kind).toBe('trail')
    expect(finalMessages[3]?.role).toBe('assistant')
    expect(finalMessages[3]?.text).toBe('Fixed the bug.')

    // Verify relative order: tools < todos < assistantText
    const toolIdx = finalMessages.findIndex(m => Boolean(m.tools?.length))
    const todoIdx = finalMessages.findIndex(m => Boolean(m.todos?.length))
    const textIdx = finalMessages.findIndex(m => m.role === 'assistant')

    expect(toolIdx).toBeLessThan(todoIdx)
    expect(todoIdx).toBeLessThan(textIdx)
  })

  it('rehydrates todos trailing after preceding tools in toTranscriptMessages', () => {
    const todos: TodoItem[] = [
      { content: 'Check logs', id: '1', status: 'completed' }
    ]

    const rows = [
      { role: 'user', text: 'check status' },
      { role: 'tool', context: 'cmd', name: 'terminal', text: 'server ok' },
      { role: 'tool', context: 'todos', name: 'todo_list', todos },
      { role: 'assistant', text: 'All checked.' }
    ]

    const result = toTranscriptMessages(rows)
    expect(result).toHaveLength(4)
    expect(result[0]?.role).toBe('user')
    // Preceding tools trail first
    expect(result[1]?.tools?.[0]).toContain('Terminal')
    // Todos trail second (trailing the tools)
    expect(result[2]?.todos).toEqual(todos)
    expect(result[2]?.kind).toBe('trail')
    // Final assistant reply last
    expect(result[3]?.role).toBe('assistant')
    expect(result[3]?.text).toBe('All checked.')
  })

  it('renders LiveTodoPanel in the bottom ComposerPane area above the status rule', () => {
    patchUiState({ sessionTitle: 'test', sid: 'sid-1', status: 'working' })
    patchTurnState({
      todos: [
        { content: 'Main goal', id: 'g1', status: 'in_progress' },
        { content: 'Sub task', id: 's1', parent: 'g1', status: 'pending' }
      ]
    })

    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let rendered = ''

    Object.assign(stdout, { columns: 100, isTTY: true, rows: 30 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })
    stdout.on('data', chunk => {
      rendered += chunk.toString()
    })

    const instance = renderSync(
      React.createElement(
        GatewayProvider,
        { value: gatewayStub },
        React.createElement(AppLayout, {
          ...layoutProps,
          transcript: {
            ...layoutProps.transcript,
            historyItems: [{ role: 'user', text: 'solve this task with todos' }]
          }
        })
      ),
      {
        patchConsole: false,
        stderr: stderr as NodeJS.WriteStream,
        stdin: stdin as NodeJS.ReadStream,
        stdout: stdout as NodeJS.WriteStream
      }
    )

    instance.unmount()
    instance.cleanup()

    const clean = stripAnsi(rendered)
    expect(clean).toContain('Todo')
    expect(clean).toContain('(0/2)')
    expect(clean).toContain('Maingoal')
    expect(clean).toContain('Subtask')
  })
})
