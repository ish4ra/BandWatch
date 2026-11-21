"""Minimal logger writing to stderr (captured by procd/logd on OpenWrt).

The standard ``logging`` package is deliberately not used: it is a separate
OpenWrt package (python3-logging) and costs memory on small routers.
Messages must never contain secrets; callers pass already-sanitised text.
"""

import sys
import threading
import time

LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_level = LEVELS["info"]
_lock = threading.Lock()


def set_level(name):
    global _level
    _level = LEVELS.get(str(name).lower(), LEVELS["info"])


def _emit(lvl, msg, args):
    if LEVELS[lvl] < _level:
        return
    if args:
        try:
            msg = msg % args
        except (TypeError, ValueError):
            msg = "%s %r" % (msg, args)
    line = "%s bandwatch[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), lvl, msg)
    # procd logs stdout as daemon.info and stderr as daemon.err.
    stream = sys.stderr if LEVELS[lvl] >= LEVELS["warning"] else sys.stdout
    with _lock:
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):
            pass


def debug(msg, *args):
    _emit("debug", msg, args)


def info(msg, *args):
    _emit("info", msg, args)


def warning(msg, *args):
    _emit("warning", msg, args)


def error(msg, *args):
    _emit("error", msg, args)
