"""Database persistence, service lifecycle, restart/reboot recovery and
bounded self-monitoring (beta.2 reliability work, from the AW1000 findings)."""

import os
import shutil
import sqlite3
import time

import pytest

from bandwatch import engine as engmod
from bandwatch import selfmon
from bandwatch import store as stmod
from test_engine_api import make_engine, run_for


def restart(tmp_path, plat=None, **kw):
    """A new engine on the same data directory (and optionally the same simulated router)."""
    return make_engine(tmp_path, plat=plat, **kw)


def stop_threads(eng):
    eng.stop_event.set()
    for t in (eng.thread, eng.aux_thread):
        if t:
            t.join(5)


# -- first start and first flush ------------------------------------------------------


def test_empty_database_on_first_start_is_reported_as_normal(tmp_path):
    eng, _ = make_engine(tmp_path, fast=False)  # 300 s flush interval like the AW1000
    run_for(eng, 2.0)
    p = eng.persistence_status()
    assert p["state"] == "collecting"
    assert "normal" in p["explanation"]
    assert p["last_successful_save"] is None and p["pending_buckets"] > 0
    # The first save is scheduled sooner than the 300 s interval.
    assert p["next_flush_in_s"] is not None and p["next_flush_in_s"] <= engmod.FIRST_FLUSH_DELAY
    assert p["service_uptime_s"] <= 5 and p["service_started"] > 0
    eng.store.close()


def test_first_flush_happens_early_then_regular_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(engmod, "FIRST_FLUSH_DELAY", 2.5)
    eng, _ = make_engine(tmp_path, fast=False)
    run_for(eng, 4.5)
    p = eng.persistence_status()
    assert p["state"] == "ok", p
    assert p["last_successful_save"] is not None and p["rows"]["m1"] > 0
    assert p["flush_count"] == 1  # exactly one early save, then the 300 s schedule
    assert p["next_flush_in_s"] > 250
    eng.store.close()


def test_empty_flush_is_not_reported_as_a_save(tmp_path):
    st = stmod.Store(str(tmp_path / "x.db"))
    assert st.flush()
    s = st.status()
    assert s["last_flush"] is not None and s["last_write"] is None
    st.add("wan", "*", 1_790_000_040, 60, 100, 50)
    assert st.flush()
    assert st.status()["last_write"] is not None and st.status()["last_write_rows"] == 1
    st.close()


# -- restart, reboot, unclean stop ------------------------------------------------------


def test_clean_restart_preserves_history_and_is_classified(tmp_path):
    eng, plat = make_engine(tmp_path)
    assert eng.lifecycle["current"]["kind"] == "first_start"
    run_for(eng, 2.0)
    eng.store.add("wan", "*", time.time() - 600, 60, 1000, 500)  # older data
    eng.stop()
    before = sqlite3.connect(str(tmp_path / "bandwatch-demo.db")).execute("SELECT sum(rx) FROM m1 WHERE s='wan' AND k='*'").fetchone()[0]
    assert before and before >= 1000
    eng2, _ = restart(tmp_path, plat)
    cur = eng2.lifecycle["current"]
    assert cur["kind"] == "restart" and cur["previous_stop"] == "clean"
    assert eng2.lifecycle["starts"] == 2
    assert eng2.store.summary("wan", "*", 0, time.time() + 60)[stmod.RX] == before
    assert eng2.persistence_status()["history_predates_this_start"] is True
    eng2.store.close()


def test_unclean_stop_is_detected(tmp_path):
    eng, plat = make_engine(tmp_path)
    run_for(eng, 1.5)
    eng.flush()
    eng.store.close()  # no stop(): crash / kill -9 / power loss
    eng2, _ = restart(tmp_path, plat)
    assert eng2.lifecycle["current"]["previous_stop"] == "unclean"
    assert any(e[3] == "service_restart" and e[1] == "warning" for e in eng2.recent_events)
    eng2.store.close()


def test_router_reboot_is_detected_from_boot_id(tmp_path):
    eng, plat = make_engine(tmp_path)
    eng.stop()
    plat.boot = plat._new_boot_id()  # simulated reboot
    eng2, _ = restart(tmp_path, plat)
    assert eng2.lifecycle["current"]["kind"] == "after_reboot"
    eng2.store.close()


def test_upgrade_from_database_without_lifecycle(tmp_path):
    # A beta.1 database has first_start but none of the lifecycle keys.
    st = stmod.Store(str(tmp_path / "bandwatch-demo.db"), mode="demo")
    st.set_meta("first_start", 1_790_000_000)
    st.add("wan", "*", 1_790_000_060, 60, 1234, 56)
    st.flush()
    st.close()
    eng, _ = make_engine(tmp_path)
    assert eng.lifecycle["current"]["kind"] == "upgrade"
    assert eng.store.summary("wan", "*", 0, 1_790_100_000)[stmod.RX] == 1234
    assert eng.lifecycle["first_start"] == 1_790_000_000
    eng.store.close()


def test_lifecycle_history_is_bounded(tmp_path):
    for _ in range(engmod.LIFECYCLE_KEEP + 5):
        eng, _ = make_engine(tmp_path)
        eng.stop()
    eng, _ = make_engine(tmp_path)
    assert len(eng.lifecycle["history"]) == engmod.LIFECYCLE_KEEP
    assert eng.lifecycle["starts"] == engmod.LIFECYCLE_KEEP + 6
    eng.store.close()


# -- samples gathered before the clock is trusted ----------------------------------------


def _untrusted_engine(tmp_path):
    eng, plat = make_engine(tmp_path)
    eng.clock_trusted = False
    eng.prebuffer = {}
    stmod.accumulate(eng.prebuffer, "wan", "*", 5000.0, 60.0, 4242, 4141)
    return eng, plat


def test_pending_count_includes_prebuffer(tmp_path):
    eng, _ = _untrusted_engine(tmp_path)
    p = eng.persistence_status()
    assert p["state"] == "waiting_for_clock" and p["prebuffered_buckets"] >= 1 and p["pending_buckets"] >= 1
    eng.store.close()


def test_prebuffer_survives_restart_within_the_same_boot(tmp_path):
    eng, plat = _untrusted_engine(tmp_path)
    eng.stop()  # used to discard these samples
    eng2, _ = restart(tmp_path, plat)
    assert sum(b[stmod.RX] for b in eng2.prebuffer.values()) == 4242
    assert any(e[3] == "prebuffer_restored" for e in eng2.recent_events)
    assert eng2.store.get_state("prebuffer") is None  # consumed once
    eng2.store.close()


def test_prebuffer_is_dropped_after_reboot(tmp_path):
    eng, plat = _untrusted_engine(tmp_path)
    eng.stop()
    plat.boot = plat._new_boot_id()
    eng2, _ = restart(tmp_path, plat)
    assert eng2.prebuffer == {}
    assert any(e[3] == "prebuffer_lost" for e in eng2.recent_events)
    eng2.store.close()


def test_restored_prebuffer_is_persisted_once_clock_is_trusted(tmp_path):
    eng, plat = _untrusted_engine(tmp_path)
    eng.stop()
    eng2, _ = restart(tmp_path, plat)
    run_for(eng2, 1.5)  # demo clock is trusted immediately
    eng2.flush()
    assert eng2.store.summary("wan", "*", 0, time.time() + 3600)[stmod.RX] >= 4242
    eng2.store.close()


# -- WAL, power loss, flush failures ----------------------------------------------------------


def test_clean_stop_checkpoints_the_wal(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 1.5)
    eng.stop()
    wal = str(tmp_path / "bandwatch-demo.db-wal")
    assert not os.path.exists(wal) or os.path.getsize(wal) == 0


def test_power_loss_snapshot_is_consistent(tmp_path):
    st = stmod.Store(str(tmp_path / "a.db"))
    for i in range(30):
        st.add("wan", "*", 1_790_000_000 + i * 60, 60, 1000, 10)
    st.flush()
    st.add("wan", "*", 1_790_010_000, 60, 999_999, 0)  # unflushed: lost on power cut
    # Copy the files while the connection is still open (no checkpoint, no close).
    cut = tmp_path / "cut"
    cut.mkdir()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(str(tmp_path / "a.db") + suffix):
            shutil.copy(str(tmp_path / "a.db") + suffix, str(cut / "a.db") + suffix)
    st.close()
    again = stmod.Store(str(cut / "a.db"))
    assert again.integrity == "ok" and again.recovered_from is None
    assert again.summary("wan", "*", 0, 1_800_000_000)[stmod.RX] == 30_000
    again.close()


def test_flush_failure_is_reported_and_retried(tmp_path, monkeypatch):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 1.5)
    real = eng.store._write
    calls = {"n": 0}

    def failing(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return real(batch)

    monkeypatch.setattr(eng.store, "_write", failing)
    assert eng.flush() is False
    p = eng.persistence_status()
    assert p["state"] == "failing" and p["last_flush_error_code"] == "disk_full" and p["pending_buckets"] > 0
    assert eng.flush() is True
    assert eng.persistence_status()["state"] == "ok"
    eng.store.close()


ERRS = [
    ("database or disk is full", "disk_full"), ("attempt to write a readonly database", "read_only"), ("database is locked", "locked"),
    ("database disk image is malformed", "corrupt"), ("disk I/O error", "io_error"), ("weird", "other"), (None, None),
]


@pytest.mark.parametrize("text,code", ERRS)
def test_flush_error_codes(text, code):
    assert stmod.flush_error_code(text) == code


def test_corrupt_database_is_recreated_and_reported(tmp_path):
    path = tmp_path / "bandwatch-demo.db"
    path.write_bytes(b"this is not a database" * 100)
    eng, _ = make_engine(tmp_path)
    p = eng.persistence_status()
    assert p["integrity_check"] == "recreated" and p["recovered_from_corruption"] is True
    eng.store.close()


def test_health_table_does_not_change_schema_version(tmp_path):
    st = stmod.Store(str(tmp_path / "s.db"))
    assert st.meta("schema_version") == "1"
    assert st.conn.execute("SELECT name FROM sqlite_master WHERE name='health'").fetchone()
    st.close()


# -- bounded self-monitoring ------------------------------------------------------------------


def _counters(i):
    return {"wan_bytes": i * 1000, "samples": i * 2, "late": 0, "missed": i // 100, "coll_err": 0, "loop_err": 0}


def test_selfmon_memory_is_bounded_and_weighted():
    m = selfmon.SelfMonitor()
    mono, wall = 0.0, 1_790_000_000.0
    for i in range(20000):  # ~28 hours of 5 s observations
        m.observe(wall + i * 5, mono + i * 5, {"self_cpu_percent": 1.0 if i % 2 else 3.0, "self_rss": 18_000_000, "self_threads": 7, "cpu_percent": 10.0, "mem_percent": 33.0}, _counters(i))
    assert len(m.closed) == selfmon.KEEP_MEMORY
    assert len(m.unsaved) <= selfmon.KEEP_MEMORY
    s = m.summaries(wall + 20000 * 5)["last_hour"]
    assert s["cpu_avg_percent"] == pytest.approx(2.0, abs=0.05)
    assert s["cpu_peak_percent"] == 3.0 and s["rss_peak_bytes"] == 18_000_000 and s["threads_peak"] == 7
    assert 2600 <= s["duration_s"] <= 4600 and s["samples"] > 500  # intervals that started in the window


def test_selfmon_rows_persist_with_flush_and_survive_restart(tmp_path):
    eng, plat = make_engine(tmp_path)
    m = eng.monitor
    for i in range(400):
        m.observe(time.time() - 4000 + i * 5, i * 5.0, {"self_cpu_percent": 0.6, "self_rss": 18_059_264, "self_threads": 7}, _counters(i))
    assert m.unsaved
    eng.flush()
    assert not m.unsaved and eng.store.health_rows(0)
    eng.stop()
    eng2, _ = restart(tmp_path, plat)
    s = eng2.monitor.summaries(time.time())["last_24h"]
    assert s["intervals"] >= 2 and s["cpu_avg_percent"] == pytest.approx(0.6)
    eng2.store.close()


def test_engine_feeds_monitor_and_sampling_stats(tmp_path):
    eng, _ = make_engine(tmp_path)
    run_for(eng, 3.0)
    assert eng.monitor.observations >= 1
    assert eng.sample_stats["samples"] >= 2
    cur = eng.monitor.current()
    assert cur and cur["n"] >= 1
    eng.store.close()


# -- Bandix direction verification -----------------------------------------------------------


class _Snap:
    def __init__(self, down, up):
        self.down, self.up = down, up


def _direction_engine(tmp_path):
    eng, _ = make_engine(tmp_path)
    eng.devices.status.provider = "bandix"
    eng.aria2.live = None
    return eng


def _feed(eng, wrx, wtx, down, up, n):
    for _ in range(n):
        eng.wan_live.push(time.time(), wrx, wtx)
        eng._direction_sample({"aa": _Snap(down, up)})


def test_direction_pending_explains_what_is_needed(tmp_path):
    eng = _direction_engine(tmp_path)
    v = eng.direction_verdict()
    assert v["state"] == "insufficient_data" and v["samples"] == 0 and v["required"] == 20
    assert "never creates traffic" in v["message"]
    eng.store.close()


def test_direction_ignores_router_generated_and_symmetric_traffic(tmp_path):
    eng = _direction_engine(tmp_path)
    _feed(eng, 2_000_000, 50_000, 10_000, 2_000, 30)  # WAN busy, LAN devices idle (router download)
    eng.aria2.live = {"down": 1_900_000, "up": 0}
    _feed(eng, 2_000_000, 50_000, 1_000_000, 20_000, 30)  # aria2 on the router dominates
    eng.aria2.live = None
    _feed(eng, 1_000_000, 900_000, 900_000, 1_000_000, 30)  # symmetric: cannot tell
    v = eng.direction_verdict()
    assert v["state"] == "insufficient_data"
    assert v["skipped"]["unattributed"] == 30 and v["skipped"]["router_traffic"] == 30 and v["skipped"]["symmetric"] == 30
    eng.store.close()


def test_direction_consistent_and_swapped(tmp_path):
    eng = _direction_engine(tmp_path)
    _feed(eng, 2_000_000, 60_000, 1_900_000, 55_000, 25)
    assert eng.direction_verdict()["state"] == "consistent"
    eng.direction_check.clear()
    _feed(eng, 2_000_000, 60_000, 55_000, 1_900_000, 25)
    v = eng.direction_verdict()
    assert v["state"] == "suspect_swapped"
    assert eng.settings["devices.bandix_direction"] == "rx_is_download"  # never swapped automatically
    eng.store.close()


def test_direction_not_applicable_without_bandix(tmp_path):
    eng = _direction_engine(tmp_path)
    eng.devices.status.provider = "nlbwmon"
    assert eng.direction_verdict()["state"] == "not_applicable"
    eng.store.close()
