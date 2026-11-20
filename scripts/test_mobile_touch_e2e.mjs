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
const DASHBOARD_URL = process.env.DASHBOARD_URL || 'http://localhost:9119/chat';
const OUTPUT_DIR = process.env.OUTPUT_DIR || path.join(os.homedir(), '.hermes', 'images');

async function run() {
  console.log(`Connecting to CDP on ${CDP_URL}...`);
  const browser = await chromium.connectOverCDP(CDP_URL);
  const context = browser.contexts()[0];

  console.log('Finding or creating chat tab...');
  let page = context.pages().find(p => p.url().includes('/chat'));
  if (!page) {
    page = await context.newPage();
  }

  // Create CDP session to disable cache and send raw touch events
  const client = await page.context().newCDPSession(page);
  await client.send('Network.setCacheDisabled', { cacheDisabled: true });

  console.log(`Navigating to ${DASHBOARD_URL} with cache disabled...`);
  await page.goto(DASHBOARD_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

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

  console.log('Waiting for xterm screen...');
  const xtermScreen = page.locator('.xterm-screen');
  await xtermScreen.waitFor({ state: 'visible', timeout: 10000 });
  await page.waitForTimeout(1500);

  const box = await xtermScreen.boundingBox();
  console.log('xterm bounding box:', box);

  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }
  const beforeImg = path.join(OUTPUT_DIR, 'mobile_touch_before.png');
  await page.screenshot({ path: beforeImg });
  console.log('Captured baseline screenshot to:', beforeImg);

  const startX = Math.round(box.x + box.width / 2);
  const startY = Math.round(box.y + box.height * 0.7);
  const endY = Math.round(box.y + box.height * 0.3);

  console.log(`Executing touch swipe UP from (${startX}, ${startY}) to (${startX}, ${endY})...`);

  // Dispatch Touch Start
  await client.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: startX, y: startY, id: 1 }]
  });
  await page.waitForTimeout(16);

  // Intermediate Touch Move points
  const steps = 12;
  for (let i = 1; i <= steps; i++) {
    const curY = Math.round(startY + ((endY - startY) * i) / steps);
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: startX, y: curY, id: 1 }]
    });
    await page.waitForTimeout(16);
  }

  // Dispatch Touch End
  await client.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
  });

  // Wait for rAF kinetic inertia and PTY redraw
  await page.waitForTimeout(600);

  const afterUpImg = path.join(OUTPUT_DIR, 'mobile_touch_after_swipe_up.png');
  await page.screenshot({ path: afterUpImg });
  console.log('Captured after-swipe-up screenshot to:', afterUpImg);

  // Now execute swipe DOWN (reveal older history above)
  const downStartY = Math.round(box.y + box.height * 0.3);
  const downEndY = Math.round(box.y + box.height * 0.8);
  console.log(`Executing touch swipe DOWN from (${startX}, ${downStartY}) to (${startX}, ${downEndY})...`);

  await client.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: startX, y: downStartY, id: 2 }]
  });
  await page.waitForTimeout(16);

  for (let i = 1; i <= steps; i++) {
    const curY = Math.round(downStartY + ((downEndY - downStartY) * i) / steps);
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: startX, y: curY, id: 2 }]
    });
    await page.waitForTimeout(16);
  }

  await client.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
  });

  await page.waitForTimeout(600);

  const afterDownImg = path.join(OUTPUT_DIR, 'mobile_touch_after_swipe_down.png');
  await page.screenshot({ path: afterDownImg });
  console.log('Captured after-swipe-down screenshot to:', afterDownImg);

  console.log('Touch gesture test completed successfully!');
  process.exit(0);
}

run().catch(e => {
  console.error('Test failed:', e);
  process.exit(1);
});
