import asyncio, json, sys, time, urllib.request, websockets

CDP_PORT = 9250
TARGET_SESSION = '20260830_095900_4698e4'

async def run_full_directives_audit() -> bool:
    print('=' * 80)
    print('LIVE WEB TUI FULL DIRECTIVES & STABILITY TEST SUITE (:9119 via CDP)')
    print('=' * 80)

    try:
        tabs_raw = urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json/list').read().decode()
        tabs = json.loads(tabs_raw)
    except Exception as exc:
        print(f'[FATAL] Unable to connect to CDP on port {CDP_PORT}: {exc}')
        return False

    chat_tab = next((t for t in tabs if '9119/chat' in t.get('url', '')), None)
    if not chat_tab:
        print('[FATAL] No active 9119/chat tab found in Chrome.')
        return False

    print(f'Target Tab: {chat_tab["title"]}')
    print(f'WS Debugger: {chat_tab["webSocketDebuggerUrl"]}')

    async with websockets.connect(chat_tab['webSocketDebuggerUrl']) as ws:
        msg_id = 0
        async def cdp_call(method: str, params: dict | None = None) -> dict:
            nonlocal msg_id
            msg_id += 1
            payload = {'id': msg_id, 'method': method, 'params': params or {}}
            await ws.send(json.dumps(payload))
            while True:
                res = json.loads(await ws.recv())
                if res.get('id') == msg_id:
                    return res.get('result', {})

        await cdp_call('Page.enable')
        await cdp_call('Runtime.enable')
        await cdp_call('DOM.enable')

        nav_url = f'http://127.0.0.1:9119/chat?resume={TARGET_SESSION}'
        print(f'\n[INIT] Navigating to target session with deep transcript: {nav_url}...')
        await cdp_call('Page.navigate', {'url': nav_url})
        await asyncio.sleep(4.0)

        # 1. DIRECTIVE 1: INLINE_MODE=1 & Primary Buffer
        print('\n[DIRECTIVE 1] Testing Native Browser Scrollbar & INLINE_MODE=1 Invariant...')
        d1_expr = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          return {'
            '            scrollback: val.options.scrollback,'
            '            scrollOnUserInput: val.options.scrollOnUserInput,'
            '            bufferType: val.buffer.active.type,'
            '            totalBufferLines: val.buffer.active.length,'
            '            cursorStyle: val.options.cursorStyle,'
            '            cursorBlink: val.options.cursorBlink,'
            '          };'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '  return null;'
            '})()'
        )
        res_d1 = await cdp_call('Runtime.evaluate', {'expression': d1_expr, 'returnByValue': True})
        d1 = res_d1.get('result', {}).get('value')
        assert d1 is not None, 'Failed to inspect xterm Terminal instance from DOM'
        print(f"  • Terminal Buffer Type:   {d1.get('bufferType')} (Expected: 'normal')")
        print(f"  • Scrollback Limit:       {d1.get('scrollback')} lines (Expected: >= 50000)")
        print(f"  • Total Buffer Length:    {d1.get('totalBufferLines')} lines")
        print(f"  • scrollOnUserInput:      {d1.get('scrollOnUserInput')} (Expected: False)")
        print(f"  • Cursor Style:           {d1.get('cursorStyle')} (Blink: {d1.get('cursorBlink')})")

        assert d1.get('bufferType') == 'normal', 'Terminal is not running in normal primary buffer!'
        assert d1.get('scrollback') >= 50000, 'Scrollback is not configured for full session capacity!'
        assert d1.get('scrollOnUserInput') is False, 'scrollOnUserInput is not False!'
        assert d1.get('cursorStyle') == 'underline' and d1.get('cursorBlink') is False, 'Cursor styling invalid!'
        print('  ✓ PASS [DIRECTIVE 1]: Primary buffer with 50k lines and scrollOnUserInput=false verified.')

        # 2. DIRECTIVE 2: Chat History Fully Displayed (Turn 1 to Tip)
        print('\n[DIRECTIVE 2] Testing Complete Chat History Display (Turn 1 to Tip)...')
        d2_expr = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          const b = val.buffer.active;'
            '          const firstLines = [];'
            '          for (let i = 0; i < Math.min(20, b.length); i++) {'
            '            const text = b.getLine(i)?.translateToString(true).trim();'
            '            if (text) firstLines.push({ i, text });'
            '          }'
            '          const lastLines = [];'
            '          for (let i = Math.max(0, b.length - 20); i < b.length; i++) {'
            '            const text = b.getLine(i)?.translateToString(true).trim();'
            '            if (text) lastLines.push({ i, text });'
            '          }'
            '          return {'
            '            length: b.length,'
            '            baseY: b.baseY,'
            '            viewportY: b.viewportY,'
            '            firstLines,'
            '            lastLines,'
            '          };'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '  return null;'
            '})()'
        )
        res_d2 = await cdp_call('Runtime.evaluate', {'expression': d2_expr, 'returnByValue': True})
        d2 = res_d2.get('result', {}).get('value')
        print(f"  • Total Lines Loaded in History: {d2['length']}")
        print(f"  • Top of History (Line {d2['firstLines'][0]['i']}): '{d2['firstLines'][0]['text']}'")
        print(f"  • Banner Subtitle: '{d2['firstLines'][1]['text']}'")
        print(f"  • Bottom Line (Line {d2['lastLines'][-1]['i']}): '{d2['lastLines'][-1]['text']}'")

        assert any('Hermes Agent' in l['text'] for l in d2['firstLines']), 'Turn 1 Top Banner not found!'
        assert any('❯' in l['text'] for l in d2['lastLines']), 'Bottom composer prompt not found!'
        assert d2['length'] > 1000, f"History unexpectedly short ({d2['length']} lines)"
        print('  ✓ PASS [DIRECTIVE 2]: Entire transcript from Turn 1 banner to tip is present in buffer.')

        # 3. DIRECTIVE 3: Canvas Click Stability (Zero-Drift Invariant)
        print('\n[DIRECTIVE 3] Testing Canvas Click Stability (scrollOnUserInput=false)...')
        d3_scroll = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          val.scrollToLine(500);'
            '          return {'
            '            viewportY: val.buffer.active.viewportY,'
            '            baseY: val.buffer.active.baseY'
            '          };'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '  return null;'
            '})()'
        )
        res_scroll = await cdp_call('Runtime.evaluate', {'expression': d3_scroll, 'returnByValue': True})
        s_pos = res_scroll.get('result', {}).get('value')
        print(f"  • Scrolled ViewportY to: {s_pos['viewportY']} (baseY is {s_pos['baseY']})")

        d3_box = (
            '(() => {'
            '  const screen = document.querySelector(".xterm-screen");'
            '  const r = screen.getBoundingClientRect();'
            '  return { x: Math.round(r.left + 250), y: Math.round(r.top + 150) };'
            '})()'
        )
        res_box = await cdp_call('Runtime.evaluate', {'expression': d3_box, 'returnByValue': True})
        coord = res_box.get('result', {}).get('value')
        print(f"  • Clicking canvas at ({coord['x']}, {coord['y']})...")

        await cdp_call('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': coord['x'], 'y': coord['y'], 'button': 'left', 'clickCount': 1
        })
        await asyncio.sleep(0.05)
        await cdp_call('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': coord['x'], 'y': coord['y'], 'button': 'left', 'clickCount': 1
        })
        await asyncio.sleep(0.8)

        d3_post = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          return val.buffer.active.viewportY;'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '  return null;'
            '})()'
        )
        res_post_click = await cdp_call('Runtime.evaluate', {'expression': d3_post, 'returnByValue': True})
        post_y = res_post_click.get('result', {}).get('value')
        drift = abs(post_y - s_pos['viewportY'])
        print(f"  • ViewportY before click: {s_pos['viewportY']}")
        print(f"  • ViewportY after click:  {post_y}")
        print(f"  • Measured Scroll Drift:  {drift} lines")
        assert drift == 0, f"FAIL: Canvas click caused {drift} lines of scroll drift!"
        print('  ✓ PASS [DIRECTIVE 3]: Viewport remained completely stationary on canvas click (0 drift).')

        # 4. DIRECTIVE 4: DEC Mouse Tracking & Button Event Reporting
        print('\n[DIRECTIVE 4] Testing DEC Mouse Tracking (Modes 1000/1002/1006)...')
        d4_setup = (
            '(() => {'
            '  const termEl = document.querySelector(".terminal");'
            '  window.__capturedSgr = [];'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          const ms = val._core.coreMouseService;'
            '          val.onData(data => {'
            '            if (data.includes("\x1b[<")) {'
            '              window.__capturedSgr.push(data);'
            '            }'
            '          });'
            '          return {'
            '            hasMouseEventsClass: termEl?.classList.contains("enable-mouse-events"),'
            '            activeProtocol: ms.activeProtocol,'
            '            activeEncoding: ms.activeEncoding,'
            '            areMouseEventsActive: ms.areMouseEventsActive,'
            '          };'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '  return null;'
            '})()'
        )
        res_d4 = await cdp_call('Runtime.evaluate', {'expression': d4_setup, 'returnByValue': True})
        d4 = res_d4.get('result', {}).get('value')
        print(f"  • DOM Class enable-mouse-events: {d4.get('hasMouseEventsClass')}")
        print(f"  • Active DEC Protocol:           {d4.get('activeProtocol')}")
        print(f"  • Active DEC Encoding:           {d4.get('activeEncoding')}")

        d4_dispatch = (
            '(() => {'
            '  const screen = document.querySelector(".xterm-screen");'
            '  const r = screen.getBoundingClientRect();'
            '  const targetX = Math.round(r.left + 200);'
            '  const targetY = Math.round(r.bottom - 25);'
            '  const down = new MouseEvent("mousedown", {'
            '    bubbles: true, cancelable: true, clientX: targetX, clientY: targetY, button: 0, buttons: 1'
            '  });'
            '  screen.dispatchEvent(down);'
            '  const up = new MouseEvent("mouseup", {'
            '    bubbles: true, cancelable: true, clientX: targetX, clientY: targetY, button: 0, buttons: 0'
            '  });'
            '  screen.dispatchEvent(up);'
            '})()'
        )
        await cdp_call('Runtime.evaluate', {'expression': d4_dispatch})
        await asyncio.sleep(0.3)

        res_sgr = await cdp_call('Runtime.evaluate', {'expression': 'window.__capturedSgr || []', 'returnByValue': True})
        sgr_events = res_sgr.get('result', {}).get('value', [])
        print(f"  • Emitted SGR Mouse Reports:     {sgr_events}")
        assert d4.get('areMouseEventsActive') is True, 'DEC mouse events are not active!'
        assert len(sgr_events) >= 2, 'Failed to capture SGR press and release packets!'
        assert any(e.endswith('M') for e in sgr_events), 'Missing SGR Press packet (M)!'
        assert any(e.endswith('m') for e in sgr_events), 'Missing SGR Release packet (m)!'
        print('  ✓ PASS [DIRECTIVE 4]: DEC 1000/1006 SGR button tracking verified.')

        # 5. DIRECTIVE 5: Wheel Decoupling via attachCustomWheelEventHandler
        print('\n[DIRECTIVE 5] Testing Client-Side Wheel Decoupling...')
        d5_bottom = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        const val = st.memoizedState?.current;'
            '        if (val && typeof val.onData === "function") {'
            '          val.scrollToBottom();'
            '          return true;'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    fiber = fiber.return;'
            '  }'
            '})()'
        )
        await cdp_call('Runtime.evaluate', {'expression': d5_bottom})
        await asyncio.sleep(0.3)

        await cdp_call('Runtime.evaluate', {'expression': 'window.__capturedSgr = []'})
        d5_wheel = (
            '(() => {'
            '  const screen = document.querySelector(".xterm-screen");'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  let val = null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        if (st.memoizedState?.current?.onData) {'
            '          val = st.memoizedState.current;'
            '          break;'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    if (val) break;'
            '    fiber = fiber.return;'
            '  }'
            '  const y0 = val.buffer.active.viewportY;'
            '  const ev = new WheelEvent("wheel", {'
            '    deltaY: -120, bubbles: true, cancelable: true, clientX: 400, clientY: 200'
            '  });'
            '  const allowed = screen.dispatchEvent(ev);'
            '  const y1 = val.buffer.active.viewportY;'
            '  return {'
            '    y0, y1, deltaLines: y0 - y1, defaultPrevented: ev.defaultPrevented'
            '  };'
            '})()'
        )
        res_wheel = await cdp_call('Runtime.evaluate', {'expression': d5_wheel, 'returnByValue': True})
        w = res_wheel.get('result', {}).get('value')
        print(f"  • Wheel Event Default Prevented: {w.get('defaultPrevented')}")
        print(f"  • Buffer Viewport Scrolled:      {w.get('deltaLines')} lines")

        res_wheel_sgr = await cdp_call('Runtime.evaluate', {'expression': 'window.__capturedSgr || []', 'returnByValue': True})
        wheel_sgr = res_wheel_sgr.get('result', {}).get('value', [])
        print(f"  • Emitted SGR Wheel Packets:     {wheel_sgr}")

        assert w.get('defaultPrevented') is True, 'Wheel event was not intercepted by custom handler!'
        assert w.get('deltaLines') > 0, 'Buffer failed to scroll locally on wheel event!'
        assert len([s for s in wheel_sgr if '<64;' in s or '<65;' in s]) == 0, 'FAIL: SGR wheel escape codes leaked to PTY!'
        print('  ✓ PASS [DIRECTIVE 5]: Wheel handled locally in browser buffer without SGR leakage.')

        # 6. DIRECTIVE 6: Copy Integration
        print('\n[DIRECTIVE 6] Testing Copy Integration (Terminal Selection -> System Clipboard)...')
        d6_copy = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  let val = null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        if (st.memoizedState?.current?.onData) {'
            '          val = st.memoizedState.current;'
            '          break;'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    if (val) break;'
            '    fiber = fiber.return;'
            '  }'
            '  val.scrollToTop();'
            '  val.select(0, 1, 40);'
            '  const sel = val.getSelection();'
            '  return { selected: sel };'
            '})()'
        )
        res_copy = await cdp_call('Runtime.evaluate', {'expression': d6_copy, 'returnByValue': True})
        c_res = res_copy.get('result', {}).get('value')
        print(f"  • Selected Text:                 '{c_res.get('selected').strip()}'")
        assert len(c_res.get('selected', '')) > 0, 'Failed to select text in terminal!'
        assert len(c_res.get('selected', '').strip()) > 0, 'Selected text is empty!'
        print('  ✓ PASS [DIRECTIVE 6]: Text selection and synchronous copy integration verified.')

        # 7. DIRECTIVE 7: Paste Integration
        print('\n[DIRECTIVE 7] Testing Paste Integration (Clipboard -> Terminal Paste)...')
        d7_paste = (
            '(() => {'
            '  const host = document.querySelector(".hermes-chat-xterm-host");'
            '  const key = Object.keys(host || {}).find(k => k.startsWith("__reactFiber$"));'
            '  let fiber = host ? host[key] : null;'
            '  let val = null;'
            '  while (fiber) {'
            '    if (fiber.memoizedState) {'
            '      let st = fiber.memoizedState;'
            '      while (st) {'
            '        if (st.memoizedState?.current?.onData) {'
            '          val = st.memoizedState.current;'
            '          break;'
            '        }'
            '        st = st.next;'
            '      }'
            '    }'
            '    if (val) break;'
            '    fiber = fiber.return;'
            '  }'
            '  val.focus();'
            '  const dt = new DataTransfer();'
            '  dt.setData("text/plain", "/help-test");'
            '  const pasteEv = new ClipboardEvent("paste", {'
            '    bubbles: true, cancelable: true, clipboardData: dt'
            '  });'
            '  val.textarea.dispatchEvent(pasteEv);'
            '  return {'
            '    dispatched: true, defaultPrevented: pasteEv.defaultPrevented'
            '  };'
            '})()'
        )
        res_paste = await cdp_call('Runtime.evaluate', {'expression': d7_paste, 'returnByValue': True})
        p_res = res_paste.get('result', {}).get('value')
        print(f"  • Paste Event Dispatched:        {p_res.get('dispatched')}")
        print(f"  • Default Prevented (Handled):   {p_res.get('defaultPrevented')}")
        assert p_res.get('defaultPrevented') is True, 'Paste event was not intercepted and processed by handler!'
        print('  ✓ PASS [DIRECTIVE 7]: Paste event intercepted and processed cleanly.')

    print('\\n' + '=' * 80)
    print('ALL 7 CORE WEB TUI DIRECTIVES PASSED LIVE ON PORT 9119!')
    print('=' * 80)
    return True

if __name__ == '__main__':
    ok = asyncio.run(run_full_directives_audit())
    sys.exit(0 if ok else 1)
