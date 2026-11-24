"""HTTP server security behaviour (real sockets on loopback)."""

import http.client
import json
import os
import socket
import time

import pytest

from bandwatch import auth as authmod
from bandwatch import config as cfgmod
from bandwatch import demo
from bandwatch.engine import Engine
from bandwatch.server import App, serve


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def srv(tmp_path):
    port = free_port()
    service = cfgmod.ServiceConfig(listen=["loopback"], port=port, data_dir=str(tmp_path), run_dir=str(tmp_path / "run"), auth="password", demo=True)
    os.makedirs(tmp_path / "run" / "sso")
    settings = cfgmod.Settings(str(tmp_path / "settings.json"))
    eng = Engine(service, demo.DemoPlatform(), settings=settings)
    a = authmod.Auth(service.data_dir, service.run_dir)
    a.set_password("correct horse")
    app = App(eng, a, service)
    servers = serve(app, ["127.0.0.1"], port)
    eng.start()
    yield {"port": port, "app": app, "tmp": tmp_path}
    for s in servers:
        s.shutdown()
        s.server_close()
    eng.stop()


def req(port, method, path, body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": "127.0.0.1:%d" % port}
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None and "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    return r, payload


def login(port, pw="correct horse"):
    r, body = req(port, "POST", "/api/login", {"password": pw})
    assert r.status == 200, body
    cookie = r.getheader("Set-Cookie").split(";")[0]
    return cookie, json.loads(body)["csrf"]


def test_static_and_security_headers(srv):
    r, body = req(srv["port"], "GET", "/")
    assert r.status == 200 and b"BandWatch" in body
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'self' http://127.0.0.1:* https://127.0.0.1:*" in csp
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("Server") == "BandWatch"


@pytest.mark.parametrize("path", ["/../bandwatch/server.py", "/%2e%2e/%2e%2e/etc/passwd", "/js/../../__init__.py", "/css/app.css%00.js", "/settings.json", "/..%5c..%5cserver.py"])
def test_path_traversal_blocked(srv, path):
    r, _ = req(srv["port"], "GET", path)
    assert r.status in (400, 404)


def test_api_requires_auth(srv):
    for path in ("/api/overview", "/api/devices", "/api/settings", "/api/diagnostics", "/api/export", "/api/diagnostics/report.zip", "/api/diagnostics/report.json", "/api/diagnostics/checks"):
        r, _ = req(srv["port"], "GET", path)
        assert r.status == 401, path
    r, body = req(srv["port"], "GET", "/api/session")
    assert r.status == 200 and json.loads(body)["authenticated"] is False


def test_login_flow_and_csrf(srv):
    port = srv["port"]
    r, _ = req(port, "POST", "/api/login", {"password": "wrong"})
    assert r.status == 401
    cookie, csrf = login(port)
    assert "HttpOnly" in req(port, "POST", "/api/login", {"password": "correct horse"})[0].getheader("Set-Cookie")
    r, body = req(port, "GET", "/api/overview", headers={"Cookie": cookie})
    assert r.status == 200 and json.loads(body)["demo"] is True
    # write without CSRF token
    r, _ = req(port, "POST", "/api/settings", {"ui.theme": "light"}, headers={"Cookie": cookie})
    assert r.status == 403
    # cross-origin write with a valid token
    r, _ = req(port, "POST", "/api/settings", {"ui.theme": "light"}, headers={"Cookie": cookie, "X-BandWatch-CSRF": csrf, "Origin": "http://evil.example"})
    assert r.status == 403
    r, body = req(port, "POST", "/api/settings", {"ui.theme": "light"}, headers={"Cookie": cookie, "X-BandWatch-CSRF": csrf, "Origin": "http://127.0.0.1:%d" % port})
    assert r.status == 200 and json.loads(body)["settings"]["ui.theme"] == "light"
    r, _ = req(port, "POST", "/api/logout", {}, headers={"Cookie": cookie, "X-BandWatch-CSRF": csrf})
    assert r.status == 200
    r, _ = req(port, "GET", "/api/overview", headers={"Cookie": cookie})
    assert r.status == 401


def test_host_header_allowlist(srv):
    r, _ = req(srv["port"], "GET", "/api/session", headers={"Host": "attacker.example.com"})
    assert r.status == 421
    for host in ("openwrt.lan", "router", "192.168.1.1:8787", "[fd00::1]:8787"):
        r, _ = req(srv["port"], "GET", "/api/session", headers={"Host": host})
        assert r.status == 200, host


def test_client_allowlist(srv):
    app = srv["app"]
    assert app.client_allowed("192.168.1.10") and app.client_allowed("fd00::5") and app.client_allowed("::ffff:10.0.0.2")
    assert not app.client_allowed("8.8.8.8") and not app.client_allowed("2001:4860::8888")


def test_body_limits_and_content_type(srv):
    cookie, csrf = login(srv["port"])
    h = {"Cookie": cookie, "X-BandWatch-CSRF": csrf}
    r, _ = req(srv["port"], "POST", "/api/settings", raw=b"x" * (70 * 1024), headers=h)
    assert r.status == 413
    r, _ = req(srv["port"], "POST", "/api/settings", raw=b"{}", headers=dict(h, **{"Content-Type": "text/plain"}))
    assert r.status == 415
    r, _ = req(srv["port"], "POST", "/api/settings", raw=b"{bad", headers=h)
    assert r.status == 400
    r, _ = req(srv["port"], "PUT", "/api/settings", {}, headers=h)
    assert r.status == 405


def test_secret_never_returned(srv):
    cookie, csrf = login(srv["port"])
    h = {"Cookie": cookie, "X-BandWatch-CSRF": csrf}
    r, _ = req(srv["port"], "POST", "/api/settings/aria2-secret", {"secret": "s3cr3t-value"}, headers=h)
    assert r.status == 200
    for path in ("/api/settings", "/api/aria2", "/api/diagnostics", "/api/health", "/api/overview", "/api/diagnostics/report.json", "/api/diagnostics/checks"):
        r, body = req(srv["port"], "GET", path, headers={"Cookie": cookie})
        assert r.status == 200 and b"s3cr3t-value" not in body, path
    sec = srv["tmp"] / "secrets.json"
    assert oct(os.stat(sec).st_mode & 0o777) == "0o600"


def test_rate_limit_after_failures(srv):
    for _ in range(6):
        req(srv["port"], "POST", "/api/login", {"password": "nope"})
    r, _ = req(srv["port"], "POST", "/api/login", {"password": "correct horse"})
    assert r.status == 429 and r.getheader("Retry-After")


def test_luci_sso_token_single_use(srv):
    token = "A" * 40
    path = srv["tmp"] / "run" / "sso" / token
    path.write_text("1")
    r, _ = req(srv["port"], "GET", "/auth/sso?t=" + token)
    assert r.status == 303 and "bw_session=" in (r.getheader("Set-Cookie") or "")
    r2, _ = req(srv["port"], "GET", "/auth/sso?t=" + token)
    assert r2.status == 303 and "sso=failed" in r2.getheader("Location")
    # expired token
    old = srv["tmp"] / "run" / "sso" / ("B" * 40)
    old.write_text("1")
    os.utime(old, (time.time() - 600, time.time() - 600))
    r3, _ = req(srv["port"], "GET", "/auth/sso?t=" + "B" * 40)
    assert "sso=failed" in r3.getheader("Location")
    r4, _ = req(srv["port"], "GET", "/auth/sso?t=../../secrets.json")
    assert "sso=failed" in r4.getheader("Location")


def test_password_change_requires_current(srv):
    cookie, csrf = login(srv["port"])
    h = {"Cookie": cookie, "X-BandWatch-CSRF": csrf}
    r, _ = req(srv["port"], "POST", "/api/password", {"current": "wrong", "password": "another-pass"}, headers=h)
    assert r.status == 403
    r, _ = req(srv["port"], "POST", "/api/password", {"current": "correct horse", "password": "short"}, headers=h)
    assert r.status == 422
    r, _ = req(srv["port"], "POST", "/api/password", {"current": "correct horse", "password": "another-pass"}, headers=h)
    assert r.status == 200
    login(srv["port"], "another-pass")


def test_live_endpoint_and_csv_download(srv):
    cookie, _ = login(srv["port"])
    time.sleep(2.5)
    r, body = req(srv["port"], "GET", "/api/live?since=0", headers={"Cookie": cookie})
    d = json.loads(body)
    assert r.status == 200 and d["wan"]["points"]
    r, body = req(srv["port"], "GET", "/api/history.csv?series=wan&period=today", headers={"Cookie": cookie})
    assert r.status == 200 and r.getheader("Content-Disposition").startswith("attachment") and b"start_local" in body


def test_password_hashing_fallback_matches_openssl():
    salt = b"0123456789abcdef"
    fast = authmod.pbkdf2("pässword", salt, 2000)
    slow = authmod._pbkdf2_py("pässword".encode(), salt, 2000)
    assert fast == slow
    h = authmod.hash_password("x" * 10, iterations=2000)
    assert authmod.verify_password("x" * 10, h) and not authmod.verify_password("y" * 10, h)
    assert not authmod.verify_password("x", "md5$1$aa$bb")


def test_port_conflict_reported(srv):
    from bandwatch.server import serve as serve2

    with pytest.raises(RuntimeError, match="already in use"):
        serve2(srv["app"], ["127.0.0.1"], srv["port"])


def test_live_streams_limited_per_client(srv):
    cookie, _ = login(srv["port"])
    conns = []
    statuses = []
    for _ in range(3):
        c = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=10)
        c.request("GET", "/api/live/stream", headers={"Host": "127.0.0.1", "Cookie": cookie})
        r = c.getresponse()
        statuses.append(r.status)
        conns.append(c)
    assert statuses == [200, 200, 503]  # third tab falls back to polling
    for c in conns:
        c.close()


def test_one_click_report_download(srv):
    import io
    import zipfile

    cookie, _csrf = login(srv["port"])
    r, body = req(srv["port"], "GET", "/api/diagnostics/report.zip", headers={"Cookie": cookie})
    assert r.status == 200 and r.getheader("Content-Type") == "application/zip"
    disp = r.getheader("Content-Disposition")
    assert disp.startswith('attachment; filename="bandwatch-report-') and disp.endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        doc = json.loads(zf.read("bandwatch-report.json"))
    assert doc["schema"] == "bandwatch-diagnostic-report"
    assert doc["listening"]["bound_scopes"] == ["loopback"] and doc["listening"]["auth"] == "password"
    r, body = req(srv["port"], "GET", "/api/diagnostics/checks", headers={"Cookie": cookie})
    data = json.loads(body)
    assert r.status == 200 and data["checks"] and "persistence" in data
    # State-changing methods are not accepted on report URLs.
    r, _ = req(srv["port"], "POST", "/api/diagnostics/report.zip", {}, headers={"Cookie": cookie})
    assert r.status in (403, 404)
