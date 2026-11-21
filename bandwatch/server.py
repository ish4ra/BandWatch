"""HTTP server: static dashboard assets and the JSON API.

Security controls (see SECURITY.md):

* binds to the LAN address (or loopback) by default, never to all
  addresses unless configured; client addresses outside
  ``allowed_networks`` are rejected
* ``Host`` header allowlist (DNS-rebinding protection)
* session authentication, CSRF header + same-origin check on writes
* request body limit (64 KiB), socket timeouts, bounded worker threads
* strict Content-Security-Policy; no inline scripts; all assets local
* static files are served only from the web root with an extension allowlist
"""

import ipaddress
import json
import os
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import __version__, api, log
from .auth import SESSION_TTL

MAX_BODY = 64 * 1024
MAX_WORKERS = 32
MAX_STREAMS = 8
STREAM_LIFETIME = 900
COOKIE = "bw_session"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10")]


def default_web_root():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, addr, handler, app):
        self.app = app
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__(addr, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class App:
    """Glue between the HTTP layer, the engine and authentication."""

    def __init__(self, engine, auth, service, web_root=None):
        self.engine = engine
        self.auth = auth
        self.service = service
        self.web_root = os.path.realpath(web_root or service.web_root or default_web_root())
        self.streams = 0
        self.stream_lock = threading.Lock()
        self.allowed = []
        self.allow_any = False
        for item in service.allowed_networks:
            if item == "any":
                self.allow_any = True
            elif item == "private":
                self.allowed.extend(PRIVATE_NETS)
            else:
                self.allowed.append(ipaddress.ip_network(item, strict=False))
        self.extra_hosts = set(h.lower() for h in getattr(service, "allowed_hosts", []) or [])
        self.asset_version = __version__

    def client_allowed(self, host):
        if self.allow_any:
            return True
        try:
            ip = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.allowed)

    def host_allowed(self, host_header):
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if host in ("localhost",) or host in self.extra_hosts:
            return True
        if "." not in host and host.replace("-", "").isalnum():
            return True  # single-label LAN name, e.g. "openwrt"
        return host.endswith((".lan", ".local", ".home.arpa", ".localdomain", ".home", ".internal"))


def _hostname(host_header):
    """Host header without the port (IPv6 literals keep their brackets)."""
    h = (host_header or "").strip()
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BandWatch"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 12

        # -- plumbing ------------------------------------------------------

        def version_string(self):
            return self.server_version  # no Python/OS version disclosure

        def log_message(self, fmt, *args):
            pass

        def _security_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            host = _hostname(self.headers.get("Host"))
            ancestors = "'self'"
            if host and app.host_allowed(self.headers.get("Host")):
                # Allow embedding by LuCI on the same router (ports 80/443).
                ancestors += " http://%s https://%s" % (host, host)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors %s" % ancestors,
            )

        def _send(self, status, body, ctype="application/json", headers=None, cache="no-store"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self._security_headers()
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj, headers=None):
            self._send(status, json.dumps(obj, separators=(",", ":"), default=str), headers=headers)

        def _error(self, status, message):
            self._json(status, {"error": message, "status": status})

        def _cookie(self):
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def _session(self):
            if app.service.auth == "none":
                return {"via": "none", "csrf": "none"}
            return app.auth.session(self._cookie())

        def _same_origin(self):
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if origin:
                if origin == "null":
                    return False
                parsed = urllib.parse.urlsplit(origin)
                return parsed.netloc.lower() == (host or "").lower()
            ref = self.headers.get("Referer")
            if ref:
                return urllib.parse.urlsplit(ref).netloc.lower() == (host or "").lower()
            return True  # non-browser clients (curl) send neither

        def _body(self):
            length = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding"):
                raise api.ApiError(411, "chunked bodies are not accepted")
            try:
                n = int(length or 0)
            except ValueError:
                raise api.ApiError(400, "bad Content-Length") from None
            if n < 0 or n > MAX_BODY:
                raise api.ApiError(413, "request body too large")
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise api.ApiError(415, "expected application/json")
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise api.ApiError(400, "invalid JSON") from None

        def _gate(self):
            """Network-level checks shared by every request."""
            if not app.client_allowed(self.client_address[0]):
                self._error(403, "client address not allowed")
                return False
            if not app.host_allowed(self.headers.get("Host")):
                self._error(421, "unexpected Host header")
                return False
            return True

        # -- verbs -----------------------------------------------------------

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._gate():
                return
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            if len(self.path) > 2048:
                return self._error(414, "URI too long")
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, max_num_fields=20).items()}
            try:
                if path.startswith("/api/"):
                    return self._api_get(path, params)
                if path == "/auth/sso":
                    return self._sso(params)
                return self._static(path)
            except api.ApiError as exc:
                return self._error(exc.status, exc.message)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as exc:
                log.error("GET %s failed: %s: %s", path[:80], type(exc).__name__, exc)
                return self._error(500, "internal error")

        def do_POST(self):
            if not self._gate():
                return
            path = urllib.parse.urlsplit(self.path).path
            try:
                if not self._same_origin():
                    raise api.ApiError(403, "cross-origin request rejected")
                body = self._body()
                if path == "/api/login":
                    return self._login(body)
                sess = self._session()
                if not sess:
                    raise api.ApiError(401, "authentication required")
                if app.service.auth != "none" and not app.auth.csrf_ok(self._cookie(), self.headers.get("X-BandWatch-CSRF")):
                    raise api.ApiError(403, "missing or invalid CSRF token")
                if path == "/api/logout":
                    app.auth.destroy(self._cookie())
                    return self._json(200, {"ok": True}, {"Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % COOKIE})
                if path == "/api/settings":
                    return self._json(200, api.settings_update(app.engine, body))
                if path == "/api/settings/aria2-secret":
                    return self._json(200, api.set_secret(app.engine, body))
                if path == "/api/password":
                    return self._change_password(body)
                if path.startswith("/api/devices/") and path.endswith("/alias"):
                    mac = urllib.parse.unquote(path[len("/api/devices/"):-len("/alias")])
                    return self._json(200, api.set_alias(app.engine, mac, body))
                raise api.ApiError(404, "not found")
            except api.ApiError as exc:
                return self._error(exc.status, exc.message)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as exc:
                log.error("POST %s failed: %s: %s", path[:80], type(exc).__name__, exc)
                return self._error(500, "internal error")

        def do_PUT(self):
            self._error(405, "method not allowed")

        do_DELETE = do_PATCH = do_PUT

        # -- authentication ---------------------------------------------------------

        def _login(self, body):
            if app.service.auth == "none":
                return self._json(200, {"ok": True})
            client = self.client_address[0]
            wait = app.auth.login_delay(client)
            if wait:
                return self._json(429, {"error": "too many attempts, retry in %d s" % wait, "retry_after": wait}, {"Retry-After": str(wait)})
            if not app.auth.password_set():
                raise api.ApiError(409, "no password configured; run 'bandwatch passwd' on the router")
            password = body.get("password") if isinstance(body, dict) else None
            if not app.auth.check_password(password):
                app.auth.record_failure(client)
                time.sleep(0.5)
                raise api.ApiError(401, "incorrect password")
            app.auth.record_success(client)
            token, csrf = app.auth.create_session("password")
            return self._json(200, {"ok": True, "csrf": csrf}, {"Set-Cookie": self._cookie_header(token)})

        def _cookie_header(self, token):
            return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (COOKIE, token, SESSION_TTL)

        def _sso(self, params):
            if app.service.auth != "none" and not app.auth.consume_sso(params.get("t", "")):
                return self._send(303, b"", "text/plain", {"Location": "/#/login?sso=failed"})
            headers = {"Location": "/#/overview"}
            if app.service.auth != "none":
                token, _csrf = app.auth.create_session("luci")
                headers["Set-Cookie"] = self._cookie_header(token)
            return self._send(303, b"", "text/plain", headers)

        def _change_password(self, body):
            if not isinstance(body, dict):
                raise api.ApiError(400, "expected an object")
            sess = self._session()
            # LuCI-authenticated sessions may set a password without the old one.
            if sess.get("via") != "luci" and not app.auth.check_password(body.get("current", "")):
                raise api.ApiError(403, "current password is incorrect")
            try:
                app.auth.set_password(body.get("password"))
            except ValueError as exc:
                raise api.ApiError(422, str(exc)) from None
            token, csrf = app.auth.create_session(sess.get("via", "password"))
            return self._json(200, {"ok": True, "csrf": csrf}, {"Set-Cookie": self._cookie_header(token)})

        # -- API -------------------------------------------------------------------

        def _api_get(self, path, params):
            if path == "/api/session":
                sess = self._session()
                return self._json(
                    200,
                    {
                        "authenticated": bool(sess),
                        "csrf": sess["csrf"] if sess else None,
                        "auth": app.service.auth,
                        "password_set": app.auth.password_set() if app.service.auth != "none" else True,
                        "demo": app.service.demo,
                        "version": __version__,
                        "theme": app.engine.settings["ui.theme"],
                    },
                )
            if not self._session():
                raise api.ApiError(401, "authentication required")
            e = app.engine
            if path == "/api/live":
                try:
                    since = int(params.get("since", 0))
                except ValueError:
                    since = 0
                return self._json(200, e.live(since))
            if path == "/api/live/stream":
                return self._stream()
            routes = {
                "/api/overview": api.overview,
                "/api/history": api.history,
                "/api/devices": api.devices,
                "/api/aria2": api.aria2,
                "/api/health": api.health,
                "/api/settings": api.settings_get,
                "/api/vnstat": api.vnstat,
                "/api/events": api.events,
            }
            if path in routes:
                return self._json(200, routes[path](e, params))
            if path == "/api/history.csv":
                text, fname = api.history_csv(e, params)
                return self._download(text, fname, "text/csv; charset=utf-8")
            if path == "/api/export":
                data, fname = api.export(e, params)
                if fname:
                    return self._download(data, fname, "text/csv; charset=utf-8")
                return self._download(json.dumps(data, indent=1), "bandwatch-export.json", "application/json")
            if path == "/api/diagnostics":
                return self._download(json.dumps(e.diagnostics(), indent=1, default=str), "bandwatch-diagnostics.json", "application/json")
            if path.startswith("/api/devices/"):
                mac = urllib.parse.unquote(path[len("/api/devices/"):])
                return self._json(200, api.device_detail(e, mac, params))
            raise api.ApiError(404, "not found")

        def _download(self, text, fname, ctype):
            safe = "".join(c for c in fname if c.isalnum() or c in "-_.")[:100]
            self._send(200, text, ctype, {"Content-Disposition": 'attachment; filename="%s"' % safe})

        def _stream(self):
            with app.stream_lock:
                if app.streams >= MAX_STREAMS:
                    raise api.ApiError(503, "too many live streams; falling back to polling")
                app.streams += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self._security_headers()
                self.end_headers()
                self.close_connection = True
                seq = -1
                last_ms = int(time.time() * 1000) - 2000
                started = time.time()
                beat = time.time()
                while time.time() - started < STREAM_LIFETIME:
                    new_seq = app.engine.wait_for_update(seq, 15.0)
                    if new_seq != seq:
                        seq = new_seq
                        data = app.engine.live(last_ms)
                        if data["wan"]["points"]:
                            last_ms = data["wan"]["points"][-1][0]
                        self.wfile.write(b"data: " + json.dumps(data, separators=(",", ":")).encode() + b"\n\n")
                        self.wfile.flush()
                        beat = time.time()
                    elif time.time() - beat > 14:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        beat = time.time()
                    if not self._session():
                        break
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                pass
            finally:
                with app.stream_lock:
                    app.streams -= 1

        # -- static files ------------------------------------------------------------

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            rel = urllib.parse.unquote(path).lstrip("/")
            if "\x00" in rel or "\\" in rel:
                raise api.ApiError(400, "bad path")
            full = os.path.realpath(os.path.join(app.web_root, rel))
            if os.path.commonpath([full, app.web_root]) != app.web_root:
                raise api.ApiError(404, "not found")
            ext = os.path.splitext(full)[1].lower()
            if ext not in MIME or not os.path.isfile(full):
                raise api.ApiError(404, "not found")
            cache = "no-cache" if ext in (".html", ".webmanifest") or rel == "sw.js" else "public, max-age=86400"
            gz = full + ".gz"
            headers = {}
            if "gzip" in (self.headers.get("Accept-Encoding") or "") and os.path.isfile(gz):
                full = gz
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
            if rel == "sw.js":
                headers["Service-Worker-Allowed"] = "/"
            with open(full, "rb") as fh:
                data = fh.read(4 * 1024 * 1024)
            self._send(200, data, MIME[ext], headers, cache=cache)

    return Handler


def resolve_listen(service, platform):
    """Turn listen tokens ('lan', 'loopback', IPs) into bind addresses."""
    addrs = []
    for item in service.listen:
        if item == "loopback":
            addrs.append("127.0.0.1")
        elif item == "any":
            addrs.append("0.0.0.0")
        elif item == "lan":
            ip = None
            if getattr(platform, "has_ubus", False):
                st = platform.ubus("network.interface.%s" % service.lan_interface, "status")
                for a in (st or {}).get("ipv4-address", []) or []:
                    if a.get("address"):
                        ip = a["address"]
                        break
            if ip:
                addrs.append(ip)
                addrs.append("127.0.0.1")
            else:
                log.warning("could not determine the LAN address; listening on loopback only")
                addrs.append("127.0.0.1")
        else:
            addrs.append(item)
    return list(dict.fromkeys(addrs))


def serve(app, addresses, port):
    servers = []
    for addr in addresses:
        try:
            srv = Server((addr, port), make_handler(app), app)
        except OSError as exc:
            if exc.errno in (98, 48):
                raise RuntimeError("port %d is already in use on %s; choose another port in /etc/config/bandwatch" % (port, addr)) from None
            raise RuntimeError("cannot listen on %s:%d (%s)" % (addr, port, exc.strerror or exc)) from None
        servers.append(srv)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1.0}, name="http-%s" % addr, daemon=True)
        t.start()
        log.info("dashboard listening on http://%s:%d/", addr if ":" not in addr else "[%s]" % addr, port)
    return servers
