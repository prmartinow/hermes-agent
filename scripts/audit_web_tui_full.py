#!/usr/bin/env python3
"""Comprehensive Multi-Layer CDP Inspection & Testing Tool for Web TUI.

Implements all 4 inspection layers:
1. Isolated browser tab on CDP 9250.
2. Clean cookie purge strictly for http://127.0.0.1:9119.
3. Full 15-second settle window for complete hydration.
4. Triangulated ground truth:
   - xterm.js internal virtual buffer (term.buffer.active)
   - DOM elements & scroll geometry (.xterm-rows, viewport.scrollTop/scrollHeight)
   - User-selectable text projection (selectAll / getSelection)
   - Console logs & uncaught exceptions
   - Duplication analytics (banner count, capability matrix occurrences, line 0 check)
"""

import urllib.request
import json
import asyncio
import websockets
import time

async def run_full_audit(port=9250, session_id="20260907_060313_3673ae", wait_seconds=15):
    base_cdp = f"http://127.0.0.1:{port}"
    print(f"[CDP] Connecting to Chrome on port {port}...")
    
    # 1. Create fresh isolated tab
    req = urllib.request.Request(f"{base_cdp}/json/new?about:blank", method="PUT")
    tab = json.loads(urllib.request.urlopen(req).read().decode())
    tab_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    print(f"[CDP] Created isolated test tab: {tab_id}")
    
    console_logs = []
    ws_frames_count = 0
    ws_bytes_count = 0
    
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
                        if m == "Runtime.consoleAPICalled":
                            args = data.get("params", {}).get("args", [])
                            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
                            console_logs.append(f"[{data.get('params', {}).get('type')}] {text}")
                        elif m == "Log.entryAdded":
                            entry = data.get("params", {}).get("entry", {})
                            console_logs.append(f"[Log:{entry.get('level')}] {entry.get('text')}")
                return pending.pop(mid).get("result", {})

            # Enable CDP domains
            await cmd("Log.enable")
            await cmd("Runtime.enable")
            await cmd("Network.enable")
            
            # Delete cookies ONLY for http://127.0.0.1:9119
            await cmd("Network.deleteCookies", {"url": "http://127.0.0.1:9119"})
            print("[CDP] Cleaned cookies strictly for http://127.0.0.1:9119")
            
            target_url = f"http://127.0.0.1:9119/chat?resume={session_id}"
            print(f"[CDP] Navigating to {target_url}...")
            await cmd("Page.navigate", {"url": target_url})
            
            print(f"[CDP] Waiting exactly {wait_seconds} seconds for full session hydration & settle...")
            for s in range(1, wait_seconds + 1):
                await asyncio.sleep(1)
                if s % 5 == 0 or s == wait_seconds:
                    print(f"  ... waited {s}s / {wait_seconds}s")
                    
            async def eval_js(expr):
                res = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in res:
                    print("[JS EXCEPTION]:", res["exceptionDetails"])
                return res.get("result", {}).get("value")

            # Layer 1 & 2: Comprehensive in-page inspection script
            inspection_js = '''(() => {
                const xterm = document.querySelector('.xterm');
                const parent = xterm ? xterm.parentElement : null;
                const viewport = document.querySelector('.xterm-viewport');
                const scrollArea = document.querySelector('.xterm-scroll-area');
                
                // Locate xterm instance from React fiber
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
                
                if (!term) {
                    return { error: 'Terminal instance could not be located in React fiber' };
                }
                
                const buf = term.buffer.active;
                const totalLines = buf.length;
                const baseY = buf.baseY;
                const cursorY = buf.cursorY;
                
                // Scan all lines in memory
                let bannerLines = [];
                let capabilityLines = [];
                let userPromptOccurrences = [];
                let line0To20 = [];
                
                for (let i = 0; i < totalLines; i++) {
                    const l = buf.getLine(i)?.translateToString(true) || '';
                    if (l.includes('Messenger of the Digital Gods') || l.includes('Nous Research ·')) {
                        bannerLines.push({ line: i, text: l });
                    }
                    if (l.toLowerCase().includes('capability matrix') || l.toLowerCase().includes('governance')) {
                        capabilityLines.push({ line: i, text: l.slice(0, 90) });
                    }
                    if (l.includes('i need your help with investigating web tui bugs')) {
                        userPromptOccurrences.push({ line: i, text: l });
                    }
                    if (i < 20) {
                        line0To20.push({ line: i, text: l });
                    }
                }
                
                // DOM analysis
                const domRows = Array.from(document.querySelectorAll('.xterm-rows > div')).map(el => el.textContent);
                
                // Selection test
                let fullSelectionText = '';
                try {
                    term.selectAll();
                    fullSelectionText = term.getSelection() || '';
                    term.clearSelection();
                } catch {
                    /* ignore */
                }
                
                return {
                    totalLines,
                    baseY,
                    cursorY,
                    geometry: `${term.cols}x${term.rows}`,
                    domRowCount: domRows.length,
                    domFirstRow: domRows[0]?.slice(0, 80) || '',
                    domLastRow: domRows[domRows.length - 1]?.slice(0, 80) || '',
                    viewportScrollHeight: viewport ? viewport.scrollHeight : null,
                    viewportClientHeight: viewport ? viewport.clientHeight : null,
                    viewportScrollTop: viewport ? viewport.scrollTop : null,
                    bannerCount: bannerLines.length,
                    bannerLines,
                    userPromptCount: userPromptOccurrences.length,
                    userPromptLines: userPromptOccurrences,
                    capabilityCount: capabilityLines.length,
                    capabilityLines: capabilityLines.slice(0, 5),
                    line0To20,
                    selectionCharLength: fullSelectionText.length,
                    selectionLineCount: fullSelectionText ? fullSelectionText.split('\\n').length : 0
                };
            })()'''
            
            report = await eval_js(inspection_js)
            
            print("\n" + "="*60)
            print("         CDP 15-SECOND SETTLE AUDIT REPORT")
            print("="*60)
            if report.get("error"):
                print(f"FAILED: {report['error']}")
            else:
                print(f"• Total Virtual Buffer Lines : {report['totalLines']}")
                print(f"• Scrollback BaseY           : {report['baseY']}")
                print(f"• Cursor Coordinate          : line {report['cursorY']}")
                print(f"• Active Terminal Geometry   : {report['geometry']}")
                print(f"• DOM Viewport Geometry      : scrollHeight={report['viewportScrollHeight']}, clientHeight={report['viewportClientHeight']}, scrollTop={report['viewportScrollTop']}")
                print(f"• Full Selection Text Length : {report['selectionCharLength']:,} chars ({report['selectionLineCount']} lines)")
                print(f"• Hermes Banner Occurrences  : {report['bannerCount']}")
                for b in report.get("bannerLines", []):
                    print(f"    - Line {b['line']}: {repr(b['text'])}")
                print(f"• Original User Prompt Count : {report['userPromptCount']}")
                for u in report.get("userPromptLines", []):
                    print(f"    - Line {u['line']}: {repr(u['text'])}")
                print(f"• Capability Matrix Mentions : {report['capabilityCount']}")
                for c in report.get("capabilityLines", []):
                    print(f"    - Line {c['line']}: {repr(c['text'])}")
                print("\n• Line 0 to 15 in Buffer:")
                for l in report.get("line0To20", [])[:15]:
                    if l['text'].strip():
                        print(f"    Line {l['line']:>2}: {repr(l['text'][:90])}")

            print("\n• Console Logs & Warnings Captured (" + str(len(console_logs)) + "):")
            for cl in console_logs[:10]:
                print("   ", cl)
            print("="*60 + "\n")
            
    finally:
        req_close = urllib.request.Request(f"{base_cdp}/json/close/{tab_id}", method="PUT")
        urllib.request.urlopen(req_close)
        print(f"[CDP] Closed test tab {tab_id}")

if __name__ == "__main__":
    asyncio.run(run_full_audit())
