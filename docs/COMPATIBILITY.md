# Compatibility matrix

Status meanings:

* **Container-tested**: automated tests in an OpenWrt rootfs container with
  procd, ubusd and rpcd (x86_64), see `tests/openwrt/run.sh`.
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
| Arcadyan AW1000 (qualcommax/ipq807x, aarch64_cortex-a53) | **First hardware test pending** |
| x86_64 (container) | Container-tested |
| Other targets with ≥ 128 MB RAM | Untested |
| Devices with < 64 MB RAM or < 16 MB flash | Not recommended (Python footprint) |

## WAN types

| WAN | Status | Notes |
| --- | --- | --- |
| Cellular QMI/MBIM (`wwan0`) | Simulated | AW1000 scenario |
| Ethernet / fibre ONT (DHCP/static) | Simulated | |
| PPPoE | Simulated | measured on `pppoe-wan` |
| Multi-WAN (mwan3) | Unit-tested detection | devices summed |
| 6in4 / CLAT / DS-Lite / WireGuard tunnels | Unit-tested detection | ignored when a physical WAN exists |
| Hardware offloading (NSS/PPE) | Untested | may bypass counters — reported in Health |

## Providers

| Provider | Version | Status |
| --- | --- | --- |
| Bandix | 0.12.x API (README v0.12.11 schema) | Fixture + simulation; **real 0.12.12 response must be verified** |
| nlbwmon | `nlbw -c json -g mac` | Unit-tested with fixture output |
| aria2 | 1.35+ JSON-RPC | Simulated and unit-tested |
| vnStat | 2.x JSON (`jsonversion` 1 and 2) | Fixture-tested |
| LuCI | `luci-base` 24.10 / 25.12 JS views, rpcd | rpcd plugin container-tested; view not rendered in a real LuCI yet |

## Browsers

Chromium (automated tests). Firefox and Safari are expected to work
(standard ES2020, CSS `color-mix()` requires Safari ≥ 16.2 / Firefox ≥ 113 /
Chrome ≥ 111). PWA installation requires HTTPS (or localhost).

## Python

Python 3.10+ standard library. CI tests 3.10, 3.11 and 3.13 (OpenWrt 25.12 ships 3.13). The code also parses as 3.9 but 3.9 is not tested.
`python3-openssl` is optional (faster PBKDF2).
