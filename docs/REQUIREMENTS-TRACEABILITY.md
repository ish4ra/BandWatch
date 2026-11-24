# Requirements traceability matrix (v0.1.0-beta.2)

Verification levels (strongest evidence available today):

| Level | Meaning |
| --- | --- |
| **HW** | Verified on physical router hardware — **one AW1000 snapshot** (installation, collector states, provider connectivity only) |
| **OWRT** | Automated test inside an OpenWrt 24.10/25.12 rootfs container (real procd, ubusd, rpcd, uhttpd, LuCI, apk/opkg, BusyBox) |
| **SIM** | Automated test against the demo simulator, fixtures or fakes |
| **UNIT** | Unit test of the logic |
| **DOC** | Documented / designed; not automatically testable |
| **—** | Not implemented |

Only installation, collector states and provider connectivity have been
observed on physical hardware (one AW1000 snapshot). Every HW-relevant item is
in [HARDWARE-TESTING.md](HARDWARE-TESTING.md).

## Reliability and diagnostics (0.1.0-beta.2)

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| Empty start-up DB distinguished from persistence failure; service vs router uptime | `Engine.persistence_status()` | `test_empty_database_on_first_start_is_reported_as_normal`, `test_aw1000_like_snapshot_is_classified_as_normal_startup` | SIM |
| Early first save, then regular interval | `FIRST_FLUSH_DELAY` in `engine.py` | `test_first_flush_happens_early_then_regular_interval` | SIM |
| Restart / reboot / unclean stop / upgrade detection | `Engine._record_start()` | `test_clean_restart_…`, `test_unclean_stop_is_detected`, `test_router_reboot_is_detected_from_boot_id`, `test_upgrade_from_database_without_lifecycle` | SIM |
| Pre-clock samples survive a restart within the same boot | `_save_prebuffer` / `_load_prebuffer` | `test_prebuffer_survives_restart_within_the_same_boot`, `…_dropped_after_reboot`, `…_persisted_once_clock_is_trusted` | SIM |
| WAL checkpoint, power-loss consistency, flush-failure retry | `store.py` | `test_clean_stop_checkpoints_the_wal`, `test_power_loss_snapshot_is_consistent`, `test_flush_failure_is_reported_and_retried` | UNIT |
| Bounded self-monitoring (CPU, RSS, threads, sampling, errors) | `selfmon.py`, `health` table | `test_selfmon_memory_is_bounded_and_weighted`, `test_selfmon_rows_persist_with_flush_and_survive_restart` | UNIT |
| One-click sanitised report (JSON + ZIP), six statuses | `report.py`, `/api/diagnostics/*`, Health/Settings UI | `test_report_privacy.py`, `test_one_click_report_download`, UI "one-click diagnostic report" | SIM |
| Allow-listed privacy with hostile inputs | `report.py` validators, `Status.code` | `test_report_never_contains_planted_personal_data`, `test_hostile_aria2_error_is_reduced_to_a_code`, `test_hostile_bandix_reply_is_reduced_to_a_code` | SIM |
| ModemManager WAN, missing ssl/_hashlib, aria2 with/without secret | `wan.py`, `report.dependency_guidance` | `test_hardware_compat.py` | UNIT / HW snapshot |
| Bandix direction: pending state, router traffic ignored, no auto-swap | `Engine._direction_sample` | `test_direction_*` | UNIT |
| Manual, browser-side update check | `app.js` `mountUpdates`, CSP | UI "manual update check" | SIM |
| Upgrade from published beta.1, rollback, re-upgrade | installer, apk, opkg | `test_upgrade_beta1.py`, `tests/openwrt/upgrade-beta1.sh` | OWRT |

## Data collection

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| WAN counters as primary real-time source | `collectors/wan.py` (`/proc/net/dev`) | `test_collectors::test_wan_sampling_reset_and_reconnect` | SIM, OWRT (service runs) |
| Auto WAN detection via ubus + routing | `wan.detect`, `detect_from_ubus`, `detect_from_routes` | `test_ubus_detection_dedupes_and_ignores_tunnels`, `test_ubus_garbage_falls_back_to_routes` | UNIT, SIM |
| Ethernet / PPPoE / fibre / cellular / overrides | `wan.py`, setting `wan.interfaces` | demo scenarios `fibre-ethernet`, `pppoe`, `aw1000-cellular`; `test_wan_override_and_counter_reset_without_reconnect` | SIM |
| IPv4 + IPv6 without double counting | per-L3-device dedupe, tunnel filter | `test_ubus_detection_dedupes_and_ignores_tunnels` | UNIT |
| No logical + physical double counting (PPPoE) | `choose()`; pppoe-* measured only | demo `pppoe` (underlay counter ignored) | SIM |
| Interface changes, reconnects, resets, reboots | `counters.CounterTracker`, ifindex + boot-id identity | `test_counter_*`, demo `reboot`/`reconnect`/`counter-reset` | UNIT, SIM |
| Restart gap recovery (no reboot) | `engine._recover_gap`, atomic checkpoints | `test_restart_recovers_gap_from_counters` | SIM |
| Optional vnStat reconciliation | `collectors/extras.VnstatReader` (on demand) | `test_vnstat_parse`, `test_vnstat_malformed`, UI vnStat check | SIM |
| Unavailable/incomplete measurements identified | collector `Status`, coverage | `test_missing_data_is_not_zero`, `test_no_provider_message` | UNIT |
| Bandix integration, read-only | `perdevice.BandixProvider` (GET only) | `test_bandix_never_calls_write_endpoints`, malformed-response tests | SIM (schema from upstream docs) |
| Bandix version/capability detection | `probe()` with `/api/version` fallback, `capabilities` | `test_bandix_version_fallback_endpoint` | SIM |
| Bandix RX/TX direction | default `rx_is_download` (matches upstream LuCI labels), setting + WAN cross-check | `test_bandix_direction_swap_setting`; `engine.direction_verdict` | SIM — **HW required** |
| Works without Bandix | `AutoProvider`, nlbwmon fallback | `test_auto_provider_falls_back_to_nlbwmon`, demo `no-bandix`, UI no-bandix check | SIM |
| Never fabricate data | gaps instead of zeros, strict parsing | `test_missing_data_is_not_zero`, `test_bandix_malformed_responses_never_crash_or_invent_data` | UNIT |
| Internet vs LAN-local separation | `dev` vs `devl` series | `test_device_lan_traffic_kept_separate` | SIM |
| Device enrichment (DHCP, ARP, Wi-Fi, bridge) | `collectors/identity.py` | `test_identity_merges_sources`, `test_parse_brforward_struct` | UNIT |
| Wi-Fi / Ethernet / unknown types | `IdentityCollector.poll` | UI devices check, `test_identity_merges_sources` | SIM — **HW required** |
| Offloading not modified, limitations reported | `extras.offload_notes` | `test_offload_notes` | UNIT |
| aria2 optional, auto-detect/configure | `collectors/aria2.py`, `aria2.mode/url` | `test_aria2_disabled`, demo `no-aria2` | SIM |
| aria2 authenticated read-only RPC | `system.multicall` of read methods | `test_aria2_does_not_count_preexisting_and_handles_restart` (method allow-list) | SIM |
| aria2 secret never exposed | `SecretStore` 0600, write-only API | `test_secret_never_returned`, UI "aria2 secret is write-only", `test_aria2_secret_cleared_when_endpoint_changes` | SIM |
| aria2 live speeds, task states | `Aria2Collector.live` | UI aria2 checks | SIM |
| aria2 restarts / stopped / removed / resumed / outages | session id re-baseline, outage baseline kept | `test_aria2_*` | UNIT |
| Pre-existing data not counted; short tasks reported | first-sight baseline + `uncounted` | `test_aria2_short_download_between_polls_reported_not_counted` | UNIT |

## Dashboard

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| Overview (speeds, today, month, devices, live chart, sources, top devices, health, collectors) | `web/js/app.js` `views.overview`, `api.overview` | UI view checks (4 sizes), `test_engine_collects_and_flushes` | SIM |
| aria2 page (live, tasks, today/month, periods, custom, peak/avg/min, recent, charts) | `views.aria2`, `api.aria2` | `test_aria2_api_has_today_and_month`, UI aria2 cards | SIM |
| Hide aria2 when unavailable | empty state | UI "no-aria2" check | SIM |
| Devices (name/alias, IP, MAC, type, status, live, totals, peak/min/avg, charts, top, search/sort/filter) | `views.devices`, `deviceDetail` | UI devices checks, `test_device_detail_every_period`, `test_device_custom_range_equals_sum_of_days` | SIM |
| History (all periods, custom, charts, total/peak/avg/min, CSV) | `views.history`, `api.history(_csv)` | `test_history_api_and_csv`, UI history check | SIM |
| System Health (CPU, RAM, storage, uptime, temperature, own resources, collectors, DB) | `views.health`, `SystemCollector` | UI health checks, `test_production_storage_separates_database_from_filesystem` | SIM |
| Settings (theme, units, interfaces, sampling, retention, timezone, quotas, aliases, integrations, diagnostics, export) | `views.settings`, `config.SETTINGS_SCHEMA` | `test_settings_validation_rejects_bad_values`, UI settings check | SIM |
| Dark/light/system theme | CSS tokens, `applyTheme` | UI theme toggle + light palette checks | SIM |
| Responsive desktop/tablet/mobile | CSS breakpoints | UI overflow checks at 320/390/820/1440 px, Chromium (local + CI), Firefox and WebKit (CI) | SIM |
| Micro-animations, reduced motion | CSS `rise`, `pulse`, `prefers-reduced-motion` | UI reduced-motion check | SIM |
| Accessible, keyboard navigation | semantic markup, focus styles | UI keyboard check | SIM (no full audit) |
| Offline assets, no CDN/fonts | local assets, CSP `'self'` | `ci-security-checks.sh`, CSP test | UNIT |
| PWA when secure context | manifest + service worker | DOC (requires HTTPS) |
| Original logo/favicon | `web/assets/*.svg` | DOC |

## Analytics correctness

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| Separate WAN / device / aria2 | separate series, never summed | `test_engine_collects_and_flushes` (sources) | SIM |
| Integer bytes, monotonic-time rates | `counters.py`, `store.accumulate` | `test_accumulate_splits_across_minutes_without_losing_bytes` | UNIT |
| Missing samples ≠ zero; coverage shown | `analytics.summary/chart` | `test_missing_data_is_not_zero` | UNIT |
| Average over measured duration; peak/min | `analytics` | `test_peak_and_min_tracked_only_for_rate_samples`, `test_device_rows_use_aggregate_coverage` | UNIT |
| Idle zero vs unavailable | coverage + null buckets | `test_missing_data_is_not_zero` | UNIT |
| Timezone calendars; midnight/week/month/year; DST | `tz.py` (POSIX TZ incl. fold) | `test_posix_*`, `test_period_bounds_*`, `test_rebuild_days_after_timezone_change` | UNIT |
| Friendly timezone labels | `tz.label` | `test_timezone_labels_hide_posix_strings` | UNIT |
| Exact totals over long runs | tiered storage + edge fallback | `test_long_running_bounded_memory_and_exact_totals` (2 simulated days) | UNIT |

## Performance and storage

| Requirement | Implementation | Tests / measurements | Level |
| --- | --- | --- | --- |
| Live refresh 1–2 s, shared samples (SSE) | `engine`, `/api/live/stream` | UI live check, `test_concurrent_dashboard_clients` | SIM |
| Expensive collectors cannot block WAN | separate `wan` / `providers` threads | `test_slow_provider_does_not_block_wan_sampling` | SIM |
| Flush never blocks sampling | `store.plock` vs connection lock | `test_flush_io_does_not_block_sample_accumulation` | UNIT |
| Bounded caches/memory | ring buffers, TTLs, caps | `test_long_running_bounded_memory_and_exact_totals`, 6-min benchmark ([PERFORMANCE.md](PERFORMANCE.md)) | SIM |
| Bounded DB, roll-ups, retention, batching | `store._prune`, tiers | `test_retention_prunes_tiers` | UNIT |
| Crash / recovery | WAL, quick_check, quarantine | `test_unexpected_shutdown_keeps_flushed_data` (SIGKILL), `test_corrupt_database_is_quarantined_and_recreated` | SIM |
| Don't wake sleeping disks | vnStat on demand; data on flash | DOC |
| <2 % idle CPU, <30 MiB RSS | — | x86 measurement only (see PERFORMANCE.md) — **HW required** |

## Security

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| No telemetry/cloud/external deps | none present | `ci-security-checks.sh` | UNIT |
| Not exposed to WAN by default | `listen 'lan'`, client allow-list (no CGNAT) | `test_client_allowlist`, `test_cgnat_not_private_by_default` | SIM — **HW required** |
| Authentication (password, LuCI SSO) | `auth.py`, rpcd plugin (write ACL) | `test_login_flow_and_csrf`, `luci-e2e.js` (real LuCI) | OWRT |
| CSRF | header token + Origin | `test_login_flow_and_csrf` | SIM |
| Input validation / path traversal / XSS | `api.py`, `server._static`, `esc()` | `test_path_traversal_blocked`, hostile-names UI test | SIM |
| DNS rebinding | Host allow-list | `test_host_header_allowlist` | SIM |
| SSRF via provider URLs | loopback-only unless `allow_lan_providers` | `test_provider_urls_restricted_to_router` | UNIT |
| Request limits / timeouts | 64 KiB, socket timeouts, bounded threads | `test_body_limits_and_content_type` | SIM |
| Secrets protected | 0600 files, never returned | `test_secret_never_returned`, OWRT permission check | OWRT |
| Unprivileged web process | procd `user bandwatch`, ubus ACL | `run.sh` (process user, ACL allow/deny) | OWRT |
| No shell endpoint | no command execution | `test_no_shell_or_eval_in_application` | UNIT |
| Sanitised diagnostics | `engine.diagnostics`, `bandwatch-diag.sh` | `test_diagnostics_are_sanitised`, OWRT diag check | OWRT |
| Optional HTTPS | `server.make_ssl_context`, px5g cert | `test_https_mode_serves_tls_and_secure_cookies`, OWRT HTTPS check | OWRT |

## OpenWrt integration and packaging

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| procd service, autostart, start/stop/restart/status | `etc/init.d/bandwatch` | `run.sh` | OWRT |
| UCI configuration | `etc/config/bandwatch` | `test_service_config_*` | UNIT, OWRT |
| LuCI menu integration | `luci-app-bandwatch` | `luci-e2e.js` in real LuCI 25.12 | OWRT |
| Port 8787 default, conflict detection | installer + `serve()` | `test_port_conflict_reported`, OWRT port conflict check | OWRT |
| Preflight, dry run, dependency detection | `install.sh` | OWRT dry run check | OWRT |
| Rollback, upgrade | `install.sh` | OWRT upgrade/rollback checks | OWRT |
| Uninstall keeps data; explicit purge | `uninstall.sh` | OWRT uninstall/purge checks | OWRT |
| apk (25.12) and ipk (24.10) | `scripts/build.py`, feed Makefiles | OWRT apk/ipk install; SDK build job | OWRT |
| Package signing | optional `BANDWATCH_APK_SIGN_KEY` (OpenWrt apk) | signing verified with a throwaway key (not committed) | DOC (no maintainer key yet) |
| Version reporting | `bandwatch version`, rpcd status | OWRT LuCI status | OWRT |

## Demo and documentation

| Requirement | Implementation | Tests | Level |
| --- | --- | --- | --- |
| Demo mode with simulated scenarios, labelled | `demo.py` (13 scenarios) | `test_demo_scenarios_run_without_errors` (all), UI demo label | SIM |
| Demo data never mixed into production | DB `mode` marker | `test_demo_and_production_databases_never_mix` | UNIT |
| Docs incl. Sinhala tester guide, hardware checklist | `docs/` | DOC |
