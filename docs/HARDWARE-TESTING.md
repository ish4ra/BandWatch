# Hardware acceptance checklist (v0.1.0-beta.1)

Use this on the first physical router (Arcadyan AW1000, OpenWrt 25.12.5,
qualcommax/ipq807x) and on any new device before marking it supported.
Record results in a GitHub issue using the **Hardware test report** template.
Attach the output of `bandwatch-diag` (sanitised) — never raw configuration
files.

Legend: ☐ not run · ✅ pass · ⚠️ pass with notes · ❌ fail

**Helper:** `bandwatch-test-round.sh` (in the tester bundle) collects most of
the evidence in one go and never changes the router:

```sh
sh bandwatch-test-round.sh before   # before installing (config fingerprint)
sh bandwatch-test-round.sh after    # after testing: status, sanitised Bandix sample, diagnostics, config compare
sh bandwatch-test-round.sh perf     # 5-minute CPU/RSS measurement (dashboard closed)
sh bandwatch-test-round.sh report   # tarball for the maintainer (run before rebooting)
```

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
- ☐ Router reboot: service starts automatically; reboot gap shown as
  unmeasured; no bogus spike
- ☐ Power-cut test (optional): at most `flush_interval` of samples lost;
  database opens without errors
- ☐ Midnight rollover in Asia/Colombo (+05:30): Today resets at 00:00 local
- ☐ Month rollover: This month / Last month correct
- ☐ Change timezone in Settings: daily totals rebuilt

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
- ☐ `netstat -ltn` shows BandWatch only on the LAN address and 127.0.0.1
- ☐ Firewall, DHCP, DNS, Wi-Fi, routing and offloading settings unchanged
  (`uci export firewall` before/after identical)

## J. Upgrade and removal
- ☐ Re-running the installer keeps history, settings and password
- ☐ `sh uninstall.sh` removes the program and keeps data
- ☐ `sh uninstall.sh --purge` removes data and the `bandwatch` user
- ☐ Router behaves normally afterwards
