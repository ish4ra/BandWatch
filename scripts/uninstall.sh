#!/bin/sh
# BandWatch uninstaller.
#
#   sh uninstall.sh            remove the program, KEEP data and configuration
#   sh uninstall.sh --purge    also delete /etc/bandwatch (database, settings,
#                              password) and /etc/config/bandwatch
#
# Packages installed with apk/opkg should be removed with the package
# manager instead (apk del bandwatch luci-app-bandwatch); this script also
# handles that case for --purge.

set -u
PURGE=0
YES=0
for a in "$@"; do
	case "$a" in
		--purge) PURGE=1 ;;
		-y|--yes) YES=1 ;;
		-h|--help) sed -n '2,11p' "$0"; exit 0 ;;
		*) echo "unknown option: $a" >&2; exit 1 ;;
	esac
done

[ "$(id -u)" = 0 ] || { echo "must be run as root" >&2; exit 1; }

DATA_DIR=$(uci -q get bandwatch.main.data_dir 2>/dev/null || echo /etc/bandwatch)
case "$DATA_DIR" in
	/|/etc|/usr|/tmp|/overlay|/root|/www|/mnt|"") echo "refusing to purge suspicious data_dir '$DATA_DIR'" >&2; exit 1 ;;
esac

if [ "$PURGE" = 1 ] && [ "$YES" != 1 ]; then
	printf 'This permanently deletes all BandWatch history in %s. Continue? [y/N] ' "$DATA_DIR"
	read -r answer
	case "$answer" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 1 ;; esac
fi

echo "Stopping BandWatch..."
[ -x /etc/init.d/bandwatch ] && { /etc/init.d/bandwatch stop; /etc/init.d/bandwatch disable; } 2>/dev/null
# Wait (up to 20 s) for the process to exit so its port is released.
i=0
while ps w 2>/dev/null | grep -q '[b]andwatch run' && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done

PKG_INSTALLED=0
if command -v apk >/dev/null 2>&1 && apk info -e bandwatch >/dev/null 2>&1; then
	apk del luci-app-bandwatch bandwatch 2>/dev/null; PKG_INSTALLED=1
elif command -v opkg >/dev/null 2>&1 && opkg list-installed 2>/dev/null | grep -q '^bandwatch '; then
	opkg remove luci-app-bandwatch bandwatch 2>/dev/null; PKG_INSTALLED=1
fi

if [ "$PKG_INSTALLED" = 0 ]; then
	if [ -f /usr/lib/bandwatch/MANIFEST ]; then
		while read -r f; do
			case "$f" in /etc/config/bandwatch|"") continue ;; esac
			[ -f "$f" ] && rm -f "$f"
		done < /usr/lib/bandwatch/MANIFEST
	fi
	for f in /etc/init.d/bandwatch /usr/bin/bandwatch /usr/sbin/bandwatch-diag /usr/share/acl.d/bandwatch.json \
		/lib/upgrade/keep.d/bandwatch /usr/libexec/rpcd/luci.bandwatch /usr/share/luci/menu.d/luci-app-bandwatch.json \
		/usr/share/rpcd/acl.d/luci-app-bandwatch.json /www/luci-static/resources/view/bandwatch/dashboard.js; do
		rm -f "$f"
	done
	[ -d /usr/lib/bandwatch ] && rm -r /usr/lib/bandwatch
	rmdir /www/luci-static/resources/view/bandwatch 2>/dev/null
	rm -f /tmp/luci-indexcache /tmp/luci-indexcache.* 2>/dev/null
	/etc/init.d/rpcd reload 2>/dev/null
	killall -HUP ubusd 2>/dev/null
fi
[ -d /var/run/bandwatch ] && rm -r /var/run/bandwatch

if [ "$PURGE" = 1 ]; then
	[ -d "$DATA_DIR" ] && rm -r "$DATA_DIR"
	rm -f /etc/config/bandwatch
	if grep -q '^bandwatch:' /etc/passwd 2>/dev/null; then
		sed -i '/^bandwatch:/d' /etc/passwd /etc/shadow 2>/dev/null
		sed -i '/^bandwatch:/d' /etc/group 2>/dev/null
	fi
	echo "BandWatch and all of its data were removed."
else
	echo "BandWatch was removed. Data kept in $DATA_DIR and /etc/config/bandwatch."
	echo "Run 'sh uninstall.sh --purge' to delete them."
fi
