# Compatibility matrix

Verification levels, from weakest to strongest:

1. **Automated unit and integration tests**: backend on Python 3.10, 3.11
   and 3.13, and the dashboard in Chromium, WebKit and Firefox (Firefox:
   [#2](https://github.com/ish4ra/BandWatch/issues/2)).
2. **OpenWrt container tests**: real procd, ubusd, rpcd, uhttpd, LuCI and
   apk/opkg in OpenWrt 25.12 and 24.10 x86-64 rootfs containers.
3. **OpenWrt SDK build validation**: the feed Makefiles build with the
   25.12 SDK.
4. **Physical router verification**: **one diagnostic snapshot** from an
   Arcadyan AW1000 (production mode, November 2026). That snapshot confirms
   installation and provider connectivity on real hardware. It is **not**
   hardware acceptance: traffic-accounting accuracy, sustained performance,
   WAN isolation and reboot recovery were not established by it.

Status meanings in the tables below:

* **Container-tested**: automated tests in an OpenWrt rootfs container with
  procd, ubusd and rpcd (x86_64), see `tests/openwrt/run.sh`.
* **Snapshot-observed**: seen working in one diagnostic snapshot from a
  physical router; not yet verified by the hardware checklist.
* **Simulated**: exercised with the demo platform and unit-test fixtures only.
* **Untested**: expected to work by design, never run.

Nothing is marked *officially supported* until it passes the
[hardware checklist](HARDWARE-TESTING.md).

## OpenWrt releases

| Release | Package format | Status |
| --- | --- | --- |
| 25.12.x | apk v3 (`.apk`) + installer archive | Container-tested (25.12.0 rootfs, apk 3.0.2) |
| 24.10.x | opkg (`.ipk`) + installer archive | Container-tested (24.10.4 rootfs) |
| 23.05.x and older | opkg | Untested (needs Python ≥ 3.10 packages; 23.05 ships 3.11) |

## Hardware / targets

BandWatch is architecture independent (pure Python, `noarch`/`all`).

| Device / target | Status |
| --- | --- |
| Arcadyan AW1000 (qualcommax/ipq807x, aarch64_cortex-a53), OpenWrt 25.12.5, kernel 6.12.94 | **Snapshot-observed** (beta.1): installed, production mode, all six collectors OK, no loop errors. Checklist not yet completed |
| x86_64 (container) | Container-tested |
| Other targets with ≥ 128 MB RAM | Untested |
| Devices with < 64 MB RAM or < 16 MB flash | Not recommended (Python footprint) |

## WAN types

| WAN | Status | Notes |
| --- | --- | --- |
| Cellular via ModemManager (`proto modemmanager`, `wwan0`) | Snapshot-observed (AW1000, IPv4) + unit-tested | detected via ubus; `_4`/`_6` sub-interfaces counted once |
| Cellular QMI/MBIM (`wwan0`, uqmi/umbim) | Simulated | AW1000 scenario |
| Ethernet / fibre ONT (DHCP/static) | Simulated | |
| PPPoE | Simulated | measured on `pppoe-wan` |
| Multi-WAN (mwan3) | Unit-tested detection | devices summed |
| 6in4 / CLAT / DS-Lite / WireGuard tunnels | Unit-tested detection | ignored when a physical WAN exists |
| Hardware offloading (NSS/PPE) | Untested | may bypass counters — reported in Health |

## Providers

| Provider | Version | Status |
| --- | --- | --- |
| Bandix | 0.12.x API (README v0.12.11 schema; upstream LuCI labels rx = Download) | **0.12.12 snapshot-observed** (8 devices reported and identified). rx/tx direction: *insufficient data* in the snapshot - still to be verified (the dashboard now shows progress) |
| nlbwmon | `nlbw -c json -g mac` | Unit-tested with fixture output |
| aria2 | 1.35+ JSON-RPC | **1.37.0 snapshot-observed**: RPC connected without a secret, 0 active / 0 waiting. Download-accounting accuracy not yet verified. With and without RPC secret unit-tested |
| vnStat | 2.x JSON (`jsonversion` 1 and 2) | Fixture-tested; available and queried on demand in the AW1000 snapshot |
| LuCI | `luci-base` 25.12 JS views, rpcd | End-to-end tested in a real LuCI 25.12 (uhttpd + rpcd) container: menu, embedded SSO, new-tab SSO, token replay rejected; 24.10 LuCI not yet rendered |

## Packaging

| Item | Status |
| --- | --- |
| Feed Makefiles (`openwrt/bandwatch`, `openwrt/luci-app-bandwatch`) | Built with the OpenWrt 25.12.0 SDK (x86-64); SDK packages installed and ran in the 25.12 container |
| apk v3 packages (`scripts/build.py`) | Installed with OpenWrt apk 3.0.2; optional signing verified with OpenWrt's apk using a throwaway key |
| ipk packages | Installed with opkg on 24.10.4 |

## Browsers

| Browser | Status |
| --- | --- |
| Chromium-based (Chrome, Edge, Brave) | Automated end-to-end tests pass (locally and in CI). **Recommended for the first hardware test round.** |
| Safari / WebKit | Automated end-to-end tests pass (WebKit, CI only) |
| Firefox | **Not verified.** The automated suite stalls intermittently under Playwright ([#2](https://github.com/ish4ra/BandWatch/issues/2)); whether real Firefox users are affected is not yet known |

Safari on iOS/macOS is expected to work
(standard ES2020, CSS `color-mix()` requires Safari ≥ 16.2 / Firefox ≥ 113 /
Chrome ≥ 111). PWA installation requires HTTPS (or localhost).

## Accounting caveats

* WAN monitoring works on OpenWrt routers whose WAN traffic passes through
  normal Linux interface counters.
* Per-device monitoring requires a provider (Bandix or nlbwmon).
* Hardware or software flow offloading can bypass the counters some
  providers rely on, so per-device totals may be lower than the WAN total.
* aria2 history is an estimate of payload traffic, built from aria2's own
  counters.
* Resource use on router hardware: one snapshot only (AW1000: 0.6 % CPU,
  ~17.2 MiB RSS). Sustained averages come from the built-in self-monitoring
  once testers send reports; see [PERFORMANCE.md](PERFORMANCE.md).
* Packages are unsigned unless the maintainer configures a trusted signing
  key; verify `SHA256SUMS`.

## Python

Python 3.10+ standard library. CI tests 3.10, 3.11 and 3.13 (OpenWrt 25.12 ships 3.13). The code also parses as 3.9 but 3.9 is not tested.

`python3-openssl` is optional. Without it (as on the AW1000 snapshot, where
`ssl` and `_hashlib` were missing):

* everything except BandWatch's own HTTPS mode works; password hashing uses
  a slower pure-Python PBKDF2 (same security);
* the update check still works, because it runs in the browser;
* `option https '1'` refuses to start with a clear message instead of
  falling back to anything insecure. Install `python3-openssl` to use it.
