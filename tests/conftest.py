import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

from bandwatch import config as cfgmod
from bandwatch.platform import Platform


class FakePlatform(Platform):
    """Scriptable platform: files, ubus replies, HTTP replies and a fake clock."""

    name = "fake"

    def __init__(self):
        self.files = {}
        self.bins = {}
        self.ubus_replies = {}
        self.http = {}
        self.commands = {}
        self.has_ubus = False
        self.mono = 1000.0
        self.wall = 1_790_000_000.0
        self.dirs = {}
        self.http_calls = []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, sec):
        self.mono += sec
        self.wall += sec

    def read_text(self, path, limit=1 << 21):
        v = self.files.get(path)
        if callable(v):
            v = v()
        if isinstance(v, bytes):
            return v.decode()
        return v

    def read_bytes(self, path, limit=1 << 21):
        v = self.files.get(path)
        if callable(v):
            v = v()
        if isinstance(v, str):
            return v.encode()
        return v

    def listdir(self, path):
        return list(self.dirs.get(path, []))

    def exists(self, path):
        return path in self.files or path in self.dirs

    def statvfs(self, path):
        return {"total": 100, "free": 50, "used": 50}

    def which(self, prog):
        return self.bins.get(prog)

    def run(self, argv, timeout=5.0):
        if argv[:1] == ["ubus"] and "call" in argv:
            i = argv.index("call")
            rep = self.ubus_replies.get((argv[i + 1], argv[i + 2]))
            return json.dumps(rep) if rep is not None else None
        if argv[:1] == ["ubus"] and "list" in argv:
            return "\n".join(k[0] for k in self.ubus_replies if k[0].startswith("hostapd."))
        out = self.commands.get(tuple(argv))
        return out() if callable(out) else out

    def http_request(self, url, body=None, timeout=2.0, headers=None):
        self.http_calls.append((url, body))
        for prefix, handler in self.http.items():
            if url.startswith(prefix):
                res = handler(url, body) if callable(handler) else handler
                if isinstance(res, Exception):
                    raise res
                status, payload = res
                return status, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        raise OSError("connection refused")


def net_dev(counters):
    lines = ["Inter-| Receive | Transmit", " face |bytes packets errs drop fifo frame compressed multicast|bytes packets"]
    for name, (rx, tx) in counters.items():
        lines.append("%s: %d 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0" % (name, rx, tx))
    return "\n".join(lines) + "\n"


@pytest.fixture
def fake():
    return FakePlatform()


@pytest.fixture
def settings():
    return cfgmod.Settings(None)


@pytest.fixture
def fixture_text():
    def load(name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()

    return load
