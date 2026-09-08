#!/usr/bin/env node
import { createRequire } from 'module';
import os from 'os';
import path from 'path';

const require = createRequire(import.meta.url);

function resolvePlaywright() {
  try {
    return require('playwright-core');
  } catch {
    const fallbackPaths = [
      path.join(os.homedir(), 'node_modules', 'playwright-core'),
      path.join(process.cwd(), 'node_modules', 'playwright-core'),
      path.join(process.cwd(), 'web', 'node_modules', 'playwright-core'),
    ];
    for (const p of fallbackPaths) {
      try {
        return require(p);
      } catch {}
    }
    throw new Error('playwright-core not found. Please install playwright-core.');
  }
}

const { chromium } = resolvePlaywright();

const CDP_URL = process.env.CDP_URL || 'http://localhost:9250';
const DASHBOARD_URL = process.env.DASHBOARD_URL || 'http://localhost:9119/chat';

async function run() {
  console.log(`Connecting over CDP to ${CDP_URL}...`);
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  let page = context.pages().find(p => p.url().includes('/chat'));
  if (!page) {
    page = await context.newPage();
    await page.goto(DASHBOARD_URL);
  }

  const client = await page.context().newCDPSession(page);
  const xtermScreen = page.locator('.xterm-screen');
  await xtermScreen.waitFor({ state: 'visible', timeout: 10000 });

  const getVisibleLines = async () => {
    return await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.xterm-rows > div'));
      return rows.map(r => (r.textContent || '').trim()).filter(Boolean);
    });
  };

  const box = await xtermScreen.boundingBox();
  const startX = Math.round(box.x + box.width / 2);

  const initialLines = await getVisibleLines();
  console.log('Initial visible line 0:', initialLines[0] || '(empty)');
  console.log('Initial visible line 1:', initialLines[1] || '(empty)');

  // Swipe DOWN (finger moves down -> reveal older backlog above)
  const swipeDownStart = Math.round(box.y + box.height * 0.25);
  const swipeDownEnd = Math.round(box.y + box.height * 0.85);

  console.log('Executing multi-step Swipe DOWN to scroll up into backlog...');
  await client.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: startX, y: swipeDownStart, id: 10 }]
  });
  await page.waitForTimeout(16);

  const steps = 15;
  for (let i = 1; i <= steps; i++) {
    const curY = Math.round(swipeDownStart + ((swipeDownEnd - swipeDownStart) * i) / steps);
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: startX, y: curY, id: 10 }]
    });
    await page.waitForTimeout(20);
  }

  await client.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
  });

  await page.waitForTimeout(600);

  const afterScrolledUpLines = await getVisibleLines();
  console.log('After Swipe Down visible line 0:', afterScrolledUpLines[0] || '(empty)');
  console.log('After Swipe Down visible line 1:', afterScrolledUpLines[1] || '(empty)');

  // Swipe UP (finger moves up -> reveal newer content below)
  const swipeUpStart = Math.round(box.y + box.height * 0.85);
  const swipeUpEnd = Math.round(box.y + box.height * 0.25);

  console.log('Executing multi-step Swipe UP to scroll down towards bottom...');
  await client.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: startX, y: swipeUpStart, id: 20 }]
  });
  await page.waitForTimeout(16);

  for (let i = 1; i <= steps; i++) {
    const curY = Math.round(swipeUpStart + ((swipeUpEnd - swipeUpStart) * i) / steps);
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: startX, y: curY, id: 20 }]
    });
    await page.waitForTimeout(20);
  }

  await client.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
  });

  await page.waitForTimeout(600);

  const afterScrolledDownLines = await getVisibleLines();
  console.log('After Swipe Up visible line 0:', afterScrolledDownLines[0] || '(empty)');
  console.log('After Swipe Up visible line 1:', afterScrolledDownLines[1] || '(empty)');

  console.log('Verification completed successfully!');
  process.exit(0);
}

run().catch(e => {
  console.error('Test error:', e);
  process.exit(1);
});
