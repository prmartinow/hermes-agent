import asyncio
import json
import urllib.request
import websockets

async def test_drag():
    tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9250/json/list").read())
    chat_tab = next((t for t in tabs if "9119/chat" in t.get("url", "")), None)
    if not chat_tab:
        print("No chat tab found!")
        return

    async with websockets.connect(chat_tab["webSocketDebuggerUrl"]) as ws:
        msg_id = 0
        async def call(method, params=None):
            nonlocal msg_id
            msg_id += 1
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                res = json.loads(await ws.recv())
                if res.get("id") == msg_id:
                    return res

        await call("Runtime.enable")

        js_setup = """(() => {
            const host = document.querySelector(".hermes-chat-xterm-host");
            const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));
            let fiber = host ? host[key] : null;
            let term = null;
            while (fiber) {
                if (fiber.memoizedState) {
                    let st = fiber.memoizedState;
                    while (st) {
                        if (st.memoizedState?.current?.onData) {
                            term = st.memoizedState.current;
                            break;
                        }
                        st = st.next;
                    }
                }
                if (term) break;
                fiber = fiber.return;
            }
            term.clearSelection();
            const screen = document.querySelector(".xterm-screen");
            const rect = screen.getBoundingClientRect();
            
            window.__emittedPtyData = [];
            if (!window.__hasOnDataHook) {
                term.onData(data => {
                    window.__emittedPtyData.push(data);
                });
                window.__hasOnDataHook = true;
            }
            window.__emittedPtyData.length = 0;

            const vy = term.buffer.active.viewportY;
            const line8 = term.buffer.active.getLine(vy + 8);
            const line8Text = line8 ? line8.translateToString(true) : "";

            return {
                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                cellWidth: term._core._renderService.dimensions.css.cell.width,
                cellHeight: term._core._renderService.dimensions.css.cell.height,
                mouseTrackingActive: term._core.coreMouseService.areMouseEventsActive,
                selectionServiceEnabled: term._core._selectionService?._enabled,
                line8Text
            };
        })()"""

        setup_res = await call("Runtime.evaluate", {"expression": js_setup, "returnByValue": True})
        info = setup_res["result"]["result"]["value"]
        print("Setup Info:")
        print(json.dumps(info, indent=2))

        rx = info["rect"]["x"]
        ry = info["rect"]["y"]
        cw = info["cellWidth"]
        ch = info["cellHeight"]

        start_x = rx + cw * 4 + cw / 2
        start_y = ry + ch * 8 + ch / 2
        end_x = rx + cw * 39 + cw / 2
        end_y = start_y

        print(f"Targeting text on row 8: {info['line8Text']}")
        print(f"Coordinates: from ({start_x:.1f}, {start_y:.1f}) to ({end_x:.1f}, {end_y:.1f})")

        # TEST 1: STANDARD MOUSE DRAG
        print("\n" + "="*60)
        print("TEST 1: Standard User Left-Click & Drag (NO MODIFIERS)")
        print("="*60)

        await call("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": start_x,
            "y": start_y,
            "button": "left",
            "clickCount": 1
        })
        for step in range(1, 15):
            cx = start_x + (end_x - start_x) * (step / 14.0)
            await call("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": cx,
                "y": start_y,
                "button": "left"
            })
            await asyncio.sleep(0.01)
        await call("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": end_x,
            "y": end_y,
            "button": "left"
        })
        await asyncio.sleep(0.05)

        js_check = """(() => {
            const host = document.querySelector(".hermes-chat-xterm-host");
            const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));
            let fiber = host ? host[key] : null;
            let term = null;
            while (fiber) {
                if (fiber.memoizedState) {
                    let st = fiber.memoizedState;
                    while (st) {
                        if (st.memoizedState?.current?.onData) {
                            term = st.memoizedState.current;
                            break;
                        }
                        st = st.next;
                    }
                }
                if (term) break;
                fiber = fiber.return;
            }
            const sel = document.querySelector(".xterm-selection");
            return {
                hasSelection: term.hasSelection(),
                selectedText: term.getSelection(),
                domSelection: window.getSelection().toString(),
                divCount: sel ? sel.children.length : 0,
                emittedPtyData: window.__emittedPtyData
            };
        })()"""

        res1 = await call("Runtime.evaluate", {"expression": js_check, "returnByValue": True})
        out1 = res1["result"]["result"]["value"]
        print("Result of Standard Drag:")
        print(f"  term.hasSelection():     {out1['hasSelection']}")
        print(f"  term.getSelection():     '{out1['selectedText']}'")
        print(f"  window.getSelection():   '{out1['domSelection']}'")
        print(f"  .xterm-selection divs:   {out1['divCount']}")
        print(f"  Emitted PTY SGR packets: {out1['emittedPtyData']}")

        # TEST 2: SHIFT + MOUSE DRAG
        print("\n" + "="*60)
        print("TEST 2: Shift + Left-Click & Drag (Modifier Bypass)")
        print("="*60)

        await call("Runtime.evaluate", {"expression": "window.__emittedPtyData.length = 0;"})

        await call("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": start_x,
            "y": start_y,
            "button": "left",
            "modifiers": 8,
            "clickCount": 1
        })
        for step in range(1, 15):
            cx = start_x + (end_x - start_x) * (step / 14.0)
            await call("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": cx,
                "y": start_y,
                "button": "left",
                "modifiers": 8
            })
            await asyncio.sleep(0.01)
        await call("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": end_x,
            "y": end_y,
            "button": "left",
            "modifiers": 8
        })
        await asyncio.sleep(0.05)

        res2 = await call("Runtime.evaluate", {"expression": js_check, "returnByValue": True})
        out2 = res2["result"]["result"]["value"]
        print("Result of Shift + Drag:")
        print(f"  term.hasSelection():     {out2['hasSelection']}")
        print(f"  term.getSelection():     '{out2['selectedText']}'")
        print(f"  .xterm-selection divs:   {out2['divCount']}")
        print(f"  Emitted PTY SGR packets: {out2['emittedPtyData']}")

asyncio.run(test_drag())
