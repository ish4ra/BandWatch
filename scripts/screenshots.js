#!/usr/bin/env node
// Regenerates the README screenshots from the real dashboard in demo mode
// (all values simulated).
//
//   node scripts/screenshots.js            (writes docs/assets/screenshots/*.png)
//   ONLY=health-dark,settings-report-dark WARMUP_S=60 node scripts/screenshots.js   (selected shots)
//
// Requires Playwright with Chromium. Starts its own demo server on a free
// loopback port with a temporary data directory and a fixed seed.

const playwright = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, 'docs', 'assets', 'screenshots');
const PORT = 18950;
const BASE = `http://127.0.0.1:${PORT}`;

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bw-shots-'));
  const demo = spawn('python3', ['-m', 'bandwatch', 'demo', '--port', String(PORT), '--data-dir', dir, '--days', '35', '--seed', '7'], { cwd: ROOT, stdio: 'ignore' });
  try {
    for (let i = 0; i < 60; i++) {
      try { if ((await fetch(`${BASE}/api/session`)).ok) break; } catch { /* starting */ }
      await new Promise((r) => setTimeout(r, 500));
    }
    // Let the live charts fill their 5-minute window and device names load.
    const warm = Number(process.env.WARMUP_S || 300);
    console.log(`warming up the demo for ${warm} s...`);
    await new Promise((r) => setTimeout(r, warm * 1000));
    const browser = await playwright.chromium.launch();
    const only = (process.env.ONLY || '').split(',').filter(Boolean);
    const shot = async (name, { theme = 'dark', view = 'overview', width = 1440, height = 900, scale = 1.5, wait = '#main .card', settle = 4000, mobile = false, scrollTo = null } = {}) => {
      if (only.length && !only.includes(name)) return;
      const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale, isMobile: mobile, hasTouch: mobile, colorScheme: theme });
      await ctx.addInitScript((t) => { try { localStorage.setItem('bw.theme', t); } catch { /* ignore */ } }, theme);
      const page = await ctx.newPage();
      await page.goto(`${BASE}/#/${view}`);
      await page.waitForSelector(wait, { timeout: 20000 });
      await page.waitForTimeout(settle); // let live charts fill and entry animations finish
      await page.evaluate(() => document.querySelectorAll('.toast').forEach((t) => t.remove()));
      if (scrollTo) await page.evaluate((sel) => { const e = document.querySelector(sel); if (e) e.scrollIntoView({ block: 'start' }); window.scrollBy(0, -90); }, scrollTo);
      await page.screenshot({ path: path.join(OUT, `${name}.png`) });
      await ctx.close();
      console.log(`wrote ${name}.png`);
    };
    await shot('overview-dark', { wait: '#k-today .big', settle: 5000 });
    await shot('devices-dark', { view: 'devices', wait: 'table.devices tbody tr' });
    await shot('aria2-dark', { view: 'aria2', wait: '#a-today-card' });
    await shot('history-dark', { view: 'history', wait: '#h-stats .stats' });
    await shot('health-dark', { view: 'health', wait: '#h-checks-body .check-counts' });
    await shot('settings-report-dark', { view: 'settings', wait: '#u-check', scrollTo: '#s-report', settle: 1500 });
    await shot('overview-light', { theme: 'light', wait: '#k-today .big', settle: 5000 });
    await shot('mobile-overview-dark', { width: 390, height: 844, scale: 2, mobile: true, wait: '#k-today .big', settle: 5000 });
    await browser.close();
  } finally {
    demo.kill('SIGTERM');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
