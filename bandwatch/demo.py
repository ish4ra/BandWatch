"""Demo / development mode: a simulated OpenWrt router.

:class:`DemoPlatform` implements the :class:`~bandwatch.platform.Platform`
interface with synthetic procfs/sysfs files, ubus objects, a fake Bandix API
and a fake aria2 JSON-RPC server, so the *real* collectors run unchanged.
All names, MAC addresses and IP addresses are invented for the simulation.

Demo data is written to a separate database (``bandwatch-demo.db``) whose
``mode`` metadata is ``demo``; the production engine refuses to open it and
vice versa, so simulated data can never mix with real measurements.
"""

import json
import math
import os
import random
import struct
import time

from . import log
from . import store as stmod
from . import tz as tzmod
from .platform import Platform

SCENARIOS = {
    "aw1000-cellular": "Arcadyan AW1000 style 5G/4G router, WAN on wwan0 (QMI), Bandix + aria2 + vnStat",
    "fibre-ethernet": "Fibre ONT / Ethernet WAN on 'wan' (DHCP)",
    "pppoe": "PPPoE WAN (pppoe-wan over the 'wan' port)",
    "no-bandix": "Bandix not installed: WAN-only monitoring",
    "no-vnstat": "vnStat not installed",
    "no-aria2": "aria2 not installed",
    "reboot": "Router reboots every 5 minutes (counter + boot-id reset)",
    "reconnect": "Cellular WAN drops for 20 s every 3 minutes and returns with new counters",
    "counter-reset": "Bandix and aria2 restart every 4 minutes (counters reset)",
    "outage": "Bandix and aria2 are unreachable for 60 s every 4 minutes",
    "empty-history": "Fresh installation with no history",
    "hostile-names": "Security test: device and download names contain HTML/script payloads",
    "db-recovery": "Damaged database file is detected and recovered at start-up",
}

DEMO_TZ = "<+0530>-5:30"  # POSIX form of Asia/Colombo (exercises the fallback parser)

# name, mac, ip, conn, port/iface, down B/s, up B/s, lan B/s, schedule, freq
_DEVICES = [
    ("living-room-tv", "00:1a:2b:00:00:11", "192.168.1.20", "ethernet", "lan1", 1_800_000, 45_000, 0, "evening", 0),
    ("work-laptop", "00:1a:2b:00:00:12", "192.168.1.21", "wifi", "phy1-ap0", 450_000, 160_000, 60_000, "work", 5180),
    ("pixel-phone", "0a:3c:41:00:00:13", "192.168.1.22", "wifi", "phy1-ap0", 260_000, 35_000, 0, "always", 5500),
    ("galaxy-tab", "00:1a:2b:00:00:14", "192.168.1.23", "wifi", "phy0-ap0", 380_000, 20_000, 0, "evening", 2437),
    ("gaming-pc", "00:1a:2b:00:00:15", "192.168.1.24", "ethernet", "lan2", 900_000, 120_000, 20_000, "night", 0),
    ("home-nas", "00:1a:2b:00:00:16", "192.168.1.25", "ethernet", "lan3", 60_000, 140_000, 900_000, "always", 0),
    ("smart-speaker", "00:1a:2b:00:00:17", "192.168.1.26", "wifi", "phy0-ap0", 18_000, 4_000, 0, "always", 2412),
    ("ip-camera", "00:1a:2b:00:00:18", "192.168.1.27", "wifi", "phy0-ap0", 6_000, 210_000, 0, "always", 2462),
    ("guest-phone", "5e:91:c2:00:00:19", "192.168.1.28", "wifi", "phy1-ap0", 220_000, 30_000, 0, "guest", 5745),
    ("printer", "00:1a:2b:00:00:1a", "192.168.1.29", "wifi", "phy0-ap0", 2_000, 1_000, 3_000, "rare", 2412),
]
# Scale the profiles to a typical household on a 4G/5G plan (tens of GB a day).
DEVICES = [d[:5] + (int(d[5] * 0.45), int(d[6] * 0.45), int(d[7] * 0.45)) + d[8:] for d in _DEVICES]
HISTORY_SCALE = 0.55


def activity(schedule, hour, day_seed=0.0):
    """Activity factor 0..1 by local hour for a schedule profile."""
    if schedule == "always":
        return 0.6 + 0.4 * math.sin((hour - 6) / 24 * 2 * math.pi) ** 2
    if schedule == "evening":
        return max(0.0, math.exp(-((hour - 20.5) ** 2) / 6.0)) * 1.0 + (0.05 if 7 <= hour <= 23 else 0.0)
    if schedule == "work":
        return 0.9 if 9 <= hour < 18 else (0.15 if 7 <= hour < 23 else 0.0)
    if schedule == "night":
        return 1.0 if hour >= 21 or hour < 1 else (0.2 if 16 <= hour < 21 else 0.0)
    if schedule == "guest":
        return 0.8 if (int(day_seed) % 3 == 0 and 14 <= hour < 22) else 0.0
    if schedule == "rare":
        return 0.3 if hour in (10, 16) else 0.0
    return 0.5


class _Counter:
    __slots__ = ("rx", "tx")

    def __init__(self):
        self.rx = 0
        self.tx = 0


class DemoPlatform(Platform):
    name = "demo"
    simulated = True

    def __init__(self, scenario="aw1000-cellular", seed=7, speed=1.0):
        if scenario not in SCENARIOS:
            raise ValueError("unknown demo scenario %r (choose from: %s)" % (scenario, ", ".join(SCENARIOS)))
        self.scenario = scenario
        self.rng = random.Random(seed)
        self.has_ubus = True
        self.tz = tzmod.PosixTimezone(DEMO_TZ)
        self.t0 = time.monotonic()
        self.last = self.t0
        self.boot_t = self.t0 - 3 * 86400 - 4321  # simulated uptime
        self.boot = self._new_boot_id()
        self.wan_dev, self.wan_proto, self.wan_logical = {
            "fibre-ethernet": ("wan", "dhcp", "wan"),
            "pppoe": ("pppoe-wan", "pppoe", "wan"),
        }.get(scenario, ("wwan0", "qmi", "wan"))
        self.ifindex = {"lo": 1, "eth0": 2, "lan1": 3, "lan2": 4, "lan3": 5, "wan": 6, "br-lan": 7, "phy0-ap0": 8, "phy1-ap0": 9, self.wan_dev: 10}
        self.wan_present = True
        self.counters = {name: _Counter() for name in self.ifindex}
        for c in self.counters.values():  # pre-existing traffic since "boot"
            c.rx = self.rng.randint(10, 90) * 1_000_000_000
            c.tx = c.rx // 8
        self.dev = {d[1]: {"spec": d, "wan": _Counter(), "lan": _Counter(), "rate": [0.0, 0.0, 0.0, 0.0], "online": True, "last": 0} for d in DEVICES}
        for d in self.dev.values():
            d["wan"].rx = self.rng.randint(1, 40) * 100_000_000
            d["wan"].tx = d["wan"].rx // 10
        self.bandix_epoch = 0
        self.aria = _Aria2Sim(self.rng)
        self.hostile = scenario == "hostile-names"
        if self.hostile:
            # Names are attacker-controlled in real life (DHCP option 12,
            # torrent metadata); the dashboard must render them as text.
            self.aria.tasks[0]["name"] = '"><svg onload=alert(1)>.iso'
            self.aria.tasks[1]["name"] = "<img src=x onerror=alert(2)>"
        self.cpu = [0, 0]
        self.next_event = {"reboot": self.t0 + 300, "reconnect": self.t0 + 180, "counter-reset": self.t0 + 240, "outage": self.t0 + 240}
        self.outage_until = 0.0
        self.reconnect_until = 0.0

    # -- simulation core -----------------------------------------------------

    def _new_boot_id(self):
        return "%08x-0000-4000-8000-%012x" % (self.rng.getrandbits(32), self.rng.getrandbits(48))

    def _hour(self):
        d = time.time() + self.tz.std_offset
        return (d % 86400) / 3600.0

    def _advance(self):
        now = time.monotonic()
        dt = now - self.last
        if dt <= 0:
            return
        self.last = now
        self._scenario_events(now)
        hour = self._hour()
        day = int((time.time() + self.tz.std_offset) // 86400)
        total_rx = total_tx = 0.0
        wifi = [0.0, 0.0, 0.0, 0.0]
        eth = {"lan1": [0, 0], "lan2": [0, 0], "lan3": [0, 0]}
        for mac, d in self.dev.items():
            spec = d["spec"]
            act = activity(spec[8], hour, day)
            d["online"] = act > 0.01 or spec[8] in ("always",)
            # Smooth random walk around the profile target with bursts.
            target_down = spec[5] * act * (2.5 if self.rng.random() < 0.02 else 1.0)
            target_up = spec[6] * (act if spec[8] != "always" else 1.0)
            target_lan = spec[7] * act
            r = d["rate"]
            k = min(1.0, dt / 4.0)
            r[0] += (target_down * self.rng.uniform(0.4, 1.6) - r[0]) * k
            r[1] += (target_up * self.rng.uniform(0.6, 1.4) - r[1]) * k
            r[2] += (target_lan * self.rng.uniform(0.3, 1.7) - r[2]) * k
            r[3] = r[2] * 0.15
            if not d["online"]:
                r[0] = r[1] = r[2] = r[3] = 0.0
            if not self.wan_present:
                r[0] = r[1] = 0.0
            down, up = int(max(0, r[0]) * dt), int(max(0, r[1]) * dt)
            ldown, lup = int(max(0, r[2]) * dt), int(max(0, r[3]) * dt)
            d["wan"].rx += down
            d["wan"].tx += up
            d["lan"].rx += ldown
            d["lan"].tx += lup
            if down or up or ldown:
                d["last"] = int(time.time() * 1000)
            total_rx += down
            total_tx += up
            if spec[3] == "wifi":
                idx = 0 if spec[4] == "phy0-ap0" else 2
                wifi[idx] += down + ldown
                wifi[idx + 1] += up + lup
            else:
                eth[spec[4]][0] += up + lup
                eth[spec[4]][1] += down + ldown
        arx, atx = self.aria.advance(dt, self.wan_present and self.scenario != "no-aria2")
        router_rx = int(self.rng.uniform(1500, 6000) * dt) if self.wan_present else 0  # DNS, NTP, updates
        router_tx = int(self.rng.uniform(800, 3000) * dt) if self.wan_present else 0
        wrx = int(total_rx + arx + router_rx)
        wtx = int(total_tx + atx + router_tx)
        if self.wan_present:
            c = self.counters[self.wan_dev]
            c.rx += wrx
            c.tx += wtx
            if self.scenario == "pppoe":
                self.counters["wan"].rx += int(wrx * 1.01)
                self.counters["wan"].tx += int(wtx * 1.01)
        for name, idx in (("phy0-ap0", 0), ("phy1-ap0", 2)):
            self.counters[name].rx += int(wifi[idx + 1])
            self.counters[name].tx += int(wifi[idx])
        for port, (rx, tx) in eth.items():
            self.counters[port].rx += int(rx)
            self.counters[port].tx += int(tx)
        lan_rx = sum(v[0] for v in eth.values()) + wifi[1] + wifi[3]
        lan_tx = sum(v[1] for v in eth.values()) + wifi[0] + wifi[2]
        self.counters["br-lan"].rx += int(lan_rx)
        self.counters["br-lan"].tx += int(lan_tx)
        self.counters["eth0"].rx += int(lan_rx + wrx)
        self.counters["eth0"].tx += int(lan_tx + wtx)
        busy = 0.03 + 0.10 * min(1.0, (wrx + wtx) / dt / 30e6) + self.rng.uniform(0, 0.03)
        self.cpu[0] += int(busy * 400 * dt)
        self.cpu[1] += int(400 * dt)

    def _scenario_events(self, now):
        sc = self.scenario
        if sc == "reboot" and now >= self.next_event["reboot"]:
            self.next_event["reboot"] = now + 300
            self.boot = self._new_boot_id()
            self.boot_t = now
            for c in self.counters.values():
                c.rx = c.tx = 0
            for d in self.dev.values():
                d["wan"].rx = d["wan"].tx = d["lan"].rx = d["lan"].tx = 0
            self.aria.restart()
            log.info("demo: simulated router reboot")
        if sc == "reconnect":
            if self.wan_present and now >= self.next_event["reconnect"]:
                self.wan_present = False
                self.reconnect_until = now + 20
                self.next_event["reconnect"] = now + 180
                log.info("demo: simulated WAN drop")
            elif not self.wan_present and now >= self.reconnect_until:
                self.wan_present = True
                self.ifindex[self.wan_dev] += 10
                self.counters[self.wan_dev] = _Counter()
                log.info("demo: simulated WAN reconnect")
        if sc == "counter-reset" and now >= self.next_event["counter-reset"]:
            self.next_event["counter-reset"] = now + 240
            for d in self.dev.values():
                d["wan"].rx = d["wan"].tx = d["lan"].rx = d["lan"].tx = 0
            self.aria.restart()
            log.info("demo: simulated Bandix + aria2 restart")
        if sc == "outage" and now >= self.next_event["outage"]:
            self.next_event["outage"] = now + 240
            self.outage_until = now + 60
            log.info("demo: simulated provider outage")

    # -- Platform primitives -------------------------------------------------

    def read_text(self, path, limit=1 << 21):
        if path.startswith("/proc/self/"):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read(limit)
            except OSError:
                return None
        self._advance()
        gen = self._files().get(path)
        if gen is None:
            if path.startswith("/sys/class/net/"):
                parts = path.split("/")
                if len(parts) == 6 and parts[4] in self.ifindex and (parts[4] != self.wan_dev or self.wan_present):
                    if parts[5] == "ifindex":
                        return "%d\n" % self.ifindex[parts[4]]
                    if parts[5] == "type":
                        return "512\n" if parts[4].startswith("pppoe") else ("519\n" if parts[4] == "wwan0" else "1\n")
                if len(parts) == 8 and parts[5] == "brif" and parts[7] == "port_no":
                    return "0x%x\n" % {"lan1": 1, "lan2": 2, "lan3": 3, "phy0-ap0": 4, "phy1-ap0": 5}.get(parts[6], 9)
            return None
        return gen() if callable(gen) else gen

    def read_bytes(self, path, limit=1 << 21):
        if path == "/sys/class/net/br-lan/brforward":
            self._advance()
            return self._brforward()
        txt = self.read_text(path, limit)
        return txt.encode() if txt is not None else None

    def listdir(self, path):
        if path == "/sys/class/net/br-lan/brif":
            return ["lan1", "lan2", "lan3", "phy0-ap0", "phy1-ap0"]
        if path == "/sys/class/net":
            return sorted(n for n in self.ifindex if n != self.wan_dev or self.wan_present)
        if path == "/sys/class/thermal":
            return ["thermal_zone0", "thermal_zone1"]
        return []

    def exists(self, path):
        return path in self._files()

    def statvfs(self, path):
        # Only simulated filesystems: never report the host machine's disks
        # or paths (the demo data directory lives on the "overlay").
        sizes = {"/overlay": (512 * 2**20, 489 * 2**20), "/tmp": (432 * 2**20, 410 * 2**20)}
        if path in sizes:
            total, free = sizes[path]
            return {"total": total, "free": free, "used": total - free}
        return None

    def which(self, prog):
        if prog == "vnstat":
            return None if self.scenario == "no-vnstat" else "/usr/bin/vnstat"
        if prog == "ubus":
            return "/bin/ubus"
        return None

    def boot_id(self):
        return self.boot

    def run(self, argv, timeout=5.0):
        self._advance()
        if argv[:1] == ["ubus"]:
            if "list" in argv:
                return "hostapd.phy0-ap0\nhostapd.phy1-ap0\n"
            if "call" in argv:
                i = argv.index("call")
                obj, method = argv[i + 1], argv[i + 2]
                res = self._ubus(obj, method)
                return json.dumps(res) if res is not None else None
        if argv[:1] == ["vnstat"] and self.scenario != "no-vnstat":
            return json.dumps(self._vnstat())
        return None

    def http_request(self, url, body=None, timeout=2.0, headers=None):
        self._advance()
        now = time.monotonic()
        if ":8686" in url:
            if self.scenario == "no-bandix" or now < self.outage_until:
                raise OSError("connection refused")
            path = url.split(":8686", 1)[1]
            if path.startswith("/api/system/info"):
                return 200, json.dumps({"status": "success", "data": {"version": "0.12.12"}, "message": None}).encode()
            if path.startswith("/api/traffic/devices"):
                return 200, json.dumps(self._bandix_devices()).encode()
            return 404, b"{}"
        if "/jsonrpc" in url:
            if self.scenario == "no-aria2" or now < self.outage_until:
                raise OSError("connection refused")
            return 200, json.dumps(self.aria.rpc(body)).encode()
        raise OSError("connection refused")

    # -- simulated files and services -----------------------------------------

    def _files(self):
        return {
            "/proc/net/dev": self._net_dev,
            "/proc/net/route": self._route4,
            "/proc/net/ipv6_route": self._route6,
            "/proc/net/arp": self._arp,
            "/tmp/dhcp.leases": self._leases,
            "/proc/stat": lambda: "cpu  %d 0 0 %d 0 0 0 0 0 0\n" % (self.cpu[0], self.cpu[1] - self.cpu[0]),
            "/proc/meminfo": lambda: "MemTotal:        885760 kB\nMemFree:         402000 kB\nMemAvailable:    %d kB\nCached:          120000 kB\n" % (600000 + self.rng.randint(-8000, 8000)),
            "/proc/uptime": lambda: "%.2f 1000.00\n" % (time.monotonic() - self.boot_t),
            "/proc/loadavg": lambda: "%.2f %.2f %.2f 1/120 4242\n" % (self.rng.uniform(0.05, 0.4), 0.2, 0.18),
            "/proc/cpuinfo": "processor\t: 0\nprocessor\t: 1\nprocessor\t: 2\nprocessor\t: 3\n",
            "/proc/sys/kernel/osrelease": "6.12.94\n",
            "/proc/sys/kernel/random/boot_id": lambda: self.boot + "\n",
            "/etc/openwrt_release": "DISTRIB_ID='OpenWrt'\nDISTRIB_RELEASE='25.12.5'\nDISTRIB_REVISION='r0-simulated'\nDISTRIB_TARGET='qualcommax/ipq807x'\nDISTRIB_ARCH='aarch64_cortex-a53'\n",
            "/tmp/sysinfo/model": "Simulated AW1000-class router\n" if self.scenario not in ("fibre-ethernet", "pppoe") else "Simulated Ethernet router\n",
            "/sys/class/thermal/thermal_zone0/temp": lambda: "%d\n" % (52000 + self.rng.randint(-1500, 1500)),
            "/sys/class/thermal/thermal_zone0/type": "cpu-thermal\n",
            "/sys/class/thermal/thermal_zone1/temp": lambda: "%d\n" % (47000 + self.rng.randint(-1000, 1000)),
            "/sys/class/thermal/thermal_zone1/type": "modem-thermal\n",
            "/etc/config/dhcp": "config dnsmasq\n\toption leasefile '/tmp/dhcp.leases'\n\nconfig host\n\toption name 'home-nas'\n\toption mac '00:1a:2b:00:00:16'\n\toption ip '192.168.1.25'\n",
            "/etc/config/firewall": "config defaults\n\toption flow_offloading '1'\n",
        }

    def _net_dev(self):
        lines = ["Inter-|   Receive                                                |  Transmit", " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed"]
        for name in self.ifindex:
            if name == self.wan_dev and not self.wan_present:
                continue
            c = self.counters[name]
            lines.append("%6s: %d %d 0 0 0 0 0 0 %d %d 0 0 0 0 0 0" % (name, c.rx, c.rx // 1200, c.tx, c.tx // 1200))
        return "\n".join(lines) + "\n"

    def _route4(self):
        out = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        if self.wan_present:
            out += "%s\t00000000\t0100000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n" % self.wan_dev
        out += "br-lan\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
        return out

    def _route6(self):
        if not self.wan_present:
            return ""
        return "%s 00 %s 00 fe800000000000000000000000000001 00000400 00000000 00000000 00000003 %s\n" % ("0" * 32, "0" * 32, self.wan_dev)

    def _arp(self):
        out = "IP address       HW type     Flags       HW address            Mask     Device\n"
        for mac, d in self.dev.items():
            spec = d["spec"]
            flags = "0x2" if d["online"] or spec[8] == "evening" else "0x0"
            out += "%-16s 0x1         %-11s %s     *        br-lan\n" % (spec[2], flags, mac)
        return out

    def _leases(self):
        exp = int(time.time()) + 36000
        out = ""
        for mac, d in self.dev.items():
            spec = d["spec"]
            if spec[0] == "home-nas":
                continue  # static lease, name comes from /etc/config/dhcp
            host = spec[0] if spec[0] != "guest-phone" else "*"
            if self.hostile and spec[0] == "work-laptop":
                host = "<img/src=x/onerror=alert(4)>"
            out += "%d %s %s %s 01:%s\n" % (exp, mac, spec[2], host, mac)
        return out

    def _brforward(self):
        ports = {"lan1": 1, "lan2": 2, "lan3": 3, "phy0-ap0": 4, "phy1-ap0": 5}
        data = b""
        now_ms = int(time.time() * 1000)
        for mac, d in self.dev.items():
            spec = d["spec"]
            age_s = 1.0 if d["online"] else min(290.0, max(0.0, (now_ms - d["last"]) / 1000.0)) if d["last"] else 290.0
            port = ports.get(spec[4], 1)
            data += struct.pack("=6sBBIBBH", bytes(int(x, 16) for x in mac.split(":")), port & 0xFF, 0, int(age_s * 100), port >> 8, 0, 0)
        return data

    def _ubus(self, obj, method):
        if obj == "network.interface" and method == "dump":
            itfs = [
                {"interface": "lan", "up": True, "proto": "static", "l3_device": "br-lan", "device": "br-lan", "route": [], "ipv4-address": [{"address": "192.168.1.1", "mask": 24}]},
                {"interface": "loopback", "up": True, "proto": "static", "l3_device": "lo", "route": []},
            ]
            if self.wan_present:
                itfs.append({"interface": self.wan_logical, "up": True, "proto": self.wan_proto, "l3_device": self.wan_dev, "device": self.wan_dev, "route": [{"target": "0.0.0.0", "mask": 0, "nexthop": "10.0.0.1"}]})
                itfs.append({"interface": "wan6", "up": True, "proto": "dhcpv6", "l3_device": self.wan_dev, "device": self.wan_dev, "route": [{"target": "::", "mask": 0, "nexthop": "fe80::1"}]})
            return {"interface": itfs}
        if obj == "network.interface.lan" and method == "status":
            return {"up": True, "ipv4-address": [{"address": "127.0.0.1", "mask": 8}]}
        if obj.startswith("hostapd.") and method == "get_clients":
            ifname = obj.split(".", 1)[1]
            clients = {}
            freq = 2437
            for mac, d in self.dev.items():
                spec = d["spec"]
                if spec[3] == "wifi" and spec[4] == ifname and d["online"]:
                    clients[mac] = {"auth": True, "assoc": True, "authorized": True}
                    freq = spec[9] or freq
            return {"freq": freq, "clients": clients}
        return None

    def _bandix_devices(self):
        items = []
        for mac, d in self.dev.items():
            spec = d["spec"]
            r = d["rate"]
            items.append(
                {
                    "ip4": spec[2], "ip6": [], "mac": mac, "host": ("<script>alert(3)</script>" if self.hostile and spec[0] == "pixel-phone" else spec[0]) if spec[0] != "guest-phone" else "",
                    "conn": spec[3] if spec[3] == "wifi" else "wired", "uplink": spec[4], "w_ch": 0, "w_freq": spec[9],
                    "t_rx_b": d["wan"].rx + d["lan"].rx, "t_tx_b": d["wan"].tx + d["lan"].tx,
                    "t_rx_r": int(r[0] + r[2]), "t_tx_r": int(r[1] + r[3]), "w_rx_l": 0, "w_tx_l": 0,
                    "l_rx_b": d["lan"].rx, "l_tx_b": d["lan"].tx, "l_rx_r": int(r[2]), "l_tx_r": int(r[3]),
                    "w_rx_b": d["wan"].rx, "w_tx_b": d["wan"].tx, "w_rx_r": int(r[0]), "w_tx_r": int(r[1]),
                    "last": d["last"],
                }
            )
        return {"status": "success", "data": {"d": items}}

    def _vnstat(self):
        today = tzmod.local_date(time.time(), self.tz)
        c = self.counters[self.wan_dev]
        return {
            "vnstatversion": "2.13", "jsonversion": "2",
            "interfaces": [{"name": self.wan_dev, "traffic": {
                "day": [{"date": {"year": today.year, "month": today.month, "day": today.day}, "rx": c.rx % 30_000_000_000, "tx": c.tx % 4_000_000_000}],
                "month": [{"date": {"year": today.year, "month": today.month}, "rx": c.rx % 300_000_000_000, "tx": c.tx % 40_000_000_000}],
            }}],
        }


class _Aria2Sim:
    """Very small aria2 RPC simulation (read-only methods only)."""

    def __init__(self, rng):
        self.rng = rng
        self.session = "%032x" % rng.getrandbits(128)
        self.next_gid = 0x1000
        self.tasks = []
        for name, total, done, status, speed in (
            ("ubuntu-26.04-desktop-amd64.iso", 6_200_000_000, 2_100_000_000, "active", 1_400_000),
            ("openwrt-sdk-25.12.tar.zst", 410_000_000, 410_000_000, "complete", 0),
            ("debian-13-netinst.iso", 750_000_000, 0, "waiting", 0),
            ("archive-photos-2025.zip", 1_900_000_000, 380_000_000, "paused", 0),
        ):
            self._add(name, total, done, status, speed)

    def _add(self, name, total, done, status, speed):
        self.next_gid += 1
        self.tasks.append({"gid": "%016x" % self.next_gid, "name": name, "total": total, "done": done, "up": 0, "status": status, "speed": speed, "seed": name.endswith(".iso")})

    def restart(self):
        self.session = "%032x" % self.rng.getrandbits(128)
        for t in self.tasks:
            self.next_gid += 1
            t["gid"] = "%016x" % self.next_gid

    def advance(self, dt, online):
        rx = tx = 0
        for t in self.tasks:
            if t["status"] == "active" and online:
                rate = t["speed"] * self.rng.uniform(0.5, 1.4)
                t["cur"] = rate
                inc = int(min(rate * dt, t["total"] - t["done"]))
                t["done"] += inc
                rx += inc
                if t["seed"]:
                    up = int(rate * 0.08 * dt)
                    t["up"] += up
                    tx += up
                if t["done"] >= t["total"]:
                    t["status"] = "complete"
        if not any(t["status"] == "active" for t in self.tasks):
            waiting = [t for t in self.tasks if t["status"] == "waiting"]
            if waiting:
                waiting[0]["status"] = "active"
                waiting[0]["speed"] = self.rng.choice([1_200_000, 3_500_000, 6_000_000])
            elif self.rng.random() < dt / 600:
                self._add("dataset-%d.tar" % self.rng.randint(100, 999), self.rng.randint(200, 2000) * 1_000_000, 0, "active", 2_000_000)
        done = [t for t in self.tasks if t["status"] == "complete"]
        if len(done) > 8:
            self.tasks.remove(done[0])
        return rx, tx

    def _task(self, t):
        return {
            "gid": t["gid"], "status": t["status"], "totalLength": str(t["total"]), "completedLength": str(t["done"]),
            "uploadLength": str(t["up"]), "downloadSpeed": str(int(t.get("cur", t["speed"])) if t["status"] == "active" else 0),
            "uploadSpeed": str(int(t.get("cur", t["speed"]) * 0.08) if t["status"] == "active" and t["seed"] else 0),
            "connections": "8" if t["status"] == "active" else "0",
            "files": [{"path": "/mnt/storage/downloads/" + t["name"], "uris": []}],
        }

    def rpc(self, body):
        calls = body.get("params", [[]])[0] if isinstance(body, dict) else []
        results = []
        for call in calls:
            m = call.get("methodName")
            if m == "aria2.getVersion":
                results.append([{"version": "1.37.0", "enabledFeatures": ["BitTorrent", "Metalink"]}])
            elif m == "aria2.getSessionInfo":
                results.append([{"sessionId": self.session}])
            elif m == "aria2.getGlobalStat":
                act = [t for t in self.tasks if t["status"] == "active"]
                results.append([{
                    "downloadSpeed": str(sum(int(t.get("cur", t["speed"])) for t in act)),
                    "uploadSpeed": str(sum(int(t.get("cur", t["speed"]) * 0.08) for t in act if t["seed"])),
                    "numActive": str(len(act)),
                    "numWaiting": str(sum(1 for t in self.tasks if t["status"] in ("waiting", "paused"))),
                    "numStopped": str(sum(1 for t in self.tasks if t["status"] in ("complete", "error", "removed"))),
                    "numStoppedTotal": str(sum(1 for t in self.tasks if t["status"] in ("complete", "error", "removed"))),
                }])
            elif m == "aria2.tellActive":
                results.append([[self._task(t) for t in self.tasks if t["status"] == "active"]])
            elif m == "aria2.tellWaiting":
                results.append([[self._task(t) for t in self.tasks if t["status"] in ("waiting", "paused")]])
            elif m == "aria2.tellStopped":
                results.append([[self._task(t) for t in self.tasks if t["status"] in ("complete", "error", "removed")]])
            else:
                results.append({"faultCode": 1, "faultString": "method not allowed in simulation"})
        return {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None, "result": results}


# ---------------------------------------------------------------------------
# History backfill


def backfill(store, days=45, seed=11, now=None):
    """Write synthetic 15-minute history directly into the demo database."""
    if store.mode != "demo":
        raise RuntimeError("refusing to write simulated history into a non-demo database")
    if store.meta("demo_backfilled"):
        return 0
    now = now or time.time()
    rng = random.Random(seed)
    tzinfo = store.tz
    end = int(now // 900 * 900)
    start = end - days * 86400
    gap = (end - 9 * 86400 + 3 * 3600, end - 9 * 86400 + 6 * 3600)  # simulated 3-hour service outage
    rows = []
    for q in range(start, end, 900):
        if gap[0] <= q < gap[1]:
            continue
        local = q + tzmod.utc_offset_at(q, tzinfo)
        hour = (local % 86400) / 3600.0
        day = local // 86400
        dev_rx = dev_tx = 0
        dev_prx = dev_ptx = 0
        for spec in DEVICES:
            act = activity(spec[8], hour, day)
            if act <= 0.01 and spec[8] != "always":
                continue
            noise = rng.uniform(0.3, 1.7)
            rx = int(spec[5] * act * noise * 900 * HISTORY_SCALE)
            tx = int(spec[6] * (act if spec[8] != "always" else 1.0) * rng.uniform(0.6, 1.4) * 900 * HISTORY_SCALE)
            lrx = int(spec[7] * act * rng.uniform(0.3, 1.7) * 900 * HISTORY_SCALE)
            prx, ptx = int(rx / 900 * rng.uniform(2, 4)), int(tx / 900 * rng.uniform(2, 4))
            if rx or tx:
                rows.append(("dev", spec[1], q, rx, tx, 900_000, prx, ptx, int(rx / 900 * 0.1), int(tx / 900 * 0.1), 300, 0))
            if lrx:
                rows.append(("devl", spec[1], q, lrx, lrx // 7, 900_000, int(lrx / 300), int(lrx / 2000), 0, 0, 300, 0))
            dev_rx += rx
            dev_tx += tx
            dev_prx += prx
            dev_ptx += ptx
        rows.append(("dev", "*", q, dev_rx, dev_tx, 900_000, dev_prx, dev_ptx, int(dev_rx / 900 * 0.2), int(dev_tx / 900 * 0.2), 300, 0))
        arx = int(rng.choice([0, 0, 0, 1, 2, 3]) * 400_000 * 900 * rng.uniform(0.5, 1.2)) if 1 <= hour < 7 or rng.random() < 0.05 else 0
        atx = arx // 15
        rows.append(("aria2", "*", q, arx, atx, 900_000, int(arx / 900 * 1.6), int(atx / 900 * 1.6), 0, 0, 300, 0))
        wrx = int((dev_rx + arx) * 1.01 + rng.uniform(1e6, 5e6))
        wtx = int((dev_tx + atx) * 1.02 + rng.uniform(5e5, 2e6))
        for key in ("*",):
            rows.append(("wan", key, q, wrx, wtx, 900_000, int(dev_prx + arx / 900 * 1.6), int(dev_ptx + atx / 900), int(wrx / 900 * 0.15), int(wtx / 900 * 0.15), 450, 0))
    with store.lock:
        conn = store.conn
        conn.execute("BEGIN")
        conn.executemany("INSERT OR REPLACE INTO m15 (s, k, t, rx, tx, cov, prx, ptx, nrx, ntx, n, est) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        store._rollup_days(start, end)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('demo_backfilled', ?)", (str(int(now)),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('first_start', ?)", (str(start),))
        conn.execute("COMMIT")
        store.set_alias("00:1a:2b:00:00:11", "Living room TV")
        store.set_alias("00:1a:2b:00:00:15", "Gaming PC")
    store.add_events([(int(gap[0]), "warning", "store", "gap", "Simulated: BandWatch was not running for 3 hours (no data recorded)")])
    return len(rows)


def corrupt_file(path):
    """For the db-recovery scenario: write a damaged database file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"SQLite format 3\x00" + os.urandom(4096))


def prepare(service, scenario):
    """Prepare a demo data directory (never the production one)."""
    path = os.path.join(service.data_dir, "bandwatch-demo.db")
    if scenario == "db-recovery":
        corrupt_file(path)
    if scenario == "empty-history" and os.path.exists(path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
    return path


__all__ = ["SCENARIOS", "DemoPlatform", "backfill", "prepare", "stmod"]
