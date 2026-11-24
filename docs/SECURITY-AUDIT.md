# Security audits (v0.1.0-beta.1 pre-beta, v0.1.0-beta.2 diagnostics)

Scope: authentication, authorisation, LuCI single sign-on, sessions, CSRF,
XSS, DNS rebinding, SSRF via provider URLs, secret handling, service
permissions, installer/uninstaller safety, network exposure and HTTPS.
Method: code review plus automated tests (pytest, Playwright including a
real LuCI in an OpenWrt 25.12 container). Not a third-party penetration test.

## Diagnostics audit (v0.1.0-beta.2)

Scope: the diagnostics export (`Engine.diagnostics()`, `/api/diagnostics`,
`bandwatch diag`) and the new report, update-check and self-monitoring code.

| # | Severity | Finding | Fix | Test |
| --- | --- | --- | --- | --- |
| D1 | Medium | The beta.1 export copied nested collector objects after removing a few keys (deny list). Collector `message` and `notes` can quote provider replies verbatim (e.g. an aria2 `faultString`, Bandix errors, configured URLs); `settings` and storage `path` values were passed through. A provider reply containing a token, address or file name would have been exported. | Report built from an explicit allow-list with per-field validators (`bandwatch/report.py`); collectors expose machine-readable codes with fixed descriptions; addresses reduced to scopes; event messages not exported. `/api/diagnostics` and `bandwatch diag` use the same builder. | `tests/test_report_privacy.py` (hostile provider replies, planted secrets/MACs/IPs/SSIDs/paths/script payloads) |
| D2 | Low | New outbound capability (update check) could leak router context. | Browser-side only, on explicit click, `credentials: 'omit'`, no referrer, CSP `connect-src` limited to `https://api.github.com`; notes rendered as text; links restricted to the official repository. | UI test "manual update check" (no request before click, no cookie/referrer, HTML in notes not rendered, foreign/draft releases ignored) |
| D3 | Info | Report endpoints are new GET routes. | Same session requirement and network gate as every API route; no state change; no command execution. | `test_api_requires_auth`, `test_one_click_report_download` |

## Findings fixed in this audit

| # | Severity | Finding | Fix | Test |
| --- | --- | --- | --- | --- |
| 1 | High | Authenticated settings API accepted any private LAN address for Bandix/aria2 endpoints. An authenticated user (or CSRF-free script with a session) could make BandWatch send requests — including the aria2 RPC secret — to any LAN host (SSRF / secret exfiltration). | Provider URLs are restricted to loopback (127.0.0.1, ::1, localhost) unless the administrator sets `option allow_lan_providers '1'` in UCI; URLs with credentials, queries, https or bad ports rejected; the stored aria2 secret is cleared whenever the aria2 endpoint changes. | `test_provider_urls_restricted_to_router`, `test_lan_provider_requires_explicit_opt_in`, `test_aria2_secret_cleared_when_endpoint_changes` |
| 2 | Medium | LuCI SSO was granted by the *read* ACL: a read-only LuCI user could obtain a full (read/write) BandWatch session. | `sso` moved to the `write` ACL of `luci-app-bandwatch`; `status` stays read. | `test_luci_sso_requires_write_acl`, real-LuCI SSO test |
| 3 | Medium | `100.64.0.0/10` (CGNAT) was in the default "private" client allow-list. Cellular WAN addresses and some ISP networks use this range. | Removed from the default; can be added explicitly (e.g. for a tailnet). | `test_cgnat_not_private_by_default` |
| 4 | Medium | Init script ran `chown -R bandwatch` on `/var/run/bandwatch` and chowned matching files in the data directory. A compromised service account could plant links so root would chown other files. | No recursive chown; directories only (symlinks skipped); only regular, root-owned files are handed over (OpenWrt's `fs.protected_hardlinks=1`, verified, blocks hardlinks to foreign files). The CLI wrapper applies the same rule. | OWRT container suite |
| 5 | Low | rpcd SSO helper (root) wrote token files into a directory owned by the service user without symlink checks. | Refuses symlinked run/sso directories and existing token paths. | code review, real-LuCI test |
| 6 | Low | `uninstall.sh --purge` would `rm -r` whatever `data_dir` UCI pointed to (except a few system paths); a name merely containing `bandwatch` was not a sufficient check. | Purge requires a canonical absolute path with no symbolic link in it. The directory must be named `bandwatch`/`bandwatch-*` and must not be a system location, `/` child or mount point. It must be owned by root or the service user and contain only files BandWatch creates. The check is repeated right before deleting, and a failure removes nothing. | `test_tester_scripts.py::test_purge_*`, OWRT container (foreign file, symlinked data_dir) |
| 12 | Low | Tester helper and `bandwatch-diag` used predictable `/tmp` paths (a planted symlink could redirect root's writes or reads). Their sanitising missed compressed IPv6, URL credentials, Wi-Fi/PPPoE/aria2 values and Bandix system info. | Work and report directories must be real, root-owned, mode 0700 directories (symlinks refused); `bandwatch-diag` uses `mktemp`. A shared sanitiser replaces router-specific private values literally, plus MAC/IPv4/IPv6/credential/file-name patterns. Bandix samples keep only keys and numbers, and only version strings from system info. Reports are refused if anything private-looking survives. | `test_tester_scripts.py`, OWRT container helper check |
| 7 | Low | Unauthenticated login floods could run many PBKDF2 verifications in parallel (CPU exhaustion on a router). | At most 2 concurrent verifications, plus existing per-address back-off. | — |
| 8 | Low | Malformed provider data (`1e400`, NaN, nested types, invalid ubus replies) raised exceptions that aborted collector loops (availability). | Strict integer parsing (`counters.parse_count`), type-checked ubus parsing, vnStat overflow guard. | malformed-response tests in `test_hardening.py` |
| 9 | Info | Demo mode exposed host filesystem sizes and the demo data path through the Health API. | Simulated filesystems only; host paths masked in API responses. | `test_demo_health_has_no_host_storage_or_paths` |
| 10 | Info | LuCI embedding required LuCI on ports 80/443 (CSP `frame-ancestors`). | Same host, any port. Other origins still cannot frame the dashboard. | `test_static_and_security_headers`, real-LuCI test |
| 11 | Info | TLS certificate directory created world-writable when HTTPS was enabled (procd umask). | Created with umask 077, `chmod 0700`. | OWRT HTTPS check |

## Controls verified

* **Exposure**: binds to the LAN address + loopback by default; `listen 'any'`
  logs a warning; client allow-list (RFC 1918, ULA, link-local, loopback);
  Host header allow-list against DNS rebinding.
* **Authentication**: PBKDF2-HMAC-SHA256 (120 000 iterations, pure-Python
  fallback verified equal to OpenSSL); random 256-bit session tokens, in
  memory, `HttpOnly; SameSite=Strict` (+ `Secure` under HTTPS); 12 h / 4 h
  idle expiry; rate-limited login; LuCI SSO tokens are 192-bit, single use,
  valid 60 s.
* **Authorisation**: every API route except `/api/session`, `/api/login` and
  `/auth/sso` requires a session; all writes need the CSRF header and a
  same-origin `Origin`/`Referer` when present.
* **Input handling**: JSON only, 64 KiB limit, schema-validated settings,
  MAC/interface/date validation, alias character restrictions, static files
  from the web root only (traversal tests), strict CSP without inline code;
  hostile DHCP hostnames and torrent names render as text (Playwright
  `hostile-names` scenario, no script dialogs, no injected elements).
* **No command execution surface**: subprocesses use fixed argument lists
  without a shell (ubus, vnstat, nlbw); ubus object names validated; no
  `eval`/`exec` (`test_no_shell_or_eval_in_application`).
* **Secrets**: `auth.json`/`secrets.json` 0600 (container-verified); never
  returned by the API, logged or included in diagnostics.
* **Privileges**: service runs as `bandwatch`; ubus ACL allows only
  `network.interface` status/dump and `hostapd.*:get_clients` (allowed and
  denied calls verified in the container).
* **Router configuration untouched**: installer adds only its own files,
  user, ubus ACL and init script (`bandwatch-test-round.sh` fingerprints
  firewall/network/DHCP/Wi-Fi/Bandix/aria2 config before and after).

## HTTPS

Optional `option https '1'`: BandWatch creates its own self-signed P-256
certificate with OpenWrt's `px5g` (already present with uhttpd) in
`/etc/bandwatch/tls/` and serves TLS 1.2+ itself (needs `python3-openssl`).
uhttpd, LuCI and their certificates are not modified or reused. TLS
handshakes happen in worker threads, so a slow client cannot block
`accept()`. With HTTPS, the LuCI page embeds the dashboard even when LuCI
itself uses HTTPS. Browsers warn about the self-signed certificate until it
is trusted.

## Residual risks

* Default dashboard transport is plain HTTP on the LAN (session cookie can be
  sniffed on an untrusted LAN) — enable HTTPS where this matters.
* Packages are unsigned unless the maintainer configures a signing key; the
  tester must verify SHA-256 checksums.
* The pure-Python PBKDF2 fallback (without `python3-openssl`) makes each
  login take noticeably longer on ARM routers (not measured on hardware).
* Not reviewed: kernel/firmware, Bandix and aria2 themselves.
