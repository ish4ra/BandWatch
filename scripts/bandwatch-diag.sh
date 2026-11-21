#!/bin/sh
# BandWatch sanitised diagnostics collector.
#
#   bandwatch-diag [output-file]
#
# Collects versions, service state, BandWatch's own checks and recent log
# lines. MAC addresses, IPv4/IPv6 addresses, hostnames from DHCP leases,
# download file names and secrets are removed. Review the file before
# sharing it. Nothing on the router is changed.

umask 077
# Temporary files are created with mktemp (never a predictable /tmp name).
LIT=$(mktemp /tmp/bandwatch-diag-lit.XXXXXX) || { echo "cannot create a temporary file" >&2; exit 1; }
trap 'rm -f "$LIT"' EXIT INT TERM
if [ -n "${1:-}" ]; then
	OUT=$1
	# Never write through a symbolic link (e.g. one planted in /tmp).
	[ -L "$OUT" ] && { echo "refusing to write to symbolic link $OUT" >&2; exit 1; }
else
	OUT=$(mktemp /tmp/bandwatch-diag-XXXXXX) || { echo "cannot create the output file" >&2; exit 1; }
fi

# --- keep in sync with scripts/bandwatch-test-round.sh (tests check both) ---
# Values that identify people, devices or networks. Each one found on the
# router is replaced literally wherever it appears in a report.
collect_private_literals() {
	{
		uci -q get system.@system[0].hostname
		cat /proc/sys/kernel/hostname 2>/dev/null; echo
		for f in /tmp/dhcp.leases /tmp/hosts/odhcpd; do
			[ -r "$f" ] && [ ! -L "$f" ] && awk '{print $4}' "$f"
		done
		uci -q show dhcp | sed -n "s/^dhcp\.[^.]*\.\(name\|hostname\|domain\|local\)='\(.*\)'$/\2/p"
		uci -q show wireless | sed -n "s/^wireless\.[^.]*\.\(ssid\|key\|key[1-4]\|password\|identity\|sae_password\)='\(.*\)'$/\2/p"
		uci -q show network | sed -n "s/^network\.[^.]*\.\(username\|password\|pincode\|apn\|hostname\|auth\|key\)='\(.*\)'$/\2/p"
		uci -q show aria2 | sed -n "s/^aria2\.[^.]*\.\(rpc_secret\|dir\|rpc_user\|rpc_passwd\|user\)='\(.*\)'$/\2/p"
		uci -q show ddns | sed -n "s/^ddns\.[^.]*\.\(domain\|lookup_host\|username\|password\)='\(.*\)'$/\2/p"
	} 2>/dev/null | tr -d '\r' | awk 'length($0) >= 3' | \
		grep -v -i -x -E '(openwrt|lan|wan|wwan|none|auto|any|default|\*|-)' | sort -u
}

# sanitize [literal-file]: filter stdin -> stdout.
sanitize() {
	lit=${1:-/dev/null}
	awk -v lf="$lit" '
		BEGIN { n = 0; while ((getline l < lf) > 0) if (length(l) >= 3) lits[++n] = l }
		{
			for (i = 1; i <= n; i++) {
				s = lits[i]; out = ""; line = $0
				while ((p = index(line, s)) > 0) { out = out substr(line, 1, p - 1) "<private>"; line = substr(line, p + length(s)) }
				$0 = out line
			}
			print
		}' | sed -E \
		-e 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@ "]+@#\1<credentials>@#g' \
		-e 's/(Download (finished|failed): ).*/\1<file name>/' \
		-e 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<mac>/g' \
		-e 's/([0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}/<mac>/g' \
		-e 's/(^|[^0-9.])127\.0\.0\.1([^0-9]|$)/\1<loopback>\2/g' \
		-e 's/([0-9]{1,3}\.){3}[0-9]{1,3}(\/[0-9]{1,2})?/<ipv4>/g' \
		-e 's/([0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}(\/[0-9]{1,3})?/<ipv6>/g' \
		-e 's/[0-9A-Fa-f]{0,4}(:[0-9A-Fa-f]{0,4})*::([0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{1,4})*)?(\/[0-9]{1,3})?/<ipv6>/g' \
		-e 's/"(host|hostname|alias|nickname|ssid|mac|ip|ip4|ip6|ipv4|ipv6|filename|file|path|dir|url|uri|user|username|device_name|comment)"( *: *)"[^"]*"/"\1"\2"<redacted>"/g' \
		-e 's/([Ss]ecret|SECRET|[Pp]ass(word|wd)?|PASSWORD|[Tt]oken|TOKEN|[Aa]pi[_-]?[Kk]ey|psk|PSK|[Cc]ookie|[Aa]uthorization)([" :=]+)[^ ",}]+/\1\3<redacted>/g' \
		-e 's/[A-Za-z0-9][A-Za-z0-9-]*\.(lan|local|home|internal|localdomain|home\.arpa)([^A-Za-z0-9.-]|$)/<host>\2/g'
}

# --- end of shared sanitiser ---

collect_private_literals > "$LIT"

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
} 2>&1 | sanitize "$LIT" > "$OUT"

echo "Diagnostics written to $OUT"
echo "Please review the file before sharing it."
