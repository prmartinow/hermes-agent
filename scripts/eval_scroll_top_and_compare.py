from pathlib import Path
#!/usr/bin/env python3
"""Evaluation: Scroll to the top in real browser via CDP, extract top visible message and banner,
and compare with the first entry in state.db.
"""

import asyncio
import base64
import json
import os
import urllib.request
import websockets
from hermes_state import SessionDB


async def main():
    print("=" * 80)
    print("REAL BROWSER USE EVALUATION: SCROLL TO TOP & VERIFY EARLIEST TURN + BANNER")
    print("=" * 80)

    # 1. Connect to active browser tab via Chrome DevTools Protocol (CDP)
    tabs_data = urllib.request.urlopen("http://127.0.0.1:9250/json/list").read().decode()
    tabs = json.loads(tabs_data)
    page_tab = [t for t in tabs if t["type"] == "page"][0]
    ws_url = page_tab["webSocketDebuggerUrl"]
    current_url = page_tab.get("url", "")
    print(f"Connected to Browser Tab: {page_tab.get('title')}")
    print(f"Tab URL: {current_url}")

    # Extract target session_id from URL or default to 20260831_071104_f61c30
    session_id = "20260831_071104_f61c30"
    if "resume=" in current_url:
        session_id = current_url.split("resume=")[-1].split("&")[0]
    print(f"Target Session ID: {session_id}")

    # Query database for the ground truth Turn 1
    db = SessionDB()
    db_messages = db.get_messages(session_id, include_compacted=True)
    first_user_msg = next((m for m in db_messages if m.get("role") == "user"), None)
    total_db_count = len(db_messages)
    print(f"\n[Database Ground Truth]:")
    print(f"  • Total DB Messages: {total_db_count}")
    if first_user_msg:
        print(f"  • DB First User Msg ID: {first_user_msg.get('id')}")
        db_first_text = str(first_user_msg.get("content") or "")[:150].strip()
        print(f"  • DB First User Msg Preview: {db_first_text!r}")
    else:
        print("  • No user messages found in DB!")
        db_first_text = ""

    async with websockets.connect(ws_url) as ws:
        msg_id = 0

        async def call(method, params=None):
            nonlocal msg_id
            msg_id += 1
            cur_id = msg_id
            payload = {"id": cur_id, "method": method}
            if params:
                payload["params"] = params
            await ws.send(json.dumps(payload))
            while True:
                raw = await ws.recv()
                r = json.loads(raw)
                if r.get("id") == cur_id:
                    return r.get("result", {})

        await call("Runtime.enable")
        await call("Page.enable")

        # Focus xterm terminal
        await call(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                const term = document.querySelector('.xterm-helper-textarea') || document.querySelector('.xterm');
                if (term) term.focus();
            })()"""
            },
        )
        await asyncio.sleep(0.5)

        # 2. Scroll up to the top!
        # In xterm / Ink TUI, send wheel events or PageUp keys repeatedly to scroll to top.
        print("\nScrolling up to the top of the transcript...")
        # Get terminal container coordinates
        box_res = await call(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                const el = document.querySelector('.xterm-screen') || document.querySelector('.xterm');
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
            })()""",
                "returnByValue": True,
            },
        )
        coords = box_res.get("result", {}).get("value") or {"x": 500, "y": 400}
        cx, cy = coords["x"], coords["y"]

        # Send consecutive mouse wheel up events
        for _ in range(30):
            await call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": cx,
                    "y": cy,
                    "deltaX": 0,
                    "deltaY": -600,
                },
            )
            await asyncio.sleep(0.05)

        # Also send PageUp / Shift+Up keystrokes as secondary drive
        for _ in range(25):
            await call(
                "Input.dispatchKeyEvent",
                {
                    "type": "rawKeyDown",
                    "windowsVirtualKeyCode": 33,  # PageUp
                    "nativeVirtualKeyCode": 33,
                    "key": "PageUp",
                    "code": "PageUp",
                },
            )
            await call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "windowsVirtualKeyCode": 33,
                    "nativeVirtualKeyCode": 33,
                    "key": "PageUp",
                    "code": "PageUp",
                },
            )
            await asyncio.sleep(0.05)

        await asyncio.sleep(1.0)

        # 3. Read top rendered DOM lines
        inspect_script = r"""(() => {
            const rows = Array.from(document.querySelectorAll(".xterm-rows > div"));
            const nonBlank = rows.map((r, i) => ({ line: i, text: r.textContent.trim() })).filter(r => r.text.length > 0);
            return {
                totalRows: rows.length,
                renderedCount: nonBlank.length,
                lines: nonBlank,
                fullText: nonBlank.map(l => l.text).join("\n")
            };
        })()"""

        res = await call("Runtime.evaluate", {"expression": inspect_script, "returnByValue": True})
        val = res.get("result", {}).get("value", {})
        lines = val.get("lines", [])
        full_text = val.get("fullText", "")

        # 4. Capture screenshot
        image_dir = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(image_dir / f"chat_{session_id}_top.png")
        screenshot_res = await call("Page.captureScreenshot", {"format": "png"})
        if "data" in screenshot_res:
            with open(screenshot_path, "wb") as f:
                f.write(base64.b64decode(screenshot_res["data"]))
            print(f"\n[Screenshot Saved]: {screenshot_path}")

        print("\n[Top Rendered Lines in Browser Viewport]:")
        for l in lines[:15]:
            print(f"  [{l['line']:2d}] {l['text']}")

        # 5. Analysis & Invariant Checks
        has_hermes_banner = "Hermes" in full_text or "Hermes Agent" in full_text or "HERMES" in full_text
        has_first_user_turn = any(
            db_first_text[:30].lower() in l["text"].lower() or "input lag" in l["text"].lower()
            for l in lines
        )

        print("\n" + "=" * 80)
        print("EVALUATION RESULTS & COMPARISON")
        print("=" * 80)
        print(f"1. Hermes Banner / Tile Present at Top: {'✓ YES' if has_hermes_banner else '✗ NO'}")
        print(f"2. Earliest User Message Displayed:     {'✓ YES' if has_first_user_turn else '✗ NO'}")
        print(f"3. First Visible Text vs DB Entry:")
        print(f"   • Database Turn 1:  {db_first_text[:100]}")
        first_content_line = next((l["text"] for l in lines if "You" in l["text"] or "input lag" in l["text"] or "#" in l["text"]), "N/A")
        print(f"   • Browser Top Line: {first_content_line}")


if __name__ == "__main__":
    asyncio.run(main())
