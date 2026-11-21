"""Usage statistics built from stored buckets.

Definitions (see docs/ARCHITECTURE.md "Analytics"):

* **Total** - sum of measured bytes in the range.
* **Measured duration** - milliseconds for which the collector was producing
  valid measurements. **Coverage** = measured / expected, where expected is
  the part of the range that lies between the start of data collection and
  *now*. Missing time is never treated as zero traffic.
* **Average speed** - total bytes / measured duration.
* **Peak / minimum speed** - highest / lowest rate of any single sample
  interval (about ``sample_interval`` seconds). For devices, the minimum is
  taken over intervals with traffic ("minimum while active"), because idle
  intervals are not stored per device.
"""

import calendar
import datetime as dt

from . import store as st
from . import tz as tzmod

ONE_DAY = dt.timedelta(days=1)


def _bucket_stats(bk, expected_s):
    cov_s = bk[st.COV] / 1000.0
    has = cov_s > 0
    return {
        "rx": bk[st.RX] if has else None,
        "tx": bk[st.TX] if has else None,
        "total": bk[st.RX] + bk[st.TX] if has else None,
        "measured_s": round(cov_s, 1),
        "expected_s": round(expected_s, 1),
        "coverage": round(min(1.0, cov_s / expected_s), 4) if expected_s > 0 else None,
        "avg_rx": bk[st.RX] / cov_s if has else None,
        "avg_tx": bk[st.TX] / cov_s if has else None,
        "peak_rx": bk[st.PRX],
        "peak_tx": bk[st.PTX],
        "min_rx": bk[st.NRX],
        "min_tx": bk[st.NTX],
        "estimated": bk[st.EST],
        "samples": bk[st.N],
    }


def expected_seconds(a, b, now, first_ts):
    start = max(a, first_ts) if first_ts is not None else None
    if start is None:
        return 0.0
    return max(0.0, min(b, now) - start)


def summary(store, series, key, a, b, now, first_ts, coverage_key=None):
    """Summary for ``series``/``key`` over [a, b).

    ``coverage_key`` names a dense aggregate key whose measured duration is
    used for coverage and averages (sparse per-device rows).
    """
    bk = store.summary(series, key, a, b)
    exp = expected_seconds(a, b, now, first_ts)
    if coverage_key is not None and coverage_key != key:
        cov = store.summary(series, coverage_key, a, b)
        res = _bucket_stats(cov, exp)
        has = cov[st.COV] > 0
        res.update(
            rx=bk[st.RX] if has else None,
            tx=bk[st.TX] if has else None,
            total=bk[st.RX] + bk[st.TX] if has else None,
            avg_rx=bk[st.RX] / (cov[st.COV] / 1000.0) if has else None,
            avg_tx=bk[st.TX] / (cov[st.COV] / 1000.0) if has else None,
            peak_rx=bk[st.PRX],
            peak_tx=bk[st.PTX],
            min_rx=bk[st.NRX],
            min_tx=bk[st.NTX],
            estimated=bk[st.EST],
            samples=bk[st.N],
            active_s=round(bk[st.COV] / 1000.0, 1),
        )
        return res
    return _bucket_stats(bk, exp)


def _month_start(d):
    return d.replace(day=1)


def _next_month(d):
    return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def edges_for(a, b, tz):
    """Chart bucket edges and a resolution name for the range [a, b)."""
    a, b = int(a), int(b)
    span = b - a
    if span <= 3 * 86400:
        edges = list(range(a, b + 1, 3600))
        if edges[-1] != b:
            edges.append(b)
        return edges, "hour"
    da, db = tzmod.local_date(a, tz), tzmod.local_date(b - 1, tz) + ONE_DAY
    if span <= 92 * 86400:
        edges, d = [], da
        while d <= db:
            edges.append(tzmod.local_midnight(d, tz))
            d += ONE_DAY
        return edges, "day"
    edges, d = [], _month_start(da)
    while True:
        edges.append(max(a, tzmod.local_midnight(d, tz)))
        if d >= db:
            break
        d = _next_month(d)
    if edges[-1] < b:
        edges.append(b)
    return edges, "month"


def chart(store, series, key, a, b, now, first_ts, tz, coverage_key=None):
    edges, res = edges_for(a, b, tz)
    buckets = store.buckets(series, key, edges)
    cov_buckets = store.buckets(series, coverage_key, edges) if coverage_key and coverage_key != key else None
    points = []
    for i, bk in enumerate(buckets):
        s, e = edges[i], edges[i + 1]
        exp = expected_seconds(s, e, now, first_ts)
        cb = cov_buckets[i] if cov_buckets else bk
        cov_s = cb[st.COV] / 1000.0
        has = cov_s > 0
        points.append(
            {
                "t": s,
                "end": e,
                "rx": bk[st.RX] if has else None,
                "tx": bk[st.TX] if has else None,
                "peak_rx": bk[st.PRX],
                "peak_tx": bk[st.PTX],
                "coverage": round(min(1.0, cov_s / exp), 3) if exp > 0 else None,
                "future": s >= now,
            }
        )
    return {"resolution": res, "points": points}


def resolve_period(params, now, tz, week_start=0, month_start_day=1):
    """Parse period query parameters into (name, a, b, start_date, end_date)."""
    period = params.get("period", "today")
    if period not in tzmod.PERIODS:
        raise ValueError("unknown period")
    start = end = None
    if period == "custom":
        try:
            start = dt.date.fromisoformat(params.get("start", ""))
            end = dt.date.fromisoformat(params.get("end", ""))
        except ValueError:
            raise ValueError("custom period needs start and end dates (YYYY-MM-DD)") from None
        if end < start:
            raise ValueError("end date is before start date")
        if (end - start).days > 3660:
            raise ValueError("range too long (max 10 years)")
    month_day = month_start_day if period in ("this_month", "last_month") else 1
    a, b, da, db = tzmod.period_bounds(period, now, tz, start, end, week_start, month_day)
    return period, a, b, da, db - ONE_DAY


def month_days(d):
    return calendar.monthrange(d.year, d.month)[1]
