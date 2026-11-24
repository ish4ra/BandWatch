"""BandWatch self-monitoring: bounded history of its own resource use.

Every system poll (5 s) feeds one observation: BandWatch CPU, RSS and
threads, router CPU/memory, and cumulative sampling and error counters kept
by the engine. Observations are folded into 15-minute intervals:

* the open interval is a handful of numbers (no per-sample storage);
* the last 24 hours of closed intervals stay in memory (96 small dicts);
* closed intervals are written to the ``health`` table together with the
  regular batched flush (no extra flash writes) and kept for 31 days.

The figures describe BandWatch under whatever load the router had at the
time - including serving the dashboard. They are not idle benchmarks;
"low-traffic" intervals are reported separately, still without claiming
that the router was idle.
"""

import collections
import threading

INTERVAL = 900
KEEP_MEMORY = 96
LOW_TRAFFIC_BPS = 12_500  # 100 kbit/s average WAN traffic
WINDOWS = (("last_hour", 3600), ("last_24h", 86400), ("last_7d", 7 * 86400), ("last_30d", 30 * 86400))


class SelfMonitor:
    def __init__(self, store=None):
        self.store = store
        self.cur = None
        self.closed = collections.deque(maxlen=KEEP_MEMORY)
        self.unsaved = []
        self.observations = 0
        # observe() runs in the WAN thread; summaries are read by web threads.
        self.lock = threading.RLock()

    # -- feeding ------------------------------------------------------------------

    def _open(self, wall, mono, counters):
        self.cur = {
            "t": int(wall),
            "mono": mono,
            "last_mono": mono,
            "n": 0,
            "cpu_n": 0,
            "cpu_sum": 0.0,
            "cpu_peak": None,
            "rss_sum": 0,
            "rss_n": 0,
            "rss_peak": None,
            "thr_peak": None,
            "sys_n": 0,
            "sys_sum": 0.0,
            "mem_n": 0,
            "mem_sum": 0.0,
            "base": dict(counters),
        }

    def observe(self, wall, mono, info, counters):
        """One observation. ``counters`` are cumulative totals:
        ``wan_bytes``, ``samples``, ``late``, ``missed``, ``coll_err``, ``loop_err``."""
        with self.lock:
            self._observe(wall, mono, info, counters)

    def _observe(self, wall, mono, info, counters):
        self.observations += 1
        if self.cur is None:
            self._open(wall, mono, counters)
        elif mono - self.cur["mono"] >= INTERVAL:
            self._close(counters)
            self._open(wall, mono, counters)
        c = self.cur
        c["n"] += 1
        c["last_mono"] = mono
        c["last_counters"] = dict(counters)
        cpu = info.get("self_cpu_percent")
        if isinstance(cpu, (int, float)):
            c["cpu_n"] += 1
            c["cpu_sum"] += cpu
            c["cpu_peak"] = cpu if c["cpu_peak"] is None else max(c["cpu_peak"], cpu)
        rss = info.get("self_rss")
        if isinstance(rss, int):
            c["rss_n"] += 1
            c["rss_sum"] += rss
            c["rss_peak"] = rss if c["rss_peak"] is None else max(c["rss_peak"], rss)
        thr = info.get("self_threads")
        if isinstance(thr, int):
            c["thr_peak"] = thr if c["thr_peak"] is None else max(c["thr_peak"], thr)
        for key, src in (("sys", "cpu_percent"), ("mem", "mem_percent")):
            v = info.get(src)
            if isinstance(v, (int, float)):
                c[key + "_n"] += 1
                c[key + "_sum"] += v

    def _row(self, c, counters):
        base = c["base"]

        def d(k):
            return max(0, int(counters.get(k, 0) - base.get(k, 0)))

        return {
            "t": c["t"],
            "dur": int(round(c["last_mono"] - c["mono"])) + 5,
            "n": c["n"],
            "cpu_avg": round(c["cpu_sum"] / c["cpu_n"], 3) if c["cpu_n"] else None,
            "cpu_peak": c["cpu_peak"],
            "rss_avg": int(c["rss_sum"] / c["rss_n"]) if c["rss_n"] else None,
            "rss_peak": c["rss_peak"],
            "thr_peak": c["thr_peak"],
            "sys_cpu_avg": round(c["sys_sum"] / c["sys_n"], 2) if c["sys_n"] else None,
            "mem_avg": round(c["mem_sum"] / c["mem_n"], 2) if c["mem_n"] else None,
            "wan_bytes": d("wan_bytes"),
            "samples": d("samples"),
            "late": d("late"),
            "missed": d("missed"),
            "coll_err": d("coll_err"),
            "loop_err": d("loop_err"),
        }

    def _close(self, counters):
        row = self._row(self.cur, counters)
        self.closed.append(row)
        self.unsaved.append(row)
        del self.unsaved[:-KEEP_MEMORY]

    def finish(self):
        """Close the open interval at shutdown if it holds at least a minute."""
        with self.lock:
            if self.cur and self.cur["n"] >= 12:
                self._close(self.cur.get("last_counters", self.cur["base"]))
            self.cur = None

    def take_unsaved(self):
        with self.lock:
            rows, self.unsaved = self.unsaved, []
        return rows

    def restore_unsaved(self, rows):
        with self.lock:
            self.unsaved = (rows + self.unsaved)[-KEEP_MEMORY:]

    def current(self):
        """The open interval as a row (not persisted)."""
        with self.lock:
            if not self.cur or not self.cur["n"]:
                return None
            return self._row(self.cur, self.cur.get("last_counters", self.cur["base"]))

    # -- summaries ----------------------------------------------------------------------

    def rows(self, since):
        out = {}
        with self.lock:
            closed = list(self.closed)
        if self.store is not None and since < (closed[0]["t"] if closed else float("inf")):
            try:
                for r in self.store.health_rows(since):
                    out[r["t"]] = r
            except Exception:
                pass
        for r in closed:
            if r["t"] >= since:
                out[r["t"]] = r
        cur = self.current()
        if cur and cur["t"] + cur["dur"] >= since:
            out[cur["t"]] = cur
        return [out[k] for k in sorted(out)]

    @staticmethod
    def aggregate(rows):
        if not rows:
            return {"intervals": 0, "samples": 0, "duration_s": 0}
        n = sum(r["n"] for r in rows)
        cpu_rows = [r for r in rows if r.get("cpu_avg") is not None]
        rss_rows = [r for r in rows if r.get("rss_avg") is not None]
        cpu_w = sum(r["n"] for r in cpu_rows)
        rss_w = sum(r["n"] for r in rss_rows)
        quiet = [r for r in cpu_rows if r["dur"] > 0 and (r.get("wan_bytes") or 0) / r["dur"] < LOW_TRAFFIC_BPS]
        quiet_w = sum(r["n"] for r in quiet)
        samples = sum(r.get("samples") or 0 for r in rows)
        missed = sum(r.get("missed") or 0 for r in rows)

        def mx(key):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return max(vals) if vals else None

        return {
            "intervals": len(rows),
            "samples": n,
            "duration_s": sum(r["dur"] for r in rows),
            "from": rows[0]["t"],
            "cpu_avg_percent": round(sum(r["cpu_avg"] * r["n"] for r in cpu_rows) / cpu_w, 2) if cpu_w else None,
            "cpu_peak_percent": mx("cpu_peak"),
            "rss_avg_bytes": int(sum(r["rss_avg"] * r["n"] for r in rss_rows) / rss_w) if rss_w else None,
            "rss_peak_bytes": mx("rss_peak"),
            "threads_peak": mx("thr_peak"),
            "router_cpu_avg_percent": _wavg(rows, "sys_cpu_avg"),
            "router_mem_avg_percent": _wavg(rows, "mem_avg"),
            "low_traffic_cpu_avg_percent": round(sum(r["cpu_avg"] * r["n"] for r in quiet) / quiet_w, 2) if quiet_w else None,
            "low_traffic_intervals": len(quiet),
            "wan_samples": samples,
            "wan_samples_late": sum(r.get("late") or 0 for r in rows),
            "wan_samples_missed": missed,
            "missed_ratio": round(missed / (samples + missed), 4) if samples + missed else None,
            "collector_errors": sum(r.get("coll_err") or 0 for r in rows),
            "loop_errors": sum(r.get("loop_err") or 0 for r in rows),
        }

    def summaries(self, now):
        return {name: self.aggregate(self.rows(now - secs)) for name, secs in WINDOWS}


def _wavg(rows, key):
    vals = [(r[key], r["n"]) for r in rows if r.get(key) is not None]
    w = sum(n for _v, n in vals)
    return round(sum(v * n for v, n in vals) / w, 2) if w else None
