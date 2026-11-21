"""Device identification and presence (no network scanning).

Passive sources only, read at a slow interval:

* dnsmasq DHCP leases (``/tmp/dhcp.leases``) and static hosts from
  ``/etc/config/dhcp``
* the kernel neighbour table (``/proc/net/arp``)
* the bridge forwarding database (``/sys/class/net/<bridge>/brforward``),
  which also tells which bridge port (Wi-Fi or Ethernet) a MAC was last
  seen on and how long ago
* hostapd station lists via ``ubus call hostapd.<ifname> get_clients``
* hostnames, addresses and connection type reported by Bandix
"""

from .. import config as cfgmod
from .. import platform as plat
from . import DEGRADED, Status

WIFI_PREFIXES = ("wlan", "phy", "ath", "ra", "rai", "rax", "wl", "mld")


def is_wifi_ifname(name):
    return bool(name) and (name.startswith(WIFI_PREFIXES) or "-ap" in name or "-sta" in name)


def band_from_freq(freq):
    if not freq:
        return None
    if freq < 3000:
        return "2.4 GHz"
    if freq < 5925:
        return "5 GHz"
    if freq < 7200:
        return "6 GHz"
    return "60 GHz"


def is_randomized(mac):
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except (TypeError, ValueError):
        return False


class DeviceInfo:
    __slots__ = ("arp", "assoc", "band", "conn", "fdb_age", "hostname", "iface", "ip4", "ip6", "lease", "mac", "provider_seen_ms", "static_name", "wifi")

    def __init__(self, mac):
        self.mac = mac
        self.hostname = self.ip4 = self.ip6 = self.conn = self.iface = self.band = None
        self.wifi = False  # connection type is Wi-Fi
        self.assoc = False  # currently associated according to hostapd
        self.fdb_age = None
        self.arp = False
        self.lease = False
        self.static_name = None
        self.provider_seen_ms = None


class IdentityCollector:
    def __init__(self, platform, settings, lan_bridge="br-lan"):
        self.p = platform
        self.settings = settings
        self.bridge = lan_bridge
        self.status = Status("identity", "Device identification")
        self.status.provider = "passive"
        self.devices = {}
        self.next_wifi = 0.0
        self.wifi_clients = {}
        self.wifi_ok = None
        self.lease_file = None

    def _lease_path(self):
        if self.lease_file is None:
            path = "/tmp/dhcp.leases"
            for sec in cfgmod.parse_uci(self.p.read_text("/etc/config/dhcp") or ""):
                if sec[".type"] == "dnsmasq" and isinstance(sec.get("leasefile"), str):
                    path = sec["leasefile"]
                    break
            self.lease_file = path
        return self.lease_file

    def _static_hosts(self):
        out = {}
        for sec in cfgmod.parse_uci(self.p.read_text("/etc/config/dhcp") or ""):
            if sec[".type"] != "host" or not isinstance(sec.get("name"), str):
                continue
            macs = sec.get("mac")
            if isinstance(macs, str):
                macs = macs.split()
            for mac in macs or []:
                out[mac.lower()] = sec["name"][:64]
        return out

    def _bridge_ports(self):
        ports = {}
        base = "/sys/class/net/%s/brif" % self.bridge
        for name in self.p.listdir(base):
            num = self.p.read_int("%s/%s/port_no" % (base, name))
            if num is not None:
                ports[num] = name
        return ports

    def _poll_wifi(self, now):
        if not getattr(self.p, "has_ubus", False):
            self.wifi_ok = False
            return
        if now < self.next_wifi:
            return
        self.next_wifi = now + 30.0
        clients = {}
        objs = self.p.ubus_list("hostapd.*")
        ok = False
        for obj in objs[:16]:
            ifname = obj.split(".", 1)[1] if "." in obj else obj
            res = self.p.ubus(obj, "get_clients", timeout=3.0)
            if not isinstance(res, dict):
                continue
            ok = True
            freq = res.get("freq")
            for mac, info in (res.get("clients") or {}).items():
                if isinstance(info, dict) and info.get("assoc", True):
                    clients[mac.lower()] = (ifname, freq)
        self.wifi_clients = clients
        self.wifi_ok = ok or not objs

    def poll(self, now, provider_snaps=None):
        devices = {}

        def get(mac):
            d = devices.get(mac)
            if d is None:
                d = devices[mac] = DeviceInfo(mac)
            return d

        statics = self._static_hosts()
        for lease in plat.parse_dhcp_leases(self.p.read_text(self._lease_path())):
            d = get(lease["mac"])
            d.lease = True
            d.ip4 = d.ip4 or lease["ip"]
            d.hostname = lease["hostname"] or d.hostname
        for entry in plat.parse_arp(self.p.read_text("/proc/net/arp")):
            if not entry["complete"] or not entry["dev"].startswith(("br-", "lan", "eth", "wlan", "phy")):
                continue
            d = get(entry["mac"])
            d.arp = True
            d.ip4 = d.ip4 or entry["ip"]
        ports = self._bridge_ports()
        fdb = plat.parse_brforward(self.p.read_bytes("/sys/class/net/%s/brforward" % self.bridge, limit=256 * 1024))
        for mac, port, is_local, age in fdb:
            if is_local:
                continue
            d = devices.get(mac)
            if d is None:
                continue  # fdb alone (no IP) is usually infrastructure
            d.fdb_age = age
            d.iface = ports.get(port, d.iface)
        self._poll_wifi(now)
        for mac, (ifname, freq) in self.wifi_clients.items():
            d = get(mac)
            d.wifi = True
            d.assoc = True
            d.iface = ifname
            d.band = band_from_freq(freq) if isinstance(freq, int) else None
        for mac, snap in (provider_snaps or {}).items():
            d = get(mac)
            d.ip4 = snap.ip4 or d.ip4
            d.ip6 = snap.ip6 or d.ip6
            if snap.host and snap.host not in ("*", "unknown"):
                d.hostname = d.hostname or snap.host
            if snap.uplink and not d.iface:
                d.iface = snap.uplink
            if snap.freq and not d.band:
                d.band = band_from_freq(snap.freq)
            if snap.conn and not d.wifi and snap.conn.lower() in ("wifi", "wireless", "wlan"):
                d.wifi = True
            d.provider_seen_ms = snap.last_ms
        for mac, name in statics.items():
            if mac in devices:
                devices[mac].static_name = name
        for d in devices.values():
            if d.wifi or is_wifi_ifname(d.iface):
                d.conn = "wifi"
            elif d.iface and d.iface.startswith(("lan", "eth", "port", "sw")):
                d.conn = "ethernet"
            else:
                d.conn = "unknown"
        self.devices = devices
        if self.wifi_ok is False and getattr(self.p, "has_ubus", False):
            self.status.set(DEGRADED, "Wi-Fi station lists unavailable (ubus permission?); connection types may be incomplete")
        else:
            self.status.ok("%d devices identified" % len(devices))
        return devices

    def presence(self, d, now_ms, window):
        """Return 'online', 'idle' (recently seen), 'offline' or 'unknown'."""
        if d is None:
            return "unknown"
        if d.assoc:
            return "online"
        if d.fdb_age is not None:
            if d.fdb_age <= min(window, 300):
                return "online"
            return "idle" if d.fdb_age <= window * 4 else "offline"
        if d.provider_seen_ms and now_ms - d.provider_seen_ms <= window * 1000:
            return "online"
        if d.arp:
            return "idle"
        return "offline" if d.lease else "unknown"


class SystemCollector:
    """Router and BandWatch process health from procfs/sysfs."""

    def __init__(self, platform, data_dir):
        self.p = platform
        self.data_dir = data_dir
        self.status = Status("system", "Router health")
        self.status.provider = "procfs"
        self.prev_cpu = None
        self.prev_self = None
        self.info = {}
        try:
            import os

            self.clk_tck = os.sysconf("SC_CLK_TCK") or 100
        except (ValueError, OSError, AttributeError):
            self.clk_tck = 100
        self.board = None

    def _board(self):
        if self.board is None:
            self.board = {}
            rel = self.p.read_text("/etc/openwrt_release") or ""
            for line in rel.splitlines():
                k, _, v = line.partition("=")
                if k in ("DISTRIB_RELEASE", "DISTRIB_TARGET", "DISTRIB_ARCH", "DISTRIB_REVISION"):
                    self.board[k[8:].lower()] = v.strip().strip("'\"")[:64]
            model = self.p.read_text("/tmp/sysinfo/model")
            if model:
                self.board["model"] = model.strip()[:64]
            self.board["kernel"] = (self.p.read_text("/proc/sys/kernel/osrelease") or "").strip()[:64] or None
        return self.board

    def _temperature(self):
        temps = []
        for zone in self.p.listdir("/sys/class/thermal")[:16]:
            if not zone.startswith("thermal_zone"):
                continue
            val = self.p.read_int("/sys/class/thermal/%s/temp" % zone)
            if val is not None and -40000 < val < 150000:
                kind = (self.p.read_text("/sys/class/thermal/%s/type" % zone) or zone).strip()[:32]
                temps.append({"name": kind, "celsius": round(val / 1000.0, 1)})
        return temps

    def poll(self, now):
        info = {"board": self._board()}
        cpu = plat.parse_proc_stat_cpu(self.p.read_text("/proc/stat"))
        if cpu and self.prev_cpu:
            busy = cpu[0] - self.prev_cpu[0]
            total = cpu[1] - self.prev_cpu[1]
            info["cpu_percent"] = round(100.0 * busy / total, 1) if total > 0 else None
        self.prev_cpu = cpu or self.prev_cpu
        mem = plat.parse_meminfo(self.p.read_text("/proc/meminfo"))
        if mem.get("MemTotal"):
            avail = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Cached", 0))
            info["mem_total"] = mem["MemTotal"]
            info["mem_available"] = avail
            info["mem_percent"] = round(100.0 * (mem["MemTotal"] - avail) / mem["MemTotal"], 1)
        up = self.p.read_text("/proc/uptime")
        if up:
            try:
                info["uptime"] = int(float(up.split()[0]))
            except (ValueError, IndexError):
                pass
        load = self.p.read_text("/proc/loadavg")
        if load:
            try:
                info["load"] = [float(x) for x in load.split()[:3]]
            except ValueError:
                pass
        info["cpus"] = sum(1 for ln in (self.p.read_text("/proc/cpuinfo") or "").splitlines() if ln.startswith("processor")) or None
        storage = []
        seen = set()
        for label, path in (("Overlay", "/overlay"), ("Root", "/"), ("Temporary (RAM)", "/tmp"), ("BandWatch data", self.data_dir)):
            st = self.p.statvfs(path)
            if not st or not st["total"]:
                continue
            sig = (st["total"], st["free"])
            if sig in seen:
                continue
            seen.add(sig)
            storage.append({"label": label, "path": path, **st})
        info["storage"] = storage
        info["temperatures"] = self._temperature()
        # BandWatch's own resource use.
        stat = self.p.read_text("/proc/self/stat")
        if stat:
            try:
                fields = stat.rsplit(")", 1)[1].split()
                cpu_ticks = int(fields[11]) + int(fields[12])
                if self.prev_self:
                    dt = now - self.prev_self[1]
                    if dt > 0:
                        info["self_cpu_percent"] = round(100.0 * (cpu_ticks - self.prev_self[0]) / self.clk_tck / dt, 2)
                self.prev_self = (cpu_ticks, now)
            except (IndexError, ValueError):
                pass
        status = self.p.read_text("/proc/self/status") or ""
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    info["self_rss"] = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
            elif line.startswith("Threads:"):
                try:
                    info["self_threads"] = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
        self.info = info
        self.status.ok("OK")
        return info
