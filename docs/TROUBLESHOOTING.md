# Troubleshooting

Start with:

```sh
/etc/init.d/bandwatch status
logread -e bandwatch | tail -n 50
bandwatch check --running
bandwatch-diag            # sanitised report in /tmp
```

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Installer: `python3 is not installed` / missing modules | Python packages absent | `apk add python3-light python3-sqlite3 python3-urllib` (25.12) or `opkg install …` (24.10), or `sh install.sh --install-deps` |
| Installer: port already used | another service on 8787 | `sh install.sh --port 8790` |
| Installer rolled back | service failed to start | read the log lines it printed; `logread -e bandwatch` |
| Dashboard not reachable from LAN | listening on loopback only (LAN address unknown at start) | log says "could not determine the LAN address"; check `ubus call network.interface.lan status`; `/etc/init.d/bandwatch restart` once LAN is up; or set `list listen '192.168.1.1'` |
| `403 client address not allowed` | client outside `allowed_networks` | add your subnet: `uci add_list bandwatch.main.allowed_networks='10.8.0.0/24'; uci commit; /etc/init.d/bandwatch restart` |
| `421 unexpected Host header` | opened via a public DNS name | use the IP, a `.lan` name, or add `list allowed_hosts 'name'` (advanced) |
| Login says no password configured | first install via package | `bandwatch passwd` |
| Forgot password | — | `bandwatch passwd` on the router |
| LuCI page: "not running" | service stopped | `/etc/init.d/bandwatch start` |
| LuCI over HTTPS: no embedded dashboard | browsers block HTTP frames in HTTPS pages | use "Open BandWatch in a new tab" |
| WAN collector *unavailable* | no default route (WAN down) or ubus not readable | check `ip route`; set **Settings → WAN interfaces** manually |
| WAN shows tunnel device only | only a VPN has a default route | select the physical WAN in Settings |
| Per-device: "No supported per-device provider" | Bandix/nlbwmon not installed or not reachable | install/start Bandix; check `curl -s http://127.0.0.1:8686/api/system/info` |
| Device download/upload swapped | Bandix direction convention | Settings → Bandix direction → *tx = device download*; please report it |
| Device totals lower than WAN | router's own traffic (aria2, updates, DNS), offloaded flows, or devices behind NAT | expected; see "Router & unattributed" |
| aria2 *error: rejected … check the RPC secret* | secret missing/wrong | enter the `rpc-secret` in Settings → aria2 |
| aria2 *unavailable* | RPC disabled or different port | enable RPC in aria2; set the URL in Settings |
| History has gaps | BandWatch not running, reboot, provider outage | gaps are intentional (unmeasured ≠ zero) |
| "Recovered WAN traffic … (timing estimated)" | service restarted without reboot | normal; bytes are exact, timing within the gap is spread evenly |
| Database "recreated" warning | damaged file (e.g. power loss during write) | damaged copy kept as `bandwatch.db.corrupt-*`; attach `bandwatch-diag` to an issue |
| High flash writes | very short flush interval | increase **Settings → Save to storage every** (default 300 s) or move `data_dir` |
| External HDD never sleeps | data dir or vnStat DB on that disk | keep `data_dir` on flash; vnStat is only read on request |
| Times are off by hours | timezone | Settings → Timezone (`system` uses `/etc/config/system`) |
| Clock warning after boot | NTP not yet synced | samples are buffered until the clock is trusted |

Reset everything (keeps the program): stop the service, remove
`/etc/bandwatch/bandwatch.db*` and start again.
