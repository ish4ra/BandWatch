"""Configuration loading and validation.

Two layers are used:

* **Service configuration** (``/etc/config/bandwatch``, UCI format) holds
  deployment options owned by the administrator: listen address, port,
  service user, data directory and allowed client networks. The daemon
  only reads this file.
* **Application settings** (``<data_dir>/settings.json``) hold options the
  dashboard can change (units, sampling, retention, quotas, integrations).
  They are validated against :data:`SETTINGS_SCHEMA` before being saved.

Secrets (the aria2 RPC secret, the dashboard password hash) live in
separate ``0600`` files and are never returned by any API.
"""

import ipaddress
import json
import os
import re
import tempfile

from . import tz as tzmod

DEFAULT_UCI_PATH = "/etc/config/bandwatch"

# ---------------------------------------------------------------------------
# UCI parsing (read-only; a small subset of the UCI grammar is sufficient).

_TOKEN_RE = re.compile(r"""'([^']*)'|"((?:[^"\\]|\\.)*)"|(\S+)""")


def _tokens(line):
    out = []
    for m in _TOKEN_RE.finditer(line):
        if m.group(3) is not None and m.group(3).startswith("#"):
            break
        out.append(next(g for g in m.groups() if g is not None))
    return out


def parse_uci(text):
    """Parse UCI text into a list of sections ``{'.type', '.name', opts}``."""
    sections = []
    cur = None
    anon = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tok = _tokens(line)
        if not tok:
            continue
        if tok[0] == "config" and len(tok) >= 2:
            name = tok[2] if len(tok) > 2 else None
            if name is None:
                name = "@%s[%d]" % (tok[1], anon)
                anon += 1
            cur = {".type": tok[1], ".name": name}
            sections.append(cur)
        elif tok[0] == "option" and len(tok) >= 3 and cur is not None:
            cur[tok[1]] = tok[2]
        elif tok[0] == "list" and len(tok) >= 3 and cur is not None:
            cur.setdefault(tok[1], [])
            if isinstance(cur[tok[1]], list):
                cur[tok[1]].append(tok[2])
    return sections


def read_uci_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_uci(fh.read())
    except OSError:
        return []


def uci_section(sections, stype, name=None):
    for sec in sections:
        if sec[".type"] == stype and (name is None or sec[".name"] == name):
            return sec
    return None


# ---------------------------------------------------------------------------
# Service configuration


class ServiceConfig:
    """Deployment-level options from UCI (or defaults for development)."""

    def __init__(self, **kw):
        self.enabled = kw.get("enabled", True)
        self.listen = kw.get("listen", ["lan"])  # 'lan', 'loopback' or IPs
        self.port = kw.get("port", 8787)
        self.data_dir = kw.get("data_dir", "/etc/bandwatch")
        self.run_dir = kw.get("run_dir", "/var/run/bandwatch")
        self.allowed_networks = kw.get("allowed_networks", ["private"])
        self.auth = kw.get("auth", "password")  # password | none
        self.log_level = kw.get("log_level", "info")
        self.lan_interface = kw.get("lan_interface", "lan")
        self.demo = kw.get("demo", False)
        self.demo_scenario = kw.get("demo_scenario", "aw1000-cellular")
        self.allowed_hosts = kw.get("allowed_hosts", [])  # extra Host header names
        self.web_root = kw.get("web_root")
        self.trust_proxy = False

    @classmethod
    def from_uci(cls, path=DEFAULT_UCI_PATH):
        sec = uci_section(read_uci_file(path), "bandwatch", "main") or {}
        kw = {}
        if "enabled" in sec:
            kw["enabled"] = sec["enabled"] in ("1", "yes", "on", "true")
        if "listen" in sec:
            val = sec["listen"]
            kw["listen"] = val if isinstance(val, list) else val.split()
        if "port" in sec:
            kw["port"] = sec["port"]
        for key in ("data_dir", "run_dir", "auth", "log_level", "lan_interface"):
            if key in sec:
                kw[key] = sec[key]
        if "allowed_hosts" in sec:
            val = sec["allowed_hosts"]
            kw["allowed_hosts"] = [h for h in (val if isinstance(val, list) else val.split()) if re.match(r"^[A-Za-z0-9.-]{1,253}$", h)]
        if "allowed_networks" in sec:
            val = sec["allowed_networks"]
            kw["allowed_networks"] = val if isinstance(val, list) else val.split()
        cfg = cls(**kw)
        cfg.validate()
        return cfg

    def validate(self):
        try:
            self.port = int(self.port)
        except (TypeError, ValueError):
            raise ValueError("port must be an integer") from None
        if not 1 <= self.port <= 65535:
            raise ValueError("port out of range")
        if self.auth not in ("password", "none"):
            raise ValueError("auth must be 'password' or 'none'")
        for item in self.listen:
            if item in ("lan", "loopback", "any"):
                continue
            ipaddress.ip_address(item)
        nets = []
        for item in self.allowed_networks:
            if item in ("private", "any"):
                nets.append(item)
            else:
                nets.append(str(ipaddress.ip_network(item, strict=False)))
        self.allowed_networks = nets
        if not os.path.isabs(self.data_dir) or ".." in self.data_dir.split("/"):
            raise ValueError("data_dir must be an absolute path")
        if self.auth == "none" and any(x not in ("loopback", "127.0.0.1", "::1") for x in self.listen):
            raise ValueError("auth 'none' is only permitted when listening on loopback")
        return self


# ---------------------------------------------------------------------------
# Application settings

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,15}$")
_URL_RE = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]|[0-9.]{7,15}|[A-Za-z0-9.-]{1,253})(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?$")


def _int(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError("must be an integer between %d and %d" % (lo, hi))
        return v

    return check


def _enum(*choices):
    def check(v):
        if v not in choices:
            raise ValueError("must be one of: %s" % ", ".join(map(str, choices)))
        return v

    return check


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _iface_list(v):
    if not isinstance(v, list) or len(v) > 8:
        raise ValueError("must be a list of up to 8 interface names")
    for item in v:
        if not isinstance(item, str) or not _IFACE_RE.match(item):
            raise ValueError("invalid interface name")
    return list(dict.fromkeys(v))


def _local_url(v):
    if not isinstance(v, str) or not _URL_RE.match(v):
        raise ValueError("must be an http:// URL (local network endpoints only)")
    host = v[7:].split("/")[0].rsplit(":", 1)[0].strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise ValueError("endpoint must be on the router or the local network")
    except ValueError as exc:
        if "endpoint" in str(exc):
            raise
        if host != "localhost":
            raise ValueError("use an IP address or 'localhost'") from None
    return v


def _tz(v):
    if not isinstance(v, str) or len(v) > 64 or not tzmod.is_valid(v):
        raise ValueError("unknown timezone (use 'system', an IANA name or a POSIX TZ string)")
    return v


SETTINGS_SCHEMA = {
    # key: (default, validator)
    "general.timezone": ("system", _tz),
    "general.week_start": (0, _int(0, 6)),
    "ui.theme": ("dark", _enum("dark", "light", "system")),
    "ui.rate_unit": ("bits", _enum("bits", "bytes")),
    "ui.volume_base": (1000, _enum(1000, 1024)),
    "ui.live_window": (300, _int(60, 900)),
    "wan.interfaces": ([], _iface_list),
    "wan.sample_interval": (2, _int(1, 10)),
    "wan.max_rate_mbps": (10000, _int(10, 100000)),
    "devices.provider": ("auto", _enum("auto", "bandix", "nlbwmon", "none")),
    "devices.bandix_url": ("http://127.0.0.1:8686", _local_url),
    "devices.bandix_direction": ("rx_is_download", _enum("rx_is_download", "tx_is_download")),
    "devices.poll_interval": (3, _int(1, 60)),
    "devices.online_window": (180, _int(30, 3600)),
    "aria2.mode": ("auto", _enum("auto", "on", "off")),
    "aria2.url": ("http://127.0.0.1:6800/jsonrpc", _local_url),
    "aria2.poll_interval": (3, _int(1, 60)),
    "vnstat.mode": ("auto", _enum("auto", "off")),
    "storage.flush_interval": (300, _int(30, 3600)),
    "storage.minute_retention_days": (7, _int(1, 60)),
    "storage.quarter_retention_days": (400, _int(30, 1100)),
    "storage.device_quarter_retention_days": (120, _int(7, 1100)),
    "storage.day_retention_days": (1830, _int(90, 7300)),
    "quota.enabled": (False, _bool),
    "quota.monthly_gb": (0, _int(0, 1000000)),
    "quota.reset_day": (1, _int(1, 28)),
    "quota.warn_percent": (80, _int(10, 100)),
}

# Settings that only take effect after a service restart.
RESTART_KEYS = {"storage.flush_interval"}


class Settings:
    def __init__(self, path=None):
        self.path = path
        self.values = {k: v[0] for k, v in SETTINGS_SCHEMA.items()}
        self.load_error = None
        if path:
            self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.load_error = "settings file unreadable, defaults used (%s)" % type(exc).__name__
            return
        if isinstance(data, dict):
            for key, val in data.items():
                if key in SETTINGS_SCHEMA:
                    try:
                        self.values[key] = SETTINGS_SCHEMA[key][1](val)
                    except ValueError:
                        pass  # keep default for invalid persisted values

    def get(self, key):
        return self.values[key]

    def __getitem__(self, key):
        return self.values[key]

    def as_dict(self):
        return dict(self.values)

    @staticmethod
    def validate(changes):
        """Validate a partial update. Returns (clean, errors)."""
        clean, errors = {}, {}
        if not isinstance(changes, dict):
            return {}, {"_": "expected an object"}
        for key, val in changes.items():
            if key not in SETTINGS_SCHEMA:
                errors[key] = "unknown setting"
                continue
            try:
                clean[key] = SETTINGS_SCHEMA[key][1](val)
            except ValueError as exc:
                errors[key] = str(exc)
        return clean, errors

    def update(self, changes):
        clean, errors = self.validate(changes)
        if errors:
            return errors
        self.values.update(clean)
        if self.path:
            atomic_write_json(self.path, self.values, mode=0o640)
        return {}


def atomic_write_json(path, data, mode=0o600):
    atomic_write(path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"), mode)


def atomic_write(path, payload, mode=0o600):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Secrets


class SecretStore:
    """Small 0600 key/value store for secrets (never exposed via the API)."""

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        val = self._read().get(key)
        return val if isinstance(val, str) else None

    def has(self, key):
        return bool(self.get(key))

    def set(self, key, value):
        data = self._read()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        atomic_write_json(self.path, data, mode=0o600)
