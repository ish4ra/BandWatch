#!/usr/bin/env python3
"""Build BandWatch release artefacts into dist/.

Produces (all architecture independent):

* ``bandwatch-<ver>-openwrt.tar.gz``      installation archive with install.sh
* ``bandwatch_<ver>_all.ipk``              opkg package (OpenWrt <= 24.10)
* ``luci-app-bandwatch_<ver>_all.ipk``
* ``bandwatch-<ver>.apk``                  apk v3 package (OpenWrt 25.12+), when
* ``luci-app-bandwatch-<ver>.apk``         ``apk mkpkg`` or Docker is available
* ``SHA256SUMS``

Archives are reproducible: fixed mtimes (SOURCE_DATE_EPOCH), root ownership,
sorted entries. Packages are unsigned (install with --allow-untrusted /
opkg's local file install) until a signing key is set up by the maintainer.
"""

import argparse
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from bandwatch import __version__  # noqa: E402

DESCRIPTION = "Lightweight, privacy-focused local traffic monitor (WAN, devices, aria2) with a web dashboard"
DEPENDS = ["python3-light", "python3-sqlite3", "python3-urllib"]
URL = "https://github.com/ish4ra/BandWatch"
EXECUTABLE = {
    "etc/init.d/bandwatch", "usr/bin/bandwatch", "usr/sbin/bandwatch-diag", "usr/libexec/rpcd/luci.bandwatch",
}


def epoch():
    try:
        return int(os.environ.get("SOURCE_DATE_EPOCH") or subprocess.check_output(["git", "-C", ROOT, "log", "-1", "--format=%ct"], text=True).strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 1_790_000_000


def ipk_version(v):
    # 0.1.0-beta.1 -> 0.1.0~beta1 (sorts before 0.1.0 in opkg)
    return v.replace("-beta.", "~beta").replace("-rc.", "~rc")


def apk_version(v):
    # 0.1.0-beta.1 -> 0.1.0_beta1 (apk version syntax)
    return v.replace("-beta.", "_beta").replace("-rc.", "_rc")


def collect_core():
    """Map of install path -> (source path or bytes)."""
    files = {}
    pkg = os.path.join(ROOT, "bandwatch")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, ROOT)
            files["usr/lib/bandwatch/" + rel] = src
    files["usr/lib/bandwatch/VERSION"] = (__version__ + "\n").encode()
    files["usr/share/doc/bandwatch/LICENSE"] = os.path.join(ROOT, "LICENSE")
    base = os.path.join(ROOT, "openwrt", "bandwatch", "files")
    for dirpath, _d, filenames in os.walk(base):
        for name in filenames:
            src = os.path.join(dirpath, name)
            files[os.path.relpath(src, base)] = src
    files["usr/sbin/bandwatch-diag"] = os.path.join(ROOT, "scripts", "bandwatch-diag.sh")
    return files


def collect_luci():
    files = {}
    base = os.path.join(ROOT, "openwrt", "luci-app-bandwatch")
    for sub, prefix in (("root", ""), ("htdocs", "www/")):
        top = os.path.join(base, sub)
        for dirpath, _d, filenames in os.walk(top):
            for name in filenames:
                src = os.path.join(dirpath, name)
                files[prefix + os.path.relpath(src, top)] = src
    return files


def _data(src):
    if isinstance(src, bytes):
        return src
    with open(src, "rb") as fh:
        return fh.read()


def _add(tar, name, data, mtime, mode=None, is_dir=False):
    info = tarfile.TarInfo(name)
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        return
    info.size = len(data)
    info.mode = mode if mode is not None else 0o644
    tar.addfile(info, io.BytesIO(data))


def _mode(path):
    rel = path.lstrip("./")
    return 0o755 if rel in EXECUTABLE or rel.endswith(".sh") else 0o644


def tar_gz(files, mtime, prefix="./", extra_dirs=True):
    """Deterministic .tar.gz bytes from {path: src}."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        dirs = set()
        for path in sorted(files):
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if extra_dirs and d not in dirs:
                    dirs.add(d)
                    _add(tar, prefix + d + "/", b"", mtime, is_dir=True)
            _add(tar, prefix + path, _data(files[path]), mtime, _mode(path))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=mtime, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def installed_size(files):
    return sum(len(_data(s)) for s in files.values())


def build_ipk(name, version, files, depends, scripts, mtime, conffiles=()):
    control = "\n".join(
        [
            "Package: %s" % name,
            "Version: %s" % version,
            "Depends: %s" % ", ".join(depends),
            "Source: %s" % URL,
            "SourceName: %s" % name,
            "License: MIT",
            "LicenseFiles: LICENSE",
            "Section: net",
            "URL: %s" % URL,
            "Maintainer: BandWatch contributors",
            "Architecture: all",
            "Installed-Size: %d" % installed_size(files),
            "Description: %s" % DESCRIPTION,
        ]
    ) + "\n"
    ctrl = {"control": control.encode()}
    if conffiles:
        ctrl["conffiles"] = ("\n".join("/" + c for c in conffiles) + "\n").encode()
    for kind, path in scripts.items():
        ctrl[kind] = _data(path)
    ctrl_files = {k: v for k, v in ctrl.items()}
    # control scripts must be executable
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for k in sorted(ctrl_files):
            _add(tar, "./" + k, ctrl_files[k], mtime, 0o755 if k in ("postinst", "prerm", "postrm", "preinst") else 0o644)
    ctrl_gz = io.BytesIO()
    with gzip.GzipFile(fileobj=ctrl_gz, mode="wb", mtime=mtime) as gz:
        gz.write(raw.getvalue())
    data_gz = tar_gz(files, mtime)
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        _add(tar, "./debian-binary", b"2.0\n", mtime)
        _add(tar, "./data.tar.gz", data_gz, mtime)
        _add(tar, "./control.tar.gz", ctrl_gz.getvalue(), mtime)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=mtime) as gz:
        gz.write(outer.getvalue())
    return out.getvalue()


def stage(files, root):
    for path, src in files.items():
        dst = os.path.join(root, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(_data(src))
        os.chmod(dst, _mode(path))


def build_apk(name, version, files, depends, scripts, workdir, out_path, mtime):
    """Build an apk v3 package with ``apk mkpkg`` (locally or in Docker)."""
    root = os.path.join(workdir, name + "-root")
    if os.path.isdir(root):
        shutil.rmtree(root)
    stage(files, root)
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (mtime, mtime), follow_symlinks=False)
    sdir = os.path.join(workdir, name + "-scripts")
    os.makedirs(sdir, exist_ok=True)
    args = [
        "mkpkg",
        "--info", "name:%s" % name,
        "--info", "version:%s" % version,
        "--info", "description:%s" % DESCRIPTION,
        "--info", "arch:noarch",
        "--info", "license:MIT",
        "--info", "origin:%s" % name,
        "--info", "url:%s" % URL,
        "--info", "maintainer:BandWatch contributors",
        "--info", "depends:%s" % " ".join(depends),
    ]
    for kind, path in scripts.items():
        dst = os.path.join(sdir, kind)
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o755)
        args += ["--script", "%s:%s" % (kind, dst)]
    args += ["--files", root, "--output", out_path]
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(mtime))
    apk = shutil.which("apk")
    if apk and subprocess.run([apk, "mkpkg", "--help"], capture_output=True).returncode == 0:
        subprocess.run([apk] + args, check=True, env=env)
        return True
    docker = shutil.which("docker")
    if not docker:
        print("  apk: skipped (no 'apk mkpkg' and no Docker available)")
        return False
    # Run apk-tools v3 from Alpine with the work dir mounted at the same path.
    cmd = [docker, "run", "--rm", "-e", "SOURCE_DATE_EPOCH=%d" % mtime, "-v", "%s:%s" % (workdir, workdir),
           "-v", "%s:%s" % (os.path.dirname(out_path), os.path.dirname(out_path)),
           "--user", "%d:%d" % (os.getuid(), os.getgid()), os.environ.get("APK_IMAGE", "alpine:edge"), "apk"] + args
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print("  apk: build failed (exit %d)" % res.returncode)
        return False
    return True


SDK_IMAGE = os.environ.get("BANDWATCH_SDK_IMAGE", "openwrt/sdk:x86-64-25.12.0")


def sign_apks(paths, key_path):
    """Sign apk v3 packages with OpenWrt's own apk-tools (from the SDK image).

    apk-tools from other distributions produced signatures that OpenWrt's
    apk 3.0.x did not accept in testing, so the SDK's host apk is used.
    The private key never leaves the build machine and is never committed.
    """
    if not paths:
        return True
    docker = shutil.which("docker")
    if not docker or not os.path.isfile(key_path):
        return False
    out = os.path.dirname(paths[0])
    keydir = os.path.dirname(os.path.abspath(key_path))
    cmd = [docker, "run", "--rm", "-v", "%s:/out" % out, "-v", "%s:/key:ro" % keydir, "--user", "0:0", SDK_IMAGE, "sh", "-c",
           " && ".join("/builder/staging_dir/host/bin/apk adbsign --allow-untrusted --sign-key /key/%s /out/%s" % (os.path.basename(key_path), os.path.basename(p)) for p in paths)]
    return subprocess.run(cmd).returncode == 0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--no-apk", action="store_true")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, ".work")
    os.makedirs(work, exist_ok=True)
    mtime = epoch()
    ver = __version__
    core = collect_core()
    luci = collect_luci()
    pkgdir = os.path.join(ROOT, "scripts", "pkg")
    produced = []

    # 1. Installation archive
    top = "bandwatch-%s/" % ver
    archive = {}
    for p, s in core.items():
        archive["rootfs/" + p] = s
    for p, s in luci.items():
        archive["rootfs-luci/" + p] = s
    for n in ("install.sh", "uninstall.sh"):
        archive[n] = os.path.join(ROOT, "scripts", n)
    for n in ("postinst", "prerm", "postrm", "luci-postinst"):
        archive["scripts/" + n] = os.path.join(pkgdir, n)
    archive["VERSION"] = (ver + "\n").encode()
    archive["README-INSTALL.txt"] = os.path.join(ROOT, "docs", "INSTALL-QUICK.txt")
    archive["bandwatch-test-round.sh"] = os.path.join(ROOT, "scripts", "bandwatch-test-round.sh")
    sums = "".join("%s  %s\n" % (hashlib.sha256(_data(archive[p])).hexdigest(), p) for p in sorted(archive) if p.startswith(("rootfs", "scripts/")))
    archive["SHA256SUMS"] = sums.encode()
    path = os.path.join(out, "bandwatch-%s-openwrt.tar.gz" % ver)
    with open(path, "wb") as fh:
        fh.write(tar_gz(archive, mtime, prefix=top))
    produced.append(path)

    # 2. ipk packages
    iv = ipk_version(ver) + "-1"
    for name, files, deps, scripts, conff in (
        ("bandwatch", core, DEPENDS, {"postinst": os.path.join(pkgdir, "postinst"), "prerm": os.path.join(pkgdir, "prerm"), "postrm": os.path.join(pkgdir, "postrm")}, ["etc/config/bandwatch"]),
        ("luci-app-bandwatch", luci, ["bandwatch", "luci-base"], {"postinst": os.path.join(pkgdir, "luci-postinst"), "postrm": os.path.join(pkgdir, "luci-postrm")}, []),
    ):
        path = os.path.join(out, "%s_%s_all.ipk" % (name, iv))
        with open(path, "wb") as fh:
            fh.write(build_ipk(name, iv, files, deps, scripts, mtime, conff))
        produced.append(path)

    # 3. apk v3 packages
    if not args.no_apk:
        av = apk_version(ver) + "-r1"
        for name, files, deps, scripts in (
            ("bandwatch", core, DEPENDS, {"post-install": os.path.join(pkgdir, "postinst"), "post-upgrade": os.path.join(pkgdir, "postinst"), "pre-deinstall": os.path.join(pkgdir, "prerm"), "post-deinstall": os.path.join(pkgdir, "postrm")}),
            ("luci-app-bandwatch", luci, ["bandwatch", "luci-base"], {"post-install": os.path.join(pkgdir, "luci-postinst"), "post-upgrade": os.path.join(pkgdir, "luci-postinst"), "post-deinstall": os.path.join(pkgdir, "luci-postrm")}),
        ):
            path = os.path.join(out, "%s-%s.apk" % (name, av))
            if build_apk(name, av, files, deps, scripts, work, path, mtime):
                produced.append(path)

    # 4. Optional apk signing (maintainer key supplied at build time only).
    key = os.environ.get("BANDWATCH_APK_SIGN_KEY")
    if key:
        apks = [p for p in produced if p.endswith(".apk")]
        if sign_apks(apks, key):
            print("  apk packages signed with %s (install the matching public key in /etc/apk/keys/)" % os.path.basename(key))
        else:
            print("  apk signing FAILED; packages remain unsigned")
            sys.exit(1)

    shutil.rmtree(work, ignore_errors=True)
    with open(os.path.join(out, "SHA256SUMS"), "w") as fh:
        for p in sorted(produced):
            fh.write("%s  %s\n" % (sha256(p), os.path.basename(p)))
    for p in produced:
        print("%8.1f KiB  %s" % (os.path.getsize(p) / 1024, os.path.relpath(p, ROOT)))
    print("SHA256SUMS written")
    bundle = build_tester_bundle(out, produced, mtime)
    print("%8.1f KiB  %s" % (os.path.getsize(bundle) / 1024, os.path.relpath(bundle, ROOT)))


TESTER_DOCS = {
    "README-FIRST.txt": "docs/tester/README-FIRST.txt",
    "INSTALL-SINHALA.md": "docs/INSTALL-SINHALA.md",
    "INSTALL.md": "docs/INSTALL.md",
    "HARDWARE-TESTING.md": "docs/HARDWARE-TESTING.md",
    "TROUBLESHOOTING.md": "docs/TROUBLESHOOTING.md",
    "UPGRADE.md": "docs/UPGRADE.md",
    "UPGRADE-SINHALA.md": "docs/UPGRADE-SINHALA.md",
    "bandwatch-test-round.sh": "scripts/bandwatch-test-round.sh",
}


def build_tester_bundle(out, produced, mtime):
    """One zip for hardware testers: packages, checksums, guides."""
    import zipfile

    name = "bandwatch-%s-tester-bundle" % __version__
    path = os.path.join(out, name + ".zip")
    stamp = time_tuple(mtime)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(produced) + [os.path.join(out, "SHA256SUMS")]:
            info = zipfile.ZipInfo("%s/%s" % (name, os.path.basename(p)), stamp)
            info.external_attr = 0o644 << 16
            with open(p, "rb") as fh:
                zf.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
        for arc, src in sorted(TESTER_DOCS.items()):
            info = zipfile.ZipInfo("%s/%s" % (name, arc), stamp)
            info.external_attr = (0o755 if arc.endswith(".sh") else 0o644) << 16
            with open(os.path.join(ROOT, src), "rb") as fh:
                zf.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
    return path


def time_tuple(ts):
    import time as _time

    t = _time.gmtime(max(ts, 315532800))
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


if __name__ == "__main__":
    main()
