import { appendFileSync } from 'node:fs'
import { EventEmitter } from 'node:events'
import { createRequire } from 'node:module'
import { Writable } from 'node:stream'
import React, { useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', () => import('../../packages/hermes-ink/src/entry-exports.js'))
import { Box, Text, createRoot } from '@hermes/ink'
import instances from '../../packages/hermes-ink/src/ink/instances.js'
import { ModelPicker } from '../components/modelPicker.js'
import { MessageLine } from '../components/messageLine.js'
import { TextInput } from '../components/textInput.js'
import { DARK_THEME } from '../theme.js'
import type { GatewayClient } from '../gatewayClient.js'

const { Terminal } = createRequire(import.meta.url)('@xterm/xterm')
const settle = (ms = 40) => new Promise(resolve => setTimeout(resolve, ms))
class Input extends EventEmitter {
  chunks: string[] = []
  isTTY = true
  isRaw = false
  readableLength = 0
  read() { const value=this.chunks.shift() ?? null; this.readableLength=this.chunks.length; return value }
  ref() {}
  unref() {}
  setEncoding() {}
  setRawMode(value: boolean) { this.isRaw=value }
  send(text: string) { this.chunks.push(text);this.readableLength=this.chunks.length;this.emit('readable') }
}
async function fixture() {
  const term = new Terminal({ cols: 100, rows: 30, scrollback: 20000 })
  const stdin=new Input()
  const stdout=Object.assign(new Writable({write(chunk,_encoding,done){term.write(chunk.toString(),done)}}),{isTTY:true,columns:100,rows:30}) as unknown as NodeJS.WriteStream
  const root=await createRoot({stdout,stdin:stdin as unknown as NodeJS.ReadStream,stderr:stdout,patchConsole:false,exitOnCtrlC:false})
  instances.get(stdout)!.setInlineMouseTracking('buttons')
  const visible=()=>Array.from({length:term.rows},(_,i)=>term.buffer.active.getLine(term.buffer.active.baseY+i)?.translateToString(true)||'')
  const click=async (label: string)=>{
    const lines=visible(), row=lines.findIndex(s=>s.includes(label))
    expect(row,`visible target ${label}`).toBeGreaterThanOrEqual(0)
    const col=lines[row].indexOf(label)+1
    stdin.send(`\x1b[<0;${col+1};${row+1}M\x1b[<0;${col+1};${row+1}m`)
    await settle(80)
  }
  return {term,stdin,stdout,root,visible,click,close(){root.unmount();term.dispose();instances.delete(stdout)}}
}

describe('active UI interaction through Ink and xterm',()=>{
  it('does not roll back newer typing when an older own echo commits',async()=>{
    const f=await fixture();const changes:string[]=[];let submitted='';let deliver:(value:string)=>void=()=>{}
    function Harness(){const [value,setValue]=useState('');deliver=setValue;return <TextInput busy columns={90} value={value} onChange={v=>changes.push(v)} onSubmit={v=>{submitted=v}}/>}
    try {
      f.root.render(<Harness/>);await settle(80)
      f.stdin.send('a');await settle(40)
      expect(changes.at(-1)).toBe('a')
      f.stdin.send('b');deliver('a');await settle(40)
      f.stdin.send('c');await settle(40)
      f.stdin.send('\x1b[13u');await settle(40)
      expect(submitted).toBe('abc')
      deliver('external');await settle(40)
      f.stdin.send('z\x1b[13u');await settle(40)
      expect(submitted).toBe('externalz')
    }finally{f.close()}
  })

  it('clicks provider, model and effort through the real SGR input path',async()=>{
    const f=await fixture();const selected:string[]=[]
    const gw={request:async()=>({providers:[{slug:'fixture',name:'Fixture Provider',authenticated:true,models:['fixture-model'],capabilities:{'fixture-model':{reasoning:true}}}]})} as unknown as GatewayClient
    try {
      f.root.render(<Box flexDirection="column"><Text>{Array.from({length:80},(_,i)=>`old ${i}`).join('\n')}</Text><ModelPicker gw={gw} onCancel={()=>{}} onSelect={s=>selected.push(s)} t={DARK_THEME}/></Box>)
      await settle(150)
      await f.click('Fixture Provider')
      expect(f.visible().join('\n')).toContain('Select model')
      await f.click('1. fixture-model')
      expect(f.visible().join('\n')).toContain('Reasoning effort')
      await f.click('1. ')
      expect(selected).toHaveLength(1)
      expect(selected[0]).toContain('fixture-model --provider fixture')
    } finally {f.close()}
  })

  it.each([100, 1000, 3800])('retains input during streaming with %i mounted history messages',async(count)=>{
    const f=await fixture();let latest='';let submitted=''
    const history=Array.from({length:count},(_,i)=>({role:'assistant' as const,text:`Completed ${i}: **formatted history** with a paragraph.\n\n- item alpha\n- item beta`}))
    const sent=new Map<number,number>();const latencies:number[]=[];const started=performance.now()
    function Harness(){
      const [value,setValue]=useState(''),[stream,setStream]=useState('')
      useEffect(()=>{const timer=setInterval(()=>setStream(s=>(s+'token ').slice(-200)),8);return()=>clearInterval(timer)},[])
      return <Box flexDirection="column">{history.map((msg,i)=><MessageLine key={i} msg={msg} prev={history[i-1]} cols={100} compact={false} t={DARK_THEME}/>)}<MessageLine msg={{role:'assistant',text:stream}} cols={100} compact={false} isStreaming t={DARK_THEME}/><TextInput busy columns={90} value={value} onChange={v=>{latest=v;setValue(v);const at=sent.get(v.length);if(at!==undefined)latencies.push(performance.now()-at)}} onSubmit={v=>{submitted=v}}/></Box>
    }
    try {
      f.root.render(<Harness/>);await settle(500)
      const initialMs=performance.now()-started
      const input='abcdefghijklmnopqrstuvwxyz0123456789'
      const painted=new Set<number>(),paintLatencies:number[]=[]
      f.term.onWriteParsed(()=>{const text=f.visible().join('\n');for(const [n,at] of sent){if(n>=6&&!painted.has(n)&&text.includes(input.slice(0,n))){painted.add(n);paintLatencies.push(performance.now()-at)}}})
      for(const [i,c] of [...input].entries()){sent.set(i+1,performance.now());f.stdin.send(c);await settle(12)}
      await settle(100)
      expect(latest).toBe(input)
      expect(f.visible().join('\n')).toContain(input)
      f.stdin.send('\r');await settle(50)
      expect(submitted).toBe(input)
      latencies.sort((a,b)=>a-b)
      paintLatencies.sort((a,b)=>a-b)
      expect(painted.has(input.length)).toBe(true)
      if (process.env.WEB_TUI_BENCH_REPORT) appendFileSync(process.env.WEB_TUI_BENCH_REPORT,JSON.stringify({count,readyCheckMs:initialMs,p95StateMs:latencies[Math.floor(latencies.length*.95)],p95ParsedDisplayMs:paintLatencies[Math.floor(paintLatencies.length*.95)],rssBytes:process.memoryUsage().rss,typed:input.length,received:latest.length})+'\n')
      f.stdin.send('\x15/model\x1b[13u');await settle(100)
      expect(submitted).toBe('/model')
    }finally{f.close()}
  },30000)
})
