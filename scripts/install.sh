#!/bin/sh
# BandWatch installer for OpenWrt (installation archive).
#
#   sh install.sh [--dry-run] [--install-deps] [--no-luci] [--no-start] [--port N]
#
# Safe by design:
#   * read-only preflight checks run first; nothing changes if they fail
#   * the previous BandWatch installation (if any) is backed up and restored
#     automatically when any step fails (rollback)
#   * existing /etc/config/bandwatch and data in /etc/bandwatch are preserved
#   * no firewall, DNS, DHCP, Wi-Fi, routing or offloading settings are touched

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
VERSION=$(cat "$HERE/VERSION" 2>/dev/null || echo unknown)
DRY_RUN=0
INSTALL_DEPS=0
WITH_LUCI=auto
START=1
PORT=""
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/tmp/bandwatch-rollback-$STAMP.tar.gz"
NEW_FILES="/tmp/bandwatch-newfiles-$STAMP.txt"
APP_DIR=/usr/lib/bandwatch
DEPS="python3-light python3-sqlite3 python3-urllib"

say() { printf '%s\n' "$*"; }
ok() { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--dry-run) DRY_RUN=1 ;;
		--install-deps) INSTALL_DEPS=1 ;;
		--no-luci) WITH_LUCI=0 ;;
		--no-start) START=0 ;;
		--port) shift; PORT="${1:-}" ;;
		-h|--help) sed -n '2,13p' "$0"; exit 0 ;;
		*) die "unknown option: $1" ;;
	esac
	shift
done

case "$PORT" in ''|*[!0-9]*) [ -n "$PORT" ] && die "--port needs a number";; esac

pkg_manager() {
	if command -v apk >/dev/null 2>&1; then echo apk
	elif command -v opkg >/dev/null 2>&1; then echo opkg
	else echo none; fi
}

port_in_use() {
	# $1 = port. Looks for a listening socket in /proc/net/tcp{,6}.
	hex=$(printf '%04X' "$1")
	for f in /proc/net/tcp /proc/net/tcp6; do
		[ -r "$f" ] || continue
		awk -v p=":$hex" 'NR>1 && $4=="0A" && substr($2, length($2)-4)==p {found=1} END {exit !found}' "$f" && return 0
	done
	return 1
}

# ---------------------------------------------------------------- preflight
say "BandWatch $VERSION installer"
say ""
say "Preflight checks (read-only):"
PROBLEMS=0

[ "$(id -u)" = 0 ] && ok "running as root" || { fail "must be run as root"; PROBLEMS=1; }

if [ -r /etc/openwrt_release ]; then
	. /etc/openwrt_release
	ok "OpenWrt ${DISTRIB_RELEASE:-?} (${DISTRIB_TARGET:-?}, ${DISTRIB_ARCH:-?})"
else
	fail "this does not look like OpenWrt (/etc/openwrt_release missing)"
	PROBLEMS=1
fi

PM=$(pkg_manager)
ok "package manager: $PM"

for f in "$HERE/rootfs/usr/lib/bandwatch/bandwatch/__main__.py" "$HERE/rootfs/etc/init.d/bandwatch"; do
	[ -f "$f" ] || { fail "archive incomplete: missing ${f#$HERE/}"; PROBLEMS=1; }
done
if [ -f "$HERE/SHA256SUMS" ] && command -v sha256sum >/dev/null 2>&1; then
	if (cd "$HERE" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then ok "archive checksums verified"
	else fail "archive checksum mismatch (corrupted download?)"; PROBLEMS=1; fi
fi

MISSING_DEPS=""
if command -v python3 >/dev/null 2>&1; then
	ok "python3 $(python3 -c 'import sys; print(sys.version.split()[0])' 2>/dev/null)"
	for mod in sqlite3 http.server http.client urllib.parse email.utils json hashlib; do
		python3 -c "import $mod" >/dev/null 2>&1 || MISSING_DEPS="$MISSING_DEPS $mod"
	done
	if [ -n "$MISSING_DEPS" ]; then
		warn "missing Python modules:$MISSING_DEPS"
	else
		ok "required Python modules present"
	fi
	python3 -c 'import hashlib; hashlib.pbkdf2_hmac("sha256", b"a", b"b", 1)' >/dev/null 2>&1 \
		&& ok "fast password hashing (python3-openssl)" \
		|| warn "python3-openssl not installed: slower built-in password hashing is used (optional)"
else
	warn "python3 is not installed"
	MISSING_DEPS="python3"
fi

if [ -n "$MISSING_DEPS" ]; then
	if [ "$INSTALL_DEPS" = 1 ] && [ "$PM" != none ]; then
		warn "will install: $DEPS"
	else
		fail "install the dependencies first: $( [ "$PM" = opkg ] && echo "opkg update && opkg install $DEPS" || echo "apk update && apk add $DEPS" ) (or re-run with --install-deps)"
		PROBLEMS=1
	fi
fi

FREE_KB=$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')
[ -n "$FREE_KB" ] || FREE_KB=$(df -k / | awk 'NR==2 {print $4}')
if [ "${FREE_KB:-0}" -ge 3072 ]; then ok "free storage: $((FREE_KB / 1024)) MiB"
else fail "less than 3 MiB free storage"; PROBLEMS=1; fi

EXISTING=""
[ -f "$APP_DIR/VERSION" ] && EXISTING=$(cat "$APP_DIR/VERSION")
[ -n "$EXISTING" ] && ok "existing installation $EXISTING will be upgraded (data kept)"
WAS_RUNNING=0
[ -x /etc/init.d/bandwatch ] && /etc/init.d/bandwatch running >/dev/null 2>&1 && WAS_RUNNING=1

CFG_PORT=$(uci -q get bandwatch.main.port 2>/dev/null)
CHECK_PORT=${PORT:-${CFG_PORT:-8787}}
if port_in_use "$CHECK_PORT"; then
	if [ "$WAS_RUNNING" = 1 ] && [ "$CHECK_PORT" = "${CFG_PORT:-8787}" ]; then ok "port $CHECK_PORT is used by the running BandWatch"
	else fail "TCP port $CHECK_PORT is already used by another program; re-run with --port <free port>"; PROBLEMS=1; fi
else
	ok "port $CHECK_PORT is free"
fi

if [ "$WITH_LUCI" = auto ]; then
	if [ -d /www/luci-static/resources ] && [ -d /usr/share/rpcd/acl.d ]; then WITH_LUCI=1; ok "LuCI detected: menu entry will be installed"
	else WITH_LUCI=0; ok "LuCI not detected: standalone dashboard only"; fi
fi

for svc in bandix vnstat aria2 nlbwmon; do
	[ -x "/etc/init.d/$svc" ] && ok "optional integration available: $svc"
done

if [ "$PROBLEMS" != 0 ]; then
	say ""
	die "preflight checks failed; nothing was changed"
fi
if [ "$DRY_RUN" = 1 ]; then
	say ""
	say "Dry run complete: the system is ready. Nothing was changed."
	exit 0
fi

# ---------------------------------------------------------------- install
rollback() {
	say ""
	say "Installation failed - rolling back..."
	/etc/init.d/bandwatch stop >/dev/null 2>&1
	if [ -f "$NEW_FILES" ]; then
		while read -r f; do [ -n "$f" ] && [ -f "$f" ] && rm -f "$f"; done < "$NEW_FILES"
	fi
	if [ -s "$BACKUP" ]; then
		tar -xzf "$BACKUP" -C / && say "Previous installation restored from $BACKUP"
		[ "$WAS_RUNNING" = 1 ] && /etc/init.d/bandwatch start >/dev/null 2>&1
	else
		rm -f /etc/init.d/bandwatch /usr/bin/bandwatch /usr/sbin/bandwatch-diag
		[ -d "$APP_DIR" ] && [ -f "$APP_DIR/MANIFEST" ] && rm -r "$APP_DIR"
	fi
	rm -f "$NEW_FILES"
	die "rolled back; your data in /etc/bandwatch was not modified"
}

say ""
say "Installing..."

if [ -n "$MISSING_DEPS" ]; then
	if [ "$PM" = apk ]; then apk update && apk add $DEPS || die "dependency installation failed; nothing else was changed"
	else opkg update && opkg install $DEPS || die "dependency installation failed; nothing else was changed"; fi
fi

[ "$WAS_RUNNING" = 1 ] && /etc/init.d/bandwatch stop >/dev/null 2>&1

# Back up the files of the existing installation (code only, never data).
OLD=""
for p in "$APP_DIR" /etc/init.d/bandwatch /usr/bin/bandwatch /usr/sbin/bandwatch-diag /usr/share/acl.d/bandwatch.json \
	/lib/upgrade/keep.d/bandwatch /usr/libexec/rpcd/luci.bandwatch /usr/share/luci/menu.d/luci-app-bandwatch.json \
	/usr/share/rpcd/acl.d/luci-app-bandwatch.json /www/luci-static/resources/view/bandwatch; do
	[ -e "$p" ] && OLD="$OLD ${p#/}"
done
if [ -n "$OLD" ]; then
	tar -czf "$BACKUP" -C / $OLD || rollback
	ok "backup of previous version: $BACKUP"
fi

trap rollback INT TERM

: > "$NEW_FILES"
install_tree() {
	# $1 = source root. Copies files, recording new ones for rollback.
	src="$1"
	(cd "$src" && find . -type f) | while read -r rel; do
		dst="/${rel#./}"
		case "$dst" in
			/etc/config/bandwatch) [ -f "$dst" ] && { say "  keeping existing $dst"; continue; } ;;
		esac
		[ -e "$dst" ] || echo "$dst" >> "$NEW_FILES"
		mkdir -p "$(dirname "$dst")" || exit 1
		cp "$src/$rel" "$dst.bw-new" && mv "$dst.bw-new" "$dst" || exit 1
	done
}

[ -d "$APP_DIR/bandwatch" ] && rm -r "$APP_DIR/bandwatch"
install_tree "$HERE/rootfs" || rollback
if [ "$WITH_LUCI" = 1 ] && [ -d "$HERE/rootfs-luci" ]; then
	install_tree "$HERE/rootfs-luci" || rollback
fi
chmod 755 /etc/init.d/bandwatch /usr/bin/bandwatch /usr/sbin/bandwatch-diag 2>/dev/null
[ -f /usr/libexec/rpcd/luci.bandwatch ] && chmod 755 /usr/libexec/rpcd/luci.bandwatch
(cd "$HERE/rootfs" && find . -type f | sed 's#^\.##') > "$APP_DIR/MANIFEST"
[ "$WITH_LUCI" = 1 ] && [ -d "$HERE/rootfs-luci" ] && (cd "$HERE/rootfs-luci" && find . -type f | sed 's#^\.##') >> "$APP_DIR/MANIFEST"
ok "files installed"
# Test hook used by the automated installer tests to exercise rollback.
[ -n "${BW_INSTALL_TEST_FAIL:-}" ] && { fail "simulated failure (BW_INSTALL_TEST_FAIL)"; rollback; }

if [ -n "$PORT" ]; then
	uci -q set bandwatch.main.port="$PORT" && uci -q commit bandwatch && ok "port set to $PORT"
fi

# Service account, ACL and service.
sh "$HERE/scripts/postinst" >/dev/null 2>&1 || true
if [ "$WITH_LUCI" = 1 ]; then sh "$HERE/scripts/luci-postinst" >/dev/null 2>&1 || true; fi
if [ "$START" = 0 ]; then
	/etc/init.d/bandwatch stop >/dev/null 2>&1
	trap - INT TERM
	rm -f "$NEW_FILES"
	ok "installed (not started; run /etc/init.d/bandwatch start)"
	exit 0
fi

# Health check.
PORT_NOW=$(uci -q get bandwatch.main.port || echo 8787)
i=0
HEALTHY=0
while [ $i -lt 25 ]; do
	if wget -q -T 2 -O /dev/null "http://127.0.0.1:$PORT_NOW/api/session" 2>/dev/null; then HEALTHY=1; break; fi
	sleep 1
	i=$((i + 1))
done
if [ "$HEALTHY" != 1 ]; then
	fail "BandWatch did not answer on port $PORT_NOW; recent log:"
	logread -e bandwatch 2>/dev/null | tail -n 15
	rollback
fi
trap - INT TERM
rm -f "$NEW_FILES"
ok "service running and answering on port $PORT_NOW"

DATA_DIR=$(uci -q get bandwatch.main.data_dir || echo /etc/bandwatch)
LAN_IP=$(uci -q get network.lan.ipaddr | cut -d/ -f1)
say ""
say "BandWatch $VERSION is installed."
say "  Dashboard:  http://${LAN_IP:-<router-ip>}:$PORT_NOW/"
[ "$WITH_LUCI" = 1 ] && say "  LuCI:       Status -> BandWatch (signs you in automatically)"
if [ ! -s "$DATA_DIR/auth.json" ]; then
	PW=$(bandwatch passwd --random 2>/dev/null)
	if [ -n "$PW" ]; then
		say "  Password:   $PW"
		say "              (change it in Settings or with 'bandwatch passwd')"
	fi
fi
say "  Diagnostics: bandwatch-diag     Uninstall: sh uninstall.sh [--purge]"
[ -s "$BACKUP" ] && say "  Previous version backup: $BACKUP"
exit 0
