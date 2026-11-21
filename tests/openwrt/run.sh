#!/bin/sh
# OpenWrt container integration test for BandWatch packaging.
#
#   tests/openwrt/run.sh [openwrt-rootfs-tag]     (default x86-64-25.12.0)
#
# Boots procd inside an OpenWrt rootfs container and exercises: installer
# dry run, install, service health, unprivileged user, ubus ACL, LuCI rpcd
# SSO, password login, upgrade with backup, rollback, diagnostics,
# uninstall (keep data), purge, and apk/ipk package installation.
# Requires Docker and dist/ artefacts (python3 scripts/build.py).

set -eu
TAG=${1:-x86-64-25.12.0}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMG="bandwatch-owrt-test:$TAG"
NAME="bw-it-$$"
VER=$(cat "$ROOT/bandwatch/__init__.py" | sed -n 's/^__version__ = "\(.*\)"/\1/p')
FAILS=0

pass() { echo "PASS: $*"; }
failt() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
# Readiness probe used after package installs (service start time varies by machine).
WAIT_UP='i=0; until /etc/init.d/bandwatch running && wget -q -T 2 -O /dev/null http://127.0.0.1:8787/api/session; do i=$((i+1)); [ $i -ge 30 ] && exit 1; sleep 1; done'

check() {
	desc="$1"; shift
	if docker exec "$NAME" sh -c "$*" >/tmp/bw-it-out.$$ 2>&1; then
		pass "$desc"
	else
		failt "$desc"
		sed 's/^/    /' /tmp/bw-it-out.$$ | tail -20
		echo "    --- recent system log:"
		docker exec "$NAME" sh -c 'logread | tail -n 25' 2>&1 | sed 's/^/    /'
	fi
}

docker build -q --build-arg OPENWRT_TAG="$TAG" -t "$IMG" "$ROOT/tests/openwrt" >/dev/null
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -f /tmp/bw-it-out.$$; }
trap cleanup EXIT
docker run -d --name "$NAME" --privileged --tmpfs /tmp:exec -v "$ROOT/dist:/dist:ro" "$IMG" /sbin/init >/dev/null
sleep 8

check "procd and ubusd are running" "pgrep -x procd >/dev/null || ps | grep -q '[p]rocd'; ubus list >/dev/null"
check "extract archive" "cd /tmp && tar -xzf /dist/bandwatch-$VER-openwrt.tar.gz"
check "installer dry run changes nothing" "cd /tmp/bandwatch-$VER && sh install.sh --dry-run && ! test -e /etc/init.d/bandwatch"
check "install" "cd /tmp/bandwatch-$VER && sh install.sh | tee /tmp/install.log && grep -q 'is installed' /tmp/install.log"
check "service runs as unprivileged user" "ps w | grep '[b]andwatch run' | grep -q '^ *[0-9]* bandwatc'"
check "dashboard answers" "wget -q -O - http://127.0.0.1:8787/api/session | grep -q '\"version\"'"
check "API rejects anonymous access" "! wget -q -O /dev/null http://127.0.0.1:8787/api/overview"
check "ubus ACL: allowed for service user" "python3 -c 'import os,pwd,subprocess as s; p=pwd.getpwnam(\"bandwatch\"); os.setgid(p.pw_gid); os.setuid(p.pw_uid); raise SystemExit(s.run([\"ubus\",\"call\",\"network.interface\",\"dump\"],capture_output=True).returncode)'"
check "ubus ACL: other objects denied" "python3 -c 'import os,pwd,subprocess as s; p=pwd.getpwnam(\"bandwatch\"); os.setgid(p.pw_gid); os.setuid(p.pw_uid); raise SystemExit(0 if s.run([\"ubus\",\"call\",\"system\",\"board\"],capture_output=True).returncode else 1)'"
check "data directory is private" "ls -ld /etc/bandwatch | grep -q '^drwxr-x---' && ls -l /etc/bandwatch/auth.json | grep -q '^-rw-------' && ! ls -l /etc/bandwatch/bandwatch.db | grep -q '^-rw-r--r--'"
check "LuCI rpcd status" "ubus call luci.bandwatch status | grep -q '\"running\": true'"
check "LuCI SSO sign-in" "T=\$(ubus call luci.bandwatch sso | jsonfilter -e @.token); python3 -c 'import http.client,sys; c=http.client.HTTPConnection(\"127.0.0.1\",8787); c.request(\"GET\",\"/auth/sso?t=\"+sys.argv[1]); r=c.getresponse(); raise SystemExit(0 if r.status==303 and \"bw_session=\" in (r.getheader(\"Set-Cookie\") or \"\") else 1)' \$T"
check "password login" "echo 'integration-pass-1' | bandwatch passwd --stdin >/dev/null && python3 -c 'import http.client,json; c=http.client.HTTPConnection(\"127.0.0.1\",8787); c.request(\"POST\",\"/api/login\",body=json.dumps({\"password\":\"integration-pass-1\"}),headers={\"Content-Type\":\"application/json\",\"Host\":\"192.168.1.1\"}); raise SystemExit(0 if c.getresponse().status==200 else 1)'"
check "bandwatch check" "bandwatch check --running"
check "upgrade keeps password and makes a backup" "cd /tmp/bandwatch-$VER && sh install.sh | grep -q 'Previous version backup' && test -s /etc/bandwatch/auth.json"
check "rollback on failure" "cd /tmp/bandwatch-$VER && ! BW_INSTALL_TEST_FAIL=1 sh install.sh >/tmp/rb.log 2>&1; grep -q 'rolled back' /tmp/rb.log && /etc/init.d/bandwatch running"
check "port conflict detected" "python3 -c 'import socket,time; s=socket.socket(); s.bind((\"127.0.0.1\",8799)); s.listen(); time.sleep(4)' & sleep 1; cd /tmp/bandwatch-$VER && ! sh install.sh --port 8799 --dry-run"
check "sanitised diagnostics" "bandwatch-diag /tmp/d.txt >/dev/null && ! grep -Eq '([0-9a-f]{2}:){5}[0-9a-f]{2}' /tmp/d.txt && ! grep -q 'integration-pass-1' /tmp/d.txt"
check "uninstall keeps data" "cd /tmp/bandwatch-$VER && sh uninstall.sh && ! test -e /etc/init.d/bandwatch && test -s /etc/bandwatch/auth.json && test -f /etc/config/bandwatch"
check "purge removes data and user" "cd /tmp/bandwatch-$VER && sh install.sh >/dev/null && sh uninstall.sh --purge --yes && ! test -e /etc/bandwatch && ! grep -q '^bandwatch:' /etc/passwd"

if docker exec "$NAME" sh -c 'command -v apk' >/dev/null 2>&1 && ls "$ROOT"/dist/bandwatch-*.apk >/dev/null 2>&1; then
	STUB="$ROOT/dist/.stub"
	mkdir -p "$STUB/root/usr/share/doc/bw-test-stub" && echo stub > "$STUB/root/usr/share/doc/bw-test-stub/README"
	docker run --rm -v "$STUB:/s" "${APK_IMAGE:-alpine:edge}" apk mkpkg --info name:bw-test-python-stub --info version:3.13.0-r1 --info arch:noarch --info "description:test stub" \
		--info "provides:python3-light=3.13.0-r1 python3-sqlite3=3.13.0-r1 python3-urllib=3.13.0-r1 luci-base=26.0-r1" --files /s/root --output /s/stub.apk >/dev/null
	check "apk package install" "apk add --allow-untrusted --no-network /dist/.stub/stub.apk /dist/bandwatch-*.apk /dist/luci-app-bandwatch-*.apk && $WAIT_UP"
	check "apk package removal keeps data" "apk del bandwatch luci-app-bandwatch && ! test -e /etc/init.d/bandwatch && test -d /etc/bandwatch"
	rm -rf "$STUB"
elif docker exec "$NAME" sh -c 'command -v opkg' >/dev/null 2>&1; then
	# --force-depends only because the test image's Python is not opkg-managed; opkg then exits non-zero.
	check "ipk package install (opkg)" "opkg install --force-depends /dist/bandwatch_*_all.ipk /dist/luci-app-bandwatch_*_all.ipk; $WAIT_UP"
	check "ipk package removal keeps data" "opkg remove luci-app-bandwatch bandwatch && ! test -e /etc/init.d/bandwatch && test -d /etc/bandwatch"
fi

echo
if [ "$FAILS" = 0 ]; then echo "All OpenWrt container checks passed ($TAG)"; else echo "$FAILS check(s) failed ($TAG)"; exit 1; fi
