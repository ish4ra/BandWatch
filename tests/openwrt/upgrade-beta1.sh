#!/bin/sh
# Upgrade test from the PUBLISHED 0.1.0-beta.1 release to the current build.
#
#   tests/openwrt/upgrade-beta1.sh <openwrt-rootfs-tag> <dir with beta.1 release assets>
#
# The beta.1 assets are downloaded by CI from the GitHub release and verified
# against its SHA256SUMS before this script runs. Two paths are tested:
#
# 1. Release archive installer: beta.1 install -> seed data with beta.1's own
#    code -> upgrade -> verify -> roll back to beta.1 -> verify -> re-upgrade.
# 2. Package manager (apk on 25.12, opkg on 24.10): beta.1 packages ->
#    upgrade to the new packages -> verify.
#
# Requires Docker and dist/ artefacts (python3 scripts/build.py).

set -eu
TAG=${1:-x86-64-25.12.0}
B1=$(cd "${2:?beta.1 asset directory}" && pwd)
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMG="bandwatch-owrt-test:$TAG"
NAME="bw-up-$$"
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/bandwatch/__init__.py")
OLD=0.1.0-beta.1
FAILS=0
WAIT_UP='i=0; until /etc/init.d/bandwatch running && wget -q -T 2 -O /dev/null http://127.0.0.1:8787/api/session; do i=$((i+1)); [ $i -ge 40 ] && exit 1; sleep 1; done'
H="python3 /t/upgrade_helper.py"

pass() { echo "PASS: $*"; }
failt() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
check() {
	desc="$1"; shift
	if docker exec "$NAME" sh -c "$*" >/tmp/bw-up-out.$$ 2>&1; then
		pass "$desc"
	else
		failt "$desc"
		sed 's/^/    /' /tmp/bw-up-out.$$ | grep -v '^\s*~*\^*$' | tail -40
		echo "    --- service state, files, recent log:"
		docker exec "$NAME" sh -c '/etc/init.d/bandwatch running && echo running || echo "not running"; ls -la /etc/bandwatch /var/run/bandwatch; logread | tail -n 40' 2>&1 | sed 's/^/    /'
	fi
}
boot() {
	docker rm -f "$NAME" >/dev/null 2>&1 || true
	docker run -d --name "$NAME" --privileged --tmpfs /tmp:exec -v "$ROOT/dist:/dist:ro" -v "$B1:/beta1:ro" -v "$ROOT/tests/openwrt:/t:ro" "$IMG" /sbin/init >/dev/null
	sleep 8
}

docker build -q --build-arg OPENWRT_TAG="$TAG" -t "$IMG" "$ROOT/tests/openwrt" >/dev/null
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -f /tmp/bw-up-out.$$; }
trap cleanup EXIT

# -- 1. release archive installer -------------------------------------------------------
boot
check "install published beta.1" "cd /tmp && tar -xzf /beta1/bandwatch-$OLD-openwrt.tar.gz && cd bandwatch-$OLD && sh install.sh >/tmp/i1.log && cat /usr/lib/bandwatch/VERSION | grep -qx $OLD"
check "seed history, alias, settings, secret, password with beta.1 code" "/etc/init.d/bandwatch stop; uci set bandwatch.main.log_level=debug && uci commit bandwatch && $H seed && /etc/init.d/bandwatch start && $WAIT_UP && sleep 3 && $H verify $OLD"
check "upgrade to $VER without uninstalling (installer)" "cd /tmp && tar -xzf /dist/bandwatch-$VER-openwrt.tar.gz && cd bandwatch-$VER && sh install.sh | tee /tmp/i2.log | grep -q 'is installed' && grep -q 'will be upgraded' /tmp/i2.log"
check "data, password, settings, aliases and config kept; lifecycle says upgrade" "$WAIT_UP && $H verify $VER upgrade && [ \"\$(uci get bandwatch.main.log_level)\" = debug ] && test -f /www/luci-static/resources/view/bandwatch/dashboard.js"
check "roll back to beta.1 (installer) keeps data" "/etc/init.d/bandwatch stop; cd /tmp/bandwatch-$OLD && sh install.sh >/tmp/i3.log && $WAIT_UP && sleep 2 && $H verify $OLD"
check "upgrade again after rollback" "cd /tmp/bandwatch-$VER && sh install.sh >/tmp/i4.log && $WAIT_UP && $H verify $VER restart"

# -- 2. package manager ------------------------------------------------------------------
boot
if docker exec "$NAME" sh -c 'command -v apk' >/dev/null 2>&1 && ls "$ROOT"/dist/bandwatch-*.apk >/dev/null 2>&1; then
	STUB="$ROOT/dist/.stub"
	mkdir -p "$STUB/root/usr/share/doc/bw-test-stub" && echo stub > "$STUB/root/usr/share/doc/bw-test-stub/README"
	docker run --rm -v "$STUB:/s" "${APK_IMAGE:-alpine:edge}" apk mkpkg --info name:bw-test-python-stub --info version:3.13.0-r1 --info arch:noarch --info "description:test stub" \
		--info "provides:python3-light=3.13.0-r1 python3-sqlite3=3.13.0-r1 python3-urllib=3.13.0-r1 luci-base=26.0-r1" --files /s/root --output /s/stub.apk >/dev/null
	check "apk: install published beta.1 packages" "apk add --allow-untrusted --no-network /dist/.stub/stub.apk /beta1/bandwatch-0.1.0_beta1-r1.apk /beta1/luci-app-bandwatch-0.1.0_beta1-r1.apk && $WAIT_UP"
	check "apk: seed data with beta.1" "/etc/init.d/bandwatch stop; $H seed && /etc/init.d/bandwatch start && $WAIT_UP && sleep 3 && $H verify $OLD"
	check "apk: upgrade to $VER packages (no uninstall)" "apk add --allow-untrusted --no-network /dist/bandwatch-*.apk /dist/luci-app-bandwatch-*.apk && apk info -v 2>/dev/null | grep -q '^bandwatch-' && $WAIT_UP && $H verify $VER upgrade"
	rm -rf "$STUB"
elif docker exec "$NAME" sh -c 'command -v opkg' >/dev/null 2>&1; then
	# The test image's Python is not opkg-managed: a stub package provides the
	# dependency names so opkg resolves them normally (no --force-depends,
	# which would make opkg skip the upgrade of 'bandwatch').
	STUB="$ROOT/dist/.stub"
	mkdir -p "$STUB"
	(cd "$ROOT" && python3 -c "import sys; sys.path.insert(0, 'scripts'); import build; open(sys.argv[1], 'wb').write(build.build_ipk('bw-test-python-stub', '3.13.0-1', {'usr/share/doc/bw-test-stub/README': b'stub'}, [], {}, 1790000000, provides=['python3-light', 'python3-sqlite3', 'python3-urllib', 'luci-base']))" "$STUB/stub.ipk")
	check "opkg: install published beta.1 packages" "opkg install /dist/.stub/stub.ipk /beta1/bandwatch_*_all.ipk /beta1/luci-app-bandwatch_*_all.ipk && $WAIT_UP"
	check "opkg: seed data with beta.1" "/etc/init.d/bandwatch stop; $H seed && /etc/init.d/bandwatch start && $WAIT_UP && sleep 3 && $H verify $OLD"
	check "opkg: upgrade to $VER packages (no uninstall)" "opkg install /dist/bandwatch_*_all.ipk /dist/luci-app-bandwatch_*_all.ipk && $WAIT_UP && $H verify $VER upgrade"
	rm -rf "$STUB"
fi

echo
if [ "$FAILS" = 0 ]; then echo "All upgrade checks passed ($TAG: $OLD -> $VER)"; else echo "$FAILS upgrade check(s) failed ($TAG)"; exit 1; fi
