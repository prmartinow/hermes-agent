#!/usr/bin/env python3
"""Automated Playwright Mobile Touch Scroll Validation Script.

Mints a test session token and launches Chromium in mobile emulation mode
to verify mobile touch scrolling and capture diagnostic snapshots.
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
except Exception as e:
    auth_data = {"at": "", "rt": ""}

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:9119/chat")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", str(Path.home() / ".hermes" / "images"))
RESUME_ID = os.getenv("RESUME_ID", "")

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

  console.log('========================================================================');
  console.log('PLAYWRIGHT MOBILE TOUCH GESTURE VALIDATION & SCREENSHOT PROOF');
  console.log('========================================================================');

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
  const targetUrl = {json.dumps(DASHBOARD_URL)} + ({json.dumps(RESUME_ID)} ? `?resume=${json.dumps(RESUME_ID)}` : '');
  console.log(`1. Loading chat session (${{targetUrl}})...`);
  await page.goto(targetUrl, {{ waitUntil: 'networkidle' }});

  await page.waitForSelector('.hermes-chat-xterm-host', {{ timeout: 10000 }});
  console.log('   ✓ Terminal host element mounted.');

  await page.waitForTimeout(3000);

  const outDir = {json.dumps(OUTPUT_DIR)};
  if (!fs.existsSync(outDir)) {{
    fs.mkdirSync(outDir, {{ recursive: true }});
  }}

  const proof1 = path.join(outDir, 'mobile_proof_1_bottom.png');
  await page.screenshot({{ path: proof1 }});
  console.log(`   ✓ Screenshot 1 captured: ${{proof1}}`);

  const host = await page.$('.hermes-chat-xterm-host');
  const box = await host.boundingBox();
  const startX = box.x + box.width / 2;

  console.log('2. Performing touch swipe DOWN gestures (scrolling UP)...');
  for (let swipe = 1; swipe <= 3; swipe++) {{
    const downStartY = box.y + box.height * 0.25;
    const downEndY = box.y + box.height * 0.85;
    const steps = 15;

    for (let i = 0; i <= steps; i++) {{
      const y = downStartY + (downEndY - downStartY) * (i / steps);
      await page.evaluate(({{ sx, cy, isFirst, isLast, swipeId }}) => {{
        const el = document.querySelector('.hermes-chat-xterm-host') || document.body;
        const touch = new Touch({{
          identifier: 200 + swipeId,
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
      }}, {{ sx: startX, cy: y, isFirst: i === 0, isLast: i === steps, swipeId: swipe }});
      await page.waitForTimeout(25);
    }}
    await page.waitForTimeout(200);
  }}

  await page.waitForTimeout(1000);

  const proof2 = path.join(outDir, 'mobile_proof_2_scrolled_up.png');
  await page.screenshot({{ path: proof2 }});
  console.log(`   ✓ Screenshot 2 captured: ${{proof2}}`);

  console.log('\\nTouch scroll actions executed and captured successfully!');
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
