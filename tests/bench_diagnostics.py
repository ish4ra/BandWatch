"""Cost of the self-monitoring and report subsystem (not a pytest test).

    cd tests && python3 bench_diagnostics.py

Numbers depend on the machine; docs/PERFORMANCE.md says where they were taken.
"""

import os
import pathlib
import sys
import tempfile
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bandwatch import report, selfmon
from test_engine_api import make_engine, run_for

INFO = {"self_cpu_percent": 0.6, "self_rss": 18_000_000, "self_threads": 7, "cpu_percent": 16.1, "mem_percent": 32.9}


def counters(i):
    return {"wan_bytes": i * 1000, "samples": i, "late": 0, "missed": 0, "coll_err": 0, "loop_err": 0}


def main():
    m = selfmon.SelfMonitor()
    n = 200_000
    t = time.perf_counter()
    for i in range(n):
        m.observe(1.79e9 + i * 5, i * 5.0, INFO, counters(i))
    per = (time.perf_counter() - t) / n
    print("observe(): %.2f us/call; at one call per 5 s = %.6f%% of one core" % (per * 1e6, per / 5 * 100))

    tracemalloc.start()
    m2 = selfmon.SelfMonitor()
    for i in range(96 * 180 + 10):
        m2.observe(1.79e9 + i * 5, i * 5.0, INFO, counters(i))
    cur = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    print("self-monitor memory with 24 h of intervals: %.1f KiB (%d intervals)" % (cur / 1024, len(m2.closed)))

    eng, _ = make_engine(pathlib.Path(tempfile.mkdtemp()))
    run_for(eng, 3)
    eng.flush()
    size0 = eng.store.status()["size_bytes"]
    eng.store.add_health([dict(m2.closed[0], t=m2.closed[0]["t"] + k * 900) for k in range(2976)])
    eng.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("health table, 31 days (2976 rows): ~%.0f KiB" % ((eng.store.status()["size_bytes"] - size0) / 1024))

    report.build(eng)
    runs = 20
    t = time.perf_counter()
    for _ in range(runs):
        r = report.build(eng)
    print("report.build(): %.1f ms; JSON %d bytes; ZIP %d bytes" % ((time.perf_counter() - t) / runs * 1000, len(report.to_json(r)), len(report.to_zip(r))))
    eng.store.close()


if __name__ == "__main__":
    main()
