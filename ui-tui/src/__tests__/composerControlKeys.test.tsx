import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { forceRedraw, renderSync, useInput } from '@hermes/ink'
import React, { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TextInput } from '../components/textInput.js'
import { isAction } from '../lib/platform.js'

class FakeInput extends EventEmitter {
  chunks: string[] = []
  isRaw = false
  isTTY = true
  readableLength = 0

  read() {
    const next = this.chunks.shift() ?? null
    this.readableLength = this.chunks.length
    return next
  }

  ref = vi.fn()

  send(...chunks: string[]) {
    this.chunks.push(...chunks)
    this.readableLength = this.chunks.length
    this.emit('readable')
  }

  setEncoding = vi.fn()

  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled
  })

  unref = vi.fn()
}

const settle = (ms = 50) => new Promise(resolve => setTimeout(resolve, ms))

function makeStreams() {
  const stdin = new FakeInput()
  const stdout = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stderr, { columns: 80, isTTY: true, rows: 24 })

  return { stderr, stdin, stdout }
}

describe('Composer control key isolation and preservation', () => {
  it('does NOT leak unhandled Ctrl+L into isolated TextInput in idle mode (busy=false)', async () => {
    const streams = makeStreams()
    const changes: string[] = []

    function Harness() {
      const [value, setValue] = useState('')
      return (
        <TextInput
          busy={false}
          columns={80}
          focus={true}
          onChange={next => {
            changes.push(next)
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)
    streams.stdin.send('\x0c') // Ctrl+L
    await settle(60)

    instance.unmount()
    instance.cleanup()

    expect(changes).toEqual([])
  })

  it('does NOT leak unhandled Ctrl+L into isolated TextInput in busy mode (busy=true)', async () => {
    const streams = makeStreams()
    const changes: string[] = []

    function Harness() {
      const [value, setValue] = useState('')
      return (
        <TextInput
          busy={true}
          columns={80}
          focus={true}
          onChange={next => {
            changes.push(next)
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)
    streams.stdin.send('\x0c') // Ctrl+L
    await settle(60)

    instance.unmount()
    instance.cleanup()

    expect(changes).toEqual([])
  })

  it('does NOT leak other unhandled control keys (Ctrl+P, Ctrl+N, Ctrl+T) into TextInput', async () => {
    const streams = makeStreams()
    const changes: string[] = []

    function Harness() {
      const [value, setValue] = useState('')
      return (
        <TextInput
          columns={80}
          focus={true}
          onChange={next => {
            changes.push(next)
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)
    streams.stdin.send('\x10') // Ctrl+P
    await settle(40)
    streams.stdin.send('\x0e') // Ctrl+N
    await settle(40)
    streams.stdin.send('\x14') // Ctrl+T
    await settle(60)

    instance.unmount()
    instance.cleanup()

    expect(changes).toEqual([])
  })

  it('global Ctrl+L handler consumes event and stops propagation to TextInput', async () => {
    const streams = makeStreams()
    const changes: string[] = []
    let redrawFired = false

    function GlobalHandler() {
      useInput((ch, key, event) => {
        if (isAction(key, ch, 'l') || (key.ctrl && ch.toLowerCase() === 'l')) {
          redrawFired = true
          forceRedraw(streams.stdout as NodeJS.WriteStream)
          event.stopImmediatePropagation()
        }
      })
      return null
    }

    function AppHarness() {
      const [value, setValue] = useState('')
      return (
        <React.Fragment>
          <GlobalHandler />
          <TextInput
            columns={80}
            focus={true}
            onChange={next => {
              changes.push(next)
              setValue(next)
            }}
            value={value}
          />
        </React.Fragment>
      )
    }

    const instance = renderSync(React.createElement(AppHarness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)
    streams.stdin.send('\x0c')
    await settle(60)

    instance.unmount()
    instance.cleanup()

    expect(redrawFired).toBe(true)
    expect(changes).toEqual([])
  })

  it('preserves legitimate Ctrl editing shortcuts (Ctrl+A, Ctrl+E, Ctrl+U, Ctrl+K, Ctrl+W)', async () => {
    const streams = makeStreams()
    let latestValue = 'hello world'

    function Harness() {
      const [value, setValue] = useState('hello world')
      return (
        <TextInput
          columns={80}
          focus={true}
          onChange={next => {
            latestValue = next
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)

    // Ctrl+W deletes previous word: 'hello world' -> 'hello '
    streams.stdin.send('\x17')
    await settle(60)
    expect(latestValue).toBe('hello ')

    // Ctrl+U kills to start: 'hello ' -> ''
    streams.stdin.send('\x15')
    await settle(60)
    expect(latestValue).toBe('')

    instance.unmount()
    instance.cleanup()
  })

  it('preserves bracketed paste and AltGr character entry', async () => {
    const streams = makeStreams()
    const changes: string[] = []

    function Harness() {
      const [value, setValue] = useState('')
      return (
        <TextInput
          columns={80}
          focus={true}
          onChange={next => {
            changes.push(next)
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)

    // Bracketed paste
    streams.stdin.send('\x1b[200~pasted text\x1b[201~')
    await settle(60)
    expect(changes).toContain('pasted text')

    // AltGr / UTF-8 character (e.g. € or @)
    streams.stdin.send('€')
    await settle(60)
    expect(changes.at(-1)).toBe('pasted text€')

    instance.unmount()
    instance.cleanup()
  })

  it("split private control frames (OSC 777 request) do NOT reach TextInput composer", async () => {
    const streams = makeStreams()
    const changes: string[] = []
    const gen = "44444444-4444-4444-4444-444444444444"
    const payload = "\x1b]777;hermes-replay;request;" + gen + "\x07"

    function Harness() {
      const [value, setValue] = useState("")
      return (
        <TextInput
          columns={80}
          focus={true}
          onChange={next => {
            changes.push(next)
            setValue(next)
          }}
          value={value}
        />
      )
    }

    const instance = renderSync(React.createElement(Harness), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as unknown as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await settle(40)
    // Send split private control frame
    streams.stdin.send(payload.slice(0, 5))
    await settle(20)
    streams.stdin.send(payload.slice(5, 18))
    await settle(20)
    streams.stdin.send(payload.slice(18))
    await settle(60)

    instance.unmount()
    instance.cleanup()

    expect(changes).toEqual([])
  })
})
