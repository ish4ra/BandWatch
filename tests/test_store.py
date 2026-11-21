"""SQLite persistence, roll-ups, recovery and analytics correctness."""

import datetime as dt
import os
import sqlite3

import pytest

from bandwatch import analytics
from bandwatch import store as stmod
from bandwatch import tz as tzmod

COLOMBO = tzmod.PosixTimezone("<+0530>-5:30")
RET = {"minute_days": 7, "quarter_days": 400, "device_quarter_days": 120, "day_days": 1830}


def local_ts(y, m, d, h=0, mi=0, tz=COLOMBO):
    return dt.datetime(y, m, d, h, mi, tzinfo=tz).timestamp()


@pytest.fixture
def store(tmp_path):
    s = stmod.Store(str(tmp_path / "bw.db"), tzinfo=COLOMBO)
    yield s
    s.close()


def test_accumulate_splits_across_minutes_without_losing_bytes():
    pend = {}
    stmod.accumulate(pend, "wan", "*", 130.0, 20.0, 1001, 333)  # 110..130 spans minute 60 and 120
    assert sorted(t for (_s, _k, t) in pend) == [60, 120]
    assert sum(b[stmod.RX] for b in pend.values()) == 1001
    assert sum(b[stmod.TX] for b in pend.values()) == 333
    assert pend[("wan", "*", 60)][stmod.COV] == 10000
    assert pend[("wan", "*", 120)][stmod.N] == 1


def test_peak_and_min_tracked_only_for_rate_samples():
    pend = {}
    stmod.accumulate(pend, "wan", "*", 2.0, 2.0, 2000, 0)
    stmod.accumulate(pend, "wan", "*", 4.0, 2.0, 6000, 0)
    stmod.accumulate(pend, "wan", "*", 40.0, 36.0, 999999, 0, rate_ok=False)
    b = pend[("wan", "*", 0)]
    assert b[stmod.PRX] == 3000 and b[stmod.NRX] == 1000


def test_flush_rollup_and_query_consistency(store):
    base = local_ts(2026, 11, 20, 23, 50)  # crosses local midnight
    t = base
    for _ in range(600):  # 20 minutes at 2 s
        t += 2
        store.add("wan", "*", t, 2.0, 2000, 500)
    total_before = store.summary("wan", "*", base, base + 3600)
    assert total_before[stmod.RX] == 600 * 2000  # from pending memory
    assert store.flush(t, RET)
    after = store.summary("wan", "*", base, base + 3600)
    assert after[stmod.RX] == 600 * 2000 and after[stmod.TX] == 600 * 500
    d20 = store.summary("wan", "*", local_ts(2026, 11, 20), local_ts(2026, 11, 21))
    d21 = store.summary("wan", "*", local_ts(2026, 11, 21), local_ts(2026, 11, 22))
    assert d20[stmod.RX] == 300 * 2000 and d21[stmod.RX] == 300 * 2000
    rows = store.conn.execute("SELECT day, rx FROM d1 WHERE s='wan' ORDER BY day").fetchall()
    assert rows == [(20261120, 600000), (20261121, 600000)]


def test_repeated_flushes_merge_not_overwrite(store):
    t0 = 1_790_000_000 // 60 * 60
    store.add("wan", "*", t0 + 10, 10, 100, 10)
    store.flush(t0 + 11, RET)
    store.add("wan", "*", t0 + 20, 10, 50, 5)
    store.flush(t0 + 21, RET)
    s = store.summary("wan", "*", t0, t0 + 60)
    assert s[stmod.RX] == 150 and s[stmod.COV] == 20000 and s[stmod.N] == 2


def test_day_tier_used_after_quarter_retention(store):
    # Insert quarters 500 days ago and roll up days, then prune quarters.
    now = local_ts(2026, 11, 21, 12)
    old_day = dt.date(2025, 7, 1)
    a = tzmod.local_midnight(old_day, COLOMBO)
    for q in range(a, a + 86400, 900):
        store.conn.execute("INSERT INTO m15 (s,k,t,rx,tx,cov,n,est) VALUES ('wan','*',?,1000,100,900000,1,0)", (q,))
    store._rollup_days(a, a + 86400)
    store.conn.execute("DELETE FROM m15")
    store.add("wan", "*", now, 2.0, 10, 1)
    store.flush(now, RET)
    s = store.summary("wan", "*", a, a + 86400)
    assert s[stmod.RX] == 96 * 1000
    ch = analytics.chart(store, "wan", "*", tzmod.local_midnight(dt.date(2025, 6, 30), COLOMBO), tzmod.local_midnight(dt.date(2025, 7, 3), COLOMBO), now, a, COLOMBO)
    assert ch["resolution"] in ("hour", "day")


def test_missing_data_is_not_zero(store):
    now = local_ts(2026, 11, 21, 12)
    a, b = local_ts(2026, 11, 21), local_ts(2026, 11, 22)
    first = local_ts(2026, 11, 21, 6)
    for i in range(30):
        store.add("wan", "*", first + 2 * (i + 1), 2.0, 0, 0)
    s = analytics.summary(store, "wan", "*", a, b, now, first)
    assert s["rx"] == 0  # measured idle = real zero
    assert s["coverage"] < 0.01  # but only one minute of six hours measured
    ch = analytics.chart(store, "wan", "*", a, b, now, first, COLOMBO)
    pts = ch["points"]
    assert ch["resolution"] == "hour" and len(pts) == 24
    assert pts[3]["rx"] is None  # before data collection started: no data
    assert pts[6]["rx"] == 0  # measured idle
    assert pts[7]["rx"] is None and pts[7]["coverage"] == 0  # unmeasured past hour
    assert pts[20]["future"] is True


def test_device_rows_use_aggregate_coverage(store):
    now = 1_790_000_600.0
    t = now - 300
    for i in range(100):
        t += 3
        store.add("dev", "*", t, 3.0, 300 if i % 2 else 0, 30)
        if i % 2:
            store.add("dev", "aa:bb:cc:00:00:01", t, 3.0, 300, 30, count=True)
    s = analytics.summary(store, "dev", "aa:bb:cc:00:00:01", now - 300, now, now, now - 300, coverage_key="*")
    assert s["rx"] == 50 * 300
    assert abs(s["measured_s"] - 300) < 1
    assert abs(s["avg_rx"] - 50.0) < 0.5  # averaged over all measured time, including idle
    assert s["min_rx"] == 100  # minimum while active


def test_retention_prunes_tiers(store):
    now = 1_800_000_000.0
    old = now - 10 * 86400
    store.add("wan", "*", old, 2.0, 1, 1)
    store.add("dev", "aa:bb:cc:00:00:01", old, 2.0, 1, 1)
    store.flush(now, RET)
    assert store.conn.execute("SELECT count(*) FROM m1").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM m15").fetchone()[0] == 2
    store.flush(now, dict(RET, device_quarter_days=7))
    assert store.conn.execute("SELECT count(*) FROM m15 WHERE s='dev'").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM d1").fetchone()[0] == 2


def test_corrupt_database_is_quarantined_and_recreated(tmp_path):
    path = tmp_path / "bw.db"
    path.write_bytes(b"SQLite format 3\x00" + os.urandom(2048))
    s = stmod.Store(str(path))
    st = s.status()
    assert st["recovered_from"] and st["schema_version"] == 1
    assert any(f.startswith("bw.db.corrupt-") for f in os.listdir(tmp_path))
    ev = s.events()
    assert ev and ev[0]["kind"] == "recovered"
    s.close()


def test_demo_and_production_databases_never_mix(tmp_path):
    p = str(tmp_path / "x.db")
    stmod.Store(p, mode="demo").close()
    with pytest.raises(stmod.ModeMismatch):
        stmod.Store(p, mode="production")


def test_newer_schema_refused(tmp_path):
    p = str(tmp_path / "x.db")
    s = stmod.Store(p)
    s.set_meta("schema_version", 99)
    s.close()
    with pytest.raises(RuntimeError, match="newer"):
        stmod.Store(p)


def test_migration_from_empty_file_and_reopen(tmp_path):
    p = str(tmp_path / "x.db")
    sqlite3.connect(p).close()
    s = stmod.Store(p)
    assert s.meta("schema_version") == "1"
    s.close()
    s2 = stmod.Store(p)
    assert s2.meta("schema_version") == "1"
    s2.close()


def test_flush_failure_keeps_pending_data(store, monkeypatch):
    store.add("wan", "*", 1_790_000_010.0, 10.0, 100, 1)

    def boom(_p):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_write", boom)
    assert store.flush(1_790_000_020.0, RET) is False
    assert store.last_flush_error
    monkeypatch.undo()
    assert store.flush(1_790_000_030.0, RET)
    assert store.summary("wan", "*", 1_789_999_000, 1_790_001_000)[stmod.RX] == 100


def test_rebuild_days_after_timezone_change(store):
    t = tzmod.local_midnight(dt.date(2026, 11, 21), tzmod.UTC) + 3 * 3600  # 03:00 UTC = 08:30 Colombo
    store.add("wan", "*", t, 2.0, 500, 0)
    store.flush(t, RET)
    assert store.conn.execute("SELECT day FROM d1").fetchall() == [(20261121,)]
    store.rebuild_days(tzmod.PosixTimezone("PST8"))  # 19:00 previous day in UTC-8
    assert store.conn.execute("SELECT day FROM d1").fetchall() == [(20261120,)]


def test_devices_alias_and_tasks(store):
    store.upsert_devices([{"mac": "aa:bb:cc:00:00:01", "hostname": "tv", "ip4": "192.168.1.5", "first_seen": 1, "last_seen": 5}])
    store.set_alias("aa:bb:cc:00:00:01", "Living room")
    store.upsert_devices([{"mac": "aa:bb:cc:00:00:01", "hostname": None, "ip4": "192.168.1.6", "first_seen": 9, "last_seen": None}])
    d = store.devices()["aa:bb:cc:00:00:01"]
    assert d["alias"] == "Living room" and d["hostname"] == "tv" and d["ip4"] == "192.168.1.6" and d["last_seen"] == 5
    store.upsert_tasks([{"gid": "g1", "name": "a.iso", "status": "active", "total": 10, "done": 5, "up": 0, "first_seen": 1, "last_seen": 2}])
    assert store.recent_tasks()[0]["gid"] == "g1"


def test_edges_hour_day_month():
    a = tzmod.local_midnight(dt.date(2026, 11, 21), COLOMBO)
    e, r = analytics.edges_for(a, a + 86400, COLOMBO)
    assert r == "hour" and len(e) == 25
    a2 = tzmod.local_midnight(dt.date(2026, 11, 1), COLOMBO)
    b2 = tzmod.local_midnight(dt.date(2026, 12, 1), COLOMBO)
    e, r = analytics.edges_for(a2, b2, COLOMBO)
    assert r == "day" and len(e) == 31
    a3 = tzmod.local_midnight(dt.date(2026, 1, 1), COLOMBO)
    b3 = tzmod.local_midnight(dt.date(2027, 1, 1), COLOMBO)
    e, r = analytics.edges_for(a3, b3, COLOMBO)
    assert r == "month" and len(e) == 13
