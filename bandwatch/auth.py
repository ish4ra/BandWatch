"""Dashboard authentication.

* Passwords are stored as PBKDF2-HMAC-SHA256 hashes in ``auth.json`` (0600).
  ``hashlib.pbkdf2_hmac`` needs OpenSSL (``python3-openssl`` on OpenWrt);
  a pure-Python implementation is used when it is missing.
* Sessions are random 256-bit tokens kept in memory (a service restart
  logs everyone out). Cookies are ``HttpOnly`` and ``SameSite=Strict``.
* Every state-changing request must carry the per-session CSRF token in the
  ``X-BandWatch-CSRF`` header (checked with a constant-time comparison) and
  a same-origin ``Origin``/``Referer`` when the browser sends one.
* Failed logins are rate limited per client address.
* LuCI single sign-on: the rpcd helper (running as root for an
  authenticated LuCI administrator) drops a one-time token file into
  ``<run_dir>/sso``; the token is accepted once, within 60 seconds.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import config as cfgmod

ITERATIONS = 120_000
SESSION_TTL = 12 * 3600
SESSION_IDLE = 4 * 3600
MAX_SESSIONS = 64
SSO_MAX_AGE = 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
MIN_PASSWORD = 8


def _pbkdf2_py(password, salt, iterations, dklen=32):
    inner = hashlib.sha256()
    outer = hashlib.sha256()
    key = password
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key.ljust(64, b"\0")
    inner.update(bytes(b ^ 0x36 for b in key))
    outer.update(bytes(b ^ 0x5C for b in key))

    def prf(msg):
        i = inner.copy()
        i.update(msg)
        o = outer.copy()
        o.update(i.digest())
        return o.digest()

    out = b""
    block = 1
    while len(out) < dklen:
        u = prf(salt + block.to_bytes(4, "big"))
        acc = int.from_bytes(u, "big")
        for _ in range(iterations - 1):
            u = prf(u)
            acc ^= int.from_bytes(u, "big")
        out += acc.to_bytes(32, "big")
        block += 1
    return out[:dklen]


def pbkdf2(password, salt, iterations):
    pw = password.encode("utf-8")
    try:
        return hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)
    except (AttributeError, ValueError):
        return _pbkdf2_py(pw, salt, iterations)


def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    dk = pbkdf2(password, salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters)
        if not 1000 <= iters <= 10_000_000:
            return False
        dk = pbkdf2(password, bytes.fromhex(salt_hex), iters)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def validate_new_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return "Password must be at least %d characters" % MIN_PASSWORD
    if len(password) > 256:
        return "Password is too long"
    return None


class Auth:
    def __init__(self, data_dir, run_dir, mode="password"):
        self.path = os.path.join(data_dir, "auth.json")
        self.sso_dir = os.path.join(run_dir, "sso")
        self.mode = mode
        self.sessions = {}
        self.failures = {}
        self.lock = threading.Lock()
        # At most two password verifications at a time so unauthenticated
        # login floods cannot pin the router's CPU.
        self.verify_slots = threading.BoundedSemaphore(2)

    # -- password management -------------------------------------------------

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def password_set(self):
        return bool(self._load().get("password"))

    def set_password(self, password):
        err = validate_new_password(password)
        if err:
            raise ValueError(err)
        data = self._load()
        data["password"] = hash_password(password)
        data["changed"] = int(time.time())
        cfgmod.atomic_write_json(self.path, data, mode=0o600)
        with self.lock:
            self.sessions.clear()

    def check_password(self, password):
        stored = self._load().get("password")
        if not stored or not isinstance(password, str) or len(password) > 256:
            return False
        if not self.verify_slots.acquire(timeout=10):
            return False
        try:
            return verify_password(password, stored)
        finally:
            self.verify_slots.release()

    # -- rate limiting -------------------------------------------------------------

    def login_delay(self, client):
        """Seconds the client must wait before another attempt (0 = allowed)."""
        with self.lock:
            rec = self.failures.get(client)
            if not rec:
                return 0
            count, last = rec
            if count < 5:
                return 0
            wait = min(300, 2 ** (count - 4)) - (time.time() - last)
            return max(0, int(wait + 0.999))

    def record_failure(self, client):
        with self.lock:
            count, _ = self.failures.get(client, (0, 0))
            self.failures[client] = (count + 1, time.time())
            if len(self.failures) > 1024:
                oldest = sorted(self.failures.items(), key=lambda kv: kv[1][1])[:512]
                for k, _v in oldest:
                    del self.failures[k]

    def record_success(self, client):
        with self.lock:
            self.failures.pop(client, None)

    # -- sessions ------------------------------------------------------------------

    def create_session(self, via="password"):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.items(), key=lambda kv: kv[1]["seen"])[0]
                del self.sessions[oldest]
            self.sessions[token] = {"created": now, "seen": now, "csrf": csrf, "via": via}
        return token, csrf

    def session(self, token):
        if not token or not TOKEN_RE.match(token):
            return None
        now = time.time()
        with self.lock:
            sess = self.sessions.get(token)
            if not sess:
                return None
            if now - sess["created"] > SESSION_TTL or now - sess["seen"] > SESSION_IDLE:
                del self.sessions[token]
                return None
            sess["seen"] = now
            return dict(sess)

    def destroy(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def csrf_ok(self, token, header):
        sess = self.session(token)
        if not sess or not header:
            return False
        return hmac.compare_digest(sess["csrf"], header)

    # -- LuCI single sign-on ------------------------------------------------------------

    def consume_sso(self, token):
        if not token or not TOKEN_RE.match(token):
            return False
        path = os.path.join(self.sso_dir, token)
        try:
            st = os.stat(path)
            os.unlink(path)
        except OSError:
            return False
        if time.time() - st.st_mtime > SSO_MAX_AGE:
            return False
        self._sweep_sso()
        return True

    def _sweep_sso(self):
        try:
            names = os.listdir(self.sso_dir)
        except OSError:
            return
        now = time.time()
        for name in names[:256]:
            p = os.path.join(self.sso_dir, name)
            try:
                if now - os.stat(p).st_mtime > SSO_MAX_AGE:
                    os.unlink(p)
            except OSError:
                pass
