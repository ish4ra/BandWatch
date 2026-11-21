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
# purge_dir_problem DIR: prints why DIR must not be deleted by --purge, or
# nothing when it is a safe BandWatch data directory. Rules:
#  - an absolute, canonical path: no "..", no symbolic link anywhere in it
#  - a real directory named "bandwatch" or "bandwatch-*" / "bandwatch_*",
#    not directly under /, not inside system locations, not a mount point
#  - owned by root or the bandwatch user
#  - containing only files BandWatch creates (database, settings, auth,
#    secrets, TLS certificate); anything else means it is not ours
purge_dir_problem() {
	d=$1
	case "$d" in
		"") echo "data_dir is empty"; return ;;
		/*) ;;
		*) echo "'$d' is not an absolute path"; return ;;
	esac
	case "$d" in */../*|*/..|*/./*|*/.|*//*) echo "'$d' is not a canonical path"; return ;; esac
	d=${d%/}
	[ -n "$d" ] || { echo "'/' is not a data directory"; return; }
	# Shape of the path first (also for directories that do not exist yet).
	case "${d##*/}" in
		bandwatch|bandwatch-?*|bandwatch_?*) ;;
		*) echo "'$d' is not named 'bandwatch'"; return ;;
	esac
	parent=${d%/*}
	case "$parent" in
		"") echo "'$d' is directly under /"; return ;;
		/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/rom|/rom/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/www|/www/*|/boot|/boot/*|/var/run|/var/run/*|/run|/run/*)
			echo "'$d' is inside a system location"; return ;;
	esac
	[ -e "$d" ] || [ -L "$d" ] || return 0
	[ -L "$d" ] && { echo "'$d' is a symbolic link"; return; }
	[ -d "$d" ] || { echo "'$d' is not a directory"; return; }
	canon=$(readlink -f "$d" 2>/dev/null)
	[ "$canon" = "$d" ] || { echo "'$d' resolves to '$canon' (a symbolic link in the path)"; return; }
	if awk -v m="$d" '$2 == m { found = 1 } END { exit !found }' /proc/mounts 2>/dev/null; then
		echo "'$d' is a mount point"; return
	fi
	owner=$(ls -ldn "$d" | awk '{print $3}')
	bw_uid=$(awk -F: '$1 == "bandwatch" { print $3 }' /etc/passwd 2>/dev/null)
	if [ "$owner" != 0 ] && [ "$owner" != "$bw_uid" ] && [ "$owner" != "$(id -u)" ]; then
		echo "'$d' is owned by uid $owner (not root or bandwatch)"; return
	fi
	unknown=""
	for e in "$d"/* "$d"/.[!.]* "$d"/..?*; do
		[ -e "$e" ] || [ -L "$e" ] || continue
		n=${e##*/}
		if [ -L "$e" ]; then unknown="$unknown $n(link)"; continue; fi
		case "$n" in
			bandwatch.db|bandwatch.db-wal|bandwatch.db-shm|bandwatch.db-journal|bandwatch.db.corrupt-*) [ -f "$e" ] || unknown="$unknown $n" ;;
			bandwatch-demo.db|bandwatch-demo.db-*|settings.json|auth.json|secrets.json|.tmp-*) [ -f "$e" ] || unknown="$unknown $n" ;;
			tls)
				if [ -d "$e" ]; then
					for t in "$e"/* "$e"/.[!.]*; do
						[ -e "$t" ] || [ -L "$t" ] || continue
						case "${t##*/}" in cert.pem|key.pem|.tmp-*) [ -f "$t" ] && [ ! -L "$t" ] || unknown="$unknown tls/${t##*/}" ;; *) unknown="$unknown tls/${t##*/}" ;; esac
					done
				else
					unknown="$unknown tls"
				fi ;;
			*) unknown="$unknown $n" ;;
		esac
	done
	[ -z "$unknown" ] || echo "'$d' contains files BandWatch did not create:$unknown"
}

for a in "$@"; do
	case "$a" in
		--purge) PURGE=1 ;;
		-y|--yes) YES=1 ;;
		--check-purge-dir=*)
			# Validation only (used by the tests); changes nothing.
			why=$(purge_dir_problem "${a#*=}")
			[ -z "$why" ] && { echo "OK"; exit 0; }
			echo "REFUSE: $why"; exit 2 ;;
		-h|--help) sed -n '2,11p' "$0"; exit 0 ;;
		*) echo "unknown option: $a" >&2; exit 1 ;;
	esac
done

[ "$(id -u)" = 0 ] || { echo "must be run as root" >&2; exit 1; }

DATA_DIR=$(uci -q get bandwatch.main.data_dir 2>/dev/null || echo /etc/bandwatch)
if [ "$PURGE" = 1 ]; then
	why=$(purge_dir_problem "$DATA_DIR")
	if [ -n "$why" ]; then
		echo "refusing to purge: $why" >&2
		echo "Nothing was removed. Delete the data directory manually after checking it, or run without --purge." >&2
		exit 1
	fi
fi

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
	# Re-validated right before deleting; rm -r never follows symbolic links.
	if [ -d "$DATA_DIR" ] && [ -z "$(purge_dir_problem "$DATA_DIR")" ]; then
		rm -r "${DATA_DIR%/}"
	fi
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
