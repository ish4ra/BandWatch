"""The diagnostic report is built from an allow-list: hostile provider
responses, names and paths planted everywhere must never reach it."""

import io
import json
import re
import zipfile

import pytest

from bandwatch import report
from bandwatch.collectors import aria2 as aria2mod
from bandwatch.collectors import perdevice
from bandwatch.config import SecretStore
from conftest import FakePlatform
from test_engine_api import make_engine, run_for

PLANTED = [
    "SECRET-ARIA2-TOKEN", "hunter2-ppp", "apn-password-xyz", "MyHomeWiFi-SSID", "wifi-key-0123456789",
    "Secret Family Video.mkv", "/home/alice", "/mnt/sda1/private", "alice-laptop", "Bob's iPhone",
    "aa:bb:cc:dd:ee:ff", "AA-BB-CC-11-22-33", "192.168.1.77", "10.20.30.40", "203.0.113.9", "2001:db8::77",
    "fe80::1234", "http://", "https://", "example.org", "<script>", "csrf-token-abcdef", "session-cookie-123",
    "Asia/Colombo-secret-tz",
]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC = re.compile(r"\b[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}\b")


class HostileAria2:
    """aria2 that answers with an error quoting secrets, addresses and paths."""

    def __call__(self, url, body):
        return 200, {"jsonrpc": "2.0", "id": "bw", "error": {"code": 1, "message": "Unauthorized token:SECRET-ARIA2-TOKEN from 192.168.1.77 for /mnt/sda1/private/Secret Family Video.mkv"}}


def _plant(eng, tmp_path):
    """Put hostile values everywhere the report could conceivably read."""
    eng.set_aria2_secret("SECRET-ARIA2-TOKEN")
    # Collector messages, notes and extras quoting provider text.
    for st in (eng.wan.status, eng.devices.status, eng.identity.status, eng.vnstat.status, eng.system.status):
        st.message = "failed at http://192.168.1.77/ for alice-laptop aa:bb:cc:dd:ee:ff SECRET-ARIA2-TOKEN"
        st.notes = ["note /home/alice MyHomeWiFi-SSID wifi-key-0123456789"]
        st.extra["leak"] = {"ssid": "MyHomeWiFi-SSID", "key": "wifi-key-0123456789", "pppoe": "hunter2-ppp", "apn": "apn-password-xyz"}
    eng.devices.status.version = "0.12.12 192.168.1.77"  # not a version token: dropped
    eng.devices.status.code = "aa:bb:cc:dd:ee:ff"  # invalid code: reported as unknown
    # Events with personal text.
    eng._event("warning", "aria2", "aria2_down", "download 'Secret Family Video.mkv' from 10.20.30.40 by Bob's iPhone AA-BB-CC-11-22-33")
    eng._event("info", "<script>", "kind with spaces", "csrf-token-abcdef session-cookie-123")
    # System info with paths, hostnames and a hostile model string.
    info = eng.system.info
    info["storage"] = [{"label": "Filesystem holding BandWatch data", "path": "/home/alice/bandwatch", "total": 100, "free": 50, "used": 50}, {"label": "/mnt/sda1/private", "path": "/mnt/sda1/private", "total": 1, "free": 1, "used": 0}]
    info["hostname"] = "alice-laptop"
    info["board"] = dict(info.get("board") or {}, model="<script>alert(1)</script> 192.168.1.77", hostname="alice-laptop", release="25.12.5", kernel="6.12.94")
    eng.system.board = info["board"]
    # WAN interface descriptions carry the logical (user-chosen) name.
    for itf in eng.wan.ifaces.values():
        itf.logical = "alice-laptop"
    # Device identities.
    for d in list(eng.identity.devices.values())[:3]:
        d.hostname = "Bob's iPhone"
    eng.store.set_alias("aa:bb:cc:dd:ee:ff", "alice-laptop")
    eng.store.event("warning", "aria2", "aria2_down", "stored event hunter2-ppp 2001:db8::77 fe80::1234")
    # Settings with personal values (URLs are allowed to point at the LAN).
    # (written directly: LAN provider URLs need allow_lan_providers)
    eng.settings.values.update({"devices.bandix_url": "http://admin:hunter2-ppp@192.168.1.77:8686", "aria2.url": "http://10.20.30.40:6800/jsonrpc?token=SECRET-ARIA2-TOKEN"})
    eng.offload_notes = eng.offload_notes + ["Note quoting http://example.org/ MyHomeWiFi-SSID"]
    eng.bind_addresses = ["192.168.1.77", "127.0.0.1", "2001:db8::77"]


def _assert_clean(text):
    for s in PLANTED:
        assert s.lower() not in text.lower(), "leaked %r" % s
    assert not MAC.search(text), MAC.search(text).group(0)
    m = IPV4.search(text)
    assert not m, "IPv4-like value %r leaked" % (m.group(0) if m else "")


@pytest.mark.parametrize("scenario", ["aw1000-cellular", "hostile-names"])
def test_report_never_contains_planted_personal_data(tmp_path, scenario):
    eng, plat = make_engine(tmp_path, scenario=scenario)
    run_for(eng, 2.5)
    eng.flush()
    _plant(eng, tmp_path)
    r = report.build(eng)
    text = report.to_json(r)
    _assert_clean(text)
    _assert_clean(report.summary_text(r))
    # The demo's own device names, MACs and IPs are absent too.
    from bandwatch import demo

    for name, mac, ip, *_ in demo.DEVICES:
        assert mac not in text and name not in text and ip not in text
    # Still useful: states, codes and counts survive.
    assert r["listening"]["bound_scopes"] == ["lan", "loopback"]
    assert r["settings"]["devices.bandix_endpoint_scope"] == "lan"
    assert r["aria2"]["endpoint_scope"] == "lan" and r["aria2"]["secret_configured"] is True
    assert r["router"]["model"] is None and r["router"]["openwrt_release"] == "25.12.5"
    assert next(c for c in r["collectors"] if c["name"] == "devices")["code"] == "unknown"
    assert next(c for c in r["collectors"] if c["name"] == "devices")["version"] is None
    assert all(i["device"] for i in r["wan"]["interfaces"])
    assert "other.other" in r["events"]["counts_by_kind"]
    eng.store.close()


def test_hostile_aria2_error_is_reduced_to_a_code(tmp_path):
    fake = FakePlatform()
    fake.http["http://127.0.0.1:6800/jsonrpc"] = HostileAria2()
    eng, _ = make_engine(tmp_path)
    eng.aria2 = aria2mod.Aria2Collector(fake, eng.settings, SecretStore(str(tmp_path / "s.json")))
    eng.aria2.poll(fake.mono, fake.wall, lambda *a, **k: None)
    assert "SECRET-ARIA2-TOKEN" in eng.aria2.status.message  # the local UI shows it ...
    r = report.build(eng)
    text = report.to_json(r)
    assert "SECRET-ARIA2-TOKEN" not in text and "Secret Family Video" not in text and "192.168.1.77" not in text  # ... the report does not
    a = r["aria2"]
    assert a["code"] == "auth_rejected" and a["rpc_connected"] is False
    assert next(c for c in r["checks"] if c["id"] == "aria2_rpc")["status"] == "failed"
    eng.store.close()


def test_hostile_bandix_reply_is_reduced_to_a_code(tmp_path, settings):
    fake = FakePlatform()
    fake.http["http://127.0.0.1:8686/api/system/info"] = (200, {"data": {"version": "0.12.12; rm -rf / 192.168.1.77"}})
    fake.http["http://127.0.0.1:8686/api/traffic/devices"] = (200, {"status": "error", "message": "token SECRET-ARIA2-TOKEN alice-laptop"})
    eng, _ = make_engine(tmp_path)
    eng.devices = perdevice.BandixProvider(fake, eng.settings)
    eng.devices.poll(fake.mono, lambda *a, **k: None)
    r = report.build(eng)
    _assert_clean(report.to_json(r))
    dev = next(c for c in r["collectors"] if c["name"] == "devices")
    assert dev["code"] == "bad_response" and dev["version"] is None
    eng.store.close()


def test_report_statuses_are_from_the_fixed_set_and_untested_is_never_passed(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 2.0)
    r = report.build(eng)
    assert {c["status"] for c in r["checks"]} <= set(report.STATUSES)
    by = {c["id"]: c["status"] for c in r["checks"]}
    for cid in ("traffic_accuracy", "wan_isolation", "aria2_accounting", "reboot_recovery", "history_across_restart"):
        assert by[cid] == "not_tested", cid
    assert by["cpu_target"] == "insufficient_data"  # < 1 hour of self-monitoring
    assert by["bandix_direction"] in ("insufficient_data", "not_tested")
    assert r["unverified"] and sum(r["summary_counts"].values()) == len(r["checks"])
    eng.store.close()


def test_aw1000_like_snapshot_is_classified_as_normal_startup(tmp_path):
    """The first AW1000 snapshot: production-like start, 300 s interval,
    no flush yet, pending buckets, zero rows -> 'collecting', not a failure."""
    eng, _ = make_engine(tmp_path, fast=False)
    run_for(eng, 2.0)
    r = report.build(eng)
    p = r["persistence"]
    assert p["state"] == "collecting" and p["last_successful_save"] is None and p["pending_buckets"] > 0
    assert p["rows"]["m1"] == 0 and p["flush_interval_s"] == 300
    assert r["service"]["uptime_s"] < 60  # the missing field in beta.1's export
    assert next(c for c in r["checks"] if c["id"] == "persistence")["status"] == "insufficient_data"
    eng.store.close()


def test_zip_contains_json_and_summary(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 1.5)
    r = report.build(eng)
    data = report.to_zip(r)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["SUMMARY.txt", "bandwatch-report.json"]
        doc = json.loads(zf.read("bandwatch-report.json"))
        assert doc["schema"] == "bandwatch-diagnostic-report" and doc["schema_version"] == 1
        assert b"Checks" in zf.read("SUMMARY.txt")
    assert re.match(r"^bandwatch-report-[0-9A-Za-z.+-]+-\d{8}-\d{4}\.zip$", report.filename(r, "zip"))
    eng.store.close()


def test_report_without_zlib_falls_back_to_stored(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_zlib(name, *a, **k):
        if name == "zlib":
            raise ImportError("no zlib")
        return real(name, *a, **k)

    eng, _ = make_engine(tmp_path)
    r = report.build(eng)
    monkeypatch.setattr(builtins, "__import__", no_zlib)
    data = report.to_zip(r)
    monkeypatch.setattr(builtins, "__import__", real)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
    eng.store.close()


def test_missing_ssl_and_hashlib_give_guidance_not_failure():
    g = report.dependency_guidance({"ssl": False, "_hashlib": False, "zlib": True}, https_enabled=False)
    mods = {x["module"]: x for x in g}
    assert "Optional" in mods["ssl"]["impact"] and "python3-openssl" in mods["ssl"]["action"]
    assert "never falls back" in mods["ssl"]["action"]
    assert "slower" in mods["_hashlib"]["impact"]
    g = report.dependency_guidance({"ssl": False, "_hashlib": True, "zlib": True}, https_enabled=True)
    assert "cannot start" in g[0]["impact"]


SCOPES = [
    ("127.0.0.1", "loopback"), ("localhost", "loopback"), ("192.168.1.1", "lan"), ("10.0.0.1", "lan"), ("0.0.0.0", "wildcard"),
    ("8.8.8.8", "public"), ("fe80::1", "link_local"), ("::1", "loopback"), ("router.lan", "name"),
]


@pytest.mark.parametrize("host,scope", SCOPES)
def test_address_scope(host, scope):
    assert report.address_scope(host) == scope
