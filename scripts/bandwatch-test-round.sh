#!/bin/sh
# BandWatch tester helper - one organised hardware test round.
#
#   sh bandwatch-test-round.sh before      # BEFORE installing BandWatch
#   sh bandwatch-test-round.sh after       # after installing and testing
#   sh bandwatch-test-round.sh perf        # 5-minute CPU / memory measurement
#   sh bandwatch-test-round.sh report      # bundle the results - BEFORE rebooting
#   sh bandwatch-test-round.sh postreboot  # after the reboot test (own report)
#
# Read-only: it never changes router settings. Work files live in
# /tmp/bandwatch-test and reports in /tmp/bandwatch-test-reports, both in
# RAM: a reboot deletes them AND this script, so copy the report to your
# computer before rebooting. Everything that goes into a report is
# sanitised: MAC/IP addresses, host and device names, Wi-Fi names, user
# names, passwords, secrets and download file names are removed.

umask 077
OUT=${BW_TEST_DIR:-/tmp/bandwatch-test}
REPORTS=${BW_TEST_REPORTS:-/tmp/bandwatch-test-reports}
DATA_DIR=$(uci -q get bandwatch.main.data_dir 2>/dev/null || echo /etc/bandwatch)
PORT=$(uci -q get bandwatch.main.port 2>/dev/null || echo 8787)
case "$PORT" in *[!0-9]*|"") PORT=8787 ;; esac

die() { echo "ERROR: $*" >&2; exit 1; }

# A directory in /tmp (world-writable) must be a real directory owned by us,
# never a symbolic link or something another user created first.
safe_dir() {
	d=$1
	[ -L "$d" ] && die "$d is a symbolic link - refusing to use it (remove it and run again)"
	if [ ! -e "$d" ]; then
		mkdir -m 700 "$d" 2>/dev/null || die "cannot create $d"
	fi
	[ -d "$d" ] && [ ! -L "$d" ] || die "$d is not a directory - refusing to use it"
	owner=$(ls -ldn "$d" | awk '{print $3}')
	[ "$owner" = "$(id -u)" ] || die "$d is owned by another user (uid $owner) - refusing to use it"
	chmod 700 "$d" || die "cannot set permissions on $d"
}

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

# JSON from a provider -> keys and numbers only (every string value replaced).
json_structure() {
	head -c 4000 | sed -E 's/"([^"\\]|\\.)*"( *[],}])/"<s>"\2/g'
}

config_fingerprint() {
	# Hashes of router configuration that BandWatch must never change.
	for c in firewall network dhcp wireless system bandix aria2 vnstat; do
		if uci -q export "$c" >/dev/null 2>&1; then
			printf '%s %s\n' "$c" "$(uci -q export "$c" | sha256sum | cut -c1-16)"
		fi
	done
}

# Where does BandWatch listen? Addresses are classified, never printed.
listen_check() {
	lan="" wan="" l4="" l6="" w4="" w6="" a4="" a6=""
	if [ -r /lib/functions/network.sh ]; then
		# shellcheck disable=SC1091
		. /lib/functions/network.sh
		network_get_ipaddrs l4 lan; network_get_ipaddrs6 l6 lan
		lan="$l4 $l6"
		network_find_wan w4; network_find_wan6 w6
		for i in $w4 $w6 wan wan6 wwan; do
			network_get_ipaddrs a4 "$i"; network_get_ipaddrs6 a6 "$i"
			wan="$wan $a4 $a6"
		done
	fi
	# shellcheck disable=SC2086 # word splitting intended (address lists)
	lan=$(printf '%s\n' $lan | sort -u)
	# shellcheck disable=SC2086
	wan=$(printf '%s\n' $wan | sort -u)
	addrs=$(netstat -ltn 2>/dev/null | awk -v p=":$PORT" '$1 ~ /^tcp/ && substr($4, length($4) - length(p) + 1) == p { a = $4; sub(/:[0-9]+$/, "", a); print a }' | sort -u)
	[ -n "$addrs" ] || { echo "listen: nothing is listening on port $PORT"; echo "VERDICT: FAIL (BandWatch not listening)"; return; }
	bad=0
	for a in $addrs; do
		case "$a" in
		127.0.0.1|::1) echo "listen: loopback (127.0.0.1 / ::1)" ;;
		0.0.0.0|::|"*") echo "listen: ALL addresses (WAN exposure then depends only on the firewall)"; bad=1 ;;
		*)
			if printf '%s\n' "$wan" | grep -qxF "$a"; then echo "listen: a WAN address"; bad=2
			elif printf '%s\n' "$lan" | grep -qxF "$a"; then echo "listen: the LAN address"
			else echo "listen: another local address (not LAN, not WAN)"; bad=1
			fi ;;
		esac
	done
	if uci -q show firewall | grep -E "dest_port='([^']*[^0-9])?$PORT([^0-9][^']*)?'" >/dev/null; then
		echo "firewall: a firewall rule mentions port $PORT (check it is not open to the WAN)"; [ $bad = 0 ] && bad=1
	else
		echo "firewall: no firewall rule mentions port $PORT"
	fi
	z=$(uci -q show firewall | sed -n "s/^firewall\.\([^.]*\)\.name='wan'$/\1/p" | head -n 1)
	[ -n "$z" ] && echo "firewall: wan zone input policy = $(uci -q get "firewall.$z.input")"
	case $bad in
	0) echo "VERDICT: PASS (BandWatch listens only on loopback and LAN addresses)" ;;
	1) echo "VERDICT: CHECK (not limited to loopback/LAN - see lines above)" ;;
	*) echo "VERDICT: FAIL (BandWatch listens on a WAN address)" ;;
	esac
}

history_summary() {
	db="$DATA_DIR/bandwatch.db"
	[ -f "$db" ] && [ ! -L "$db" ] || { echo "history: database not found"; return; }
	python3 - "$db" <<'PY' 2>&1
import sqlite3, sys, time
c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=5)
n15, a, b = c.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM m15 WHERE s='wan'").fetchone()
nd = c.execute("SELECT COUNT(DISTINCT day) FROM d1 WHERE s='wan'").fetchone()[0]
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "-"
print("history: %d WAN 15-minute rows, %d days, oldest %s, newest %s" % (n15, nd, fmt(a), fmt(b)))
PY
}

# Builds a sanitised tarball of the given work files; refuses if anything
# that looks private survived sanitising.
make_report() {
	kind=$1; shift
	safe_dir "$REPORTS"
	stage="$OUT/stage"
	[ -e "$stage" ] && rm -rf "$stage"
	mkdir -m 700 "$stage" || die "cannot create $stage"
	collect_private_literals > "$OUT/private-literals"
	[ -n "${BW_TEST_LITERALS:-}" ] && cat "$BW_TEST_LITERALS" >> "$OUT/private-literals"
	for f in "$@"; do
		[ -f "$OUT/$f" ] && [ ! -L "$OUT/$f" ] || continue
		sanitize "$OUT/private-literals" < "$OUT/$f" > "$stage/$f"
	done
	leaks=$(grep -l -E '([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|([0-9]{1,3}\.){3}[0-9]{1,3}' "$stage"/* 2>/dev/null)
	if [ -s "$OUT/private-literals" ]; then
		leaks="$leaks
$(grep -l -F -f "$OUT/private-literals" "$stage"/* 2>/dev/null)"
	fi
	leaks=$(printf '%s\n' "$leaks" | sed -n "s#^$stage/##p" | sort -u | tr '\n' ' ')
	if [ -n "$leaks" ]; then
		rm -rf "$stage"
		die "possible private data in: $leaks- report NOT created; please tell the maintainer"
	fi
	F="$REPORTS/bandwatch-$kind-report-$(date +%Y%m%d-%H%M%S).tar.gz"
	tar -czf "$F" -C "$stage" . || die "could not write $F"
	rm -rf "$stage" "$OUT/private-literals"
	echo "Report: $F"
	echo
	echo "Copy it to your computer NOW (run this on your computer, not on the router):"
	echo "  scp -O \"root@<router-ip>:$REPORTS/*.tar.gz\" ."
	echo "Please look through the files before sharing them."
}

# Library mode for the automated tests: define the functions only.
if [ -n "${BW_TEST_LIB:-}" ]; then return 0 2>/dev/null || exit 0; fi

[ "$(id -u)" = 0 ] || [ -n "${BW_TEST_ALLOW_USER:-}" ] || die "run this as root on the router"
case "${1:-}" in before|after|perf|report|postreboot) safe_dir "$OUT" ;; esac

case "${1:-}" in
before)
	{
		date -u
		cat /etc/openwrt_release
		echo "model: $(cat /tmp/sysinfo/model 2>/dev/null)"
		df -h /overlay /tmp 2>/dev/null
		free
	} > "$OUT/system-before.txt" 2>&1
	config_fingerprint > "$OUT/config-before.txt"
	echo "Saved. Now install BandWatch (see INSTALL-SINHALA.md), use the router normally,"
	echo "then run:  sh $0 after"
	;;
after)
	{
		date -u
		echo "== version"; cat /usr/lib/bandwatch/VERSION 2>/dev/null
		echo "== service"; /etc/init.d/bandwatch status 2>&1
		echo "== process"; ps w | grep '[b]andwatch run'
		echo "== listening (WAN isolation)"; listen_check
		echo "== data dir"; ls -ld "$DATA_DIR"; ls -l "$DATA_DIR" 2>&1
		echo "== history"; history_summary
		echo "== installed size"; du -sh /usr/lib/bandwatch 2>/dev/null
		echo "== check"; bandwatch check --running 2>&1
		echo "== recent log"; logread -e bandwatch 2>/dev/null | tail -n 60
	} > "$OUT/after.txt" 2>&1
	config_fingerprint > "$OUT/config-after.txt"
	if [ -f "$OUT/config-before.txt" ]; then
		if cmp -s "$OUT/config-before.txt" "$OUT/config-after.txt"; then
			echo "OK: firewall/network/DHCP/Wi-Fi/Bandix/aria2 configuration unchanged" | tee "$OUT/config-compare.txt"
		else
			echo "NOTE: some configuration changed since 'before' (may be your own edits):" | tee "$OUT/config-compare.txt"
			diff "$OUT/config-before.txt" "$OUT/config-after.txt" | tee -a "$OUT/config-compare.txt"
		fi
	fi
	grep '^VERDICT' "$OUT/after.txt"
	# Bandix samples: structure and numbers only, never names or addresses.
	if wget -q -T 3 -O - http://127.0.0.1:8686/api/traffic/devices 2>/dev/null | json_structure > "$OUT/bandix-sample.txt" && [ -s "$OUT/bandix-sample.txt" ]; then
		wget -q -T 3 -O - http://127.0.0.1:8686/api/system/info 2>/dev/null | head -c 4000 | \
			grep -o -E '"[A-Za-z_]*version[A-Za-z_]*" *: *"[0-9A-Za-z.+~_-]{1,40}"' > "$OUT/bandix-version.txt"
		echo "Bandix API sample saved (structure only)."
	else
		echo "Bandix API not reachable on 127.0.0.1:8686" > "$OUT/bandix-sample.txt"
	fi
	bandwatch-diag "$OUT/diagnostics.txt" >/dev/null 2>&1
	echo "Saved. Next: sh $0 perf  (takes 5 minutes)"
	;;
perf)
	PID=$(ps w | grep '[b]andwatch run' | awk '{print $1}' | head -n 1)
	[ -n "$PID" ] || die "BandWatch is not running"
	TCK=100
	echo "Measuring BandWatch for 5 minutes (keep the dashboard CLOSED for an idle measurement)..."
	{
		echo "t_s,cpu_percent,rss_kib,threads"
		a=$(awk '{print $14+$15}' "/proc/$PID/stat")
		i=0
		while [ $i -lt "${PERF_SAMPLES:-10}" ]; do
			sleep "${PERF_INTERVAL:-30}"
			b=$(awk '{print $14+$15}' "/proc/$PID/stat")
			cpu=$(awk -v d=$((b - a)) -v t=$TCK -v s="${PERF_INTERVAL:-30}" 'BEGIN { printf "%.2f", d * 100 / t / s }')
			rss=$(awk '/VmRSS/ {print $2}' "/proc/$PID/status")
			thr=$(awk '/Threads/ {print $2}' "/proc/$PID/status")
			i=$((i + 1))
			echo "$((i * ${PERF_INTERVAL:-30})),$cpu,$rss,$thr"
			a=$b
		done
		echo "write_bytes_total=$(awk '/^write_bytes/ {print $2}' "/proc/$PID/io" 2>/dev/null)"
		echo "db_size=$(ls -l "$DATA_DIR/bandwatch.db" 2>/dev/null | awk '{print $5}')"
		uptime
	} | tee "$OUT/perf.csv"
	echo "Saved. Targets: average CPU < 2 %, RSS < 30 MiB (30720 KiB). Next: sh $0 report"
	;;
report)
	make_report pre-reboot system-before.txt config-before.txt after.txt config-after.txt \
		config-compare.txt bandix-sample.txt bandix-version.txt diagnostics.txt perf.csv
	echo
	echo "Only reboot the router AFTER the report is on your computer."
	echo "After the reboot, copy this script to /tmp again and run:  sh /tmp/bandwatch-test-round.sh postreboot"
	;;
postreboot)
	{
		date -u
		echo "== uptime (seconds since boot)"; cut -d' ' -f1 /proc/uptime
		echo "== version"; cat /usr/lib/bandwatch/VERSION 2>/dev/null
		echo "== service"; /etc/init.d/bandwatch status 2>&1
		echo "== starts at boot"; /etc/init.d/bandwatch enabled && echo yes || echo no
		echo "== process"; ps w | grep '[b]andwatch run'
		echo "== listening (WAN isolation)"; listen_check
		echo "== history (compare with 'history' in the pre-reboot report)"; history_summary
		echo "== check"; bandwatch check --running 2>&1
		echo "== log since boot"; logread -e bandwatch 2>/dev/null | tail -n 60
	} > "$OUT/postreboot.txt" 2>&1
	config_fingerprint > "$OUT/config-postreboot.txt"
	grep '^VERDICT' "$OUT/postreboot.txt"
	make_report post-reboot postreboot.txt config-postreboot.txt
	;;
*)
	sed -n '2,15p' "$0"
	exit 1
	;;
esac
