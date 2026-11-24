"""Upgrade from the *published* 0.1.0-beta.1 code, rollback and re-upgrade.

The beta.1 release archive is not stored in this repository. CI downloads it
from the GitHub release, verifies it against the release's SHA256SUMS and
passes its path in ``BW_BETA1_TARBALL``; locally:

    curl -LO https://github.com/ish4ra/BandWatch/releases/download/v0.1.0-beta.1/bandwatch-0.1.0-beta.1-openwrt.tar.gz
    BW_BETA1_TARBALL=$PWD/bandwatch-0.1.0-beta.1-openwrt.tar.gz python -m pytest tests/test_upgrade_beta1.py

beta.1 runs in a separate Python process from its own extracted package, so
the database, settings, secrets and password files are written by the real
beta.1 code.
"""

import json
import os
import subprocess
import sys
import tarfile

import pytest

from bandwatch import auth as authmod
from bandwatch import config as cfgmod
from bandwatch import demo
from bandwatch import store as stmod
from bandwatch.engine import Engine

TARBALL = os.environ.get("BW_BETA1_TARBALL")
pytestmark = pytest.mark.skipif(not TARBALL or not os.path.isfile(TARBALL), reason="BW_BETA1_TARBALL not provided")

BETA1_WRITE = r"""
import json, sys
from bandwatch import __version__, auth, config, store
assert __version__ == "0.1.0-beta.1", __version__
d = sys.argv[1]
st = store.Store(d + "/bandwatch.db", mode="production")
st.set_meta("first_start", 1790000000)
for i in range(240):  # four hours of WAN minutes + one device
    st.add("wan", "*", 1790000040 + i * 60, 60, 1_000_000, 100_000)
    st.add("dev", "aa:bb:cc:00:11:22", 1790000040 + i * 60, 60, 400_000, 40_000)
    st.add("dev", "*", 1790000040 + i * 60, 60, 400_000, 40_000)
st.flush(now=1790015000, retention={"minute_days": 7, "quarter_days": 400, "device_quarter_days": 60, "day_days": 3650})
st.set_alias("aa:bb:cc:00:11:22", "Living room TV")
st.close()
s = config.Settings(d + "/settings.json")
assert not s.update({"storage.day_retention_days": 1000, "aria2.mode": "on", "devices.bandix_direction": "tx_is_download", "ui.theme": "light"})
config.SecretStore(d + "/secrets.json").set("aria2_secret", "beta1-secret")
auth.Auth(d, d + "/run").set_password("beta1-password")
print("ok")
"""

BETA1_READ = r"""
import json, sys
from bandwatch import auth, config, store
d = sys.argv[1]
st = store.Store(d + "/bandwatch.db", mode="production")
out = {
    "wan_rx": st.summary("wan", "*", 0, 1_900_000_000)[store.RX],
    "alias": st.devices().get("aa:bb:cc:00:11:22", {}).get("alias"),
    "schema": st.meta("schema_version"),
    "quick_check": st.conn.execute("PRAGMA quick_check").fetchone()[0],
}
st.close()
out["settings"] = config.Settings(d + "/settings.json").as_dict()["storage.day_retention_days"]
out["password_ok"] = auth.Auth(d, d + "/run").check_password("beta1-password")
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def beta1(tmp_path_factory):
    root = tmp_path_factory.mktemp("beta1")
    with tarfile.open(TARBALL) as tf:
        members = [m for m in tf.getmembers() if "/rootfs/usr/lib/bandwatch/" in m.name and (m.isfile() or m.isdir())]
        # Release archive verified against the release's SHA256SUMS in CI;
        # the "data" filter additionally rejects absolute paths and links.
        kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tf.extractall(root, members=members, **kw)
    libs = [dp for dp, _dn, _fn in os.walk(root) if dp.endswith("/usr/lib/bandwatch")]
    assert libs, "beta.1 package not found in archive"
    return libs[0]


def run_beta1(lib, code, data_dir):
    env = dict(os.environ, PYTHONPATH=lib, PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n" % lib + code, str(data_dir)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def beta2_engine(data_dir):
    service = cfgmod.ServiceConfig(listen=["loopback"], data_dir=str(data_dir), run_dir=str(data_dir / "run"), auth="password", demo=False)
    return Engine(service, demo.DemoPlatform())


def test_upgrade_rollback_and_reupgrade_preserve_everything(beta1, tmp_path):
    os.makedirs(tmp_path / "run", exist_ok=True)
    assert run_beta1(beta1, BETA1_WRITE, tmp_path) == "ok"
    wan_before = 240 * 1_000_000

    # -- upgrade to this version ------------------------------------------------------
    eng = beta2_engine(tmp_path)
    cur = eng.lifecycle["current"]
    assert cur["kind"] == "upgrade" and eng.lifecycle["first_start"] == 1790000000
    assert eng.store.summary("wan", "*", 0, 1_900_000_000)[stmod.RX] == wan_before
    assert eng.store.summary("dev", "aa:bb:cc:00:11:22", 0, 1_900_000_000)[stmod.RX] == 240 * 400_000
    assert eng.store.devices()["aa:bb:cc:00:11:22"]["alias"] == "Living room TV"
    s = eng.settings.as_dict()
    assert s["storage.day_retention_days"] == 1000 and s["aria2.mode"] == "on" and s["devices.bandix_direction"] == "tx_is_download" and s["ui.theme"] == "light"
    assert eng.secrets.get("aria2_secret") == "beta1-secret"
    assert authmod.Auth(str(tmp_path), str(tmp_path / "run")).check_password("beta1-password")
    assert eng.store.meta("schema_version") == "1"
    p = eng.persistence_status()
    assert p["history_predates_this_start"] is True and p["integrity_check"] == "ok"
    eng.flush()
    eng.stop()

    # -- roll back to beta.1: it still opens and reads the upgraded data ----------------------
    back = json.loads(run_beta1(beta1, BETA1_READ, tmp_path))
    assert back == {"wan_rx": wan_before, "alias": "Living room TV", "schema": "1", "quick_check": "ok", "settings": 1000, "password_ok": True}

    # -- upgrade again ---------------------------------------------------------------------
    eng = beta2_engine(tmp_path)
    assert eng.lifecycle["current"]["kind"] == "restart" and eng.lifecycle["current"]["previous_stop"] == "clean"
    assert eng.store.summary("wan", "*", 0, 1_900_000_000)[stmod.RX] == wan_before
    assert eng.lifecycle["starts"] == 2
    eng.stop()
