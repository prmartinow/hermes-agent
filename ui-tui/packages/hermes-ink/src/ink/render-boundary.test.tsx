import React, { useLayoutEffect } from 'react'
import { PassThrough, Writable } from 'node:stream'
import { expect, it } from 'vitest'
import Text from './components/Text.js'
import { createRoot, writeAfterRender } from './root.js'

it('emits a committed frame before its completion boundary, once', async () => {
  const chunks: string[] = []
  const stdout = Object.assign(new Writable({ write(chunk, _encoding, done) { chunks.push(chunk.toString()); done() } }), { isTTY: true, rows: 12, columns: 80 }) as unknown as NodeJS.WriteStream
  const stdin = Object.assign(new PassThrough(), { isTTY: false }) as unknown as NodeJS.ReadStream
  const root = await createRoot({ stdout, stdin, stderr: stdout, patchConsole: false, exitOnCtrlC: false })
  const marker = '\x1b]777;hermes-replay;end;11111111-1111-1111-1111-111111111111\x07'
  function Frame() {
    useLayoutEffect(() => { writeAfterRender(marker, stdout, true) }, [])
    return React.createElement(Text, null, 'COMMITTED-HISTORY')
  }
  try {
    root.render(React.createElement(Frame))
    await new Promise(resolve => setTimeout(resolve, 100))
    const output = chunks.join('')
    expect(output).toContain('COMMITTED-HISTORY')
    expect(output.split(marker)).toHaveLength(2)
    expect(output.indexOf(marker)).toBeGreaterThan(output.indexOf('COMMITTED-HISTORY'))
    const boundaryWrite = chunks.find(chunk => chunk.includes(marker))!
    expect(boundaryWrite).toContain('COMMITTED-HISTORY')
  } finally { root.unmount() }
})
