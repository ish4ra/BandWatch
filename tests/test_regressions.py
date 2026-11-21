"""Regression tests for issues found during the pre-beta audit."""

import datetime as dt
import json
import os

import pytest

from bandwatch import api, demo
from bandwatch import tz as tzmod
from test_engine_api import make_engine, run_for

# -- demo must never expose host machine storage or paths ---------------------


def test_demo_statvfs_never_reads_host_filesystem(tmp_path):
    plat = demo.DemoPlatform()
    assert plat.statvfs(str(tmp_path)) is None
    assert plat.statvfs("/") is None
    assert plat.statvfs("/home") is None
    assert plat.statvfs("/overlay")["total"] == 512 * 2**20


def test_demo_health_has_no_host_storage_or_paths(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 2.5)
    h = api.health(eng)
    text = json.dumps(h)
    assert str(tmp_path) not in text
    labels = [x["label"] for x in h["system"]["storage"]]
    assert "Filesystem holding BandWatch data" not in labels
    host = os.statvfs(str(tmp_path))
    host_total = host.f_blocks * host.f_frsize
    assert all(x["total"] != host_total for x in h["system"]["storage"])
    assert h["data_dir"] == "(demo data directory)"
    assert "/" not in h["database"]["path"]
    s = api.settings_get(eng)
    assert str(tmp_path) not in json.dumps(s)
    eng.store.close()


def test_production_storage_separates_database_from_filesystem(fake, settings, tmp_path):
    from bandwatch.collectors.identity import SystemCollector

    fake.files["/proc/stat"] = "cpu  1 0 1 10 0 0 0 0 0 0\n"
    sizes = {"/overlay": {"total": 100, "free": 40, "used": 60}, str(tmp_path): {"total": 100, "free": 40, "used": 60}}
    fake.statvfs = sizes.get
    info = SystemCollector(fake, str(tmp_path)).poll(fake.mono)
    assert [x["label"] for x in info["storage"]] == ["Overlay (flash)"]
    assert info["data_filesystem"] == "Overlay (flash)"
    sizes[str(tmp_path)] = {"total": 900, "free": 100, "used": 800}  # USB disk
    info = SystemCollector(fake, str(tmp_path)).poll(fake.mono)
    assert info["data_filesystem"] == "Filesystem holding BandWatch data"


# -- timezone labels ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("<+0530>-5:30", "UTC+05:30"),
        ("<-03>3", "UTC-03:00"),
        ("UTC", "UTC"),
        ("CET-1CEST,M3.5.0,M10.5.0/3", "CET (UTC+01:00)"),
    ],
)
def test_timezone_labels_hide_posix_strings(spec, expected):
    tzinfo, desc = tzmod.resolve(spec)
    winter = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
    assert tzmod.label(tzinfo, desc, winter, spec) == expected


def test_timezone_label_dst_and_system_zonename(tmp_path):
    tzinfo, desc = tzmod.resolve("CET-1CEST,M3.5.0,M10.5.0/3")
    summer = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp()
    assert tzmod.label(tzinfo, desc, summer, "x") == "CEST (UTC+02:00)"
    cfg = tmp_path / "system"
    cfg.write_text("config system\n\toption zonename 'Asia/Colombo'\n\toption timezone '<+0530>-5:30'\n")
    tzinfo, desc = tzmod.resolve("<+0530>-5:30")
    assert tzmod.label(tzinfo, desc, summer, "system", str(cfg)) == "Asia/Colombo (UTC+05:30)"


def test_api_reports_friendly_timezone(tmp_path):
    eng, _ = make_engine(tmp_path, days=1)
    h = api.history(eng, {"series": "wan", "period": "today"})
    assert h["period"]["timezone"] == "UTC+05:30"
    text, _ = api.history_csv(eng, {"series": "wan", "period": "today"})
    assert "timezone=UTC+05:30" in text and "<+0530>" not in text
    eng.store.close()


# -- aria2 Today / This month ------------------------------------------------------------


def test_aria2_api_has_today_and_month(tmp_path):
    eng, _ = make_engine(tmp_path, days=40)
    run_for(eng, 2.5)
    a = api.aria2(eng, {"period": "last_month"})
    for key in ("today", "month"):
        assert a[key]["total"] is not None and a[key]["total"] >= 0
        assert a[key]["coverage"] is not None
    assert a["month"]["total"] >= a["today"]["total"]
    assert a["summary"]["total"] != a["month"]["total"]  # selected period is independent
    eng.store.close()


# -- device history: every period and custom ranges --------------------------------------


@pytest.mark.parametrize("period", ["today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "last_7_days", "last_30_days"])
def test_device_detail_every_period(tmp_path, period):
    eng, _ = make_engine(tmp_path, days=70)
    mac = demo.DEVICES[0][1]
    d = api.device_detail(eng, mac, {"period": period})
    assert d["chart"]["points"] and d["summary"]["expected_s"] >= 0
    s = d["summary"]
    if s["total"] is not None:
        assert s["total"] == s["rx"] + s["tx"]
        assert s["peak_rx"] is None or s["peak_rx"] >= (s["min_rx"] or 0)
    eng.store.close()


def test_device_custom_range_equals_sum_of_days(tmp_path):
    eng, _ = make_engine(tmp_path, days=30)
    mac = demo.DEVICES[1][1]
    today = tzmod.local_date(eng.now(), eng.tz)
    start, end = today - dt.timedelta(days=10), today - dt.timedelta(days=4)
    custom = api.device_detail(eng, mac, {"period": "custom", "start": start.isoformat(), "end": end.isoformat()})
    total = 0
    d = start
    while d <= end:
        one = api.device_detail(eng, mac, {"period": "custom", "start": d.isoformat(), "end": d.isoformat()})
        total += one["summary"]["rx"] or 0
        d += dt.timedelta(days=1)
    assert custom["summary"]["rx"] == total
    assert custom["chart"]["resolution"] == "day" and len(custom["chart"]["points"]) == 7
    long = api.device_detail(eng, mac, {"period": "custom", "start": (today - dt.timedelta(days=200)).isoformat(), "end": today.isoformat()})
    assert long["chart"]["resolution"] == "month"
    with pytest.raises(api.ApiError):
        api.device_detail(eng, mac, {"period": "custom", "start": "2026-13-01", "end": "2026-12-01"})
    eng.store.close()


def test_device_lan_traffic_kept_separate(tmp_path):
    eng, _ = make_engine(tmp_path, days=3)
    nas = next(d[1] for d in demo.DEVICES if d[0] == "home-nas")
    d = api.device_detail(eng, nas, {"period": "yesterday"})
    assert d["lan_summary"]["rx"] > 0
    wan_total = api.history(eng, {"series": "dev", "period": "yesterday"})["summary"]["rx"]
    assert d["summary"]["rx"] <= wan_total
    eng.store.close()
