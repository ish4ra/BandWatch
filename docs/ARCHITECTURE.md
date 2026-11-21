# BandWatch architecture

## Overview

```
 ┌──────────────── router (OpenWrt) ───────────────────────────────────────┐
 │                                                                         │
 │  procd ──► python3 -m bandwatch run   (user: bandwatch, one process)    │
 │             │                                                           │
 │             ├─ collector thread (single scheduler)                      │
 │             │    ├─ WanCollector ─────── /proc/net/dev, sysfs, ubus,    │
 │             │    │                       /proc/net/route, ipv6_route    │
 │             │    ├─ per-device provider ─ Bandix HTTP (read-only)       │
 │             │    │                       or `nlbw -c json`              │
 │             │    ├─ IdentityCollector ── DHCP leases, ARP, bridge FDB,  │
 │             │    │                       hostapd stations (ubus)        │
 │             │    ├─ Aria2Collector ───── aria2 JSON-RPC (read-only)     │
 │             │    ├─ SystemCollector ──── /proc, sysfs, statvfs          │
 │             │    └─ VnstatReader ─────── `vnstat --json` (on demand)    │
 │             │                                                           │
 │             ├─ Store: in-memory minute buckets ─► SQLite (batched)      │
 │             │                                                           │
 │             └─ HTTP threads: static UI + JSON API + SSE live stream     │
 │                                                                         │
 │  LuCI ─► rpcd plugin luci.bandwatch ─► one-time SSO token file          │
 └─────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
| --- | --- |
| `bandwatch/platform.py` | All OS access (files, subprocess without shell, local HTTP); parsers for procfs/sysfs formats |
| `bandwatch/counters.py` | Integer counter deltas, reset/rebaseline/glitch classification, monotonic-time rates |
| `bandwatch/collectors/*` | Providers; each owns a `Status` (ok / degraded / unavailable / error / disabled) |
| `bandwatch/store.py` | SQLite schema, migrations, recovery, tiered buckets, retention |
| `bandwatch/analytics.py` | Period summaries, coverage, chart buckets |
| `bandwatch/tz.py` | Timezones incl. POSIX TZ strings (OpenWrt often lacks tzdata), calendar periods |
| `bandwatch/engine.py` | Scheduler, clock safety, live ring buffers, quota, settings changes, diagnostics |
| `bandwatch/api.py` | Transport-independent API handlers and validation |
| `bandwatch/server.py` | HTTP server, security controls, static files, SSE |
| `bandwatch/auth.py` | Password hashing, sessions, CSRF, rate limiting, LuCI SSO |
| `bandwatch/demo.py` | Simulated router platform and synthetic history |
| `bandwatch/web/` | Vanilla JS dashboard, canvas charts, CSS, PWA files |

Design choices:

* **Python standard library only.** No pip dependencies; runs with
  `python3-light`, `python3-sqlite3` and `python3-urllib`. The `logging`
  package is avoided to save a dependency.
* **One process, one collector thread.** Samples are taken once and shared
  by all browser sessions (SSE fan-out); web requests never trigger
  collection, except the on-demand vnStat comparison.
* **Subprocesses only at slow intervals** (ubus every 30 s, `nlbw`, vnStat on
  demand), with fixed argument lists, no shell and timeouts.
* **Unprivileged service user.** A ubus ACL (`/usr/share/acl.d/bandwatch.json`)
  grants read access to `network.interface` and `hostapd.*:get_clients` only.

## Data providers

### WAN (interface counters)

* Source: `/proc/net/dev` (64-bit counters) every `wan.sample_interval`
  seconds (default 2 s).
* Detection: explicit override → ubus `network.interface dump` (interfaces
  that are up with a default route; their `l3_device`) → `/proc/net/route` +
  `/proc/net/ipv6_route`. Re-detected every 30 s and immediately when a
  device disappears.
* No double counting: each L3 device is counted once (IPv4 + IPv6 share
  it); tunnel types (ARPHRD 768/769/776/778/823/65534 such as 6in4, DS-Lite,
  CLAT, WireGuard) are ignored when a non-tunnel WAN exists; PPPoE is
  measured on `pppoe-*`, never together with its Ethernet port.
* Identity = (ifindex, boot id). A changed identity or decreasing counter is
  a reset: the new value is counted only if physically plausible for the
  elapsed time (`wan.max_rate_mbps`), otherwise the counter is re-baselined
  and the interval is unmeasured.
* WAN down (no default route / device gone) is recorded as measured zero.
* Restart recovery: counter checkpoints are written after every successful
  flush. If BandWatch restarts without a reboot or reconnect, the traffic of
  the downtime is recovered from the kernel counters, spread evenly over the
  gap and flagged as *estimated timing*.

### Per-device: Bandix (preferred)

Verified against the upstream Bandix README for v0.12.x (source code was not
available for review; re-validate on hardware):

* `GET /api/system/info` → `data.version`
* `GET /api/traffic/devices` → `data.d[]` with `mac`, `ip4`, `ip6[]`, `host`,
  `conn`, `uplink`, `w_freq`, cumulative bytes `w_rx_b`/`w_tx_b` (WAN),
  `l_rx_b`/`l_tx_b` (LAN), rates `*_r` (bytes/s), `last` (ms).

BandWatch never calls Bandix write endpoints (limits, quotas, bindings,
deletes). Bandix does not document the rx/tx direction; BandWatch assumes
rx = device download, offers `devices.bandix_direction`, and cross-checks
the sum of device rates against WAN counters (shown in Health when the
direction looks inverted).

### Per-device: nlbwmon (fallback)

`nlbw -c json -g mac` every poll; totals reset at each accounting period
(handled as counter resets). Conntrack based: offloaded flows may be
under-counted. Requires permission to query nlbwmon (often root only).

### aria2 (optional)

Read-only `system.multicall` of `getVersion`, `getSessionInfo`,
`getGlobalStat`, `tellActive`, `tellWaiting`, `tellStopped`. Transfer
history = per-task `completedLength`/`uploadLength` deltas; the first
observation of a task is a baseline (pre-existing data is never counted); a
new `sessionId` (aria2 restart) re-baselines; jumps larger than physically
possible (integrity checks) are discarded. Progress made during a short RPC
outage is counted once, without peak statistics. aria2 figures are payload
estimates and are never added to WAN totals.

### Identity and presence

DHCP leases, static hosts from `/etc/config/dhcp`, `/proc/net/arp`
(complete entries on LAN devices only), the bridge FDB (`brforward`, which
also gives the port and seconds since the MAC was last seen), hostapd
station lists and Bandix metadata. Presence: associated Wi-Fi station or
FDB age ≤ 3 min → *online*; recently seen → *idle*; otherwise *offline*;
no evidence → *unknown*. No network scanning is performed.

## Analytics

* Integer bytes everywhere; rates are bytes / monotonic seconds.
* **Coverage** = measured time / elapsed time of the period since data
  collection began. Unmeasured time is shown as a gap.
* **Average** = bytes / measured time. For devices the measured time is that
  of the per-device provider (dense `dev/*` aggregate), so idle time counts.
* **Peak / minimum** = highest / lowest per-sample rate. Samples spanning
  long gaps are excluded from peak/min. Device minimum is "minimum while
  active".
* Internet (WAN), per-device Internet (`dev`), per-device LAN-local
  (`devl`) and aria2 payload are separate series and are never summed.
  "Router & unattributed" = WAN − devices, only shown when both are ≥ 90 %
  covered.
* Calendar periods use the configured timezone (router timezone by default,
  including POSIX TZ rules with DST). Weeks start on Monday (configurable);
  the quota month can start on any day 1–28.

## Storage

| Tier | Resolution | Default retention |
| --- | --- | --- |
| `m1` | 1 minute (UTC aligned) | 7 days |
| `m15` | 15 minutes (UTC aligned; every real UTC offset is a multiple of 15 min) | 400 days (devices: 120 days) |
| `d1` | local calendar day | 5 years |

Queries use the finest tier available; days older than 15-minute retention
come from `d1`; unaligned range edges come from `m1`; unflushed samples are
merged from memory. Every bucket stores bytes, measured milliseconds, peak
and minimum rates, sample count and estimated bytes.

Writes are batched every `storage.flush_interval` (default 5 min) in one
transaction (WAL journal where supported). **Up to one flush interval of
samples can be lost on sudden power loss.** Roll-ups, retention and bounded
event/task tables keep the database small (typically a few MB). A damaged
database is moved aside (max. two copies kept) and recreated. Demo and
production databases carry a `mode` marker and refuse to be mixed.

## Clock safety

Routers often boot with a wrong clock until NTP syncs. Samples are buffered
with monotonic timestamps until the clock is trusted (year ≥ 2025 and
uptime ≥ 10 min, or a clock step was observed) and then placed with the
corrected offset. Large clock steps are logged as events.

## Security model

See [SECURITY.md](../SECURITY.md).
