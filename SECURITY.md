# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
**Security → Report a vulnerability** (private advisory) on this
repository. Do not open public issues for security problems. Include the
BandWatch version, OpenWrt release and steps to reproduce. You can expect an
acknowledgement within 7 days.

Supported versions: the latest beta (currently 0.1.0-beta.1).

## Security model

BandWatch is a local-first application for the LAN side of a router.

**Network exposure**
* Listens on the LAN address and 127.0.0.1 by default (`option listen 'lan'`),
  never on all addresses unless explicitly configured.
* Rejects clients outside `allowed_networks` (default: private, ULA,
  link-local, loopback and CGNAT ranges) even if a firewall rule is wrong.
* Rejects unexpected `Host` headers (DNS-rebinding protection).
* Makes no outbound connections except to the local Bandix/aria2
  endpoints configured by the administrator (restricted to loopback/private
  addresses). No telemetry, update checks or CDNs.

**Authentication**
* Password (PBKDF2-HMAC-SHA256, 120 000 iterations, random salt) stored in
  `/etc/bandwatch/auth.json` (mode 0600), or LuCI single sign-on through an
  rpcd plugin that requires the `luci-app-bandwatch` ACL and issues one-time
  tokens valid for 60 seconds.
* Sessions: random 256-bit tokens, `HttpOnly`, `SameSite=Strict`, 12 h max /
  4 h idle, kept in memory only.
* Failed logins are rate limited per client address (exponential back-off).
* `auth 'none'` is only accepted when listening on loopback.

**Requests**
* State-changing requests need a per-session CSRF token header and a
  same-origin `Origin`/`Referer` when present.
* JSON only, 64 KiB body limit, socket timeouts, bounded worker threads and
  live streams.
* Strict input validation (settings schema, MAC/interface/date formats,
  aliases without control characters or markup).
* Static files are served only from the web root with an extension
  allowlist; path traversal is rejected.
* Content-Security-Policy without `unsafe-inline`/`unsafe-eval`; all
  dynamic text is HTML-escaped; `nosniff`, `no-referrer`, no server version.
* No endpoint executes commands; there is no shell access through BandWatch.

**Privileges**
* The service runs as the unprivileged `bandwatch` user (procd). A ubus ACL
  grants read-only access to `network.interface` status and
  `hostapd.*:get_clients`.
* The rpcd plugin (root) only reports status and writes token files.
* Secrets (aria2 RPC secret, password hash) are stored in 0600 files, never
  returned by the API, logged, or included in diagnostics.

**Router configuration**
* BandWatch never modifies firewall, DNS, DHCP, Wi-Fi, routing, offloading,
  Bandix, vnStat or aria2 configuration, and never flashes firmware. The
  installer only adds its own files, user, ubus ACL and init script.

## Known limitations

* The dashboard uses plain HTTP on the LAN. Anyone who can sniff LAN
  traffic could capture a session cookie. Use a trusted LAN, or put
  BandWatch behind an HTTPS reverse proxy you control.
* Beta packages are unsigned; verify `SHA256SUMS` before installing.
* Sessions are invalidated on service restart (by design).
