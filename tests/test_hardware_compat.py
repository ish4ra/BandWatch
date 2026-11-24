"""Portable compatibility fixes driven by the first physical AW1000 snapshot
(OpenWrt 25.12.5, qualcommax/ipq807x, ModemManager WAN on wwan0, Bandix
0.12.12, aria2 1.37.0 without an RPC secret, Python without ssl/_hashlib).
Nothing here is AW1000-specific: the same code paths serve any router."""

import builtins
import json

import pytest

from bandwatch import auth as authmod
from bandwatch import report
from bandwatch import server as srvmod
from bandwatch.collectors import aria2 as aria2mod
from bandwatch.collectors import wan as wanmod
from bandwatch.config import SecretStore
from conftest import net_dev
from test_collectors import Aria2Fake, Sink
from test_engine_api import make_engine, run_for

# ModemManager on OpenWrt: the logical interface (proto modemmanager) brings
# the modem up; the IP configuration lives on dynamic sub-interfaces
# (<name>_4 static/dhcp, <name>_6 dhcpv6) on the same L3 device.
MM_DUMP = {
    "interface": [
        {"interface": "lan", "up": True, "proto": "static", "l3_device": "br-lan", "route": []},
        {"interface": "wwan", "up": True, "proto": "modemmanager", "l3_device": "wwan0", "device": "wwan0", "route": []},
        {"interface": "wwan_4", "up": True, "proto": "static", "l3_device": "wwan0", "device": "wwan0", "route": [{"target": "0.0.0.0", "mask": 0, "nexthop": "10.64.0.1"}]},
        {"interface": "wwan_6", "up": True, "proto": "dhcpv6", "l3_device": "wwan0", "device": "wwan0", "route": [{"target": "::", "mask": 0, "nexthop": "fe80::1"}]},
    ]
}


def test_modemmanager_wan_is_one_device_counted_once(fake, settings):
    fake.has_ubus = True
    fake.ubus_replies[("network.interface", "dump")] = MM_DUMP
    fake.files["/sys/class/net/wwan0/type"] = "519"  # ARPHRD_RAWIP (QMI/MBIM raw-ip)
    fake.files["/sys/class/net/wwan0/ifindex"] = "7"
    fake.files["/proc/net/dev"] = net_dev({"wwan0": (1_000_000, 200_000), "br-lan": (5, 5)})
    col = wanmod.WanCollector(fake, settings)
    col.sample(fake.mono, Sink())
    assert col.source == "ubus" and list(col.ifaces) == ["wwan0"]
    itf = col.ifaces["wwan0"]
    assert itf.families == {"ipv4", "ipv6"} and not itf.tunnel
    assert set(itf.logical.split("+")) == {"wwan_4", "wwan_6"}
    sink = Sink()
    fake.advance(2)
    fake.files["/proc/net/dev"] = net_dev({"wwan0": (3_000_000, 600_000), "br-lan": (9, 9)})
    col.sample(fake.mono, sink)
    assert sink.total("wan", "*") == (2_000_000, 400_000)  # once, not per sub-interface


def test_modem_reconnect_rebaselines_without_spikes(fake, settings):
    fake.has_ubus = True
    fake.ubus_replies[("network.interface", "dump")] = MM_DUMP
    fake.files["/sys/class/net/wwan0/ifindex"] = "7"
    fake.files["/proc/net/dev"] = net_dev({"wwan0": (10_000_000, 1_000_000)})
    col = wanmod.WanCollector(fake, settings)
    sink = Sink()
    col.sample(fake.mono, sink)
    fake.advance(2)
    fake.files["/proc/net/dev"] = net_dev({})  # modem resets: device gone
    col.sample(fake.mono, sink)
    fake.advance(2)
    fake.files["/sys/class/net/wwan0/ifindex"] = "9"  # re-created with fresh counters
    fake.files["/proc/net/dev"] = net_dev({"wwan0": (5_000, 1_000)})
    col.sample(fake.mono, sink)
    fake.advance(2)
    fake.files["/proc/net/dev"] = net_dev({"wwan0": (15_000, 3_000)})
    col.sample(fake.mono, sink)
    rx, tx = sink.total("wan", "*")
    assert rx <= 15_000 and tx <= 3_000  # never the 10 MB "negative wrap"


def test_aria2_without_secret_sends_no_token_and_report_warns_without_claiming_exposure(fake, settings, tmp_path):
    rpc = Aria2Fake()
    fake.http["http://127.0.0.1:6800/jsonrpc"] = rpc
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    assert col.poll(fake.mono, fake.wall, Sink())["active"] == 1
    body = fake.http_calls[-1][1]
    assert all(not (c["params"] and isinstance(c["params"][0], str) and c["params"][0].startswith("token:")) for c in body["params"][0])
    (tmp_path / "e").mkdir()
    eng, _ = make_engine(tmp_path / "e")
    eng.aria2 = col
    r = report.build(eng)
    assert r["aria2"]["rpc_connected"] is True and r["aria2"]["secret_configured"] is False
    chk = {c["id"]: c for c in r["checks"]}
    assert chk["aria2_rpc"]["status"] == "passed"
    assert chk["aria2_rpc_secret"]["status"] == "warning" and "did not test" in chk["aria2_rpc_secret"]["detail"]
    assert chk["aria2_accounting"]["status"] == "not_tested"  # connectivity != verified accounting
    eng.store.close()


def test_aria2_with_secret_sends_token_and_no_warning(fake, settings, tmp_path):
    rpc = Aria2Fake()
    rpc.secret = "s3"
    fake.http["http://127.0.0.1:6800/jsonrpc"] = rpc
    secrets = SecretStore(str(tmp_path / "s.json"))
    secrets.set("aria2_secret", "s3")
    col = aria2mod.Aria2Collector(fake, settings, secrets)
    assert col.poll(fake.mono, fake.wall, Sink())["active"] == 1
    (tmp_path / "e").mkdir()
    eng, _ = make_engine(tmp_path / "e")
    eng.aria2 = col
    eng.secrets = secrets
    r = report.build(eng)
    assert r["aria2"]["accepts_requests_without_secret"] is None
    assert "aria2_rpc_secret" not in {c["id"] for c in r["checks"]}
    eng.store.close()


def _without(monkeypatch, *names):
    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name in names or name.split(".")[0] in names:
            raise ImportError("No module named %r (simulated)" % name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_core_runs_without_ssl_and_hashlib(tmp_path, monkeypatch):
    """AW1000: python3 without ssl and _hashlib. Collection, persistence, the
    report and password hashing must all keep working."""
    eng, _ = make_engine(tmp_path)
    _without(monkeypatch, "ssl", "_hashlib")
    monkeypatch.delattr(authmod.hashlib, "pbkdf2_hmac")  # as on Python built without OpenSSL
    run_for(eng, 2.0)
    assert eng.flush() and eng.loop_errors == 0
    r = report.build(eng)
    mods = r["runtime"]["python"]["modules"]
    assert mods["ssl"] is False and mods["_hashlib"] is False
    guidance = {g["module"] for g in r["runtime"]["dependency_guidance"]}
    assert guidance >= {"ssl", "_hashlib"}
    https = next(c for c in r["checks"] if c["id"] == "https")
    assert https["status"] == "unavailable"  # optional: not a failure while HTTPS is off
    a = authmod.Auth(str(tmp_path), str(tmp_path / "run"))
    a.set_password("long enough password")
    assert a.check_password("long enough password") and not a.check_password("wrong")
    eng.store.close()


def test_https_without_ssl_module_gives_actionable_error(monkeypatch):
    _without(monkeypatch, "ssl")
    with pytest.raises(RuntimeError, match="python3-openssl"):
        srvmod.make_ssl_context("/nonexistent/cert.pem", "/nonexistent/key.pem")


def test_aw1000_like_demo_report_matches_the_physical_snapshot_shape(tmp_path):
    eng, _ = make_engine(tmp_path, scenario="aw1000-cellular")
    run_for(eng, 2.5)
    r = report.build(eng)
    assert r["router"]["target"] == "qualcommax/ipq807x" and r["router"]["arch"] == "aarch64_cortex-a53"
    assert [i["device"] for i in r["wan"]["interfaces"]] == ["wwan0"]
    assert r["devices"]["provider"] == "bandix" and r["devices"]["provider_version"] == "0.12.12"
    assert r["aria2"]["version"] == "1.37.0"
    assert {c["name"]: c["state"] for c in r["collectors"]}["wan"] == "ok"
    assert json.loads(report.to_json(r))["schema_version"] == report.SCHEMA_VERSION
    eng.store.close()
