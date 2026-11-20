import { describe, expect, it } from 'vitest'

import {
  PTY_GAVE_UP_BANNER,
  PTY_SESSION_ENDED_MESSAGE,
  PTY_START_FAILED_MESSAGE,
  PTY_TOKEN_MISSING_BANNER,
  ptyReconnectExhausted,
  ptyRejectionBanner
} from './pty-close-copy'

describe('pty close banners', () => {
  it('offers a Reload action for the stale-token and missing-token cases', () => {
    expect(ptyRejectionBanner(4401)?.action).toBe('reload')
    expect(PTY_TOKEN_MISSING_BANNER.action).toBe('reload')
  })

  it('treats server rejections as banners but not transient drops or the agent exit', () => {
    for (const code of [4401, 4403, 4404, 4408]) {
      expect(ptyRejectionBanner(code)).not.toBeNull()
    }
    // Transient drops and the agent's own exit are not rejections: the caller
    // must route them to the reconnect ladder / restart affordance instead.
    expect(ptyRejectionBanner(1006)).toBeNull()
    expect(ptyRejectionBanner(4410)).toBeNull()
  })

  it('stops retrying after the last attempt and points at the server', () => {
    expect(ptyReconnectExhausted(5, 5)).toBe(true)
    expect(ptyReconnectExhausted(4, 5)).toBe(false)
    expect(PTY_GAVE_UP_BANNER.action).toBe('check-server')
    expect(PTY_GAVE_UP_BANNER.text).toContain('hermes dashboard')
    expect(PTY_SESSION_ENDED_MESSAGE).toMatch(/crashed/i)
    expect(PTY_SESSION_ENDED_MESSAGE).toMatch(/logs/i)
  })
})

describe('resource admission refusals', () => {
  it('offers same-page reload and distinguishes memory from terminal count', () => {
    const memory = ptyRejectionBanner(4429, 'terminal-memory')
    const capacity = ptyRejectionBanner(4429, 'terminal-capacity')
    expect(memory?.action).toBe('reload')
    expect(memory?.text).toMatch(/memory/i)
    expect(memory?.text).not.toMatch(/too many|new session/i)
    expect(capacity?.action).toBe('reload')
    expect(capacity?.text).toMatch(/capacity/i)
    expect(capacity?.text).not.toMatch(/new session/i)
    expect(ptyRejectionBanner(4429)?.action).toBe('reload')
    expect(ptyRejectionBanner(4429, 'untrusted arbitrary reason')?.text).not.toContain('untrusted')
  })
})

describe('start-failed overlay copy', () => {
  it('stays neutral: close 1011 also means "no terminal support here", where retrying cannot help', () => {
    expect(PTY_START_FAILED_MESSAGE).toMatch(/printed above/)
    expect(PTY_START_FAILED_MESSAGE).not.toMatch(/fix it|Start new session/)
    expect(PTY_START_FAILED_MESSAGE).not.toMatch(/1011/)
  })
})
