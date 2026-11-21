# Changelog

All notable changes to BandWatch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0-beta.1] - unreleased (beta candidate)

First beta candidate for hardware testing. Not yet verified on a physical router.

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
