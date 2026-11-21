#!/bin/sh
# BandWatch sanitised diagnostics collector.
#
#   bandwatch-diag [output-file]
#
# Collects versions, service state, BandWatch's own checks and recent log
# lines. MAC addresses, IPv4/IPv6 addresses, hostnames from DHCP leases,
# download file names and secrets are removed. Review the file before
# sharing it. Nothing on the router is changed.

OUT=${1:-/tmp/bandwatch-diag-$(date +%Y%m%d-%H%M%S).txt}
umask 077

# Hostnames known to the router are replaced as well.
HOSTS_SED=/tmp/.bandwatch-diag-hosts.$$
: > "$HOSTS_SED"
for f in /tmp/dhcp.leases /tmp/hosts/odhcpd; do
	[ -r "$f" ] || continue
	awk '{print $4}' "$f" 2>/dev/null
done | grep -E '^[A-Za-z0-9._-]{3,}$' | sort -u | while read -r h; do
	printf 's/%s/<host>/g\n' "$(printf '%s' "$h" | sed 's/[.]/\\./g')" >> "$HOSTS_SED"
done
HN=$(uci -q get system.@system[0].hostname)
[ -n "$HN" ] && printf 's/%s/<router-hostname>/g\n' "$(printf '%s' "$HN" | sed 's/[.]/\\./g')" >> "$HOSTS_SED"

sanitize() {
	sed -E \
		-e 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<mac>/g' \
		-e 's/(^|[^0-9.])(127\.0\.0\.1)([^0-9]|$)/\1<loopback>\3/g' \
		-e 's/([0-9]{1,3}\.){3}[0-9]{1,3}(\/[0-9]{1,2})?/<ipv4>/g' \
		-e 's/([0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(\/[0-9]{1,3})?/<ipv6>/g' \
		-e 's/[0-9A-Fa-f]{0,4}(:[0-9A-Fa-f]{1,4})*::([0-9A-Fa-f]{1,4}:?)*(\/[0-9]{1,3})?/<ipv6>/g' \
		-e 's/(secret|Secret|SECRET|password|Password|passwd|token|Token)([" :=]+)[^ ",}]+/\1\2<redacted>/g' \
		-e 's/(Download (finished|failed): ).*/\1<file name>/' \
		-f "$HOSTS_SED"
}

section() { printf '\n===== %s =====\n' "$1"; }

{
	echo "BandWatch diagnostics (sanitised) - generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
	echo "Review before sharing. MAC/IP addresses, hostnames and secrets are replaced."

	section "System"
	cat /etc/openwrt_release 2>/dev/null
	echo "model: $(cat /tmp/sysinfo/model 2>/dev/null)"
	echo "board: $(cat /tmp/sysinfo/board_name 2>/dev/null)"
	echo "kernel: $(uname -r) $(uname -m)"
	echo "uptime: $(cut -d' ' -f1 /proc/uptime) s"
	free 2>/dev/null
	df -h / /overlay /tmp 2>/dev/null

	section "Python"
	command -v python3 && python3 -c 'import sys; print(sys.version)'
	for m in sqlite3 http.server email.utils urllib.parse ssl zoneinfo; do
		python3 -c "import $m" 2>/dev/null && echo "module $m: yes" || echo "module $m: no"
	done

	section "Packages"
	if command -v apk >/dev/null 2>&1; then
		apk info -v 2>/dev/null | grep -E '^(bandwatch|luci-app-bandwatch|bandix|luci-app-bandix|vnstat|aria2|nlbwmon|python3)'
	elif command -v opkg >/dev/null 2>&1; then
		opkg list-installed 2>/dev/null | grep -E '^(bandwatch|luci-app-bandwatch|bandix|luci-app-bandix|vnstat|aria2|nlbwmon|python3)'
	fi
	echo "bandwatch files: $(cat /usr/lib/bandwatch/VERSION 2>/dev/null || echo not installed)"

	section "Service"
	/etc/init.d/bandwatch status 2>&1
	echo "enabled: $(/etc/init.d/bandwatch enabled && echo yes || echo no)"
	ubus call service list '{"name":"bandwatch"}' 2>/dev/null
	echo "--- /etc/config/bandwatch"
	cat /etc/config/bandwatch 2>/dev/null
	echo "--- listening sockets of interest"
	netstat -ltn 2>/dev/null | grep -E ':(8787|8686|6800|80|443) ' || echo "(netstat unavailable)"

	section "Network (names and types only)"
	for d in /sys/class/net/*; do
		n=${d##*/}
		echo "$n type=$(cat "$d/type" 2>/dev/null) operstate=$(cat "$d/operstate" 2>/dev/null)"
	done
	echo "--- default routes (device only)"
	ip -4 route show default 2>/dev/null | sed -E 's/.* dev ([^ ]+).*/IPv4 default via \1/'
	ip -6 route show default 2>/dev/null | sed -E 's/.* dev ([^ ]+).*/IPv6 default via \1/'
	echo "--- logical interfaces"
	ubus call network.interface dump 2>/dev/null | jsonfilter -e '@.interface[*]' 2>/dev/null | \
		sed -n 's/.*"interface": *"\([^"]*\)".*"up": *\([a-z]*\).*"proto": *"\([^"]*\)".*/\1 up=\2 proto=\3/p'
	echo "--- offloading"
	echo "flow_offloading=$(uci -q get firewall.@defaults[0].flow_offloading || echo 0) flow_offloading_hw=$(uci -q get firewall.@defaults[0].flow_offloading_hw || echo 0)"
	echo "conntrack accounting=$(cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null)"

	section "BandWatch preflight"
	bandwatch check --running 2>&1

	section "BandWatch diagnostic snapshot"
	bandwatch diag 2>&1 | head -c 200000

	section "Recent log lines"
	logread -e bandwatch 2>/dev/null | tail -n 150
	logread -e bandix 2>/dev/null | tail -n 20
} 2>&1 | sanitize > "$OUT"

rm -f "$HOSTS_SED"
echo "Diagnostics written to $OUT"
echo "Please review the file before sharing it."
