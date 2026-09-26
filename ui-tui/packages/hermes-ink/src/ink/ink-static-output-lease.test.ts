import { EventEmitter } from 'events'
import React, { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import Text from './components/Text.js'
import Box from './components/Box.js'
import Ink from './ink.js'
import instances from './instances.js'
import { acquireMainScreenStaticOutput, type MainScreenStaticOutputLease } from './root.js'

class MockTty extends EventEmitter {
  chunks: string[] = []
  columns = 80
  rows = 24
  isTTY = true
  private pendingDrains: Array<(err?: Error | null) => void> = []

  write(chunk: string | Uint8Array, cb?: any): boolean {
    const str = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    this.chunks.push(str)
    if (typeof cb === 'function') {
      cb()
    }
    return true
  }
}

class BackpressureTty extends EventEmitter {
  chunks: string[] = []
  columns = 80
  rows = 24
  isTTY = true
  writeReturns = true

  write(chunk: string | Uint8Array, cb?: any): boolean {
    const str = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    this.chunks.push(str)
    if (!this.writeReturns) {
      if (typeof cb === 'function') {
        this.once('drain', cb)
      }
      return false
    }
    if (typeof cb === 'function') {
      cb()
    }
    return true
  }

  simulateDrain() {
    this.emit('drain')
  }
}

function createTestInk(stdout: any) {
  const stdin = new EventEmitter() as any
  const stderr = new MockTty() as any
  const ink = new Ink({
    stdout: stdout as any,
    stdin,
    stderr,
    exitOnCtrlC: false,
    patchConsole: false
  })
  instances.set(stdout as any, ink)
  return ink
}

describe('Hermes Ink Main-Screen Static Output Lease (Step A)', () => {
  it('acquires lease on active instance and fails closed on unregistered stream', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      const lease = await acquireMainScreenStaticOutput(stdout as any)
      expect(lease).toBeDefined()
      expect(typeof lease.token).toBe('symbol')

      // Unregistered stream throws
      const otherStdout = new MockTty()
      await expect(acquireMainScreenStaticOutput(otherStdout as any)).rejects.toThrow(
        'No active Ink instance found for stdout'
      )

      await lease.release()
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('rejects acquisition when alternate screen is active', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)
    ink.setAltScreenActive(true)

    try {
      await expect(acquireMainScreenStaticOutput(stdout as any)).rejects.toThrow(
        'main-screen static output lease unavailable in alternate screen'
      )
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('rejects concurrent acquisition while lease is already held', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      const lease1 = await acquireMainScreenStaticOutput(stdout as any)
      await expect(acquireMainScreenStaticOutput(stdout as any)).rejects.toThrow(
        'main-screen output already leased'
      )
      await lease1.release()
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('rejects wrong-token operations', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      const lease = await acquireMainScreenStaticOutput(stdout as any)
      const fakeToken = Symbol('FakeToken')

      await expect(ink.writeMainScreenStaticOutput(fakeToken, 'data')).rejects.toThrow(
        'Invalid or unheld main-screen static output lease'
      )
      expect(() => ink.prepareMainScreenAppendHandoff(fakeToken)).toThrow(
        'Invalid or unheld main-screen static output lease'
      )
      await expect(ink.releaseMainScreenStaticOutput(fakeToken)).rejects.toThrow(
        'Invalid or unheld main-screen static output lease'
      )
      await expect(ink.abortMainScreenStaticOutput(fakeToken)).rejects.toThrow(
        'Invalid or unheld main-screen static output lease'
      )

      await lease.release()
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('suppresses Ink renders, forceRedraw, requestRedraw, and mode healing while leased', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    let updateCount: () => void = () => {}

    function DynamicComp() {
      const [count, setCount] = useState(0)
      updateCount = () => setCount(c => c + 1)
      return React.createElement(Box, null, React.createElement(Text, null, `Count: ${count}`))
    }

    ink.render(React.createElement(DynamicComp, null))
    await new Promise(r => setTimeout(r, 20))

    const baselineWrites = stdout.chunks.length
    expect(baselineWrites).toBeGreaterThan(0)

    const lease = await acquireMainScreenStaticOutput(stdout as any)
    const countAfterAcquire = stdout.chunks.length

    // 1. React re-render occurs while leased
    updateCount()
    await new Promise(r => setTimeout(r, 20))
    expect(stdout.chunks.length).toBe(countAfterAcquire)

    // 2. forceRedraw called while leased emits zero bytes
    ink.forceRedraw()
    expect(stdout.chunks.length).toBe(countAfterAcquire)

    // 3. requestRedraw called while leased emits zero bytes
    ink.requestRedraw('gen-123')
    expect(stdout.chunks.length).toBe(countAfterAcquire)

    // 4. reassertTerminalModes called while leased emits zero bytes
    ink.reassertTerminalModes()
    expect(stdout.chunks.length).toBe(countAfterAcquire)

    // Clean release allows normal renders to resume
    await lease.release()
    await new Promise(r => setTimeout(r, 20))

    expect(stdout.chunks.length).toBeGreaterThan(countAfterAcquire)

    ink.unmount()
    instances.delete(stdout as any)
  })

  it('captures terminal resize without emitting terminal writes while leased', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    ink.render(React.createElement(Text, null, 'Resize test'))
    await new Promise(r => setTimeout(r, 20))

    const lease = await acquireMainScreenStaticOutput(stdout as any)
    const countAfterAcquire = stdout.chunks.length

    // Simulate terminal window resize
    stdout.columns = 120
    stdout.rows = 40
    stdout.emit('resize')

    await new Promise(r => setTimeout(r, 20))

    // Zero stdout writes emitted during resize while leased
    expect(stdout.chunks.length).toBe(countAfterAcquire)

    // Ink internal dimensions were updated
    expect((ink as any).terminalColumns).toBe(120)
    expect((ink as any).terminalRows).toBe(40)
    expect((ink as any).mainScreenLease.resized).toBe(true)

    await lease.release()
    ink.unmount()
    instances.delete(stdout as any)
  })

  it('allows static owner writes with backpressure and rejects writes after release', async () => {
    const stdout = new BackpressureTty()
    const ink = createTestInk(stdout)

    try {
      const lease = await acquireMainScreenStaticOutput(stdout as any)
      const countAfterAcquire = stdout.chunks.length

      // 1. Static write succeeds
      await lease.write('static-line-1\r\n')
      expect(stdout.chunks.length).toBe(countAfterAcquire + 1)
      expect(stdout.chunks[stdout.chunks.length - 1]).toBe('static-line-1\r\n')

      // 2. Backpressure handling
      stdout.writeReturns = false
      let writeResolved = false
      const writePromise = lease.write('static-line-2\r\n').then(() => {
        writeResolved = true
      })

      // Drain has not fired yet
      await new Promise(r => setTimeout(r, 10))
      expect(writeResolved).toBe(false)

      // Fire drain event
      stdout.simulateDrain()
      await writePromise
      expect(writeResolved).toBe(true)

      // 3. Prepare append handoff and release
      lease.prepareAppendHandoff()
      await lease.release()

      // 4. Writing after release throws
      await expect(lease.write('post-release')).rejects.toThrow(
        'Invalid or unheld main-screen static output lease'
      )
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('fails closed on dirty release without append handoff', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      const lease = await acquireMainScreenStaticOutput(stdout as any)
      await lease.write('modified physical terminal\r\n')

      // Releasing without prepareAppendHandoff throws reconstruction required
      await expect(lease.release()).rejects.toThrow(
        'Cannot release dirty main-screen static output lease without handoff: reconstruction required'
      )

      // Ink remains paused
      expect((ink as any).isPaused).toBe(true)
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('abort() reports requiresReconstruction: false when clean, true when dirty', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      // 1. Clean abort
      const cleanLease = await acquireMainScreenStaticOutput(stdout as any)
      const cleanAbort = await cleanLease.abort()
      expect(cleanAbort.requiresReconstruction).toBe(false)
      expect((ink as any).isPaused).toBe(false)

      // 2. Dirty abort
      const dirtyLease = await acquireMainScreenStaticOutput(stdout as any)
      await dirtyLease.write('dirty data')
      const dirtyAbort = await dirtyLease.abort()
      expect(dirtyAbort.requiresReconstruction).toBe(true)
      expect((ink as any).isPaused).toBe(true) // fails closed, remains paused
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('awaits prior stdout flush before lease acquisition resolves (byte-boundary invariant)', async () => {
    class DelayedFlushTty extends EventEmitter {
      chunks: string[] = []
      columns = 80
      rows = 24
      isTTY = true
      flushCallbacks: Array<() => void> = []

      write(chunk: string | Uint8Array, cb?: any): boolean {
        const str = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
        this.chunks.push(str)
        if (str === '') {
          // Stream flush barrier from acquireMainScreenStaticOutput
          if (typeof cb === 'function') {
            this.flushCallbacks.push(cb)
          }
        } else {
          if (typeof cb === 'function') {
            cb()
          }
        }
        return true
      }

      fireFlush() {
        const cbs = this.flushCallbacks
        this.flushCallbacks = []
        for (const cb of cbs) cb()
      }
    }

    const stdout = new DelayedFlushTty()
    const ink = createTestInk(stdout)

    try {
      let resolved = false
      const acquirePromise = acquireMainScreenStaticOutput(stdout as any).then(l => {
        resolved = true
        return l
      })

      // Must remain pending until stdout flush barrier completes
      await new Promise(r => setTimeout(r, 20))
      expect(resolved).toBe(false)
      expect(stdout.flushCallbacks.length).toBe(1)

      // Fire the flush barrier
      stdout.fireFlush()
      const lease = await acquirePromise
      expect(resolved).toBe(true)
      expect(lease).toBeDefined()

      await lease.release()
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })

  it('never emits alternate screen sequences during acquire, write, or release', async () => {
    const stdout = new MockTty()
    const ink = createTestInk(stdout)

    try {
      const lease = await acquireMainScreenStaticOutput(stdout as any)
      await lease.write('test data\r\n')
      lease.prepareAppendHandoff()
      await lease.release()

      const allOutput = stdout.chunks.join('')
      expect(allOutput.includes('\x1b[?1049h')).toBe(false)
      expect(allOutput.includes('\x1b[?1049l')).toBe(false)
    } finally {
      ink.unmount()
      instances.delete(stdout as any)
    }
  })
})
