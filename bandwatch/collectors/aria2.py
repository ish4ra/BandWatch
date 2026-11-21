"""Optional aria2 JSON-RPC collector (read-only).

Only these methods are called: ``aria2.getVersion``, ``aria2.getSessionInfo``,
``aria2.getGlobalStat``, ``aria2.tellActive``, ``aria2.tellWaiting`` and
``aria2.tellStopped`` (batched with ``system.multicall``). BandWatch never
adds, pauses, removes or reconfigures downloads and never restarts aria2.

Transfer accounting uses per-task ``completedLength`` / ``uploadLength``
deltas. The first observation of a task is only a baseline, so data that
existed before BandWatch saw the task (pre-existing files, resumed
downloads, sessions restored at aria2 start-up) is never counted as new
traffic. These figures are application payload as reported by aria2: they
are an estimate of aria2's share of the WAN traffic, not a WAN measurement.

The RPC secret is only sent in the POST body to the configured local
endpoint; it is never logged or returned by the BandWatch API.
"""

import os

from .. import counters
from . import DISABLED, ERROR, UNAVAILABLE, Backoff, Status

KEYS = ["gid", "status", "totalLength", "completedLength", "uploadLength", "downloadSpeed", "uploadSpeed", "errorCode", "bittorrent", "files", "numSeeders", "connections"]
STOPPED_WINDOW = 50
WAITING_WINDOW = 100


def task_name(task):
    bt = task.get("bittorrent")
    if isinstance(bt, dict):
        info = bt.get("info")
        if isinstance(info, dict) and info.get("name"):
            return str(info["name"])[:120]
    files = task.get("files")
    if isinstance(files, list) and files:
        f0 = files[0] if isinstance(files[0], dict) else {}
        path = f0.get("path") or ""
        if path:
            return os.path.basename(str(path))[:120]
        uris = f0.get("uris") or []
        if uris and isinstance(uris[0], dict) and uris[0].get("uri"):
            uri = str(uris[0]["uri"]).split("?")[0]
            return (uri.rsplit("/", 1)[-1] or "download")[:120]
    return "task " + str(task.get("gid", "?"))[:16]


def _i(v):
    iv = counters.parse_count(v)
    return 0 if iv is None else iv


class _Task:
    __slots__ = ("counted_rx", "counted_tx", "dirty", "done", "finished", "first_seen", "gid", "last_seen", "name", "status", "total", "up")

    def __init__(self, gid):
        self.gid = gid
        self.done = self.up = None
        self.status = None
        self.name = None
        self.total = 0
        self.first_seen = self.last_seen = None
        self.finished = None
        self.counted_rx = self.counted_tx = 0
        self.dirty = True


class Aria2Collector:
    def __init__(self, platform, settings, secrets):
        self.p = platform
        self.settings = settings
        self.secrets = secrets
        self.status = Status("aria2", "aria2 downloads")
        self.status.provider = "aria2"
        self.backoff = Backoff()
        self.session = None
        self.tasks = {}
        self.live = None
        self.last_poll = None
        self.events = []
        self._removed = []
        self.version_known = False
        # Data that existed when a task was first seen is never counted as
        # transfer. After the first poll of a session such data is either
        # resumed/pre-existing or a short download that started and finished
        # between polls; it is reported here instead of being guessed.
        self.session_polls = 0
        self.uncounted = {"tasks": 0, "bytes": 0}
        self.max_rate = settings["wan.max_rate_mbps"] * 125_000
        self.enabled = settings["aria2.mode"] != "off"
        if not self.enabled:
            self.status.set(DISABLED, "aria2 integration disabled in settings")

    def _token(self):
        secret = self.secrets.get("aria2_secret")
        return "token:" + secret if secret else None

    def _call(self, calls, timeout=2.5):
        token = self._token()
        methods = []
        for name, params in calls:
            full = ([token] if token else []) + list(params)
            methods.append({"methodName": name, "params": full})
        body = {"jsonrpc": "2.0", "id": "bw", "method": "system.multicall", "params": [methods]}
        resp = self.p.http_json(self.settings["aria2.url"], body=body, timeout=timeout)
        if not isinstance(resp, dict):
            raise ValueError("bad response")
        if "error" in resp:
            err = resp["error"] or {}
            msg = str(err.get("message", "error"))[:80]
            raise PermissionError(msg)
        result = resp.get("result")
        if not isinstance(result, list) or len(result) != len(calls):
            raise ValueError("bad multicall result")
        out = []
        for item in result:
            if isinstance(item, dict) and "faultCode" in item:
                raise PermissionError(str(item.get("faultString", "fault"))[:80])
            out.append(item[0] if isinstance(item, list) and item else None)
        return out

    def poll(self, now, wall, emit):
        if not self.enabled:
            return None
        if not self.backoff.ready(now):
            return self.live
        try:
            if not self.version_known:
                (version,) = self._call([("aria2.getVersion", [])])
                self.status.version = str((version or {}).get("version", "?"))[:16]
                self.version_known = True
            stat, info, active, waiting, stopped = self._call(
                [
                    ("aria2.getGlobalStat", []),
                    ("aria2.getSessionInfo", []),
                    ("aria2.tellActive", [KEYS]),
                    ("aria2.tellWaiting", [0, WAITING_WINDOW, KEYS]),
                    ("aria2.tellStopped", [-1, STOPPED_WINDOW, KEYS]),
                ]
            )
        except PermissionError as exc:
            self.status.set(ERROR, "aria2 rejected the request (%s). Check the RPC secret." % exc)
            self._fail(now)
            return None
        except (OSError, ValueError) as exc:
            was_error = self.status.state == ERROR
            if self.settings["aria2.mode"] == "auto" and self.session is None:
                self.status.set(UNAVAILABLE, "aria2 RPC not reachable at %s" % self.settings["aria2.url"])
            else:
                self.status.set(ERROR, "aria2 RPC unavailable (%s)" % (str(exc)[:60] or type(exc).__name__))
                if not was_error and self.session is not None:
                    self.events.append(("warning", "aria2_down", "aria2 RPC became unreachable"))
            self._fail(now)
            return None
        self.backoff.succeed()
        sid = (info or {}).get("sessionId")
        if sid != self.session:
            if self.session is not None:
                self.events.append(("info", "aria2_restart", "aria2 session changed (restart); task baselines reset"))
            self._new_session(sid, wall)
        prev_poll = self.last_poll
        dt = now - prev_poll if prev_poll is not None else None
        self.last_poll = now
        rx = tx = 0
        seen = set()
        counts = {"active": 0, "waiting": 0, "paused": 0, "complete": 0, "error": 0, "removed": 0}
        for group in (active or [], waiting or [], stopped or []):
            for task in group:
                if not isinstance(task, dict) or not task.get("gid"):
                    continue
                gid = str(task["gid"])[:32]
                seen.add(gid)
                status = str(task.get("status", "unknown"))[:16]
                counts[status] = counts.get(status, 0) + 1
                drx, dtx = self._account(gid, task, status, wall, dt)
                rx += drx
                tx += dtx
        rate_ok = dt is not None and dt <= max(15.0, 4 * self.settings["aria2.poll_interval"])
        if dt is not None and dt > 0:
            emit("aria2", "*", now, dt, rx, tx, rate_ok)
        stat = stat or {}
        self.live = {
            "down": _i(stat.get("downloadSpeed")),
            "up": _i(stat.get("uploadSpeed")),
            "active": _i(stat.get("numActive")),
            "waiting": _i(stat.get("numWaiting")),
            "stopped": _i(stat.get("numStoppedTotal") or stat.get("numStopped")),
            "counts": counts,
            "tasks": self._live_tasks(active or []),
            "uncounted": dict(self.uncounted),
        }
        self.session_polls += 1
        # Forget tasks that aria2 no longer reports (purged/removed results).
        for gid in [g for g in self.tasks if g not in seen]:
            t = self.tasks.pop(gid)
            if t.status not in ("complete", "error", "removed"):
                t.status = "removed"
                t.dirty = True
                self._removed.append(t)
        self.status.ok("aria2 %s: %d active, %d waiting" % (self.status.version or "", self.live["active"], self.live["waiting"]))
        return self.live

    def _fail(self, now):
        # Keep task baselines: if aria2 comes back with the same session id,
        # progress made during the outage is real transfer and is counted
        # (without peak-rate statistics). A new session id re-baselines.
        self.backoff.fail(now)
        self.live = None
        self.version_known = False

    def _new_session(self, sid, wall):
        self.session = sid
        self.session_polls = 0
        for t in self.tasks.values():
            t.done = t.up = None  # re-baseline; never count jumps across sessions
        self.last_poll = None

    def _account(self, gid, task, status, wall, dt):
        t = self.tasks.get(gid)
        done, up = _i(task.get("completedLength")), _i(task.get("uploadLength"))
        if t is None:
            t = self.tasks[gid] = _Task(gid)
            t.first_seen = wall
            if self.session_polls > 0 and done > 0:
                self.uncounted["tasks"] += 1
                self.uncounted["bytes"] += done
        t.last_seen = wall
        if t.status != status:
            if status == "complete" and t.status is not None:
                t.finished = wall
                self.events.append(("info", "aria2_complete", "Download finished: %s" % task_name(task)))
            elif status == "error" and t.status is not None:
                self.events.append(("warning", "aria2_error", "Download failed: %s (code %s)" % (task_name(task), str(task.get("errorCode", "?"))[:8])))
            t.status = status
            t.dirty = True
        t.name = task_name(task)
        t.total = _i(task.get("totalLength"))
        drx = dtx = 0
        if t.done is not None and dt is not None:
            if done >= t.done:
                drx = done - t.done
            if up >= t.up:
                dtx = up - t.up
            limit = counters.plausible_max(dt, self.max_rate)
            if drx > limit:  # e.g. integrity check jump: not network traffic
                drx = 0
            if dtx > limit:
                dtx = 0
        if drx or dtx:
            t.counted_rx += drx
            t.counted_tx += dtx
            t.dirty = True
        t.done, t.up = done, up
        return drx, dtx

    def _live_tasks(self, active):
        out = []
        for task in active[:20]:
            if not isinstance(task, dict):
                continue
            total = _i(task.get("totalLength"))
            done = _i(task.get("completedLength"))
            out.append(
                {
                    "gid": str(task.get("gid", ""))[:32],
                    "name": task_name(task),
                    "down": _i(task.get("downloadSpeed")),
                    "up": _i(task.get("uploadSpeed")),
                    "total": total,
                    "done": done,
                    "progress": (done / total) if total else None,
                    "connections": _i(task.get("connections")),
                }
            )
        return out

    def dirty_tasks(self):
        out = []
        for t in list(self.tasks.values()) + self._removed:
            if t.dirty:
                t.dirty = False
                out.append(
                    {
                        "gid": t.gid, "name": t.name, "status": t.status, "total": t.total,
                        "done": t.done, "up": t.up, "first_seen": int(t.first_seen or 0),
                        "last_seen": int(t.last_seen or 0), "finished": int(t.finished) if t.finished else None,
                        "counted_rx": t.counted_rx, "counted_tx": t.counted_tx,
                    }
                )
        self._removed = []
        return out
