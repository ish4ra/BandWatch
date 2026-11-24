# Upgrading BandWatch (0.1.0-beta.1 → 0.1.0-beta.2)

You do **not** need to uninstall beta.1 or delete anything. The upgrade keeps:

* your traffic history (the SQLite database),
* settings, device names (aliases), retention and quota settings,
* the dashboard password and active LuCI integration,
* the aria2 integration settings and RPC secret,
* `/etc/config/bandwatch` (port, listen address, HTTPS options and certificate).

The database format did not change (schema 1), so you can go back to beta.1
at any time without losing data (see *Rolling back*).

Sinhala version: [UPGRADE-SINHALA.md](UPGRADE-SINHALA.md).

## 1. Download

From the [v0.1.0-beta.2 release](https://github.com/ish4ra/BandWatch/releases/tag/v0.1.0-beta.2)
(no GitHub account needed) download `bandwatch-0.1.0-beta.2-openwrt.tar.gz`
and `SHA256SUMS`. Check the file before copying it to the router:

* Windows PowerShell: `Get-FileHash bandwatch-0.1.0-beta.2-openwrt.tar.gz`
* macOS / Linux: `shasum -a 256 bandwatch-0.1.0-beta.2-openwrt.tar.gz`

The value must match the line for that file in `SHA256SUMS` exactly.

## 2. Upgrade with the installer (recommended, same as the first install)

On your computer:

```bash
scp -O bandwatch-0.1.0-beta.2-openwrt.tar.gz root@192.168.1.1:/tmp/
```

On the router (`ssh root@192.168.1.1`):

```bash
cd /tmp && tar -xzf bandwatch-0.1.0-beta.2-openwrt.tar.gz && cd bandwatch-0.1.0-beta.2 && sh install.sh
```

The installer says `existing installation 0.1.0-beta.1 will be upgraded (data
kept)`, saves a backup of the old program files, installs, restarts BandWatch
and checks that it answers. If anything fails it restores beta.1
automatically. No password is printed on an upgrade: your existing password
stays valid.

### Alternative: package manager

If you installed beta.1 with `apk` (OpenWrt 25.12) or `opkg` (24.10 and
older), upgrade the same way, without removing the old packages first:

```bash
apk add --allow-untrusted /tmp/bandwatch-0.1.0_beta2-r1.apk /tmp/luci-app-bandwatch-0.1.0_beta2-r1.apk
```

```bash
opkg install /tmp/bandwatch_0.1.0-beta2-1_all.ipk /tmp/luci-app-bandwatch_0.1.0-beta2-1_all.ipk
```

(`--allow-untrusted` is needed because the beta packages are not signed by
OpenWrt; that is why you verify `SHA256SUMS`.)

## 3. Check the upgrade

Open the dashboard (or LuCI → Status → BandWatch) and reload the page once
(Ctrl+F5) so the browser picks up the new files. Then open **Health**:

* **Database & history** shows *Started: first start after upgrade* and your
  existing rows. Right after the restart it may say *Earlier history is
  stored; data collected since this start is saved for the first time in
  about N s* — that is normal.
* Press **Download Diagnostic Report** and keep the ZIP file if the
  maintainer asked for one.

## Rolling back to beta.1

Run the beta.1 installer again (`sh install.sh` in the extracted
`bandwatch-0.1.0-beta.1` folder), or install the beta.1 packages. Your
history and settings stay. beta.2 adds one small table (`health`, BandWatch's
own resource history); beta.1 ignores it.

## What changed

See the [release notes](release-notes/v0.1.0-beta.2.md) and
[CHANGELOG](../CHANGELOG.md).
