#!/usr/bin/env python3
"""Browser -> real ChatPage -> WebSocket -> PTY -> Ink active-fixture test.

Refuses to type unless ACTIVE-FIXTURE is visible. Never run against a real turn.
Pass the URL of a temporary dashboard whose PTY entrypoint is active-ui-fixture.
"""
import argparse
import base64
import asyncio
import json
from pathlib import Path
import secrets
import time
import urllib.request
from urllib.parse import urlsplit

import websockets
from diagnose_web_tui_replay import CDP, PROBE, ISOLATE_PTY, digest, redact

VISIBLE = """(() => {const t=window.__replayAudit?.term;if(!t)return null;const b=t.buffer.active;const r=t.element.querySelector('.xterm-screen').getBoundingClientRect();return {domLines:Array.from(t.element.querySelectorAll('.xterm-rows>div')).map(e=>e.textContent||''),lines:Array.from({length:t.rows},(_,i)=>b.getLine(b.viewportY+i)?.translateToString(true)||''),cols:t.cols,rows:t.rows,baseY:b.baseY,viewportY:b.viewportY,rect:{x:r.x,y:r.y,width:r.width,height:r.height}}})()"""

async def run(args):
    target=urlsplit(args.url)
    if not args.allow_fixture_input or target.hostname not in ('127.0.0.1','localhost','::1') or target.port == 9119:
        raise ValueError('Fixture input requires explicit opt-in on a temporary loopback server, never serving port 9119')
    tab=json.load(urllib.request.urlopen(urllib.request.Request(args.cdp+'/json/new?about:blank',method='PUT')))
    result={'status':'failed','scope':'deterministic active-stream fixture; no real model or gateway recovery test'}
    start=time.monotonic()
    try:
        async with websockets.connect(tab['webSocketDebuggerUrl'],max_size=128*1024*1024) as ws:
            c=CDP(ws,start);reader=asyncio.create_task(c.receive())
            try:
                for domain in ['Runtime','Log','Network','Page']:await c.call(domain+'.enable')
                await c.call('Page.addScriptToEvaluateOnNewDocument',{'source':ISOLATE_PTY.replace('TOKEN',json.dumps(secrets.token_hex(16)))})
                await c.call('Page.navigate',{'url':args.url})
                await asyncio.sleep(15)
                probe=await c.evaluate(PROBE)
                assert probe.get('available'), 'No terminal after minimum settle window'
                async def visible():return await c.evaluate(VISIBLE)
                state=await visible()
                assert any(l.strip().startswith('ACTIVE-FIXTURE tick ') and 'deterministic simulated output' in l for l in state['lines']), 'Safety stop: not the deterministic fixture'
                await c.evaluate('window.__replayAudit.term.focus(); true')
                async def type_text(text, delay=0.02):
                    for ch in text:
                        await c.call('Input.dispatchKeyEvent',{'type':'keyDown','key':ch,'text':ch,'unmodifiedText':ch})
                        await c.call('Input.dispatchKeyEvent',{'type':'keyUp','key':ch})
                        if delay: await asyncio.sleep(delay)
                async def enter():
                    await c.call('Input.dispatchKeyEvent',{'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'text':'\r'})
                    await c.call('Input.dispatchKeyEvent',{'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
                async def wait_text(text):
                    deadline=time.monotonic()+8
                    while time.monotonic()<deadline:
                        s=await visible()
                        if any(text in l for l in s['lines']) and (not s['domLines'] or any(text in l for l in s['domLines'])):return s
                        await asyncio.sleep(.1)
                    raise AssertionError('Missing visible fixture text: '+text+'; fixture tail='+json.dumps(s['lines'][-8:]))
                async def click(text, hold=0):
                    s=await wait_text(text);row=next(i for i,l in enumerate(s['lines']) if text in l);col=s['lines'][row].index(text)+1;r=s['rect']
                    point={'x':r['x']+(col+.5)*r['width']/s['cols'],'y':r['y']+(row+.5)*r['height']/s['rows'],'button':'left','clickCount':1}
                    await c.call('Input.dispatchMouseEvent',{'type':'mousePressed',**point})
                    if hold: await asyncio.sleep(hold)
                    await c.call('Input.dispatchMouseEvent',{'type':'mouseReleased',**point})
                text='QZfixture_abcdefghijklmnopqrstuvwxyz0123456789'
                t=time.monotonic();await type_text(text);await wait_text(text)
                result['typingVisibleMs']=(time.monotonic()-t)*1000
                await enter();await wait_text('SUBMITTED: '+text)
                result['exactInputSubmitted']=True
                result['domTextChecksAvailable']=bool((await visible())['domLines'])
                await type_text('/model', delay=0);await enter();await wait_text('Select provider')
                await click('Fixture Provider', hold=.8);await wait_text('Select model')
                await click('1. fixture-model');await wait_text('Reasoning effort')
                await click('1. ');await wait_text('SELECTED: fixture-model --provider fixture')
                result['providerModelEffortClicks']=True
                await wait_text('Thinking')
                await click('Thinking')
                await asyncio.sleep(.4)
                collapsed=await visible()
                assert not any('ACTIVE-DETAIL-' in line for line in collapsed['lines']), 'Live reasoning did not collapse'
                await click('Thinking')
                await wait_text('ACTIVE-DETAIL-')
                result['liveReasoningCollapseExpand']=True
                s=await visible()
                row=next(i for i,line in enumerate(s['lines']) if 'Fixture history' in line)
                r=s['rect'];x=r['x']+2*r['width']/s['cols'];y=r['y']+(row+.5)*r['height']/s['rows']
                await c.call('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','buttons':1,'clickCount':1,'x':x,'y':y})
                await c.call('Input.dispatchMouseEvent',{'type':'mouseMoved','button':'left','buttons':1,'x':x+18*r['width']/s['cols'],'y':y})
                await c.call('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','buttons':0,'clickCount':1,'x':x+18*r['width']/s['cols'],'y':y})
                selected=await c.evaluate('window.__replayAudit.term.getSelection()')
                assert selected.strip(), 'Native drag selection was empty'
                await asyncio.sleep(.6)
                assert await c.evaluate('window.__replayAudit.term.getSelection()')==selected, 'Selection changed during streaming'
                result['dragSelectionSurvivesStreaming']=True
                await c.evaluate('window.__replayAudit.term.clearSelection();window.__replayAudit.term.scrollLines(-100);true')
                await asyncio.sleep(.1)
                s=await visible();assert s['baseY']-s['viewportY']>s['rows'], 'No immutable scrollback available'
                before=len(c.events);r=s['rect'];col=2;row=2
                point={'x':r['x']+(col+.5)*r['width']/s['cols'],'y':r['y']+(row+.5)*r['height']/s['rows'],'button':'left','clickCount':1}
                await c.call('Input.dispatchMouseEvent',{'type':'mousePressed',**point});await c.call('Input.dispatchMouseEvent',{'type':'mouseReleased',**point})
                await asyncio.sleep(.1)
                forbidden=digest(f'\x1b[<0;{col+1};{row+1}M\x1b[<0;{col+1};{row+1}m')
                assert not any(e.get('sha256')==forbidden for e in c.events[before:]), 'History click reached live controls'
                result['historyClickNotForwarded']=True


                result['sharedStorageUnchanged']=await c.evaluate('window.__auditSharedStorageUnchanged()')
                result['exceptions']=[e for e in c.events if e['event']=='Runtime.exceptionThrown']
                assert not result['exceptions'], 'Uncaught JavaScript exception'
                assert result['sharedStorageUnchanged'], 'Shared token changed'
                result['assets']=[]
                for asset in c.responses:
                    if '/ChatPage-' in asset['url']:
                        body=await c.call('Network.getResponseBody',{'requestId':asset['requestId']})
                        data=base64.b64decode(body['body']) if body.get('base64Encoded') else body['body'].encode()
                        result['assets'].append({'url':asset['url'],'sha256':digest(data)})
                assert result['assets'], 'No loaded ChatPage asset identified'
                result['status']='passed'
            finally:
                result['events']=c.events
                reader.cancel();await asyncio.gather(reader,return_exceptions=True)
    except Exception as exc:result['error']=redact(exc)
    finally:
        urllib.request.urlopen(args.cdp+'/json/close/'+tab['id']).close()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2));args.output.chmod(0o600)
    print(json.dumps({k:v for k,v in result.items() if k!='events'}))
    return 0 if result['status']=='passed' else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cdp',required=True);p.add_argument('--url',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--allow-fixture-input',action='store_true')
    raise SystemExit(asyncio.run(run(p.parse_args())))
