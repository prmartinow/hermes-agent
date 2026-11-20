import { format } from 'util'

import { logForDebugging } from '../../utils/debug.js'
import { logError } from '../../utils/log.js'

const CONSOLE_STDOUT_METHODS = [
  'log',
  'info',
  'debug',
  'dir',
  'dirxml',
  'count',
  'countReset',
  'group',
  'groupCollapsed',
  'groupEnd',
  'table',
  'time',
  'timeEnd',
  'timeLog'
] as const

const CONSOLE_STDERR_METHODS = ['warn', 'error', 'trace'] as const

export function patchConsole(): () => void {
  // biome-ignore lint/suspicious/noConsole: intentionally patching global console
  const con = console
  const originals: Partial<Record<keyof Console, Console[keyof Console]>> = {}
  const toDebug = (...args: unknown[]) => logForDebugging(`console.log: ${format(...args)}`)
  const toError = (...args: unknown[]) => logError(new Error(`console.error: ${format(...args)}`))

  for (const m of CONSOLE_STDOUT_METHODS) {
    originals[m] = con[m]
    con[m] = toDebug
  }

  for (const m of CONSOLE_STDERR_METHODS) {
    originals[m] = con[m]
    con[m] = toError
  }

  originals.assert = con.assert

  con.assert = (condition: unknown, ...args: unknown[]) => {
    if (!condition) {
      toError(...args)
    }
  }

  return () => Object.assign(con, originals)
}

/**
 * Intercept process.stderr.write so stray writes (config.ts, hooks.ts,
 * third-party deps) don't corrupt the alt-screen buffer. patchConsole only
 * hooks console.* methods — direct stderr writes bypass it, land at the
 * parked cursor, scroll the alt-screen, and desync frontFrame from the
 * physical terminal. Next diff writes only changed-in-React cells at
 * absolute coords → interleaved garbage.
 *
 * Swallows the write (routes text to the debug log) and, in alt-screen,
 * lets the renderer invalidate its previous frame. Not patching
 * process.stdout — Ink itself writes there.
 */
export function patchStderr(onWrite: () => void): () => void {
  const stderr = process.stderr
  const originalWrite = stderr.write
  let reentered = false

  const intercept = (
    chunk: Uint8Array | string,
    encodingOrCb?: BufferEncoding | ((err?: Error | null) => void),
    cb?: (err?: Error | null) => void
  ): boolean => {
    const callback = typeof encodingOrCb === 'function' ? encodingOrCb : cb

    // Reentrancy guard: logForDebugging → writeToStderr → here. Pass
    // through to the original so --debug-to-stderr still works and we
    // don't stack-overflow.
    if (reentered) {
      const encoding = typeof encodingOrCb === 'string' ? encodingOrCb : undefined

      return originalWrite.call(stderr, chunk, encoding, callback)
    }

    reentered = true

    try {
      const text = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
      logForDebugging(`[stderr] ${text}`, {
        level: 'warn'
      })

      onWrite()
    } finally {
      reentered = false
      callback?.()
    }

    return true
  }

  stderr.write = intercept

  return () => {
    if (stderr.write === intercept) {
      stderr.write = originalWrite
    }
  }
}
