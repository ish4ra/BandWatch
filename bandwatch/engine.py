"""Collection engine: schedules collectors, owns live state and persistence.

A single collector thread performs all sampling, so every dashboard client
shares the same samples. Web requests only read engine state and the
database.
"""

import collections
import json
import os
import threading
import time

from . import __version__, analytics, log
from . import config as cfgmod
from . import store as stmod
from . import tz as tzmod
from .collectors import aria2 as aria2mod
from .collectors import extras, identity, perdevice, wan

MIN_VALID_WALL = 1748736000  # 2025-06-01: earlier clocks are certainly unsynchronised
CLOCK_TRUST_UPTIME = 600.0
GAP_RECOVERY_MAX = 7 * 86400


class LiveSeries:
    """Bounded ring buffer of (wall_ms, rx_rate, tx_rate); None = unavailable."""

    def __init__(self, maxlen):
        self.data = collections.deque(maxlen=maxlen)

    def push(self, wall, rx, tx):
        self.data.append((int(wall * 1000), None if rx is None else round(rx, 1), None if tx is None else round(tx, 1)))

    def since(self, ms):
        return [p for p in self.data if p[0] > ms]

    def last(self):
        return self.data[-1] if self.data else None


class Engine:
    def __init__(self, service, platform, settings=None, secrets=None, store=None):
        self.service = service
        self.p = platform
        cfgmod.ALLOW_LAN_PROVIDERS = bool(getattr(service, "allow_lan_providers", False))
        os.makedirs(service.data_dir, mode=0o750, exist_ok=True)
        self.settings = settings or cfgmod.Settings(os.path.join(service.data_dir, "settings.json"))
        self.secrets = secrets or cfgmod.SecretStore(os.path.join(service.data_dir, "secrets.json"))
        self.tz, self.tz_desc = tzmod.resolve(self.settings["general.timezone"])
        mode = "demo" if service.demo else "production"
        db_name = "bandwatch-demo.db" if service.demo else "bandwatch.db"
        self.store = store or stmod.Store(os.path.join(service.data_dir, db_name), mode=mode, tzinfo=self.tz)
        self.store.tz = self.tz
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.thread = None
        self.aux_thread = None
        self.emit_lock = threading.Lock()  # pre-clock buffer + emit ordering
        self.wan_lock = threading.Lock()  # WAN sample vs. flush checkpoint snapshot
        self.flush_lock = threading.Lock()
        self.heartbeat = {"wan": None, "providers": None}
        self.seq = 0
        self.started_wall = time.time()
        self.started_mono = platform.monotonic()
        self.pending_events = []
        self.events_lock = threading.Lock()
        self.recent_events = collections.deque(maxlen=50)
        self.clock_trusted = False
        self.prebuffer = {}
        self.wall_offset = None
        self.flush_due = 0.0
        self.quota_alerted = None
        self.direction_check = collections.deque(maxlen=120)
        self.loop_errors = 0
        self._build_collectors()
        self.live_window = max(120, self.settings["ui.live_window"])
        self.wan_live = LiveSeries(int(900 / max(1, self.settings["wan.sample_interval"])) + 10)
        self.aria2_live = LiveSeries(450)
        self.dev_snaps = {}
        self.dev_live_ms = None
        if self.store.meta("first_start") is None:
            self.store.set_meta("first_start", int(time.time()))
        self.offload_notes = extras.offload_notes(platform)
        self.recovery = None

    # -- construction -------------------------------------------------------

    def _build_collectors(self):
        self.wan = wan.WanCollector(self.p, self.settings)
        self.devices = perdevice.make_provider(self.p, self.settings)
        bridge = "br-" + self.service.lan_interface if not self.service.lan_interface.startswith("br-") else self.service.lan_interface
        self.identity = identity.IdentityCollector(self.p, self.settings, bridge)
        self.aria2 = aria2mod.Aria2Collector(self.p, self.settings, self.secrets)
        self.system = identity.SystemCollector(self.p, self.service.data_dir)
        self.vnstat = extras.VnstatReader(self.p, self.settings)

    # -- clock handling and sample emission --------------------------------------

    def _uptime(self):
        txt = self.p.read_text("/proc/uptime")
        try:
            return float(txt.split()[0])
        except (AttributeError, ValueError, IndexError):
            return None

    def _check_clock(self, now):
        wall = self.p.time()
        offset = wall - now
        if self.wall_offset is not None and abs(offset - self.wall_offset) > 120:
            self._event("warning", "clock", "clock_step", "System clock changed by %+d s; samples near the change may be attributed to the wrong time" % (offset - self.wall_offset))
            if not self.clock_trusted:
                self.clock_trusted = wall >= MIN_VALID_WALL
        self.wall_offset = offset
        if not self.clock_trusted:
            up = self._uptime()
            if wall >= MIN_VALID_WALL and (up is None or up >= CLOCK_TRUST_UPTIME or self.service.demo):
                self.clock_trusted = True
            if self.clock_trusted:
                # Samples gathered before the clock was trusted carry
                # monotonic timestamps; convert them with the current offset.
                with self.emit_lock:
                    if self.prebuffer:
                        self.store.merge_pending(self.prebuffer, shift=offset)
                    self.prebuffer = {}
        return wall

    def _emit(self, series, key, mono_end, dt, rx, tx, rate_ok=True, est=False, count=True):
        with self.emit_lock:
            if self.clock_trusted:
                self.store.add(series, key, mono_end + self.wall_offset, dt, rx, tx, rate_ok, est, count)
            elif len(self.prebuffer) < 20000:
                stmod.accumulate(self.prebuffer, series, key, mono_end, dt, rx, tx, rate_ok, est, count)

    def _event(self, level, source, kind, message):
        item = (int(time.time()), level, source, kind, message)
        with self.events_lock:
            self.pending_events.append(item)
            self.recent_events.appendleft(item)
        (log.warning if level in ("warning", "error") else log.info)("%s: %s", source, message)

    def _drain(self, collector, source):
        while collector.events:
            level, kind, message = collector.events.pop(0)
            self._event(level, source, kind, message)

    # -- restart recovery -----------------------------------------------------------

    def _load_checkpoints(self):
        saved = {}
        for dev in list(self.wan.ifaces):
            st = self.store.get_state("wan:" + dev)
            if st:
                saved[dev] = st
        return saved

    def _recover_gap(self, saved, now_mono, wall):
        """Attribute traffic that happened while BandWatch was not running.

        Only possible when the router did not reboot and the interface was
        not re-created: then the kernel counters still include that traffic.
        The bytes are exact, their timing is spread evenly over the gap and
        flagged as estimated.
        """
        rec_rx = rec_tx = 0
        details = []
        for dev, st in saved.items():
            itf = self.wan.ifaces.get(dev)
            if not itf or itf.rx.value is None:
                continue
            gap = wall - float(st.get("wall", 0))
            if st.get("boot") != self.wan.boot_id or st.get("ifindex") != itf.ifindex:
                details.append("%s: counters restarted (reboot or reconnect); downtime not recoverable" % dev)
                continue
            if not 0 < gap <= GAP_RECOVERY_MAX:
                continue
            drx, dtx = itf.rx.value - int(st["rx"]), itf.tx.value - int(st["tx"])
            limit = self.settings["wan.max_rate_mbps"] * 125_000 * gap * 1.25
            if drx < 0 or dtx < 0 or drx > limit or dtx > limit:
                continue
            mono_end = now_mono
            self._emit("wan", dev, mono_end, gap, drx, dtx, rate_ok=False, est=True, count=False)
            rec_rx += drx
            rec_tx += dtx
            details.append("%s: recovered %d bytes over %d s" % (dev, drx + dtx, gap))
        if rec_rx or rec_tx:
            self._emit("wan", "*", now_mono, max(1.0, wall - min(float(s.get("wall", wall)) for s in saved.values())), rec_rx, rec_tx, rate_ok=False, est=True, count=False)
            self._event("info", "wan", "gap_recovered", "Recovered WAN traffic during BandWatch downtime (timing estimated): " + "; ".join(details))
        elif details:
            self._event("info", "wan", "gap_lost", "; ".join(details))
        self.recovery = details

    # -- main loop ---------------------------------------------------------------

    def start(self):
        """Start two collector threads.

        * ``wan``: WAN counters and router health only (cheap file reads);
          never blocked by slow providers or database writes.
        * ``providers``: Bandix/nlbwmon, device identification (ubus), aria2
          and database flushes, which may wait on network/subprocess
          timeouts or slow flash.
        """
        self.thread = threading.Thread(target=self._run_wan, name="wan", daemon=True)
        self.aux_thread = threading.Thread(target=self._run_providers, name="providers", daemon=True)
        self.thread.start()
        self.aux_thread.start()

    def stop(self, timeout=10.0):
        self.stop_event.set()
        for t in (self.thread, self.aux_thread):
            if t:
                t.join(timeout)
        try:
            self.flush()
        except Exception as exc:
            log.error("final flush failed: %s", exc)
        self.store.close()

    def _loop_error(self, where, exc):
        self.loop_errors += 1
        log.error("%s loop error: %s: %s", where, type(exc).__name__, exc)
        if self.loop_errors < 20:
            import traceback

            traceback.print_exc()
        self.stop_event.wait(1.0)

    def _run_wan(self):
        s = self.settings
        start = self.p.monotonic()
        due = {"wan": start, "sys": start + 0.2}
        saved = None
        first = True
        while not self.stop_event.is_set():
            now = self.p.monotonic()
            try:
                wall = self._check_clock(now)
                if now >= due["wan"]:
                    with self.wan_lock:
                        if saved is None:
                            self.wan.detect(now)
                            saved = self._load_checkpoints()
                        rx, tx = self.wan.sample(now, self._emit)
                        if first and saved:
                            self._recover_gap(saved, now, wall)
                        first = False
                    self._drain(self.wan, "wan")
                    with self.cond:
                        self.wan_live.push(wall, rx, tx)
                        self.seq += 1
                        self.cond.notify_all()
                    self.heartbeat["wan"] = now
                    due["wan"] = max(due["wan"] + s["wan.sample_interval"], now + 0.2)
                if now >= due["sys"]:
                    self.system.poll(now)
                    self._check_quota()
                    due["sys"] = now + 5.0
            except Exception as exc:
                self._loop_error("wan", exc)
            delay = min(due.values()) - self.p.monotonic()
            self.stop_event.wait(max(0.05, min(delay, 1.0)))

    def _run_providers(self):
        s = self.settings
        start = self.p.monotonic()
        due = {"dev": start + 0.5, "ident": start + 1.0, "aria2": start + 1.5}
        self.flush_due = start + s["storage.flush_interval"]
        while not self.stop_event.is_set():
            now = self.p.monotonic()
            try:
                wall = self.p.time()
                if now >= due["dev"]:
                    snaps = self.devices.poll(now, self._emit)
                    if snaps is not None:
                        self.dev_snaps = snaps
                        self.dev_live_ms = int(wall * 1000)
                        self._direction_sample(snaps)
                    elif self.devices.status.state not in ("ok", "degraded"):
                        self.dev_snaps = {}
                    self._drain(self.devices, "devices")
                    due["dev"] = max(due["dev"] + s["devices.poll_interval"], self.p.monotonic() + 0.5)
                if now >= due["ident"]:
                    self.identity.poll(now, self.dev_snaps)
                    due["ident"] = self.p.monotonic() + 15.0
                if now >= due["aria2"]:
                    live = self.aria2.poll(now, wall, self._emit)
                    self._drain(self.aria2, "aria2")
                    if live is not None:
                        self.aria2_live.push(wall, live["down"], live["up"])
                    due["aria2"] = self.p.monotonic() + (s["aria2.poll_interval"] if live is not None else 15.0)
                if now >= self.flush_due:
                    self.flush()
                    self.flush_due = self.p.monotonic() + s["storage.flush_interval"]
                self.heartbeat["providers"] = now
            except Exception as exc:
                self._loop_error("providers", exc)
            delay = min(*due.values(), self.flush_due) - self.p.monotonic()
            self.stop_event.wait(max(0.05, min(delay, 1.0)))

    def flush(self):
        wall = time.time()
        devs = []
        for mac, d in self.identity.devices.items():
            devs.append({"mac": mac, "hostname": d.hostname or d.static_name, "ip4": d.ip4, "ip6": d.ip6, "conn": d.conn, "iface": d.iface, "first_seen": int(wall), "last_seen": int(wall) if self._presence(mac) == "online" else None})
        try:
            self.store.upsert_devices(devs)
            self.store.upsert_tasks(self.aria2.dirty_tasks())
            with self.events_lock:
                events, self.pending_events = self.pending_events, []
            self.store.add_events(events)
        except Exception as exc:
            log.warning("metadata flush failed: %s", exc)
        s = self.settings
        with self.flush_lock:
            # Snapshot WAN counters together with the samples they produced,
            # so a checkpoint never refers to data that is not yet persisted.
            with self.wan_lock:
                checkpoint = self.wan.checkpoint(wall) if self.clock_trusted else None
                self.store.begin_flush()
            ok = self.store.complete_flush(
                wall,
                retention={
                    "minute_days": s["storage.minute_retention_days"],
                    "quarter_days": s["storage.quarter_retention_days"],
                    "device_quarter_days": s["storage.device_quarter_retention_days"],
                    "day_days": s["storage.day_retention_days"],
                },
            )
            if ok and checkpoint is not None:
                # Written only after the matching samples are persisted, so
                # restart recovery never double counts.
                try:
                    self.store.set_states(checkpoint)
                except Exception as exc:
                    log.warning("checkpoint failed: %s", exc)
        return ok

    # -- cross checks -----------------------------------------------------------

    def _direction_sample(self, snaps):
        last = self.wan_live.last()
        if not last or last[1] is None:
            return
        down = sum(sn.down or 0 for sn in snaps.values())
        up = sum(sn.up or 0 for sn in snaps.values())
        if max(last[1], last[2]) > 250_000:  # only meaningful with real traffic
            self.direction_check.append((last[1], last[2], down, up))

    def direction_verdict(self):
        """Compare device download/upload sums with WAN rx/tx."""
        if len(self.direction_check) < 20:
            return {"state": "insufficient_data"}
        same = swapped = 0.0
        for wrx, wtx, down, up in self.direction_check:
            same += abs(wrx - down) + abs(wtx - up)
            swapped += abs(wrx - up) + abs(wtx - down)
        if swapped < same * 0.5:
            return {"state": "suspect_swapped", "message": "Per-device download/upload appear inverted relative to WAN counters. Consider changing 'Bandix direction' in Settings."}
        return {"state": "consistent"}

    # -- quota ---------------------------------------------------------------------

    def quota_status(self, now=None):
        s = self.settings
        if not s["quota.enabled"] or not s["quota.monthly_gb"]:
            return {"enabled": False}
        now = now or time.time()
        a, b, da, db = tzmod.period_bounds("this_month", now, self.tz, month_start_day=s["quota.reset_day"])
        bk = self.store.summary("wan", "*", a, b)
        used = bk[stmod.RX] + bk[stmod.TX]
        limit = s["quota.monthly_gb"] * 1_000_000_000
        elapsed = max(1.0, now - a)
        projected = used / elapsed * (b - a)
        return {
            "enabled": True,
            "limit": limit,
            "used": used,
            "percent": round(100.0 * used / limit, 1),
            "warn_percent": s["quota.warn_percent"],
            "period_start": da.isoformat(),
            "period_end": (db - analytics.ONE_DAY).isoformat(),
            "projected": int(projected),
            "state": "exceeded" if used >= limit else ("warning" if used >= limit * s["quota.warn_percent"] / 100 else "ok"),
        }

    def _check_quota(self):
        q = self.quota_status()
        if not q.get("enabled") or q["state"] == "ok":
            return
        marker = (q["period_start"], q["state"])
        if self.quota_alerted != marker:
            self.quota_alerted = marker
            self._event("warning", "quota", "quota_" + q["state"], "Monthly data quota %s: %.1f%% used" % ("exceeded" if q["state"] == "exceeded" else "warning", q["percent"]))

    # -- settings changes -------------------------------------------------------------

    def update_settings(self, changes):
        old = self.settings.as_dict()
        with self.lock:
            errors = self.settings.update(changes)
            if errors:
                return errors
            new = self.settings.as_dict()
            if new["general.timezone"] != old["general.timezone"]:
                self.tz, self.tz_desc = tzmod.resolve(new["general.timezone"])
                self.store.rebuild_days(self.tz)
                self._event("info", "settings", "timezone", "Timezone changed to %s; daily totals rebuilt from 15-minute data" % self.tz_label)
            if new["wan.interfaces"] != old["wan.interfaces"] or new["wan.max_rate_mbps"] != old["wan.max_rate_mbps"]:
                self.wan.next_detect = 0.0
            if new["devices.provider"] != old["devices.provider"] or new["devices.bandix_url"] != old["devices.bandix_url"] or new["devices.bandix_direction"] != old["devices.bandix_direction"]:
                self.devices = perdevice.make_provider(self.p, self.settings)
                self.dev_snaps = {}
                self.direction_check.clear()
            if new["aria2.url"] != old["aria2.url"] and self.secrets.has("aria2_secret"):
                # The secret belongs to the old endpoint: never send it elsewhere.
                self.secrets.set("aria2_secret", "")
                self._event("info", "settings", "aria2_secret_cleared", "aria2 endpoint changed; the stored RPC secret was cleared and must be entered again")
            if new["aria2.mode"] != old["aria2.mode"] or new["aria2.url"] != old["aria2.url"]:
                self.aria2 = aria2mod.Aria2Collector(self.p, self.settings, self.secrets)
            if new["vnstat.mode"] != old["vnstat.mode"]:
                self.vnstat = extras.VnstatReader(self.p, self.settings)
        return {}

    def set_aria2_secret(self, secret):
        self.secrets.set("aria2_secret", secret)
        with self.lock:
            self.aria2 = aria2mod.Aria2Collector(self.p, self.settings, self.secrets)

    # -- read API used by the web layer -----------------------------------------------

    def now(self):
        return time.time()

    @property
    def tz_label(self):
        return tzmod.label(self.tz, self.tz_desc, time.time(), self.settings["general.timezone"])

    @property
    def data_dir_label(self):
        """Data directory as shown to users (demo paths stay private)."""
        return "(demo data directory)" if self.service.demo else self.service.data_dir

    def first_ts(self):
        first = self.store.first_data_time()
        fs = self.store.meta("first_start")
        cands = [x for x in (first, int(fs) if fs else None) if x is not None]
        return min(cands) if cands else None

    def _presence(self, mac):
        d = self.identity.devices.get(mac)
        now_ms = int(time.time() * 1000)
        snap = self.dev_snaps.get(mac)
        if snap and (snap.down or snap.up):
            return "online"
        return self.identity.presence(d, now_ms, self.settings["devices.online_window"])

    def live(self, since_ms=0):
        with self.lock:
            wan_points = self.wan_live.since(since_ms)
            last = self.wan_live.last()
            aria = self.aria2.live
            devs = []
            for mac, sn in self.dev_snaps.items():
                if sn.down is None and sn.up is None:
                    continue
                devs.append({"mac": mac, "down": sn.down, "up": sn.up})
            devs.sort(key=lambda d: -((d["down"] or 0) + (d["up"] or 0)))
            return {
                "seq": self.seq,
                "now": int(time.time() * 1000),
                "interval": self.settings["wan.sample_interval"],
                "wan": {"points": wan_points, "down": last[1] if last else None, "up": last[2] if last else None, "state": self.wan.status.state},
                "aria2": None if aria is None else {"down": aria["down"], "up": aria["up"], "active": aria["active"], "waiting": aria["waiting"]},
                "aria2_points": self.aria2_live.since(since_ms) if aria is not None else [],
                "devices": devs[:12],
                "devices_state": self.devices.status.state,
                "demo": self.service.demo,
            }

    def wait_for_update(self, seq, timeout):
        with self.cond:
            if self.seq == seq:
                self.cond.wait(timeout)
            return self.seq

    def collectors_status(self):
        items = [self.wan.status, self.devices.status, self.identity.status, self.aria2.status, self.vnstat.status, self.system.status]
        out = [c.as_dict() for c in items]
        for c in out:
            for k in ("last_ok", "last_error"):
                if c.get(k):
                    c[k] = int(c[k])
        return out

    def device_name(self, mac, stored):
        d = self.identity.devices.get(mac)
        rec = stored.get(mac) or {}
        return rec.get("alias") or (d.static_name if d else None) or (d.hostname if d else None) or rec.get("hostname")

    def device_table(self, a, b):
        now = time.time()
        stored = self.store.devices()
        totals = self.store.per_key("dev", a, b)
        local = self.store.per_key("devl", a, b)
        totals.pop("*", None)
        local.pop("*", None)
        macs = set(totals) | set(local) | set(self.identity.devices) | set(self.dev_snaps)
        if stored:
            macs |= {m for m, r in stored.items() if r.get("alias")}
        rows = []
        for mac in macs:
            d = self.identity.devices.get(mac)
            rec = stored.get(mac) or {}
            snap = self.dev_snaps.get(mac)
            t = totals.get(mac)
            loc = local.get(mac)
            rows.append(
                {
                    "mac": mac,
                    "name": self.device_name(mac, stored),
                    "alias": rec.get("alias"),
                    "hostname": (d.hostname if d else None) or rec.get("hostname"),
                    "ip4": (d.ip4 if d else None) or (snap.ip4 if snap else None) or rec.get("ip4"),
                    "ip6": (d.ip6 if d else None) or rec.get("ip6"),
                    "conn": (d.conn if d else None) or rec.get("conn") or "unknown",
                    "band": d.band if d else None,
                    "iface": (d.iface if d else None) or rec.get("iface"),
                    "status": self._presence(mac),
                    "randomized_mac": identity.is_randomized(mac),
                    "down_rate": snap.down if snap else None,
                    "up_rate": snap.up if snap else None,
                    "rx": t[stmod.RX] if t else 0,
                    "tx": t[stmod.TX] if t else 0,
                    "lan_rx": loc[stmod.RX] if loc else 0,
                    "lan_tx": loc[stmod.TX] if loc else 0,
                    "peak_rx": t[stmod.PRX] if t else None,
                    "peak_tx": t[stmod.PTX] if t else None,
                    "last_seen": rec.get("last_seen"),
                    "first_seen": rec.get("first_seen"),
                }
            )
        rows.sort(key=lambda r: -(r["rx"] + r["tx"]))
        cov = analytics.summary(self.store, "dev", "*", a, b, now, self.first_ts())
        return rows, cov

    def health(self):
        sysinfo = dict(self.system.info)
        if self.service.demo:
            # Simulated router: hide host paths of the demo data directory.
            sysinfo["storage"] = [dict(x, path=x["path"] if x["path"] in ("/overlay", "/", "/tmp") else "(demo)") for x in sysinfo.get("storage", [])]
        db = self.store.status()
        db["path"] = os.path.basename(db["path"])
        return {
            "version": __version__,
            "demo": self.service.demo,
            "uptime_service": int(time.time() - self.started_wall),
            "system": sysinfo,
            "collectors": self.collectors_status(),
            "database": db,
            "timezone": self.tz_label,
            "timezone_spec": self.tz_desc,
            "clock_trusted": self.clock_trusted,
            "accounting_notes": self.offload_notes + (["Bandix direction check: " + self.direction_verdict().get("message", "")] if self.direction_verdict()["state"] == "suspect_swapped" else []),
            "events": [dict(zip(("t", "level", "source", "kind", "message"), e)) for e in list(self.recent_events)[:20]],
            "loop_errors": self.loop_errors,
            "flush_interval": self.settings["storage.flush_interval"],
            "data_dir": self.data_dir_label,
        }

    def diagnostics(self):
        """Sanitised diagnostic snapshot: no MACs, IPs, hostnames or secrets."""
        h = self.health()
        board = h["system"].get("board", {})
        colls = []
        for c in h["collectors"]:
            c = dict(c)
            c.pop("interfaces", None)
            colls.append(c)
        wan_ifaces = [{"device": i.get("device"), "proto": i.get("proto"), "families": i.get("families"), "present": i.get("present"), "tunnel": i.get("tunnel")} for i in self.wan.status.extra.get("interfaces", [])]
        return {
            "generated": int(time.time()),
            "bandwatch_version": __version__,
            "mode": "demo" if self.service.demo else "production",
            "board": board,
            "python": _python_info(),
            "settings": {k: v for k, v in self.settings.as_dict().items() if not k.startswith("devices.bandix_url") and not k.startswith("aria2.url")},
            "aria2_secret_configured": self.secrets.has("aria2_secret"),
            "collectors": colls,
            "wan_detection": {"source": self.wan.source, "interfaces": wan_ifaces, "count": len(self.wan.ifaces)},
            "device_counts": {"identified": len(self.identity.devices), "provider_reported": len(self.dev_snaps)},
            "direction_check": self.direction_verdict(),
            "database": {k: v for k, v in h["database"].items() if k != "path"},
            "system": {k: v for k, v in h["system"].items() if k not in ("board",)},
            "accounting_notes": h["accounting_notes"],
            "clock_trusted": self.clock_trusted,
            "loop_errors": self.loop_errors,
            "timezone": self.tz_label,
            "recent_event_kinds": [e["kind"] for e in h["events"]],
        }


def _python_info():
    import platform as pyplat
    import sys

    mods = {}
    for name in ("sqlite3", "ssl", "zoneinfo", "_hashlib"):
        try:
            __import__(name)
            mods[name] = True
        except ImportError:
            mods[name] = False
    try:
        import sqlite3

        sqlite_version = sqlite3.sqlite_version
    except ImportError:
        sqlite_version = None
    return {"version": sys.version.split()[0], "implementation": pyplat.python_implementation(), "modules": mods, "sqlite": sqlite_version}


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), default=str)
