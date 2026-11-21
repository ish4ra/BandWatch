"""JSON API handlers (transport independent, unit tested without HTTP)."""

import csv
import datetime as dt
import io
import re

from . import __version__, analytics
from . import config as cfgmod
from . import store as stmod
from . import tz as tzmod
from .collectors.perdevice import norm_mac

ALIAS_RE = re.compile(r"^[^\x00-\x1f\x7f<>\"'`]{0,48}$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _period(engine, params, month_start_day=1):
    try:
        return analytics.resolve_period(params, engine.now(), engine.tz, engine.settings["general.week_start"], month_start_day)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


def _label(ts, res, tz):
    d = dt.datetime.fromtimestamp(ts, tz)
    if res == "hour":
        return d.strftime("%H:%M")
    if res == "day":
        return d.strftime("%a %d %b")
    return d.strftime("%b %Y")


def _chart(engine, series, key, a, b, coverage_key=None):
    ch = analytics.chart(engine.store, series, key, a, b, engine.now(), engine.first_ts(), engine.tz, coverage_key)
    for p in ch["points"]:
        p["label"] = _label(p["t"], ch["resolution"], engine.tz)
    return ch


def _period_meta(name, a, b, da, db, engine):
    return {"name": name, "start": da.isoformat(), "end": db.isoformat(), "start_ts": a, "end_ts": b, "timezone": engine.tz_desc}


def overview(engine, params=None):
    now = engine.now()
    first = engine.first_ts()
    tz = engine.tz
    a, b, _, _ = tzmod.period_bounds("today", now, tz)
    ma, mb, mda, mdb = tzmod.period_bounds("this_month", now, tz)
    today = analytics.summary(engine.store, "wan", "*", a, b, now, first)
    month = analytics.summary(engine.store, "wan", "*", ma, mb, now, first)
    dev_today = analytics.summary(engine.store, "dev", "*", a, b, now, first)
    aria_today = analytics.summary(engine.store, "aria2", "*", a, b, now, first)
    rows, _cov = engine.device_table(a, b)
    counts = {"known": len(rows), "online": 0, "wifi": 0, "ethernet": 0, "unknown": 0}
    for r in rows:
        if r["status"] == "online":
            counts["online"] += 1
            counts[r["conn"] if r["conn"] in ("wifi", "ethernet") else "unknown"] += 1
    top = [
        {k: r[k] for k in ("mac", "name", "ip4", "conn", "status", "rx", "tx", "down_rate", "up_rate")}
        for r in rows
        if r["rx"] + r["tx"] > 0
    ][:6]
    sources = {
        "wan": today["total"],
        "wan_coverage": today["coverage"],
        "devices": dev_today["total"],
        "devices_coverage": dev_today["coverage"],
        "aria2": aria_today["total"],
        "aria2_coverage": aria_today["coverage"],
        "router_unattributed": None,
    }
    if today["total"] is not None and dev_today["total"] is not None and (today["coverage"] or 0) > 0.9 and (dev_today["coverage"] or 0) > 0.9:
        sources["router_unattributed"] = max(0, today["total"] - dev_today["total"])
    sysinfo = engine.system.info
    return {
        "version": __version__,
        "demo": engine.service.demo,
        "live": engine.live(since_ms=int(now * 1000) - engine.settings["ui.live_window"] * 1000),
        "today": today,
        "month": month,
        "month_period": {"start": mda.isoformat(), "end": (mdb - analytics.ONE_DAY).isoformat()},
        "devices": counts,
        "devices_state": engine.devices.status.as_dict(),
        "top_devices": top,
        "sources": sources,
        "aria2_available": engine.aria2.live is not None,
        "health": {
            "cpu_percent": sysinfo.get("cpu_percent"),
            "mem_percent": sysinfo.get("mem_percent"),
            "uptime": sysinfo.get("uptime"),
            "temperatures": sysinfo.get("temperatures", [])[:1],
            "self_rss": sysinfo.get("self_rss"),
            "self_cpu_percent": sysinfo.get("self_cpu_percent"),
        },
        "collectors": [{k: c[k] for k in ("name", "label", "state", "message")} for c in engine.collectors_status()],
        "database": {k: v for k, v in engine.store.status().items() if k in ("last_flush", "last_flush_error", "size_bytes", "recovered_from", "journal_mode")},
        "quota": engine.quota_status(now),
        "wan_interfaces": engine.wan.status.extra.get("interfaces", []),
        "timezone": engine.tz_desc,
    }


SERIES = {"wan", "aria2", "dev"}


def history(engine, params):
    series = params.get("series", "wan")
    if series not in SERIES:
        raise ApiError(400, "unknown series")
    key = params.get("key") or "*"
    coverage_key = None
    if series == "dev" and key != "*":
        key = norm_mac(key)
        if not key:
            raise ApiError(400, "invalid device MAC address")
        coverage_key = "*"
    elif series == "wan" and key != "*" and not re.match(r"^[A-Za-z0-9_.@:-]{1,15}$", key):
        raise ApiError(400, "invalid interface")
    name, a, b, da, db = _period(engine, params)
    now = engine.now()
    return {
        "series": series,
        "key": key,
        "period": _period_meta(name, a, b, da, db, engine),
        "summary": analytics.summary(engine.store, series, key, a, b, now, engine.first_ts(), coverage_key),
        "chart": _chart(engine, series, key, a, b, coverage_key),
    }


def history_csv(engine, params):
    data = history(engine, params)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["# BandWatch %s export" % __version__, "series=%s" % data["series"], "key=%s" % data["key"], "timezone=%s" % data["period"]["timezone"], "simulated=%s" % ("yes" if engine.service.demo else "no")])
    w.writerow(["start_local", "end_local", "download_bytes", "upload_bytes", "coverage_percent", "peak_download_bytes_per_s", "peak_upload_bytes_per_s"])
    for p in data["chart"]["points"]:
        if p["future"]:
            continue
        w.writerow(
            [
                dt.datetime.fromtimestamp(p["t"], engine.tz).isoformat(timespec="minutes"),
                dt.datetime.fromtimestamp(p["end"], engine.tz).isoformat(timespec="minutes"),
                "" if p["rx"] is None else p["rx"],
                "" if p["tx"] is None else p["tx"],
                "" if p["coverage"] is None else round(p["coverage"] * 100, 1),
                "" if p["peak_rx"] is None else p["peak_rx"],
                "" if p["peak_tx"] is None else p["peak_tx"],
            ]
        )
    s = data["summary"]
    w.writerow([])
    w.writerow(["total_download_bytes", s["rx"] if s["rx"] is not None else ""])
    w.writerow(["total_upload_bytes", s["tx"] if s["tx"] is not None else ""])
    w.writerow(["measured_seconds", s["measured_s"]])
    w.writerow(["coverage_percent", "" if s["coverage"] is None else round(s["coverage"] * 100, 1)])
    return out.getvalue(), "bandwatch-%s-%s-%s.csv" % (data["series"], data["period"]["start"], data["period"]["end"])


def devices(engine, params):
    name, a, b, da, db = _period(engine, params)
    rows, cov = engine.device_table(a, b)
    q = (params.get("q") or "").strip().lower()[:64]
    if q:
        rows = [r for r in rows if any(q in str(r.get(k) or "").lower() for k in ("name", "hostname", "ip4", "ip6", "mac"))]
    return {
        "period": _period_meta(name, a, b, da, db, engine),
        "devices": rows[:512],
        "coverage": cov,
        "provider": engine.devices.status.as_dict(),
        "measures_lan": bool(getattr(engine.devices, "measures_lan", False)),
        "direction_check": engine.direction_verdict(),
    }


def device_detail(engine, mac, params):
    mac = norm_mac(mac)
    if not mac:
        raise ApiError(400, "invalid device MAC address")
    name, a, b, da, db = _period(engine, params)
    rows, _ = engine.device_table(a, b)
    info = next((r for r in rows if r["mac"] == mac), None)
    if info is None:
        raise ApiError(404, "unknown device")
    now = engine.now()
    return {
        "device": info,
        "period": _period_meta(name, a, b, da, db, engine),
        "summary": analytics.summary(engine.store, "dev", mac, a, b, now, engine.first_ts(), "*"),
        "lan_summary": analytics.summary(engine.store, "devl", mac, a, b, now, engine.first_ts(), None),
        "chart": _chart(engine, "dev", mac, a, b, "*"),
        "provider": engine.devices.status.as_dict(),
    }


def set_alias(engine, mac, body):
    mac = norm_mac(mac)
    if not mac:
        raise ApiError(400, "invalid device MAC address")
    alias = body.get("alias") if isinstance(body, dict) else None
    if alias is None:
        alias = ""
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        raise ApiError(400, "alias must be up to 48 characters without control characters or quotes")
    engine.store.set_alias(mac, alias.strip())
    return {"ok": True, "mac": mac, "alias": alias.strip() or None}


def aria2(engine, params):
    name, a, b, da, db = _period(engine, params)
    now = engine.now()
    status = engine.aria2.status.as_dict()
    live = engine.aria2.live
    recent = engine.store.recent_tasks(40)
    return {
        "available": live is not None,
        "status": status,
        "secret_configured": engine.secrets.has("aria2_secret"),
        "live": live,
        "points": engine.aria2_live.since(int(now * 1000) - engine.settings["ui.live_window"] * 1000),
        "period": _period_meta(name, a, b, da, db, engine),
        "summary": analytics.summary(engine.store, "aria2", "*", a, b, now, engine.first_ts()),
        "chart": _chart(engine, "aria2", "*", a, b),
        "recent": recent,
        "events": [dict(zip(("t", "level", "source", "kind", "message"), e)) for e in engine.recent_events if e[2] == "aria2"][:20],
        "note": "aria2 figures are payload bytes reported by aria2 (an estimate of its share of WAN traffic), not a separate WAN measurement.",
    }


def health(engine, params=None):
    return engine.health()


def settings_get(engine, params=None):
    names = sorted(n for n in engine.p.listdir("/sys/class/net") if n != "lo")[:64]
    return {
        "settings": engine.settings.as_dict(),
        "restart_required_keys": sorted(cfgmod.RESTART_KEYS),
        "secrets": {"aria2_secret": engine.secrets.has("aria2_secret")},
        "service": {
            "port": engine.service.port,
            "listen": engine.service.listen,
            "auth": engine.service.auth,
            "data_dir": engine.service.data_dir,
            "demo": engine.service.demo,
        },
        "interfaces": names,
        "timezone_resolved": engine.tz_desc,
        "wan_detected": [i["device"] for i in engine.wan.status.extra.get("interfaces", [])],
        "version": __version__,
    }


def settings_update(engine, body):
    if not isinstance(body, dict):
        raise ApiError(400, "expected an object")
    errors = engine.update_settings(body)
    if errors:
        raise ApiError(422, "; ".join("%s: %s" % kv for kv in sorted(errors.items())))
    return {"ok": True, "settings": engine.settings.as_dict()}


def set_secret(engine, body):
    secret = body.get("secret") if isinstance(body, dict) else None
    if secret is None or not isinstance(secret, str) or len(secret) > 256 or any(ord(c) < 32 for c in secret):
        raise ApiError(400, "invalid secret")
    engine.set_aria2_secret(secret)
    return {"ok": True, "secret_configured": bool(secret)}


def vnstat(engine, params=None):
    data = engine.vnstat.read()
    if not data:
        return {"available": False, "status": engine.vnstat.status.as_dict()}
    now = engine.now()
    today = tzmod.local_date(now, engine.tz)
    wans = [i["device"] for i in engine.wan.status.extra.get("interfaces", [])]
    out = []
    for name, itf in data["interfaces"].items():
        if wans and name not in wans:
            continue
        day = next((r for r in itf["days"] if (r["y"], r["m"], r["d"]) == (today.year, today.month, today.day)), None)
        mon = next((r for r in itf["months"] if (r["y"], r["m"]) == (today.year, today.month)), None)
        out.append({"interface": name, "today": day, "month": mon})
    a, b, _, _ = tzmod.period_bounds("today", now, engine.tz)
    ma, mb, _, _ = tzmod.period_bounds("this_month", now, engine.tz)
    first = engine.first_ts()
    return {
        "available": True,
        "status": engine.vnstat.status.as_dict(),
        "interfaces": out,
        "bandwatch": {
            "today": analytics.summary(engine.store, "wan", "*", a, b, now, first),
            "month": analytics.summary(engine.store, "wan", "*", ma, mb, now, first),
        },
        "note": "vnStat keeps its own counters and may use a different day boundary; small differences are expected.",
    }


def export(engine, params):
    """Daily totals for all series (JSON or CSV)."""
    name, a, b, da, db = _period(engine, dict(params, period=params.get("period", "this_year")))
    rows = []
    stored = engine.store.devices()
    edges = []
    d = da
    while d <= db + analytics.ONE_DAY:
        edges.append(tzmod.local_midnight(d, engine.tz))
        d += analytics.ONE_DAY
    for series in ("wan", "dev", "devl", "aria2"):
        keys = {"*"} if series in ("wan", "aria2") else (engine.store.keys_seen(series, a, b) or set())
        for key in sorted(keys):
            buckets = engine.store.buckets(series, key, edges)
            for i, bk in enumerate(buckets):
                if bk[stmod.COV] == 0 and bk[stmod.RX] == 0 and bk[stmod.TX] == 0:
                    continue
                rows.append(
                    {
                        "date": tzmod.local_date(edges[i], engine.tz).isoformat(),
                        "series": series,
                        "key": key,
                        "name": engine.device_name(key, stored) if series in ("dev", "devl") and key != "*" else None,
                        "download_bytes": bk[stmod.RX],
                        "upload_bytes": bk[stmod.TX],
                        "measured_seconds": round(bk[stmod.COV] / 1000.0, 1),
                    }
                )
    if params.get("format") == "csv":
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["date", "series", "key", "name", "download_bytes", "upload_bytes", "measured_seconds"])
        for r in rows:
            w.writerow([r["date"], r["series"], r["key"], r["name"] or "", r["download_bytes"], r["upload_bytes"], r["measured_seconds"]])
        return out.getvalue(), "bandwatch-export-%s-%s.csv" % (da.isoformat(), db.isoformat())
    return {"version": __version__, "simulated": engine.service.demo, "timezone": engine.tz_desc, "period": _period_meta(name, a, b, da, db, engine), "rows": rows}, None


def events(engine, params):
    try:
        limit = max(1, min(200, int(params.get("limit", 50))))
    except ValueError:
        raise ApiError(400, "invalid limit") from None
    source = params.get("source")
    if source and not re.match(r"^[a-z0-9_]{1,16}$", source):
        raise ApiError(400, "invalid source")
    keys = ("t", "level", "source", "kind", "message")
    pending = [dict(zip(keys, e)) for e in reversed(engine.pending_events) if not source or e[2] == source]
    return {"events": (pending + engine.store.events(limit, source))[:limit]}
