# Changelog

All notable changes to BandWatch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0-beta.1] - unreleased (beta candidate)

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
