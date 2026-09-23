import { join } from 'node:path'
import { appendFileSync } from 'node:fs'
// Deterministic active-output fixture. No model calls or credentials.
// Bundled as a test PTY entrypoint, then served through the real dashboard.
import React, { useEffect, useLayoutEffect, useState } from 'react'
import { AlternateScreen, Box, Text, render, writeAfterRender } from '@hermes/ink'
import { MessageLine } from '../src/components/messageLine.js'
import { ModelPicker } from '../src/components/modelPicker.js'
import { TextInput } from '../src/components/textInput.js'
import { DARK_THEME } from '../src/theme.js'
import type { GatewayClient } from '../src/gatewayClient.js'

const history=Array.from({length:Number(process.env.WEB_TUI_FIXTURE_HISTORY || 1000)},(_,i)=>({role:'assistant' as const,text:`Fixture history ${i}: **completed content**.\n\n- alpha\n- beta`}))
const gw={request:async(method:string)=>{
  if(method!=='model.options')throw new Error(`Unexpected fixture RPC ${method}`)
  return {providers:[{slug:'fixture',name:'Fixture Provider',authenticated:true,models:['fixture-model'],capabilities:{'fixture-model':{reasoning:true}}}]}
}} as unknown as GatewayClient
const report=process.env.WEB_TUI_FIXTURE_REPORT || (process.env.HERMES_HOME ? join(process.env.HERMES_HOME,'active-ui-fixture.jsonl') : '')
const record=(kind:string,value:string)=>{if(report)appendFileSync(report,JSON.stringify({pid:process.pid,time:Date.now(),kind,value})+'\n')}
const generation='11111111-1111-1111-1111-111111111111'
const finiteTicks = Number(process.env.WEB_TUI_FIXTURE_MAX_TICKS || (process.env.WEB_TUI_FIXTURE_FINITE ? 1500 : 0))
const sentinel = process.env.WEB_TUI_FIXTURE_SENTINEL || 'HERMES_FIXTURE_FINAL_COMPLETION_SENTINEL_OK'

function Fixture(){
  const [input,setInput]=useState(''),[stream,setStream]=useState(0),[completed,setCompleted]=useState(false),[picker,setPicker]=useState(false),[selection,setSelection]=useState('none'),[submitted,setSubmitted]=useState('none')
  useEffect(()=>{
    const timer=setInterval(()=>{
      setStream(n=>{
        if(finiteTicks > 0 && n + 1 >= finiteTicks){
          clearInterval(timer)
          setCompleted(true)
          record('completion', sentinel)
          return finiteTicks
        }
        return n + 1
      })
    },40)
    return()=>clearInterval(timer)
  },[])
  useLayoutEffect(()=>{writeAfterRender(`\x1b]777;hermes-replay;end;${generation}\x07`,process.stdout,true)},[])
  return <AlternateScreen inline mouseTracking="buttons"><Box flexDirection="column">
    {history.map((msg,i)=><MessageLine key={i} msg={msg} prev={history[i-1]} cols={process.stdout.columns || 100} compact={false} t={DARK_THEME}/>)}
    <MessageLine msg={{role:'assistant',text:'',thinking:completed ? `COMPLETED-DETAIL-${stream}` : `ACTIVE-DETAIL-${stream}`}} cols={process.stdout.columns || 100} compact={false} liveDetails={!completed} reasoningActive={!completed} t={DARK_THEME}/>
    {completed ? <Text>{sentinel}</Text> : <Text>ACTIVE-FIXTURE tick {stream} — deterministic simulated output, not a model response</Text>}
    {picker ? <ModelPicker gw={gw} onCancel={()=>setPicker(false)} onSelect={s=>{setSelection(s);setPicker(false)}} t={DARK_THEME}/> : <>
      <Text>SELECTED: {selection}</Text><Text>SUBMITTED: {submitted}</Text>
      <TextInput busy={!completed} columns={(process.stdout.columns || 100)-2} value={input} onChange={v=>{record('change',v);setInput(v)}} onSubmit={v=>{record('submit',v);if(v==='/model')setPicker(true);else setSubmitted(v);setInput('')}}/>
    </>}
  </Box></AlternateScreen>
}
process.stdout.write(`\x1b]777;hermes-replay;begin;${generation}\x07`)
const instance=await render(<Fixture/>,{patchConsole:false,exitOnCtrlC:false})
process.stdin.on('end',()=>{instance.unmount();process.exit(0)})
