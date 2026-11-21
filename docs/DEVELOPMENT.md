# Development

## Requirements

* Python 3.10+ (no third-party runtime packages)
* For tests: `pytest`; optional `ruff`, `bandit`, `shellcheck`
* For UI tests: Node.js 18+ and Playwright (Chromium)
* For packaging/container tests: Docker

## Run the demo

```sh
python3 -m bandwatch demo                          # http://127.0.0.1:8787/, no login
python3 -m bandwatch demo --auth password          # login with "bandwatch-demo"
python3 -m bandwatch demo --scenario no-bandix --days 0
python3 -m bandwatch demo --fast                   # flush to SQLite every 30 s
```

Demo data lives in a separate directory (default `$TMPDIR/bandwatch-demo`)
and a database marked `mode=demo`; the production service refuses to open
it, and the demo refuses to use `/etc/bandwatch`. Every page shows a
"simulated" banner.

| Scenario | What it simulates |
| --- | --- |
| `aw1000-cellular` (default) | 5G/4G WAN on `wwan0` (QMI), Bandix 0.12.12, aria2, vnStat, 10 clients |
| `fibre-ethernet` | Ethernet/fibre WAN on `wan` (DHCP) |
| `pppoe` | `pppoe-wan` over `wan` (underlay not counted) |
| `no-bandix` | WAN-only monitoring |
| `no-vnstat` | vnStat absent |
| `no-aria2` | aria2 absent |
| `reboot` | reboot every 5 min: counters and boot id reset |
| `reconnect` | cellular drop for 20 s every 3 min, new ifindex and counters |
| `counter-reset` | Bandix and aria2 restart every 4 min |
| `outage` | Bandix and aria2 unreachable 60 s every 4 min |
| `empty-history` | fresh install |
| `db-recovery` | damaged database detected and recreated at start |

## Tests

```sh
python3 -m pytest                       # ~106 backend tests (~80 s)
ruff check bandwatch scripts tests
bandit -c pyproject.toml -r bandwatch scripts
shellcheck -s sh --severity=warning scripts/*.sh scripts/pkg/* tests/openwrt/run.sh \
  openwrt/bandwatch/files/usr/bin/bandwatch openwrt/luci-app-bandwatch/root/usr/libexec/rpcd/luci.bandwatch

# UI (starts its own demo servers)
cd tests/frontend && npm install && npx playwright install chromium && cd ../..
node tests/frontend/ui-test.js

# Packaging and OpenWrt container integration
python3 scripts/build.py                # dist/: tar.gz, ipk, apk (apk needs Docker or apk mkpkg)
sh tests/openwrt/run.sh x86-64-25.12.0  # procd/ubus/rpcd in an OpenWrt rootfs
sh tests/openwrt/run.sh x86-64-24.10.4
```

Test layout:

| File | Covers |
| --- | --- |
| `tests/test_core.py` | counters, POSIX timezones and DST, calendar periods, UCI parsing, settings validation, secrets |
| `tests/test_store.py` | bucket splitting, roll-ups, tier selection, missing-data semantics, retention, recovery, migrations, demo/production separation |
| `tests/test_collectors.py` | parsers, WAN detection and double-count avoidance, resets/reconnects, Bandix schema and direction, provider fallback, aria2 accounting/restart/outage/secret, identity, vnStat |
| `tests/test_engine_api.py` | engine with the simulator, API validation, diagnostics sanitisation, all demo scenarios, restart gap recovery |
| `tests/test_server.py` | HTTP security: auth, CSRF, Origin, Host allowlist, traversal, body limits, secrets, rate limiting, SSO, port conflicts |
| `tests/frontend/ui-test.js` | all views at 3 viewport sizes, overflow, console/CSP errors, interactions, login, empty states |
| `tests/openwrt/run.sh` | installer, procd service, unprivileged user, ubus ACL, rpcd SSO, upgrade, rollback, uninstall/purge, apk/ipk |

The OpenWrt container test image grafts a musl Python from the official
`python:3.13-alpine` image into the OpenWrt rootfs, because the OpenWrt
package mirror is not needed then. It is a test harness only; on a router
the real `python3-*` packages are used.

## Building packages

`scripts/build.py` writes reproducible archives (fixed mtimes from the last
commit, root ownership). apk v3 packages are produced with `apk mkpkg` from
Alpine edge (apk-tools ≥ 3.0) when Docker is available. Packages are
unsigned; signing should be added by the maintainer before a public release.

`openwrt/bandwatch/Makefile` and `openwrt/luci-app-bandwatch/Makefile` are
feed Makefiles for building inside an OpenWrt SDK. They have not been built
in an SDK yet.

## Code layout

See [ARCHITECTURE.md](ARCHITECTURE.md). Conventions:

* standard library only; no `logging` (use `bandwatch.log`)
* all OS access through `Platform`; subprocesses without a shell
* integer bytes; rates from monotonic time; never invent data
* frontend: no inline scripts or styles (strict CSP); escape all dynamic text with `esc()`
