// @vitest-environment jsdom

import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatSessionList } from './ChatSessionList'

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const { getSessions } = vi.hoisted(() => ({ getSessions: vi.fn() }))

vi.mock('@nous-research/ui/ui/components/button', () => ({
  Button: ({
    children,
    ghost,
    onClick,
    outlined,
    prefix,
    size,
    ...props
  }: React.ButtonHTMLAttributes<HTMLButtonElement> & { ghost?: boolean; outlined?: boolean; size?: string }) => {
    void ghost
    void outlined
    void prefix
    void size
    return (
      <button type="button" onClick={onClick} {...props}>
        {children}
      </button>
    )
  }
}))

vi.mock('@nous-research/ui/ui/components/list-item', () => ({
  ListItem: ({ children, onClick, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button type="button" onClick={onClick} {...props}>
      {children}
    </button>
  )
}))

vi.mock('@nous-research/ui/ui/components/spinner', () => ({
  Spinner: () => <span>Loading</span>
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { loading: 'Loading', refresh: 'Refresh', retry: 'Retry' },
      sessions: {
        newChat: 'New chat',
        noSessions: 'No sessions',
        title: 'Sessions',
        untitledSession: 'Untitled session'
      }
    }
  })
}))

vi.mock('@/lib/api', () => ({ api: { getSessions } }))

vi.mock('@/lib/utils', () => ({
  cn: (...classes: Array<string | false | null | undefined>) => classes.filter(Boolean).join(' '),
  timeAgo: () => 'just now'
}))

const mockSessions = [
  {
    id: "session-1",
    title: "Research Paper Review",
    preview: "First turn preview text",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 100000,
    ended_at: null,
    last_active: Date.now() - 60000,
    is_active: true,
    message_count: 14,
    total_message_count: 14,
    tool_call_count: 2,
    input_tokens: 1000,
    output_tokens: 500,
    source: "tui",
    account_alias: "prm",
  },
  {
    id: "session-2",
    title: "Untrusted Task",
    preview: "Second turn preview",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 200000,
    ended_at: Date.now() - 120000,
    last_active: Date.now() - 120000,
    is_active: false,
    message_count: 5,
    total_message_count: 5,
    tool_call_count: 0,
    input_tokens: 200,
    output_tokens: 100,
    source: "cli",
    account_alias: null,
  },
  {
    id: "session-3",
    title: "CD-DOCI Tuning",
    preview: "Third turn preview",
    model: "gemini-3.7-flash-high",
    started_at: Date.now() - 500000,
    ended_at: null,
    last_active: Date.now() - 300000,
    is_active: false,
    message_count: 8,
    total_message_count: 20,
    tool_call_count: 4,
    input_tokens: 4000,
    output_tokens: 1200,
    source: "web",
    account_alias: "tnn",
  },
];


function session(id: string, title: string) {
  return {
    id,
    source: 'cli',
    model: null,
    title,
    started_at: 0,
    ended_at: null,
    last_active: 0,
    is_active: false,
    message_count: 0,
    tool_call_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    preview: null
  }
}

function response(id: string, title: string) {
  return { sessions: [session(id, title)], total: 1, limit: 30, offset: 0 }
}

describe('ChatSessionList', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.useFakeTimers()
    getSessions.mockReset()
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'visible'
    })
    container = document.createElement('div')
    document.body.append(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    container.remove()
    vi.useRealTimers()
  })

  it('renders sessions returned by an automatic refresh', async () => {
    getSessions
      .mockResolvedValueOnce(response('first', 'First session'))
      .mockResolvedValueOnce(response('second', 'Newly created session'))

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ChatSessionList activeSessionId={null} />
        </MemoryRouter>
      )
    })

    expect(container.textContent).toContain('First session')
    expect(getSessions).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })

    expect(getSessions).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('Newly created session')
  })

  it('refreshes immediately when the tab becomes visible', async () => {
    getSessions
      .mockResolvedValueOnce(response('first', 'First session'))
      .mockResolvedValueOnce(response('second', 'Newly created session'))

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ChatSessionList activeSessionId={null} />
        </MemoryRouter>
      )
    })

    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'visible'
    })
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    expect(getSessions).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('Newly created session')
  })

  it('waits for the Chat tab to become active before loading or polling', async () => {
    getSessions
      .mockResolvedValueOnce(response('first', 'First session'))
      .mockResolvedValueOnce(response('second', 'Newly created session'))

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ChatSessionList activeSessionId={null} isActive={false} />
        </MemoryRouter>
      )
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    expect(getSessions).not.toHaveBeenCalled()

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ChatSessionList activeSessionId={null} isActive />
        </MemoryRouter>
      )
    })

    expect(getSessions).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('First session')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })

    expect(getSessions).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('Newly created session')
  })
})


describe('ChatSessionList account metadata', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    getSessions.mockReset()
    getSessions.mockResolvedValue({ sessions: mockSessions, total: mockSessions.length })
    container = document.createElement('div')
    document.body.append(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    container.remove()
  })

  async function render() {
    await act(async () => {
      root.render(<MemoryRouter><ChatSessionList activeSessionId="session-1" /></MemoryRouter>)
    })
  }

  function row(title: string) {
    return Array.from(container.querySelectorAll('button')).find(button => button.textContent?.includes(title))
  }

  it('keeps stored account attribution out of the chat session list', async () => {
    await render()
    expect(row('Research Paper Review')?.textContent).not.toMatch(/\bprm\b/)
    expect(row('CD-DOCI Tuning')?.textContent).not.toMatch(/\btnn\b/)
    await act(async () => document.dispatchEvent(new Event('visibilitychange')))
    expect(row('Research Paper Review')?.textContent).not.toMatch(/\bprm\b/)
  })

  it('renders clean metadata separators', async () => {
    await render()
    const subline = row('Research Paper Review')?.querySelector('span.text-\\[0\\.6875rem\\]')
    expect(subline?.textContent).toContain('14 msgs')
    expect(subline?.textContent).toContain('tui')
    expect(subline?.textContent).not.toContain('prm')
    expect(subline?.querySelectorAll('span[aria-hidden]')).toHaveLength(2)
  })

  it('shows total messages with the compacted active-count tooltip', async () => {
    await render()
    const messages = row('CD-DOCI Tuning')?.querySelector('span[title*="total msgs"]')
    expect(messages?.getAttribute('title')).toBe('20 total msgs (8 active in context)')
    expect(messages?.textContent).toBe('20 msgs')
  })

})
