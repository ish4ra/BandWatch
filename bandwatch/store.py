"""SQLite persistence: tiered usage buckets, devices, events and aria2 tasks.

Storage tiers (all byte values are integers):

* ``m1``  - 1-minute buckets, UTC aligned, short retention (default 7 days).
* ``m15`` - 15-minute buckets, UTC aligned. Every real-world UTC offset is a
  multiple of 15 minutes, so local days/hours can be assembled exactly.
* ``d1``  - local calendar days (``YYYYMMDD`` in the configured timezone),
  long retention.

Each bucket row carries: ``rx``/``tx`` bytes, ``cov`` (measured milliseconds),
``prx``/``ptx`` peak rates, ``nrx``/``ntx`` minimum rates (bytes/s), ``n``
(sample count) and ``est`` (bytes whose timing was estimated, e.g. traffic
recovered after a service restart).

Series: ``wan`` (key ``*`` = all WAN devices, or the device name),
``dev`` (per-device Internet traffic, key = MAC, ``*`` = all devices),
``devl`` (per-device LAN-local traffic), ``aria2`` (key ``*``).

Samples are accumulated in memory and written in batches every
``flush_interval`` seconds to limit flash wear. Samples not yet flushed are
lost on power failure.
"""

import json
import os
import sqlite3
import threading
import time

from . import SCHEMA_VERSION, log
from . import tz as tzmod

COLS = ("rx", "tx", "cov", "prx", "ptx", "nrx", "ntx", "n", "est")
# index positions inside an in-memory bucket list
RX, TX, COV, PRX, PTX, NRX, NTX, N, EST = range(9)

BUCKET_CAP_MS = {"m1": 60_000, "m15": 900_000}

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS m1 (
  s TEXT NOT NULL, k TEXT NOT NULL, t INTEGER NOT NULL,
  rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
  cov INTEGER NOT NULL DEFAULT 0, prx INTEGER, ptx INTEGER, nrx INTEGER, ntx INTEGER,
  n INTEGER NOT NULL DEFAULT 0, est INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (s, k, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS m15 (
  s TEXT NOT NULL, k TEXT NOT NULL, t INTEGER NOT NULL,
  rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
  cov INTEGER NOT NULL DEFAULT 0, prx INTEGER, ptx INTEGER, nrx INTEGER, ntx INTEGER,
  n INTEGER NOT NULL DEFAULT 0, est INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (s, k, t)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS m15_t ON m15 (t);
CREATE TABLE IF NOT EXISTS d1 (
  s TEXT NOT NULL, k TEXT NOT NULL, day INTEGER NOT NULL,
  rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0,
  cov INTEGER NOT NULL DEFAULT 0, prx INTEGER, ptx INTEGER, nrx INTEGER, ntx INTEGER,
  n INTEGER NOT NULL DEFAULT 0, est INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (s, k, day)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS d1_day ON d1 (day);
CREATE TABLE IF NOT EXISTS devices (
  mac TEXT PRIMARY KEY, alias TEXT, hostname TEXT, ip4 TEXT, ip6 TEXT,
  conn TEXT, iface TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER NOT NULL, level TEXT NOT NULL,
  source TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  gid TEXT PRIMARY KEY, name TEXT, status TEXT, total INTEGER, done INTEGER,
  up INTEGER, first_seen INTEGER, last_seen INTEGER, finished INTEGER,
  counted_rx INTEGER NOT NULL DEFAULT 0, counted_tx INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tasks_seen ON tasks (last_seen);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
"""

# Ordered list of (version, sql). Version 1 is the initial schema.
MIGRATIONS = [(1, SCHEMA_V1)]

MAX_EVENTS = 2000
MAX_TASKS = 1000


class ModeMismatch(RuntimeError):
    """Raised when a demo database is opened in production mode or vice versa."""


def _merge_bucket(dst, src):
    dst[RX] += src[RX]
    dst[TX] += src[TX]
    dst[COV] += src[COV]
    for i in (PRX, PTX):
        if src[i] is not None and (dst[i] is None or src[i] > dst[i]):
            dst[i] = src[i]
    for i in (NRX, NTX):
        if src[i] is not None and (dst[i] is None or src[i] < dst[i]):
            dst[i] = src[i]
    dst[N] += src[N]
    dst[EST] += src[EST]


def new_bucket():
    return [0, 0, 0, None, None, None, None, 0, 0]


def _merge_into(dst, src):
    for key, bucket in src.items():
        cur = dst.get(key)
        if cur is None:
            dst[key] = list(bucket)
        else:
            _merge_bucket(cur, bucket)
            cur[COV] = min(cur[COV], 60000)


def accumulate(pending, series, key, end, dt, rx, tx, rate_ok=True, est=False, count=True):
    """Add bytes measured over ``dt`` seconds ending at ``end`` into minute buckets.

    The interval is split proportionally across minute boundaries; the
    integer parts always sum to the original byte counts. Peak and minimum
    rates are updated only when ``rate_ok`` (the interval was short enough
    for its average rate to be meaningful).
    """
    if dt <= 0:
        return
    rx, tx = int(rx), int(tx)
    rate_rx = rx / dt if rate_ok else None
    rate_tx = tx / dt if rate_ok else None
    t0 = end - dt
    remaining_rx, remaining_tx = rx, tx
    while t0 < end - 1e-9:
        minute = int(t0 // 60) * 60
        t1 = min(end, minute + 60)
        last = t1 >= end - 1e-9
        frac = (t1 - t0) / dt
        part_rx = remaining_rx if last else min(remaining_rx, int(round(rx * frac)))
        part_tx = remaining_tx if last else min(remaining_tx, int(round(tx * frac)))
        remaining_rx -= part_rx
        remaining_tx -= part_tx
        b = pending.get((series, key, minute))
        if b is None:
            b = pending[(series, key, minute)] = new_bucket()
        b[RX] += part_rx
        b[TX] += part_tx
        b[COV] = min(60000, b[COV] + int(round((t1 - t0) * 1000)))
        if est:
            b[EST] += part_rx + part_tx
        if rate_rx is not None:
            if b[PRX] is None or rate_rx > b[PRX]:
                b[PRX] = int(rate_rx)
            if b[NRX] is None or rate_rx < b[NRX]:
                b[NRX] = int(rate_rx)
            if b[PTX] is None or rate_tx > b[PTX]:
                b[PTX] = int(rate_tx)
            if b[NTX] is None or rate_tx < b[NTX]:
                b[NTX] = int(rate_tx)
        if count and last:
            b[N] += 1
        t0 = t1


class Store:
    def __init__(self, path, mode="production", tzinfo=None):
        self.path = path
        self.mode = mode
        self.tz = tzinfo or tzmod.UTC
        self.lock = threading.RLock()  # serialises use of the SQLite connection
        # Sample accumulation has its own lock so collectors never wait for
        # database I/O (a flush can take a while on slow flash).
        self.plock = threading.Lock()
        self.pending = {}  # (s, k, minute_t) -> bucket list
        self.inflight = {}  # buckets currently being written by flush()
        self.recovered_from = None
        self.journal_mode = None
        self.last_flush = None
        self.last_flush_error = None
        self.flush_count = 0
        self.conn = self._open()

    # -- opening, recovery and migrations -----------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _open(self):
        directory = os.path.dirname(self.path)
        if directory and self.path != ":memory:":
            os.makedirs(directory, mode=0o750, exist_ok=True)
        try:
            conn = self._connect()
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
            if not row or row[0] != "ok":
                raise sqlite3.DatabaseError("quick_check: %s" % (row[0] if row else "no result"))
            self._configure(conn)
            self._migrate(conn)
        except sqlite3.DatabaseError as exc:
            if self.path == ":memory:":
                raise
            log.error("database %s unusable (%s); moving it aside and starting fresh", self.path, exc)
            try:
                conn.close()
            except Exception:
                pass
            self.recovered_from = self._quarantine()
            conn = self._connect()
            self._configure(conn)
            self._migrate(conn)
            self._event(conn, "error", "store", "recovered", "Database was damaged and has been recreated; the damaged copy was kept for inspection.")
        self._check_mode(conn)
        if self.path != ":memory:":
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.chmod(self.path + suffix, 0o640)
                except OSError:
                    pass
        return conn

    def _quarantine(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = "%s.corrupt-%s" % (self.path, stamp)
        for suffix in ("", "-wal", "-shm", "-journal"):
            src = self.path + suffix
            if os.path.exists(src):
                try:
                    os.replace(src, target + suffix)
                except OSError:
                    try:
                        os.unlink(src)
                    except OSError:
                        pass
        # Keep at most two quarantined copies to bound storage use.
        directory = os.path.dirname(self.path) or "."
        base = os.path.basename(self.path) + ".corrupt-"
        old = sorted(f for f in os.listdir(directory) if f.startswith(base) and not f.endswith(("-wal", "-shm", "-journal")))
        for name in old[:-2]:
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.unlink(os.path.join(directory, name + suffix))
                except OSError:
                    pass
        return target

    def _configure(self, conn):
        mode = "memory"
        if self.path != ":memory:":
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.DatabaseError:
                mode = None
            if mode != "wal":
                mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        self.journal_mode = mode
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-1024")  # 1 MiB page cache bound
        conn.execute("PRAGMA wal_autocheckpoint=256")

    def _migrate(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError("database schema %d is newer than this BandWatch (%d); refusing to downgrade" % (current, SCHEMA_VERSION))
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in sql.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(version),))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            log.info("database migrated to schema %d", version)

    def _check_mode(self, conn):
        row = conn.execute("SELECT v FROM meta WHERE k='mode'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta VALUES ('mode', ?)", (self.mode,))
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('created', ?)", (str(int(time.time())),))
        elif row[0] != self.mode:
            raise ModeMismatch("database %s holds %s data; refusing to open it in %s mode" % (self.path, row[0], self.mode))

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # -- meta / state ---------------------------------------------------------

    def meta(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def get_state(self, key):
        with self.lock:
            row = self.conn.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set_states(self, items):
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                for key, val in items.items():
                    if val is None:
                        self.conn.execute("DELETE FROM state WHERE k=?", (key,))
                    else:
                        self.conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(val)))
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                self.conn.execute("ROLLBACK")
                raise

    # -- events -------------------------------------------------------------

    def _event(self, conn, level, source, kind, message):
        conn.execute(
            "INSERT INTO events (t, level, source, kind, message) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), level, source, kind, message[:500]),
        )

    def event(self, level, source, kind, message):
        self.add_events([(int(time.time()), level, source, kind, message)])

    def add_events(self, items):
        if not items:
            return
        with self.lock:
            try:
                self.conn.executemany(
                    "INSERT INTO events (t, level, source, kind, message) VALUES (?, ?, ?, ?, ?)",
                    [(int(t), lv, src, kind, msg[:500]) for t, lv, src, kind, msg in items],
                )
            except sqlite3.Error as exc:
                log.warning("could not record events: %s", exc)

    def events(self, limit=50, source=None):
        sql = "SELECT t, level, source, kind, message FROM events"
        args = []
        if source:
            sql += " WHERE source=?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(zip(("t", "level", "source", "kind", "message"), r)) for r in rows]

    # -- sample accumulation ----------------------------------------------------

    def add(self, series, key, wall_end, dt, rx, tx, rate_ok=True, est=False, count=True):
        """Add ``rx``/``tx`` bytes measured over ``dt`` seconds ending at ``wall_end``."""
        with self.plock:
            accumulate(self.pending, series, key, wall_end, dt, rx, tx, rate_ok, est, count)

    def merge_pending(self, buckets, shift=0):
        """Merge externally accumulated minute buckets (optionally time-shifted)."""
        with self.plock:
            for (s, k, t), bk in buckets.items():
                minute = int((t + shift) // 60) * 60
                cur = self.pending.get((s, k, minute))
                if cur is None:
                    self.pending[(s, k, minute)] = list(bk)
                else:
                    _merge_bucket(cur, bk)
                    cur[COV] = min(cur[COV], 60000)

    # -- flushing and roll-ups ----------------------------------------------------

    def begin_flush(self):
        """Move pending buckets to the in-flight set (cheap; call under any
        caller lock that must be consistent with the persisted data)."""
        with self.plock:
            if self.inflight:
                _merge_into(self.inflight, self.pending)
            else:
                self.inflight = self.pending
            self.pending = {}

    def complete_flush(self, now=None, retention=None):
        """Write the in-flight buckets, update roll-ups and apply retention.

        Queries keep seeing in-flight buckets until the transaction commits;
        the in-flight set is cleared while the connection lock is still held,
        so nothing is ever counted twice.
        """
        now = now or time.time()
        with self.lock:
            with self.plock:
                batch = self.inflight
            try:
                self._write(batch)
                if retention:
                    self._prune(now, retention)
            except sqlite3.Error as exc:
                # Keep the data in memory so it is retried on the next flush.
                with self.plock:
                    _merge_into(self.pending, self.inflight)
                    self.inflight = {}
                self.last_flush_error = str(exc)
                log.error("database flush failed: %s", exc)
                return False
            with self.plock:
                self.inflight = {}
            self.last_flush = now
            self.last_flush_error = None
            self.flush_count += 1
        return True

    def flush(self, now=None, retention=None):
        """Write all pending buckets (begin + complete)."""
        self.begin_flush()
        return self.complete_flush(now, retention)

    def _write(self, pending):
        if not pending:
            return
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """INSERT INTO m1 (s, k, t, rx, tx, cov, prx, ptx, nrx, ntx, n, est)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (s, k, t) DO UPDATE SET
                     rx = rx + excluded.rx, tx = tx + excluded.tx,
                     cov = min(60000, cov + excluded.cov),
                     prx = max(coalesce(prx, excluded.prx), coalesce(excluded.prx, prx)),
                     ptx = max(coalesce(ptx, excluded.ptx), coalesce(excluded.ptx, ptx)),
                     nrx = min(coalesce(nrx, excluded.nrx), coalesce(excluded.nrx, nrx)),
                     ntx = min(coalesce(ntx, excluded.ntx), coalesce(excluded.ntx, ntx)),
                     n = n + excluded.n, est = est + excluded.est""",
                [(s, k, t, b[RX], b[TX], min(b[COV], 60000), b[PRX], b[PTX], b[NRX], b[NTX], b[N], b[EST]) for (s, k, t), b in pending.items()],
            )
            tmin = min(t for (_s, _k, t) in pending)
            tmax = max(t for (_s, _k, t) in pending)
            qmin = tmin // 900 * 900
            qmax = tmax // 900 * 900 + 900
            self._rollup_quarters(qmin, qmax)
            self._rollup_days(qmin, qmax)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _rollup_quarters(self, qmin, qmax):
        self.conn.execute(
            """INSERT OR REPLACE INTO m15 (s, k, t, rx, tx, cov, prx, ptx, nrx, ntx, n, est)
               SELECT s, k, (t / 900) * 900, sum(rx), sum(tx), min(900000, sum(cov)),
                      max(prx), max(ptx), min(nrx), min(ntx), sum(n), sum(est)
               FROM m1 WHERE t >= ? AND t < ? GROUP BY s, k, t / 900""",
            (qmin, qmax),
        )

    def _rollup_days(self, tmin, tmax):
        day = tzmod.local_date(tmin, self.tz)
        last = tzmod.local_date(tmax - 1, self.tz)
        while day <= last:
            a = tzmod.local_midnight(day, self.tz)
            b = tzmod.local_midnight(day + _ONE_DAY, self.tz)
            self.conn.execute(
                """INSERT OR REPLACE INTO d1 (s, k, day, rx, tx, cov, prx, ptx, nrx, ntx, n, est)
                   SELECT s, k, ?, sum(rx), sum(tx), sum(cov), max(prx), max(ptx),
                          min(nrx), min(ntx), sum(n), sum(est)
                   FROM m15 WHERE t >= ? AND t < ? GROUP BY s, k""",
                (tzmod.day_key(day), a, b),
            )
            day += _ONE_DAY

    def rebuild_days(self, tzinfo):
        """Recompute daily roll-ups after a timezone change (where 15-minute data exists)."""
        with self.lock:
            self.tz = tzinfo
            row = self.conn.execute("SELECT min(t), max(t) FROM m15").fetchone()
            if not row or row[0] is None:
                return 0
            first_day = tzmod.day_key(tzmod.local_date(row[0], tzinfo))
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DELETE FROM d1 WHERE day >= ?", (first_day,))
                self._rollup_days(row[0], row[1] + 900)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            return 1

    def _day_floor(self, ts):
        return tzmod.local_midnight(tzmod.local_date(ts, self.tz), self.tz)

    def _prune(self, now, ret):
        conn = self.conn
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM m1 WHERE t < ?", (int(now - ret["minute_days"] * 86400),))
            conn.execute("DELETE FROM m15 WHERE s IN ('dev', 'devl') AND t < ?", (self._day_floor(now - ret["device_quarter_days"] * 86400),))
            conn.execute("DELETE FROM m15 WHERE t < ?", (self._day_floor(now - ret["quarter_days"] * 86400),))
            cutoff = tzmod.day_key(tzmod.local_date(now - ret["day_days"] * 86400, self.tz))
            conn.execute("DELETE FROM d1 WHERE day < ?", (cutoff,))
            conn.execute("DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - ?", (MAX_EVENTS,))
            conn.execute(
                "DELETE FROM tasks WHERE gid IN (SELECT gid FROM tasks ORDER BY last_seen DESC LIMIT -1 OFFSET ?)",
                (MAX_TASKS,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- queries ----------------------------------------------------------------

    def _pending_rows(self, series, key, a, b):
        """Unflushed minute rows as (k, t, bucket) within [a, b)."""
        out = []
        a = int(a) // 60 * 60
        with self.plock:
            for source in (self.inflight, self.pending):
                for (s, k, t), bucket in source.items():
                    if s == series and (key is None or k == key) and a <= t < b:
                        out.append((k, t, list(bucket)))
        return out

    def _db_rows(self, series, key, a, b):
        """Rows covering [a, b) using the finest tier that still holds the data.

        Returns a list of (k, t_start, t_len, bucket). Day rows report their
        local start timestamp.
        """
        rows = []
        cols = ", ".join(COLS)
        kfilter = "" if key is None else " AND k = ?"
        kargs = () if key is None else (key,)
        m15_start = self.conn.execute(
            "SELECT min(t) FROM m15 WHERE s = ?" + kfilter, (series,) + kargs
        ).fetchone()[0]
        split = a
        day = tzmod.local_date(a, self.tz)
        if tzmod.local_midnight(day, self.tz) == a and (m15_start is None or m15_start > a):
            # Whole local days that ended before the oldest retained 15-minute
            # row are served from the daily roll-ups. 15-minute data is pruned
            # on local-day boundaries, so a day is never split across tiers.
            while True:
                ds = tzmod.local_midnight(day, self.tz)
                de = tzmod.local_midnight(day + _ONE_DAY, self.tz)
                if de > b or (m15_start is not None and de > m15_start):
                    split = ds
                    break
                for r in self.conn.execute(
                    "SELECT k, %s FROM d1 WHERE s = ? AND day = ?%s" % (cols, kfilter),
                    (series, tzmod.day_key(day)) + kargs,
                ):
                    rows.append((r[0], ds, de - ds, list(r[1:])))
                day += _ONE_DAY
            if split >= b:
                return rows
        # Whole quarters come from m15; unaligned edges from the 1-minute tier
        # while it still holds them (otherwise the whole edge quarter is used,
        # which can only include data from just outside the range).
        qa = -(-int(split) // 900) * 900
        qb = int(b) // 900 * 900
        m1_start = self.conn.execute("SELECT min(t) FROM m1 WHERE s = ?" + kfilter, (series,) + kargs).fetchone()[0]
        if qa != split and (m1_start is None or m1_start > split):
            qa = int(split) // 900 * 900
        if qb != b and (m1_start is None or m1_start > qb):
            qb = -(-int(b) // 900) * 900
        if qa >= qb:
            qa = qb = None
        for lo, hi in ((split, qa), (qb, b)) if qa is not None else ((split, b),):
            if hi is None or lo >= hi:
                continue
            for r in self.conn.execute(
                "SELECT k, t, %s FROM m1 WHERE s = ? AND t >= ? AND t < ?%s" % (cols, kfilter),
                (series, int(lo) // 60 * 60 if lo % 60 else lo, hi) + kargs,
            ):
                rows.append((r[0], r[1], 60, list(r[2:])))
        if qa is not None:
            for r in self.conn.execute(
                "SELECT k, t, %s FROM m15 WHERE s = ? AND t >= ? AND t < ?%s" % (cols, kfilter),
                (series, qa, qb) + kargs,
            ):
                rows.append((r[0], r[1], 900, list(r[2:])))
        return rows

    def rows(self, series, key, a, b):
        """All buckets (DB + unflushed) as (k, t, len, bucket) for [a, b)."""
        with self.lock:
            rows = self._db_rows(series, key, a, b)
            rows.extend((k, t, 60, list(bk)) for k, t, bk in self._pending_rows(series, key, a, b))
        return rows

    def summary(self, series, key, a, b):
        tot = new_bucket()
        for _k, _t, _l, bk in self.rows(series, key, a, b):
            _merge_bucket(tot, bk)
        return tot

    def per_key(self, series, a, b):
        """Totals grouped by key: {k: bucket}."""
        out = {}
        for k, _t, _l, bk in self.rows(series, None, a, b):
            cur = out.get(k)
            if cur is None:
                out[k] = list(bk)
            else:
                _merge_bucket(cur, bk)
        return out

    def buckets(self, series, key, edges):
        """Aggregate into the half-open intervals defined by sorted ``edges``."""
        import bisect

        out = [new_bucket() for _ in range(len(edges) - 1)]
        if len(edges) < 2:
            return out
        for _k, t, _l, bk in self.rows(series, key, edges[0], edges[-1]):
            idx = bisect.bisect_right(edges, t) - 1
            if 0 <= idx < len(out):
                _merge_bucket(out[idx], bk)
        return out

    def keys_seen(self, series, a, b):
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT k FROM m15 WHERE s = ? AND t >= ? AND t < ?", (series, a, b)).fetchall()
        keys = {r[0] for r in rows}
        with self.lock:
            with self.plock:
                keys.update(k for (s, k, t) in list(self.pending) + list(self.inflight) if s == series and a <= t < b)
        return keys

    def first_data_time(self):
        with self.lock:
            row = self.conn.execute("SELECT min(t) FROM m15").fetchone()
            d = self.conn.execute("SELECT min(day) FROM d1").fetchone()
        cands = []
        if row and row[0] is not None:
            cands.append(row[0])
        if d and d[0] is not None:
            cands.append(tzmod.local_midnight(tzmod.key_to_date(d[0]), self.tz))
        with self.lock:
            with self.plock:
                ts = [t for (_s, _k, t) in list(self.pending) + list(self.inflight)]
            if ts:
                cands.append(min(ts))
        return min(cands) if cands else None

    # -- devices ----------------------------------------------------------------

    def upsert_devices(self, devices):
        """Persist identity fields of known devices (batched with flushes)."""
        if not devices:
            return
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    """INSERT INTO devices (mac, hostname, ip4, ip6, conn, iface, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (mac) DO UPDATE SET
                         hostname = coalesce(excluded.hostname, hostname),
                         ip4 = coalesce(excluded.ip4, ip4), ip6 = coalesce(excluded.ip6, ip6),
                         conn = coalesce(excluded.conn, conn), iface = coalesce(excluded.iface, iface),
                         last_seen = max(coalesce(last_seen, 0), coalesce(excluded.last_seen, 0))""",
                    [
                        (d["mac"], d.get("hostname"), d.get("ip4"), d.get("ip6"), d.get("conn"), d.get("iface"), d.get("first_seen"), d.get("last_seen"))
                        for d in devices
                    ],
                )
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                self.conn.execute("ROLLBACK")
                raise

    def devices(self):
        with self.lock:
            rows = self.conn.execute("SELECT mac, alias, hostname, ip4, ip6, conn, iface, first_seen, last_seen FROM devices").fetchall()
        keys = ("mac", "alias", "hostname", "ip4", "ip6", "conn", "iface", "first_seen", "last_seen")
        return {r[0]: dict(zip(keys, r)) for r in rows}

    def set_alias(self, mac, alias):
        with self.lock:
            self.conn.execute(
                "INSERT INTO devices (mac, alias, first_seen) VALUES (?, ?, ?) ON CONFLICT (mac) DO UPDATE SET alias = excluded.alias",
                (mac, alias or None, int(time.time())),
            )

    # -- aria2 tasks --------------------------------------------------------------

    def upsert_tasks(self, tasks):
        if not tasks:
            return
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    """INSERT INTO tasks (gid, name, status, total, done, up, first_seen, last_seen, finished, counted_rx, counted_tx)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (gid) DO UPDATE SET name = excluded.name, status = excluded.status,
                         total = excluded.total, done = excluded.done, up = excluded.up,
                         last_seen = excluded.last_seen, finished = coalesce(finished, excluded.finished),
                         counted_rx = excluded.counted_rx, counted_tx = excluded.counted_tx""",
                    [
                        (t["gid"], t.get("name"), t.get("status"), t.get("total"), t.get("done"), t.get("up"), t.get("first_seen"), t.get("last_seen"), t.get("finished"), t.get("counted_rx", 0), t.get("counted_tx", 0))
                        for t in tasks
                    ],
                )
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                self.conn.execute("ROLLBACK")
                raise

    def recent_tasks(self, limit=30):
        keys = ("gid", "name", "status", "total", "done", "up", "first_seen", "last_seen", "finished", "counted_rx", "counted_tx")
        with self.lock:
            rows = self.conn.execute("SELECT %s FROM tasks ORDER BY last_seen DESC LIMIT ?" % ", ".join(keys), (int(limit),)).fetchall()
        return [dict(zip(keys, r)) for r in rows]

    # -- status -----------------------------------------------------------------

    def status(self):
        info = {
            "path": self.path,
            "journal_mode": self.journal_mode,
            "last_flush": self.last_flush,
            "last_flush_error": self.last_flush_error,
            "pending_buckets": len(self.pending) + len(self.inflight),
            "recovered_from": os.path.basename(self.recovered_from) if self.recovered_from else None,
            "schema_version": int(self.meta("schema_version", "0")),
            "mode": self.mode,
        }
        try:
            info["size_bytes"] = sum(os.path.getsize(self.path + s) for s in ("", "-wal") if os.path.exists(self.path + s))
        except OSError:
            info["size_bytes"] = None
        with self.lock:
            for table in ("m1", "m15", "d1", "devices", "events", "tasks"):
                info["rows_" + table] = self.conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
        return info


import datetime as _datetime  # noqa: E402

_ONE_DAY = _datetime.timedelta(days=1)
