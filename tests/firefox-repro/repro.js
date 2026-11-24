#!/usr/bin/env node
// Focused Firefox stall reproduction for issue #2 (investigation tool, not a
// pass/fail gate). Each variant runs in a FRESH browser so a stall in one
// variant cannot leak into the next. Every page load is timed; a load that
// takes longer than STALL_MS is reported as a stall together with its length.
//
//   BROWSER=firefox node tests/firefox-repro/repro.js [variant ...]
//
// Variants isolate one factor each:
//   min-*  standalone stdlib server (tests/firefox-repro/server.py), no BandWatch code
//   bw-*   the BandWatch demo server, same navigation pattern as ui-test.js
// Factors: ordinary fetch in flight (always), SSE stream, service worker,
// HTTP keep-alive vs Connection: close, context close vs in-context navigation.

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');

let playwright;
try { playwright = require(path.join(__dirname, '..', 'frontend', 'node_modules', 'playwright')); } catch { playwright = require('playwright'); }

const BROWSER = process.env.BROWSER || 'firefox';
const ROOT = path.resolve(__dirname, '..', '..');
const ITER = Number(process.env.REPRO_ITER || 30);
const BUDGET_MS = Number(process.env.REPRO_BUDGET_MS || 300000);
const STALL_MS = 8000;
const STEP_TIMEOUT = Number(process.env.REPRO_STEP_TIMEOUT || 120000);

const VARIANTS = {
  'min-poll-close': { server: 'min', q: 'sse=0&sw=0', mode: 'close' },
  'min-sse-close': { server: 'min', q: 'sse=1&sw=0', mode: 'close' },
  'min-sw-sse-close': { server: 'min', q: 'sse=1&sw=1', mode: 'close' },
  'min-sse-navigate': { server: 'min', q: 'sse=1&sw=0', mode: 'navigate' },
  'min-sw-sse-navigate': { server: 'min', q: 'sse=1&sw=1', mode: 'navigate' },
  'min-sw-sse-navigate-connclose': { server: 'min-close', q: 'sse=1&sw=1', mode: 'navigate' },
  'bw-sw-allowed': { server: 'bw', mode: 'bw', serviceWorkers: 'allow' },
  'bw-sw-blocked': { server: 'bw', mode: 'bw', serviceWorkers: 'block' },
  // Mirror tests/frontend/ui-test.js exactly: five demo servers (20 days of
  // history on the main one), a context per screen size, request/console
  // listeners, overflow check, 900 ms dwell; finished contexts either kept
  // open after 'pagehide' (the beta.1 Firefox workaround) or really closed.
  'uitest-keepopen-sw': { server: 'bw5', mode: 'uitest', close: 'keep', serviceWorkers: 'allow' },
  'uitest-keepopen-nosw': { server: 'bw5', mode: 'uitest', close: 'keep', serviceWorkers: 'block' },
  'uitest-close-sw': { server: 'bw5', mode: 'uitest', close: 'real', serviceWorkers: 'allow' },
  'uitest-close-nosw': { server: 'bw5', mode: 'uitest', close: 'real', serviceWorkers: 'block' },
};
const SIZES = [[320, 640], [390, 844], [820, 1180], [1440, 900]];
const VIEWS = ['overview', 'devices', 'aria2', 'history', 'health', 'settings'];

function startProc(args, readyText, port) {
  const proc = spawn('python3', args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  proc.stdout.on('data', (d) => { log += d; });
  proc.stderr.on('data', (d) => { log += d; });
  return {
    proc,
    async ready() {
      for (let i = 0; i < 120; i++) {
        try { const r = await fetch(`http://127.0.0.1:${port}${readyText}`); if (r.ok) return; } catch { /* not up */ }
        await new Promise((r) => setTimeout(r, 250));
      }
      throw new Error(`server ${port} did not start: ${log}`);
    },
    stop() { proc.kill('SIGTERM'); },
  };
}

// When a load stalls, ask the page itself what state it is in. If the
// document reports readyState "complete" while Playwright still waits for
// "load", the browser finished and the automation layer missed the event;
// if it is still "loading", the listed resources show what arrived.
async function probe(page) {
  if (!page) return null;
  const evalP = page.evaluate(() => {
    const res = performance.getEntriesByType('resource').map((e) => `${new URL(e.name).pathname} ${Math.round(e.startTime)}-${Math.round(e.responseEnd)}ms`);
    const nav = performance.getEntriesByType('navigation')[0];
    return {
      href: location.href,
      readyState: document.readyState,
      swController: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
      navigation: nav ? { type: nav.type, responseEnd: Math.round(nav.responseEnd), domComplete: Math.round(nav.domComplete), loadEventEnd: Math.round(nav.loadEventEnd) } : null,
      resources: res.slice(-12),
      mainCards: document.querySelectorAll('#main .card').length,
      sinceOrigin: Math.round(performance.now()),
    };
  });
  const timeout = new Promise((r) => setTimeout(() => r('page.evaluate did not answer within 5 s'), 5000));
  return Promise.race([evalP, timeout]).catch((e) => `page.evaluate failed: ${String(e.message || e).split('\n')[0]}`).then((r) => ({ playwrightUrl: page.url(), page: r }));
}

async function timed(fn, page) {
  const t0 = Date.now();
  let err = null;
  try { await fn(); } catch (e) { err = String(e.message || e).split('\n')[0]; }
  const ms = Date.now() - t0;
  return { ms, err, probe: err || ms > STALL_MS ? await probe(page) : undefined };
}

async function runVariant(name, v, port) {
  let server;
  let extra = [];
  if (v.server === 'bw5') {
    const mk = (p, args) => startProc(['-m', 'bandwatch', 'demo', '--port', String(p), '--data-dir', fs.mkdtempSync(path.join(os.tmpdir(), 'bw-repro-')), '--days', '20', ...args], '/api/session', p);
    server = mk(port, []);
    extra = [mk(port + 100, ['--auth', 'password', '--password', 'x-test-password']), mk(port + 200, ['--scenario', 'no-bandix', '--days', '0']), mk(port + 300, ['--scenario', 'no-aria2', '--days', '0']), mk(port + 400, ['--scenario', 'hostile-names', '--days', '2'])];
    await Promise.all(extra.map((e) => e.ready()));
  } else if (v.server === 'bw') {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bw-repro-'));
    server = startProc(['-m', 'bandwatch', 'demo', '--port', String(port), '--data-dir', dir, '--days', '3'], '/api/session', port);
  } else {
    server = startProc(['tests/firefox-repro/server.py', '--port', String(port), ...(v.server === 'min-close' ? ['--close'] : [])], '/', port);
  }
  await server.ready();
  const browser = await playwright[BROWSER].launch();
  const base = `http://127.0.0.1:${port}`;
  const loads = [];
  const failed = [];
  const started = Date.now();
  const onFail = (r) => failed.push(`${(r.failure() || {}).errorText} ${new URL(r.url()).pathname}`);
  try {
    if (v.mode === 'close') {
      for (let i = 0; i < ITER && Date.now() - started < BUDGET_MS; i++) {
        const ctx = await browser.newContext();
        ctx.on('requestfailed', onFail);
        const page = await ctx.newPage();
        loads.push(await timed(async () => {
          await page.goto(`${base}/?${v.q}&i=${i}`, { timeout: STEP_TIMEOUT });
          await page.waitForSelector('body[data-ready]', { timeout: STEP_TIMEOUT });
        }, page));
        await page.waitForTimeout(400);
        await ctx.close(); // requests (and the stream) are still in flight
      }
    } else if (v.mode === 'navigate') {
      const ctx = await browser.newContext();
      ctx.on('requestfailed', onFail);
      const page = await ctx.newPage();
      for (let i = 0; i < ITER && Date.now() - started < BUDGET_MS; i++) {
        loads.push(await timed(async () => {
          await page.goto(`${base}/?${v.q}&i=${i}`, { timeout: STEP_TIMEOUT });
          await page.waitForSelector('body[data-ready]', { timeout: STEP_TIMEOUT });
        }, page));
        await page.waitForTimeout(400);
      }
      await ctx.close();
    } else if (v.mode === 'uitest') {
      for (let round = 0; loads.length < ITER && Date.now() - started < BUDGET_MS; round++) {
        for (const [w, h] of SIZES) {
          const ctx = await browser.newContext({ viewport: { width: w, height: h }, serviceWorkers: v.serviceWorkers });
          for (const ev of ['request', 'response', 'requestfinished']) ctx.on(ev, () => {});
          ctx.on('requestfailed', onFail);
          const page = await ctx.newPage();
          page.on('console', () => {});
          page.on('pageerror', () => {});
          for (const view of VIEWS) {
            loads.push(await timed(async () => {
              await page.goto(`${base}/#/${view}`, { timeout: STEP_TIMEOUT });
              await page.waitForSelector('#main .card', { timeout: STEP_TIMEOUT });
            }, page));
            await page.waitForTimeout(900);
            await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth).catch(() => {});
          }
          if (v.close === 'keep') {
            await page.evaluate(() => window.dispatchEvent(new Event('pagehide'))).catch(() => {});
            await new Promise((r) => setTimeout(r, 1500));
          } else {
            await page.goto('about:blank', { timeout: 5000 }).catch(() => {});
            await ctx.close();
          }
          if (loads.length >= ITER || Date.now() - started >= BUDGET_MS) break;
        }
      }
    } else {
      // BandWatch: the ui-test.js pattern - a context per screen size, six
      // views by URL, then a real context close.
      const views = ['overview', 'devices', 'aria2', 'history', 'health', 'settings'];
      for (let round = 0; round * views.length < ITER && Date.now() - started < BUDGET_MS; round++) {
        const ctx = await browser.newContext({ serviceWorkers: v.serviceWorkers });
        ctx.on('requestfailed', onFail);
        const page = await ctx.newPage();
        for (const view of views) {
          loads.push(await timed(async () => {
            await page.goto(`${base}/#/${view}`, { timeout: STEP_TIMEOUT });
            await page.waitForSelector('#main .card', { timeout: STEP_TIMEOUT });
          }, page));
          await page.waitForTimeout(600);
        }
        await ctx.close();
      }
    }
  } finally {
    await browser.close().catch(() => {});
    server.stop();
    extra.forEach((e) => e.stop());
  }
  const stalls = loads.filter((l) => l.ms > STALL_MS || l.err);
  const sorted = loads.map((l) => l.ms).sort((a, b) => a - b);
  return {
    variant: name,
    loads: loads.length,
    stalls: stalls.length,
    stall_ms: stalls.map((s) => s.ms),
    errors: [...new Set(stalls.map((s) => s.err).filter(Boolean))].slice(0, 3),
    probes: stalls.map((s) => s.probe).filter(Boolean).slice(0, 4),
    median_ms: sorted.length ? sorted[Math.floor(sorted.length / 2)] : null,
    max_ms: sorted.length ? sorted[sorted.length - 1] : null,
    failed_request_kinds: [...new Set(failed)].slice(0, 6),
  };
}

(async () => {
  const fromEnv = (process.env.REPRO_VARIANTS || '').split(/[ ,]+/).filter(Boolean);
  const names = process.argv.slice(2).length ? process.argv.slice(2) : fromEnv.length ? fromEnv : Object.keys(VARIANTS);
  const browser = await playwright[BROWSER].launch();
  console.log(`Browser: ${BROWSER} ${browser.version()}  iterations/variant: ${ITER}  stall threshold: ${STALL_MS} ms`);
  await browser.close();
  const out = [];
  let port = 18950; // bw5 variants also use port+100 .. port+400
  for (const name of names) {
    const v = VARIANTS[name];
    if (!v) throw new Error(`unknown variant ${name}`);
    const r = await runVariant(name, v, port++);
    out.push(r);
    console.log(`${r.stalls ? 'STALL' : 'clean'}  ${name.padEnd(32)} loads=${r.loads} stalls=${r.stalls} median=${r.median_ms}ms max=${r.max_ms}ms ${r.stall_ms.length ? `stall_ms=[${r.stall_ms.join(',')}]` : ''}`);
    if (r.errors.length) console.log(`       errors: ${r.errors.join(' | ')}`);
    if (r.failed_request_kinds.length) console.log(`       aborted requests seen: ${r.failed_request_kinds.join(' | ')}`);
    for (const p of r.probes) console.log(`       stalled page: ${JSON.stringify(p)}`);
  }
  const file = process.env.REPRO_OUT || path.join(os.tmpdir(), `firefox-repro-${BROWSER}.json`);
  fs.writeFileSync(file, JSON.stringify({ browser: BROWSER, iterations: ITER, results: out }, null, 1));
  console.log(`results: ${file}`);
  if (process.env.GITHUB_STEP_SUMMARY) {
    const rows = out.map((r) => `| ${r.variant} | ${r.loads} | ${r.stalls} | ${r.median_ms} | ${r.max_ms} | ${r.stall_ms.join(', ')} |`).join('\n');
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `### ${BROWSER} stall reproduction\n\n| variant | loads | stalls | median ms | max ms | stall durations ms |\n|---|---|---|---|---|---|\n${rows}\n`);
  }
})().catch((e) => { console.error(e); process.exit(1); });
