#!/bin/sh
# Runs inside the OpenWrt test container (tests/openwrt/run.sh) with
# BandWatch installed and running. Exercises the tester helper exactly as a
# tester would (BusyBox sed/awk/grep, musl regex) and checks:
#   - it refuses a symlinked or foreign-owned work directory in /tmp
#   - reports contain no MAC/IP addresses, host/device names, secrets or
#     download file names, even when those appear in the router's logs
#   - the listening-address check classifies BandWatch's sockets
#   - "postreboot" builds its own report
set -eu
H=${1:?path to bandwatch-test-round.sh}
fail() { echo "FAIL: $*"; exit 1; }

# 1. Predictable /tmp paths must not be trusted.
ln -s /etc /tmp/bandwatch-test
if sh "$H" before >/tmp/tr.log 2>&1; then fail "accepted a symlinked work directory"; fi
grep -q "symbolic link" /tmp/tr.log || fail "no symlink message: $(cat /tmp/tr.log)"
[ ! -e /etc/system-before.txt ] || fail "wrote through the symlink"
rm /tmp/bandwatch-test
mkdir /tmp/bandwatch-test && chown nobody /tmp/bandwatch-test
if sh "$H" before >/tmp/tr.log 2>&1; then fail "accepted a directory owned by another user"; fi
rm -r /tmp/bandwatch-test

# 2. Private data in the router's own logs and configuration.
uci set system.@system[0].hostname='privatebox-x'
uci commit system
printf '1700000000 aa:bb:cc:11:22:33 192.168.1.50 secret-laptop-x 01:aa:bb:cc:11:22:33\n' > /tmp/dhcp.leases
logger -t bandwatch "test: device secret-laptop-x at 192.168.1.50 aa:bb:cc:11:22:33 fd00::50 on privatebox-x"
logger -t bandwatch "aria2: Download finished: My Personal Holiday.mkv"
logger -t bandwatch "test: aria2 secret=s3cr3t-rpc-value url http://admin:pw@192.168.1.1:6800/jsonrpc"

sh "$H" before >/dev/null
sh "$H" after >/tmp/tr-after.log 2>&1 || fail "after: $(cat /tmp/tr-after.log)"
PERF_SAMPLES=1 PERF_INTERVAL=1 sh "$H" perf >/dev/null
sh "$H" report >/tmp/tr-report.log 2>&1 || fail "report: $(cat /tmp/tr-report.log)"
sh "$H" postreboot >/tmp/tr-post.log 2>&1 || fail "postreboot: $(cat /tmp/tr-post.log)"

[ "$(ls /tmp/bandwatch-test-reports/bandwatch-pre-reboot-report-*.tar.gz | wc -l)" = 1 ] || fail "no pre-reboot report"
[ "$(ls /tmp/bandwatch-test-reports/bandwatch-post-reboot-report-*.tar.gz | wc -l)" = 1 ] || fail "no post-reboot report"
[ "$(ls -ld /tmp/bandwatch-test-reports | cut -c1-10)" = drwx------ ] || fail "reports directory is not private"

X=/tmp/tr-extract
rm -rf "$X"; mkdir "$X"
for f in /tmp/bandwatch-test-reports/*.tar.gz; do tar -xzf "$f" -C "$X"; done
[ -f "$X/after.txt" ] || fail "after.txt missing from report"
grep -q "listening on" "$X/after.txt" "$X/diagnostics.txt" || fail "log lines missing (test would be vacuous)"
for bad in secret-laptop-x privatebox-x 'My Personal Holiday' s3cr3t-rpc-value 'admin:pw' integration-pass-1; do
	if grep -r -F -l "$bad" "$X"; then fail "report contains '$bad'"; fi
done
if grep -r -E -l '([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}' "$X"; then fail "report contains a MAC address"; fi
if grep -r -E -l '([0-9]{1,3}\.){3}[0-9]{1,3}' "$X"; then fail "report contains an IPv4 address"; fi
if grep -r -E -l 'fd00::50' "$X"; then fail "report contains an IPv6 address"; fi

grep -q '^VERDICT: ' "$X/after.txt" || fail "no listening verdict"
grep -q '^listen: ' "$X/after.txt" || fail "no listening classification"
grep -q '^listen: ' "$X/postreboot.txt" || fail "no listening classification after reboot"
grep -q '^history: ' "$X/postreboot.txt" || fail "no history summary"
echo "test-round helper OK: $(grep -h '^VERDICT' "$X/after.txt")"
