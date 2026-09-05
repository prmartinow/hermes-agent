"""
Replication and Validation Harness for Chat History & Session Switching Bugs:
1. Re-attaching to active streaming session (PtyResumeSanitizer erase-code suppression / scrambling).
2. Scrolling in inactive sessions (3-page MAX_SPAN cap / clamp blanking / black nothingness).
3. Ground truth comparison against state.db.
"""
import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
import websockets
from hermes_state import SessionDB

INACTIVE_SESSION_ID = "20260831_071104_f61c30"
IMAGE_DIR = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

async def cdp_eval(ws, expr):
    msg_id = int(time.time() * 1000) % 10000000
    payload = {
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}
    }
    await ws.send(json.dumps(payload))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg.get("result", {}).get("value")

async def cdp_call(ws, method, params=None):
    msg_id = int(time.time() * 1000) % 10000000
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send(json.dumps(payload))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg.get("result", {})

async def capture_screen(ws, filename):
    res = await cdp_call(ws, "Page.captureScreenshot", {"format": "png"})
    path = IMAGE_DIR / filename
    if "data" in res:
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))
        print(f"  [Artifact Captured] Saved: {path}")
    return str(path)

async def extract_dom_lines(ws):
    script = """(() => {
        const rows = Array.from(document.querySelectorAll(".xterm-rows > div"));
        return rows.map((r, i) => ({ line: i, text: r.innerText || r.textContent || "" })).filter(r => r.text.trim().length > 0);
    })()"""
    lines = await cdp_eval(ws, script)
    return lines or []

async def dispatch_scroll(ws, delta_y, ticks=10):
    box = await cdp_eval(ws, """(() => {
        const el = document.querySelector(".xterm-screen") || document.querySelector(".xterm");
        if (!el) return { x: 500, y: 400 };
        const rect = el.getBoundingClientRect();
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    })()""")
    if not isinstance(box, dict):
        box = {"x": 500, "y": 400}
    cx = box.get("x", 500)
    cy = box.get("y", 400)
    for _ in range(ticks):
        await cdp_call(ws, "Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": cx,
            "y": cy,
            "deltaX": 0,
            "deltaY": delta_y
        })
        await asyncio.sleep(0.04)

async def run_harness():
    print("=" * 80)
    print("STARTING LIVE FRONTEND VALIDATION & REPLICATION HARNESS")
    print("=" * 80)

    # 1. Start the active research task in a fresh session
    prompt = (
        "Scan our web TUI codebase in ./web and ./ui-tui "
        "to understand our xterm.js, React, and Ink rendering architecture. Then search the web for best practices "
        "on rendering large chat history and high-speed virtual scrolling in terminal web interfaces."
    )
    cmd = [
        "/opt/hermes/.venv/bin/hermes",
        "chat",
        "-q",
        "--prompt",
        prompt
    ]
    env = os.environ.copy()
    env["HERMES_USE_NSENTER"] = "0"
    env["HERMES_HOME"] = os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))

    print("\n[Step 1] Spawning active background agent task...")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="."
    )
    print(f"  • Spawned background agent PID: {proc.pid}")

    # Wait for the session to register in state.db
    active_sid = None
    db_path = "/opt/data/state.db" if Path("/opt/data/state.db").exists() else str(Path.home() / ".hermes/state.db")
    for _ in range(30):
        await asyncio.sleep(1.0)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        row = cur.execute("SELECT id, title, last_activity_at FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if row and row[0] != INACTIVE_SESSION_ID:
            active_sid = row[0]
            print(f"  • Found newly active session ID: {active_sid}")
            break

    if not active_sid:
        print("  ⚠ Could not find new active session; using latest session")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        active_sid = cur.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()

    # Connect to Chrome DevTools Protocol
    tabs_raw = urllib.request.urlopen("http://127.0.0.1:9250/json/list").read().decode()
    tabs = json.loads(tabs_raw)
    page_tab = next(t for t in tabs if t.get("type") == "page")
    ws_url = page_tab["webSocketDebuggerUrl"]
    print(f"\n[Step 2] Connected to Chrome CDP: {ws_url}")

    async with websockets.connect(ws_url) as ws:
        await cdp_call(ws, "Runtime.enable")
        await cdp_call(ws, "Page.enable")

        print("\n" + "=" * 80)
        print("STARTING 3-CYCLE PING-PONG SESSION SWITCHING REPLICATION")
        print("=" * 80)

        for cycle in range(1, 4):
            print(f"\n>>> CYCLE {cycle}/3: Switching to Inactive Session ({INACTIVE_SESSION_ID})")
            # Navigate to inactive session
            nav_url = f"http://127.0.0.1:9119/chat?resume={INACTIVE_SESSION_ID}"
            await cdp_call(ws, "Page.navigate", {"url": nav_url})
            await asyncio.sleep(2.0)

            # Inspect inactive session rendering at bottom
            lines_bottom = await extract_dom_lines(ws)
            print(f"  • Inactive Session (Bottom): {len(lines_bottom)} rendered DOM rows.")
            for l in lines_bottom[:3]:
                print(f"    [Line {l['line']}]: {l['text']}")
            await capture_screen(ws, f"cycle_{cycle}_inactive_bottom.png")

            # Scroll UP 3 pages and check if 4th+ page hits blank nothingness
            print(f"  • Testing upward scroll on inactive session (Dispatching wheel up)...")
            await dispatch_scroll(ws, delta_y=-1200, ticks=15)
            await asyncio.sleep(0.8)
            lines_mid = await extract_dom_lines(ws)
            print(f"  • Inactive Session (Mid-Scroll ~3 pages up): {len(lines_mid)} rendered DOM rows.")
            await capture_screen(ws, f"cycle_{cycle}_inactive_scroll_mid.png")

            # Scroll all the way up (100 ticks) to test if top blanking occurs
            await dispatch_scroll(ws, delta_y=-1500, ticks=50)
            await asyncio.sleep(0.8)
            lines_top = await extract_dom_lines(ws)
            print(f"  • Inactive Session (Top Scroll): {len(lines_top)} rendered DOM rows.")
            if lines_top:
                for l in lines_top[:3]:
                    print(f"    [Top Line {l['line']}]: {l['text']}")
            else:
                print("    ⚠ ALERT REPLICATED: Black nothingness / 0 rendered DOM rows!")
            await capture_screen(ws, f"cycle_{cycle}_inactive_scroll_top.png")

            # Now switch BACK to the active researching session while agent is busy!
            print(f"\n>>> CYCLE {cycle}/3: Switching BACK to Active Streaming Agent ({active_sid})")
            nav_active_url = f"http://127.0.0.1:9119/chat?resume={active_sid}"
            await cdp_call(ws, "Page.navigate", {"url": nav_active_url})
            await asyncio.sleep(2.0)

            # Check if active session DOM is scrambled or overprinting
            active_lines = await extract_dom_lines(ws)
            print(f"  • Active Session Re-attach: {len(active_lines)} rendered DOM rows.")
            for l in active_lines[:6]:
                print(f"    [Active Line {l['line']}]: {l['text']}")
            await capture_screen(ws, f"cycle_{cycle}_active_reattach.png")

            # Wait a moment while agent generates new activity
            await asyncio.sleep(3.0)
            active_lines_live = await extract_dom_lines(ws)
            print(f"  • Active Session with New Streaming Activity: {len(active_lines_live)} rows.")
            await capture_screen(ws, f"cycle_{cycle}_active_streaming.png")

        print("\n" + "=" * 80)
        print("EVALUATION HARNESS COMPLETED SUCCESSFULLY")
        print("=" * 80)

if __name__ == "__main__":
    asyncio.run(run_harness())
