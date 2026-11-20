#!/usr/bin/env python3
"""Automated CDP audit tool for Web TUI rendering and history integrity.
Queries xterm.js internal buffer directly via React fiber on Chrome Display :96.
Clears cookies strictly for port 9119 only.
"""
import argparse
import urllib.request
from urllib.parse import quote
import json
import asyncio
import websockets
import sys

async def run_audit(port=9250, *, session_id):
    base_cdp = f"http://127.0.0.1:{port}"
    print(f"[CDP] Connecting to Chrome on port {port}...")

    # 1. Create fresh isolated tab
    req = urllib.request.Request(f"{base_cdp}/json/new?about:blank", method="PUT")
    tab = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(req, timeout=5).read().decode()))
    tab_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    print(f"[CDP] Created isolated test tab {tab_id}")

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
                return pending.pop(mid).get("result", {})

            # Enable network and delete cookies ONLY for 127.0.0.1:9119
            await cmd("Network.enable")
            await cmd("Network.deleteCookies", {"url": "http://127.0.0.1:9119"})
            print("[CDP] Cleaned cookies strictly for http://127.0.0.1:9119")

            # Navigate to session
            target_url = f"http://127.0.0.1:9119/chat?resume={quote(session_id, safe='')}"
            print(f"[CDP] Navigating to {target_url}...")
            await cmd("Page.navigate", {"url": target_url})

            async def eval_js(expr):
                res = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return res.get("result", {}).get("value")

            # Wait 6 seconds for PTY replay and hydration
            print("[CDP] Waiting 6s for terminal hydration...")
            await asyncio.sleep(6)

            # Extract xterm instance from React fiber and inspect buffer
            extract_script = '''(() => {
                const xterm = document.querySelector('.xterm');
                const parent = xterm ? xterm.parentElement : null;
                let fiberKey = Object.keys(parent || {}).find(k => k.startsWith('__reactFiber'));
                let curr = parent ? parent[fiberKey] : null;
                let term = null;

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

                if (!term) return { error: 'term not found in fiber' };

                const buf = term.buffer.active;
                const total = buf.length;
                const baseY = buf.baseY;
                const cursorY = buf.cursorY;

                let bannerLines = [];
                let capabilityLines = [];
                let firstPromptLines = [];

                for (let i = 0; i < total; i++) {
                    const l = buf.getLine(i)?.translateToString(true) || '';
                    if (l.includes('Messenger of the Digital Gods') || l.includes('Nous Research ·')) {
                        bannerLines.push({ line: i, text: l });
                    }
                    if (l.toLowerCase().includes('capability matrix') || l.toLowerCase().includes('governance')) {
                        capabilityLines.push({ line: i, text: l.slice(0, 90) });
                    }
                    if (l.includes('i need your help with investigating web tui bugs')) {
                        firstPromptLines.push({ line: i, text: l });
                    }
                }

                // First 15 non-empty lines
                let topLines = [];
                for (let i = 0; i < Math.min(60, total); i++) {
                    const l = buf.getLine(i)?.translateToString(true) || '';
                    if (l.trim()) topLines.push({ line: i, text: l });
                    if (topLines.length >= 15) break;
                }

                return {
                    totalLines: total,
                    baseY,
                    cursorY,
                    geometry: `${term.cols}x${term.rows}`,
                    bannerCount: bannerLines.length,
                    bannerLines,
                    firstPromptCount: firstPromptLines.length,
                    firstPromptLines,
                    capabilityCount: capabilityLines.length,
                    capabilitySample: capabilityLines.slice(0, 5),
                    topLines
                };
            })()'''

            res = await eval_js(extract_script)
            print("\n=== CDP AUDIT RESULTS ===")
            print(f"Total buffer lines: {res.get('totalLines')}")
            print(f"Scrollback baseY  : {res.get('baseY')}")
            print(f"Geometry          : {res.get('geometry')}")
            print(f"Banner count      : {res.get('bannerCount')}")
            for b in res.get('bannerLines', []):
                print(f"  - Banner at line {b['line']}: {b['text']!r}")
            print(f"Original user prompt count: {res.get('firstPromptCount')}")
            for u in res.get('firstPromptLines', []):
                print(f"  - User prompt at line {u['line']}: {u['text']!r}")
            print(f"Capability Matrix occurrences: {res.get('capabilityCount')}")
            for c in res.get('capabilitySample', []):
                print(f"  - Line {c['line']}: {c['text']!r}")
            print("\nTop 10 non-empty lines in buffer:")
            for t in res.get('topLines', [])[:10]:
                print(f"  Line {t['line']}: {t['text'][:100]!r}")

    finally:
        req_close = urllib.request.Request(f"{base_cdp}/json/close/{tab_id}", method="PUT")
        await asyncio.to_thread(lambda: urllib.request.urlopen(req_close, timeout=5).close())
        print(f"[CDP] Closed test tab {tab_id}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    args = parser.parse_args()
    asyncio.run(run_audit(session_id=args.session_id))
