"""Runs inside the OpenWrt test container (tests/openwrt/upgrade-beta1.sh).

    python3 upgrade_helper.py seed                 # with the installed (beta.1) code
    python3 upgrade_helper.py verify VERSION [KIND] # after an upgrade / rollback

``seed`` writes history, an alias, settings, the aria2 secret and a password
with the code that is currently installed. ``verify`` checks them using only
the standard library (sqlite3, json, http.client), so it works against any
installed version.
"""

import http.client
import io
import json
import re
import sqlite3
import sys
import time
import zipfile

DATA = "/etc/bandwatch"
SEED = "/tmp/bw-seed.json"
PASSWORD = "upgrade-pass-1"
MAC = "aa:bb:cc:00:11:22"


def seed():
    sys.path.insert(0, "/usr/lib/bandwatch")
    from bandwatch import __version__, auth, config, store

    t0 = (int(time.time()) - 5 * 3600) // 60 * 60
    st = store.Store(DATA + "/bandwatch.db", mode="production")
    for i in range(240):
        st.add("wan", "*", t0 + (i + 1) * 60, 60, 1_000_000, 100_000)
        st.add("dev", MAC, t0 + (i + 1) * 60, 60, 400_000, 40_000)
    st.flush(retention={"minute_days": 7, "quarter_days": 400, "device_quarter_days": 60, "day_days": 3650})
    st.set_alias(MAC, "Living room TV")
    st.close()
    errs = config.Settings(DATA + "/settings.json").update({"storage.day_retention_days": 1000, "ui.theme": "light", "devices.bandix_direction": "tx_is_download"})
    assert not errs, errs
    config.SecretStore(DATA + "/secrets.json").set("aria2_secret", "seeded-secret")
    auth.Auth(DATA, "/var/run/bandwatch").set_password(PASSWORD)
    json.dump({"t0": t0, "t1": t0 + 240 * 60, "wan_rx": 240 * 1_000_000, "version": __version__}, open(SEED, "w"))
    print("seeded with", __version__)


def _req(method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", 8787, timeout=20)
    h = {"Host": "192.168.1.1"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r, r.read()


def verify(version, kind=None):
    s = json.load(open(SEED))
    got = open("/usr/lib/bandwatch/VERSION").read().strip()
    assert got == version, "installed %s, expected %s" % (got, version)
    db = sqlite3.connect("file:%s/bandwatch.db?mode=ro" % DATA, uri=True)
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    rx = db.execute("SELECT coalesce(sum(rx), 0) FROM m1 WHERE s='wan' AND k='*' AND t >= ? AND t < ?", (s["t0"], s["t1"])).fetchone()[0]
    assert rx == s["wan_rx"], "history changed: %s != %s" % (rx, s["wan_rx"])
    dev = db.execute("SELECT coalesce(sum(rx), 0) FROM m1 WHERE s='dev' AND k=?", (MAC,)).fetchone()[0]
    assert dev >= 240 * 400_000, dev
    assert db.execute("SELECT alias FROM devices WHERE mac=?", (MAC,)).fetchone()[0] == "Living room TV"
    assert db.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0] == "1"
    db.close()
    st = json.load(open(DATA + "/settings.json"))
    assert st["storage.day_retention_days"] == 1000 and st["ui.theme"] == "light" and st["devices.bandix_direction"] == "tx_is_download", st
    assert json.load(open(DATA + "/secrets.json")).get("aria2_secret") == "seeded-secret"
    r, body = _req("POST", "/api/login", {"password": PASSWORD})
    assert r.status == 200, (r.status, body)
    cookie = r.getheader("Set-Cookie").split(";")[0]
    r, body = _req("GET", "/api/health", cookie=cookie)
    assert r.status == 200
    h = json.loads(body)
    assert h["version"] == version
    if kind:
        assert h["lifecycle"]["current"]["kind"] == kind, h["lifecycle"]
        assert h["persistence"]["history_predates_this_start"] is True, h["persistence"]
        r, body = _req("GET", "/api/diagnostics/report.zip", cookie=cookie)
        assert r.status == 200 and body[:2] == b"PK"
        text = zipfile.ZipFile(io.BytesIO(body)).read("bandwatch-report.json").decode()
        assert MAC not in text and "Living room TV" not in text and "seeded-secret" not in text
        assert not re.search(r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])", text), "IPv4 address in report"
    print("verified", version, kind or "")


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        seed()
    else:
        verify(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
