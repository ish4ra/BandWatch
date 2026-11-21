"""Engine with the demo platform, API handlers and demo scenarios."""

import json
import time

import pytest

from bandwatch import api, demo
from bandwatch import config as cfgmod
from bandwatch import store as stmod
from bandwatch.engine import Engine


def make_engine(tmp_path, scenario="aw1000-cellular", days=0, fast=True):
    service = cfgmod.ServiceConfig(listen=["loopback"], data_dir=str(tmp_path), run_dir=str(tmp_path / "run"), auth="none", demo=True)
    settings = cfgmod.Settings(str(tmp_path / "settings.json"))
    settings.update({"general.timezone": demo.DEMO_TZ})
    if fast:
        settings.update({"wan.sample_interval": 1, "devices.poll_interval": 1, "aria2.poll_interval": 1, "storage.flush_interval": 30})
    demo.prepare(service, scenario)
    plat = demo.DemoPlatform(scenario)
    eng = Engine(service, plat, settings=settings)
    if days:
        demo.backfill(eng.store, days=days)
    return eng, plat


def run_for(eng, seconds):
    eng.start()
    time.sleep(seconds)
    eng.stop_event.set()
    eng.thread.join(5)


def test_engine_collects_and_flushes(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 3.5)
    assert eng.flush()
    assert eng.wan_live.last()[1] is not None
    assert eng.loop_errors == 0
    rows = eng.store.conn.execute("SELECT s, count(*) FROM m1 GROUP BY s").fetchall()
    series = {r[0] for r in rows}
    assert {"wan", "dev", "aria2"} <= series
    ov = api.overview(eng)
    assert ov["demo"] is True and ov["today"]["coverage"] is not None
    assert ov["devices"]["known"] >= 9
    # WAN is never smaller than the sum attributed to devices + aria2 payload
    # in the simulation (no double counting across sources).
    s = ov["sources"]
    assert s["wan"] >= s["devices"]
    eng.store.close()


def test_production_engine_refuses_demo_db(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.store.close()
    with pytest.raises(stmod.ModeMismatch):
        stmod.Store(str(tmp_path / "bandwatch-demo.db"), mode="production")


def test_backfill_refuses_production_db(tmp_path):
    s = stmod.Store(str(tmp_path / "p.db"))
    with pytest.raises(RuntimeError):
        demo.backfill(s)


def test_history_api_and_csv(tmp_path):
    eng, _ = make_engine(tmp_path, days=40)
    h = api.history(eng, {"series": "wan", "period": "last_month"})
    assert h["chart"]["resolution"] == "day" and h["summary"]["total"] > 0
    assert 0.8 < h["summary"]["coverage"] <= 1.0
    text, name = api.history_csv(eng, {"series": "wan", "period": "this_week"})
    assert name.endswith(".csv") and "download_bytes" in text and "simulated=yes" in text
    with pytest.raises(api.ApiError):
        api.history(eng, {"series": "bogus"})
    with pytest.raises(api.ApiError):
        api.history(eng, {"period": "custom", "start": "2026-02-10", "end": "2026-01-01"})
    with pytest.raises(api.ApiError):
        api.history(eng, {"series": "dev", "key": "../../etc"})
    data, fname = api.export(eng, {"format": "json", "period": "this_month"})
    assert fname is None and data["simulated"] is True and data["rows"]
    eng.store.close()


def test_devices_api_alias_validation(tmp_path):
    eng, _ = make_engine(tmp_path, days=3)
    d = api.devices(eng, {"period": "yesterday"})
    assert d["devices"] and all("mac" in r for r in d["devices"])
    mac = d["devices"][0]["mac"]
    with pytest.raises(api.ApiError):
        api.set_alias(eng, mac, {"alias": "<script>alert(1)</script>"})
    with pytest.raises(api.ApiError):
        api.set_alias(eng, "zz:zz", {"alias": "x"})
    assert api.set_alias(eng, mac, {"alias": "Office laptop"})["alias"] == "Office laptop"
    det = api.device_detail(eng, mac.upper(), {"period": "yesterday"})
    assert det["device"]["name"] == "Office laptop"
    with pytest.raises(api.ApiError) as exc:
        api.device_detail(eng, "02:00:00:00:00:99", {"period": "today"})
    assert exc.value.status == 404
    eng.store.close()


def test_settings_api(tmp_path):
    eng, _ = make_engine(tmp_path)
    with pytest.raises(api.ApiError) as exc:
        api.settings_update(eng, {"wan.sample_interval": 0})
    assert exc.value.status == 422
    assert api.settings_update(eng, {"general.timezone": "UTC"})["ok"]
    assert eng.tz_desc == "UTC"
    api.set_secret(eng, {"secret": "hunter2"})
    got = api.settings_get(eng)
    assert got["secrets"]["aria2_secret"] is True
    assert "hunter2" not in json.dumps(got)
    with pytest.raises(api.ApiError):
        api.set_secret(eng, {"secret": "bad\nsecret"})
    eng.store.close()


def test_diagnostics_are_sanitised(tmp_path):
    eng, plat = make_engine(tmp_path)
    run_for(eng, 2.5)
    eng.set_aria2_secret("topsecret")
    text = json.dumps(eng.diagnostics())
    for mac in [d[1] for d in demo.DEVICES]:
        assert mac not in text
    for d in demo.DEVICES:
        assert d[2] not in text  # IPs
        assert d[0] not in text  # hostnames
    assert "topsecret" not in text
    assert "192.168.1.1" not in text
    eng.store.close()


@pytest.mark.parametrize("scenario", sorted(demo.SCENARIOS))
def test_demo_scenarios_run_without_errors(tmp_path, scenario):
    eng, plat = make_engine(tmp_path, scenario=scenario)
    now = time.monotonic()
    for k in plat.next_event:
        plat.next_event[k] = now + 1.0
    run_for(eng, 3.5)
    assert eng.loop_errors == 0
    assert eng.flush()
    ov = api.overview(eng)
    if scenario == "no-bandix":
        assert ov["devices_state"]["state"] == "unavailable"
        assert ov["today"]["total"] is not None  # WAN monitoring still works
    if scenario == "no-aria2":
        assert ov["aria2_available"] is False
    if scenario == "db-recovery":
        assert eng.store.recovered_from
    if scenario == "reboot":
        assert any(e[3] == "counter_reset" for e in eng.recent_events) or eng.wan.boot_id != plat.boot
    eng.store.close()


def test_restart_recovers_gap_from_counters(tmp_path):
    eng, plat = make_engine(tmp_path, scenario="fibre-ethernet")
    run_for(eng, 2.5)
    assert eng.flush()
    before = eng.store.summary("wan", "*", 0, 4_000_000_000)[stmod.RX]
    eng.store.close()
    time.sleep(1.5)  # "downtime": the simulated router keeps counting
    # New engine on the same DB and the same (still running) router.
    service = eng.service
    eng2 = Engine(service, plat, settings=eng.settings)
    run_for(eng2, 2.5)
    eng2.flush()
    est = eng2.store.summary("wan", "*", 0, 4_000_000_000)[stmod.EST]
    after = eng2.store.summary("wan", "*", 0, 4_000_000_000)[stmod.RX]
    assert est > 0 and after > before
    assert any(e[3] == "gap_recovered" for e in eng2.recent_events)
    eng2.store.close()
