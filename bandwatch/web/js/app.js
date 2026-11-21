// BandWatch dashboard (vanilla JS, no external dependencies).
import { BarChart, LiveChart, sparkline } from './charts.js';
import { ago, bytes, bytesParts, clock, dateTime, duration, esc, percent, prefs, rate, rateParts } from './format.js';
import { icons, logo } from './icons.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  session: null,
  csrf: null,
  view: null,
  cleanup: [],
  live: { points: [], aria2: [], last: null },
  liveListeners: new Set(),
  stream: null,
  pollTimer: null,
  period: sessionStorageGet('bw.period') || 'today',
  custom: { start: '', end: '' },
  settings: null,
};

function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } }
function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

// ---------------------------------------------------------------------------
// API

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (state.csrf) headers['X-BandWatch-CSRF'] = state.csrf;
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store' });
  } catch (e) {
    throw new ApiError(0, 'BandWatch is not reachable. Check that the service is running.');
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && path !== '/api/login') {
    showLogin();
    throw new ApiError(401, 'Sign-in required');
  }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || `Request failed (${res.status})`);
  return data;
}

function download(path) {
  const a = document.createElement('a');
  a.href = path;
  a.rel = 'noopener';
  // Explicit download: Safari/WebKit otherwise navigates to text/csv responses.
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------------------
// Theme and preferences

function applyTheme(choice) {
  const mode = choice || localGet('bw.theme') || (state.session && state.session.theme) || 'dark';
  const dark = mode === 'system' ? matchMedia('(prefers-color-scheme: dark)').matches : mode === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeChoice = mode;
  const meta = $('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#0B1019' : '#F4F7FB';
  const btn = $('#theme-btn');
  if (btn) {
    btn.innerHTML = mode === 'dark' ? icons.moon : mode === 'light' ? icons.sun : icons.auto;
    btn.setAttribute('aria-label', `Theme: ${mode}. Activate to change.`);
    btn.title = `Theme: ${mode}`;
  }
  redrawCharts();
}

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme());

function cycleTheme() {
  const order = ['dark', 'light', 'system'];
  const cur = document.documentElement.dataset.themeChoice || 'dark';
  const next = order[(order.indexOf(cur) + 1) % order.length];
  localSet('bw.theme', next);
  applyTheme(next);
}

const charts = new Set();
function track(chart) { charts.add(chart); state.cleanup.push(() => { chart.destroy(); charts.delete(chart); }); return chart; }
function redrawCharts() { charts.forEach((c) => c.draw()); }

function applyPrefs(settings) {
  if (!settings) return;
  prefs.rateUnit = settings['ui.rate_unit'] || 'bits';
  prefs.volumeBase = settings['ui.volume_base'] || 1000;
}

// ---------------------------------------------------------------------------
// Toasts and small UI helpers

function toast(message, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `${kind === 'error' ? icons.alert : icons.check}<span>${esc(message)}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 4200);
}

function statePill(st) {
  const map = { ok: ['ok', 'Working'], degraded: ['warn', 'Limited'], unavailable: ['off', 'Unavailable'], error: ['err', 'Error'], disabled: ['off', 'Disabled'], starting: ['off', 'Starting'] };
  const [cls, label] = map[st] || ['off', st];
  return `<span class="pill ${cls}"><i></i>${label}</span>`;
}

function bigValue(parts, cls = '') {
  return `<span class="big ${cls}">${esc(parts.num)}<small>${esc(parts.unit)}</small></span>`;
}

function coverageChip(cov, label = 'measured') {
  if (cov === null || cov === undefined) return '<span class="chip muted" title="No measurements yet for this period">no data yet</span>';
  const pct = Math.round(cov * 100);
  const cls = pct >= 98 ? 'ok' : pct >= 80 ? 'warn' : 'err';
  return `<span class="chip ${cls}" title="Share of the elapsed time in this period that BandWatch was able to measure">${pct}% ${label}</span>`;
}

function emptyState(icon, title, text, extra = '') {
  return `<div class="empty">${icons[icon] || icons.info}<h3>${esc(title)}</h3><p>${text}</p>${extra}</div>`;
}

function skeleton(rows = 3) {
  return `<div class="skeleton-wrap" aria-busy="true" aria-label="Loading">${'<div class="skeleton"></div>'.repeat(rows)}</div>`;
}

function errorCard(err, retry) {
  const id = `retry-${Math.random().toString(36).slice(2)}`;
  setTimeout(() => { const b = document.getElementById(id); if (b) b.onclick = retry; });
  return `<div class="card error-card">${icons.alert}<div><h3>Could not load data</h3><p>${esc(err.message)}</p></div><button class="btn" id="${id}">Retry</button></div>`;
}

function every(ms, fn) {
  const id = setInterval(() => { if (!document.hidden) fn(); }, ms);
  state.cleanup.push(() => clearInterval(id));
}

function connIcon(c) { return icons[c === 'wifi' ? 'wifi' : c === 'ethernet' ? 'ethernet' : 'unknown']; }

function statusBadge(s) {
  const map = { online: ['ok', 'Online'], idle: ['warn', 'Idle'], offline: ['off', 'Offline'], unknown: ['off', 'Unknown'] };
  const [cls, label] = map[s] || map.unknown;
  return `<span class="pill ${cls}"><i></i>${label}</span>`;
}

// ---------------------------------------------------------------------------
// Period selector (shared by Devices, aria2, History)

const PERIODS = [
  ['today', 'Today'], ['yesterday', 'Yesterday'], ['this_week', 'This week'], ['last_week', 'Last week'],
  ['this_month', 'This month'], ['last_month', 'Last month'], ['this_year', 'This year'], ['custom', 'Custom'],
];

function periodQuery() {
  const p = state.period;
  if (p === 'custom') return `period=custom&start=${encodeURIComponent(state.custom.start)}&end=${encodeURIComponent(state.custom.end)}`;
  return `period=${p}`;
}

function periodPicker(onChange, compact = false) {
  const wrap = document.createElement('div');
  wrap.className = 'period';
  const tabs = PERIODS.map(([k, l]) => `<button type="button" role="tab" class="seg ${state.period === k ? 'on' : ''}" data-p="${k}" aria-selected="${state.period === k}">${l}</button>`).join('');
  const today = new Date().toISOString().slice(0, 10);
  wrap.innerHTML = `${compact ? `<label class="sr-only" for="period-sel">Period</label><select id="period-sel" class="select">${PERIODS.map(([k, l]) => `<option value="${k}" ${state.period === k ? 'selected' : ''}>${l}</option>`).join('')}</select>` : `<div class="segmented" role="tablist" aria-label="Period">${tabs}</div>`}
    <form class="custom-range" ${state.period === 'custom' ? '' : 'hidden'}>
      <label>From <input type="date" name="start" required max="${today}" value="${esc(state.custom.start)}"></label>
      <label>To <input type="date" name="end" required max="${today}" value="${esc(state.custom.end)}"></label>
      <button class="btn small" type="submit">Apply</button>
    </form>`;
  const form = $('form', wrap);
  const choose = (k) => {
    state.period = k;
    sessionStorageSet('bw.period', k);
    $$('.seg', wrap).forEach((b) => { b.classList.toggle('on', b.dataset.p === k); b.setAttribute('aria-selected', b.dataset.p === k); });
    form.hidden = k !== 'custom';
    if (k !== 'custom' || (state.custom.start && state.custom.end)) onChange();
  };
  $$('.seg', wrap).forEach((b) => b.addEventListener('click', () => choose(b.dataset.p)));
  const sel = $('select', wrap);
  if (sel) sel.addEventListener('change', () => choose(sel.value));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    if (fd.get('end') < fd.get('start')) { toast('The end date is before the start date', 'error'); return; }
    state.custom = { start: fd.get('start'), end: fd.get('end') };
    onChange();
  });
  return wrap;
}

function periodLabel(p) {
  if (!p) return '';
  const f = (d) => new Date(`${d}T00:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });
  return p.start === p.end ? f(p.start) : `${f(p.start)} – ${f(p.end)}`;
}

function statsGrid(s, { devices = false } = {}) {
  const items = [
    ['Total', s.total === null ? '—' : bytes(s.total), ''],
    ['Download', s.rx === null ? '—' : bytes(s.rx), 'down'],
    ['Upload', s.tx === null ? '—' : bytes(s.tx), 'up'],
    ['Peak ↓ / ↑', s.peak_rx === null ? '—' : `${rate(s.peak_rx)} / ${rate(s.peak_tx)}`, ''],
    ['Average ↓ / ↑', s.avg_rx === null ? '—' : `${rate(s.avg_rx)} / ${rate(s.avg_tx)}`, ''],
    [devices ? 'Minimum while active ↓ / ↑' : 'Minimum ↓ / ↑', s.min_rx === null || s.min_rx === undefined ? '—' : `${rate(s.min_rx)} / ${rate(s.min_tx)}`, ''],
    ['Measured time', `${duration(s.measured_s)} of ${duration(s.expected_s)}`, ''],
  ];
  let html = `<div class="stats">${items.map(([k, v, c]) => `<div class="stat ${c}"><span>${k}</span><b>${esc(v)}</b></div>`).join('')}</div>`;
  const notes = [];
  if (s.coverage !== null && s.coverage < 0.98) notes.push(`Measurements cover ${percent(s.coverage)} of the elapsed time in this period; unmeasured time is shown as a gap, not as zero usage.`);
  if (s.estimated) notes.push(`${bytes(s.estimated)} were recovered from interface counters after a service restart; their timing within the gap is estimated.`);
  if (notes.length) html += `<p class="note">${icons.info}<span>${notes.map(esc).join(' ')}</span></p>`;
  return html;
}

function legend() {
  return '<div class="legend"><span><i class="dot down"></i>Download</span><span><i class="dot up"></i>Upload</span><span><i class="dot nodata"></i>No data</span></div>';
}

// ---------------------------------------------------------------------------
// Live data (SSE with polling fallback). One connection serves all views.

function startLive() {
  stopLive();
  const onData = (d) => {
    const pts = state.live.points;
    for (const p of d.wan.points) if (!pts.length || p[0] > pts[pts.length - 1][0]) pts.push(p);
    const cutoff = Date.now() - 16 * 60 * 1000;
    while (pts.length && pts[0][0] < cutoff) pts.shift();
    const ap = state.live.aria2;
    for (const p of d.aria2_points || []) if (!ap.length || p[0] > ap[ap.length - 1][0]) ap.push(p);
    while (ap.length && ap[0][0] < cutoff) ap.shift();
    state.live.last = d;
    state.live.received = Date.now();
    setLivePill(d.wan.state === 'ok' ? 'live' : 'warn');
    state.liveListeners.forEach((fn) => fn(d));
  };
  const poll = async () => {
    try {
      const since = state.live.points.length ? state.live.points[state.live.points.length - 1][0] : Date.now() - 16 * 60 * 1000;
      onData(await api(`/api/live?since=${since}`));
    } catch { setLivePill('off'); }
  };
  const startPolling = () => {
    if (state.pollTimer) return;
    poll();
    state.pollTimer = setInterval(() => { if (!document.hidden) poll(); }, 2000);
  };
  // Seed with history, then stream.
  poll().then(() => {
    if (!window.EventSource) return startPolling();
    const es = new EventSource('/api/live/stream');
    state.stream = es;
    let opened = false;
    es.onopen = () => { opened = true; };
    es.onmessage = (ev) => { try { onData(JSON.parse(ev.data)); } catch { /* ignore */ } };
    es.onerror = () => {
      if (!opened || es.readyState === EventSource.CLOSED) { es.close(); state.stream = null; startPolling(); }
    };
  });
}

function stopLive() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (!state.session || !state.session.authenticated) return;
  if (document.hidden) stopLive(); else { startLive(); redrawCharts(); }
});

setInterval(() => {
  if (state.live.received && Date.now() - state.live.received > 12000) setLivePill('off');
}, 4000);

function setLivePill(kind) {
  const el = $('#live-pill');
  if (!el) return;
  el.className = `live-pill ${kind}`;
  el.innerHTML = `<i></i>${kind === 'live' ? 'Live' : kind === 'warn' ? 'Degraded' : 'Offline'}`;
  el.title = kind === 'live' ? 'Receiving live samples' : kind === 'warn' ? 'WAN measurement is limited — see System Health' : 'No live samples received recently';
}

function onLive(fn) { state.liveListeners.add(fn); state.cleanup.push(() => state.liveListeners.delete(fn)); if (state.live.last) fn(state.live.last); }

// ---------------------------------------------------------------------------
// Views

const views = {};

views.overview = {
  title: 'Overview',
  async mount(el) {
    el.innerHTML = `
      <section class="grid hero">
        <article class="card kpi down"><header><span class="kpi-icon">${icons.down}</span>Download now</header><div id="k-down" class="kpi-val">${bigValue({ num: '—', unit: '' })}</div><canvas class="spark" id="s-down" aria-hidden="true"></canvas></article>
        <article class="card kpi up"><header><span class="kpi-icon">${icons.up}</span>Upload now</header><div id="k-up" class="kpi-val">${bigValue({ num: '—', unit: '' })}</div><canvas class="spark" id="s-up" aria-hidden="true"></canvas></article>
        <article class="card kpi"><header>Today</header><div id="k-today" class="kpi-val">${skeleton(1)}</div><footer id="k-today-f"></footer></article>
        <article class="card kpi"><header>This month</header><div id="k-month" class="kpi-val">${skeleton(1)}</div><footer id="k-month-f"></footer></article>
      </section>
      <section class="card chart-card">
        <header class="card-h"><div><h2>Live bandwidth</h2><p class="sub">WAN interface counters · <span id="live-iface">detecting…</span></p></div>
          <div class="segmented small" role="tablist" aria-label="Live window">${[60, 300, 900].map((w) => `<button type="button" class="seg ${w === 300 ? 'on' : ''}" data-w="${w}">${w < 60 ? `${w}s` : `${w / 60} min`}</button>`).join('')}</div></header>
        <div class="chart-wrap tall"><canvas id="live-chart" role="img" aria-label="Live download and upload rate chart"></canvas></div>
        ${legend()}
      </section>
      <section class="grid three">
        <article class="card" id="c-sources"><header class="card-h"><h2>Traffic sources · today</h2></header>${skeleton(3)}</article>
        <article class="card" id="c-top"><header class="card-h"><h2>Top devices · today</h2><a class="link" href="#/devices">All devices</a></header>${skeleton(4)}</article>
        <article class="card" id="c-devs"><header class="card-h"><h2>Connected devices</h2></header>${skeleton(3)}</article>
      </section>
      <section class="grid two">
        <article class="card" id="c-health"><header class="card-h"><h2>Router health</h2><a class="link" href="#/health">Details</a></header>${skeleton(3)}</article>
        <article class="card" id="c-coll"><header class="card-h"><h2>Collectors &amp; database</h2></header>${skeleton(4)}</article>
      </section>`;
    const chart = track(new LiveChart($('#live-chart', el), { window: 300, tooltip: $('#tip') }));
    $$('.seg[data-w]', el).forEach((b) => b.addEventListener('click', () => {
      $$('.seg[data-w]', el).forEach((x) => x.classList.toggle('on', x === b));
      chart.setWindow(+b.dataset.w);
    }));
    onLive((d) => {
      chart.setData(state.live.points, d.now);
      $('#k-down', el).innerHTML = bigValue(rateParts(d.wan.down), 'down');
      $('#k-up', el).innerHTML = bigValue(rateParts(d.wan.up), 'up');
      const recent = state.live.points.slice(-60);
      sparkline($('#s-down', el), recent.map((p) => p[1]), getComputedStyle(document.documentElement).getPropertyValue('--c-down').trim());
      sparkline($('#s-up', el), recent.map((p) => p[2]), getComputedStyle(document.documentElement).getPropertyValue('--c-up').trim());
    });
    const load = async () => {
      let o;
      try { o = await api('/api/overview'); } catch (e) { if (e.status !== 401) $('#c-sources', el).innerHTML = errorCard(e, load); return; }
      renderOverview(el, o);
    };
    await load();
    every(15000, load);
  },
};

function kpiTotal(s) {
  if (s.total === null) return '<span class="big muted">No data</span>';
  return bigValue(bytesParts(s.total));
}

function renderOverview(el, o) {
  $('#live-iface', el).textContent = o.wan_interfaces.length ? o.wan_interfaces.map((i) => `${i.device}${i.proto ? ` (${i.proto})` : ''}`).join(' + ') : 'no WAN detected';
  $('#k-today', el).innerHTML = kpiTotal(o.today);
  $('#k-today-f', el).innerHTML = `<span class="down">↓ ${bytes(o.today.rx)}</span><span class="up">↑ ${bytes(o.today.tx)}</span>${coverageChip(o.today.coverage)}`;
  $('#k-month', el).innerHTML = kpiTotal(o.month);
  let mf = `<span class="down">↓ ${bytes(o.month.rx)}</span><span class="up">↑ ${bytes(o.month.tx)}</span>${coverageChip(o.month.coverage)}`;
  if (o.quota && o.quota.enabled) {
    const q = o.quota;
    mf += `<div class="quota ${q.state}" title="Projected for the period: ${bytes(q.projected)}"><div class="bar"><i data-w="${Math.min(100, q.percent)}"></i></div><span>${q.percent}% of ${bytes(q.limit)} quota</span></div>`;
  }
  $('#k-month-f', el).innerHTML = mf;

  // Sources
  const s = o.sources;
  let src = '<header class="card-h"><h2>Traffic sources · today</h2></header>';
  if (s.wan === null) src += emptyState('info', 'No WAN data yet', 'Measurements will appear after the first samples are collected.');
  else {
    const total = s.wan || 1;
    const dev = s.devices;
    const rows = [['Internet (WAN)', s.wan, 'Measured on the WAN interface', s.wan_coverage, 'wan']];
    if (dev !== null) rows.push(['LAN devices', dev, o.devices_state.provider ? `Reported by ${o.devices_state.provider}` : 'Per-device provider', s.devices_coverage, 'dev']);
    if (s.router_unattributed !== null) rows.push(['Router & unattributed', s.router_unattributed, 'WAN minus device traffic (includes aria2 and router services)', null, 'rtr']);
    if (s.aria2 !== null && o.aria2_available) rows.push(['aria2 (estimate)', s.aria2, 'Payload reported by aria2 — part of the router traffic above', s.aria2_coverage, 'ar']);
    src += `<ul class="bars">${rows.map(([n, v, t, c, k]) => `<li title="${esc(t)}"><div class="bar-row"><span>${n}</span><b>${bytes(v)}</b></div><div class="bar ${k}"><i data-w="${Math.min(100, (v / total) * 100).toFixed(1)}"></i></div></li>`).join('')}</ul>`;
    if (dev === null) src += `<p class="note">${icons.info}<span>${esc(o.devices_state.message)}</span></p>`;
    else src += '<p class="note subtle">Sources overlap: device and router traffic are parts of the WAN total, they are never added together.</p>';
  }
  $('#c-sources', el).innerHTML = src;

  // Top devices
  let top = '<header class="card-h"><h2>Top devices · today</h2><a class="link" href="#/devices">All devices</a></header>';
  if (!o.top_devices.length) top += emptyState('devices', 'No device data', o.devices_state.state === 'ok' ? 'No device has used the Internet today yet.' : esc(o.devices_state.message));
  else {
    const max = Math.max(...o.top_devices.map((d) => d.rx + d.tx), 1);
    top += `<ul class="toplist">${o.top_devices.map((d) => `<li><a href="#/devices/${encodeURIComponent(d.mac)}"><span class="conn">${connIcon(d.conn)}</span><span class="name"><b>${esc(d.name || d.mac)}</b><small>${esc(d.ip4 || d.mac)}</small></span><span class="val">${bytes(d.rx + d.tx)}<small>${d.down_rate !== null ? `${rate(d.down_rate)} ↓` : ''}</small></span></a><div class="bar dev"><i data-w="${(((d.rx + d.tx) / max) * 100).toFixed(1)}"></i></div></li>`).join('')}</ul>`;
  }
  $('#c-top', el).innerHTML = top;

  // Device counts
  const c = o.devices;
  $('#c-devs', el).innerHTML = `<header class="card-h"><h2>Connected devices</h2><a class="link" href="#/devices">Manage</a></header>
    <div class="devcount"><div class="ring" data-p="${c.known ? (c.online / c.known) * 100 : 0}"><b>${c.online}</b><small>online</small></div>
    <ul class="kv"><li><span>${icons.wifi} Wi-Fi</span><b>${c.wifi}</b></li><li><span>${icons.ethernet} Ethernet</span><b>${c.ethernet}</b></li><li><span>${icons.unknown} Unknown type</span><b>${c.unknown}</b></li><li><span>Known devices</span><b>${c.known}</b></li></ul></div>
    <p class="note subtle">Presence comes from Wi-Fi association, bridge activity and the provider; it is a best-effort indication.</p>`;

  // Health
  const h = o.health;
  const t = h.temperatures && h.temperatures[0];
  $('#c-health', el).innerHTML = `<header class="card-h"><h2>Router health</h2><a class="link" href="#/health">Details</a></header>
    <div class="gauges">${gauge('CPU', h.cpu_percent)}${gauge('Memory', h.mem_percent)}${t ? gauge('Temp', t.celsius, '°C', 100) : ''}</div>
    <ul class="kv"><li><span>Router uptime</span><b>${duration(h.uptime)}</b></li><li><span>BandWatch memory</span><b>${h.self_rss ? bytes(h.self_rss) : '—'}</b></li><li><span>BandWatch CPU</span><b>${h.self_cpu_percent !== null && h.self_cpu_percent !== undefined ? `${h.self_cpu_percent.toFixed(1)}%` : '—'}</b></li></ul>`;

  // Collectors
  const db = o.database;
  $('#c-coll', el).innerHTML = `<header class="card-h"><h2>Collectors &amp; database</h2><a class="link" href="#/health">Details</a></header>
    <ul class="collectors">${o.collectors.filter((x) => x.name !== 'system').map((x) => `<li><span><b>${esc(x.label)}</b><small>${esc(x.message)}</small></span>${statePill(x.state)}</li>`).join('')}
    <li><span><b>Database</b><small>${db.last_flush_error ? esc(db.last_flush_error) : db.last_flush ? `Last saved ${ago(db.last_flush)} · ${bytes(db.size_bytes)}` : 'Not saved yet (batched to reduce flash writes)'}</small></span>${statePill(db.last_flush_error ? 'error' : db.recovered_from ? 'degraded' : 'ok')}</li></ul>`;
}

function gauge(label, value, unit = '%', max = 100) {
  const v = value === null || value === undefined ? null : value;
  const pct = v === null ? 0 : Math.max(0, Math.min(100, (v / max) * 100));
  const cls = pct > 85 ? 'err' : pct > 65 ? 'warn' : 'ok';
  return `<div class="gauge ${cls}" data-p="${pct}" role="img" aria-label="${label} ${v === null ? 'unknown' : v + unit}"><div class="g-ring"><b>${v === null ? '—' : Math.round(v)}<small>${unit}</small></b></div><span>${label}</span></div>`;
}

// -- Devices --------------------------------------------------------------------

views.devices = {
  title: 'Devices',
  async mount(el, params) {
    if (params[0]) return deviceDetail(el, decodeURIComponent(params[0]));
    const ui = { q: '', filter: localGet('bw.devfilter') || 'all', sort: localGet('bw.devsort') || 'total' };
    el.innerHTML = `<div class="toolbar">
        <div class="search">${icons.search}<label class="sr-only" for="dev-q">Search devices</label><input id="dev-q" type="search" placeholder="Search name, IP or MAC" autocomplete="off"></div>
        <div class="segmented small" role="group" aria-label="Filter">${[['all', 'All'], ['online', 'Online'], ['wifi', 'Wi-Fi'], ['ethernet', 'Ethernet']].map(([k, l]) => `<button type="button" class="seg ${ui.filter === k ? 'on' : ''}" data-f="${k}">${l}</button>`).join('')}</div>
        <label class="sort"><span>Sort</span><select class="select" id="dev-sort">${[['total', 'Total usage'], ['down', 'Download'], ['up', 'Upload'], ['live', 'Live speed'], ['name', 'Name'], ['ip', 'IP address']].map(([k, l]) => `<option value="${k}" ${ui.sort === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <div id="dev-period"></div>
      </div>
      <div id="dev-notice"></div>
      <section class="card table-card"><div id="dev-table">${skeleton(6)}</div></section>`;
    $('#dev-period', el).appendChild(periodPicker(() => load(), true));
    let data = null;
    const render = () => {
      if (!data) return;
      let rows = data.devices.slice();
      const q = ui.q.trim().toLowerCase();
      if (q) rows = rows.filter((r) => [r.name, r.hostname, r.ip4, r.ip6, r.mac].some((v) => (v || '').toLowerCase().includes(q)));
      if (ui.filter === 'online') rows = rows.filter((r) => r.status === 'online');
      if (ui.filter === 'wifi' || ui.filter === 'ethernet') rows = rows.filter((r) => r.conn === ui.filter);
      const cmp = {
        total: (a, b) => (b.rx + b.tx) - (a.rx + a.tx), down: (a, b) => b.rx - a.rx, up: (a, b) => b.tx - a.tx,
        live: (a, b) => ((b.down_rate || 0) + (b.up_rate || 0)) - ((a.down_rate || 0) + (a.up_rate || 0)),
        name: (a, b) => (a.name || a.mac).localeCompare(b.name || b.mac),
        ip: (a, b) => ipNum(a.ip4) - ipNum(b.ip4),
      }[ui.sort];
      rows.sort(cmp);
      const max = Math.max(...rows.map((r) => r.rx + r.tx), 1);
      const prov = data.provider;
      $('#dev-notice', el).innerHTML = prov.state === 'ok' ? (data.direction_check.state === 'suspect_swapped' ? `<div class="banner-inline warn">${icons.alert}<span>${esc(data.direction_check.message)}</span></div>` : '') :
        `<div class="banner-inline ${prov.state === 'degraded' ? 'warn' : 'info'}">${icons.info}<span><b>Per-device usage: ${esc(prov.state)}.</b> ${esc(prov.message)}${prov.state !== 'degraded' ? ' Devices are still identified from DHCP leases, the ARP table and Wi-Fi stations, but their traffic cannot be measured.' : ''}</span></div>`;
      if (!rows.length) {
        $('#dev-table', el).innerHTML = emptyState('devices', data.devices.length ? 'No matching devices' : 'No devices found', data.devices.length ? 'Try a different search or filter.' : 'Devices appear when they obtain a DHCP lease or generate traffic.');
        return;
      }
      $('#dev-table', el).innerHTML = `<table class="table devices"><caption class="sr-only">Devices for ${esc(periodLabel(data.period))}</caption>
        <thead><tr><th scope="col">Device</th><th scope="col">Connection</th><th scope="col">Status</th><th scope="col" class="num">Live ↓ / ↑</th><th scope="col" class="num">Download</th><th scope="col" class="num">Upload</th><th scope="col" class="num">Total</th></tr></thead>
        <tbody>${rows.map((r) => `<tr tabindex="0" data-mac="${esc(r.mac)}">
          <td data-label="Device"><div class="dev-cell"><span class="conn">${connIcon(r.conn)}</span><span><b>${esc(r.name || 'Unnamed device')}</b><small>${esc(r.ip4 || '—')} · ${esc(r.mac)}${r.randomized_mac ? ' <em title="Locally administered (randomised) MAC address">private</em>' : ''}</small></span></div></td>
          <td data-label="Connection">${esc(r.conn === 'wifi' ? `Wi-Fi${r.band ? ` ${r.band}` : ''}` : r.conn === 'ethernet' ? `Ethernet${r.iface ? ` (${r.iface})` : ''}` : 'Unknown')}</td>
          <td data-label="Status">${statusBadge(r.status)}</td>
          <td data-label="Live" class="num">${r.down_rate === null ? '<span class="muted">—</span>' : `<span class="down">${rate(r.down_rate)}</span> <span class="up">${rate(r.up_rate)}</span>`}</td>
          <td data-label="Download" class="num">${bytes(r.rx)}</td>
          <td data-label="Upload" class="num">${bytes(r.tx)}</td>
          <td data-label="Total" class="num"><b>${bytes(r.rx + r.tx)}</b><div class="bar dev thin"><i data-w="${(((r.rx + r.tx) / max) * 100).toFixed(1)}"></i></div></td></tr>`).join('')}</tbody></table>
        <p class="note subtle">${coverageChip(data.coverage.coverage, 'measured')} Internet traffic only (LAN-local traffic is shown on each device's page). ${esc(periodLabel(data.period))}.</p>`;
      $$('tbody tr', el).forEach((tr) => {
        const go = () => { location.hash = `#/devices/${encodeURIComponent(tr.dataset.mac)}`; };
        tr.addEventListener('click', go);
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
      });
    };
    const load = async () => {
      try { data = await api(`/api/devices?${periodQuery()}`); render(); } catch (e) { if (e.status !== 401) $('#dev-table', el).innerHTML = errorCard(e, load); }
    };
    $('#dev-q', el).addEventListener('input', (e) => { ui.q = e.target.value; render(); });
    $$('.seg[data-f]', el).forEach((b) => b.addEventListener('click', () => { ui.filter = b.dataset.f; localSet('bw.devfilter', ui.filter); $$('.seg[data-f]', el).forEach((x) => x.classList.toggle('on', x === b)); render(); }));
    $('#dev-sort', el).addEventListener('change', (e) => { ui.sort = e.target.value; localSet('bw.devsort', ui.sort); render(); });
    await load();
    every(5000, load);
  },
};

function ipNum(ip) {
  if (!ip) return 1e12;
  return ip.split('.').reduce((a, b) => a * 256 + (+b || 0), 0);
}

async function deviceDetail(el, mac) {
  el.innerHTML = `<a class="back" href="#/devices">${icons.back} All devices</a><div id="dd">${skeleton(5)}</div>`;
  let chart = null;
  const load = async () => {
    let d;
    try { d = await api(`/api/devices/${encodeURIComponent(mac)}?${periodQuery()}`); } catch (e) { if (e.status !== 401) $('#dd', el).innerHTML = errorCard(e, load); return; }
    const dev = d.device;
    const root = $('#dd', el);
    if (!root.dataset.ready) {
      root.dataset.ready = '1';
      root.innerHTML = `<section class="card dev-head"><div class="dev-title"><span class="conn big-ic">${connIcon(dev.conn)}</span><div><h2 id="dd-name"></h2><p class="sub" id="dd-sub"></p></div></div>
          <form id="alias-form" class="alias"><label for="alias">Custom name</label><div class="inline"><input id="alias" maxlength="48" placeholder="e.g. Living room TV"><button class="btn small" type="submit">Save</button></div></form>
          <dl class="info" id="dd-info"></dl></section>
        <div class="toolbar" id="dd-period"></div>
        <section class="card"><header class="card-h"><div><h2>Internet usage</h2><p class="sub" id="dd-range"></p></div><div id="dd-cov"></div></header><div id="dd-stats"></div>
          <div class="chart-wrap"><canvas id="dd-chart" role="img" aria-label="Device usage chart"></canvas></div>${legend()}</section>
        <section class="card"><header class="card-h"><h2>LAN-local traffic</h2></header><div id="dd-lan"></div></section>`;
      $('#dd-period', el).appendChild(periodPicker(() => load()));
      chart = track(new BarChart($('#dd-chart', el), { tooltip: $('#tip') }));
      $('#alias', el).value = dev.alias || '';
      $('#alias-form', el).addEventListener('submit', async (e) => {
        e.preventDefault();
        try { await api(`/api/devices/${encodeURIComponent(mac)}/alias`, { method: 'POST', body: { alias: $('#alias', e.target).value } }); toast('Name saved'); load(); } catch (err) { toast(err.message, 'error'); }
      });
    }
    $('#dd-name', el).textContent = dev.name || 'Unnamed device';
    $('#dd-sub', el).innerHTML = `${statusBadge(dev.status)} ${dev.down_rate !== null ? `<span class="down">${rate(dev.down_rate)} ↓</span> <span class="up">${rate(dev.up_rate)} ↑</span>` : ''}`;
    const info = [['MAC address', dev.mac + (dev.randomized_mac ? ' (private / randomised)' : '')], ['IPv4', dev.ip4], ['IPv6', dev.ip6], ['Hostname', dev.hostname], ['Connection', dev.conn === 'wifi' ? `Wi-Fi ${dev.band || ''}` : dev.conn], ['Interface', dev.iface], ['Last seen online', dev.last_seen ? dateTime(dev.last_seen) : null]];
    $('#dd-info', el).innerHTML = info.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v || '—')}</dd></div>`).join('');
    $('#dd-range', el).textContent = periodLabel(d.period);
    $('#dd-cov', el).innerHTML = coverageChip(d.summary.coverage);
    $('#dd-stats', el).innerHTML = d.provider.state === 'ok' || d.summary.total !== null ? statsGrid(d.summary, { devices: true }) : `<p class="note">${icons.info}<span>${esc(d.provider.message)}</span></p>`;
    chart.setData(d.chart.points);
    const l = d.lan_summary;
    $('#dd-lan', el).innerHTML = l.rx === null && l.tx === null ? '<p class="muted">No LAN-local traffic recorded for this period (or the provider does not report it).</p>' :
      `<div class="stats"><div class="stat down"><span>Received from LAN</span><b>${bytes(l.rx)}</b></div><div class="stat up"><span>Sent to LAN</span><b>${bytes(l.tx)}</b></div></div><p class="note subtle">Traffic between devices on your network. It is not Internet usage and is not part of the WAN totals.</p>`;
  };
  await load();
  every(10000, load);
}

// -- aria2 ------------------------------------------------------------------------

views.aria2 = {
  title: 'aria2',
  async mount(el) {
    el.innerHTML = `<div id="a-body">${skeleton(5)}</div>`;
    let chart = null;
    let live = null;
    const load = async () => {
      let d;
      try { d = await api(`/api/aria2?${periodQuery()}`); } catch (e) { if (e.status !== 401) $('#a-body', el).innerHTML = errorCard(e, load); return; }
      const body = $('#a-body', el);
      if (!d.available && d.summary.total === null) {
        body.innerHTML = `<section class="card">${emptyState('aria2', d.status.state === 'disabled' ? 'aria2 integration is disabled' : 'aria2 is not connected', `${esc(d.status.message)}. BandWatch only reads aria2 status over JSON-RPC; it never starts, stops or changes downloads.`, '<a class="btn" href="#/settings">Configure aria2</a>')}</section>`;
        body.dataset.ready = '';
        return;
      }
      if (!body.dataset.ready) {
        body.dataset.ready = '1';
        body.innerHTML = `<section class="grid hero">
            <article class="card kpi down"><header><span class="kpi-icon">${icons.down}</span>aria2 download</header><div class="kpi-val" id="a-down"></div></article>
            <article class="card kpi up"><header><span class="kpi-icon">${icons.up}</span>aria2 upload</header><div class="kpi-val" id="a-up"></div></article>
            <article class="card kpi"><header>Tasks</header><div class="kpi-val" id="a-tasks"></div><footer id="a-tasks-f"></footer></article>
            <article class="card kpi"><header>Connection</header><div class="kpi-val small" id="a-conn"></div><footer id="a-conn-f"></footer></article>
          </section>
          <section class="grid two">
            <article class="card kpi" id="a-today-card"><header>aria2 today</header><div class="kpi-val" id="a-today"></div><footer id="a-today-f"></footer></article>
            <article class="card kpi" id="a-month-card"><header>aria2 this month</header><div class="kpi-val" id="a-month"></div><footer id="a-month-f"></footer></article>
          </section>
          <section class="card chart-card"><header class="card-h"><div><h2>Live aria2 speed</h2><p class="sub">Reported by aria2 (getGlobalStat)</p></div></header><div class="chart-wrap"><canvas id="a-live" role="img" aria-label="aria2 live speed"></canvas></div>${legend()}</section>
          <section class="card"><header class="card-h"><h2>Active downloads</h2></header><div id="a-active"></div></section>
          <div class="toolbar" id="a-period"></div>
          <section class="card"><header class="card-h"><div><h2>Transfer history</h2><p class="sub" id="a-range"></p></div><div id="a-cov"></div></header><div id="a-stats"></div><div class="chart-wrap"><canvas id="a-chart" role="img" aria-label="aria2 usage chart"></canvas></div>${legend()}<p class="note subtle">${esc(d.note)}</p></section>
          <section class="card"><header class="card-h"><h2>Recent activity</h2></header><div id="a-recent"></div></section>`;
        $('#a-period', el).appendChild(periodPicker(() => load()));
        chart = track(new BarChart($('#a-chart', el), { tooltip: $('#tip') }));
        live = track(new LiveChart($('#a-live', el), { window: 300, tooltip: $('#tip') }));
        onLive((ld) => {
          live.setData(state.live.aria2, ld.now);
          if (ld.aria2) {
            $('#a-down', el).innerHTML = bigValue(rateParts(ld.aria2.down), 'down');
            $('#a-up', el).innerHTML = bigValue(rateParts(ld.aria2.up), 'up');
          }
        });
      }
      const lv = d.live;
      if (!lv) { $('#a-down', el).innerHTML = '<span class="big muted">—</span>'; $('#a-up', el).innerHTML = '<span class="big muted">—</span>'; }
      $('#a-tasks', el).innerHTML = lv ? `<span class="big">${lv.active}<small>active</small></span>` : '<span class="big muted">—</span>';
      $('#a-tasks-f', el).innerHTML = lv ? `<span>${lv.waiting} waiting</span><span>${lv.counts.complete || 0} complete</span><span class="${lv.counts.error ? 'err-text' : ''}">${lv.counts.error || 0} failed</span>` : '';
      for (const [k, sum] of [['today', d.today], ['month', d.month]]) {
        $(`#a-${k}`, el).innerHTML = kpiTotal(sum);
        $(`#a-${k}-f`, el).innerHTML = sum.total === null ? '<span>No aria2 transfers measured in this period yet</span>' :
          `<span class="down">↓ ${bytes(sum.rx)}</span><span class="up">↑ ${bytes(sum.tx)}</span>${sum.peak_rx !== null ? `<span>peak ${rate(sum.peak_rx)}</span>` : ''}${coverageChip(sum.coverage)}`;
      }
      $('#a-conn', el).innerHTML = statePill(d.status.state);
      $('#a-conn-f', el).innerHTML = `<span>${esc(d.status.message)}</span>`;
      const unc = lv && lv.uncounted && lv.uncounted.tasks ? `<p class="note">${icons.info}<span>${lv.uncounted.tasks} task(s) already had ${bytes(lv.uncounted.bytes)} when BandWatch first saw them (resumed data, or downloads that started and finished between two polls). That data is not counted as new transfer.</span></p>` : '';
      $('#a-active', el).innerHTML = unc + (lv && lv.tasks.length ? `<ul class="tasks">${lv.tasks.map((t) => `<li><div class="task-h"><b title="${esc(t.name)}">${esc(t.name)}</b><span><span class="down">${rate(t.down)}</span>${t.up ? ` <span class="up">${rate(t.up)}</span>` : ''}</span></div><div class="bar dev"><i data-w="${t.progress === null ? 0 : (t.progress * 100).toFixed(1)}"></i></div><small>${bytes(t.done)} of ${t.total ? bytes(t.total) : 'unknown size'}${t.progress !== null ? ` · ${(t.progress * 100).toFixed(1)}%` : ''}${t.connections ? ` · ${t.connections} connections` : ''}</small></li>`).join('')}</ul>` : '<p class="muted">No active downloads.</p>');
      $('#a-range', el).textContent = periodLabel(d.period);
      $('#a-cov', el).innerHTML = coverageChip(d.summary.coverage);
      $('#a-stats', el).innerHTML = statsGrid(d.summary);
      chart.setData(d.chart.points);
      $('#a-recent', el).innerHTML = d.recent.length ? `<table class="table compact"><thead><tr><th>Task</th><th>Status</th><th class="num">Counted ↓</th><th class="num">Counted ↑</th><th>Last update</th></tr></thead><tbody>${d.recent.map((t) => `<tr><td data-label="Task"><span class="ellipsis" title="${esc(t.name)}">${esc(t.name)}</span></td><td data-label="Status"><span class="tag ${esc(t.status)}">${esc(t.status)}</span></td><td data-label="Counted ↓" class="num">${bytes(t.counted_rx)}</td><td data-label="Counted ↑" class="num">${bytes(t.counted_tx)}</td><td data-label="Updated">${ago(t.last_seen)}</td></tr>`).join('')}</tbody></table><p class="note subtle">"Counted" is the transfer BandWatch observed; data that already existed when a task was first seen is not counted.</p>` : '<p class="muted">No tasks recorded yet. Recent tasks are saved with the next database flush.</p>';
    };
    await load();
    every(5000, load);
  },
};

// -- History ------------------------------------------------------------------------

views.history = {
  title: 'History',
  async mount(el) {
    const ui = { series: sessionStorageGet('bw.series') || 'wan', stacked: true };
    el.innerHTML = `<div class="toolbar wrap">
        <div class="segmented" role="group" aria-label="Data source">${[['wan', 'Internet (WAN)'], ['dev', 'All devices'], ['aria2', 'aria2']].map(([k, l]) => `<button type="button" class="seg ${ui.series === k ? 'on' : ''}" data-s="${k}">${l}</button>`).join('')}</div>
        <div class="spacer"></div>
        <button class="btn ghost" id="h-csv" type="button">${icons.download} Export CSV</button>
      </div>
      <div class="toolbar" id="h-period"></div>
      <section class="card"><header class="card-h"><div><h2 id="h-title">Usage</h2><p class="sub" id="h-range"></p></div><div id="h-cov"></div></header>
        <div id="h-stats">${skeleton(2)}</div>
        <div class="chart-head"><div class="segmented small" role="group" aria-label="Chart style"><button type="button" class="seg on" data-st="1">Stacked</button><button type="button" class="seg" data-st="0">Side by side</button></div></div>
        <div class="chart-wrap tall"><canvas id="h-chart" role="img" aria-label="Usage history chart"></canvas></div>${legend()}</section>
      <section class="card" id="h-vn"><header class="card-h"><div><h2>Compare with vnStat</h2><p class="sub">Optional cross-check of WAN totals</p></div><button class="btn small ghost" id="vn-btn" type="button">Compare</button></header><div id="vn-body"><p class="muted">vnStat is queried only on request (its database may live on a sleeping disk).</p></div></section>`;
    $('#h-period', el).appendChild(periodPicker(() => load()));
    const chart = track(new BarChart($('#h-chart', el), { tooltip: $('#tip') }));
    $$('.seg[data-s]', el).forEach((b) => b.addEventListener('click', () => { ui.series = b.dataset.s; sessionStorageSet('bw.series', ui.series); $$('.seg[data-s]', el).forEach((x) => x.classList.toggle('on', x === b)); load(); }));
    $$('.seg[data-st]', el).forEach((b) => b.addEventListener('click', () => { chart.stacked = b.dataset.st === '1'; $$('.seg[data-st]', el).forEach((x) => x.classList.toggle('on', x === b)); chart.draw(); }));
    $('#h-csv', el).addEventListener('click', () => download(`/api/history.csv?series=${ui.series}&${periodQuery()}`));
    $('#vn-btn', el).addEventListener('click', async () => {
      $('#vn-body', el).innerHTML = skeleton(2);
      try {
        const v = await api('/api/vnstat');
        if (!v.available) { $('#vn-body', el).innerHTML = `<p class="muted">${esc(v.status.message)}</p>`; return; }
        const row = (label, bw, vn) => `<tr><td>${label}</td><td class="num">${bw.total === null ? '—' : bytes(bw.total)}</td><td class="num">${vn ? bytes(vn.rx + vn.tx) : '—'}</td><td class="num">${vn && bw.total ? `${(((bw.total - (vn.rx + vn.tx)) / (vn.rx + vn.tx || 1)) * 100).toFixed(1)}%` : '—'}</td></tr>`;
        $('#vn-body', el).innerHTML = v.interfaces.map((i) => `<table class="table compact"><caption>Interface ${esc(i.interface)}</caption><thead><tr><th></th><th class="num">BandWatch</th><th class="num">vnStat</th><th class="num">Difference</th></tr></thead><tbody>${row('Today', v.bandwatch.today, i.today)}${row('This month', v.bandwatch.month, i.month)}</tbody></table>`).join('') + `<p class="note subtle">${esc(v.note)}</p>`;
      } catch (e) { $('#vn-body', el).innerHTML = `<p class="err-text">${esc(e.message)}</p>`; }
    });
    const titles = { wan: 'Internet usage (WAN)', dev: 'Usage by all monitored devices', aria2: 'aria2 transfers (estimated payload)' };
    const load = async () => {
      let d;
      try { d = await api(`/api/history?series=${ui.series}&${periodQuery()}`); } catch (e) { if (e.status !== 401) $('#h-stats', el).innerHTML = errorCard(e, load); return; }
      $('#h-title', el).textContent = titles[ui.series];
      $('#h-range', el).textContent = `${periodLabel(d.period)} · ${d.chart.resolution === 'hour' ? 'hourly' : d.chart.resolution === 'day' ? 'daily' : 'monthly'} · timezone ${d.period.timezone}`;
      $('#h-cov', el).innerHTML = coverageChip(d.summary.coverage);
      $('#h-stats', el).innerHTML = d.summary.total === null && d.summary.expected_s === 0 ? emptyState('history', 'No history for this period', 'BandWatch was not collecting data during this period.') : statsGrid(d.summary);
      chart.setData(d.chart.points);
    };
    await load();
    every(30000, load);
  },
};

// -- Health -------------------------------------------------------------------------

views.health = {
  title: 'System health',
  async mount(el) {
    el.innerHTML = `<div id="hl">${skeleton(6)}</div>`;
    const load = async () => {
      let h;
      try { h = await api('/api/health'); } catch (e) { if (e.status !== 401) $('#hl', el).innerHTML = errorCard(e, load); return; }
      const s = h.system;
      const b = s.board || {};
      const db = h.database;
      $('#hl', el).innerHTML = `<section class="grid two">
        <article class="card"><header class="card-h"><h2>Router</h2></header>
          <div class="gauges">${gauge('CPU', s.cpu_percent)}${gauge('Memory', s.mem_percent)}${(s.temperatures || []).slice(0, 2).map((t) => gauge(t.name.replace(/-thermal$/, ''), t.celsius, '°C', 100)).join('')}</div>
          <ul class="kv"><li><span>Model</span><b>${esc(b.model || '—')}</b></li><li><span>OpenWrt</span><b>${esc(b.release || '—')} ${esc(b.revision || '')}</b></li><li><span>Target</span><b>${esc(b.target || '—')} · ${esc(b.arch || '')}</b></li><li><span>Kernel</span><b>${esc(b.kernel || '—')}</b></li><li><span>CPU cores</span><b>${s.cpus || '—'}</b></li><li><span>Load average</span><b>${s.load ? s.load.map((x) => x.toFixed(2)).join(' · ') : '—'}</b></li><li><span>Uptime</span><b>${duration(s.uptime)}</b></li><li><span>Memory available</span><b>${s.mem_available ? `${bytes(s.mem_available)} of ${bytes(s.mem_total)}` : '—'}</b></li></ul></article>
        <article class="card"><header class="card-h"><h2>Storage</h2></header>
          <ul class="bars">${(s.storage || []).map((x) => `<li><div class="bar-row"><span>${esc(x.label)} <small class="muted">${esc(x.path)}</small></span><b>${bytes(x.used)} / ${bytes(x.total)}</b></div><div class="bar ${x.used / x.total > 0.9 ? 'err' : 'dev'}"><i data-w="${((x.used / x.total) * 100).toFixed(1)}"></i></div></li>`).join('') || '<li class="muted">Storage information unavailable</li>'}</ul>
          <ul class="kv"><li><span>${icons.db} BandWatch database</span><b id="h-dbsize">${bytes(db.size_bytes)}</b></li><li><span>Stored on</span><b>${esc(s.data_filesystem || (h.demo ? 'simulated overlay' : 'unknown'))}</b></li></ul>
          <p class="note subtle">Filesystem bars show the whole filesystem; BandWatch itself only uses the database size above.</p>
          <h3 class="h3">BandWatch process</h3>
          <ul class="kv"><li><span>Version</span><b>${esc(h.version)}${h.demo ? ' (demo)' : ''}</b></li><li><span>Service uptime</span><b>${duration(h.uptime_service)}</b></li><li><span>Memory (RSS)</span><b>${s.self_rss ? bytes(s.self_rss) : '—'}</b></li><li><span>CPU</span><b>${s.self_cpu_percent !== undefined ? `${s.self_cpu_percent.toFixed(2)}%` : '—'}</b></li><li><span>Threads</span><b>${s.self_threads || '—'}</b></li><li><span>Timezone</span><b>${esc(h.timezone)}</b></li><li><span>Clock</span><b>${h.clock_trusted ? 'synchronised' : 'waiting for a trustworthy clock (samples are buffered)'}</b></li></ul></article>
      </section>
      <section class="card"><header class="card-h"><h2>Collectors</h2></header>
        <table class="table"><thead><tr><th>Collector</th><th>State</th><th>Details</th><th>Last success</th><th class="num">Failures</th></tr></thead><tbody>
        ${h.collectors.map((c) => `<tr><td data-label="Collector"><b>${esc(c.label)}</b>${c.version ? `<small class="muted"> v${esc(c.version)}</small>` : ''}</td><td data-label="State">${statePill(c.state)}</td><td data-label="Details">${esc(c.message)}${(c.notes || []).map((n) => `<div class="small muted">${esc(n)}</div>`).join('')}</td><td data-label="Last success">${ago(c.last_ok)}</td><td data-label="Failures" class="num">${c.error_count}</td></tr>`).join('')}
        </tbody></table></section>
      <section class="grid two">
        <article class="card"><header class="card-h"><h2>Database</h2>${statePill(db.last_flush_error ? 'error' : db.recovered_from ? 'degraded' : 'ok')}</header>
          <ul class="kv"><li><span>Size</span><b>${bytes(db.size_bytes)}</b></li><li><span>Schema version</span><b>${db.schema_version}</b></li><li><span>Journal mode</span><b>${esc(db.journal_mode)}</b></li><li><span>Last saved</span><b>${db.last_flush ? ago(db.last_flush) : 'not yet'}</b></li><li><span>Save interval</span><b>${duration(h.flush_interval)}</b></li><li><span>Unsaved buckets</span><b>${db.pending_buckets}</b></li><li><span>Rows (1 min / 15 min / day)</span><b>${db.rows_m1} / ${db.rows_m15} / ${db.rows_d1}</b></li></ul>
          ${db.recovered_from ? `<p class="note warn">${icons.alert}<span>The database was damaged and has been recreated. The damaged copy was kept as ${esc(db.recovered_from)}.</span></p>` : ''}
          ${db.last_flush_error ? `<p class="note err">${icons.alert}<span>${esc(db.last_flush_error)}</span></p>` : ''}
          <p class="note subtle">Samples are written in batches to limit flash wear. Up to ${duration(h.flush_interval)} of the most recent samples can be lost on a sudden power failure.</p></article>
        <article class="card"><header class="card-h"><h2>Accounting notes</h2></header>
          ${(h.accounting_notes || []).length ? `<ul class="notes">${h.accounting_notes.map((n) => `<li>${icons.info}<span>${esc(n)}</span></li>`).join('')}</ul>` : '<p class="muted">No known accounting limitations detected.</p>'}
          <h3 class="h3">Recent events</h3>
          ${h.events.length ? `<ul class="events">${h.events.map((e) => `<li class="${esc(e.level)}"><time>${ago(e.t)}</time><span>${esc(e.message)}</span></li>`).join('')}</ul>` : '<p class="muted">No events since the service started.</p>'}</article>
      </section>`;
    };
    await load();
    every(10000, load);
  },
};

// -- Settings -------------------------------------------------------------------------

function field(key, label, input, help = '') {
  return `<div class="field"><label for="f-${key}">${label}</label>${input}${help ? `<p class="help">${help}</p>` : ''}</div>`;
}
function sel(key, options, value) {
  return `<select class="select" id="f-${key}" data-key="${key}" data-type="${typeof options[0][0] === 'number' ? 'int' : 'str'}">${options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
}
function num(key, value, min, max, unit = '') {
  return `<div class="inline"><input type="number" id="f-${key}" data-key="${key}" data-type="int" min="${min}" max="${max}" value="${esc(value)}" required>${unit ? `<span class="unit">${unit}</span>` : ''}</div>`;
}
function txt(key, value, extra = '') {
  return `<input type="text" id="f-${key}" data-key="${key}" data-type="str" value="${esc(value)}" ${extra}>`;
}

views.settings = {
  title: 'Settings',
  async mount(el) {
    // Overlapping mounts (re-render after saving, quick navigation) must not
    // leave handlers bound to a replaced form: only the latest mount renders.
    const token = String((Number(el.dataset.mountToken) || 0) + 1);
    el.dataset.mountToken = token;
    el.innerHTML = skeleton(6);
    let d;
    try { d = await api('/api/settings'); } catch (e) { if (e.status !== 401 && el.dataset.mountToken === token) el.innerHTML = errorCard(e, () => views.settings.mount(el)); return; }
    if (el.dataset.mountToken !== token || state.view !== 'settings') return;
    const s = d.settings;
    const auto = !s['wan.interfaces'].length;
    el.innerHTML = `<form id="settings" class="settings" novalidate>
      <section class="card"><header class="card-h"><h2>Appearance &amp; units</h2></header><div class="fields">
        ${field('ui.theme', 'Default theme', sel('ui.theme', [['dark', 'Dark'], ['light', 'Light'], ['system', 'Follow system']], s['ui.theme']), 'The theme button in the header overrides this per browser.')}
        ${field('ui.rate_unit', 'Speed unit', sel('ui.rate_unit', [['bits', 'Bits per second (Mbps)'], ['bytes', 'Bytes per second (MB/s)']], s['ui.rate_unit']))}
        ${field('ui.volume_base', 'Data volume unit', sel('ui.volume_base', [[1000, 'Decimal (kB, MB, GB)'], [1024, 'Binary (KiB, MiB, GiB)']], s['ui.volume_base']), 'Internet providers usually bill in decimal units.')}
      </div></section>
      <section class="card"><header class="card-h"><h2>WAN monitoring</h2></header><div class="fields">
        <div class="field wide"><span class="label">WAN interfaces</span>
          <label class="check"><input type="checkbox" id="wan-auto" ${auto ? 'checked' : ''}> Detect automatically ${d.wan_detected.length ? `<span class="muted">(currently ${esc(d.wan_detected.join(', '))})</span>` : ''}</label>
          <div class="checks" id="wan-list" ${auto ? 'hidden' : ''}>${d.interfaces.map((i) => `<label class="check"><input type="checkbox" value="${esc(i)}" ${s['wan.interfaces'].includes(i) ? 'checked' : ''}> ${esc(i)}</label>`).join('')}</div>
          <p class="help">Choose only the devices that carry Internet traffic. Do not select both a PPPoE/tunnel device and its underlying port — that would double count.</p></div>
        ${field('wan.sample_interval', 'Sampling interval', num('wan.sample_interval', s['wan.sample_interval'], 1, 10, 'seconds'), 'Shorter intervals give finer peaks at a small CPU cost.')}
        ${field('wan.max_rate_mbps', 'Maximum link speed', num('wan.max_rate_mbps', s['wan.max_rate_mbps'], 10, 100000, 'Mbit/s'), 'Readings above this rate are treated as counter glitches.')}
      </div></section>
      <section class="card"><header class="card-h"><h2>Per-device monitoring</h2></header><div class="fields">
        ${field('devices.provider', 'Provider', sel('devices.provider', [['auto', 'Automatic (Bandix, then nlbwmon)'], ['bandix', 'Bandix'], ['nlbwmon', 'nlbwmon'], ['none', 'Disabled']], s['devices.provider']))}
        ${field('devices.bandix_url', 'Bandix address', txt('devices.bandix_url', s['devices.bandix_url'], 'spellcheck="false" autocomplete="off"'), 'Read-only API, normally http://127.0.0.1:8686')}
        ${field('devices.bandix_direction', 'Bandix direction', sel('devices.bandix_direction', [['rx_is_download', 'rx = device download (default)'], ['tx_is_download', 'tx = device download']], s['devices.bandix_direction']), 'Change only if device download/upload appear swapped compared to the WAN.')}
        ${field('devices.poll_interval', 'Polling interval', num('devices.poll_interval', s['devices.poll_interval'], 1, 60, 'seconds'))}
      </div></section>
      <section class="card"><header class="card-h"><h2>aria2</h2></header><div class="fields">
        ${field('aria2.mode', 'Integration', sel('aria2.mode', [['auto', 'Automatic'], ['on', 'Enabled'], ['off', 'Disabled']], s['aria2.mode']))}
        ${field('aria2.url', 'JSON-RPC address', txt('aria2.url', s['aria2.url'], 'spellcheck="false" autocomplete="off"'), 'Local endpoint, e.g. http://127.0.0.1:6800/jsonrpc')}
        <div class="field"><label for="a-secret">RPC secret</label><div class="inline"><input type="password" id="a-secret" autocomplete="new-password" placeholder="${d.secrets.aria2_secret ? 'A secret is stored — enter a new one to replace it' : 'Not set'}"><button type="button" class="btn small" id="a-secret-save">Save secret</button>${d.secrets.aria2_secret ? '<button type="button" class="btn small ghost" id="a-secret-clear">Clear</button>' : ''}</div><p class="help">Stored on the router with restricted permissions. It is never shown again or sent to the browser.</p></div>
        ${field('aria2.poll_interval', 'Polling interval', num('aria2.poll_interval', s['aria2.poll_interval'], 1, 60, 'seconds'))}
      </div></section>
      <section class="card"><header class="card-h"><h2>Time &amp; storage</h2></header><div class="fields">
        ${field('general.timezone', 'Timezone', txt('general.timezone', s['general.timezone'], 'spellcheck="false"'), `"system" uses the router timezone (currently ${esc(d.timezone_resolved)}). An IANA name or POSIX TZ string is also accepted.`)}
        ${field('general.week_start', 'Week starts on', sel('general.week_start', [[0, 'Monday'], [6, 'Sunday'], [5, 'Saturday']], s['general.week_start']))}
        ${field('storage.flush_interval', 'Save to storage every', num('storage.flush_interval', s['storage.flush_interval'], 30, 3600, 'seconds'), 'Longer intervals mean fewer flash writes but more data lost on power failure. Takes effect after restart.')}
        ${field('storage.minute_retention_days', 'Keep 1-minute detail', num('storage.minute_retention_days', s['storage.minute_retention_days'], 1, 60, 'days'))}
        ${field('storage.quarter_retention_days', 'Keep 15-minute detail', num('storage.quarter_retention_days', s['storage.quarter_retention_days'], 30, 1100, 'days'))}
        ${field('storage.device_quarter_retention_days', 'Keep 15-minute device detail', num('storage.device_quarter_retention_days', s['storage.device_quarter_retention_days'], 7, 1100, 'days'))}
        ${field('storage.day_retention_days', 'Keep daily totals', num('storage.day_retention_days', s['storage.day_retention_days'], 90, 7300, 'days'))}
        ${field('vnstat.mode', 'vnStat comparison', sel('vnstat.mode', [['auto', 'When installed'], ['off', 'Disabled']], s['vnstat.mode']))}
      </div></section>
      <section class="card"><header class="card-h"><h2>Monthly quota</h2></header><div class="fields">
        <div class="field"><span class="label">Quota alerts</span><label class="check"><input type="checkbox" id="f-quota.enabled" data-key="quota.enabled" data-type="bool" ${s['quota.enabled'] ? 'checked' : ''}> Track a monthly data allowance</label></div>
        ${field('quota.monthly_gb', 'Allowance', num('quota.monthly_gb', s['quota.monthly_gb'], 0, 1000000, 'GB'), 'Counts WAN download + upload, in decimal gigabytes.')}
        ${field('quota.reset_day', 'Billing cycle starts on day', num('quota.reset_day', s['quota.reset_day'], 1, 28))}
        ${field('quota.warn_percent', 'Warn at', num('quota.warn_percent', s['quota.warn_percent'], 10, 100, '%'))}
      </div></section>
      <div class="save-bar"><span id="save-msg" class="muted" role="status"></span><button class="btn primary" type="submit">Save settings</button></div>
    </form>
    <section class="grid two">
      <article class="card"><header class="card-h"><h2>Security</h2></header>
        ${d.service.auth === 'none' ? '<p class="note warn">Authentication is disabled (loopback-only demo).</p>' : `<form id="pw-form" class="fields single"><div class="field"><label for="pw-cur">Current password</label><input type="password" id="pw-cur" autocomplete="current-password"></div><div class="field"><label for="pw-new">New password</label><input type="password" id="pw-new" autocomplete="new-password" minlength="8" required></div><div class="field"><label for="pw-new2">Repeat new password</label><input type="password" id="pw-new2" autocomplete="new-password" minlength="8" required></div><button class="btn" type="submit">Change password</button></form>`}
        <ul class="kv"><li><span>Listening on</span><b>${esc(d.service.listen.join(', '))} · port ${d.service.port}</b></li><li><span>Data directory</span><b>${esc(d.service.data_dir)}</b></li></ul>
        <p class="help">Network settings are changed in /etc/config/bandwatch on the router.</p></article>
      <article class="card"><header class="card-h"><h2>Diagnostics &amp; export</h2></header>
        <p>Diagnostics contain versions, collector states and settings, but no MAC addresses, IP addresses, hostnames or secrets.</p>
        <div class="btn-row"><button class="btn" type="button" id="dl-diag">${icons.download} Diagnostics (JSON)</button><button class="btn ghost" type="button" id="dl-csv">${icons.download} Daily totals this year (CSV)</button><button class="btn ghost" type="button" id="dl-json">${icons.download} Daily totals (JSON)</button></div>
        <ul class="kv"><li><span>BandWatch</span><b>${esc(d.version)}</b></li><li><span>License</span><b>MIT</b></li><li><span>Telemetry</span><b>None</b></li></ul></article>
    </section>`;
    const form = $('#settings', el);
    $('#wan-auto', el).addEventListener('change', (e) => { $('#wan-list', el).hidden = e.target.checked; });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const changes = {};
      for (const inp of $$('[data-key]', form)) {
        const t = inp.dataset.type;
        const v = t === 'bool' ? inp.checked : t === 'int' ? parseInt(inp.value, 10) : inp.value.trim();
        if (t === 'int' && Number.isNaN(v)) { toast(`Please enter a number for ${inp.dataset.key}`, 'error'); inp.focus(); return; }
        if (JSON.stringify(v) !== JSON.stringify(s[inp.dataset.key])) changes[inp.dataset.key] = v;
      }
      const ifaces = $('#wan-auto', el).checked ? [] : $$('#wan-list input:checked', el).map((x) => x.value);
      if (!$('#wan-auto', el).checked && !ifaces.length) { toast('Select at least one WAN interface or enable automatic detection', 'error'); return; }
      if (JSON.stringify(ifaces) !== JSON.stringify(s['wan.interfaces'])) changes['wan.interfaces'] = ifaces;
      if (!Object.keys(changes).length) { $('#save-msg', el).textContent = 'No changes.'; return; }
      try {
        const r = await api('/api/settings', { method: 'POST', body: changes });
        Object.assign(s, r.settings);
        state.settings = r.settings;
        applyPrefs(r.settings);
        const restart = Object.keys(changes).some((k) => d.restart_required_keys.includes(k));
        $('#save-msg', el).textContent = restart ? 'Saved. Some changes apply after restarting BandWatch.' : 'Saved.';
        toast('Settings saved');
      } catch (err) { toast(err.message, 'error'); $('#save-msg', el).textContent = err.message; }
    });
    const saveSecret = async (value) => {
      try { await api('/api/settings/aria2-secret', { method: 'POST', body: { secret: value } }); toast(value ? 'aria2 secret saved' : 'aria2 secret cleared'); if (state.view === 'settings') views.settings.mount(el); } catch (err) { toast(err.message, 'error'); }
    };
    const secretInput = $('#a-secret', el);
    $('#a-secret-save', el).addEventListener('click', () => { const v = secretInput.value; if (!v) { toast('Enter the secret first', 'error'); return; } saveSecret(v); });
    const clr = $('#a-secret-clear', el);
    if (clr) clr.addEventListener('click', () => saveSecret(''));
    const pw = $('#pw-form', el);
    if (pw) pw.addEventListener('submit', async (e) => {
      e.preventDefault();
      if ($('#pw-new', pw).value !== $('#pw-new2', pw).value) { toast('The new passwords do not match', 'error'); return; }
      try {
        const r = await api('/api/password', { method: 'POST', body: { current: $('#pw-cur', pw).value, password: $('#pw-new', pw).value } });
        state.csrf = r.csrf;
        pw.reset();
        toast('Password changed');
      } catch (err) { toast(err.message, 'error'); }
    });
    $('#dl-diag', el).addEventListener('click', () => download('/api/diagnostics'));
    $('#dl-csv', el).addEventListener('click', () => download('/api/export?format=csv&period=this_year'));
    $('#dl-json', el).addEventListener('click', () => download('/api/export?format=json&period=this_year'));
  },
};

// ---------------------------------------------------------------------------
// Login

function showLogin(message) {
  stopLive();
  const box = $('#login');
  box.hidden = false;
  document.body.classList.add('locked');
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const msg = message || (params.get('sso') === 'failed' ? 'The LuCI sign-in link expired. Open BandWatch again from LuCI or sign in with your password.' : '');
  box.innerHTML = `<form class="login-card" id="login-form">
      <div class="login-logo">${logo}<div><b>BandWatch</b><small>Network traffic monitor</small></div></div>
      ${state.session && !state.session.password_set ? '<p class="note warn">No dashboard password has been set yet. On the router run: <code>bandwatch passwd</code></p>' : ''}
      <label for="login-pw">Password</label>
      <input type="password" id="login-pw" autocomplete="current-password" required autofocus>
      <p class="err-text" id="login-err" role="alert">${esc(msg)}</p>
      <button class="btn primary block" type="submit">Sign in</button>
      <p class="help">Tip: if LuCI is installed, open <b>Status → BandWatch</b> to sign in automatically.</p>
    </form>`;
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('button', e.target);
    btn.disabled = true;
    try {
      const r = await api('/api/login', { method: 'POST', body: { password: $('#login-pw').value } });
      state.csrf = r.csrf;
      box.hidden = true;
      document.body.classList.remove('locked');
      await boot();
    } catch (err) {
      $('#login-err').textContent = err.message;
      btn.disabled = false;
    }
  });
  setTimeout(() => { const i = $('#login-pw'); if (i) i.focus(); }, 50);
}

async function logout() {
  try { await api('/api/logout', { method: 'POST', body: {} }); } catch { /* ignore */ }
  state.session = null;
  showLogin();
}

// ---------------------------------------------------------------------------
// Router

const NAV = [['overview', 'Overview'], ['devices', 'Devices'], ['aria2', 'aria2'], ['history', 'History'], ['health', 'Health'], ['settings', 'Settings']];

function buildShell() {
  $('#brand').innerHTML = `${logo}<span><b>BandWatch</b><small>Traffic monitor</small></span>`;
  const nav = NAV.map(([k, l]) => `<a href="#/${k}" data-nav="${k}">${icons[k]}<span>${l}</span></a>`).join('');
  $('#nav').innerHTML = nav;
  $('#bottomnav').innerHTML = NAV.filter(([k]) => k !== 'settings' || true).map(([k, l]) => `<a href="#/${k}" data-nav="${k}">${icons[k]}<span>${l === 'System health' ? 'Health' : l}</span></a>`).join('');
  $('#theme-btn').addEventListener('click', cycleTheme);
  $('#menu-btn').innerHTML = icons.logout;
  $('#menu-btn').addEventListener('click', logout);
  $('#menu-btn').hidden = !state.session || state.session.auth === 'none';
}

async function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'overview';
  const [path] = hash.split('?');
  const parts = path.split('/').filter(Boolean);
  const name = views[parts[0]] ? parts[0] : 'overview';
  state.cleanup.forEach((fn) => { try { fn(); } catch { /* ignore */ } });
  state.cleanup = [];
  state.view = name;
  $$('[data-nav]').forEach((a) => {
    const on = a.dataset.nav === name;
    a.classList.toggle('on', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const v = views[name];
  $('#page-title').textContent = v.title;
  document.title = `${v.title} · BandWatch`;
  const main = $('#main');
  main.innerHTML = '';
  main.className = `view view-${name}`;
  const tip = $('#tip');
  if (tip) tip.hidden = true;
  try { await v.mount(main, parts.slice(1)); } catch (e) { if (e.status !== 401) main.innerHTML = errorCard(e, route); }
  if (document.activeElement && document.activeElement.closest('[data-nav]')) main.focus({ preventScroll: true });
}

async function boot() {
  try {
    state.session = await api('/api/session');
  } catch (e) {
    $('#main').innerHTML = errorCard(e, boot);
    return;
  }
  applyTheme();
  buildShell();
  $('#demo-badge').hidden = !state.session.demo;
  $('#demo-banner').hidden = !state.session.demo;
  $('#version').textContent = `v${state.session.version}`;
  if (!state.session.authenticated) { showLogin(); return; }
  if (state.session.csrf) state.csrf = state.session.csrf;
  try {
    const st = await api('/api/settings');
    state.settings = st.settings;
    applyPrefs(st.settings);
  } catch { /* keep defaults */ }
  startLive();
  route();
}

window.addEventListener('hashchange', () => { if (state.session && state.session.authenticated) route(); });

if ('serviceWorker' in navigator && window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
  navigator.serviceWorker.register('/sw.js').catch(() => { /* optional */ });
}

// Dynamic sizes are applied through the CSSOM (allowed by the strict CSP,
// which blocks inline style attributes).
function applySizes(root) {
  if (!root.querySelectorAll) return;
  const nodes = [...root.querySelectorAll('[data-w],[data-p]')];
  if (root.matches && root.matches('[data-w],[data-p]')) nodes.push(root);
  for (const n of nodes) {
    if (n.dataset.w !== undefined) n.style.width = `${Math.max(0, Math.min(100, parseFloat(n.dataset.w) || 0))}%`;
    if (n.dataset.p !== undefined) n.style.setProperty('--p', String(parseFloat(n.dataset.p) || 0));
  }
}
new MutationObserver((muts) => {
  for (const m of muts) {
    if (m.type === 'attributes') applySizes(m.target);
    m.addedNodes.forEach((n) => { if (n.nodeType === 1) applySizes(n); });
  }
}).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-w', 'data-p'] });

applyTheme();
boot();

export { state, api, clock };
