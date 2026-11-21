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

echo "== building the test image"
timeout 900 docker build -q --build-arg OPENWRT_TAG="$TAG" -t "$IMG" "$ROOT/tests/openwrt" >/dev/null
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$NAME" --privileged --tmpfs /tmp:exec -p "$LUCI_PORT:80" -p "$BW_PORT:$BW_PORT" \
	-v "$ROOT/dist:/dist:ro" "$IMG" /sbin/init >/dev/null
# Wait for procd to finish booting (firewall, netifd, uhttpd, rpcd).
i=0
echo "== waiting for the container to boot"
until timeout 20 docker exec "$NAME" sh -c 'pgrep uhttpd >/dev/null && pgrep rpcd >/dev/null && ubus call service list "{\"name\":\"firewall\"}" >/dev/null' 2>/dev/null; do
	i=$((i + 1)); [ $i -ge 60 ] && break; sleep 1
done
sleep 5
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$NAME")
PREFIX=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPPrefixLen}}{{end}}' "$NAME")
# The image's default network config bridges eth0 into br-lan, which leaves the
# Docker address unusable (netifd strips or shadows it, so the port mapping
# times out). In this disposable container only: take eth0 out of the bridge,
# give it the Docker address as a netifd-managed static interface, and turn the
# container's own firewall off so hotplug events cannot reload it.
diag() {
	timeout 30 docker exec "$NAME" sh -c 'ip -o addr; ip -o link; ip route; uci show network; netstat -ltn; nft list ruleset 2>&1 | head -20; ps w | grep -v "\[" | tail -n 40; logread | tail -n 40; cat /tmp/install.log 2>/dev/null' || true
}
echo "== configuring the test container network and installing BandWatch"
# Every phase is time-limited so a hang fails fast and shows where it was.
timeout 300 docker exec "$NAME" sh -c "
	/etc/init.d/firewall stop >/dev/null 2>&1; /etc/init.d/firewall disable >/dev/null 2>&1
	uci -q del_list network.@device[0].ports=eth0
	uci -q set network.@device[0].bridge_empty=1
	uci set network.dockernet=interface
	uci set network.dockernet.proto=static
	uci set network.dockernet.device=eth0
	uci set network.dockernet.ipaddr=$IP/$PREFIX
	uci commit network
	/etc/init.d/network reload
	i=0; until ip -4 -o addr show dev eth0 | grep -q '$IP/'; do i=\$((i + 1)); [ \$i -ge 30 ] && break; sleep 1; done
	nft flush ruleset
	printf 'luci-test-pw\nluci-test-pw\n' | passwd root >/dev/null 2>&1
	cd /tmp && tar -xzf /dist/bandwatch-$VER-openwrt.tar.gz && cd bandwatch-$VER && sh install.sh >/tmp/install.log 2>&1 || { cat /tmp/install.log; exit 1; }
	uci add_list bandwatch.main.listen=any && uci commit bandwatch && /etc/init.d/bandwatch restart
" || { echo "setup/install failed or timed out (exit $?); diagnostics:"; diag; exit 1; }
echo "== waiting for BandWatch through the port mapping"
i=0
until curl -s --max-time 3 -o /dev/null "http://127.0.0.1:$BW_PORT/api/session" -H "Host: 127.0.0.1"; do
	i=$((i + 1))
	if [ $i -ge 60 ]; then
		echo "BandWatch not reachable through the port mapping; diagnostics:"
		diag
		exit 1
	fi
	sleep 1
done
echo "== running the LuCI browser checks"
LUCI_URL="http://127.0.0.1:$LUCI_PORT" LUCI_PASSWORD=luci-test-pw timeout 300 node "$ROOT/tests/openwrt/luci-e2e.js" || { rc=$?; echo "LuCI checks failed (exit $rc); diagnostics:"; diag; exit "$rc"; }
