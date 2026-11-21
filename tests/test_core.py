"""Counters, timezones and configuration."""

import datetime as dt

import pytest

from bandwatch import config as cfgmod
from bandwatch import counters
from bandwatch import tz as tzmod

# -- counters ---------------------------------------------------------------


def test_counter_first_reading_is_baseline_only():
    c = counters.CounterTracker()
    d = c.update(10_000, 1.0)
    assert d.event == counters.FIRST and not d.valid and d.bytes == 0


def test_counter_normal_delta_and_rate():
    c = counters.CounterTracker()
    c.update(1000, 10.0)
    d = c.update(5000, 12.0)
    assert d.valid and d.bytes == 4000 and d.dt == 2.0 and d.rate == 2000


def test_counter_idle_zero_is_valid_measurement():
    c = counters.CounterTracker()
    c.update(1000, 0.0)
    d = c.update(1000, 2.0)
    assert d.valid and d.bytes == 0 and d.rate == 0


def test_counter_reset_counts_plausible_new_value():
    c = counters.CounterTracker(max_rate=125_000_000)
    c.update(5_000_000_000, 0.0)
    d = c.update(30_000, 2.0)
    assert d.event == counters.RESET and d.valid and d.bytes == 30_000


def test_counter_identity_change_with_implausible_value_rebaselines():
    c = counters.CounterTracker(max_rate=125_000_000)
    c.update(100, 0.0, identity=(5, "boot"))
    d = c.update(9_000_000_000, 2.0, identity=(6, "boot"))
    assert d.event == counters.REBASELINE and not d.valid and d.bytes == 0
    d2 = c.update(9_000_001_000, 4.0, identity=(6, "boot"))
    assert d2.valid and d2.bytes == 1000


def test_counter_glitch_jump_is_rejected_not_counted():
    c = counters.CounterTracker(max_rate=125_000_000)  # 1 Gbit/s
    c.update(0, 0.0)
    d = c.update(10_000_000_000, 1.0)
    assert d.event == counters.GLITCH and not d.valid


def test_counter_32bit_wrap_opt_in():
    c = counters.CounterTracker(max_rate=125_000_000, allow_wrap=True)
    c.update(counters.U32 - 1000, 0.0)
    d = c.update(500, 1.0)
    assert d.event == counters.WRAP and d.bytes == 1500
    # without opt-in a decrease is a reset
    c2 = counters.CounterTracker(max_rate=125_000_000)
    c2.update(counters.U32 - 1000, 0.0)
    assert c2.update(500, 1.0).event == counters.RESET


def test_counter_out_of_order_ignored():
    c = counters.CounterTracker()
    c.update(100, 5.0)
    d = c.update(200, 5.0)
    assert not d.valid and c.value == 100


def test_counter_long_gap_allows_large_delta():
    c = counters.CounterTracker(max_rate=12_500_000)
    c.update(0, 0.0)
    d = c.update(10_000_000_000, 3600.0)
    assert d.valid and d.bytes == 10_000_000_000


# -- timezones --------------------------------------------------------------


def test_posix_fixed_offset_colombo():
    tz = tzmod.PosixTimezone("<+0530>-5:30")
    assert tz.std_offset == 19800
    d = dt.datetime(2026, 11, 21, 0, 0, tzinfo=tz)
    assert d.timestamp() == dt.datetime(2026, 11, 20, 18, 30, tzinfo=dt.timezone.utc).timestamp()


def test_posix_dst_rules_match_known_transitions():
    tz = tzmod.PosixTimezone("CET-1CEST,M3.5.0,M10.5.0/3")
    # 2026: DST starts 29 March 01:00 UTC, ends 25 October 01:00 UTC
    before = dt.datetime(2026, 3, 29, 0, 59, tzinfo=dt.timezone.utc).astimezone(tz)
    after = dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.timezone.utc).astimezone(tz)
    assert before.utcoffset().total_seconds() == 3600
    assert after.utcoffset().total_seconds() == 7200
    autumn = dt.datetime(2026, 10, 25, 1, 0, tzinfo=dt.timezone.utc).astimezone(tz)
    assert autumn.utcoffset().total_seconds() == 3600
    # local midnights around the change are exactly one calendar day apart
    a = tzmod.local_midnight(dt.date(2026, 3, 29), tz)
    b = tzmod.local_midnight(dt.date(2026, 3, 30), tz)
    assert b - a == 23 * 3600


def test_posix_southern_hemisphere():
    tz = tzmod.PosixTimezone("AEST-10AEDT,M10.1.0,M4.1.0/3")
    jan = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).astimezone(tz)
    jul = dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).astimezone(tz)
    assert jan.utcoffset().total_seconds() == 11 * 3600
    assert jul.utcoffset().total_seconds() == 10 * 3600


def test_posix_us_rules():
    tz = tzmod.PosixTimezone("EST5EDT,M3.2.0,M11.1.0")
    assert dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc).astimezone(tz).utcoffset().total_seconds() == -4 * 3600
    assert dt.datetime(2026, 12, 1, 12, tzinfo=dt.timezone.utc).astimezone(tz).utcoffset().total_seconds() == -5 * 3600


@pytest.mark.parametrize("bad", ["", "X", "ABC", ":Europe/Paris", "EST5EDT,M13.1.0,M11.1.0"])
def test_posix_rejects_bad(bad):
    assert tzmod.from_string(bad) is None


def test_resolve_system_from_openwrt_config(tmp_path):
    p = tmp_path / "system"
    p.write_text("config system\n\toption hostname 'x'\n\toption timezone '<+0530>-5:30'\n\toption zonename 'Invalid/Zone_Name'\n")
    tz, desc = tzmod.resolve("system", str(p))
    assert desc == "<+0530>-5:30"
    assert tzmod.utc_offset_at(1795000000, tz) == 19800


def test_period_bounds_colombo_midnight_and_week():
    tz = tzmod.PosixTimezone("<+0530>-5:30")
    # 2026-11-21 (Saturday) 03:00 local
    now = dt.datetime(2026, 11, 21, 3, 0, tzinfo=tz).timestamp()
    a, b, da, db = tzmod.period_bounds("today", now, tz)
    assert da == dt.date(2026, 11, 21) and b - a == 86400
    assert dt.datetime.fromtimestamp(a, tz).hour == 0
    a, b, da, db = tzmod.period_bounds("this_week", now, tz, week_start=0)
    assert da == dt.date(2026, 11, 16) and db == dt.date(2026, 11, 23)
    a, b, da, db = tzmod.period_bounds("this_week", now, tz, week_start=6)
    assert da == dt.date(2026, 11, 15)
    a, b, da, db = tzmod.period_bounds("last_month", now, tz)
    assert da == dt.date(2026, 10, 1) and db == dt.date(2026, 11, 1)
    a, b, da, db = tzmod.period_bounds("this_year", now, tz)
    assert da == dt.date(2026, 1, 1) and db == dt.date(2027, 1, 1)


def test_period_bounds_year_and_month_boundaries():
    tz = tzmod.UTC
    now = dt.datetime(2027, 1, 1, 0, 0, 1, tzinfo=tz).timestamp()
    _, _, da, _ = tzmod.period_bounds("yesterday", now, tz)
    assert da == dt.date(2026, 12, 31)
    _, _, da, db = tzmod.period_bounds("last_month", now, tz)
    assert (da, db) == (dt.date(2026, 12, 1), dt.date(2027, 1, 1))
    _, _, da, db = tzmod.period_bounds("last_week", now, tz)
    assert db - da == dt.timedelta(days=7)


def test_billing_month_with_reset_day():
    tz = tzmod.UTC
    now = dt.datetime(2026, 11, 10, tzinfo=tz).timestamp()
    _, _, da, db = tzmod.period_bounds("this_month", now, tz, month_start_day=15)
    assert (da, db) == (dt.date(2026, 10, 15), dt.date(2026, 11, 15))
    now = dt.datetime(2026, 12, 20, tzinfo=tz).timestamp()
    _, _, da, db = tzmod.period_bounds("this_month", now, tz, month_start_day=15)
    assert (da, db) == (dt.date(2026, 12, 15), dt.date(2027, 1, 15))


def test_custom_period_validation():
    with pytest.raises(ValueError):
        tzmod.period_bounds("custom", 0, tzmod.UTC, dt.date(2026, 2, 1), dt.date(2026, 1, 1))
    a, b, _, _ = tzmod.period_bounds("custom", 0, tzmod.UTC, dt.date(2026, 1, 1), dt.date(2026, 1, 1))
    assert b - a == 86400


# -- configuration ------------------------------------------------------------


def test_uci_parser_handles_quotes_lists_comments():
    text = """
# comment
config bandwatch 'main'
	option enabled '1'
	option port "8788"
	list listen 'lan'
	list listen '127.0.0.1'
	option data_dir /etc/bandwatch # trailing comment
config host
	option name 'nas'
"""
    secs = cfgmod.parse_uci(text)
    main = cfgmod.uci_section(secs, "bandwatch", "main")
    assert main["port"] == "8788" and main["listen"] == ["lan", "127.0.0.1"] and main["data_dir"] == "/etc/bandwatch"
    assert cfgmod.uci_section(secs, "host")[".name"] == "@host[0]"


def test_service_config_validation(tmp_path):
    p = tmp_path / "bw"
    p.write_text("config bandwatch 'main'\n\toption port '99999'\n")
    with pytest.raises(ValueError):
        cfgmod.ServiceConfig.from_uci(str(p))
    p.write_text("config bandwatch 'main'\n\toption auth 'none'\n\tlist listen 'lan'\n")
    with pytest.raises(ValueError, match="loopback"):
        cfgmod.ServiceConfig.from_uci(str(p))
    p.write_text("config bandwatch 'main'\n\toption data_dir 'relative/path'\n")
    with pytest.raises(ValueError):
        cfgmod.ServiceConfig.from_uci(str(p))
    p.write_text("config bandwatch 'main'\n\toption port '8790'\n\tlist allowed_networks '192.168.1.0/24'\n\tlist allowed_hosts 'router.home.example'\n\tlist allowed_hosts 'bad host!'\n")
    c = cfgmod.ServiceConfig.from_uci(str(p))
    assert c.port == 8790 and c.allowed_networks == ["192.168.1.0/24"]
    assert c.allowed_hosts == ["router.home.example"]


def test_settings_validation_rejects_bad_values(tmp_path):
    s = cfgmod.Settings(str(tmp_path / "settings.json"))
    errs = s.update({"wan.sample_interval": 0})
    assert "wan.sample_interval" in errs
    errs = s.update({"wan.interfaces": ["eth0; rm -rf /"]})
    assert "wan.interfaces" in errs
    errs = s.update({"aria2.url": "http://evil.example.com/jsonrpc"})
    assert "aria2.url" in errs
    errs = s.update({"aria2.url": "http://8.8.8.8:6800/jsonrpc"})
    assert "aria2.url" in errs
    errs = s.update({"general.timezone": "Mars/Olympus"})
    assert "general.timezone" in errs
    errs = s.update({"unknown.key": 1})
    assert "unknown.key" in errs
    assert s.update({"ui.sample": 1}) and s["wan.sample_interval"] == 2
    assert s.update({"wan.sample_interval": True})  # bool is not an int here
    assert s.update({"wan.sample_interval": 1, "aria2.url": "http://127.0.0.1:6801/jsonrpc", "ui.volume_base": 1024}) == {}
    s2 = cfgmod.Settings(str(tmp_path / "settings.json"))
    assert s2["wan.sample_interval"] == 1 and s2["ui.volume_base"] == 1024


def test_settings_file_corruption_falls_back_to_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    s = cfgmod.Settings(str(p))
    assert s["wan.sample_interval"] == 2 and s.load_error


def test_secret_store_permissions(tmp_path):
    import os
    import stat

    st = cfgmod.SecretStore(str(tmp_path / "secrets.json"))
    st.set("aria2_secret", "s3cret")
    mode = stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode)
    assert mode == 0o600
    assert st.get("aria2_secret") == "s3cret" and st.has("aria2_secret")
    st.set("aria2_secret", "")
    assert not st.has("aria2_secret")
