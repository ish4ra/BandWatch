"""vnStat reconciliation and accounting-limitation checks."""

import json
import time

from .. import config as cfgmod
from . import DISABLED, OK, UNAVAILABLE, Status

VNSTAT_CACHE = 600.0


class VnstatReader:
    """Reads ``vnstat --json`` lazily (only when the dashboard asks).

    vnStat's database may live on external storage; polling it in the
    background could keep a sleeping hard drive awake, so BandWatch only
    queries it on demand and caches the answer for 10 minutes.
    """

    def __init__(self, platform, settings):
        self.p = platform
        self.settings = settings
        self.status = Status("vnstat", "vnStat history")
        self.status.provider = "vnstat"
        self.cache = None
        self.cache_t = 0.0
        if settings["vnstat.mode"] == "off":
            self.status.set(DISABLED, "vnStat integration disabled")
        elif not platform.which("vnstat"):
            self.status.set(UNAVAILABLE, "vnStat is not installed (optional)")
        else:
            self.status.set(OK, "Available (queried on demand)")

    def available(self):
        return self.settings["vnstat.mode"] != "off" and bool(self.p.which("vnstat"))

    def read(self, now=None):
        now = now or time.monotonic()
        if not self.available():
            return None
        if self.cache is not None and now - self.cache_t < VNSTAT_CACHE:
            return self.cache
        out = self.p.run(["vnstat", "--json"], timeout=10.0)
        if not out:
            self.status.set(UNAVAILABLE, "vnstat did not return data")
            return None
        try:
            doc = json.loads(out)
        except ValueError:
            self.status.set(UNAVAILABLE, "vnstat output not understood")
            return None
        self.cache = parse_vnstat(doc)
        self.cache_t = now
        self.status.ok("vnStat %s" % (self.cache.get("version") or ""))
        self.status.version = self.cache.get("version")
        return self.cache


def parse_vnstat(doc):
    """Normalise vnStat 2.x JSON into {version, interfaces: {name: {days, months}}}."""
    out = {"version": str(doc.get("vnstatversion", ""))[:16] or None, "interfaces": {}}
    jver = str(doc.get("jsonversion", "2"))
    scale = 1024 if jver == "1" else 1  # jsonversion 1 reported KiB
    for itf in doc.get("interfaces", [])[:32]:
        name = str(itf.get("name") or itf.get("id") or "")[:15]
        traffic = itf.get("traffic") or {}

        def rows(key, traffic=traffic):
            res = []
            for r in traffic.get(key) or traffic.get(key + "s") or []:
                d = r.get("date") or {}
                try:
                    res.append({"y": int(d["year"]), "m": int(d["month"]), "d": int(d.get("day", 1)), "rx": int(r["rx"]) * scale, "tx": int(r["tx"]) * scale})
                except (KeyError, TypeError, ValueError):
                    continue
            return res

        out["interfaces"][name] = {"days": rows("day"), "months": rows("month")}
    return out


def offload_notes(platform):
    """Describe settings known to affect per-device accounting accuracy."""
    notes = []
    fw = cfgmod.uci_section(cfgmod.parse_uci(platform.read_text("/etc/config/firewall") or ""), "defaults")
    if fw:
        if fw.get("flow_offloading") == "1":
            notes.append("Software flow offloading is enabled: interface counters remain accurate, but conntrack-based per-device providers (nlbwmon) may under-count offloaded flows.")
        if fw.get("flow_offloading_hw") == "1":
            notes.append("Hardware flow offloading is enabled: offloaded packets may bypass the CPU, so per-device providers (and on some targets WAN counters) can under-report traffic.")
    nss = platform.listdir("/sys/kernel/debug/qca-nss-drv") if platform.exists("/sys/kernel/debug/qca-nss-drv") else []
    if nss:
        notes.append("Qualcomm NSS offload detected: offloaded traffic may not be visible to software counters.")
    return notes
