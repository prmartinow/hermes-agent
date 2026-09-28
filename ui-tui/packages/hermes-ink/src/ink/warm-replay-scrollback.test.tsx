import React from 'react'
import { createRequire } from 'node:module'
import { PassThrough, Writable } from 'node:stream'
import { expect, it } from 'vitest'
import Text from './components/Text.js'
import { acquireMainScreenStaticOutput, createRoot, requestRedraw } from './root.js'

const { Terminal } = createRequire(import.meta.url)('@xterm/xterm')
const settle = () => new Promise(resolve => setTimeout(resolve, 60))

async function fixture(tailLines = 1) {
  const chunks: string[] = []
  const stdout = Object.assign(new Writable({ write(chunk, _encoding, done) {
    chunks.push(chunk.toString()); done()
  } }), { isTTY: true, rows: 12, columns: 80 }) as unknown as NodeJS.WriteStream
  const stdin = Object.assign(new PassThrough(), { isTTY: false }) as unknown as NodeJS.ReadStream
  const root = await createRoot({ stdout, stdin, stderr: stdout, patchConsole: false, exitOnCtrlC: false })
  root.render(React.createElement(Text, null, 'loading'))
  await settle()
  const lease = await acquireMainScreenStaticOutput(stdout)
  await lease.beginStaticAppendSurface()
  await lease.write(Array.from({ length: 300 }, (_, i) => `ARCHIVE-${i}\r\n`).join(''))
  root.render(React.createElement(Text, null, Array.from({ length: tailLines }, (_, i) => `LIVE-TAIL-${i}`).join('\n')))
  await settle()
  lease.prepareAppendHandoff()
  await lease.release()
  await settle()
  return { root, stdout, chunks }
}

it.each([1, 200])('settled warm replay preserves full history and a %i-line live frame without duplication', async tailLines => {
  const { root, stdout, chunks } = await fixture(tailLines)
  const term = new Terminal({ cols: 80, rows: 12, scrollback: 20000 })
  const write = (text: string) => new Promise<void>(resolve => term.write(text, resolve))
  const lines = () => Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i).translateToString(true))
  try {
    // A newly attached browser first consumes the complete retained byte stream.
    await write(chunks.join(''))
    const original = lines()
    expect(original.join('\n')).toContain('ARCHIVE-0\n')
    expect(original.join('\n')).toContain('ARCHIVE-299')
    expect(original.join('\n')).toContain('LIVE-TAIL')
    const baseY = term.buffer.active.baseY
    for (const generation of ['11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222']) {
      chunks.length = 0
      requestRedraw(generation, stdout)
      await settle()
      const replay = chunks.join('')
      const seen: string[] = []
      const observer = term.parser.registerOscHandler(777, (data: string) => { seen.push(data); return false })
      await write(replay)
      observer.dispose()
      expect(seen).toContain(`hermes-replay;end;${generation}`)
      expect(replay).not.toContain('\x1b[3J')
      expect(term.buffer.active.baseY).toBe(baseY)
      expect(lines()).toEqual(original)
    }
  } finally { root.unmount(); term.dispose() }
})

it('reports abort, not successful replay, when leased history requires reconstruction', async () => {
  const { root, stdout, chunks } = await fixture()
  const lease = await acquireMainScreenStaticOutput(stdout)
  const generation = '44444444-4444-4444-4444-444444444444'
  try {
    chunks.length = 0
    await lease.write('PARTIAL-HISTORY\r\n')
    requestRedraw(generation, stdout)
    await lease.reconstructAndRelease()
    await settle()
    const output = chunks.join('')
    expect(output).toContain(`hermes-replay;abort;${generation}`)
    expect(output).not.toContain(`hermes-replay;end;${generation}`)
  } finally { root.unmount() }
})

it('a warm replay requested during a static lease completes only after the handoff', async () => {
  const { root, stdout, chunks } = await fixture()
  const lease = await acquireMainScreenStaticOutput(stdout)
  const generation = '33333333-3333-3333-3333-333333333333'
  try {
    chunks.length = 0
    requestRedraw(generation, stdout)
    await settle()
    expect(chunks.join('')).not.toContain(`hermes-replay;end;${generation}`)
    await lease.write('LATE-ARCHIVE\r\n')
    lease.prepareAppendHandoff()
    await lease.release()
    await settle()
    const output = chunks.join('')
    expect(output).toContain(`hermes-replay;begin;${generation}`)
    expect(output).toContain(`hermes-replay;end;${generation}`)
    expect(output.indexOf(`hermes-replay;end;${generation}`)).toBeGreaterThan(output.indexOf('LATE-ARCHIVE'))
  } finally { root.unmount() }
})
