#!/usr/bin/env node
import { createRequire } from 'module';
import os from 'os';
import path from 'path';
import fs from 'fs';

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
const BASE_URL = process.env.DASHBOARD_URL || 'http://localhost:9119';
const RESUME_ID = process.env.RESUME_ID || '';
const OUTPUT_DIR = process.env.OUTPUT_DIR || path.join(os.homedir(), '.hermes', 'images');

async function run() {
  console.log(`Connecting to CDP on ${CDP_URL}...`);
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];
  let page = context.pages().find(p => p.url().includes('/chat'));
  if (!page) {
    page = await context.newPage();
  }

  const client = await page.context().newCDPSession(page);
  await client.send('Network.setCacheDisabled', { cacheDisabled: true });

  const sessionUrl = RESUME_ID ? `${BASE_URL}/chat?resume=${RESUME_ID}` : `${BASE_URL}/chat`;
  console.log(`Navigating to session: ${sessionUrl}...`);
  await page.goto(sessionUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);

  // Set mobile device metrics
  await client.send('Emulation.setDeviceMetricsOverride', {
    width: 390,
    height: 844,
    deviceScaleFactor: 3,
    mobile: true,
    hasTouch: true,
    screenOrientation: { angle: 0, type: 'portraitPrimary' }
  });
  await client.send('Emulation.setTouchEmulationEnabled', {
    enabled: true,
    maxTouchPoints: 5
  });

  const xtermScreen = page.locator('.xterm-screen');
  await xtermScreen.waitFor({ state: 'visible', timeout: 10000 });
  await page.waitForTimeout(2000);

  const getVisibleLines = async () => {
    return await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.xterm-rows > div'));
      return rows.map(r => (r.textContent || '').trim()).filter(Boolean);
    });
  };

  const initialLines = await getVisibleLines();
  console.log('Visible lines count:', initialLines.length);
  console.log('Initial Line 0:', initialLines[0] || '(empty)');
  console.log('Initial Line -1:', initialLines[initialLines.length - 1] || '(empty)');

  const box = await xtermScreen.boundingBox();
  const startX = Math.round(box.x + box.width / 2);

  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }
  const beforeImg = path.join(OUTPUT_DIR, 'long_session_before.png');
  await page.screenshot({ path: beforeImg });

  // Execute 3 consecutive upward scroll gestures (swiping down)
  const swipeDownStart = Math.round(box.y + box.height * 0.25);
  const swipeDownEnd = Math.round(box.y + box.height * 0.85);

  console.log('Executing 3 swipe down gestures to scroll far up into transcript history...');
  for (let swipe = 1; swipe <= 3; swipe++) {
    console.log(`Swipe ${swipe}...`);
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [{ x: startX, y: swipeDownStart, id: 100 + swipe }]
    });
    await page.waitForTimeout(16);

    const steps = 15;
    for (let i = 1; i <= steps; i++) {
      const curY = Math.round(swipeDownStart + ((swipeDownEnd - swipeDownStart) * i) / steps);
      await client.send('Input.dispatchTouchEvent', {
        type: 'touchMove',
        touchPoints: [{ x: startX, y: curY, id: 100 + swipe }]
      });
      await page.waitForTimeout(16);
    }

    await client.send('Input.dispatchTouchEvent', {
      type: 'touchEnd',
      touchPoints: []
    });

    await page.waitForTimeout(400);
  }

  const afterScrolledUpLines = await getVisibleLines();
  console.log('After scrolling up - Line 0:', afterScrolledUpLines[0] || '(empty)');
  console.log('After scrolling up - Line -1:', afterScrolledUpLines[afterScrolledUpLines.length - 1] || '(empty)');

  const scrolledUpImg = path.join(OUTPUT_DIR, 'long_session_scrolled_up.png');
  await page.screenshot({ path: scrolledUpImg });
  console.log('Captured scrolled up screenshot to:', scrolledUpImg);

  const isDifferent = JSON.stringify(initialLines) !== JSON.stringify(afterScrolledUpLines);
  console.log('Transcript text shifted in alternate screen buffer:', isDifferent);

  process.exit(0);
}

run().catch(e => {
  console.error('Error:', e);
  process.exit(1);
});
