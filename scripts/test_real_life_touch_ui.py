#!/usr/bin/env python3
"""Mobile Touch UI Diagnostic Harness.

Executes Playwright mobile touch tests against the local Hermes Web UI.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from plugins.dashboard_auth.basic import (
        BasicAuthProvider,
        _load_config_basic_auth_section,
        _resolve_secret,
    )
    cfg = _load_config_basic_auth_section()
    secret = _resolve_secret(cfg)
    username = cfg.get("username") or "user"
    password_hash = cfg.get("password_hash") or ""
    provider = BasicAuthProvider(username=username, password_hash=password_hash, secret=secret)
    session = provider._mint_session(provider._username)
    auth_data = {
        "at": session.access_token,
        "rt": session.refresh_token,
    }
except Exception:
    auth_data = {"at": "", "rt": ""}

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:9119/chat")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", str(Path.home() / ".hermes" / "images"))

js_test_script = f"""
const os = require('os');
const path = require('path');
const fs = require('fs');

function resolvePlaywright() {{
  try {{
    return require('playwright-core');
  }} catch {{
    const fallbackPaths = [
      path.join(os.homedir(), 'node_modules', 'playwright-core'),
      path.join(process.cwd(), 'node_modules', 'playwright-core'),
      path.join(process.cwd(), 'web', 'node_modules', 'playwright-core'),
    ];
    for (const p of fallbackPaths) {{
      try {{
        return require(p);
      }} catch {{}}
    }}
    throw new Error('playwright-core not found.');
  }}
}}

const {{ chromium }} = resolvePlaywright();

(async () => {{
  const browser = await chromium.launch({{
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  }});

  console.log('===============================================================');
  console.log('RUNNING MOBILE TOUCH TEST VIA PLAYWRIGHT (MOBILE EMULATION)');
  console.log('===============================================================');

  const mobileContext = await browser.newContext({{
    viewport: {{ width: 393, height: 851 }},
    hasTouch: true,
    isMobile: true,
    userAgent: 'Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/Mobile Safari/537.36'
  }});

  const atToken = {json.dumps(auth_data['at'])};
  const rtToken = {json.dumps(auth_data['rt'])};

  if (atToken) {{
    await mobileContext.addCookies([
      {{
        name: 'hermes_session_at',
        value: atToken,
        domain: 'localhost',
        path: '/'
      }},
      {{
        name: 'hermes_session_rt',
        value: rtToken,
        domain: 'localhost',
        path: '/'
      }}
    ]);
  }}

  const page = await mobileContext.newPage();
  console.log(`1. Navigating to ${{json.dumps(DASHBOARD_URL)}}...`);
  await page.goto({json.dumps(DASHBOARD_URL)}, {{ waitUntil: 'networkidle' }});

  await page.waitForSelector('.hermes-chat-xterm-host', {{ timeout: 10000 }});
  console.log('   ✓ Terminal host mounted.');
  await page.waitForTimeout(3000);

  const outDir = {json.dumps(OUTPUT_DIR)};
  if (!fs.existsSync(outDir)) {{
    fs.mkdirSync(outDir, {{ recursive: true }});
  }}

  const beforeScreenshot = path.join(outDir, 'real_life_touch_before.png');
  await page.screenshot({{ path: beforeScreenshot }});
  console.log(`   ✓ Baseline screenshot saved: ${{beforeScreenshot}}`);

  const host = await page.$('.hermes-chat-xterm-host');
  const box = await host.boundingBox();
  const startX = box.x + box.width / 2;

  console.log('2. Performing continuous swipe DOWN gesture...');
  const downStartY = box.y + box.height * 0.25;
  const downEndY = box.y + box.height * 0.85;
  const steps = 15;

  for (let i = 0; i <= steps; i++) {{
    const y = downStartY + (downEndY - downStartY) * (i / steps);
    await page.evaluate(({{ sx, cy, isFirst, isLast }}) => {{
      const el = document.querySelector('.hermes-chat-xterm-host') || document.body;
      const touch = new Touch({{
        identifier: 500,
        target: el,
        clientX: sx,
        clientY: cy,
        pageX: sx,
        pageY: cy,
        screenX: sx,
        screenY: cy,
      }});
      const type = isFirst ? 'touchstart' : (isLast ? 'touchend' : 'touchmove');
      const ev = new TouchEvent(type, {{
        bubbles: true,
        cancelable: true,
        touches: isLast ? [] : [touch],
        targetTouches: isLast ? [] : [touch],
        changedTouches: [touch]
      }});
      el.dispatchEvent(ev);
    }}, {{ sx: startX, cy: y, isFirst: i === 0, isLast: i === steps }});
    await page.waitForTimeout(25);
  }}

  await page.waitForTimeout(1000);

  const afterScreenshot = path.join(outDir, 'real_life_touch_after.png');
  await page.screenshot({{ path: afterScreenshot }});
  console.log(`   ✓ After-touch screenshot saved: ${{afterScreenshot}}`);

  console.log('\\nMobile touch test completed successfully!');
  await browser.close();
}})();
"""

with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
    f.write(js_test_script)
    tmp_path = f.name

try:
    res = subprocess.run(["node", tmp_path], capture_output=False)
    sys.exit(res.returncode)
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
