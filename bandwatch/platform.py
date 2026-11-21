"""Platform access layer.

Every interaction with the operating system goes through a :class:`Platform`
object so collectors can be exercised against the demo simulator and test
fixtures. The real implementation only *reads* system state. Subprocesses
are spawned without a shell, with fixed argument lists and timeouts, and
only for slow-interval operations (ubus, vnstat).
"""

import http.client
import json
import os
import shutil
import struct
import subprocess
import time

from . import log

MAX_HTTP_RESPONSE = 4 * 1024 * 1024
MAX_FILE_READ = 2 * 1024 * 1024


class Platform:
    """Interface; see :class:`LinuxPlatform` for the real implementation."""

    name = "abstract"
    simulated = False

    def monotonic(self):
        return time.monotonic()

    def time(self):
        return time.time()

    def read_text(self, path, limit=MAX_FILE_READ):
        raise NotImplementedError

    def read_bytes(self, path, limit=MAX_FILE_READ):
        raise NotImplementedError

    def listdir(self, path):
        raise NotImplementedError

    def exists(self, path):
        raise NotImplementedError

    def statvfs(self, path):
        raise NotImplementedError

    def which(self, prog):
        raise NotImplementedError

    def run(self, argv, timeout=5.0):
        """Run a command (no shell). Returns stdout text or None."""
        raise NotImplementedError

    def http_request(self, url, body=None, timeout=2.0, headers=None):
        """HTTP GET/POST returning (status, bytes) or raising OSError."""
        raise NotImplementedError

    # -- convenience helpers built on the primitives -------------------------

    def read_int(self, path):
        txt = self.read_text(path)
        if txt is None:
            return None
        try:
            return int(txt.strip(), 0)
        except ValueError:
            return None

    def http_json(self, url, body=None, timeout=2.0, headers=None):
        status, data = self.http_request(url, body=body, timeout=timeout, headers=headers)
        if status != 200:
            raise OSError("HTTP %s" % status)
        return json.loads(data.decode("utf-8", "replace"))

    def ubus(self, obj, method, args=None, timeout=5.0):
        argv = ["ubus", "-t", str(max(1, int(timeout))), "call", obj, method]
        if args is not None:
            argv.append(json.dumps(args, separators=(",", ":")))
        out = self.run(argv, timeout=timeout + 1)
        if not out:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def ubus_list(self, pattern, timeout=5.0):
        out = self.run(["ubus", "-t", str(max(1, int(timeout))), "list", pattern], timeout=timeout + 1)
        if not out:
            return []
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def boot_id(self):
        txt = self.read_text("/proc/sys/kernel/random/boot_id")
        return txt.strip() if txt else None


class LinuxPlatform(Platform):
    name = "linux"

    def __init__(self):
        self._which_cache = {}
        self.has_ubus = bool(self.which("ubus"))

    def read_text(self, path, limit=MAX_FILE_READ):
        try:
            with open(path, "rb") as fh:
                return fh.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

    def read_bytes(self, path, limit=MAX_FILE_READ):
        try:
            with open(path, "rb") as fh:
                return fh.read(limit)
        except OSError:
            return None

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def exists(self, path):
        return os.path.exists(path)

    def statvfs(self, path):
        try:
            st = os.statvfs(path)
        except OSError:
            return None
        return {
            "total": st.f_blocks * st.f_frsize,
            "free": st.f_bavail * st.f_frsize,
            "used": (st.f_blocks - st.f_bfree) * st.f_frsize,
        }

    def which(self, prog):
        if prog not in self._which_cache:
            self._which_cache[prog] = shutil.which(prog)
        return self._which_cache[prog]

    def run(self, argv, timeout=5.0):
        if not argv or not self.which(argv[0]):
            return None
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("command %s failed: %s", argv[0], type(exc).__name__)
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout[:MAX_HTTP_RESPONSE].decode("utf-8", "replace")

    def http_request(self, url, body=None, timeout=2.0, headers=None):
        if not url.startswith("http://"):
            raise OSError("only http:// endpoints are supported")
        rest = url[7:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, port = hostport, 80
        if hostport.startswith("["):
            host, _, tail = hostport[1:].partition("]")
            if tail.startswith(":"):
                port = int(tail[1:])
        elif ":" in hostport:
            host, p = hostport.rsplit(":", 1)
            port = int(p)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            hdrs = {"Accept": "application/json", "User-Agent": "BandWatch"}
            if headers:
                hdrs.update(headers)
            if body is not None:
                payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                hdrs["Content-Type"] = "application/json"
                conn.request("POST", path, body=payload, headers=hdrs)
            else:
                conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read(MAX_HTTP_RESPONSE + 1)
            if len(data) > MAX_HTTP_RESPONSE:
                raise OSError("response too large")
            return resp.status, data
        except (TimeoutError, http.client.HTTPException, ValueError) as exc:
            raise OSError(str(exc) or type(exc).__name__) from None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Parsers for procfs/sysfs formats (pure functions, unit tested).


def parse_net_dev(text):
    """Parse /proc/net/dev into {iface: (rx_bytes, tx_bytes, rx_packets, tx_packets)}."""
    out = {}
    if not text:
        return out
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            out[name.strip()] = (int(fields[0]), int(fields[8]), int(fields[1]), int(fields[9]))
        except ValueError:
            continue
    return out


def parse_route_v4(text):
    """Default-route devices from /proc/net/route as [(dev, metric)]."""
    out = []
    if not text:
        return out
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        if f[1] == "00000000" and f[7] == "00000000":
            try:
                flags = int(f[3], 16)
                metric = int(f[6])
            except ValueError:
                continue
            if flags & 0x1:  # RTF_UP
                out.append((f[0], metric))
    return out


def parse_route_v6(text):
    """Default-route devices from /proc/net/ipv6_route as [(dev, metric)]."""
    out = []
    if not text:
        return out
    for line in text.splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        if f[0] == "0" * 32 and f[1] == "00":
            try:
                metric = int(f[5], 16)
                flags = int(f[8], 16)
            except ValueError:
                continue
            if f[9] == "lo" or not flags & 0x1:
                continue
            if flags & 0x200:  # RTF_REJECT
                continue
            out.append((f[9], metric))
    return out


def parse_arp(text):
    """/proc/net/arp -> [{ip, mac, dev, complete}]."""
    out = []
    if not text:
        return out
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        try:
            flags = int(f[2], 16)
        except ValueError:
            continue
        mac = f[3].lower()
        if mac == "00:00:00:00:00:00":
            continue
        out.append({"ip": f[0], "mac": mac, "dev": f[5], "complete": bool(flags & 0x2)})
    return out


def parse_dhcp_leases(text):
    """dnsmasq lease file -> [{expiry, mac, ip, hostname}]."""
    out = []
    if not text:
        return out
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or ":" not in f[1]:
            continue
        try:
            expiry = int(f[0])
        except ValueError:
            continue
        host = None if f[3] == "*" else f[3]
        out.append({"expiry": expiry, "mac": f[1].lower(), "ip": f[2], "hostname": host})
    return out


_FDB = struct.Struct("=6sBBIBBH")


def parse_brforward(data):
    """Parse /sys/class/net/<br>/brforward (array of struct __fdb_entry).

    Returns [(mac, port_no, is_local, age_seconds)]. ``ageing_timer_value``
    is in USER_HZ (100) units on Linux.
    """
    out = []
    if not data:
        return out
    size = _FDB.size
    for off in range(0, len(data) - size + 1, size):
        mac, port_lo, is_local, age, port_hi, _pad, _unused = _FDB.unpack_from(data, off)
        port = port_lo | (port_hi << 8)
        out.append((":".join("%02x" % b for b in mac), port, bool(is_local), age / 100.0))
    return out


def parse_proc_stat_cpu(text):
    """Return (busy, total) jiffies from the aggregate cpu line."""
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals[:8])
            return total - idle, total
    return None


def parse_meminfo(text):
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[key] = int(parts[0]) * 1024
            except ValueError:
                pass
    return out
