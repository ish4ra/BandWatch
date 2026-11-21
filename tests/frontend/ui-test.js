#!/usr/bin/env node
// End-to-end dashboard checks against the demo server (Playwright).
//
//   node tests/frontend/ui-test.js                     (Chromium, full suite)
//   BROWSER=firefox node tests/frontend/ui-test.js     (core checks)
//   BROWSER=webkit  node tests/frontend/ui-test.js     (core checks)
//
// Checks: every view renders without console errors or CSP violations, no
// horizontal overflow at phone/tablet/desktop widths, navigation and
// keyboard focus, theme switching, settings validation and save, alias
// editing, CSV download, login flow with password auth, reduced motion,
// empty states (no Bandix / no aria2) and the LuCI view file syntax.

const playwright = require('playwright');

const BROWSER = process.env.BROWSER || 'chromium';
const FULL = BROWSER === 'chromium';
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const results = [];
let failures = 0;

function record(name, ok, detail = '') {
  results.push({ name, ok, detail });
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}${detail ? ` — ${detail}` : ''}`);
}

const DIAG = [];

async function check(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (e) {
    record(name, false, String(e && e.message ? e.message : e).split('\n')[0]);
    // Diagnostics: is the main demo server still answering the API?
    for (const port of [18901]) {
      const t0 = Date.now();
      try {
        const r = await fetch(`http://127.0.0.1:${port}/api/session`, { signal: AbortSignal.timeout(5000) });
        DIAG.push(`after "${name}": :${port}/api/session -> ${r.status} in ${Date.now() - t0} ms`);
      } catch (err) {
        DIAG.push(`after "${name}": :${port}/api/session -> ${err.name} after ${Date.now() - t0} ms`);
      }
    }
  }
}

function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

function startDemo(port, args = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bw-ui-'));
  const proc = spawn('python3', ['-m', 'bandwatch', 'demo', '--port', String(port), '--data-dir', dir, '--days', '20', ...args], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  proc.stdout.on('data', (d) => { log += d; });
  proc.stderr.on('data', (d) => { log += d; });
  return {
    proc,
    log: () => log,
    ready: async () => {
      for (let i = 0; i < 80; i++) {
        try {
          const r = await fetch(`http://127.0.0.1:${port}/api/session`);
          if (r.ok) return;
        } catch { /* not up yet */ }
        await new Promise((r) => setTimeout(r, 250));
      }
      throw new Error(`demo server on ${port} did not start:\n${log}`);
    },
    stop: () => proc.kill('SIGTERM'),
  };
}

const VIEWS = ['overview', 'devices', 'aria2', 'history', 'health', 'settings'];
const SIZES = [[320, 640, 'small-phone'], [390, 844, 'phone'], [820, 1180, 'tablet'], [1440, 900, 'desktop']];

async function watchErrors(page) {
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));
  return errors;
}

(async () => {
  const servers = [];
  const main = startDemo(18901);
  const pw = startDemo(18902, ['--auth', 'password', '--password', 'ui-test-password', '--listen', '127.0.0.1']);
  const nob = startDemo(18903, ['--scenario', 'no-bandix', '--days', '0']);
  const noa = startDemo(18904, ['--scenario', 'no-aria2', '--days', '0']);
  const hostile = startDemo(18905, ['--scenario', 'hostile-names', '--days', '2']);
  servers.push(main, pw, nob, noa, hostile);
  const browser = await playwright[BROWSER].launch();
  console.log(`Browser: ${BROWSER} ${browser.version()}`);
  try {
    await Promise.all(servers.map((s) => s.ready()));
    const base = 'http://127.0.0.1:18901';

    for (const [w, h, label] of SIZES) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      const page = await ctx.newPage();
      const errors = await watchErrors(page);
      for (const v of VIEWS) {
        await check(`${label}: ${v} renders without overflow or errors`, async () => {
          await page.goto(`${base}/#/${v}`);
          await page.waitForSelector('#main .card', { timeout: 10000 });
          await page.waitForTimeout(900);
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          assert(overflow <= 1, `horizontal overflow ${overflow}px`);
          assert(!(await page.$('#main .error-card')), 'error card shown');
          assert(errors.length === 0, `console errors: ${errors.join(' | ')}`);
          const title = await page.textContent('#page-title');
          assert(title && title.length > 0, 'no page title');
        });
      }
      await ctx.close();
    }

    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
    const page = await ctx.newPage();
    const errors = await watchErrors(page);

    await check('demo data is labelled as simulated', async () => {
      await page.goto(`${base}/#/overview`);
      await page.waitForSelector('#k-today .big');
      assert(await page.isVisible('#demo-banner'), 'demo banner hidden');
      assert((await page.textContent('#demo-banner')).includes('simulated'));
    });

    await check('live chart and KPIs update from the stream', async () => {
      await page.waitForFunction(() => (document.querySelector('#k-down')?.textContent || '—').indexOf('—') < 0, null, { timeout: 10000 });
      const pill = await page.getAttribute('#live-pill', 'class');
      assert(pill.includes('live'), `live pill: ${pill}`);
    });

    if (FULL) await check('keyboard navigation reaches sidebar links', async () => {
      await page.keyboard.press('Tab');
      await page.keyboard.press('Tab');
      const tag = await page.evaluate(() => document.activeElement && (document.activeElement.getAttribute('href') || document.activeElement.className));
      assert(tag, 'nothing focused');
    });

    await check('theme toggle cycles dark -> light -> system', async () => {
      await page.emulateMedia({ colorScheme: 'dark' });
      const t0 = await page.getAttribute('html', 'data-theme-choice');
      await page.click('#theme-btn');
      const t1 = await page.getAttribute('html', 'data-theme-choice');
      await page.click('#theme-btn');
      const t2 = await page.getAttribute('html', 'data-theme-choice');
      assert(t0 === 'dark' && t1 === 'light' && t2 === 'system', `${t0},${t1},${t2}`);
      assert((await page.getAttribute('html', 'data-theme')) === 'dark', 'system theme should follow emulated dark scheme');
      await page.click('#theme-btn');
    });

    await check('devices: search, filter and open detail', async () => {
      await page.goto(`${base}/#/devices`);
      await page.waitForSelector('table.devices tbody tr');
      const before = await page.$$eval('table.devices tbody tr', (r) => r.length);
      await page.fill('#dev-q', 'camera');
      const after = await page.$$eval('table.devices tbody tr', (r) => r.length);
      assert(after >= 1 && after < before, `rows ${before} -> ${after}`);
      await page.click('table.devices tbody tr');
      await page.waitForSelector('#dd-name');
      assert((await page.textContent('#dd-name')).length > 0);
    });

    await check('device alias: XSS payload rejected, valid alias saved', async () => {
      await page.fill('#alias', '<img src=x onerror=alert(1)>');
      await page.click('#alias-form button');
      await page.waitForSelector('.toast.error');
      await page.fill('#alias', 'Front door camera');
      await page.click('#alias-form button');
      await page.waitForFunction(() => (document.querySelector('#dd-name')?.textContent || '') === 'Front door camera', null, { timeout: 8000 });
      assert(!(await page.$('img[src="x"]')), 'payload rendered');
    });

    await check('history: periods, custom range and CSV export', async () => {
      await page.goto(`${base}/#/history`);
      await page.waitForSelector('#h-stats .stats');
      for (const p of ['yesterday', 'this_week', 'last_month', 'this_year']) {
        await page.click(`.seg[data-p="${p}"]`);
        await page.waitForTimeout(400);
      }
      await page.click('.seg[data-p="custom"]');
      await page.fill('input[name="start"]', '2026-01-01');
      await page.fill('input[name="end"]', '2026-01-05');
      await page.click('.custom-range button');
      await page.waitForTimeout(500);
      const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#h-csv')]);
      const file = await dl.path();
      const text = fs.readFileSync(file, 'utf8');
      assert(text.includes('download_bytes') && text.includes('simulated=yes'), 'unexpected CSV');
      await page.click('.seg[data-p="today"]');
    });

    await check('history: vnStat comparison on demand', async () => {
      await page.click('#vn-btn');
      await page.waitForSelector('#vn-body table', { timeout: 10000 });
    });

    await check('settings: invalid value rejected, valid value saved', async () => {
      await page.goto(`${base}/#/settings`);
      await page.waitForSelector('#settings');
      await page.fill('[id="f-wan.sample_interval"]', '99');
      await page.click('#settings button[type="submit"]');
      await page.waitForSelector('.toast.error');
      await page.fill('[id="f-wan.sample_interval"]', '3');
      await page.selectOption('[id="f-ui.rate_unit"]', 'bytes');
      await page.click('#settings button[type="submit"]');
      await page.waitForFunction(() => (document.querySelector('#save-msg')?.textContent || '').startsWith('Saved'), null, { timeout: 8000 });
      await page.goto(`${base}/#/overview`);
      await page.waitForFunction(() => /B\/s/.test((document.querySelector('#k-down')?.textContent || '')), null, { timeout: 10000 });
    });

    await check('aria2 secret is write-only', async () => {
      await page.goto(`${base}/#/settings`);
      await page.waitForSelector('#a-secret');
      await page.fill('#a-secret', 'very-secret-token');
      // Sticky save bar / toasts may overlap the button at the viewport edge.
      await page.$eval('#a-secret-save', (b) => b.click());
      await page.waitForSelector('#a-secret-clear', { timeout: 8000 });
      const html = await page.content();
      assert(!html.includes('very-secret-token'), 'secret visible in page');
      const api = await page.evaluate(async () => (await fetch('/api/settings')).text());
      assert(!api.includes('very-secret-token'), 'secret returned by API');
      await page.$eval('#a-secret-clear', (b) => b.click());
      await page.waitForFunction(() => !document.querySelector('#a-secret-clear'), null, { timeout: 8000 });
    });

    await check('no console errors during interaction', async () => {
      // 4xx responses from the deliberate invalid inputs above are expected.
      const real = errors.filter((e) => !/status of 4\d\d/.test(e));
      assert(real.length === 0, real.join(' | '));
    });

    await check('reduced motion disables animations', async () => {
      const c2 = await browser.newContext({ reducedMotion: 'reduce' });
      const p2 = await c2.newPage();
      await p2.goto(`${base}/#/overview`);
      await p2.waitForSelector('#main .card');
      const dur = await p2.$eval('#main .card', (el) => getComputedStyle(el).animationDuration);
      await c2.close();
      assert(parseFloat(dur) < 0.01, `animation-duration ${dur}`);
    });

    await check('password mode: login required, wrong password rejected, then signed in', async () => {
      const c3 = await browser.newContext();
      const p3 = await c3.newPage();
      await p3.goto('http://127.0.0.1:18902/');
      await p3.waitForSelector('#login-form');
      await p3.fill('#login-pw', 'wrong-password');
      await p3.click('#login-form button');
      await p3.waitForFunction(() => (document.querySelector('#login-err')?.textContent || '').length > 0);
      await p3.fill('#login-pw', 'ui-test-password');
      await p3.click('#login-form button');
      await p3.waitForSelector('#main .card', { timeout: 10000 });
      assert(await p3.isHidden('#login'), 'login still visible');
      await p3.click('#menu-btn');
      await p3.waitForSelector('#login-form');
      await c3.close();
    });

    await check('no-bandix: device view explains limitation, WAN still live', async () => {
      const c4 = await browser.newContext();
      const p4 = await c4.newPage();
      await p4.goto('http://127.0.0.1:18903/#/devices');
      await p4.waitForSelector('.banner-inline', { timeout: 10000 });
      const t = await p4.textContent('.banner-inline');
      assert(/unavailable|provider/i.test(t), t);
      await p4.goto('http://127.0.0.1:18903/#/overview');
      await p4.waitForFunction(() => (document.querySelector('#k-down')?.textContent || '—').indexOf('—') < 0, null, { timeout: 10000 });
      await c4.close();
    });

    await check('no-aria2: aria2 view shows an empty state', async () => {
      const c5 = await browser.newContext();
      const p5 = await c5.newPage();
      await p5.goto('http://127.0.0.1:18904/#/aria2');
      await p5.waitForSelector('.empty', { timeout: 10000 });
      assert((await p5.textContent('.empty h3')).includes('aria2'));
      await c5.close();
    });

    await check('XSS: hostile device and download names render as text', async () => {
      const c6 = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      const p6 = await c6.newPage();
      let dialogs = 0;
      p6.on('dialog', async (d) => { dialogs++; await d.dismiss(); });
      for (const v of ['overview', 'devices', 'aria2']) {
        await p6.goto(`http://127.0.0.1:18905/#/${v}`);
        await p6.waitForSelector('#main .card', { timeout: 10000 });
        await p6.waitForTimeout(1800);
      }
      await p6.goto('http://127.0.0.1:18905/#/devices');
      await p6.waitForSelector('table.devices tbody tr');
      const injected = await p6.$$('main img, main svg[onload], main script');
      const text = await p6.textContent('#main');
      await c6.close();
      assert(dialogs === 0, `${dialogs} script dialog(s) executed`);
      assert(injected.length === 0, 'payload elements were created');
      assert(text.includes('<img/src=x/onerror=alert(4)>') || text.includes('<script>alert(3)</script>'), 'payload text not shown literally');
    });

    await check('light theme uses the light palette', async () => {
      const c7 = await browser.newContext({ colorScheme: 'light' });
      const p7 = await c7.newPage();
      await p7.addInitScript(() => localStorage.setItem('bw.theme', 'light'));
      await p7.goto(`${base}/#/overview`);
      await p7.waitForSelector('#main .card');
      const bg = await p7.evaluate(() => getComputedStyle(document.body).backgroundColor);
      const theme = await p7.getAttribute('html', 'data-theme');
      await c7.close();
      assert(theme === 'light' && bg === 'rgb(244, 247, 251)', `${theme} ${bg}`);
    });

    await check('aria2 page shows Today and This month cards', async () => {
      const c8 = await browser.newContext();
      const p8 = await c8.newPage();
      await p8.goto(`${base}/#/aria2`);
      await p8.waitForSelector('#a-today .big', { timeout: 10000 });
      const t = await p8.textContent('#a-today-card');
      const m = await p8.textContent('#a-month-card');
      await c8.close();
      assert(/today/i.test(t) && /month/i.test(m), `${t} | ${m}`);
    });

    await check('health page shows no host storage or paths in demo mode', async () => {
      const c9 = await browser.newContext();
      const p9 = await c9.newPage();
      await p9.goto(`${base}/#/health`);
      await p9.waitForSelector('#h-dbsize', { timeout: 10000 });
      const t = await p9.textContent('#main');
      await c9.close();
      assert(!t.includes('Filesystem holding BandWatch data'), 'host data filesystem listed');
      assert(!t.includes('bw-ui-'), 'host demo data path shown');
      assert(t.includes('BandWatch database'), 'database size line missing');
      assert(!t.includes('<+0530>'), 'raw POSIX timezone shown');
    });

    if (FULL) await check('device page: custom date range', async () => {
      const c10 = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      const p10 = await c10.newPage();
      await p10.goto(`${base}/#/devices`);
      await p10.waitForSelector('table.devices tbody tr');
      await p10.click('table.devices tbody tr');
      await p10.waitForSelector('#dd-stats .stats', { timeout: 10000 });
      await p10.click('#dd-period .seg[data-p="custom"]');
      const end = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
      const start = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
      await p10.fill('#dd-period input[name="start"]', start);
      await p10.fill('#dd-period input[name="end"]', end);
      await p10.click('#dd-period .custom-range button');
      await p10.waitForTimeout(1500);
      const range = await p10.textContent('#dd-range');
      const total = await p10.textContent('#dd-stats');
      await p10.click('#dd-period .seg[data-p="today"]');
      await c10.close();
      assert(range.includes('–'), `range label: ${range}`);
      assert(/GB|MB|kB/.test(total), 'no totals for custom range');
    });

    await check('LuCI view file parses', async () => {
      const src = fs.readFileSync(path.join(ROOT, 'openwrt/luci-app-bandwatch/htdocs/luci-static/resources/view/bandwatch/dashboard.js'), 'utf8');
      // LuCI evaluates view files as a function body.
      // eslint-disable-next-line no-new-func
      new Function('view', 'rpc', 'ui', 'E', '_', src);
    });

    await check('static assets are small enough for routers', async () => {
      const dir = path.join(ROOT, 'bandwatch/web');
      let total = 0;
      const walk = (d) => fs.readdirSync(d).forEach((f) => { const p = path.join(d, f); const s = fs.statSync(p); if (s.isDirectory()) walk(p); else total += s.size; });
      walk(dir);
      assert(total < 300 * 1024, `web assets ${Math.round(total / 1024)} KiB`);
    });
  } finally {
    await browser.close();
    servers.forEach((s) => s.stop());
  }
  if (failures) {
    console.log('\n--- diagnostics ---');
    DIAG.forEach((d) => console.log(d));
    servers.forEach((s, i) => console.log(`--- demo server ${i} log (tail) ---\n${s.log().split('\n').slice(-40).join('\n')}`));
  }
  console.log(`\n${results.length - failures}/${results.length} frontend checks passed`);
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
