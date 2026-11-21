#!/bin/sh
# Real-LuCI integration test: boots an OpenWrt rootfs container with procd,
# uhttpd, rpcd and LuCI, installs BandWatch from dist/ and runs
# tests/openwrt/luci-e2e.js (Playwright) against it.
#
#   sh tests/openwrt/luci.sh [openwrt-rootfs-tag]      (default x86-64-25.12.0)
#
# Only the disposable test container is modified (root password, its own
# firewall ruleset and interface address, so Docker port mapping works).

set -eu
TAG=${1:-x86-64-25.12.0}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMG="bandwatch-owrt-test:$TAG"
NAME="bw-luci-$$"
LUCI_PORT=${LUCI_PORT:-18080}
BW_PORT=8787
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/bandwatch/__init__.py")

docker build -q --build-arg OPENWRT_TAG="$TAG" -t "$IMG" "$ROOT/tests/openwrt" >/dev/null
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$NAME" --privileged --tmpfs /tmp:exec -p "$LUCI_PORT:80" -p "$BW_PORT:$BW_PORT" \
	-v "$ROOT/dist:/dist:ro" "$IMG" /sbin/init >/dev/null
# Wait for procd to finish booting (firewall, netifd, uhttpd, rpcd).
i=0
until docker exec "$NAME" sh -c 'pgrep uhttpd >/dev/null && pgrep rpcd >/dev/null && ubus call service list "{\"name\":\"firewall\"}" >/dev/null' 2>/dev/null; do
	i=$((i + 1)); [ $i -ge 60 ] && break; sleep 1
done
sleep 5
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}/{{.IPPrefixLen}}{{end}}' "$NAME")
docker exec "$NAME" sh -c "
	/etc/init.d/firewall stop >/dev/null 2>&1; nft flush ruleset
	ip addr add $IP dev eth0 2>/dev/null; ip link set eth0 up
	printf 'luci-test-pw\nluci-test-pw\n' | passwd root >/dev/null 2>&1
	cd /tmp && tar -xzf /dist/bandwatch-$VER-openwrt.tar.gz && cd bandwatch-$VER && sh install.sh >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
	uci add_list bandwatch.main.listen=any && uci commit bandwatch && /etc/init.d/bandwatch restart
"
i=0
until curl -s -o /dev/null "http://127.0.0.1:$BW_PORT/api/session" -H "Host: 127.0.0.1"; do
	i=$((i + 1))
	if [ $i -ge 60 ]; then
		echo "BandWatch not reachable through the port mapping; diagnostics:"
		docker exec "$NAME" sh -c 'ip -o addr; netstat -ltn; nft list ruleset 2>/dev/null | head -20; logread | tail -n 30; cat /tmp/install.log'
		exit 1
	fi
	sleep 1
done
LUCI_URL="http://127.0.0.1:$LUCI_PORT" LUCI_PASSWORD=luci-test-pw node "$ROOT/tests/openwrt/luci-e2e.js"
