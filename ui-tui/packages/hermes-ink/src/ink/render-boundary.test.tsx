import React, { useLayoutEffect } from "react"
import { PassThrough, Writable } from "node:stream"
import { expect, it } from "vitest"
import Text from "./components/Text.js"
import { createRoot, requestRedraw, writeAfterRender } from "./root.js"

it("emits a committed frame before its completion boundary, once", async () => {
  const chunks: string[] = []
  const stdout = Object.assign(new Writable({ write(chunk, _encoding, done) { chunks.push(chunk.toString()); done() } }), { isTTY: true, rows: 12, columns: 80 }) as unknown as NodeJS.WriteStream
  const stdin = Object.assign(new PassThrough(), { isTTY: false }) as unknown as NodeJS.ReadStream
  const root = await createRoot({ stdout, stdin, stderr: stdout, patchConsole: false, exitOnCtrlC: false })
  const marker = "\x1b]777;hermes-replay;end;11111111-1111-1111-1111-111111111111\x07"
  function Frame() {
    useLayoutEffect(() => { writeAfterRender(marker, stdout, true) }, [])
    return React.createElement(Text, null, "COMMITTED-HISTORY")
  }
  try {
    root.render(React.createElement(Frame))
    await new Promise(resolve => setTimeout(resolve, 100))
    const output = chunks.join("")
    expect(output).toContain("COMMITTED-HISTORY")
    expect(output.split(marker)).toHaveLength(2)
    expect(output.indexOf(marker)).toBeGreaterThan(output.indexOf("COMMITTED-HISTORY"))
    const boundaryWrite = chunks.find(chunk => chunk.includes(marker))!
    expect(boundaryWrite).toContain("COMMITTED-HISTORY")
  } finally { root.unmount() }
})

it("handles repeated redraw generation correctly and emits end after fresh frame", async () => {
  const chunks: string[] = []
  const stdout = Object.assign(new Writable({ write(chunk, _encoding, done) { chunks.push(chunk.toString()); done() } }), { isTTY: true, rows: 12, columns: 80 }) as unknown as NodeJS.WriteStream
  const stdin = Object.assign(new PassThrough(), { isTTY: false }) as unknown as NodeJS.ReadStream
  const root = await createRoot({ stdout, stdin, stderr: stdout, patchConsole: false, exitOnCtrlC: false })
  const gen1 = "11111111-1111-1111-1111-111111111111"
  const gen2 = "22222222-2222-2222-2222-222222222222"

  try {
    root.render(React.createElement(Text, null, "REDRAW-FRAME-CONTENT"))
    await new Promise(resolve => setTimeout(resolve, 50))
    chunks.length = 0

    requestRedraw(gen1, stdout)
    await new Promise(resolve => setTimeout(resolve, 50))

    const out1 = chunks.join("")
    expect(out1).toContain("\x1b]777;hermes-replay;begin;" + gen1 + "\x07")
    expect(out1).toContain("\x1b]777;hermes-replay;end;" + gen1 + "\x07")
    expect(out1.indexOf("\x1b]777;hermes-replay;end;" + gen1 + "\x07")).toBeGreaterThan(out1.indexOf("\x1b]777;hermes-replay;begin;" + gen1 + "\x07"))

    chunks.length = 0
    requestRedraw(gen2, stdout)
    await new Promise(resolve => setTimeout(resolve, 50))

    const out2 = chunks.join("")
    expect(out2).toContain("\x1b]777;hermes-replay;begin;" + gen2 + "\x07")
    expect(out2).toContain("\x1b]777;hermes-replay;end;" + gen2 + "\x07")
    expect(out2.indexOf("\x1b]777;hermes-replay;end;" + gen2 + "\x07")).toBeGreaterThan(out2.indexOf("\x1b]777;hermes-replay;begin;" + gen2 + "\x07"))
  } finally {
    root.unmount()
  }
})
