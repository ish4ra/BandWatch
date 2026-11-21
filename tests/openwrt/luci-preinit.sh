#!/bin/sh
# Pre-init for the disposable LuCI test container (tests/openwrt/luci.sh).
#
# Docker has already configured eth0 when this runs. The image's default
# network config would bridge eth0 into br-lan, which makes the Docker
# address unusable from the host. Reconfiguring netifd after boot races its
# start-up ("ubus call network reload: Request timed out"), so the change
# is made during boot instead. /etc/config/network does not exist until
# first boot generates it, so a uci-defaults script is installed. OpenWrt
# runs it after generating the config and before netifd starts. It:
#   - takes eth0 out of br-lan and keeps Docker's address as a static interface
#   - puts that interface in the firewall's lan zone, so the host reaches
#     LuCI and BandWatch the way a LAN client reaches a real router
# Nothing outside this throwaway container is touched.
set -eu
addr=$(ip -4 -o addr show dev eth0 | awk '{print $4}' | head -n 1)
[ -n "$addr" ] || { echo "preinit: eth0 has no IPv4 address" >&2; exit 1; }
mkdir -p /etc/uci-defaults
cat > /etc/uci-defaults/99-bandwatch-test-dockernet <<UCI
uci -q del_list network.@device[0].ports=eth0
uci -q set network.@device[0].bridge_empty=1
uci set network.dockernet=interface
uci set network.dockernet.proto=static
uci set network.dockernet.device=eth0
uci set network.dockernet.ipaddr='$addr'
uci commit network
uci add_list firewall.@zone[0].network=dockernet
uci commit firewall
exit 0
UCI
exec /sbin/init
