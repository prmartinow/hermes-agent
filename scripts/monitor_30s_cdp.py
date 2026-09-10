#!/usr/bin/env python3
"""30-Second High-Resolution CDP Monitor for Web TUI History Injection.

Tracks xterm buffer line count, scrollback depth, banner count, viewport geometry,
and WebSocket frame traffic every 2 seconds for 30 seconds following a clean
cookie purge and navigation.
"""

import urllib.request
import json
import asyncio
import websockets
import time

async def run_30s_monitor(port=9250, session_id="20260907_060313_3673ae"):
    base_cdp = f"http://127.0.0.1:{port}"
    print(f"[MONITOR] Connecting to Chrome on port {port}...")
    
    req = urllib.request.Request(f"{base_cdp}/json/new?about:blank", method="PUT")
    tab = json.loads(urllib.request.urlopen(req).read().decode())
    tab_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    print(f"[MONITOR] Created isolated test tab: {tab_id}")
    
    ws_frames = []
    
    try:
        async with websockets.connect(ws_url, max_size=128*1024*1024) as ws:
            msg_id = 0
            pending = {}
            
            async def cmd(method, params=None):
                nonlocal msg_id
                msg_id += 1
                mid = msg_id
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while mid not in pending:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if "id" in data:
                        pending[data["id"]] = data
                    else:
                        m = data.get("method", "")
                        if "webSocketFrame" in m.lower():
                            payload = data.get("params", {}).get("response", {}).get("payloadData", "")
                            ws_frames.append({
                                "time": time.time(),
                                "len": len(payload),
                                "preview": payload[:60] if isinstance(payload, str) else ""
                            })
                return pending.pop(mid).get("result", {})

            await cmd("Log.enable")
            await cmd("Runtime.enable")
            await cmd("Network.enable")
            
            # Delete cookies ONLY for http://127.0.0.1:9119
            await cmd("Network.deleteCookies", {"url": "http://127.0.0.1:9119"})
            print("[MONITOR] Cleaned cookies strictly for http://127.0.0.1:9119")
            
            target_url = f"http://127.0.0.1:9119/chat?resume={session_id}"
            print(f"[MONITOR] Navigating to {target_url}...")
            start_time = time.time()
            await cmd("Page.navigate", {"url": target_url})
            
            async def eval_js(expr):
                res = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return res.get("result", {}).get("value")

            timeline = []
            prev_lines = None
            
            # Monitor every 2 seconds for 30 seconds
            for step in range(1, 16):
                await asyncio.sleep(2)
                elapsed = time.time() - start_time
                
                # Sample state
                sample_js = '''(() => {
                    const xterm = document.querySelector('.xterm');
                    const parent = xterm ? xterm.parentElement : null;
                    const viewport = document.querySelector('.xterm-viewport');
                    
                    let fiberKey = Object.keys(parent || {}).find(k => k.startsWith('__reactFiber'));
                    let curr = parent ? parent[fiberKey] : null;
                    let term = window.__hermes_term || null;
                    
                    while (curr && !term) {
                        let s = curr.memoizedState;
                        while (s) {
                            if (s.memoizedState && s.memoizedState.current && s.memoizedState.current.buffer) {
                                term = s.memoizedState.current;
                                break;
                            }
                            if (s.memoizedState && s.memoizedState.buffer) {
                                term = s.memoizedState;
                                break;
                            }
                            s = s.next;
                        }
                        if (curr.ref?.current?.buffer) {
                            term = curr.ref.current;
                            break;
                        }
                        curr = curr.return;
                    }
                    
                    if (!term || !term.buffer || !term.buffer.active) {
                        return { hasTerm: false, elapsed: Math.round(performance.now()) };
                    }
                    
                    const buf = term.buffer.active;
                    const totalLines = buf.length;
                    const baseY = buf.baseY;
                    const cursorY = buf.cursorY;
                    
                    // Count banner occurrences
                    let bannerCount = 0;
                    let bannerLines = [];
                    for (let i = 0; i < totalLines; i++) {
                        const l = buf.getLine(i)?.translateToString(true) || '';
                        if (l.includes('Messenger of the Digital Gods') || l.includes('Nous Research ·')) {
                            bannerCount++;
                            bannerLines.push(i);
                        }
                    }
                    
                    // Sample bottom 3 non-empty lines
                    let bottomSample = [];
                    for (let i = Math.max(0, totalLines - 10); i < totalLines; i++) {
                        const l = buf.getLine(i)?.translateToString(true) || '';
                        if (l.trim()) bottomSample.push(l.slice(0, 60));
                    }
                    
                    // Sample line 0-2
                    let topSample = [];
                    for (let i = 0; i < Math.min(5, totalLines); i++) {
                        const l = buf.getLine(i)?.translateToString(true) || '';
                        if (l.trim()) topSample.push(l.slice(0, 60));
                    }
                    
                    return {
                        hasTerm: true,
                        totalLines,
                        baseY,
                        cursorY,
                        bannerCount,
                        bannerLines,
                        scrollHeight: viewport?.scrollHeight,
                        clientHeight: viewport?.clientHeight,
                        scrollTop: viewport?.scrollTop,
                        topSample,
                        bottomSample: bottomSample.slice(-3)
                    };
                })()'''
                
                data = await eval_js(sample_js)
                recent_frames = [f for f in ws_frames if f["time"] >= time.time() - 2.1]
                recent_bytes = sum(f["len"] for f in recent_frames)
                
                entry = {
                    "sec": f"T+{elapsed:.1f}s",
                    "hasTerm": data.get("hasTerm", False),
                    "totalLines": data.get("totalLines"),
                    "baseY": data.get("baseY"),
                    "bannerCount": data.get("bannerCount"),
                    "bannerLines": data.get("bannerLines"),
                    "recentBytes": recent_bytes,
                    "deltaLines": (data.get("totalLines", 0) - prev_lines) if (prev_lines is not None and data.get("totalLines")) else 0,
                    "bottomSample": data.get("bottomSample", [])
                }
                
                if data.get("totalLines") is not None:
                    prev_lines = data.get("totalLines")
                    
                timeline.append(entry)
                
                delta_str = f" (+{entry['deltaLines']} lines)" if entry['deltaLines'] > 0 else ""
                print(f"[{entry['sec']}] Buffer: {entry['totalLines']} lines{delta_str}, BaseY: {entry['baseY']}, Banners: {entry['bannerCount']}, Recent WS Bytes: {entry['recentBytes']:,} B")
                if entry['deltaLines'] > 1000:
                    print(f"  >>> MASSIVE BUFFER EXPANSION DETECTED at {entry['sec']}: +{entry['deltaLines']} lines!")
                    print(f"  >>> Bottom lines now: {entry['bottomSample']}")
            
            print("\n" + "="*65)
            print("                 30-SECOND TIMELINE SUMMARY")
            print("="*65)
            for t in timeline:
                print(f"  {t['sec']:<8} | Lines: {str(t['totalLines']):<6} | BaseY: {str(t['baseY']):<6} | Banners: {str(t['bannerCount']):<3} | Inbound: {t['recentBytes']:>9,} B | Delta: +{t['deltaLines']}")
            print("="*65)
            
    finally:
        req_close = urllib.request.Request(f"{base_cdp}/json/close/{tab_id}", method="PUT")
        urllib.request.urlopen(req_close)
        print(f"[MONITOR] Closed isolated tab {tab_id}")

if __name__ == "__main__":
    asyncio.run(run_30s_monitor())
