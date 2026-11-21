"""Security, robustness and reliability tests added during the pre-beta audit."""

import http.client
import itertools
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from bandwatch import api, demo
from bandwatch import config as cfgmod
from bandwatch import store as stmod
from bandwatch.collectors import aria2 as aria2mod
from bandwatch.collectors import extras, perdevice, wan
from bandwatch.config import SecretStore
from conftest import net_dev
from test_collectors import Aria2Fake, Sink
from test_engine_api import make_engine, run_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# -- SSRF / provider endpoint restrictions -------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:6800/jsonrpc",  # LAN host (needs explicit opt-in)
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata style
        "http://0.0.0.0:6800/jsonrpc",
        "http://8.8.8.8/",
        "http://example.com/",
        "http://127.0.0.1:99999/",
        "https://127.0.0.1:6800/jsonrpc",
        "http://127.0.0.1:6800/jsonrpc?x=1",
        "http://user@127.0.0.1/",
        "file:///etc/shadow",
    ],
)
def test_provider_urls_restricted_to_router(url):
    cfgmod.ALLOW_LAN_PROVIDERS = False
    _clean, errors = cfgmod.Settings.validate({"aria2.url": url})
    assert "aria2.url" in errors


def test_lan_provider_requires_explicit_opt_in():
    try:
        cfgmod.ALLOW_LAN_PROVIDERS = True
        assert cfgmod.Settings.validate({"devices.bandix_url": "http://192.168.1.10:8686"})[1] == {}
        assert "devices.bandix_url" in cfgmod.Settings.validate({"devices.bandix_url": "http://8.8.8.8:8686"})[1]
    finally:
        cfgmod.ALLOW_LAN_PROVIDERS = False
    for ok in ("http://127.0.0.1:6800/jsonrpc", "http://localhost:8686", "http://[::1]:8686"):
        assert cfgmod.Settings.validate({"devices.bandix_url": ok})[1] == {}, ok


def test_aria2_secret_cleared_when_endpoint_changes(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.set_aria2_secret("s3cret")
    assert eng.secrets.has("aria2_secret")
    api.settings_update(eng, {"aria2.poll_interval": 5})
    assert eng.secrets.has("aria2_secret")  # unrelated change keeps it
    api.settings_update(eng, {"aria2.url": "http://127.0.0.1:6801/jsonrpc"})
    assert not eng.secrets.has("aria2_secret")
    eng.store.close()


def test_cgnat_not_private_by_default(tmp_path):
    from bandwatch.server import App

    service = cfgmod.ServiceConfig(listen=["loopback"], data_dir=str(tmp_path), auth="none")
    app = App(None, None, service)
    assert not app.client_allowed("100.64.12.34")  # cellular CGNAT / tailnet
    assert app.client_allowed("192.168.1.5")
    service2 = cfgmod.ServiceConfig(listen=["loopback"], data_dir=str(tmp_path), auth="none", allowed_networks=["private", "100.64.0.0/10"]).validate()
    assert App(None, None, service2).client_allowed("100.64.12.34")


def test_luci_sso_requires_write_acl():
    with open(os.path.join(ROOT, "openwrt/luci-app-bandwatch/root/usr/share/rpcd/acl.d/luci-app-bandwatch.json")) as fh:
        acl = json.load(fh)["luci-app-bandwatch"]
    assert "sso" not in acl["read"]["ubus"]["luci.bandwatch"]
    assert "sso" in acl["write"]["ubus"]["luci.bandwatch"]


def test_service_config_rejects_unsafe_values(tmp_path):
    p = tmp_path / "bw"
    p.write_text("config bandwatch 'main'\n\toption lan_interface 'lan; reboot'\n")
    with pytest.raises(ValueError):
        cfgmod.ServiceConfig.from_uci(str(p))
    p.write_text("config bandwatch 'main'\n\toption https '1'\n\toption tls_key 'relative/key.pem'\n")
    with pytest.raises(ValueError):
        cfgmod.ServiceConfig.from_uci(str(p))


def test_no_shell_or_eval_in_application():
    for dirpath, _d, files in os.walk(os.path.join(ROOT, "bandwatch")):
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(dirpath, f), encoding="utf-8").read()
                for bad in ("shell=True", "os.system(", "os.popen(", "eval(", "exec("):
                    assert bad not in src.replace("_exec(", ""), (f, bad)


# -- HTTPS ------------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl CLI not available")
def test_https_mode_serves_tls_and_secure_cookies(tmp_path):
    import ssl

    from bandwatch.auth import Auth
    from bandwatch.engine import Engine
    from bandwatch.server import App, make_ssl_context, serve

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=test"], check=True, capture_output=True)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    service = cfgmod.ServiceConfig(listen=["loopback"], port=port, data_dir=str(tmp_path), run_dir=str(tmp_path / "run"), auth="password", demo=True, https=True, tls_cert=str(cert), tls_key=str(key))
    eng = Engine(service, demo.DemoPlatform(), settings=cfgmod.Settings(str(tmp_path / "s.json")))
    a = Auth(service.data_dir, service.run_dir)
    a.set_password("tls-password-1")
    app = App(eng, a, service)
    servers = serve(app, ["127.0.0.1"], port, make_ssl_context(str(cert), str(key)))
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
        c.request("POST", "/api/login", body=json.dumps({"password": "tls-password-1"}), headers={"Content-Type": "application/json", "Host": "127.0.0.1"})
        r = c.getresponse()
        r.read()
        assert r.status == 200 and "Secure" in r.getheader("Set-Cookie")
        # plain HTTP to the TLS port must not be answered as HTTP
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raw.sendall(b"GET /api/session HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            data = raw.recv(100)
        except OSError:
            data = b""
        raw.close()
        assert not data.startswith(b"HTTP/1.1 200")
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        eng.store.close()


def test_https_missing_certificate_is_a_clear_error(tmp_path):
    from bandwatch.server import make_ssl_context

    with pytest.raises(RuntimeError, match="TLS"):
        make_ssl_context(str(tmp_path / "missing.pem"), str(tmp_path / "missing.key"))


# -- malformed upstream data -------------------------------------------------------------

BAD_BANDIX = [
    b"not json",
    b"[]",
    b'{"status":"error","message":"x"}',
    b'{"status":"success","data":null}',
    b'{"status":"success","data":{"d":"string"}}',
    b'{"status":"success","data":{"d":[null, 1, "x", {"mac": 5}]}}',
    b'{"status":"success","data":{"d":[{"mac":"aa:bb:cc:dd:ee:ff","w_rx_b":"NaN","w_tx_b":-5,"ip4":{"x":1},"host":"<script>"}]}}',
    b'{"status":"success","data":{"d":[{"mac":"aa:bb:cc:dd:ee:ff","w_rx_b":1e400,"w_tx_b":12}]}}',
]


@pytest.mark.parametrize("payload", BAD_BANDIX)
def test_bandix_malformed_responses_never_crash_or_invent_data(fake, settings, payload):
    fake.http["http://127.0.0.1:8686/api/system/info"] = (200, {"data": {"version": "0.12.12"}})
    fake.http["http://127.0.0.1:8686/api/traffic/devices"] = (200, payload)
    prov = perdevice.BandixProvider(fake, settings)
    sink = Sink()
    for _ in range(3):
        prov.poll(fake.mono, sink)
        fake.advance(3)
    assert all(r[4] >= 0 and r[5] >= 0 for r in sink.rows)
    assert sink.total("dev", "aa:bb:cc:dd:ee:ff")[0] == 0


def test_bandix_version_fallback_endpoint(fake, settings):
    fake.http["http://127.0.0.1:8686/api/version"] = (200, {"version": "0.12.12"})
    fake.http["http://127.0.0.1:8686/api/traffic/devices"] = (200, {"status": "success", "data": {"d": []}})
    prov = perdevice.BandixProvider(fake, settings)
    assert prov.poll(fake.mono, Sink()) == {}
    assert prov.version == "0.12.12"


@pytest.mark.parametrize("reply", [b"garbage", b'{"result": "x"}', b'{"error": {"code": 1, "message": "Unauthorized"}}', b'{"result": [[1], [2]]}', b'{"result": [{"faultCode": 1}, 1, 2, 3, 4]}'])
def test_aria2_malformed_rpc_responses(fake, settings, tmp_path, reply):
    fake.http["http://127.0.0.1:6800/jsonrpc"] = (200, reply)
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    sink = Sink()
    assert col.poll(fake.mono, fake.wall, sink) is None
    assert col.status.state in ("error", "unavailable")
    assert not sink.rows


def test_aria2_short_download_between_polls_reported_not_counted(fake, settings, tmp_path):
    rpc = Aria2Fake()
    fake.http["http://127.0.0.1:6800/jsonrpc"] = rpc
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    sink = Sink()
    col.poll(fake.mono, fake.wall, sink)
    rpc.tasks["z9"] = {"gid": "z9", "status": "complete", "totalLength": "5000", "completedLength": "5000", "uploadLength": "0", "files": []}
    fake.advance(3)
    live = col.poll(fake.mono, fake.wall, sink)
    assert sink.total("aria2", "*") == (0, 0)
    assert live["uncounted"] == {"tasks": 1, "bytes": 5000}


@pytest.mark.parametrize("text", ["", "garbage", "Inter-|\n face |\nwwan0: 1 2 3\n", "x\ny\nwwan0: a b c d e f g h i j k l m n o p\n"])
def test_wan_malformed_proc_net_dev(fake, settings, text):
    fake.files["/proc/net/dev"] = text
    fake.files["/proc/net/route"] = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\nwwan0\t00000000\t0\t0003\t0\t0\t0\t00000000\n"
    col = wan.WanCollector(fake, settings)
    sink = Sink()
    for _ in range(3):
        col.sample(fake.mono, sink)
        fake.advance(2)
    assert sum(r[4] for r in sink.rows) == 0


def test_ubus_garbage_falls_back_to_routes(fake, settings):
    fake.has_ubus = True
    fake.ubus_replies[("network.interface", "dump")] = {"interface": "not-a-list"}
    counters = {"wwan0": (100, 10)}
    fake.files["/proc/net/dev"] = lambda: net_dev(counters)
    fake.files["/proc/net/route"] = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\nwwan0\t00000000\t0\t0003\t0\t0\t0\t00000000\n"
    col = wan.WanCollector(fake, settings)
    col.sample(fake.mono, Sink())
    assert col.source == "routes" and list(col.ifaces) == ["wwan0"]


VNSTAT_DOCS = [
    {},
    {"interfaces": "x"},
    {"interfaces": [{"name": "x", "traffic": {"day": [{"date": "bad", "rx": 1}]}}]},
    {"jsonversion": "1", "interfaces": [{"name": "eth0", "traffic": {"days": [{"date": {"year": 2026, "month": 1, "day": 1}, "rx": 2, "tx": 3}]}}]},
]


@pytest.mark.parametrize("doc", VNSTAT_DOCS)
def test_vnstat_malformed(doc):
    out = extras.parse_vnstat(doc) if isinstance(doc.get("interfaces", []), list) else None
    if out is not None:
        assert isinstance(out["interfaces"], dict)


# -- reliability ------------------------------------------------------------------------------


class SlowBandixDemo(demo.DemoPlatform):
    """Bandix answers only after 2.4 s (just under the client timeout)."""

    def http_request(self, url, body=None, timeout=2.0, headers=None):
        if ":8686" in url:
            time.sleep(2.4)
        return super().http_request(url, body, timeout, headers)


def test_slow_provider_does_not_block_wan_sampling(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.p = SlowBandixDemo()
    eng._build_collectors()
    run_for(eng, 7.0)
    pts = [p[0] for p in eng.wan_live.data]
    gaps = [(b - a) / 1000.0 for a, b in itertools.pairwise(pts)]
    assert len(pts) >= 5
    assert max(gaps) < 1.8, gaps  # 1 s interval kept despite 2.4 s Bandix calls
    eng.store.close()


def test_flush_io_does_not_block_sample_accumulation(tmp_path):
    st = stmod.Store(str(tmp_path / "x.db"))
    orig = st._write

    def slow_write(pending):
        time.sleep(1.5)
        orig(pending)

    st._write = slow_write
    st.add("wan", "*", 1_790_000_010.0, 10.0, 100, 1)
    t = threading.Thread(target=st.flush, args=(1_790_000_020.0, None))
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    st.add("wan", "*", 1_790_000_012.0, 2.0, 50, 1)
    assert time.monotonic() - t0 < 0.2
    # in-flight data stays visible while the write is in progress
    t.join()
    assert st.summary("wan", "*", 1_789_999_000, 1_790_001_000)[stmod.RX] == 150
    st.close()


def test_long_running_bounded_memory_and_exact_totals(fake, settings, tmp_path):
    """Two simulated days of 2 s WAN samples with periodic flush/prune."""
    st = stmod.Store(str(tmp_path / "long.db"))
    counters = {"wwan0": (0, 0)}
    fake.files["/proc/net/dev"] = lambda: net_dev(counters)
    fake.files["/proc/net/route"] = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\nwwan0\t00000000\t0\t0003\t0\t0\t0\t00000000\n"
    fake.files["/sys/class/net/wwan0/ifindex"] = "4\n"
    col = wan.WanCollector(fake, settings)
    offset = fake.wall - fake.mono

    def emit(series, key, end, dt, rx, tx, rate_ok=True, est=False, count=True):
        st.add(series, key, end + offset, dt, rx, tx, rate_ok, est, count)

    total = 0
    start_wall = fake.wall
    max_pending = 0
    for i in range(2 * 86400 // 2):
        rx, tx = counters["wwan0"]
        inc = 1000 + (i % 50) * 37
        counters["wwan0"] = (rx + inc, tx + inc // 10)
        if i:
            total += inc
        col.sample(fake.mono, emit)
        fake.advance(2)
        max_pending = max(max_pending, len(st.pending))
        if i % 150 == 0:
            st.flush(fake.wall, {"minute_days": 1, "quarter_days": 400, "device_quarter_days": 120, "day_days": 1830})
    st.flush(fake.wall, None)
    assert st.summary("wan", "*", start_wall - 10, fake.wall + 10)[stmod.RX] == total
    assert max_pending <= 2 * 8  # 5 minutes of minute buckets x 2 keys (+ boundary)
    rows_m1 = st.conn.execute("SELECT count(*) FROM m1").fetchone()[0]
    assert rows_m1 <= 2 * 1500  # pruned to ~1 day x 2 keys
    st.close()


def test_concurrent_dashboard_clients(tmp_path):
    from test_server import free_port

    from bandwatch.auth import Auth
    from bandwatch.engine import Engine
    from bandwatch.server import App, serve

    port = free_port()
    service = cfgmod.ServiceConfig(listen=["loopback"], port=port, data_dir=str(tmp_path), run_dir=str(tmp_path / "run"), auth="none", demo=True)
    eng = Engine(service, demo.DemoPlatform(), settings=cfgmod.Settings(str(tmp_path / "s.json")))
    demo.backfill(eng.store, days=10)
    app = App(eng, Auth(service.data_dir, service.run_dir, "none"), service)
    servers = serve(app, ["127.0.0.1"], port)
    eng.start()
    errors, latencies = [], []

    def client(path, n):
        for _ in range(n):
            t0 = time.monotonic()
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                c.request("GET", path, headers={"Host": "127.0.0.1"})
                r = c.getresponse()
                r.read()
                if r.status != 200:
                    errors.append(r.status)
                c.close()
            except OSError as exc:
                errors.append(str(exc))
            latencies.append(time.monotonic() - t0)

    def sse():
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            c.request("GET", "/api/live/stream", headers={"Host": "127.0.0.1"})
            r = c.getresponse()
            r.fp.readline()
            c.close()
        except OSError as exc:
            errors.append("sse " + str(exc))

    threads = [threading.Thread(target=sse) for _ in range(8)]
    paths = ["/api/overview", "/api/devices?period=this_month", "/api/history?series=wan&period=this_month", "/api/health", "/api/aria2?period=today"]
    threads += [threading.Thread(target=client, args=(paths[i % len(paths)], 5)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    eng.stop()
    assert not errors, errors[:5]
    latencies.sort()
    assert latencies[int(len(latencies) * 0.95)] < 5.0


def test_unexpected_shutdown_keeps_flushed_data(tmp_path):
    """SIGKILL the demo service; the database must open cleanly afterwards."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([sys.executable, "-m", "bandwatch", "demo", "--port", str(port), "--data-dir", str(tmp_path), "--days", "0", "--fast"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        db = tmp_path / "bandwatch-demo.db"
        deadline = time.time() + 60
        flushed = 0
        while time.time() < deadline:
            time.sleep(1)
            try:
                c = sqlite3.connect(str(db), timeout=1)
                flushed = c.execute("SELECT count(*) FROM m1").fetchone()[0]
                c.close()
            except sqlite3.Error:
                continue
            if flushed:
                break
        assert flushed, "no flush happened"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
    st = stmod.Store(str(db), mode="demo")
    assert st.recovered_from is None
    assert st.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert st.conn.execute("SELECT count(*) FROM m1").fetchone()[0] >= flushed
    st.close()


def test_listen_any_with_lan_does_not_double_bind(fake):
    from bandwatch.server import resolve_listen

    service = cfgmod.ServiceConfig(listen=["lan", "any", "::1"], data_dir="/tmp/x", auth="password")
    assert resolve_listen(service, fake) == ["0.0.0.0", "::1"]
    service = cfgmod.ServiceConfig(listen=["lan", "192.168.1.1"], data_dir="/tmp/x", auth="password")
    assert resolve_listen(service, fake) == ["127.0.0.1", "192.168.1.1"]
