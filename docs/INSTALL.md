# Installing BandWatch

This guide assumes no prior OpenWrt experience. Every step that changes
something on the router is marked **(changes the router)**. Steps not marked
only read information.

> BandWatch v0.1.0-beta.1 is a **beta**. It has been tested in OpenWrt
> 24.10 / 25.12 containers but not yet on physical hardware. Keep a backup of
> your router configuration (LuCI → System → Backup / Flash Firmware →
> *Generate archive*) before installing beta software.

## 1. What you need

| Item | Notes |
| --- | --- |
| OpenWrt **25.12** (apk) or **24.10** (opkg) | Older releases may work but are untested |
| Python 3 packages | `python3-light`, `python3-sqlite3`, `python3-urllib` (~6–8 MB of flash). `python3-openssl` is optional (faster password checks) |
| ~3 MB free storage | for BandWatch itself; its database grows to a few MB over months |
| SSH access | from Windows use the built-in `ssh`/`scp` in PowerShell, or PuTTY/WinSCP |
| Optional | Bandix (per-device usage), aria2 with RPC enabled, vnStat, LuCI |

Check your version and free space:

```sh
ssh root@192.168.1.1          # use your router's address
cat /etc/openwrt_release
df -h /overlay
```

## 2. Download

Download from the public release page (no GitHub account needed):
**https://github.com/ish4ra/BandWatch/releases/tag/v0.1.0-beta.1**

| File | Use it for |
| --- | --- |
| `bandwatch-0.1.0-beta.1-tester-bundle.zip` | Hardware testers: everything below plus guides, checklist and test-round helper |
| `bandwatch-0.1.0-beta.1-openwrt.tar.gz` | **Recommended.** Installer with checks, backup and rollback |
| `bandwatch-0.1.0_beta1-r1.apk`, `luci-app-bandwatch-0.1.0_beta1-r1.apk` | OpenWrt 25.12 package manager |
| `bandwatch_0.1.0-beta1-1_all.ipk`, `luci-app-bandwatch_0.1.0-beta1-1_all.ipk` | OpenWrt 24.10 package manager (inside the tester bundle they are named `..._0.1.0~beta1-1_all.ipk`) |
| `SHA256SUMS` | Integrity check |

The packages are **unsigned** for this beta, so verify the download on your
computer first:

* Linux: `sha256sum -c SHA256SUMS --ignore-missing`
* macOS: `shasum -a 256 bandwatch-0.1.0-beta.1-openwrt.tar.gz`, then compare with the line in `SHA256SUMS`
* Windows PowerShell: `Get-FileHash bandwatch-0.1.0-beta.1-openwrt.tar.gz`, then compare with the line in `SHA256SUMS`

Use a Chromium-based browser (Chrome, Edge, Brave) or Safari for the
dashboard during this beta. Firefox is not yet verified
([#2](https://github.com/ish4ra/BandWatch/issues/2)).

## 3. Install with the installer (recommended)

```sh
# on your computer
scp -O bandwatch-0.1.0-beta.1-openwrt.tar.gz root@192.168.1.1:/tmp/

# on the router
cd /tmp
tar -xzf bandwatch-0.1.0-beta.1-openwrt.tar.gz
cd bandwatch-0.1.0-beta.1
sh install.sh --dry-run
```

The dry run only reads the system and prints `[ OK ]`, `[WARN]` or `[FAIL]`
lines. Fix any `[FAIL]` first. If Python is missing it shows the exact
command, for example `apk update && apk add python3-light python3-sqlite3 python3-urllib`.

Then **(changes the router)**:

```sh
sh install.sh                  # or: sh install.sh --install-deps
```

The installer:

1. verifies archive checksums and repeats the checks;
2. backs up any previous BandWatch version to `/tmp/bandwatch-rollback-*.tar.gz`;
3. copies files, creates the unprivileged `bandwatch` user, loads its ubus
   permission file and enables the service;
4. waits until the dashboard answers — if it does not, it **rolls back**
   automatically;
5. prints the dashboard address and a random password.

Options: `--port 8790` (different port), `--no-luci`, `--no-start`,
`--install-deps`.

It never changes firewall, DNS, DHCP, Wi-Fi, routing, offloading or other
monitoring tools.

## 4. Install with the package manager (alternative)

OpenWrt 25.12:

```sh
apk update && apk add python3-light python3-sqlite3 python3-urllib
apk add --allow-untrusted /tmp/bandwatch-0.1.0_beta1-r1.apk /tmp/luci-app-bandwatch-0.1.0_beta1-r1.apk
bandwatch passwd
```

OpenWrt 24.10:

```sh
opkg update && opkg install python3-light python3-sqlite3 python3-urllib
opkg install /tmp/bandwatch_0.1.0*_all.ipk /tmp/luci-app-bandwatch_0.1.0*_all.ipk
bandwatch passwd
```

`--allow-untrusted` is needed because beta packages are not signed yet. If
the maintainer publishes signed packages, install the provided public key
once (`cp bandwatch.pem /etc/apk/keys/`) and omit `--allow-untrusted`.

## 5. First use

* Open `http://<router-ip>:8787/` and sign in, **or** open
  **LuCI → Status → BandWatch** (signs you in automatically).
* Change the password in **Settings → Security** or with `bandwatch passwd`.
* Check **Health → Collectors**: WAN should be *Working*. Per-device data
  needs Bandix (or nlbwmon); aria2 needs its RPC secret in
  **Settings → aria2**.
* Set your timezone source and (optionally) a monthly quota in Settings.

## 6. Configuration

Service options live in `/etc/config/bandwatch` (port, listen addresses,
allowed client networks, data directory, service user). After editing:

```sh
/etc/init.d/bandwatch restart
```

Everything else is configured in the dashboard and stored in
`/etc/bandwatch/settings.json`.

Optional settings in `/etc/config/bandwatch`:

* `option https '1'` serves the dashboard over HTTPS with a self-signed
  certificate that BandWatch creates with OpenWrt's `px5g` (requires
  `python3-openssl`; uhttpd/LuCI are not changed). Your browser will ask you
  to accept the certificate once.
* `option allow_lan_providers '1'` allows Bandix/aria2 endpoints on another
  LAN host. By default only endpoints on the router itself are accepted.
* `option port '8790'` changes the dashboard port.

To keep flash writes low on very small routers you can move the data
directory to USB storage (`option data_dir '/mnt/usb/bandwatch'`). Note that
a database on a spinning disk keeps that disk awake.

## 7. Upgrade

Run the newer installer the same way; data, settings and password are kept,
and the previous version is backed up. With packages: `apk add --allow-untrusted <new.apk>`
or `opkg install <new.ipk>`.

`/etc/config/bandwatch` and `/etc/bandwatch/` are listed in
`/lib/upgrade/keep.d/bandwatch`, so they survive a firmware sysupgrade that
keeps settings (Python and BandWatch must be reinstalled afterwards).

## 8. Uninstall

```sh
cd /tmp/bandwatch-0.1.0-beta.1
sh uninstall.sh              # removes the program, KEEPS history and settings
sh uninstall.sh --purge      # also deletes /etc/bandwatch and /etc/config/bandwatch
```

`--purge` only deletes a genuine BandWatch data directory. The path must be
absolute and canonical with no symbolic link anywhere in it. It must be
named `bandwatch` (or `bandwatch-*`), must not be a system location or a
mount point, must be owned by root or the `bandwatch` user, and may only
contain files BandWatch creates. Otherwise it stops **without removing
anything** and explains why, so you can check the directory and delete it
by hand.

Package installs: `apk del luci-app-bandwatch bandwatch` or
`opkg remove luci-app-bandwatch bandwatch` (data is kept).

## 8a. Hardware test round (beta testers)

`/tmp` is RAM on OpenWrt: a reboot deletes it, together with the test helper
and any report in it. So the order matters:

1. `sh /tmp/bandwatch-test-round.sh before` (before installing), install, test.
2. `sh /tmp/bandwatch-test-round.sh after`, then `sh /tmp/bandwatch-test-round.sh perf`,
   then `sh /tmp/bandwatch-test-round.sh report`.
3. **On your computer**, copy the report and check that it arrived:
   `scp -O "root@192.168.1.1:/tmp/bandwatch-test-reports/*.tar.gz" .`
4. Only then reboot the router (`reboot`). Afterwards copy
   `bandwatch-test-round.sh` to `/tmp` again, run
   `sh /tmp/bandwatch-test-round.sh postreboot` and copy its report the same way.

**WAN isolation.** The conclusive check is local. `after` and `postreboot`
print where BandWatch listens (`VERDICT: PASS` means loopback and LAN only),
and `netstat -ltn | grep 8787` should show only `127.0.0.1:8787` and the LAN
address. An outside test from mobile data proves nothing when the router's
WAN address is behind CGNAT or private (`100.64.0.0/10`, `10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`): the router cannot be reached from
outside at all, firewall or not. The full procedure, in Sinhala, is in
[INSTALL-SINHALA.md](INSTALL-SINHALA.md), and the checklist in
[HARDWARE-TESTING.md](HARDWARE-TESTING.md).

## 9. Getting help

Run `bandwatch-diag` and attach the generated file (review it first — it is
already sanitised) to a GitHub issue. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
