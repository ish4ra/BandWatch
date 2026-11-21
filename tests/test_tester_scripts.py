"""Regression tests for the tester delivery scripts.

Covers the hardware test-round helper (safe /tmp paths, sanitising,
report leak check), bandwatch-diag's shared sanitiser, the uninstaller's
--purge path validation and the consistency of the tester instructions.
The same scripts are exercised under BusyBox in tests/openwrt/run.sh.
"""

import os
import re
import subprocess
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "scripts", "bandwatch-test-round.sh")
DIAG = os.path.join(ROOT, "scripts", "bandwatch-diag.sh")
UNINSTALL = os.path.join(ROOT, "scripts", "uninstall.sh")

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
IPV4_RE = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}")


def lib(snippet, tmp_path, stdin="", env=None):
    e = dict(os.environ, BW_TEST_LIB="1", **(env or {}))
    script = '. "%s"\n%s' % (HELPER, snippet)
    return subprocess.run(["sh", "-c", script], input=stdin, capture_output=True, text=True, cwd=str(tmp_path), env=e, timeout=30)


def sanitize(text, tmp_path, literals=()):
    lit = tmp_path / "lits"
    lit.write_text("".join(x + "\n" for x in literals))
    r = lib('sanitize "%s"' % lit, tmp_path, stdin=text)
    assert r.returncode == 0, r.stderr
    return r.stdout


PRIVATE_SAMPLE = """\
Sat Nov 21 08:47:38 2026 daemon.info bandwatch[info] dashboard listening on http://192.168.1.1:8787/
loopback http://127.0.0.1:8787/ ula [fd9a:7086:58e5::1]:8787 link-local fe80::7cb4:87ff:fe52:4691
mac aa:bb:cc:dd:ee:ff dash AA-BB-CC-DD-EE-01 cisco aabb.ccdd.eeff full 2001:db8:1:2:3:4:5:6
host my-phone.lan SSID HomeWiFi key superSecretKey1 cgnat 100.64.12.34
aria2: Download finished: Holiday Video (2026).mkv
aria2: Download failed: tax-return.pdf (code 3)
{"hostname": "my-phone", "ip": "10.0.0.5", "rx": 123}
rpc secret=abc123 password: hunter22 "token":"tok_xyz" via http://admin:pw@192.168.1.10:6800/jsonrpc
"""


def test_sanitize_removes_private_data(tmp_path):
    out = sanitize(PRIVATE_SAMPLE, tmp_path, ["my-phone", "HomeWiFi", "superSecretKey1"])
    for bad in ("my-phone", "HomeWiFi", "superSecretKey1", "Holiday Video", "tax-return", "abc123", "hunter22",
                "tok_xyz", "admin:pw", "fd9a:7086", "fe80::", "2001:db8", "aabb.ccdd", "100.64.12.34"):
        assert bad not in out, (bad, out)
    assert not MAC_RE.search(out) and not IPV4_RE.search(out), out
    assert "<loopback>:8787" in out and "<mac>" in out and "<ipv6>" in out and "<file name>" in out


def test_sanitize_keeps_useful_information(tmp_path):
    text = "OpenWrt 25.12.5 r29000 BandWatch 0.1.0-beta.1 Python 3.13.9 up 08:47:38 load 0.00, 0.01\nWAN device wwan0 detected (wwan)\n"
    assert sanitize(text, tmp_path) == text


def test_bandix_json_reduced_to_structure(tmp_path):
    doc = '{"aa:bb:cc:dd:ee:ff":{"h":"my-phone","i":"10.0.0.5","w_rx_b":5,"tags":["kid","tablet"]}}'
    r = lib("json_structure", tmp_path, stdin=doc)
    assert r.returncode == 0
    assert "my-phone" not in r.stdout and "10.0.0.5" not in r.stdout and "kid" not in r.stdout
    assert '"w_rx_b":5' in r.stdout and '"h":"<s>"' in r.stdout


def test_diag_shares_the_same_sanitiser():
    def block(path):
        s = open(path).read()
        return s[s.index("collect_private_literals() {"):s.index("# JSON from a provider") if "# JSON from a provider" in s else s.index("# --- end of shared sanitiser")]
    assert block(HELPER).strip() == block(DIAG).strip()
    diag = open(DIAG).read()
    assert "mktemp" in diag and ".bandwatch-diag-hosts" not in diag


def test_safe_dir_refuses_symlink_and_files(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "work").symlink_to(target)
    r = lib('safe_dir "%s"' % (tmp_path / "work"), tmp_path)
    assert r.returncode != 0 and "symbolic link" in r.stderr
    (tmp_path / "afile").write_text("x")
    r = lib('safe_dir "%s"' % (tmp_path / "afile"), tmp_path)
    assert r.returncode != 0 and "not a directory" in r.stderr
    r = lib('safe_dir "%s" && ls -ld "%s"' % (tmp_path / "new", tmp_path / "new"), tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("drwx------"), r.stdout + r.stderr
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)
    assert lib('safe_dir "%s"' % loose, tmp_path).returncode == 0
    assert oct(os.stat(loose).st_mode & 0o777) == "0o700"


def test_report_is_sanitised_and_private(tmp_path):
    work, reports = tmp_path / "work", tmp_path / "reports"
    work.mkdir(mode=0o700)
    (work / "after.txt").write_text(PRIVATE_SAMPLE + "VERDICT: PASS (BandWatch listens only on loopback and LAN addresses)\n")
    (work / "diagnostics.txt").write_text("device secret-laptop-x at 192.168.1.50 aa:bb:cc:11:22:33\n")
    (work / "perf.csv").write_text("t_s,cpu_percent,rss_kib,threads\n30,0.18,25400,4\n")
    lits = tmp_path / "extra"
    lits.write_text("my-phone\nHomeWiFi\nsuperSecretKey1\nsecret-laptop-x\n")
    env = dict(os.environ, BW_TEST_DIR=str(work), BW_TEST_REPORTS=str(reports), BW_TEST_ALLOW_USER="1", BW_TEST_LITERALS=str(lits))
    r = subprocess.run(["sh", HELPER, "report"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Only reboot the router AFTER the report is on your computer" in r.stdout
    files = list(reports.glob("bandwatch-pre-reboot-report-*.tar.gz"))
    assert len(files) == 1 and oct(os.stat(reports).st_mode & 0o777) == "0o700"
    assert oct(os.stat(files[0]).st_mode & 0o077) == "0o0"
    with tarfile.open(files[0]) as t:
        content = "".join(t.extractfile(m).read().decode() for m in t.getmembers() if m.isfile())
        names = sorted(os.path.basename(m.name) for m in t.getmembers() if m.isfile())
    assert names == ["after.txt", "diagnostics.txt", "perf.csv"]
    for bad in ("my-phone", "HomeWiFi", "superSecretKey1", "secret-laptop-x", "Holiday Video", "hunter22"):
        assert bad not in content
    assert not MAC_RE.search(content) and not IPV4_RE.search(content)
    assert "VERDICT: PASS" in content and "30,0.18,25400,4" in content
    assert not (work / "stage").exists() and not (work / "private-literals").exists()


def test_report_refuses_symlinked_report_dir(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    (tmp_path / "victim").mkdir()
    (tmp_path / "reports").symlink_to(tmp_path / "victim")
    env = dict(os.environ, BW_TEST_DIR=str(work), BW_TEST_REPORTS=str(tmp_path / "reports"), BW_TEST_ALLOW_USER="1")
    r = subprocess.run(["sh", HELPER, "report"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "symbolic link" in r.stderr
    assert list((tmp_path / "victim").iterdir()) == []


def test_helper_requires_root_by_default(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BW_TEST")}
    env["BW_TEST_DIR"] = str(tmp_path / "w")
    r = subprocess.run(["sh", HELPER, "before"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode != 0 and "root" in r.stderr


# --- uninstall --purge path validation --------------------------------------

def check_purge(path):
    r = subprocess.run(["sh", UNINSTALL, "--check-purge-dir=%s" % path], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip()


def make_data_dir(base):
    d = base / "bandwatch"
    (d / "tls").mkdir(parents=True)
    for f in ("bandwatch.db", "bandwatch.db-wal", "settings.json", "auth.json", "secrets.json", "tls/cert.pem", "tls/key.pem"):
        (d / f).write_text("x")
    return d


def test_purge_accepts_genuine_data_dir(tmp_path):
    d = make_data_dir(tmp_path)
    assert check_purge(d) == (0, "OK")
    assert check_purge(str(d) + "/") == (0, "OK")
    alt = tmp_path / "bandwatch-data"
    alt.mkdir()
    (alt / "bandwatch.db").write_text("x")
    assert check_purge(alt)[0] == 0


@pytest.mark.parametrize("path, reason", [
    ("", "empty"), ("/", "not a data directory"), ("/etc", "not named"), ("/bandwatch", "directly under /"),
    ("/usr/lib/bandwatch", "system location"), ("/proc/bandwatch", "system location"), ("/www/bandwatch", "system location"),
    ("relative/bandwatch", "not an absolute path"), ("/etc/../etc/bandwatch", "not a canonical path"),
    ("/mnt/usb/mybandwatchstuff", "not named"),
])
def test_purge_rejects_dangerous_paths(path, reason):
    rc, out = check_purge(path)
    assert rc == 2 and out.startswith("REFUSE") and reason in out, out


def test_purge_rejects_symlinks(tmp_path):
    real = make_data_dir(tmp_path / "real")
    (tmp_path / "bandwatch").symlink_to(real)
    rc, out = check_purge(tmp_path / "bandwatch")
    assert rc == 2 and "symbolic link" in out
    (tmp_path / "via").symlink_to(tmp_path / "real")
    rc, out = check_purge(tmp_path / "via" / "bandwatch")
    assert rc == 2 and "resolves to" in out
    (real / "settings.json").unlink()
    (real / "settings.json").symlink_to("/etc/passwd")
    rc, out = check_purge(real)
    assert rc == 2 and "settings.json(link)" in out


def test_purge_rejects_foreign_contents(tmp_path):
    d = make_data_dir(tmp_path)
    (d / "family-photos").mkdir()
    rc, out = check_purge(d)
    assert rc == 2 and "family-photos" in out
    (d / "family-photos").rmdir()
    (d / "tls" / "notes.txt").write_text("x")
    rc, out = check_purge(d)
    assert rc == 2 and "tls/notes.txt" in out


def test_purge_validated_before_anything_is_removed():
    s = open(UNINSTALL).read()
    assert s.index('why=$(purge_dir_problem "$DATA_DIR")') < s.index("Stopping BandWatch")
    assert '[ -z "$(purge_dir_problem "$DATA_DIR")" ]' in s  # re-checked right before rm


# --- tester instructions ------------------------------------------------------

TESTER_DOCS = ["docs/INSTALL-SINHALA.md", "docs/INSTALL.md", "docs/HARDWARE-TESTING.md", "docs/tester/README-FIRST.txt"]


@pytest.mark.parametrize("doc", TESTER_DOCS)
def test_report_is_copied_before_reboot(doc):
    s = open(os.path.join(ROOT, doc), encoding="utf-8").read()
    report = s.index("test-round.sh report")
    copy = s.index('scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .', report)
    reboot = re.search(r"(^|[\s`(])reboot($|[\s`)])", s[copy:], re.M)
    assert reboot, "no reboot step after copying the report"
    post = s.index("test-round.sh postreboot")
    assert copy < copy + reboot.start() < post


@pytest.mark.parametrize("doc", TESTER_DOCS)
def test_wan_isolation_guidance_mentions_cgnat_and_local_check(doc):
    s = open(os.path.join(ROOT, doc), encoding="utf-8").read()
    assert "CGNAT" in s and ("VERDICT" in s or "netstat" in s)


def test_sinhala_guide_has_no_reboot_inside_test_list():
    s = open(os.path.join(ROOT, "docs/INSTALL-SINHALA.md"), encoding="utf-8").read()
    tests = s[s.index("## 4."):s.index("## 5.")]
    assert "Router **reboot** කරන්න" not in tests
    assert "postreboot" in s[s.index("## 6."):]
