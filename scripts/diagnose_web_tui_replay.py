#!/usr/bin/env python3
"""Evidence-only CDP replay diagnostic. No shared cookie deletion or production edits.

Creates/cleans only its own tabs. Snapshots store hashes, never transcript text.
Exit 0: capture completed (NOT proof of correct rendering); 2: inconclusive/error.
"""
import argparse
import asyncio
import base64
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request
from urllib.parse import urlsplit, parse_qs
import secrets

# Override only PTY identity in this test document, never shared localStorage.
ISOLATE_PTY = r"""(() => {
 const key='hermes.pty.token.chat', get=Storage.prototype.getItem, set=Storage.prototype.setItem;
 const before=get.call(localStorage,key);let token=TOKEN;
 Storage.prototype.getItem=function(k){return this===localStorage&&k===key?token:get.call(this,k)};
 Storage.prototype.setItem=function(k,v){if(this===localStorage&&k===key){token=String(v);return}return set.call(this,k,v)};
 window.__auditSharedStorageUnchanged=()=>get.call(localStorage,key)===before;
})()"""

import websockets


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def safe_url(url):
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def redact(text):
    text = re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", lambda m: safe_url(m[0]), str(text))
    text = re.sub(r"(?i)(token|password|secret|authorization|cookie)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", text)
    return text[:1500]


def frame_bytes(frame):
    payload = frame.get('payloadData', '')
    return base64.b64decode(payload, validate=True) if frame.get('opcode') == 2 else payload.encode()


def logical_lines(rows):
    result = []
    for text, wrapped in rows:
        if wrapped and result:
            result[-1] += text
        else:
            result.append(text)
    return [s.rstrip() for s in result]


def block_counts(lines, width=12):
    # Ignore blank lines; this is a candidate detector, not an expected-transcript oracle.
    lines = [s for s in lines if s.strip()]
    return Counter(digest('\n'.join(lines[i:i + width])) for i in range(len(lines) - width + 1))


def compare_snapshots(before, after):
    a, b = before['logicalHashes'], after['logicalHashes']
    prefix = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    matches = SequenceMatcher(None, a, b[prefix:], autojunk=False).get_matching_blocks()
    blocks = [m for m in matches if m.size >= 12]
    longest = max(blocks, key=lambda m: m.size, default=None)
    return {'unchangedLogicalPrefix': prefix, 'newSuffixLines': len(b)-prefix,
            'longestRepeatedRun': longest.size if longest else 0,
            'matchedLinesInBlocks12Plus': sum(m.size for m in blocks),
            'viewportUnchanged': before['viewportY'] == after['viewportY'],
            'visibleAnchorUnchanged': before['visibleHashes'] == after['visibleHashes'],
            'interpretation': 'matching content is evidence of repetition, not proof of unintended replay'}


PROBE = r"""(() => {
 const node=document.querySelector('.xterm');
 if (!node) return {available:false,login:!!document.querySelector('input[type=password]'),path:location.pathname};
 let term=null;
 for(let el=node;el&&!term;el=el.parentElement){
  const key=Object.keys(el).find(k=>k.startsWith('__reactFiber'));
  let f=key?el[key]:null;
  for(let depth=0;f&&!term&&depth<100;depth++,f=f.return){
   let s=f.memoizedState;
   for(let n=0;s&&n<200;n++,s=s.next){
    const v=s.memoizedState;
    for(const t of [v?.current,v]) if(t?.buffer?.active&&typeof t.scrollLines==='function')term=t;
   }
   if(f.ref?.current?.buffer?.active)term=f.ref.current;
  }
 }
 if(!term)return {available:false,path:location.pathname,fiberMissing:true};
 let audit=window.__replayAudit;
 if(!audit||audit.term!==term){
  if(audit)for(const d of audit.disposables)d.dispose();
  audit={term,parses:0,renders:0,scrolls:0,lastParse:null,lastRender:null,lastScroll:null,boundaries:[],disposables:[]};
  window.__replayAudit=audit;
  audit.disposables.push(term.parser.registerOscHandler(777,data=>{if(/^hermes-replay;(begin|end|abort);/.test(data))audit.boundaries.push({phase:data.split(';')[1],browserMs:performance.now()});return false}));
  if(term.onWriteParsed)audit.disposables.push(term.onWriteParsed(()=>{audit.parses++;audit.lastParse=performance.now()}));
  if(term.onRender)audit.disposables.push(term.onRender(()=>{audit.renders++;audit.lastRender=performance.now()}));
  audit.disposables.push(term.onScroll(()=>{audit.scrolls++;audit.lastScroll=performance.now()}));
 }
 const b=term.buffer.active,r=node.getBoundingClientRect();
 const slider=node.querySelector('.scrollbar.vertical .slider'),track=node.querySelector('.scrollbar.vertical');
 const rect=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,top:r.top}};
 const rows=[];
 for(let i=0;i<b.length;i++){const l=b.getLine(i);rows.push([l?.translateToString(false)||'',!!l?.isWrapped])}
 return {available:true,browserMs:performance.now(),rows,cols:term.cols,height:term.rows,length:b.length,
  baseY:b.baseY,viewportY:b.viewportY,cursorY:b.cursorY,bufferType:b.type,
  slider:rect(slider),track:rect(track),terminal:rect(node),
  visible:rows.slice(b.viewportY,b.viewportY+term.rows).map(x=>x[0]),
  domRows:Array.from(node.querySelectorAll('.xterm-rows>div')).map(e=>e.textContent),
  loading:Array.from(document.querySelectorAll('[role=status]')).some(e=>/conversation loads/i.test(e.textContent||'')),
  replayBoundaries:audit.boundaries,parses:audit.parses,renders:audit.renders,scrolls:audit.scrolls,lastParse:audit.lastParse,lastRender:audit.lastRender,lastScroll:audit.lastScroll};
})()"""


class CDP:
    def __init__(self, ws, start):
        self.ws, self.start = ws, start
        self.serial, self.pending = 0, {}
        self.events, self.responses = [], []
        self.sockets = {}

    async def receive(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if 'id' in msg:
                    future = self.pending.pop(msg['id'], None)
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(str(msg['error'])))
                        else:
                            future.set_result(msg.get('result', {}))
                    continue
                method, p = msg.get('method'), msg.get('params', {})
                entry = {'t': time.monotonic()-self.start, 'event': method, 'browserTimestamp': p.get('timestamp')}
                if method == 'Network.webSocketCreated':
                    self.sockets[p['requestId']] = safe_url(p['url'])
                    entry['url'] = self.sockets[p['requestId']]
                    attach = parse_qs(urlsplit(p['url']).query).get('attach', [])
                    if attach: entry['attachHash'] = digest(attach[0])
                elif method in ('Network.webSocketFrameReceived', 'Network.webSocketFrameSent'):
                    data = frame_bytes(p['response'])
                    entry.update(socket=p['requestId'], url=self.sockets.get(p['requestId']), bytes=len(data), sha256=digest(data), opcode=p['response'].get('opcode'), clearScrollback=data.count(b'\x1b[3J'), clearScreen=data.count(b'\x1b[2J'), cursorHome=data.count(b'\x1b[H'), resizeCommand=data.startswith(b'\x1b[RESIZE:'), cursorReport=bool(re.fullmatch(rb'\x1b\[\d+;\d+R',data)))
                elif method in ('Network.webSocketClosed', 'Network.webSocketFrameError', 'Network.loadingFailed'):
                    entry.update(requestId=p.get('requestId'), error=redact(p.get('errorMessage', p.get('errorText', ''))))
                elif method == 'Runtime.consoleAPICalled':
                    entry.update(level=p.get('type'), text=redact(' '.join(str(a.get('value', a.get('description', ''))) for a in p.get('args', []))))
                elif method == 'Runtime.exceptionThrown':
                    entry['error'] = redact(p['exceptionDetails'].get('exception', {}).get('description', p['exceptionDetails'].get('text', '')))
                elif method == 'Log.entryAdded':
                    entry.update(level=p['entry'].get('level'), text=redact(p['entry'].get('text', '')))
                elif method == 'Network.responseReceived':
                    r = p['response']; url = safe_url(r['url'])
                    if r.get('mimeType', '').find('javascript') >= 0:
                        self.responses.append({'requestId': p['requestId'], 'url': url, 'status': r['status']})
                    continue
                else:
                    continue
                self.events.append(entry)
        except Exception as exc:
            for future in self.pending.values():
                if not future.done(): future.set_exception(exc)
            raise

    async def call(self, method, params=None):
        self.serial += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.serial] = future
        await self.ws.send(json.dumps({'id':self.serial,'method':method,'params':params or {}}))
        return await asyncio.wait_for(future, 30)

    async def evaluate(self, expression):
        r = await self.call('Runtime.evaluate', {'expression':expression,'returnByValue':True})
        if r.get('exceptionDetails'):
            raise RuntimeError(redact(r['exceptionDetails'].get('text', 'JavaScript exception')))
        if 'value' not in r.get('result', {}):
            raise RuntimeError('Evaluation returned no serializable value')
        return r['result']['value']


async def capture(args, scenario):
    base = args.cdp.rstrip('/')
    req = urllib.request.Request(base+'/json/new?about:blank', method='PUT')
    tab = json.load(urllib.request.urlopen(req, timeout=5))
    start = time.monotonic()
    report = {'scenario':scenario,'durationRequested':args.duration,'coldStart':'unknown; fresh tab does not prove fresh PTY','auth':'existing context; no cookies changed','samples':[],'events':[],'assets':[],'status':'inconclusive'}
    previous_blocks = None
    scroll_before = None
    observed_scroll = False
    next_wheel = args.scroll_after
    try:
        async with websockets.connect(tab['webSocketDebuggerUrl'], max_size=128*1024*1024) as ws:
            cdp = CDP(ws,start); receiver=asyncio.create_task(cdp.receive())
            try:
                for domain in ('Runtime','Log','Network','Page'):
                    await cdp.call(domain+'.enable')
                token=secrets.token_hex(16)
                await cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source':ISOLATE_PTY.replace('TOKEN',json.dumps(token))})
                report['expectedAttachHash']=digest(token)
                report['coldStart']='unique tab-local PTY identity requested; server creation not independently instrumented'
                await cdp.call('Page.navigate',{'url':args.url})
                while time.monotonic()-start < args.duration:
                    request_t=time.monotonic()-start
                    s=await cdp.evaluate(PROBE)
                    s.update(requestT=request_t,t=time.monotonic()-start,settledAssertionsEligible=time.monotonic()-start>=15)
                    if s.get('login'):
                        report['blocker']='Login required; shared cookies untouched';break
                    if s.get('available'):
                        rows=s.pop('rows'); lines=logical_lines(rows); blocks=block_counts(lines)
                        s['logicalCount']=len(lines)
                        s['logicalHashes']=[digest(x) for x in lines]
                        s['contentHash']=digest('\n'.join(lines))
                        s['visibleHashes']=[digest(x) for x in s.pop('visible')]
                        s['domHashes']=[digest(x) for x in s.pop('domRows')]
                        s['repeatedBlockCount']=sum(n>1 for n in blocks.values())
                        s['newRepeatedBlocks']=sum(n>1 and n>previous_blocks.get(h,0) for h,n in blocks.items()) if previous_blocks is not None else None
                        previous_blocks=blocks
                        if scroll_before is not None and s['viewportY']<scroll_before and s['baseY']>s['viewportY']:
                            observed_scroll=True
                        if scenario=='early-scroll' and not observed_scroll and s['t']>=next_wheel:
                            r=s['terminal'];scroll_before=s['viewportY']
                            action={'event':'wheel','t':time.monotonic()-start,'beforeViewportY':scroll_before,'beforeBaseY':s['baseY']}
                            await cdp.call('Input.dispatchMouseEvent',{'type':'mouseWheel','x':r['x']+r['width']/2,'y':r['y']+r['height']/2,'deltaX':0,'deltaY':-1200})
                            action['ackT']=time.monotonic()-start; cdp.events.append(action); next_wheel=action['ackT']+1
                    report['samples'].append(s)
                    await asyncio.sleep(args.interval)
                # Identity of actually loaded assets, not guesses from source variable names.
                for asset in cdp.responses:
                    if any(k in asset['url'] for k in ('ChatPage','xterm','index-')):
                        try:
                            body=await cdp.call('Network.getResponseBody',{'requestId':asset['requestId']})
                            data=base64.b64decode(body['body']) if body.get('base64Encoded') else body['body'].encode()
                            report['assets'].append({'url':asset['url'],'sha256':digest(data),'bytes':len(data)})
                        except Exception as exc: report['assets'].append({'url':asset['url'],'error':redact(exc)})
                report['events']=cdp.events
                report['sharedStorageUnchanged']=await cdp.evaluate('window.__auditSharedStorageUnchanged()')
                report['attachIdentityVerified']=any(e.get('attachHash')==report['expectedAttachHash'] for e in cdp.events)
                if not report['sharedStorageUnchanged'] or not report['attachIdentityVerified']:
                    report['blocker']='PTY identity isolation verification failed'
                report['scrollObserved']=observed_scroll
                report['observedSeconds']=time.monotonic()-start
                good=[s for s in report['samples'] if s.get('available')]
                report['status']='capture-complete; reproduction unproven' if good and not report.get('blocker') and (scenario=='control' or observed_scroll) else 'inconclusive'
                report['bufferChanges']=[{'t':b['t'],'from':a['length'],'to':b['length'],'viewportBefore':a['viewportY'],'viewportAfter':b['viewportY'],'newRepeatedBlocks':b['newRepeatedBlocks']} for a,b in zip(good,good[1:]) if a['length']!=b['length']]
                report['lateGrowthComparisons']=[dict(t=b['t'], **compare_snapshots(a,b)) for a,b in zip(good,good[1:]) if b['t']>=15 and b['length']-a['length']>1000]
            finally:
                report['events']=cdp.events
                receiver.cancel()
                await asyncio.gather(receiver,return_exceptions=True)
    except Exception as exc:
        report['error']=redact(exc)
    finally:
        try: urllib.request.urlopen(base+'/json/close/'+tab['id'],timeout=5).close()
        except Exception as exc: report['cleanupError']=redact(exc)
        args.output.mkdir(parents=True,exist_ok=True)
        args.output.chmod(0o700)
        target=args.output/(scenario+'.json');target.write_text(json.dumps(report,indent=2));target.chmod(0o600)
    print(json.dumps({'scenario':scenario,'status':report['status'],'artifact':str(target.resolve()),'samples':len(report['samples']),'scrollObserved':report.get('scrollObserved'),'bufferChanges':report.get('bufferChanges'),'error':report.get('error'),'blocker':report.get('blocker')}))
    return report


async def main(args):
    reports=[]
    for scenario in ('control','early-scroll'):
        reports.append(await capture(args,scenario))
    return 2 if any(r['status']=='inconclusive' for r in reports) else 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cdp',required=True)
    parser.add_argument('--url',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration',type=float,default=45)
    parser.add_argument('--interval',type=float,default=0.5)
    parser.add_argument('--scroll-after',type=float,default=2)
    args=parser.parse_args()
    if args.duration<30 or args.interval<=0:parser.error('duration >=30 and interval >0 required')
    raise SystemExit(asyncio.run(main(args)))
