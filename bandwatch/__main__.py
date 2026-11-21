"""Command line interface: ``python3 -m bandwatch <command>``."""

import argparse
import getpass
import json
import os
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time

from . import __version__, log
from . import config as cfgmod


def _wait_for_signal(stop):
    def handler(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass
    while not stop.is_set():
        stop.wait(1.0)


def _start(service, platform, engine_factory=None):
    from .auth import Auth
    from .engine import Engine
    from .server import App, resolve_listen, serve

    engine = (engine_factory or Engine)(service, platform)
    auth = Auth(service.data_dir, service.run_dir, service.auth)
    if service.auth == "password" and not auth.password_set():
        log.warning("no dashboard password is set; run 'bandwatch passwd' on the router")
    app = App(engine, auth, service)
    addrs = resolve_listen(service, platform)
    try:
        servers = serve(app, addrs, service.port)
    except RuntimeError as exc:
        log.error("%s", exc)
        engine.store.close()
        return 2
    engine.start()
    log.info("BandWatch %s started (%s mode)", __version__, "demo" if service.demo else "production")
    stop = threading.Event()
    _wait_for_signal(stop)
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    engine.stop()
    log.info("stopped")
    return 0


def cmd_run(args):
    from .platform import LinuxPlatform

    os.umask(0o027)  # database and settings are not world-readable
    try:
        service = cfgmod.ServiceConfig.from_uci(args.config)
    except ValueError as exc:
        log.error("invalid configuration in %s: %s", args.config, exc)
        return 2
    if args.port:
        service.port = args.port
    log.set_level(service.log_level)
    if not service.enabled:
        log.info("BandWatch is disabled in %s (option enabled '0')", args.config)
        return 0
    try:
        os.makedirs(os.path.join(service.run_dir, "sso"), mode=0o700, exist_ok=True)
    except OSError as exc:
        log.warning("cannot create %s: %s (LuCI sign-on unavailable)", service.run_dir, exc)
    return _start(service, LinuxPlatform())


def cmd_demo(args):
    from . import demo
    from .engine import Engine

    data_dir = args.data_dir or os.path.join(tempfile.gettempdir(), "bandwatch-demo")
    if os.path.abspath(data_dir) in ("/etc/bandwatch", "/etc/bandwatch/"):
        log.error("refusing to use the production data directory for demo data")
        return 2
    os.makedirs(data_dir, exist_ok=True)
    listen = [args.listen]
    service = cfgmod.ServiceConfig(
        listen=listen, port=args.port, data_dir=os.path.abspath(data_dir),
        run_dir=os.path.join(os.path.abspath(data_dir), "run"), auth=args.auth, demo=True,
        demo_scenario=args.scenario, allowed_networks=["private"],
    )
    try:
        service.validate()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    os.makedirs(os.path.join(service.run_dir, "sso"), exist_ok=True)
    demo.prepare(service, args.scenario)
    platform = demo.DemoPlatform(args.scenario, seed=args.seed)

    def factory(svc, plat):
        settings = cfgmod.Settings(os.path.join(svc.data_dir, "settings.json"))
        if settings["general.timezone"] == "system":
            settings.update({"general.timezone": demo.DEMO_TZ})
        if args.fast:
            settings.update({"storage.flush_interval": 30})
        eng = Engine(svc, plat, settings=settings)
        if args.scenario != "empty-history" and args.days > 0:
            n = demo.backfill(eng.store, days=args.days)
            if n:
                log.info("demo: generated %d simulated history rows (%d days)", n, args.days)
        return eng

    if args.auth == "password":
        from .auth import Auth

        auth = Auth(service.data_dir, service.run_dir)
        if not auth.password_set():
            auth.set_password(args.password)
            log.info("demo: dashboard password is %r", args.password)
    log.info("demo scenario '%s': %s", args.scenario, demo.SCENARIOS[args.scenario])
    log.info("open http://%s:%d/ (all data shown is SIMULATED)", args.listen if ":" not in args.listen else "[%s]" % args.listen, args.port)
    return _start(service, platform, factory)


def _read_password(args):
    if args.random:
        return secrets.token_urlsafe(12)
    if args.stdin or not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip("\r\n")
        return pw
    pw = getpass.getpass("New BandWatch dashboard password: ")
    again = getpass.getpass("Repeat password: ")
    if pw != again:
        raise ValueError("passwords do not match")
    return pw


def cmd_passwd(args):
    from .auth import Auth

    os.umask(0o077)
    try:
        service = cfgmod.ServiceConfig.from_uci(args.config)
    except ValueError as exc:
        print("invalid configuration: %s" % exc, file=sys.stderr)
        return 2
    data_dir = args.data_dir or service.data_dir
    os.makedirs(data_dir, mode=0o750, exist_ok=True)
    try:
        pw = _read_password(args)
        Auth(data_dir, service.run_dir).set_password(pw)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if args.random:
        print(pw)
    else:
        print("Password updated. Restart BandWatch to sign out existing sessions: /etc/init.d/bandwatch restart")
    return 0


REQUIRED_MODULES = ["sqlite3", "json", "http.server", "http.client", "urllib.parse", "email.utils", "hashlib", "hmac", "secrets", "ipaddress", "socketserver", "threading", "struct", "csv"]


def cmd_check(args):
    """Safe, read-only preflight checks. Exit code 0 = ready, 1 = problems."""
    results = []

    def add(name, ok, detail, required=True):
        results.append({"check": name, "ok": bool(ok), "required": required, "detail": detail})

    add("python", sys.version_info >= (3, 10), sys.version.split()[0] + " (3.10+ required)")
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
            add("module " + mod, True, "available")
        except ImportError as exc:
            add("module " + mod, False, "missing (%s)" % exc)
    try:
        import hashlib

        hashlib.pbkdf2_hmac("sha256", b"x", b"y", 1)
        add("fast password hashing", True, "OpenSSL pbkdf2 available", required=False)
    except (ImportError, AttributeError, ValueError):
        add("fast password hashing", False, "python3-openssl missing: slower pure-Python fallback is used", required=False)
    try:
        service = cfgmod.ServiceConfig.from_uci(args.config)
        add("configuration", True, args.config)
    except ValueError as exc:
        add("configuration", False, str(exc))
        service = cfgmod.ServiceConfig()
    try:
        os.makedirs(service.data_dir, exist_ok=True)
        probe = os.path.join(service.data_dir, ".write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.unlink(probe)
        add("data directory", True, "%s is writable" % service.data_dir)
    except OSError as exc:
        add("data directory", False, "%s: %s" % (service.data_dir, exc.strerror))
    port_free = _port_free(service.port)
    if port_free:
        add("port", True, "TCP %d is free" % service.port)
    else:
        add("port", args.running, "TCP %d is in use%s" % (service.port, " (expected: BandWatch is running)" if args.running else " - change 'option port' in /etc/config/bandwatch"))
    from .platform import LinuxPlatform

    p = LinuxPlatform()
    add("ubus", p.has_ubus, "available" if p.has_ubus else "not found (WAN detection falls back to routing tables)", required=False)
    add("/proc/net/dev", bool(p.read_text("/proc/net/dev")), "interface counters readable")
    st = cfgmod.Settings(os.path.join(service.data_dir, "settings.json"))
    try:
        info = p.http_json(st["devices.bandix_url"].rstrip("/") + "/api/system/info", timeout=2.0)
        add("bandix", True, "Bandix %s reachable" % ((info.get("data") or {}).get("version", "?")), required=False)
    except (OSError, ValueError, AttributeError):
        add("bandix", False, "Bandix not reachable at %s (optional: per-device data)" % st["devices.bandix_url"], required=False)
    add("vnstat", bool(p.which("vnstat")), "installed" if p.which("vnstat") else "not installed (optional)", required=False)
    add("aria2 rpc", _tcp_open("127.0.0.1", _port_of(st["aria2.url"])), "endpoint %s %s" % (st["aria2.url"], "open" if _tcp_open("127.0.0.1", _port_of(st["aria2.url"])) else "closed (optional)"), required=False)
    ok = all(r["ok"] for r in results if r["required"])
    if args.json:
        print(json.dumps({"ok": ok, "version": __version__, "results": results}, indent=1))
    else:
        for r in results:
            mark = "OK  " if r["ok"] else ("FAIL" if r["required"] else "info")
            print("[%s] %-22s %s" % (mark, r["check"], r["detail"]))
        print("\nResult: %s" % ("ready" if ok else "problems found"))
    return 0 if ok else 1


def _port_of(url):
    try:
        return int(url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 80


def _tcp_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _port_free(port):
    for fam, addr in ((socket.AF_INET, "0.0.0.0"),):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def cmd_diag(args):
    """Collect a sanitised diagnostic snapshot without starting the service."""
    from .engine import Engine
    from .platform import LinuxPlatform

    service = cfgmod.ServiceConfig.from_uci(args.config)
    tmp = tempfile.mkdtemp(prefix="bandwatch-diag-")
    diag_service = cfgmod.ServiceConfig(data_dir=tmp, run_dir=tmp, listen=["loopback"], auth="none", lan_interface=service.lan_interface)
    platform = LinuxPlatform()
    settings = cfgmod.Settings(os.path.join(service.data_dir, "settings.json"))
    settings.path = None  # never write the real settings
    secrets_store = cfgmod.SecretStore(os.path.join(service.data_dir, "secrets.json"))
    eng = Engine(diag_service, platform, settings=settings, secrets=secrets_store)
    now = platform.monotonic()
    eng.clock_trusted = True
    eng.wall_offset = time.time() - now
    eng.wan.detect(now)
    eng.wan.sample(now, lambda *a, **k: None)
    time.sleep(1.0)
    now = platform.monotonic()
    eng.wan.sample(now, lambda *a, **k: None)
    eng.dev_snaps = eng.devices.poll(now, lambda *a, **k: None) or {}
    eng.identity.poll(now, eng.dev_snaps)
    eng.aria2.poll(now, time.time(), lambda *a, **k: None)
    eng.system.poll(now)
    out = eng.diagnostics()
    out["note"] = "Generated by 'bandwatch diag'. MAC addresses, IP addresses, hostnames and secrets are not included."
    eng.store.close()
    for name in os.listdir(tmp):
        try:
            os.unlink(os.path.join(tmp, name))
        except OSError:
            pass
    os.rmdir(tmp) if not os.listdir(tmp) else None
    print(json.dumps(out, indent=1, default=str))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bandwatch", description="BandWatch traffic monitor for OpenWrt")
    parser.add_argument("--version", action="version", version="BandWatch " + __version__)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="run the monitoring service")
    p.add_argument("--config", default=cfgmod.DEFAULT_UCI_PATH)
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_run)

    from .demo import SCENARIOS

    p = sub.add_parser("demo", help="run with a simulated router (development)")
    p.add_argument("--scenario", default="aw1000-cellular", choices=sorted(SCENARIOS))
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--listen", default="127.0.0.1")
    p.add_argument("--data-dir")
    p.add_argument("--days", type=int, default=45, help="days of simulated history to generate")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--auth", choices=["none", "password"], default="none")
    p.add_argument("--password", default="bandwatch-demo")
    p.add_argument("--fast", action="store_true", help="flush to the database every 30 s")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("passwd", help="set the dashboard password")
    p.add_argument("--config", default=cfgmod.DEFAULT_UCI_PATH)
    p.add_argument("--data-dir")
    p.add_argument("--random", action="store_true", help="generate and print a random password")
    p.add_argument("--stdin", action="store_true", help="read the password from standard input")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("check", help="read-only preflight checks")
    p.add_argument("--config", default=cfgmod.DEFAULT_UCI_PATH)
    p.add_argument("--json", action="store_true")
    p.add_argument("--running", action="store_true", help="BandWatch is expected to be running (port in use is OK)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("diag", help="print a sanitised diagnostic snapshot")
    p.add_argument("--config", default=cfgmod.DEFAULT_UCI_PATH)
    p.set_defaults(func=cmd_diag)

    p = sub.add_parser("version", help="print the version")
    p.set_defaults(func=lambda a: print(__version__) or 0)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
