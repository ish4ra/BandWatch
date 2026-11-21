// Formatting helpers. All inputs are bytes or bytes/second (integers from the API).

export const prefs = { rateUnit: 'bits', volumeBase: 1000 };

const DEC = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
const BIN = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

function scaled(value, base, units) {
  let v = Math.abs(value);
  let i = 0;
  while (v >= base && i < units.length - 1) { v /= base; i++; }
  const digits = v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
  return { num: (value < 0 ? -v : v).toFixed(digits), unit: units[i] };
}

export function bytesParts(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return { num: '—', unit: '' };
  return prefs.volumeBase === 1024 ? scaled(n, 1024, BIN) : scaled(n, 1000, DEC);
}

export function bytes(n) {
  const p = bytesParts(n);
  return p.unit ? `${p.num} ${p.unit}` : p.num;
}

export function rateParts(bps) {
  if (bps === null || bps === undefined || Number.isNaN(bps)) return { num: '—', unit: '' };
  if (prefs.rateUnit === 'bits') {
    const p = scaled(bps * 8, 1000, ['bps', 'kbps', 'Mbps', 'Gbps', 'Tbps']);
    return p;
  }
  const units = prefs.volumeBase === 1024 ? ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'] : ['B/s', 'kB/s', 'MB/s', 'GB/s'];
  return scaled(bps, prefs.volumeBase, units);
}

export function rate(bps) {
  const p = rateParts(bps);
  return p.unit ? `${p.num} ${p.unit}` : p.num;
}

export function percent(x, digits = 0) {
  if (x === null || x === undefined) return '—';
  return `${(x * 100).toFixed(digits)}%`;
}

export function duration(sec) {
  if (sec === null || sec === undefined) return '—';
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${sec}s`;
}

export function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 5) return 'just now';
  if (s < 60) return `${Math.floor(s)} s ago`;
  return `${duration(s)} ago`;
}

export function clock(ms) {
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function dateTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"'`]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' }[c]));
}

// "Nice" axis maximum and tick step for a value range.
export function niceScale(max, ticks = 4) {
  if (!max || max <= 0) return { max: 1, step: 0.25 };
  const raw = max / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  return { max: Math.ceil(max / step) * step, step };
}
