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
  link-local and loopback; CGNAT 100.64.0.0/10 only if added explicitly)
  even if a firewall rule is wrong.
* Rejects unexpected `Host` headers (DNS-rebinding protection).
* Makes no outbound connections except to the Bandix/aria2 endpoints on the
  router itself (loopback only, unless the administrator sets
  `option allow_lan_providers '1'`). No telemetry, update checks or CDNs.
* Optional HTTPS (`option https '1'`) with a self-signed certificate created
  by OpenWrt's `px5g`; uhttpd/LuCI are not touched.

**Authentication**
* Password (PBKDF2-HMAC-SHA256, 120 000 iterations, random salt) stored in
  `/etc/bandwatch/auth.json` (mode 0600), or LuCI single sign-on through an
  rpcd plugin that requires *write* access in the `luci-app-bandwatch` ACL
  and issues one-time tokens valid for 60 seconds.
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

The latest audit and its findings are in
[docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).

**Diagnostic report and update check (0.1.0-beta.2)**
* The one-click diagnostic report (`/api/diagnostics/report.zip|.json`,
  `/api/diagnostics/checks`) requires an authenticated session, is served
  only to the LAN/loopback clients already allowed, and is never uploaded.
* It is built from an explicit allow-list (`bandwatch/report.py`): every
  field is validated individually (enums, numbers, short version-like
  tokens). Collector messages, notes, provider replies, event messages,
  settings values that can hold URLs, file paths and device identities are
  never copied; collectors expose machine-readable codes with fixed
  descriptions. Listening and provider addresses are reduced to scopes
  (loopback, LAN, wildcard, public). `tests/test_report_privacy.py` plants
  secrets, addresses, MACs, SSIDs, PPPoE/APN credentials, file names and
  script payloads in every input and checks that none reaches the report.
* The manual update check runs in the browser, only when the user presses
  the button, against `api.github.com/repos/ish4ra/BandWatch/releases`
  (CSP `connect-src` allows only that extra origin) with no cookies or
  referrer. Release notes are rendered as text; release links must point to
  the official repository. Nothing is installed automatically and the router
  makes no outbound request.
* There is no shell or command-execution endpoint; diagnostics only read
  BandWatch's own state.

## Known limitations

* The dashboard uses plain HTTP on the LAN by default. Anyone who can sniff
  LAN traffic could capture a session cookie. Enable `option https '1'` or
  use a trusted LAN.
* Beta packages are unsigned unless the maintainer configures the
  `BANDWATCH_APK_SIGN_KEY_PEM` CI secret; verify `SHA256SUMS` before
  installing.
* Sessions are invalidated on service restart (by design).
