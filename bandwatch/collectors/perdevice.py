"""Per-device traffic providers.

Bandix (preferred)
    Read-only ``GET /api/system/info`` and ``GET /api/traffic/devices`` on the
    local Bandix service (default ``127.0.0.1:8686``). Bandix 0.12.x returns
    ``data.d[]`` entries with short keys: ``mac``, ``ip4``, ``ip6``, ``host``,
    ``conn``, ``uplink``, ``w_freq``, cumulative bytes ``w_rx_b``/``w_tx_b``
    (WAN / Internet), ``l_rx_b``/``l_tx_b`` (LAN-local) and rates ``*_r``
    (bytes/s). BandWatch never calls the write endpoints (limits, quotas,
    bindings, deletes).

    Bandix does not document whether ``rx`` means "received by the device"
    or "received by the router". BandWatch assumes ``rx`` = device download
    and exposes the ``devices.bandix_direction`` setting; the engine also
    cross-checks against WAN counters and reports a suspected inversion.

nlbwmon (fallback)
    ``nlbw -c json -g mac`` (conntrack accounting). Totals reset at each
    nlbwmon accounting period; resets are handled as counter resets.
    Offloaded flows may be under-counted.
"""

import re

from .. import counters
from . import DEGRADED, DISABLED, ERROR, UNAVAILABLE, Backoff, Status

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
DEVICE_TTL = 900.0  # forget counter state for devices not reported for 15 min


def norm_mac(mac):
    if not isinstance(mac, str):
        return None
    mac = mac.strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else None


def _int(v):
    return counters.parse_count(v)


class _DevState:
    __slots__ = ("lrx", "ltx", "seen", "wrx", "wtx")

    def __init__(self, max_rate):
        self.wrx = counters.CounterTracker(max_rate)
        self.wtx = counters.CounterTracker(max_rate)
        self.lrx = counters.CounterTracker(max_rate)
        self.ltx = counters.CounterTracker(max_rate)
        self.seen = 0.0


class DeviceSnapshot:
    """Latest per-device live information returned to the engine."""

    __slots__ = ("conn", "down", "freq", "host", "ip4", "ip6", "lan_down", "lan_up", "last_ms", "mac", "up", "uplink")

    def __init__(self, mac):
        self.mac = mac
        self.ip4 = self.ip6 = self.host = self.conn = self.uplink = None
        self.freq = None
        self.down = self.up = self.lan_down = self.lan_up = None
        self.last_ms = None


class BaseProvider:
    name = "none"
    label = "Per-device"
    measures_lan = False

    def __init__(self, platform, settings):
        self.p = platform
        self.settings = settings
        self.state = {}
        self.backoff = Backoff()
        self.status = Status("devices", "Per-device traffic")
        self.events = []

    def _max_rate(self):
        return self.settings["wan.max_rate_mbps"] * 125_000

    def _track(self, mac, now, wrx, wtx, lrx, ltx, emit, rate_ok, ident=None):
        st = self.state.get(mac)
        if st is None:
            st = self.state[mac] = _DevState(self._max_rate())
        st.seen = now
        res = {}
        for name, tracker, value in (("wrx", st.wrx, wrx), ("wtx", st.wtx, wtx), ("lrx", st.lrx, lrx), ("ltx", st.ltx, ltx)):
            if value is None:
                res[name] = None
                continue
            res[name] = tracker.update(value, now, ident)
        w = res["wrx"], res["wtx"]
        if w[0] is not None and w[1] is not None and w[0].valid and w[1].valid:
            emit("dev", mac, now, w[0].dt, w[0].bytes, w[1].bytes, rate_ok, count=(w[0].bytes or w[1].bytes) > 0)
        loc = res["lrx"], res["ltx"]
        if loc[0] is not None and loc[1] is not None and loc[0].valid and loc[1].valid:
            emit("devl", mac, now, loc[0].dt, loc[0].bytes, loc[1].bytes, rate_ok, count=(loc[0].bytes or loc[1].bytes) > 0)
        return res

    def expire(self, now):
        for mac in [m for m, st in self.state.items() if now - st.seen > DEVICE_TTL]:
            del self.state[mac]


class NoProvider(BaseProvider):
    def __init__(self, platform, settings, reason="Per-device monitoring is disabled"):
        super().__init__(platform, settings)
        self.status.set(DISABLED, reason, code="disabled")

    def poll(self, now, emit):
        return None


class BandixProvider(BaseProvider):
    name = "bandix"
    label = "Bandix"
    measures_lan = True

    def __init__(self, platform, settings):
        super().__init__(platform, settings)
        self.status.provider = "bandix"
        self.version = None
        self.last_poll = None
        self.capabilities = {}

    def _url(self, path):
        return self.settings["devices.bandix_url"].rstrip("/") + path

    def probe(self, now):
        # /api/system/info is documented for 0.12.x; the upstream LuCI app
        # falls back to /api/version, so do the same.
        info = None
        last_exc = None
        for path in ("/api/system/info", "/api/version"):
            try:
                info = self.p.http_json(self._url(path), timeout=2.0)
                break
            except (OSError, ValueError) as exc:
                last_exc = exc
        if info is None:
            self.status.set(UNAVAILABLE, "Bandix not reachable at %s (%s)" % (self.settings["devices.bandix_url"], _short(last_exc)), code="not_reachable")
            return False
        data = info.get("data") if isinstance(info, dict) else None
        if not isinstance(data, dict) and isinstance(info, dict):
            data = info
        version = data.get("version") if isinstance(data, dict) else None
        self.version = str(version)[:32] if version else "unknown"
        self.status.version = self.version
        return True

    def poll(self, now, emit):
        if self.version is None:
            if not self.backoff.ready(now):
                return None
            if not self.probe(now):
                self.backoff.fail(now)
                return None
            self.backoff.succeed()
        try:
            resp = self.p.http_json(self._url("/api/traffic/devices"), timeout=2.5)
        except (OSError, ValueError) as exc:
            self.status.set(ERROR, "Bandix request failed (%s)" % _short(exc), code="request_failed")
            self.version = None  # re-probe (Bandix may have restarted)
            self.backoff.fail(now)
            self.last_poll = None
            return None
        if not isinstance(resp, dict) or resp.get("status") not in ("success", None):
            self.status.set(ERROR, "Unexpected Bandix response", code="bad_response")
            return None
        data = resp.get("data") or {}
        items = data.get("d")
        if items is None:
            items = data.get("devices")  # tolerate alternative naming
        if not isinstance(items, list):
            self.status.set(ERROR, "Bandix response has no device list (unsupported version?)", code="unsupported_version")
            return None
        swap = self.settings["devices.bandix_direction"] == "tx_is_download"
        has_wan = any(isinstance(d, dict) and ("w_rx_b" in d or "wan_rx_bytes" in d) for d in items)
        self.capabilities = {"wan_split": has_wan or not items, "lan": True, "rates": True}
        dt = now - self.last_poll if self.last_poll is not None else None
        dt_ok = dt is not None and dt <= max(15.0, 4 * self.settings["devices.poll_interval"])
        self.last_poll = now
        snaps = {}
        totals = [0, 0]
        any_valid = False
        for item in items[:1024]:
            if not isinstance(item, dict):
                continue
            mac = norm_mac(item.get("mac"))
            if not mac:
                continue
            # Short keys since Bandix 0.12.7; long names as a fallback for
            # older releases (best effort, unverified).
            wrx, wtx = _int(_pick(item, "w_rx_b", "wan_rx_bytes")), _int(_pick(item, "w_tx_b", "wan_tx_bytes"))
            lrx, ltx = _int(_pick(item, "l_rx_b", "lan_rx_bytes")), _int(_pick(item, "l_tx_b", "lan_tx_bytes"))
            rwrx, rwtx = _int(_pick(item, "w_rx_r", "wan_rx_rate")), _int(_pick(item, "w_tx_r", "wan_tx_rate"))
            rlrx, rltx = _int(_pick(item, "l_rx_r", "lan_rx_rate")), _int(_pick(item, "l_tx_r", "lan_tx_rate"))
            if swap:
                wrx, wtx, lrx, ltx = wtx, wrx, ltx, lrx
                rwrx, rwtx, rlrx, rltx = rwtx, rwrx, rltx, rlrx
            res = self._track(mac, now, wrx, wtx, lrx, ltx, emit, dt_ok)
            if res["wrx"] is not None and res["wrx"].valid and res["wtx"] is not None and res["wtx"].valid:
                totals[0] += res["wrx"].bytes
                totals[1] += res["wtx"].bytes
                any_valid = True
            snap = DeviceSnapshot(mac)
            snap.ip4 = _str(_pick(item, "ip4", "ip"), 45)
            ip6 = item.get("ip6")
            if isinstance(ip6, list):
                ip6 = next((a for a in ip6 if isinstance(a, str) and not a.lower().startswith("fe80")), ip6[0] if ip6 else None)
            snap.ip6 = _str(ip6, 45)
            snap.host = _str(_pick(item, "host", "hostname"), 64)
            snap.conn = _str(item.get("conn"), 16)
            snap.uplink = _str(item.get("uplink"), 16)
            snap.freq = _int(item.get("w_freq")) or None
            snap.down, snap.up = rwrx, rwtx
            snap.lan_down, snap.lan_up = rlrx, rltx
            snap.last_ms = _int(item.get("last"))
            snaps[mac] = snap
        # Dense aggregate row: provides measurement coverage for the
        # sparse per-device rows.
        if dt_ok and any_valid:
            emit("dev", "*", now, dt, totals[0], totals[1], True)
        elif dt_ok and not items:
            emit("dev", "*", now, dt, 0, 0, True)
        self.expire(now)
        if has_wan or not items:
            self.status.ok("%d devices reported by Bandix %s" % (len(snaps), self.version))
        else:
            self.status.set(DEGRADED, "Bandix does not report a WAN/LAN split; Internet usage per device is unavailable", code="no_wan_split")
        return snaps


class NlbwmonProvider(BaseProvider):
    name = "nlbwmon"
    label = "nlbwmon"

    def __init__(self, platform, settings):
        super().__init__(platform, settings)
        self.status.provider = "nlbwmon"
        self.last_poll = None

    def poll(self, now, emit):
        if not self.backoff.ready(now):
            return None
        out = self.p.run(["nlbw", "-c", "json", "-g", "mac"], timeout=5.0)
        if not out:
            self.status.set(UNAVAILABLE, "nlbwmon not available (nlbw command failed or no permission)", code="not_reachable")
            self.backoff.fail(now)
            return None
        try:
            import json

            doc = json.loads(out)
            cols = doc["columns"]
            rows = doc["data"]
            idx = {c: i for i, c in enumerate(cols)}
            imac, irx, itx = idx["mac"], idx["rx_bytes"], idx["tx_bytes"]
        except (ValueError, KeyError, TypeError):
            self.status.set(ERROR, "Unexpected nlbwmon output", code="bad_response")
            self.backoff.fail(now)
            return None
        self.backoff.succeed()
        dt_ok = self.last_poll is not None and now - self.last_poll <= 120
        dt = now - self.last_poll if self.last_poll else None
        self.last_poll = now
        snaps = {}
        tot = [0, 0]
        valid = False
        for row in rows[:1024]:
            try:
                mac = norm_mac(row[imac])
                rx, tx = _int(row[irx]), _int(row[itx])
            except (IndexError, TypeError):
                continue
            if not mac:
                continue
            res = self._track(mac, now, rx, tx, None, None, emit, False)
            if res["wrx"] and res["wrx"].valid and res["wtx"] and res["wtx"].valid:
                tot[0] += res["wrx"].bytes
                tot[1] += res["wtx"].bytes
                valid = True
                snap = DeviceSnapshot(mac)
                snap.down = res["wrx"].rate
                snap.up = res["wtx"].rate
                snaps[mac] = snap
            else:
                snaps[mac] = DeviceSnapshot(mac)
        if dt_ok and dt and (valid or not rows):
            emit("dev", "*", now, dt, tot[0], tot[1], False)
        self.expire(now)
        self.status.set(DEGRADED, "nlbwmon (conntrack based): %d devices; rates are %d-second averages; offloaded flows may be under-counted" % (len(snaps), self.settings["devices.poll_interval"]), code="conntrack_estimates")
        self.status.last_ok = now
        return snaps


def _pick(item, *names):
    for n in names:
        if n in item:
            return item[n]
    return None


def _str(v, limit):
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    if not s:
        return None
    return "".join(ch for ch in s[:limit] if ch.isprintable())


def _short(exc):
    msg = str(exc) or type(exc).__name__
    return msg[:80]


class AutoProvider(BaseProvider):
    """Use Bandix when reachable, otherwise nlbwmon, otherwise nothing.

    Counter state is discarded whenever the active provider changes so the
    same traffic can never be counted by two providers.
    """

    name = "auto"

    def __init__(self, platform, settings):
        super().__init__(platform, settings)
        self.bandix = BandixProvider(platform, settings)
        self.nlbw = NlbwmonProvider(platform, settings) if platform.which("nlbw") else None
        self.active = None
        self.status = self.bandix.status

    @property
    def measures_lan(self):
        return self.active is self.bandix

    def _switch(self, prov):
        if prov is not self.active:
            for p in (self.bandix, self.nlbw):
                if p is not None:
                    p.state.clear()
                    p.last_poll = None
            if self.active is not None:
                self.events.append(("info", "provider_change", "Per-device provider changed to %s" % (prov.label if prov else "none")))
            self.active = prov

    def poll(self, now, emit):
        if self.active is not self.bandix and self.bandix.backoff.ready(now) and self.bandix.version is None:
            if self.bandix.probe(now):
                self.bandix.backoff.succeed()
                self._switch(self.bandix)
            else:
                self.bandix.backoff.fail(now)
        if self.active is self.bandix:
            snaps = self.bandix.poll(now, emit)
            self.status = self.bandix.status
            if snaps is not None:
                self._bandix_ok = now
            # Ride out short Bandix outages (restarts) before falling back.
            if snaps is not None or now - getattr(self, "_bandix_ok", now) < 120 or self.nlbw is None:
                return snaps
            self._switch(None)
        if self.nlbw is not None:
            self._switch(self.nlbw)
            snaps = self.nlbw.poll(now, emit)
            self.status = self.nlbw.status
            return snaps
        self.status = Status("devices", "Per-device traffic")
        self.status.set(UNAVAILABLE, "No supported per-device provider found (install Bandix or nlbwmon). WAN monitoring continues normally.", code="no_provider")
        return None


def make_provider(platform, settings):
    choice = settings["devices.provider"]
    if choice == "none":
        return NoProvider(platform, settings)
    if choice == "nlbwmon":
        return NlbwmonProvider(platform, settings)
    if choice == "bandix":
        return BandixProvider(platform, settings)
    return AutoProvider(platform, settings)
