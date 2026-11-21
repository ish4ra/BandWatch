// Lightweight canvas charts. Charts redraw only when data or size changes
// (no animation loop), pause while hidden and honour devicePixelRatio.

import { bytes, niceScale, prefs, rate } from './format.js';

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function palette() {
  return {
    down: css('--c-down') || '#34D9B3',
    up: css('--c-up') || '#8D9CFF',
    grid: css('--c-grid') || 'rgba(148,163,184,.12)',
    axis: css('--c-muted') || '#94A3B8',
    text: css('--c-text') || '#F1F5F9',
    surface: css('--c-elevated') || '#172231',
    border: css('--c-border') || '#243244',
    nodata: css('--c-nodata') || 'rgba(148,163,184,.10)',
  };
}

class BaseChart {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.opts = opts;
    this.hover = null;
    this.pending = false;
    this.w = 0;
    this.h = 0;
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(canvas.parentElement || canvas);
    this.tip = opts.tooltip || null;
    canvas.addEventListener('pointermove', (e) => this.onMove(e));
    canvas.addEventListener('pointerleave', () => { this.hover = null; this.hideTip(); this.draw(); });
    this.resize();
  }

  destroy() { this.ro.disconnect(); this.hideTip(); }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (w === this.w && h === this.h && this.dpr === dpr) return;
    this.w = w; this.h = h; this.dpr = dpr;
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.draw();
  }

  draw() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      if (document.hidden || !this.w) return;
      const { ctx } = this;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      this.render(ctx, palette());
    });
  }

  showTip(html, x, y) {
    if (!this.tip) return;
    this.tip.innerHTML = html;
    this.tip.hidden = false;
    const box = this.canvas.getBoundingClientRect();
    const tw = this.tip.offsetWidth;
    let left = box.left + x + 14;
    if (left + tw > window.innerWidth - 8) left = box.left + x - tw - 14;
    this.tip.style.left = `${Math.max(8, left)}px`;
    this.tip.style.top = `${Math.max(8, box.top + y - 10)}px`;
  }

  hideTip() { if (this.tip) this.tip.hidden = true; }
}

function yAxis(ctx, pal, area, max, fmt) {
  const factor = prefs.rateUnit === 'bits' && fmt === rate ? 8 : 1;
  const s = niceScale(max * factor, 4);
  const top = s.max / factor;
  ctx.font = '11px ' + (css('--font-num') || 'system-ui');
  ctx.fillStyle = pal.axis;
  ctx.strokeStyle = pal.grid;
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let v = 0; v <= s.max + 1e-9; v += s.step) {
    const y = Math.round(area.y + area.h - (v / s.max) * area.h) + 0.5;
    ctx.beginPath();
    ctx.moveTo(area.x, y);
    ctx.lineTo(area.x + area.w, y);
    ctx.stroke();
    ctx.fillText(fmt(v / factor), area.x - 8, y);
  }
  return top;
}

export class LiveChart extends BaseChart {
  constructor(canvas, opts = {}) {
    super(canvas, opts);
    this.points = [];
    this.window = opts.window || 300;
    this.series = opts.series || ['down', 'up'];
  }

  setData(points, nowMs) {
    this.points = points;
    this.nowMs = nowMs || (points.length ? points[points.length - 1][0] : Date.now());
    this.draw();
  }

  setWindow(sec) { this.window = sec; this.draw(); }

  area() { return { x: 64, y: 12, w: Math.max(10, this.w - 76), h: Math.max(10, this.h - 34) }; }

  render(ctx, pal) {
    const area = this.area();
    const t1 = this.nowMs || Date.now();
    const t0 = t1 - this.window * 1000;
    const pts = this.points.filter((p) => p[0] >= t0 - 4000);
    let max = 0;
    for (const p of pts) max = Math.max(max, p[1] || 0, p[2] || 0);
    const top = yAxis(ctx, pal, area, Math.max(max * 1.1, 1250), rate);
    const X = (t) => area.x + ((t - t0) / (t1 - t0)) * area.w;
    const Y = (v) => area.y + area.h - (Math.min(v, top) / top) * area.h;
    // x labels
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = pal.axis;
    const step = this.window <= 60 ? 15 : this.window <= 300 ? 60 : 300;
    for (let s = 0; s <= this.window; s += step) {
      const x = X(t1 - s * 1000);
      ctx.fillText(s === 0 ? 'now' : `-${s >= 60 ? `${s / 60}m` : `${s}s`}`, x, area.y + area.h + 8);
    }
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.x, area.y - 2, area.w, area.h + 4);
    ctx.clip();
    const cols = [pal.down, pal.up];
    [1, 2].forEach((idx, si) => {
      const color = cols[si];
      // Build segments split on unavailable samples (null).
      const segs = [];
      let cur = [];
      for (const p of pts) {
        if (p[idx] === null || p[idx] === undefined) { if (cur.length) segs.push(cur); cur = []; continue; }
        cur.push([X(p[0]), Y(p[idx])]);
      }
      if (cur.length) segs.push(cur);
      for (const seg of segs) {
        if (seg.length < 2) continue;
        if (si === 0) {
          const g = ctx.createLinearGradient(0, area.y, 0, area.y + area.h);
          g.addColorStop(0, color + '55');
          g.addColorStop(1, color + '00');
          ctx.beginPath();
          ctx.moveTo(seg[0][0], area.y + area.h);
          seg.forEach(([x, y]) => ctx.lineTo(x, y));
          ctx.lineTo(seg[seg.length - 1][0], area.y + area.h);
          ctx.closePath();
          ctx.fillStyle = g;
          ctx.fill();
        }
        ctx.beginPath();
        seg.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.strokeStyle = color;
        ctx.lineWidth = si === 0 ? 2 : 1.6;
        ctx.lineJoin = 'round';
        ctx.stroke();
      }
    });
    // unavailable spans
    ctx.fillStyle = pal.nodata;
    for (const p of pts) {
      if (p[1] === null) ctx.fillRect(X(p[0]) - 3, area.y, 6, area.h);
    }
    ctx.restore();
    if (this.hover !== null && pts.length) {
      let best = null;
      for (const p of pts) if (!best || Math.abs(X(p[0]) - this.hover) < Math.abs(X(best[0]) - this.hover)) best = p;
      if (best) {
        const x = X(best[0]);
        ctx.strokeStyle = pal.axis;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, area.y);
        ctx.lineTo(x, area.y + area.h);
        ctx.stroke();
        ctx.setLineDash([]);
        [[1, pal.down], [2, pal.up]].forEach(([i, c]) => {
          if (best[i] === null) return;
          ctx.fillStyle = c;
          ctx.beginPath();
          ctx.arc(x, Y(best[i]), 3.5, 0, Math.PI * 2);
          ctx.fill();
        });
        const t = new Date(best[0]).toLocaleTimeString();
        const body = best[1] === null ? '<span class="muted">Measurement unavailable</span>' :
          `<div><i class="dot down"></i>Download <b>${rate(best[1])}</b></div><div><i class="dot up"></i>Upload <b>${rate(best[2])}</b></div>`;
        this.showTip(`<div class="tip-title">${t}</div>${body}`, x, area.y + 20);
      }
    }
  }

  onMove(e) {
    const r = this.canvas.getBoundingClientRect();
    this.hover = e.clientX - r.left;
    this.draw();
  }
}

export class BarChart extends BaseChart {
  constructor(canvas, opts = {}) {
    super(canvas, opts);
    this.points = [];
    this.stacked = opts.stacked ?? true;
  }

  setData(points) { this.points = points || []; this.draw(); }

  area() { return { x: 64, y: 12, w: Math.max(10, this.w - 76), h: Math.max(10, this.h - 40) }; }

  render(ctx, pal) {
    const area = this.area();
    const pts = this.points;
    const n = pts.length || 1;
    let max = 0;
    for (const p of pts) max = Math.max(max, this.stacked ? (p.rx || 0) + (p.tx || 0) : Math.max(p.rx || 0, p.tx || 0));
    const top = yAxis(ctx, pal, area, Math.max(max * 1.08, 1000), bytes);
    const slot = area.w / n;
    const bw = Math.max(2, Math.min(36, slot * 0.66));
    const Y = (v) => (Math.min(v, top) / top) * area.h;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const every = Math.ceil(n / Math.max(1, Math.floor(area.w / 56)));
    this.hitboxes = [];
    pts.forEach((p, i) => {
      const cx = area.x + slot * i + slot / 2;
      const x = cx - bw / 2;
      const base = area.y + area.h;
      const hovered = this.hover === i;
      if (p.rx === null) {
        if (!p.future) {
          ctx.fillStyle = pal.nodata;
          ctx.fillRect(x, area.y + area.h * 0.55, bw, area.h * 0.45);
        }
      } else {
        const partial = p.coverage !== null && p.coverage < 0.9;
        ctx.globalAlpha = partial ? 0.55 : hovered ? 1 : 0.9;
        const hrx = Y(p.rx);
        const htx = Y(p.tx);
        if (this.stacked) {
          ctx.fillStyle = pal.down;
          roundRect(ctx, x, base - hrx, bw, hrx, 0);
          ctx.fillStyle = pal.up;
          roundRect(ctx, x, base - hrx - htx, bw, htx, 3);
        } else {
          const hw = bw / 2 - 1;
          ctx.fillStyle = pal.down;
          roundRect(ctx, x, base - hrx, hw, hrx, 3);
          ctx.fillStyle = pal.up;
          roundRect(ctx, x + hw + 2, base - htx, hw, htx, 3);
        }
        ctx.globalAlpha = 1;
      }
      this.hitboxes.push([area.x + slot * i, area.x + slot * (i + 1)]);
      if (i % every === 0) {
        ctx.fillStyle = pal.axis;
        ctx.fillText(p.label || '', cx, area.y + area.h + 8);
      }
    });
    if (this.hover !== null && pts[this.hover]) {
      const p = pts[this.hover];
      const cx = area.x + slot * this.hover + slot / 2;
      let body;
      if (p.rx === null) body = `<div class="muted">${p.future ? 'Not yet reached' : 'No measurements for this interval'}</div>`;
      else {
        body = `<div><i class="dot down"></i>Download <b>${bytes(p.rx)}</b></div><div><i class="dot up"></i>Upload <b>${bytes(p.tx)}</b></div>`;
        if (p.peak_rx !== null && p.peak_rx !== undefined) body += `<div class="muted">Peak ${rate(p.peak_rx)} ↓ · ${rate(p.peak_tx)} ↑</div>`;
        if (p.coverage !== null && p.coverage < 0.999) body += `<div class="warn-text">Measured ${(p.coverage * 100).toFixed(0)}% of this interval</div>`;
      }
      this.showTip(`<div class="tip-title">${p.label}</div>${body}`, cx, area.y + 16);
    }
  }

  onMove(e) {
    const r = this.canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    const idx = (this.hitboxes || []).findIndex(([a, b]) => x >= a && x < b);
    if (idx !== this.hover) { this.hover = idx >= 0 ? idx : null; this.draw(); }
    if (idx < 0) this.hideTip();
  }
}

function roundRect(ctx, x, y, w, h, r) {
  if (h <= 0) return;
  r = Math.min(r, h, w / 2);
  ctx.beginPath();
  ctx.moveTo(x, y + h);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h);
  ctx.closePath();
  ctx.fill();
}

// Tiny sparkline (no axes, no interaction).
export function sparkline(canvas, values, color) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 120;
  const h = canvas.clientHeight || 32;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const vals = values.filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return;
  const max = Math.max(...vals, 1);
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - 2 - ((v || 0) / max) * (h - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, color + '40');
  g.addColorStop(1, color + '00');
  ctx.fillStyle = g;
  ctx.fill();
}
