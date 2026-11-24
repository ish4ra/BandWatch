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

from . import __version__, analytics, log, selfmon
from . import config as cfgmod
from . import store as stmod
from . import tz as tzmod
from .collectors import aria2 as aria2mod
from .collectors import extras, identity, perdevice, wan

MIN_VALID_WALL = 1748736000  # 2025-06-01: earlier clocks are certainly unsynchronised
CLOCK_TRUST_UPTIME = 600.0
GAP_RECOVERY_MAX = 7 * 86400
# The first save after start-up (once the clock is trusted) happens sooner
# than the regular interval, so persistence is confirmed within minutes of a
# restart. One extra small write per service start; later saves keep the
# configured interval.
FIRST_FLUSH_DELAY = 90.0
# Samples buffered before the clock was trusted are kept across a service
# restart within the same boot (monotonic timestamps stay valid).
MAX_SAVED_PREBUFFER = 6000
LIFECYCLE_KEEP = 20


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
        self.direction_skipped = {"low_traffic": 0, "router_traffic": 0, "unattributed": 0, "symmetric": 0}
        self.loop_errors = 0
        self._build_collectors()
        self.live_window = max(120, self.settings["ui.live_window"])
        self.wan_live = LiveSeries(int(900 / max(1, self.settings["wan.sample_interval"])) + 10)
        self.aria2_live = LiveSeries(450)
        self.dev_snaps = {}
        self.dev_live_ms = None
        self.boot_id = platform.boot_id()
        self.clock_trusted_at = None
        self.trusted_at_mono = None
        self.early_flush_done = False
        self.sample_stats = {"samples": 0, "late": 0, "missed": 0, "max_late_ms": 0}
        self.bind_addresses = []  # set by the service launcher (server.resolve_listen)
        self.wan_bytes_total = 0
        self.monitor = selfmon.SelfMonitor(self.store)
        self.lifecycle = self._record_start()
        self._load_prebuffer()
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
        stepped = False
        if self.wall_offset is not None and abs(offset - self.wall_offset) > 120:
            self._event("warning", "clock", "clock_step", "System clock changed by %+d s; samples near the change may be attributed to the wrong time" % (offset - self.wall_offset))
            stepped = True
        self.wall_offset = offset
        if not self.clock_trusted:
            up = self._uptime()
            # A step while untrusted is the time synchronisation after boot:
            # trust the new clock and convert the buffered samples with it.
            # (Before 0.1.0-beta.2 this path set the flag without merging,
            # leaving the buffered samples stranded until they were lost.)
            if wall >= MIN_VALID_WALL and (stepped or up is None or up >= CLOCK_TRUST_UPTIME or self.service.demo):
                self.clock_trusted = True
            if self.clock_trusted:
                self.clock_trusted_at = wall
                self.trusted_at_mono = now
                # Samples gathered before the clock was trusted carry
                # monotonic timestamps; convert them with the current offset.
                with self.emit_lock:
                    if self.prebuffer:
                        self.store.merge_pending(self.prebuffer, shift=offset)
                    self.prebuffer = {}
        return wall

    def _emit(self, series, key, mono_end, dt, rx, tx, rate_ok=True, est=False, count=True):
        with self.emit_lock:
            if series == "wan" and key == "*" and not est:
                self.wan_bytes_total += int(rx) + int(tx)
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

    # -- service lifecycle (restart / reboot detection) ------------------------------

    def _record_start(self):
        """Classify this start and remember it (one small metadata write).

        * ``first_start``: no earlier BandWatch run on this database.
        * ``upgrade``: database from an older BandWatch that did not record
          its lifecycle (0.1.0-beta.1).
        * ``after_reboot``: the kernel boot id changed since the last run.
        * ``restart``: same boot (service restart, crash or package upgrade).

        ``previous_stop`` is ``clean`` when the last run reached
        :meth:`stop`, ``unclean`` when it did not (crash, kill, power loss).
        """
        st = self.store
        now = int(time.time())
        first_start = st.meta("first_start")
        prev_boot = st.meta("last_boot_id")
        running = st.meta("running")
        prev_version = st.meta("last_version")
        try:
            starts = int(st.meta("starts", "0"))
        except ValueError:
            starts = 0
        if starts == 0 and prev_boot is None:
            kind = "upgrade" if first_start is not None else "first_start"
            previous_stop = None
        else:
            kind = "after_reboot" if (self.boot_id and prev_boot != self.boot_id) else "restart"
            previous_stop = "unclean" if running == "1" else "clean"
        up = self._uptime()
        entry = {"t": now, "kind": kind, "previous_stop": previous_stop, "router_uptime": int(up) if up is not None else None, "version": __version__}
        if prev_version and prev_version != __version__:
            entry["upgraded_from"] = prev_version[:32]
        try:
            hist = json.loads(st.meta("lifecycle", "[]"))
            hist = [h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else []
        except ValueError:
            hist = []
        hist = (hist + [entry])[-LIFECYCLE_KEEP:]
        values = {"running": "1", "last_start": now, "starts": starts + 1, "lifecycle": json.dumps(hist, separators=(",", ":")), "last_version": __version__}
        if self.boot_id:
            values["last_boot_id"] = self.boot_id
        if first_start is None:
            values["first_start"] = now
        try:
            st.set_metas(values)
        except Exception as exc:  # read-only or failing storage: keep running
            log.warning("could not record service start: %s", exc)
        text = {
            "first_start": "BandWatch started for the first time",
            "upgrade": "BandWatch started after an upgrade from a version without lifecycle tracking",
            "after_reboot": "BandWatch started after a router reboot",
            "restart": "BandWatch service restarted",
        }[kind]
        if previous_stop == "unclean":
            text += "; the previous run did not shut down cleanly (crash, forced stop or power loss)"
        if entry.get("upgraded_from"):
            text += "; upgraded from %s" % entry["upgraded_from"]
        self._event("warning" if previous_stop == "unclean" else "info", "service", "service_" + kind, text)
        return {"current": entry, "history": hist, "starts": starts + 1, "first_start": int(first_start) if first_start else now}

    def _save_prebuffer(self, buf):
        items = [[s_, k, t, b] for (s_, k, t), b in list(buf.items())[:MAX_SAVED_PREBUFFER]]
        try:
            self.store.set_states({"prebuffer": {"boot": self.boot_id, "items": items}})
            log.info("kept %d sample buckets gathered before the clock was trusted", len(items))
        except Exception as exc:
            log.warning("could not keep buffered samples: %s", exc)

    def _load_prebuffer(self):
        st = self.store.get_state("prebuffer")
        if not st:
            return
        try:
            self.store.set_states({"prebuffer": None})
        except Exception:
            pass
        items = st.get("items") if isinstance(st, dict) else None
        if not isinstance(items, list) or not items:
            return
        if st.get("boot") and st.get("boot") == self.boot_id:
            for it in items:
                try:
                    s_, k, t, b = it
                    if isinstance(b, list) and len(b) == len(stmod.new_bucket()):
                        self.prebuffer[(str(s_), str(k), int(t))] = b
                except (TypeError, ValueError):
                    continue
            self._event("info", "store", "prebuffer_restored", "Restored %d sample buckets that were waiting for a trusted clock before the restart" % len(self.prebuffer))
        else:
            self._event("warning", "store", "prebuffer_lost", "%d sample buckets gathered before the clock was trusted could not be kept across the reboot" % len(items))

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
        self.monitor.finish()
        try:
            self.flush()
        except Exception as exc:
            log.error("final flush failed: %s", exc)
        with self.emit_lock:
            pre, self.prebuffer = (self.prebuffer, {}) if not self.clock_trusted else ({}, self.prebuffer)
        if pre:
            self._save_prebuffer(pre)
        try:
            self.store.set_metas({"running": "0", "last_stop": int(time.time())})
        except Exception as exc:
            log.warning("could not record clean shutdown: %s", exc)
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
                    self._sample_timing(now - due["wan"], s["wan.sample_interval"])
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
                    self.monitor.observe(wall, now, self.system.info, self.monitor_counters())
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
                if not self.early_flush_done and self.trusted_at_mono is not None:
                    self.early_flush_done = True
                    self.flush_due = min(self.flush_due, max(now, self.trusted_at_mono) + min(FIRST_FLUSH_DELAY, s["storage.flush_interval"]))
                if now >= self.flush_due:
                    self.flush()
                    self.flush_due = self.p.monotonic() + s["storage.flush_interval"]
                self.heartbeat["providers"] = now
            except Exception as exc:
                self._loop_error("providers", exc)
            delay = min(*due.values(), self.flush_due) - self.p.monotonic()
            self.stop_event.wait(max(0.05, min(delay, 1.0)))

    def monitor_counters(self):
        st = self.sample_stats
        coll = sum(c.error_count for c in (self.wan.status, self.devices.status, self.identity.status, self.aria2.status, self.vnstat.status, self.system.status))
        return {"wan_bytes": self.wan_bytes_total, "samples": st["samples"], "late": st["late"], "missed": st["missed"], "coll_err": coll, "loop_err": self.loop_errors}

    def _sample_timing(self, late, interval):
        st = self.sample_stats
        st["samples"] += 1
        if late > 1.0:
            st["late"] += 1
            st["max_late_ms"] = max(st["max_late_ms"], int(late * 1000))
        if late >= interval:
            st["missed"] += int(late // interval)

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
            health_rows = self.monitor.take_unsaved()
            if not self.store.add_health(health_rows):
                self.monitor.restore_unsaved(health_rows)
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

    DIRECTION_MIN_SAMPLES = 20

    def _direction_sample(self, snaps):
        """Collect WAN vs. per-device samples for the Bandix direction check.

        Only samples that can tell the directions apart are kept: real
        traffic, clearly one-directional, mostly explained by LAN devices
        (not by the router itself, e.g. aria2 or package downloads). BandWatch
        never generates traffic for this check and never changes the
        setting automatically.
        """
        last = self.wan_live.last()
        if not last or last[1] is None or last[2] is None:
            return
        wrx, wtx = last[1], last[2]
        down = sum(sn.down or 0 for sn in snaps.values())
        up = sum(sn.up or 0 for sn in snaps.values())
        skip = self.direction_skipped
        if max(wrx, wtx) <= 250_000:
            skip["low_traffic"] += 1
            return
        aria = self.aria2.live
        if aria and (aria.get("down") or 0) + (aria.get("up") or 0) > 0.2 * (wrx + wtx):
            skip["router_traffic"] += 1
            return
        if down + up < 0.3 * (wrx + wtx):
            skip["unattributed"] += 1
            return
        if max(wrx, wtx) < 3 * max(1.0, min(wrx, wtx)):
            skip["symmetric"] += 1
            return
        self.direction_check.append((wrx, wtx, down, up))

    def direction_verdict(self):
        """Compare device download/upload sums with WAN rx/tx."""
        n = len(self.direction_check)
        base = {"samples": n, "required": self.DIRECTION_MIN_SAMPLES, "configured": self.settings["devices.bandix_direction"], "skipped": dict(self.direction_skipped)}
        if self.devices.status.provider != "bandix":
            return dict(base, state="not_applicable", message="The direction check applies to Bandix only.")
        if n < self.DIRECTION_MIN_SAMPLES:
            return dict(
                base,
                state="insufficient_data",
                message=(
                    "Pending: %d of %d usable samples. A usable sample needs a few seconds of clearly one-directional Internet traffic "
                    "from a LAN device (for example a large download on a phone or PC). Traffic generated by the router itself "
                    "(aria2, updates) is ignored. BandWatch never creates traffic for this check." % (n, self.DIRECTION_MIN_SAMPLES)
                ),
            )
        same = swapped = 0.0
        for wrx, wtx, down, up in self.direction_check:
            same += abs(wrx - down) + abs(wtx - up)
            swapped += abs(wrx - up) + abs(wtx - down)
        if swapped < same * 0.5:
            return dict(base, state="suspect_swapped", message="Per-device download/upload appear inverted relative to WAN counters. Consider changing 'Bandix direction' in Settings.")
        if same < swapped * 0.5:
            return dict(base, state="consistent", message="Per-device directions agree with the WAN counters.")
        return dict(base, state="inconclusive", message="Samples do not clearly confirm either direction; collection continues.")

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
                self.direction_skipped = dict.fromkeys(self.direction_skipped, 0)
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

    def clock_wait(self):
        """Why the clock is not trusted yet (None when it is)."""
        if self.clock_trusted:
            return None
        if self.p.time() < MIN_VALID_WALL:
            return {"reason": "clock_not_set"}
        up = self._uptime()
        if up is not None and up < CLOCK_TRUST_UPTIME:
            return {"reason": "waiting_after_boot", "remaining_s": int(CLOCK_TRUST_UPTIME - up)}
        return {"reason": "checking"}

    def persistence_status(self, db=None):
        """Is history being saved? Distinguishes a normal empty start-up
        database from an actual persistence problem."""
        now = time.time()
        mono = self.p.monotonic()
        db = db or self.store.status()
        interval = self.settings["storage.flush_interval"]
        svc_up = max(0, int(now - self.started_wall))
        with self.emit_lock:
            pre = len(self.prebuffer)
        pending = db["pending_buckets"] + pre
        next_in = max(0, int(self.flush_due - mono)) if self.flush_due else None
        oldest = self.store.oldest_sample_time()
        last_flush = db["last_flush"]
        if db["last_flush_error"]:
            state = "failing"
            text = "The last attempt to save data failed (%s). Unsaved data is kept in memory and retried." % (db["last_flush_error_code"] or "error")
        elif not self.clock_trusted:
            state = "waiting_for_clock"
            text = "Samples are kept in memory until the router clock is trustworthy (about 10 minutes after boot or after time synchronisation). This is normal after a reboot."
        elif db["last_write"] is None and oldest is None:
            grace = interval + FIRST_FLUSH_DELAY + 120
            if pending == 0 and svc_up > 120:
                state = "no_samples"
                text = "No samples have been collected yet. Check the collector states below."
            elif svc_up <= grace or (next_in is not None and last_flush is None):
                state = "collecting"
                text = "An empty history is normal right after the first start: collected data is saved to storage for the first time %s." % ("in about %d s" % next_in if next_in is not None else "shortly")
            else:
                state = "overdue"
                text = "Data has been collected for %d s but nothing has been saved yet." % svc_up
        elif last_flush is not None and now - last_flush > 3 * interval + 120:
            state = "overdue"
            text = "The last save was %d s ago; saves are expected every %d s." % (now - last_flush, interval)
        elif db["last_write"] is None:
            state = "ok"
            text = "Earlier history is stored. Data collected since this start is saved for the first time %s." % ("in about %d s" % next_in if next_in is not None else "shortly")
        else:
            state = "ok"
            text = "History is being saved every %d s." % interval
        return {
            "state": state,
            "explanation": text,
            "service_started": int(self.started_wall),
            "service_uptime_s": svc_up,
            "router_uptime_s": self.system.info.get("uptime"),
            "database_opened_at": db["opened_at"],
            "database_open_ms": db["open_ms"],
            "flush_interval_s": interval,
            "first_flush_delay_s": int(min(FIRST_FLUSH_DELAY, interval)),
            "next_flush_in_s": next_in,
            "last_flush_attempt": int(last_flush) if last_flush else None,
            "last_successful_save": int(db["last_write"]) if db["last_write"] else None,
            "last_save_buckets": db["last_write_rows"],
            "flush_count": db["flush_count"],
            "flush_failures": db["flush_failures"],
            "last_flush_error_code": db["last_flush_error_code"],
            "pending_buckets": pending,
            "prebuffered_buckets": pre,
            "clock_trusted": self.clock_trusted,
            "clock_wait": self.clock_wait(),
            "oldest_saved_sample": int(oldest) if oldest is not None else None,
            "history_predates_this_start": bool(oldest is not None and oldest < self.started_wall - 60),
            "integrity_check": db["integrity"],
            "recovered_from_corruption": bool(db["recovered_from"]),
            "journal_mode": db["journal_mode"],
            "schema_version": db["schema_version"],
            "size_bytes": db.get("size_bytes"),
            "rows": {k[5:]: v for k, v in db.items() if k.startswith("rows_")},
        }

    def health(self):
        sysinfo = dict(self.system.info)
        if self.service.demo:
            # Simulated router: hide host paths of the demo data directory.
            sysinfo["storage"] = [dict(x, path=x["path"] if x["path"] in ("/overlay", "/", "/tmp") else "(demo)") for x in sysinfo.get("storage", [])]
        db = self.store.status()
        persistence = self.persistence_status(dict(db))
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
            "persistence": persistence,
            "resources": {name: self.monitor.aggregate(self.monitor.rows(time.time() - secs)) for name, secs in (("last_hour", 3600), ("last_24h", 86400))},
            "lifecycle": {"current": self.lifecycle["current"], "starts": self.lifecycle["starts"], "history": self.lifecycle["history"][-10:]},
            "sampling": dict(self.sample_stats),
        }

    def diagnostics(self):
        """Sanitised diagnostic report built from an explicit allow-list
        (see :mod:`bandwatch.report`)."""
        from . import report

        return report.build(self)


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
