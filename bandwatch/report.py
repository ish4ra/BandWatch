"""Sanitised diagnostic report (one-click export from the dashboard).

Privacy model: the report is **built from an explicit allow-list**. Every
field is copied individually and passed through a validator that only
accepts the expected shape (an enum value, a number, a short version-like
token). Nothing is copied wholesale from collector, settings or system
objects, and free text from providers (error messages, task names,
hostnames, URLs, paths) never enters the report: collectors expose a
machine-readable ``code`` and the descriptions below are fixed strings.

Excluded by construction: passwords, RPC secrets, session/CSRF tokens, IP
and MAC addresses, device hostnames and aliases, Wi-Fi SSIDs and keys,
PPPoE/APN credentials, URLs, download/file names, filesystem paths,
configuration dumps and arbitrary provider messages.

Every check result is one of: passed, failed, warning, insufficient_data,
not_tested, unavailable. A check is only "passed" when BandWatch measured
the condition itself.
"""

import datetime as dt
import io
import ipaddress
import json
import re
import time
import urllib.parse
import zipfile

from . import __version__, analytics
from . import store as stmod

SCHEMA = "bandwatch-diagnostic-report"
SCHEMA_VERSION = 1

PASSED, FAILED, WARNING, INSUFFICIENT, NOT_TESTED, UNAVAILABLE = ("passed", "failed", "warning", "insufficient_data", "not_tested", "unavailable")
STATUSES = (PASSED, FAILED, WARNING, INSUFFICIENT, NOT_TESTED, UNAVAILABLE)
STATUS_LABEL = {PASSED: "PASSED", FAILED: "FAILED", WARNING: "WARNING", INSUFFICIENT: "INSUFFICIENT DATA", NOT_TESTED: "NOT TESTED", UNAVAILABLE: "UNAVAILABLE"}

CPU_TARGET = 2.0
RSS_TARGET = 30 * 1024 * 1024

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~:-]{0,39}$")
_IFNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,14}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+/,-]{0,63}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_IPLIKE = re.compile(r"\d+\.\d+\.\d+\.\d+|[0-9a-fA-F]{1,4}(:[0-9a-fA-F]{0,4}){2,}")

STATES = {"ok", "degraded", "error", "unavailable", "disabled", "starting"}
PROTOS = {
    "dhcp", "static", "pppoe", "pppoa", "ppp", "qmi", "mbim", "ncm", "3g", "modemmanager", "wwan", "dhcpv6",
    "directip", "hnet", "uqmi", "6in4", "6to4", "6rd", "dslite", "464xlat", "wireguard", "gre", "vxlan", "map", "l2tp", "pptp", "relay",
}

# Fixed descriptions for collector state codes. Unknown codes are reported
# as the code only.
CODE_TEXT = {
    "ok": "Working.",
    "starting": "Starting; no result yet.",
    "disabled": "Disabled in settings.",
    "on_demand": "Available; queried only when the dashboard asks.",
    "not_installed": "Optional tool not installed.",
    "no_data": "The tool returned no data.",
    "bad_output": "Output not understood.",
    "bad_response": "The provider returned an unexpected response.",
    "not_reachable": "Provider not reachable at the configured local address.",
    "not_running": "Provider not running or not reachable at the configured local address.",
    "request_failed": "A request to the provider failed.",
    "rpc_unreachable": "The RPC endpoint stopped answering.",
    "auth_rejected": "The provider rejected the request (RPC secret missing or wrong).",
    "unsupported_version": "Provider response has no device list (unsupported version?).",
    "no_wan_split": "Provider does not separate Internet and local traffic.",
    "conntrack_estimates": "Working with conntrack-based estimates (nlbwmon).",
    "no_provider": "No per-device provider found; WAN monitoring is unaffected.",
    "wifi_lists_unavailable": "Wi-Fi station lists unavailable; connection types may be incomplete.",
    "counters_unreadable": "Interface counters could not be read.",
    "no_default_route": "No WAN interface with a default route was found.",
    "baseline": "Establishing a counter baseline.",
    "device_missing": "A WAN device is temporarily missing (reconnecting?).",
    "error": "Error.",
    "unavailable": "Unavailable.",
    "degraded": "Working with reduced capability.",
}


# -- validators ------------------------------------------------------------------


def _tok(v, rx=_TOKEN):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not rx.match(v) or _IPLIKE.search(v):
        return None
    return v


def _enum(v, allowed, default=None):
    return v if isinstance(v, str) and v in allowed else default


def _num(v, nd=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(v, nd) if nd is not None else v


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bool(v):
    return bool(v) if isinstance(v, bool) else None


def _age(ts, now):
    return int(now - ts) if isinstance(ts, (int, float)) and ts > 0 else None


def address_scope(host):
    """'loopback', 'lan', 'link_local', 'wildcard', 'public' or 'name' - never the address."""
    h = (host or "").strip().strip("[]").split("%")[0].lower()
    if h in ("localhost",):
        return "loopback"
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "name" if h else "unknown"
    if ip.is_unspecified:
        return "wildcard"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_private:
        return "lan"
    return "public"


def url_scope(url):
    try:
        return address_scope(urllib.parse.urlsplit(url or "").hostname or "")
    except ValueError:
        return "unknown"


def _check(cid, title, status, detail):
    if status not in STATUSES:
        raise ValueError("unknown check status %r" % status)
    return {"id": cid, "title": title, "status": status, "detail": detail}


# -- report sections ------------------------------------------------------------------


def _python():
    import platform as pyplat
    import sys

    mods = {}
    for name in ("sqlite3", "ssl", "_hashlib", "zoneinfo", "zlib"):
        try:
            __import__(name)
            mods[name] = True
        except ImportError:
            mods[name] = False
    try:
        import sqlite3

        sqlite_version = _tok(sqlite3.sqlite_version)
    except ImportError:
        sqlite_version = None
    return {"version": _tok(sys.version.split()[0]), "implementation": _tok(pyplat.python_implementation()), "sqlite": sqlite_version, "modules": mods}


def dependency_guidance(mods, https_enabled):
    """Actionable, honest notes about optional Python modules."""
    out = []
    if not mods.get("ssl"):
        if https_enabled:
            out.append({
                "module": "ssl",
                "impact": "HTTPS is enabled but cannot start without the Python ssl module.",
                "action": "Install python3-openssl (apk add python3-openssl / opkg install python3-openssl), or disable 'option https' in /etc/config/bandwatch.",
            })
        else:
            out.append({
                "module": "ssl",
                "impact": "Optional. Only needed for BandWatch's own HTTPS mode. The dashboard works over HTTP on the LAN; the update check runs in your browser.",
                "action": "To use HTTPS, install python3-openssl, then set option https '1'. BandWatch never falls back to an insecure alternative.",
            })
    if not mods.get("_hashlib"):
        out.append({"module": "_hashlib", "impact": "Optional. Password checks use a slower pure-Python PBKDF2 (sign-in may take a moment).", "action": "Install python3-openssl for faster sign-in. No security difference."})
    if not mods.get("zlib"):
        out.append({"module": "zlib", "impact": "Optional. Diagnostic ZIP files are stored uncompressed.", "action": "None required."})
    return out


def _router(engine):
    info = engine.system.info or {}
    b = info.get("board") or engine.system._board() or {}
    pkg = "apk" if engine.p.exists("/lib/apk/db/installed") else ("ipk" if engine.p.exists("/usr/lib/opkg/status") else None)
    return {
        "model": _tok(b.get("model"), _MODEL),
        "openwrt_release": _tok(b.get("release")),
        "openwrt_revision": _tok(b.get("revision")),
        "target": _tok(b.get("target"), re.compile(r"^[a-z0-9_]{1,24}/[a-z0-9_]{1,24}$")),
        "arch": _tok(b.get("arch")),
        "kernel": _tok(b.get("kernel")),
        "cpu_cores": _int(info.get("cpus")),
        "memory_total_bytes": _int(info.get("mem_total")),
        "package_format": pkg,
    }


def _snapshot(engine):
    i = engine.system.info or {}
    return {
        "bandwatch_cpu_percent": _num(i.get("self_cpu_percent"), 2),
        "bandwatch_rss_bytes": _int(i.get("self_rss")),
        "bandwatch_threads": _int(i.get("self_threads")),
        "router_cpu_percent": _num(i.get("cpu_percent"), 1),
        "router_memory_percent": _num(i.get("mem_percent"), 1),
        "router_memory_available_bytes": _int(i.get("mem_available")),
        "router_uptime_s": _int(i.get("uptime")),
        "load_average": [_num(x, 2) for x in (i.get("load") or [])[:3] if isinstance(x, (int, float))],
        "temperatures_celsius": [_num(t.get("celsius"), 1) for t in (i.get("temperatures") or [])[:8] if isinstance(t, dict)],
        "storage": [
            {"label": _enum(x.get("label"), {"Overlay (flash)", "Root", "Temporary (RAM)", "Filesystem holding BandWatch data"}), "total_bytes": _int(x.get("total")), "free_bytes": _int(x.get("free"))}
            for x in (i.get("storage") or [])[:6]
            if isinstance(x, dict)
        ],
        "database_on": _enum(i.get("data_filesystem"), {"Overlay (flash)", "Root", "Temporary (RAM)", "Filesystem holding BandWatch data"}),
    }


def _collector(c, now):
    code = _tok(c.code, _KIND) or "unknown"
    return {
        "name": _enum(c.name, {"wan", "devices", "identity", "aria2", "vnstat", "system"}),
        "state": _enum(c.state, STATES, "unknown"),
        "code": code,
        "description": CODE_TEXT.get(code, "See the dashboard for details."),
        "provider": _enum(c.provider, {"kernel", "bandix", "nlbwmon", "aria2", "vnstat", "procfs", "ubus", "dnsmasq", "arp"}) or _tok(c.provider),
        "version": _tok(c.version),
        "error_count": _int(c.error_count),
        "last_ok_age_s": _age(c.last_ok, now),
        "last_error_age_s": _age(c.last_error, now),
    }


def _wan(engine):
    out = []
    for itf in list(engine.wan.ifaces.values())[:8]:
        i = itf.describe()
        out.append({
            "device": _tok(i.get("device"), _IFNAME),
            "proto": _enum(i.get("proto"), PROTOS),
            "families": [f for f in (i.get("families") or []) if f in ("ipv4", "ipv6")],
            "present": _bool(i.get("present")),
            "tunnel": _bool(i.get("tunnel")),
        })
    return {
        "detection_source": _enum(engine.wan.source, {"override", "ubus", "routes"}),
        "interface_count": len(engine.wan.ifaces),
        "interfaces": out,
        "ignored_tunnel_devices": len(engine.wan.ignored),
        "state": _enum(engine.wan.status.state, STATES, "unknown"),
    }


def _coverage(engine, now):
    first = engine.first_ts()
    out = {}
    for name, (a, b) in {"last_hour": (now - 3600, now), "last_24h": (now - 86400, now)}.items():
        row = {}
        for series, key, ckey in (("wan", "*", None), ("dev", "*", None), ("aria2", "*", None)):
            try:
                s = analytics.summary(engine.store, series, key, a, b, now, first, ckey)
                row[series] = _num(s.get("coverage"), 4)
            except Exception:
                row[series] = None
        out[name] = row
    return out


def _persistence(p):
    keep_int = (
        "service_started", "service_uptime_s", "router_uptime_s", "database_opened_at", "database_open_ms", "flush_interval_s",
        "first_flush_delay_s", "next_flush_in_s", "last_flush_attempt", "last_successful_save", "last_save_buckets", "flush_count",
        "flush_failures", "pending_buckets", "prebuffered_buckets", "oldest_saved_sample", "schema_version", "size_bytes",
    )
    out = {k: _int(p.get(k)) for k in keep_int}
    out["state"] = _enum(p.get("state"), {"ok", "collecting", "waiting_for_clock", "failing", "overdue", "no_samples"}, "unknown")
    out["last_flush_error_code"] = _enum(p.get("last_flush_error_code"), {"disk_full", "read_only", "locked", "corrupt", "io_error", "other"})
    out["clock_trusted"] = _bool(p.get("clock_trusted"))
    cw = p.get("clock_wait") or {}
    out["clock_wait"] = {"reason": _enum(cw.get("reason"), {"clock_not_set", "waiting_after_boot", "checking"}), "remaining_s": _int(cw.get("remaining_s"))} if cw else None
    out["history_predates_this_start"] = _bool(p.get("history_predates_this_start"))
    out["integrity_check"] = _enum(p.get("integrity_check"), {"ok", "recreated", "unknown"})
    out["recovered_from_corruption"] = _bool(p.get("recovered_from_corruption"))
    out["journal_mode"] = _enum(p.get("journal_mode"), {"wal", "delete", "memory", "truncate", "persist"})
    out["rows"] = {k: _int(v) for k, v in (p.get("rows") or {}).items() if k in ("m1", "m15", "d1", "devices", "events", "tasks")}
    return out


def _lifecycle(engine):
    def entry(e):
        return {
            "age_s": _age(e.get("t"), time.time()),
            "kind": _enum(e.get("kind"), {"first_start", "upgrade", "after_reboot", "restart"}),
            "previous_stop": _enum(e.get("previous_stop"), {"clean", "unclean"}),
            "router_uptime_s": _int(e.get("router_uptime")),
            "version": _tok(e.get("version")),
            "upgraded_from": _tok(e.get("upgraded_from")),
        }

    lc = engine.lifecycle
    hist = [entry(e) for e in lc["history"] if isinstance(e, dict)]
    return {
        "starts": _int(lc.get("starts")),
        "current": entry(lc["current"]),
        "history": hist[-LIFECYCLE_EXPORT:],
        "reboots_seen": sum(1 for e in hist if e["kind"] == "after_reboot"),
        "unclean_stops_seen": sum(1 for e in hist if e["previous_stop"] == "unclean"),
    }


LIFECYCLE_EXPORT = 20


def _aria2(engine):
    st = engine.aria2.status
    secret = engine.secrets.has("aria2_secret")
    connected = st.state == "ok"
    uncounted = engine.aria2.uncounted or {}
    return {
        "mode": _enum(engine.settings["aria2.mode"], {"auto", "on", "off"}),
        "endpoint_scope": url_scope(engine.settings["aria2.url"]),
        "state": _enum(st.state, STATES, "unknown"),
        "code": _tok(st.code, _KIND),
        "version": _tok(st.version),
        "rpc_connected": connected,
        "secret_configured": secret,
        "accepts_requests_without_secret": True if (connected and not secret) else None,
        "active_tasks": _int((engine.aria2.live or {}).get("active")),
        "waiting_tasks": _int((engine.aria2.live or {}).get("waiting")),
        "uncounted_preexisting_tasks": _int(uncounted.get("tasks")),
        "accounting_accuracy": NOT_TESTED,
    }


def _listening(engine):
    svc = engine.service
    scopes = sorted({address_scope(a) for a in engine.bind_addresses}) if engine.bind_addresses else []
    tokens = [t for t in svc.listen if t in ("lan", "loopback", "any")]
    nets = svc.allowed_networks
    return {
        "listen_tokens": tokens,
        "custom_listen_addresses": sum(1 for t in svc.listen if t not in ("lan", "loopback", "any")),
        "bound_scopes": scopes,
        "port": _int(svc.port),
        "auth": _enum(svc.auth, {"password", "none"}),
        "https": bool(svc.https),
        "allowed_networks": {"private": "private" in nets, "any": "any" in nets, "custom_count": sum(1 for n in nets if n not in ("private", "any"))},
        "extra_allowed_host_names": len(getattr(svc, "allowed_hosts", []) or []),
        "allow_lan_providers": bool(getattr(svc, "allow_lan_providers", False)),
    }


def _settings(engine):
    s = engine.settings
    ints = (
        "wan.sample_interval", "wan.max_rate_mbps", "devices.poll_interval", "aria2.poll_interval", "storage.flush_interval",
        "storage.minute_retention_days", "storage.quarter_retention_days", "storage.device_quarter_retention_days",
        "storage.day_retention_days", "ui.live_window",
    )
    out = {k: _int(s[k]) for k in ints}
    out["wan.interfaces_override_count"] = len(s["wan.interfaces"])
    out["devices.provider"] = _enum(s["devices.provider"], {"auto", "bandix", "nlbwmon", "none"})
    out["devices.bandix_direction"] = _enum(s["devices.bandix_direction"], {"rx_is_download", "tx_is_download"})
    out["devices.bandix_endpoint_scope"] = url_scope(s["devices.bandix_url"])
    out["vnstat.mode"] = _enum(s["vnstat.mode"], {"auto", "off"})
    out["quota.enabled"] = bool(s["quota.enabled"])
    out["general.timezone_is_system"] = s["general.timezone"] == "system"
    return out


def _events(engine, now):
    with engine.events_lock:
        items = list(engine.recent_events)
    try:
        stored = engine.store.events(200)
    except Exception:
        stored = []
    seen = {}
    rows = [(e[0], e[1], e[2], e[3]) for e in items] + [(e["t"], e["level"], e["source"], e["kind"]) for e in stored]
    for t, level, source, kind in rows:
        kind = _tok(kind, _KIND) or "other"
        source = _tok(source, _KIND) or "other"
        key = "%s.%s" % (source, kind)
        seen[key] = seen.get(key, 0) + 1
    recent = [
        {"age_s": _age(t, now), "level": _enum(level, {"info", "warning", "error"}, "info"), "source": _tok(source, _KIND) or "other", "kind": _tok(kind, _KIND) or "other"}
        for t, level, source, kind in sorted(set(rows), key=lambda r: -r[0])[:30]
    ]
    return {"counts_by_kind": dict(sorted(seen.items())), "recent": recent, "note": "Event messages are not exported; only their source and kind."}


# -- checks -----------------------------------------------------------------------------


COLLECTOR_STATUS = {"ok": PASSED, "degraded": WARNING, "error": FAILED, "unavailable": UNAVAILABLE, "disabled": NOT_TESTED, "starting": INSUFFICIENT}


def _checks(r):
    c = []
    py = r["runtime"]
    c.append(_check("runtime", "Python runtime and required modules", PASSED if py["python"]["modules"].get("sqlite3") else FAILED, "Python %s, SQLite %s." % (py["python"]["version"], py["python"]["sqlite"])))
    for col in r["collectors"]:
        c.append(_check("collector_" + str(col["name"]), "Collector: %s" % col["name"], COLLECTOR_STATUS.get(col["state"], INSUFFICIENT), col["description"]))
    wan = r["wan"]
    if wan["interface_count"] and any(i["present"] for i in wan["interfaces"]):
        c.append(_check("wan_detection", "WAN interface detected", PASSED, "%d WAN device(s) via %s." % (wan["interface_count"], wan["detection_source"])))
    elif wan["interface_count"]:
        c.append(_check("wan_detection", "WAN interface detected", WARNING, "WAN device configured but not present right now."))
    else:
        c.append(_check("wan_detection", "WAN interface detected", FAILED, "No WAN interface with a default route was found."))
    p = r["persistence"]
    pmap = {"ok": PASSED, "collecting": INSUFFICIENT, "waiting_for_clock": INSUFFICIENT, "failing": FAILED, "overdue": WARNING, "no_samples": WARNING}
    c.append(_check("persistence", "History is saved to storage", pmap.get(p["state"], INSUFFICIENT), {
        "ok": "Last successful save %s s ago." % (r["generated_ts"] - p["last_successful_save"] if p["last_successful_save"] else "?"),
        "collecting": "Normal start-up: nothing saved yet; first save in about %s s." % p["next_flush_in_s"],
        "waiting_for_clock": "Samples are held in memory until the router clock is trustworthy.",
        "failing": "The last save failed (%s)." % p["last_flush_error_code"],
        "overdue": "Saving is overdue.",
        "no_samples": "No samples collected yet.",
    }.get(p["state"], "Unknown.")))
    lc = r["service"]["lifecycle"]
    restarted = any(e["kind"] == "restart" for e in lc["history"][:-1]) or lc["current"]["kind"] == "restart"
    rebooted = lc["current"]["kind"] == "after_reboot" or lc["reboots_seen"] > 0
    survived = p["history_predates_this_start"]
    if restarted or rebooted:
        c.append(_check("history_across_restart", "History survives a restart or reboot", PASSED if survived else (WARNING if p["state"] == "ok" else INSUFFICIENT),
                        "Data saved before the current start is still present." if survived else "No data from before the current start was found."))
    else:
        c.append(_check("history_across_restart", "History survives a restart or reboot", NOT_TESTED, "No restart or reboot has happened since BandWatch began recording its lifecycle."))
    c.append(_check("reboot_recovery", "Recovery after a router reboot", (PASSED if survived else INSUFFICIENT) if rebooted else NOT_TESTED,
                    "A reboot was detected%s." % (" and earlier history is intact" if survived else "; earlier history not yet confirmed") if rebooted else "No router reboot has been observed by this installation."))
    if lc["unclean_stops_seen"]:
        c.append(_check("clean_shutdown", "Service stops cleanly", WARNING, "%d start(s) followed a run that did not shut down cleanly (crash, forced stop or power loss)." % lc["unclean_stops_seen"]))
    else:
        c.append(_check("clean_shutdown", "Service stops cleanly", PASSED if lc["starts"] and lc["starts"] > 1 else NOT_TESTED, "Every recorded stop was clean." if lc["starts"] and lc["starts"] > 1 else "No stop has been recorded yet."))
    c.append(_check("clock", "Router clock trusted", PASSED if p["clock_trusted"] else INSUFFICIENT, "Clock trusted." if p["clock_trusted"] else "Waiting for a trustworthy clock."))
    d = r["bandix_direction"]
    dmap = {"consistent": PASSED, "suspect_swapped": WARNING, "insufficient_data": INSUFFICIENT, "inconclusive": INSUFFICIENT, "not_applicable": NOT_TESTED}
    c.append(_check("bandix_direction", "Bandix download/upload direction", dmap.get(d["state"], INSUFFICIENT), {
        "consistent": "Directions agree with the WAN counters (%d samples)." % d["samples"],
        "suspect_swapped": "Directions look inverted; review 'Bandix direction' in Settings. Not changed automatically.",
        "insufficient_data": "Pending: %d of %d usable samples (needs clearly one-directional traffic from a LAN device)." % (d["samples"], d["required"]),
        "inconclusive": "Samples do not clearly confirm either direction yet.",
        "not_applicable": "Bandix is not the active per-device provider.",
    }.get(d["state"], "Unknown.")))
    a = r["aria2"]
    if a["mode"] == "off":
        c.append(_check("aria2_rpc", "aria2 RPC connectivity", NOT_TESTED, "aria2 integration is disabled."))
    elif a["rpc_connected"]:
        c.append(_check("aria2_rpc", "aria2 RPC connectivity", PASSED, "Connected (read-only)%s." % ("" if a["secret_configured"] else " without an RPC secret")))
    elif a["code"] == "auth_rejected":
        c.append(_check("aria2_rpc", "aria2 RPC connectivity", FAILED, "aria2 rejected BandWatch's request: the RPC secret is missing or wrong."))
    else:
        c.append(_check("aria2_rpc", "aria2 RPC connectivity", UNAVAILABLE, "aria2 not reachable at the configured local endpoint."))
    if a["accepts_requests_without_secret"]:
        c.append(_check("aria2_rpc_secret", "aria2 RPC protected by a secret", WARNING,
                        "aria2 answered without an RPC secret. BandWatch did not test whether the RPC port is reachable from other devices; "
                        "if aria2 listens on all interfaces (rpc-listen-all=true), set rpc-secret in aria2 and enter it in Settings. "
                        "BandWatch never changes aria2's configuration."))
    c.append(_check("aria2_accounting", "aria2 download accounting accuracy", NOT_TESTED, "Connectivity is not the same as verified accounting; compare against a known download to verify."))
    res = r["resources"]["summaries"]
    best = next((res[k] for k in ("last_24h", "last_hour") if res[k]["duration_s"] >= 3600), None)
    if best and best["cpu_avg_percent"] is not None:
        c.append(_check("cpu_target", "BandWatch average CPU below %.0f%%" % CPU_TARGET, PASSED if best["cpu_avg_percent"] < CPU_TARGET else WARNING,
                        "Average %.2f%% over %d min of normal operation (not an idle benchmark)." % (best["cpu_avg_percent"], best["duration_s"] // 60)))
    else:
        c.append(_check("cpu_target", "BandWatch average CPU below %.0f%%" % CPU_TARGET, INSUFFICIENT, "Needs at least one hour of self-monitoring."))
    if best and best["rss_peak_bytes"] is not None:
        c.append(_check("rss_target", "BandWatch memory below 30 MiB", PASSED if best["rss_peak_bytes"] < RSS_TARGET else WARNING, "Peak RSS %.1f MiB." % (best["rss_peak_bytes"] / 1048576.0)))
    else:
        c.append(_check("rss_target", "BandWatch memory below 30 MiB", INSUFFICIENT, "Needs at least one hour of self-monitoring."))
    if best and best["missed_ratio"] is not None:
        c.append(_check("sampling", "WAN sampling on schedule", PASSED if best["missed_ratio"] < 0.01 else WARNING, "%d of %d samples missed." % (best["wan_samples_missed"], best["wan_samples"] + best["wan_samples_missed"])))
    else:
        c.append(_check("sampling", "WAN sampling on schedule", INSUFFICIENT, "Not enough sampling history yet."))
    loop = r["service"]["loop_errors"]
    c.append(_check("loop_errors", "No internal loop errors", PASSED if not loop else WARNING, "%d loop error(s) since the service started." % loop))
    ls = r["listening"]
    risky = "wildcard" in ls["bound_scopes"] or "public" in ls["bound_scopes"] or ls["allowed_networks"]["any"]
    if not ls["bound_scopes"]:
        c.append(_check("listening", "Dashboard reachable only locally", NOT_TESTED, "Listening addresses unknown (report generated outside the running service)."))
    else:
        c.append(_check("listening", "Dashboard reachable only locally", WARNING if risky or ls["auth"] == "none" else PASSED,
                        "Bound to: %s; authentication: %s." % (", ".join(ls["bound_scopes"]), ls["auth"]) + (" Listening on all addresses: make sure the firewall blocks the port from the WAN." if risky else "")))
    mods = r["runtime"]["python"]["modules"]
    if ls["https"]:
        c.append(_check("https", "BandWatch HTTPS", PASSED if mods.get("ssl") else FAILED, "HTTPS enabled." if mods.get("ssl") else "HTTPS enabled but the Python ssl module is missing."))
    else:
        c.append(_check("https", "BandWatch HTTPS", NOT_TESTED if mods.get("ssl") else UNAVAILABLE, "HTTPS not enabled (optional)." + ("" if mods.get("ssl") else " Python ssl module not installed.")))
    c.append(_check("traffic_accuracy", "Traffic totals match an independent meter", NOT_TESTED, "Requires comparing against the ISP or another meter over a known period."))
    c.append(_check("wan_isolation", "LAN-only traffic excluded from WAN totals", NOT_TESTED, "Requires a controlled LAN-only transfer while watching WAN totals."))
    return c


def _unverified(r):
    out = [
        "Traffic-accounting accuracy against an independent meter (ISP portal, vnStat over a full day).",
        "WAN isolation: that LAN-to-LAN transfers never appear in WAN totals on this router.",
        "Sustained idle CPU: resource figures are measured under normal use, not under controlled idle conditions.",
        "aria2 download-accounting accuracy (RPC connectivity alone does not verify it).",
    ]
    checks = {c["id"]: c["status"] for c in r["checks"]}
    if checks.get("reboot_recovery") != PASSED:
        out.append("Recovery of history after a router reboot.")
    if checks.get("history_across_restart") != PASSED:
        out.append("History preserved across a service restart.")
    if checks.get("bandix_direction") != PASSED:
        out.append("Bandix download/upload direction.")
    if checks.get("https") != PASSED:
        out.append("BandWatch's own HTTPS mode.")
    return out


# -- public API ----------------------------------------------------------------------------


def build(engine):
    now = time.time()
    py = _python()
    r = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.fromtimestamp(int(now), dt.timezone.utc).isoformat(),
        "generated_ts": int(now),
        "privacy": {
            "contains": "Versions, router model and firmware, collector states and error codes, resource summaries, database health, check results.",
            "never_contains": "Passwords, RPC secrets, session tokens, IP or MAC addresses, device names or aliases, Wi-Fi names or keys, PPPoE/APN credentials, URLs, download names, file paths, configuration dumps, provider messages.",
            "stays_local": "BandWatch never uploads this report. You decide whether to share it.",
        },
        "bandwatch": {"version": __version__, "mode": "demo" if engine.service.demo else "production", "database_schema": stmod.MIGRATIONS[-1][0]},
        "router": _router(engine),
        "runtime": {"python": py, "dependency_guidance": dependency_guidance(py["modules"], bool(engine.service.https))},
        "service": {
            "uptime_s": int(now - engine.started_wall),
            "router_uptime_s": _int((engine.system.info or {}).get("uptime")),
            "loop_errors": engine.loop_errors,
            "lifecycle": _lifecycle(engine),
            "timezone_utc_offset_min": _utc_offset(engine, now),
        },
        "resources": {
            "snapshot": _snapshot(engine),
            "summaries": engine.monitor.summaries(now),
            "targets": {"cpu_avg_percent": CPU_TARGET, "rss_bytes": RSS_TARGET},
            "note": "Measured during normal operation, including serving the dashboard. Not an idle benchmark. 'Low-traffic' intervals had under 100 kbit/s average WAN traffic.",
        },
        "collectors": [_collector(c, now) for c in (engine.wan.status, engine.devices.status, engine.identity.status, engine.aria2.status, engine.vnstat.status, engine.system.status)],
        "wan": _wan(engine),
        "devices": {
            "provider": _enum(engine.devices.status.provider, {"bandix", "nlbwmon"}),
            "provider_version": _tok(engine.devices.status.version),
            "identified_count": len(engine.identity.devices),
            "provider_reported_count": len(engine.dev_snaps),
            "measures_lan": bool(getattr(engine.devices, "measures_lan", False)),
        },
        "persistence": _persistence(engine.persistence_status()),
        "coverage": _coverage(engine, now),
        "bandix_direction": _direction(engine.direction_verdict()),
        "aria2": _aria2(engine),
        "vnstat": {"state": _enum(engine.vnstat.status.state, STATES, "unknown"), "code": _tok(engine.vnstat.status.code, _KIND)},
        "listening": _listening(engine),
        "settings": _settings(engine),
        "accounting_notes_count": len(engine.offload_notes),
        "offload": [k for k, txt in (("software_offload", "Software flow"), ("hardware_offload", "Hardware flow"), ("nss", "NSS")) if any(txt in n for n in engine.offload_notes)],
        "sampling": {k: _int(v) for k, v in engine.sample_stats.items()},
        "events": _events(engine, now),
    }
    r["checks"] = _checks(r)
    r["unverified"] = _unverified(r)
    r["summary_counts"] = {s: sum(1 for c in r["checks"] if c["status"] == s) for s in STATUSES}
    return r


def _direction(v):
    return {
        "state": _enum(v.get("state"), {"consistent", "suspect_swapped", "insufficient_data", "inconclusive", "not_applicable"}, "unknown"),
        "samples": _int(v.get("samples")) or 0,
        "required": _int(v.get("required")) or 0,
        "configured": _enum(v.get("configured"), {"rx_is_download", "tx_is_download"}),
        "skipped": {k: _int(n) for k, n in (v.get("skipped") or {}).items() if k in ("low_traffic", "router_traffic", "unattributed", "symmetric")},
        "auto_swap": False,
    }


def _utc_offset(engine, now):
    try:
        off = dt.datetime.fromtimestamp(now, engine.tz).utcoffset()
        return int(off.total_seconds() // 60) if off is not None else None
    except Exception:
        return None


def summary_text(r):
    """Short human-readable summary for the ZIP (plain ASCII)."""
    rt = r["router"]
    res = r["resources"]
    snap = res["snapshot"]
    lines = [
        "BandWatch diagnostic report",
        "===========================",
        "Generated: %s (UTC)   Report schema: %s v%d" % (r["generated_at"], r["schema"], r["schema_version"]),
        "BandWatch %s (%s mode)" % (r["bandwatch"]["version"], r["bandwatch"]["mode"]),
        "Router: %s, OpenWrt %s (%s, %s), kernel %s" % (rt["model"] or "?", rt["openwrt_release"] or "?", rt["target"] or "?", rt["arch"] or "?", rt["kernel"] or "?"),
        "Service uptime: %s   Router uptime: %s   Starts recorded: %s" % (_dur(r["service"]["uptime_s"]), _dur(r["service"]["router_uptime_s"]), r["service"]["lifecycle"]["starts"]),
        "Now: BandWatch CPU %s, RSS %s, threads %s" % (_pct(snap["bandwatch_cpu_percent"]), _mib(snap["bandwatch_rss_bytes"]), snap["bandwatch_threads"]),
    ]
    for name in ("last_hour", "last_24h", "last_7d"):
        s = res["summaries"][name]
        if s["samples"]:
            lines.append("%-9s CPU avg %s / peak %s, RSS avg %s / peak %s, %d samples over %s, collector errors %d" % (
                name, _pct(s["cpu_avg_percent"]), _pct(s["cpu_peak_percent"]), _mib(s["rss_avg_bytes"]), _mib(s["rss_peak_bytes"]), s["samples"], _dur(s["duration_s"]), s["collector_errors"]))
    p = r["persistence"]
    lines.append("Database: %s; last save %s; pending buckets %s" % (p["state"], ("%s s ago" % (r["generated_ts"] - p["last_successful_save"])) if p["last_successful_save"] else "not yet", p["pending_buckets"]))
    lines += ["", "Checks", "------"]
    for c in r["checks"]:
        lines.append("[%-17s] %s - %s" % (STATUS_LABEL[c["status"]], c["title"], c["detail"]))
    lines += ["", "Not verified by this report", "---------------------------"] + ["- " + u for u in r["unverified"]]
    lines += ["", "Privacy: " + r["privacy"]["never_contains"], r["privacy"]["stays_local"], ""]
    return "\n".join(lines)


def _dur(s):
    if not isinstance(s, int):
        return "?"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return ("%dd %dh %dm" % (d, h, m)) if d else ("%dh %dm" % (h, m) if h else "%dm %ds" % (m, s % 60))


def _pct(v):
    return "n/a" if v is None else "%s%%" % v


def _mib(b):
    return "%.1f MiB" % (b / 1048576.0) if isinstance(b, int) else "n/a"


def filename(r, ext):
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(r["generated_ts"]))
    return "bandwatch-report-%s-%s.%s" % (r["bandwatch"]["version"], stamp, ext)


def to_json(r):
    return json.dumps(r, indent=1, sort_keys=False)


def to_zip(r):
    try:
        import zlib  # noqa: F401

        method = zipfile.ZIP_DEFLATED
    except ImportError:
        method = zipfile.ZIP_STORED
    buf = io.BytesIO()
    stamp = time.gmtime(r["generated_ts"])[:6]
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, text in (("bandwatch-report.json", to_json(r)), ("SUMMARY.txt", summary_text(r))):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = method
            info.external_attr = 0o644 << 16
            zf.writestr(info, text)
    return buf.getvalue()
