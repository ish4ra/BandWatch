# Hardware acceptance checklist (v0.1.0-beta.2)

Use this on a physical router before marking it supported. Record results in
a GitHub issue using the **Hardware test report** template.

**Routine bug reports do not need this checklist.** Open the dashboard →
**Health → Download Diagnostic Report** and attach the ZIP. It needs no SSH,
no commands and no reboot, and is built from an allow-list (no addresses,
names, paths or secrets). BandWatch starts recording its own resource use,
restarts, reboots and database saves automatically from the first start, so
a report downloaded after a day of normal use already answers most of the
questions below. The checklist and helper are for full acceptance testing.

## First physical result: Arcadyan AW1000 (one snapshot, beta.1)

Sanitised summary of the first production-mode diagnostic snapshot (taken
676 s after a router boot; BandWatch service uptime was not recorded by
beta.1):

| Item | Observed |
| --- | --- |
| Router / firmware | Arcadyan AW1000, OpenWrt 25.12.5, qualcommax/ipq807x, aarch64_cortex-a53, kernel 6.12.94, 4 cores |
| Python | 3.13.9, SQLite 3.53.1; `sqlite3`, `zoneinfo` present; `ssl`, `_hashlib` absent |
| Installation / mode | installed, production mode |
| Collectors | all six OK, zero errors; clock trusted; 0 loop errors |
| WAN | `wwan0`, ModemManager, detected via ubus, IPv4 |
| Bandix | 0.12.12, 8 devices reported, 8 identified |
| aria2 | 1.37.0, RPC connected, no secret configured, 0 active / 0 waiting |
| vnStat | available, queried on demand |
| Resources (single reading) | BandWatch 0.6 % CPU, ~17.2 MiB RSS, 7 threads; router 16.1 % CPU, 32.9 % memory |
| Database | WAL, schema 1, 40 pending buckets, no save yet (consistent with a service start < 5 minutes earlier), no error |
| Bandix direction | insufficient data |

Not established by that snapshot: traffic-accounting accuracy, sustained
performance, WAN isolation, reboot recovery and historical persistence.
Those are sections C–G below.

**Helper (full acceptance testing only):** `bandwatch-test-round.sh` (in the
tester bundle) collects additional evidence and never changes the router.
`/tmp` is RAM: a reboot deletes the helper and its reports, so follow this
order:

```sh
sh /tmp/bandwatch-test-round.sh before   # before installing (config fingerprint)
sh /tmp/bandwatch-test-round.sh after    # after testing: status, listening check, history, sanitised Bandix sample, diagnostics, config compare
sh /tmp/bandwatch-test-round.sh perf     # 5-minute CPU/RSS measurement (dashboard closed)
sh /tmp/bandwatch-test-round.sh report   # pre-reboot report in /tmp/bandwatch-test-reports/
```

Then **on your computer**, copy the report and confirm the file arrived
**before** rebooting:

```sh
scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .
```

Only then run the reboot test (section F2): reboot, copy the helper to
`/tmp` again, run `postreboot`, and copy its report the same way. Reports
are sanitised (no MAC/IP addresses, host/device/Wi-Fi names, user names,
passwords, secrets or download file names). The helper refuses to create a
report if anything that looks private remains. Review the files before
sharing them anyway. (With beta.2 the dashboard's report also shows reboots
and whether history survived them, so the reboot test can be confirmed from
Health after the router comes back.)

Legend: ☐ not run · ✅ pass · ⚠️ pass with notes · ❌ fail

**Helper:** `bandwatch-test-round.sh` (in the tester bundle) collects most of
the evidence and never changes the router. `/tmp` is RAM: a reboot deletes
the helper and its reports, so follow this order:

```sh
sh /tmp/bandwatch-test-round.sh before   # before installing (config fingerprint)
sh /tmp/bandwatch-test-round.sh after    # after testing: status, listening check, history, sanitised Bandix sample, diagnostics, config compare
sh /tmp/bandwatch-test-round.sh perf     # 5-minute CPU/RSS measurement (dashboard closed)
sh /tmp/bandwatch-test-round.sh report   # pre-reboot report in /tmp/bandwatch-test-reports/
```

Then **on your computer**, copy the report and confirm the file arrived
**before** rebooting:

```sh
scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .
```

Only then run the reboot test (section F2): reboot, copy the helper to
`/tmp` again, run `postreboot`, and copy its report the same way. Reports
are sanitised (no MAC/IP addresses, host/device/Wi-Fi names, user names,
passwords, secrets or download file names). The helper refuses to create a
report if anything that looks private remains. Review the files before
sharing them anyway.

## A. Preparation
- ☐ Router configuration backup created (LuCI → Backup)
- ☐ `cat /etc/openwrt_release` and `df -h /overlay` recorded
- ☐ Note existing services: Bandix version, vnStat version, aria2 RPC port, LuCI

## B. Installation
- ☐ `sh install.sh --dry-run` reports no `[FAIL]` and changes nothing
- ☐ Missing Python packages are reported with the correct `apk add` command
- ☐ `sh install.sh` completes, prints URL and password
- ☐ `ps w | grep bandwatch` shows the process running as user `bandwatch`
- ☐ Dashboard reachable from a LAN PC at `http://<lan-ip>:8787/`
- ☐ Dashboard **not** reachable from the WAN side / mobile network
- ☐ LuCI → Status → BandWatch opens and signs in automatically
- ☐ Installed size: `du -sh /usr/lib/bandwatch` (expected < 1 MiB)

## C. WAN accuracy (cellular `wwan0`)
- ☐ Health → WAN collector shows `wwan0` (proto qmi/mbim) and *Working*
- ☐ No tunnel/underlay device double counted (Health notes)
- ☐ During a speed test, BandWatch peak download/upload within ±10 % of the
  speed test result (record both)
- ☐ 100 MB download from a LAN PC: Today's WAN download grows by ≈100 MB
  (+ protocol overhead, expect ≤ +8 %)
- ☐ Compare Today / This month with vnStat (History → Compare): difference
  explained by vnStat's own day boundary / start date
- ☐ Disconnect/reconnect mobile data: "WAN down" then "WAN detected" events,
  no spike in History
- ☐ IPv6 traffic (e.g. test-ipv6.com) is included and not doubled

## D. Per-device (Bandix)
- ☐ `curl -s http://127.0.0.1:8686/api/traffic/devices | head -c 600`
  matches the documented schema (`data.d[]`, `w_rx_b`, `w_tx_b`, ...);
  attach a **sanitised** sample if fields differ
- ☐ Devices list shows names, IPs, Wi-Fi/Ethernet and online status
- ☐ Download on a phone appears as **download** for that device (if
  inverted, Health shows a direction warning → note it)
- ☐ Sum of device Internet usage ≤ WAN usage for the same period
- ☐ LAN copy between two devices (e.g. Samba) appears as LAN-local, not Internet
- ☐ Restart Bandix (`/etc/init.d/bandix restart`): no usage spike
- ☐ Stop Bandix: dashboard explains per-device data is unavailable; WAN unaffected
- ☐ With hardware/software offloading as configured on the router, note any
  under-reporting (Health → Accounting notes)

## E. aria2
- ☐ Enter RPC secret in Settings → aria2; status becomes *Working*
- ☐ Secret not visible in page source, API responses or `bandwatch-diag`
- ☐ Live speeds match aria2's own UI (±10 %)
- ☐ Resuming a partially downloaded file does not count existing data
- ☐ Restart aria2: no spike; restart event recorded
- ☐ Completed/failed tasks appear in Recent activity after the next flush
- ☐ BandWatch did not change any aria2 setting or task

## F. Persistence and resilience
- ☐ After 1 h, History → Today shows hourly bars and ≥ 98 % coverage
- ☐ `/etc/init.d/bandwatch restart`: downtime traffic is recovered
  (event "Recovered WAN traffic …"), coverage gap small
- ☐ (Router reboot is tested separately in F2, after the pre-reboot report
  is safely on your computer.)
- ☐ Power-cut test (optional): at most `flush_interval` of samples lost;
  database opens without errors
- ☐ Midnight rollover in Asia/Colombo (+05:30): Today resets at 00:00 local
- ☐ Month rollover: This month / Last month correct
- ☐ Change timezone in Settings: daily totals rebuilt

## F2. Reboot (only after the pre-reboot report is on your computer)
- ☐ `reboot`; wait until LuCI is back (2–3 min)
- ☐ Copy the helper again: `scp -O bandwatch-test-round.sh root@192.168.1.1:/tmp/`
- ☐ `sh /tmp/bandwatch-test-round.sh postreboot`: service *running* and starts at boot
- ☐ History from before the reboot is still there (`history:` line vs. the pre-reboot report; History → This month)
- ☐ Reboot gap shown as unmeasured; no bogus spike
- ☐ `VERDICT: PASS` still shown after the reboot
- ☐ Post-reboot report copied: `scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .`

## G. Performance (record for 24 h idle and during heavy use)
- ☐ BandWatch CPU (Health → BandWatch process, or `top`): target < 2 % idle
- ☐ BandWatch RSS: target < 30 MiB
- ☐ Database size after 24 h / 7 days (`ls -l /etc/bandwatch`)
- ☐ No measurable throughput loss in a speed test with BandWatch running vs stopped
- ☐ Overlay writes acceptable (check `/proc/<pid>/io` write_bytes per hour)

## H. UI
- ☐ Phone (portrait), tablet and desktop layouts usable
- ☐ Dark/light/system theme switch
- ☐ Units (Mbps vs MB/s, GB vs GiB) applied everywhere
- ☐ CSV export opens in a spreadsheet with correct local times

## H2. Optional HTTPS (only if time permits)
- ☐ `apk add python3-openssl` (if missing), `uci set bandwatch.main.https=1; uci commit bandwatch; /etc/init.d/bandwatch restart`
- ☐ `https://<lan-ip>:8787/` works after accepting the certificate; LuCI page embeds it
- ☐ `ls -l /etc/bandwatch/tls` shows `key.pem` as `-rw-------`
- ☐ Switch back: `uci set bandwatch.main.https=0; uci commit bandwatch; /etc/init.d/bandwatch restart`

## I. Security
- ☐ API returns 401 without login; login rate-limits after repeated failures
- ☐ `/etc/bandwatch` is `drwxr-x---` and `auth.json` is `-rw-------`
- ☐ WAN isolation, local check (conclusive): `after` prints
  `VERDICT: PASS (BandWatch listens only on loopback and LAN addresses)` and
  `netstat -ltn | grep 8787` shows only `127.0.0.1:8787` and the LAN address
  (not `0.0.0.0:8787` or `:::8787`)
- ☐ WAN isolation, external check (optional): from mobile data with Wi-Fi off,
  `http://<WAN IP>:8787/` must not open. **Only meaningful with a public WAN
  address.** If LuCI shows a WAN IPv4 in `100.64.0.0/10` (CGNAT), `10.0.0.0/8`,
  `172.16.0.0/12` or `192.168.0.0/16`, nothing from outside can reach the
  router, so record "not conclusive (CGNAT/private WAN)" and rely on the local check
- ☐ Firewall, DHCP, DNS, Wi-Fi, routing and offloading settings unchanged
  (`uci export firewall` before/after identical)

## J. Upgrade and removal
- ☐ Re-running the installer keeps history, settings and password
- ☐ `sh uninstall.sh` removes the program and keeps data
- ☐ `sh uninstall.sh --purge` removes data and the `bandwatch` user
- ☐ `--purge` refuses (and removes nothing) when `data_dir` is a symbolic link
  or contains files BandWatch did not create
- ☐ Router behaves normally afterwards
