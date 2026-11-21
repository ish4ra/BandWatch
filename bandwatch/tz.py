"""Timezone handling that works on OpenWrt without the tz database.

OpenWrt stores the zone in ``/etc/config/system`` as both an IANA name
(``zonename 'Asia/Colombo'``) and a POSIX TZ rule (``timezone '<+0530>-5:30'``).
The IANA database (``zoneinfo-*`` packages) is frequently not installed, so
``zoneinfo.ZoneInfo`` may fail. :class:`PosixTimezone` implements the POSIX
TZ rule format (including DST rules) as a fallback.

Calendar helpers convert between UTC timestamps and local calendar
boundaries (midnight, week, month, year), which drive all period statistics.
"""

import datetime as _dt
import os
import re

UTC = _dt.timezone.utc
_EPOCH = _dt.datetime(1970, 1, 1)

_NAME_RE = re.compile(r"<[^>]+>|[A-Za-z]{3,}")
_OFF_RE = re.compile(r"([+-]?)(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}))?")


def _parse_offset(text, pos):
    m = _OFF_RE.match(text, pos)
    if not m:
        raise ValueError("bad offset in TZ %r" % text)
    sign = -1 if m.group(1) == "-" else 1
    secs = int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60 + int(m.group(4) or 0)
    return sign * secs, m.end()


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (_dt.date(year, month + 1, 1) - _dt.date(year, month, 1)).days


def _is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


class _Rule:
    """One POSIX transition rule: Jn, n or Mm.w.d with optional /time."""

    def __init__(self, text):
        self.text = text
        rule, _, tpart = text.partition("/")
        self.time = 7200
        if tpart:
            self.time, end = _parse_offset(tpart, 0)
            if end != len(tpart):
                raise ValueError("bad rule time %r" % text)
        if rule.startswith("M"):
            parts = rule[1:].split(".")
            if len(parts) != 3:
                raise ValueError("bad M rule %r" % text)
            self.kind = "M"
            self.m, self.w, self.d = (int(p) for p in parts)
            if not (1 <= self.m <= 12 and 1 <= self.w <= 5 and 0 <= self.d <= 6):
                raise ValueError("bad M rule %r" % text)
        elif rule.startswith("J"):
            self.kind = "J"
            self.n = int(rule[1:])
            if not 1 <= self.n <= 365:
                raise ValueError("bad J rule %r" % text)
        else:
            self.kind = "n"
            self.n = int(rule)
            if not 0 <= self.n <= 365:
                raise ValueError("bad n rule %r" % text)

    def local_seconds(self, year):
        """Seconds from local midnight Jan 1 of ``year`` to the transition."""
        if self.kind == "M":
            first = _dt.date(year, self.m, 1)
            # weekday(): Monday=0 .. Sunday=6; POSIX d: Sunday=0
            first_dow = (first.weekday() + 1) % 7
            day = 1 + (self.d - first_dow) % 7 + (self.w - 1) * 7
            if day > _days_in_month(year, self.m):
                day -= 7
            yday = (_dt.date(year, self.m, day) - _dt.date(year, 1, 1)).days
        elif self.kind == "J":
            yday = self.n - 1
            if _is_leap(year) and self.n >= 60:
                yday += 1
        else:
            yday = self.n
        return yday * 86400 + self.time


class PosixTimezone(_dt.tzinfo):
    """tzinfo implementing a POSIX TZ string, e.g. ``CET-1CEST,M3.5.0,M10.5.0/3``."""

    def __init__(self, spec):
        spec = spec.strip()
        if spec.startswith(":"):
            raise ValueError("file-based TZ not supported: %r" % spec)
        self.spec = spec
        m = _NAME_RE.match(spec)
        if not m:
            raise ValueError("bad TZ name in %r" % spec)
        self.std_name = m.group(0).strip("<>")
        off, pos = _parse_offset(spec, m.end())
        self.std_offset = -off  # POSIX offsets are west-positive
        self.dst_name = None
        self.dst_offset = None
        self.start = self.end = None
        if pos < len(spec):
            m = _NAME_RE.match(spec, pos)
            if not m:
                raise ValueError("bad DST name in %r" % spec)
            self.dst_name = m.group(0).strip("<>")
            pos = m.end()
            if pos < len(spec) and spec[pos] != ",":
                off, pos = _parse_offset(spec, pos)
                self.dst_offset = -off
            else:
                self.dst_offset = self.std_offset + 3600
            rules = spec[pos:].lstrip(",") or "M3.2.0,M11.1.0"
            parts = rules.split(",")
            if len(parts) != 2:
                raise ValueError("bad DST rules in %r" % spec)
            self.start, self.end = _Rule(parts[0]), _Rule(parts[1])
        self._cache = {}

    def __repr__(self):
        return "PosixTimezone(%r)" % self.spec

    def _transitions(self, year):
        """(dst_start_utc, dst_end_utc) as POSIX timestamps for ``year``."""
        hit = self._cache.get(year)
        if hit is None:
            jan1 = (_dt.datetime(year, 1, 1) - _EPOCH).total_seconds()
            start = jan1 + self.start.local_seconds(year) - self.std_offset
            end = jan1 + self.end.local_seconds(year) - self.dst_offset
            hit = (start, end)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[year] = hit
        return hit

    def _is_dst_ts(self, ts):
        if self.start is None:
            return False
        year = (_EPOCH + _dt.timedelta(seconds=ts + self.std_offset)).year
        start, end = self._transitions(year)
        if start < end:  # northern hemisphere
            return start <= ts < end
        return not (end <= ts < start)  # southern hemisphere

    def _offset_for_local(self, dt):
        local = (dt.replace(tzinfo=None) - _EPOCH).total_seconds()
        if self.start is None:
            return self.std_offset
        # PEP 495: a repeated local time (DST end) uses ``fold`` to pick the
        # first (DST, fold=0) or second (standard, fold=1) occurrence.
        # Non-existent (skipped) local times resolve to standard time.
        in_dst = self._is_dst_ts(local - self.dst_offset)
        if in_dst and not self._is_dst_ts(local - self.std_offset) and getattr(dt, "fold", 0):
            return self.std_offset
        return self.dst_offset if in_dst else self.std_offset

    def utcoffset(self, dt):
        if dt is None:
            return _dt.timedelta(seconds=self.std_offset)
        return _dt.timedelta(seconds=self._offset_for_local(dt))

    def dst(self, dt):
        if dt is None or self.start is None:
            return _dt.timedelta(0)
        return _dt.timedelta(seconds=self._offset_for_local(dt) - self.std_offset)

    def tzname(self, dt):
        if dt is not None and self.start is not None and self.dst(dt):
            return self.dst_name
        return self.std_name

    def fromutc(self, dt):
        ts = (dt.replace(tzinfo=None) - _EPOCH).total_seconds()
        is_dst = self._is_dst_ts(ts)
        off = self.dst_offset if is_dst else self.std_offset
        local = (dt + _dt.timedelta(seconds=off)).replace(tzinfo=self)
        if not is_dst and self.start is not None:
            lt = ts + off
            if self._is_dst_ts(lt - self.dst_offset):
                local = local.replace(fold=1)  # second occurrence of a repeated hour
        return local


def _uci_system_tz(path="/etc/config/system"):
    """Return (zonename, posix) from OpenWrt's system config if readable."""
    zonename = posix = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(None, 2)
                if len(parts) == 3 and parts[0] == "option":
                    val = parts[2].strip().strip("'\"")
                    if parts[1] == "zonename":
                        zonename = val
                    elif parts[1] == "timezone":
                        posix = val
    except OSError:
        pass
    return zonename, posix


def resolve(setting="system", system_config="/etc/config/system"):
    """Resolve a timezone setting to a tzinfo.

    ``setting`` may be ``system`` (OpenWrt config, then /etc/TZ, then $TZ),
    an IANA zone name, or a POSIX TZ string. Returns ``(tzinfo, description)``.
    """
    candidates = []
    if not setting or setting == "system":
        zonename, posix = _uci_system_tz(system_config)
        if zonename:
            candidates.append(zonename)
        if posix:
            candidates.append(posix)
        try:
            with open("/etc/TZ", encoding="utf-8") as fh:
                candidates.append(fh.read().strip())
        except OSError:
            pass
        if os.environ.get("TZ"):
            candidates.append(os.environ["TZ"])
    else:
        candidates.append(setting)
    for cand in candidates:
        tz = from_string(cand)
        if tz is not None:
            return tz, cand
    return UTC, "UTC"


def from_string(text):
    if not text:
        return None
    text = text.strip()
    if text.upper() == "UTC":
        return UTC
    if "/" in text and "," not in text and re.match(r"^[A-Za-z0-9_+\-/]+$", text):
        try:
            import zoneinfo

            return zoneinfo.ZoneInfo(text)
        except Exception:
            return None
    try:
        return PosixTimezone(text)
    except (ValueError, OverflowError):
        return None


def is_valid(text):
    return text == "system" or from_string(text) is not None


# ---------------------------------------------------------------------------
# Calendar helpers. All return POSIX timestamps (int seconds).


def local_midnight(date, tz):
    """UTC timestamp of local midnight at the start of ``date``."""
    return int(_dt.datetime(date.year, date.month, date.day, tzinfo=tz).timestamp())


def local_date(ts, tz):
    return _dt.datetime.fromtimestamp(ts, tz).date()


def day_key(date):
    return date.year * 10000 + date.month * 100 + date.day


def key_to_date(key):
    return _dt.date(key // 10000, key // 100 % 100, key % 100)


def _add_months(date, months):
    y, m = divmod(date.month - 1 + months, 12)
    return _dt.date(date.year + y, m + 1, 1)


PERIODS = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "last_7_days",
    "last_30_days",
    "custom",
)


def period_bounds(period, now_ts, tz, start=None, end=None, week_start=0, month_start_day=1):
    """Return (start_ts, end_ts, start_date, end_date_exclusive) for a period.

    ``week_start``: 0=Monday .. 6=Sunday. ``start``/``end`` are dates for
    ``custom`` (end inclusive). End timestamps may lie in the future for
    current periods; callers clamp to ``now`` when measuring coverage.
    """
    today = local_date(now_ts, tz)
    one = _dt.timedelta(days=1)
    if period == "today":
        a, b = today, today + one
    elif period == "yesterday":
        a, b = today - one, today
    elif period in ("this_week", "last_week"):
        a = today - _dt.timedelta(days=(today.weekday() - week_start) % 7)
        if period == "last_week":
            a -= _dt.timedelta(days=7)
        b = a + _dt.timedelta(days=7)
    elif period in ("this_month", "last_month"):
        a = billing_month_start(today, month_start_day)
        if period == "last_month":
            a = billing_month_start(a - one, month_start_day)
        b = billing_month_start(_add_months(a, 1).replace(day=min(month_start_day, 28)), month_start_day)
        if b <= a:
            b = _add_months(a, 1)
    elif period == "this_year":
        a, b = _dt.date(today.year, 1, 1), _dt.date(today.year + 1, 1, 1)
    elif period == "last_7_days":
        a, b = today - _dt.timedelta(days=6), today + one
    elif period == "last_30_days":
        a, b = today - _dt.timedelta(days=29), today + one
    elif period == "custom":
        if start is None or end is None or end < start:
            raise ValueError("custom period requires start <= end")
        a, b = start, end + one
    else:
        raise ValueError("unknown period %r" % period)
    return local_midnight(a, tz), local_midnight(b, tz), a, b


def billing_month_start(date, start_day=1):
    """First day of the (billing) month containing ``date``."""
    start_day = max(1, min(28, int(start_day)))
    if date.day >= start_day:
        return date.replace(day=start_day)
    prev = _add_months(date.replace(day=1), -1)
    return prev.replace(day=start_day)


def utc_offset_at(ts, tz):
    return int(_dt.datetime.fromtimestamp(ts, tz).utcoffset().total_seconds())
