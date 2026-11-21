"""WAN traffic collector based on Linux interface counters.

Detection order:

1. Explicit ``wan.interfaces`` setting.
2. OpenWrt ``ubus call network.interface dump``: interfaces that are up and
   carry an IPv4 or IPv6 default route; their ``l3_device`` is measured.
3. ``/proc/net/route`` and ``/proc/net/ipv6_route`` default routes.

Double counting is avoided by measuring each L3 device once (IPv4 and IPv6
usually share it) and by ignoring tunnel devices (6in4, 464XLAT/CLAT,
DS-Lite, WireGuard, GRE ...) when a non-tunnel WAN device also carries a
default route: tunnel traffic is already included in the underlay counters.
For PPPoE the ``pppoe-*`` device is measured (IP payload, excluding PPPoE
framing), never together with its underlying Ethernet port.
"""

from .. import counters, platform as plat
from . import DEGRADED, ERROR, OK, UNAVAILABLE, Status

# ARPHRD types that indicate a tunnel/virtual L3 device riding on another WAN.
TUNNEL_TYPES = {768, 769, 776, 778, 823, 65534}
PHYSICAL_PROTOS = {
    "dhcp", "static", "pppoe", "pppoa", "ppp", "qmi", "mbim", "ncm", "3g",
    "modemmanager", "wwan", "dhcpv6", "directip", "hnet", "uqmi",
}
DETECT_INTERVAL = 30.0


class WanInterface:
    __slots__ = ("device", "families", "ifindex", "logical", "present", "proto", "rx", "tunnel", "tx")

    def __init__(self, device, logical=None, proto=None, families=(), max_rate=counters.DEFAULT_MAX_RATE, tunnel=False):
        self.device = device
        self.logical = logical
        self.proto = proto
        self.families = set(families)
        self.rx = counters.CounterTracker(max_rate)
        self.tx = counters.CounterTracker(max_rate)
        self.ifindex = None
        self.present = False
        self.tunnel = tunnel

    def describe(self):
        return {
            "device": self.device,
            "interface": self.logical,
            "proto": self.proto,
            "families": sorted(self.families),
            "present": self.present,
            "tunnel": self.tunnel,
        }


def detect_from_ubus(dump, iftype):
    """Return candidates [(device, logical, proto, families, is_tunnel)] from a ubus dump."""
    found = {}
    itfs = dump.get("interface") if isinstance(dump, dict) else None
    for itf in itfs if isinstance(itfs, list) else []:
        if not isinstance(itf, dict) or not itf.get("up"):
            continue
        dev = itf.get("l3_device") or itf.get("device")
        if not isinstance(dev, str) or not dev or dev == "lo":
            continue
        fams = set()
        routes = itf.get("route")
        for route in routes if isinstance(routes, list) else []:
            if isinstance(route, dict) and route.get("mask") == 0 and route.get("target") in ("0.0.0.0", "::"):
                fams.add("ipv6" if ":" in route["target"] else "ipv4")
        if not fams:
            continue
        proto = itf.get("proto") if isinstance(itf.get("proto"), str) else None
        tunnel = iftype(dev) in TUNNEL_TYPES and proto not in PHYSICAL_PROTOS
        entry = found.get(dev)
        if entry:
            entry[3].update(fams)
            if isinstance(itf.get("interface"), str) and itf["interface"] not in entry[1].split("+"):
                entry[1] = entry[1] + "+" + itf["interface"]
        else:
            name = itf.get("interface") if isinstance(itf.get("interface"), str) else dev
            found[dev] = [dev, name, proto, fams, tunnel]
    return [tuple(v) for v in found.values()]


def detect_from_routes(route4, route6, iftype):
    found = {}
    for dev, _metric in plat.parse_route_v4(route4):
        found.setdefault(dev, set()).add("ipv4")
    for dev, _metric in plat.parse_route_v6(route6):
        found.setdefault(dev, set()).add("ipv6")
    return [(dev, dev, None, fams, iftype(dev) in TUNNEL_TYPES) for dev, fams in found.items()]


def choose(candidates):
    """Drop tunnel devices when any non-tunnel WAN device exists."""
    physical = [c for c in candidates if not c[4]]
    return physical if physical else list(candidates)


class WanCollector:
    def __init__(self, platform, settings):
        self.p = platform
        self.settings = settings
        self.status = Status("wan", "WAN interface counters")
        self.status.provider = "kernel"
        self.ifaces = {}
        self.source = None  # 'override' | 'ubus' | 'routes'
        self.next_detect = 0.0
        self.last_t = None
        self.boot_id = platform.boot_id()
        self.wan_down = False
        self.events = []  # (level, kind, message) consumed by the engine
        self.ignored = []

    # -- detection -----------------------------------------------------------

    def _iftype(self, dev):
        return self.p.read_int("/sys/class/net/%s/type" % dev) if plat_safe(dev) else None

    def detect(self, now):
        self.next_detect = now + DETECT_INTERVAL
        override = self.settings["wan.interfaces"]
        if override:
            cands = [(d, d, None, set(), False) for d in override]
            source = "override"
        else:
            cands = []
            source = None
            if getattr(self.p, "has_ubus", False):
                dump = self.p.ubus("network.interface", "dump")
                if dump:
                    cands = detect_from_ubus(dump, self._iftype)
                    source = "ubus"
            if not cands:
                cands = detect_from_routes(
                    self.p.read_text("/proc/net/route"), self.p.read_text("/proc/net/ipv6_route"), self._iftype
                )
                source = "routes"
        chosen = choose(cands)
        self.ignored = [c[0] for c in cands if c not in chosen]
        max_rate = self.settings["wan.max_rate_mbps"] * 125_000
        new = {}
        for dev, logical, proto, fams, tunnel in chosen:
            if not plat_safe(dev):
                continue
            cur = self.ifaces.get(dev)
            if cur is None:
                cur = WanInterface(dev, logical, proto, fams, max_rate, tunnel)
                if self.ifaces or self.source:
                    self.events.append(("info", "wan_change", "WAN device %s detected (%s)" % (dev, logical)))
            else:
                cur.logical, cur.proto, cur.families, cur.tunnel = logical, proto, set(fams), tunnel
                cur.rx.max_rate = cur.tx.max_rate = max_rate
            new[dev] = cur
        for dev in self.ifaces:
            if dev not in new:
                self.events.append(("info", "wan_change", "WAN device %s no longer active" % dev))
        self.ifaces = new
        self.source = source
        notes = []
        if self.ignored:
            notes.append("Tunnel devices not counted to avoid double counting: %s" % ", ".join(self.ignored))
        if len(new) > 1:
            notes.append("Multiple WAN devices are summed (multi-WAN).")
        if new and all(i.tunnel for i in new.values()):
            notes.append("Only tunnel devices carry a default route; traffic is measured inside the tunnel. Set an interface override to measure the underlying WAN.")
        self.status.notes = notes
        self.status.extra["source"] = source
        self.status.extra["interfaces"] = [i.describe() for i in new.values()]

    # -- sampling -------------------------------------------------------------

    def sample(self, now, emit):
        """Read counters once. Returns (rx_rate, tx_rate) or (None, None)."""
        if now >= self.next_detect or not self.ifaces:
            self.detect(now)
        text = self.p.read_text("/proc/net/dev")
        stats = plat.parse_net_dev(text)
        if not stats:
            self.status.set(ERROR, "Cannot read /proc/net/dev")
            self.last_t = None
            return None, None
        if not self.ifaces:
            self.status.set(UNAVAILABLE, "No WAN interface with a default route was found")
            self._mark_down(now, emit, True)
            return None, None
        missing = [d for d in self.ifaces if d not in stats]
        if missing and now >= self.next_detect - DETECT_INTERVAL + 2:
            # An interface vanished (reconnect / rename): re-detect right away.
            self.detect(now)
            missing = [d for d in self.ifaces if d not in stats]
        total_rx = total_tx = 0
        valid = True
        any_present = False
        rate_ok = True
        dt_used = None
        for dev, itf in self.ifaces.items():
            st = stats.get(dev)
            if st is None:
                itf.present = False
                itf.rx.reset()
                itf.tx.reset()
                continue
            any_present = True
            itf.present = True
            ifindex = self.p.read_int("/sys/class/net/%s/ifindex" % dev)
            ident = (ifindex, self.boot_id)
            drx = itf.rx.update(st[0], now, ident)
            dtx = itf.tx.update(st[1], now, ident)
            if drx.event in (counters.RESET, counters.REBASELINE) and drx.dt:
                self.events.append(("info", "counter_reset", "Counters of %s restarted (%s)" % (dev, "interface re-created" if ifindex != itf.ifindex and itf.ifindex is not None else "reset")))
            itf.ifindex = ifindex
            if not (drx.valid and dtx.valid):
                valid = False
                continue
            dt_used = drx.dt
            rate_ok = rate_ok and drx.dt <= max(10.0, self.settings["wan.sample_interval"] * 4)
            emit("wan", dev, now, drx.dt, drx.bytes, dtx.bytes, rate_ok)
            total_rx += drx.bytes
            total_tx += dtx.bytes
        if not any_present:
            self._mark_down(now, emit, False)
            return 0.0, 0.0
        if self.wan_down:
            self.wan_down = False
            self.events.append(("info", "wan_up", "WAN connectivity restored"))
        if not valid or dt_used is None:
            # First reading or a rebaseline: no measurement for this interval.
            self.last_t = now
            if self.status.state != OK:
                self.status.set(DEGRADED, "Establishing counter baseline")
            return None, None
        emit("wan", "*", now, dt_used, total_rx, total_tx, rate_ok)
        self.last_t = now
        msg = "Measuring %s" % ", ".join(self.ifaces)
        if missing:
            self.status.set(DEGRADED, msg + " (missing: %s)" % ", ".join(missing))
        else:
            self.status.ok(msg)
        return total_rx / dt_used, total_tx / dt_used

    def _mark_down(self, now, emit, no_route):
        """No WAN device present: the WAN carries no traffic (measured zero)."""
        if not self.wan_down:
            self.wan_down = True
            self.events.append(("warning", "wan_down", "WAN is down (no default route)" if no_route else "WAN device is not present"))
        if self.last_t is not None:
            dt = now - self.last_t
            if 0 < dt <= 60:
                emit("wan", "*", now, dt, 0, 0, True)
        self.last_t = now
        if not no_route:
            self.status.set(DEGRADED, "WAN device not present (reconnecting?)")

    # -- checkpoints for restart recovery -----------------------------------------

    def checkpoint(self, wall):
        out = {}
        for dev, itf in self.ifaces.items():
            if itf.rx.value is not None and itf.tx.value is not None:
                out["wan:" + dev] = {"rx": itf.rx.value, "tx": itf.tx.value, "ifindex": itf.ifindex, "boot": self.boot_id, "wall": wall}
        return out


def plat_safe(dev):
    """Interface names are used in sysfs paths: reject anything unusual."""
    return bool(dev) and len(dev) <= 15 and "/" not in dev and dev not in (".", "..") and all(c.isalnum() or c in "_.@:-" for c in dev)
