#!/usr/bin/env node
// LuCI integration test against a real OpenWrt LuCI (uhttpd + rpcd) running in
// a container (see tests/openwrt/run.sh --luci). Logs in to LuCI, opens
// Status -> BandWatch, and checks that the embedded dashboard is signed in
// through the rpcd single sign-on, and that "Open in a new tab" works.
//
//   LUCI_URL=http://127.0.0.1:18080 LUCI_PASSWORD=... node tests/openwrt/luci-e2e.js

const playwright = require(require.resolve('playwright', { paths: [require('path').join(__dirname, '..', 'frontend'), process.cwd()] }));

const LUCI = process.env.LUCI_URL || 'http://127.0.0.1:18080';
const PASSWORD = process.env.LUCI_PASSWORD || 'luci-test-pw';
let failures = 0;

function result(name, ok, detail = '') {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}${detail ? ` — ${detail}` : ''}`);
}

(async () => {
  const browser = await playwright.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  try {
    await page.goto(`${LUCI}/cgi-bin/luci/`);
    await page.fill('input[name="luci_password"]', PASSWORD);
    await page.click('button.cbi-button-positive, input[type="submit"], button[type="submit"]');
    await page.waitForLoadState('networkidle');
    result('LuCI login', !page.url().includes('sysauth') && (await page.$('input[name="luci_password"]')) === null, page.url());

    errors.length = 0; // only count errors raised by the BandWatch page itself
    await page.goto(`${LUCI}/cgi-bin/luci/admin/status/bandwatch`);
    await page.waitForSelector('iframe[title="BandWatch"]', { timeout: 20000 });
    result('Status -> BandWatch menu page renders', true);
    const menu = await page.$$eval('a', (as) => as.map((a) => a.textContent.trim()));
    result('BandWatch appears in the LuCI menu', menu.includes('BandWatch'));

    const frameEl = await page.$('iframe[title="BandWatch"]');
    const frame = await frameEl.contentFrame();
    await frame.waitForSelector('#main .card', { timeout: 20000 });
    const loginVisible = await frame.isVisible('#login-form').catch(() => false);
    result('Embedded dashboard is signed in via SSO (no password prompt)', !loginVisible);
    const title = await frame.textContent('#page-title');
    result('Embedded dashboard shows the Overview', /Overview/.test(title), title);

    const [popup] = await Promise.all([ctx.waitForEvent('page'), page.click('button:has-text("Open BandWatch in a new tab")')]);
    await popup.waitForSelector('#main .card', { timeout: 20000 });
    result('"Open in a new tab" signs in', !(await popup.isVisible('#login-form').catch(() => false)), popup.url().replace(/t=[^&]+/, 't=<token>'));

    // A replayed SSO URL must not grant a session.
    const replay = await browser.newContext();
    const rp = await replay.newPage();
    const used = popup.url();
    await rp.goto(used.includes('/auth/sso') ? used : `${used.split('#')[0]}auth/sso?t=${'A'.repeat(48)}`);
    await rp.waitForSelector('#login-form', { timeout: 15000 });
    result('Replayed / unknown SSO token is rejected', true);
    await replay.close();
    result('No JavaScript errors on the BandWatch LuCI page', errors.length === 0, errors.join(' | '));
  } catch (e) {
    result('LuCI end-to-end flow', false, String(e.message || e).split('\n')[0]);
  } finally {
    await browser.close();
  }
  console.log(failures ? `${failures} LuCI check(s) failed` : 'All LuCI checks passed');
  process.exit(failures ? 1 : 0);
})();
