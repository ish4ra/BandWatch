# Changelog

All notable changes to BandWatch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0-beta.2] - unreleased (candidate)

Reliability, diagnostics and upgrade release driven by the first physical
router snapshot (Arcadyan AW1000, OpenWrt 25.12.5). Still a beta: one
diagnostic snapshot is not hardware acceptance.

### First physical hardware result (one snapshot, not acceptance)
- Arcadyan AW1000, OpenWrt 25.12.5 (qualcommax/ipq807x, aarch64_cortex-a53,
  kernel 6.12.94), Python 3.13.9 without `ssl`/`_hashlib`: installation and
  production-mode operation observed; all six collectors OK; ModemManager WAN
  `wwan0` detected via ubus; Bandix 0.12.12 reported 8 devices; aria2 1.37.0
  RPC connected (no secret); BandWatch CPU 0.6 %, RSS ~17.2 MiB, 7 threads;
  no loop errors. Traffic accuracy, sustained load, WAN isolation and reboot
  recovery were **not** established by that snapshot.

### Added
- **One-click diagnostic report** (Health and Settings): a ZIP with a
  versioned JSON report (`bandwatch-diagnostic-report` v1) and a plain-text
  `SUMMARY.txt`. No SSH, scripts, root shell or reboot needed. Every check is
  *passed*, *failed*, *warning*, *insufficient data*, *not tested* or
  *unavailable*; untested conditions are listed explicitly.
- **Automatic local health monitoring** from the first start: BandWatch CPU,
  RSS and threads, router CPU/memory, WAN sampling delays and missed samples,
  collector and loop errors in bounded 15-minute intervals (24 h in memory,
  31 days in the database, written with the regular batched save). Last hour,
  24 h, 7 and 30 day summaries. Never uploaded.
- **Service lifecycle**: first start, upgrade, restart and router-reboot
  detection (kernel boot id), clean vs. unclean previous stop, start history.
- **Persistence status** on Health: service uptime (distinct from router
  uptime), database open time and integrity, last successful save, time to
  the next save, unsaved buckets (including samples waiting for a trusted
  clock), last save error, and a plain explanation of whether an empty
  history is the normal start-up state or a real problem.
- **Manual update check** in Settings: runs in the browser only when asked,
  against the official ish4ra/BandWatch releases (works without Python
  `ssl`); shows version, beta/stable, date, notes and links. No background
  requests, no router or device data sent, no automatic installation.
- Upgrade guides in English and Sinhala (`docs/UPGRADE.md`,
  `docs/UPGRADE-SINHALA.md`).

### Fixed
- Samples gathered before the router clock was trusted (first ~10 minutes
  after boot) were discarded when the service stopped or restarted in that
  window (the init script restarts BandWatch when `lan` comes up). They are
  now kept across a restart within the same boot, and reported if a reboot
  makes them unusable.
- A clock step during the first 10 minutes after boot (time synchronisation)
  marked the clock as trusted without converting the samples buffered until
  then, so they stayed in memory and were never saved.
- The diagnostics export did not include service uptime, so a normal
  "nothing saved yet" start-up state could not be told apart from a
  persistence failure. The unsaved-bucket count ignored the pre-clock buffer.
- An empty save (nothing collected yet) was reported like a successful
  write.
- The first save after start-up now happens 90 s after the clock is trusted
  (one extra small write per start) instead of after a full interval, so
  persistence is confirmed within minutes. Later saves keep the configured
  interval.
- A clean stop now checkpoints the SQLite WAL.

### Changed
- **Diagnostics privacy**: the export is built from an explicit allow-list
  instead of removing a few fields from nested objects. Collector messages,
  notes and provider replies (which can quote URLs, addresses or file names)
  are no longer exported; collectors expose machine-readable codes with
  fixed descriptions. Listening addresses and provider endpoints are reported
  as scopes (loopback/LAN/wildcard), never as addresses. Tested with hostile
  provider responses containing secrets and personal identifiers.
- Bandix direction check ignores router-generated traffic (aria2, updates),
  traffic not attributed to LAN devices and symmetric traffic, shows its
  progress ("Pending: n of 20 usable samples") and adds an *inconclusive*
  state. It never changes the setting automatically and never generates
  traffic.
- `bandwatch check` reports the optional `ssl` module with guidance;
  `bandwatch diag` uses the same allow-listed report.
- CSP `connect-src` additionally allows `https://api.github.com` (manual
  update check only).

### Compatibility
- Database schema unchanged (1). One additive table (`health`); beta.1 can
  still open the database after a rollback. Upgrade, rollback and re-upgrade
  are tested against the **published** beta.1 code and packages (installer,
  apk and opkg).

### Firefox (issue #2) - root cause located, CI job still intermittent
- Reproduced in an isolated workflow (`tests/firefox-repro/`): the stalled
  page is fully loaded (`readyState` complete, dashboard rendered, polling
  continuing) while Playwright still reports `about:blank` - Playwright's
  Firefox driver loses the navigation events of a freshly created page. The
  service worker is not involved (stalls with it blocked); the beta.1
  "keep contexts open" workaround made stalls more frequent and is removed.
- Regular Firefox 160 via geckodriver: 300 new-tab loads, 0 failures.
- The Firefox UI job still fails intermittently; failures now state whether
  the page had loaded. Needs an upstream Playwright fix.

## [0.1.0-beta.1] - 2026-11-21 (public beta prerelease)

First public beta, intended for early adopters and physical-router testing.
Not production-ready. Verified by automated tests, OpenWrt 25.12 and 24.10
container tests and an OpenWrt 25.12 SDK build; **no physical router has been
tested yet**.

### Known issues
- The automated Firefox dashboard tests stall intermittently under
  Playwright after cancelled requests
  ([#2](https://github.com/ish4ra/BandWatch/issues/2)). This is the only
  failing CI check, accepted as a release exception. Firefox is not verified;
  use a Chromium-based browser or Safari for this beta.
- Packages are unsigned; verify `SHA256SUMS`.

### Release and presentation
- README redesigned, with fresh screenshots of the real dashboard in demo
  mode (`scripts/screenshots.js`), brand assets and a social preview image
  in `docs/assets/`.
- Release workflow (`.github/workflows/release.yml`) builds every asset from
  the `main` commit and publishes a GitHub prerelease. It also publishes a
  `SHA256SUMS` over the final asset names, then downloads each asset publicly
  and verifies it.
- Tester instructions link to the public release assets (no GitHub account
  needed) and recommend a Chromium-based browser.

First beta candidate for hardware testing. Not yet verified on a physical router.

### Pre-beta audit (fixed)
- Demo mode showed the host computer's disk usage and private paths on the
  Health page; simulated routers now only report simulated filesystems, and
  the BandWatch database size is shown separately from filesystem usage.
- Timezones are shown as `UTC+05:30` / `Asia/Colombo (UTC+05:30)` instead of
  raw POSIX strings.
- aria2 page: Today and This month summary cards.
- Slow Bandix/aria2/ubus calls or database flushes could delay WAN sampling;
  collection now uses separate WAN and provider threads, and flushes no
  longer block sample accumulation.
- Range queries starting mid-quarter after 1-minute retention dropped data.
- Out-of-range numbers or malformed replies from Bandix/aria2/ubus/vnStat
  could abort a collector cycle; parsing is now strict and type-checked.
- `listen 'lan'` combined with `listen 'any'` failed with "address in use".
- LuCI could not embed the dashboard when LuCI used a non-default port.
- Security: provider endpoints limited to the router (SSRF / secret
  exfiltration), aria2 secret bound to its endpoint, CGNAT no longer
  trusted by default, LuCI SSO requires the write ACL, safer ownership
  handling in the init script and rpcd helper, purge guard, bounded
  password checks. See docs/SECURITY-AUDIT.md.
- aria2: data present when a task is first seen (resumed data, downloads
  completed between polls) is reported as "not counted".
- Bandix: `/api/version` fallback and pre-0.12.7 long field names.
- Dashboard: leaving a page while its data was still loading could replace
  the next page with a "Could not load data" card (seen in WebKit CI);
  late responses from a previous view are now ignored. The Settings page no
  longer loses its form when async renders overlap, CSV/JSON downloads work
  in Safari/WebKit, and live streams are limited to 2 per client so extra
  tabs cannot stall page loads. The live stream is closed when the page is
  left or put into the back/forward cache, and resumed on restore.

### Tester delivery hardening
- Tester instructions (Sinhala, English, checklist, README-FIRST) agree: the
  pre-reboot report is copied to the computer before rebooting; the reboot
  test is a separate step with its own `postreboot` report.
- `bandwatch-test-round.sh`: safe work/report directories (no symlinks,
  root-owned, 0700), stronger sanitising (shared with `bandwatch-diag`),
  structure-only Bandix samples, a leak check before creating a report, and a
  local listening-address check (`VERDICT:`) for WAN isolation.
- `uninstall.sh --purge` only deletes a canonical, genuine BandWatch data
  directory; otherwise it removes nothing.
- Docs: an outside test from mobile data proves nothing behind CGNAT; the
  local listening check is the conclusive one.

### Added during the audit
- Optional HTTPS with a self-signed certificate created by `px5g`.
- Optional apk signing in `scripts/build.py` / CI (no keys in the repository).
- Feed Makefiles build from the local tree; validated in the OpenWrt 25.12 SDK.
- Tester bundle with Sinhala guide and `bandwatch-test-round.sh`.
- Tests: regression and hardening suites, hostile-name XSS scenario, real
  LuCI end-to-end test, Firefox/WebKit and SDK jobs in CI.

### Added
- WAN collector based on interface counters with ubus/route-based detection,
  explicit overrides, IPv4/IPv6, multi-WAN, tunnel and PPPoE double-count
  avoidance, reset/reconnect/reboot handling and restart gap recovery.
- Per-device providers: Bandix 0.12.x (read-only, WAN/LAN split, direction
  setting and cross-check) and nlbwmon fallback; passive device
  identification (DHCP, ARP, bridge FDB, hostapd).
- Optional read-only aria2 JSON-RPC integration with safe transfer accounting.
- On-demand vnStat comparison.
- SQLite storage with 1-minute, 15-minute and daily tiers, batched writes,
  retention, migrations, corruption recovery and demo/production separation.
- Analytics: periods (today … this year, custom), coverage, peak/average/
  minimum speeds, CSV/JSON export, monthly quota with billing day.
- Responsive dashboard (Overview, Devices, aria2, History, Health,
  Settings), dark/light/system themes, PWA, canvas charts, no external assets.
- Authentication (password, LuCI SSO), CSRF, Host/Origin checks, strict CSP,
  rate limiting, unprivileged service with ubus ACL.
- OpenWrt integration: procd init, UCI config, LuCI app with rpcd plugin,
  installer with preflight/backup/rollback, uninstaller with purge,
  sanitised diagnostics, apk v3 and ipk packages.
- Demo mode with 12 simulated scenarios.
- Tests: backend (pytest), UI (Playwright), OpenWrt 24.10/25.12 container
  integration; GitHub Actions CI with beta artifacts and SHA-256 checksums.
