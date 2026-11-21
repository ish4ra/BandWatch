#!/bin/sh
# BandWatch tester helper - one organised hardware test round.
#
#   sh bandwatch-test-round.sh before     # BEFORE installing BandWatch
#   sh bandwatch-test-round.sh after      # after installing and using it
#   sh bandwatch-test-round.sh perf       # 5-minute CPU / memory measurement
#   sh bandwatch-test-round.sh report     # bundle everything for the maintainer
#
# Read-only: it never changes router settings. Results are kept in
# /tmp/bandwatch-test/ (RAM, cleared on reboot - run "report" before
# rebooting, or copy the folder). MAC/IP addresses and secrets are removed.

OUT=/tmp/bandwatch-test
PORT=$(uci -q get bandwatch.main.port || echo 8787)
mkdir -p "$OUT"
umask 077

sanitize() {
	sed -E \
		-e 's/([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/<mac>/g' \
		-e 's/([0-9]{1,3}\.){3}[0-9]{1,3}/<ipv4>/g' \
		-e 's/([0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}/<ipv6>/g' \
		-e 's/("(host|hostname|name)" *: *")[^"]*"/\1<name>"/g' \
		-e 's/(secret|password|token)([" :=]+)[^ ",}]+/\1\2<redacted>/g'
}

config_fingerprint() {
	# Hashes of router configuration that BandWatch must never change.
	for c in firewall network dhcp wireless system bandix aria2 vnstat; do
		if uci -q export "$c" >/dev/null 2>&1; then
			printf '%s %s\n' "$c" "$(uci -q export "$c" | sha256sum | cut -c1-16)"
		fi
	done
}

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
		echo "== listening"; netstat -ltn 2>/dev/null | grep ":$PORT "
		echo "== data dir"; ls -l /etc/bandwatch 2>&1; ls -ld /etc/bandwatch
		echo "== installed size"; du -sh /usr/lib/bandwatch 2>/dev/null
		echo "== check"; bandwatch check --running 2>&1
		echo "== recent log"; logread -e bandwatch 2>/dev/null | tail -n 60
	} 2>&1 | sanitize > "$OUT/after.txt"
	config_fingerprint > "$OUT/config-after.txt"
	if [ -f "$OUT/config-before.txt" ]; then
		if cmp -s "$OUT/config-before.txt" "$OUT/config-after.txt"; then
			echo "OK: firewall/network/DHCP/Wi-Fi/Bandix/aria2 configuration unchanged" | tee "$OUT/config-compare.txt"
		else
			echo "NOTE: some configuration changed since 'before' (may be your own edits):" | tee "$OUT/config-compare.txt"
			diff "$OUT/config-before.txt" "$OUT/config-after.txt" | tee -a "$OUT/config-compare.txt"
		fi
	fi
	# Bandix API sample (structure only; names and addresses removed).
	if wget -q -T 3 -O "$OUT/.bandix.json" http://127.0.0.1:8686/api/traffic/devices 2>/dev/null; then
		head -c 4000 "$OUT/.bandix.json" | sanitize > "$OUT/bandix-sample.txt"
		wget -q -T 3 -O - http://127.0.0.1:8686/api/system/info 2>/dev/null > "$OUT/bandix-version.txt"
		rm -f "$OUT/.bandix.json"
		echo "Bandix API sample saved (sanitised)."
	else
		echo "Bandix API not reachable on 127.0.0.1:8686" > "$OUT/bandix-sample.txt"
	fi
	bandwatch-diag "$OUT/diagnostics.txt" >/dev/null 2>&1
	echo "Saved to $OUT. Next: sh $0 perf  (takes 5 minutes)"
	;;
perf)
	PID=$(ps w | grep '[b]andwatch run' | awk '{print $1}' | head -n 1)
	[ -n "$PID" ] || { echo "BandWatch is not running"; exit 1; }
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
		echo "db_size=$(ls -l /etc/bandwatch/bandwatch.db 2>/dev/null | awk '{print $5}')"
		uptime
	} | tee "$OUT/perf.csv"
	echo "Saved to $OUT/perf.csv. Targets: average CPU < 2 %, RSS < 30 MiB (30720 KiB)."
	;;
report)
	cp /tmp/bandwatch-test-checklist.txt "$OUT/" 2>/dev/null
	F=/tmp/bandwatch-test-report-$(date +%Y%m%d-%H%M).tar.gz
	tar -czf "$F" -C /tmp bandwatch-test
	echo "Report: $F"
	echo "Copy it to your computer, e.g.:  scp -O root@<router-ip>:$F ."
	echo "Please look through the files before sharing them."
	;;
*)
	sed -n '2,13p' "$0"
	exit 1
	;;
esac
