# Performance and reliability

**Targets** (on the router, not yet verified): average idle CPU below 2 %,
resident memory below 30 MiB, no measurable throughput impact.

**Nothing below was measured on router hardware.** All numbers come from an
x86-64 development VM (4 vCPU Xeon @ 2.1 GHz) running the demo simulator,
which exercises every collector. An ARM Cortex-A53 runs Python several times
slower, so router CPU figures will be higher. Measure on the AW1000 with
`sh bandwatch-test-round.sh perf`.

## Measured (x86-64 dev VM, demo mode, 45 days of history, 2 s WAN sampling)

| Phase | CPU (avg of 30 s windows) | RSS | Threads |
| --- | --- | --- | --- |
| Idle, no dashboard open, 3 min | 0.07–0.13 % | 27.2–27.3 MiB (flat) | 4 |
| 4 live SSE dashboards + overview/history polling every 5 s, 3 min | 0.67–0.73 % | 28.8–29.1 MiB | 8 |

* Production mode in the OpenWrt 25.12 container (x86-64, unprivileged
  service, no Bandix/aria2 present): 24.8 MiB RSS (VmRSS), 4 threads,
  0.18 % CPU averaged over 60 s idle, 164 KiB written in the first two
  minutes (database creation; no periodic flush had happened yet).
* 20 parallel API clients and 8 SSE streams: no errors, 95th percentile
  latency under 5 s on a loaded test runner (`test_concurrent_dashboard_clients`).
* Two simulated days of 2 s samples with periodic flushes: totals exact to the
  byte, pending buckets bounded (≤ 16), 1-minute rows bounded by retention
  (`test_long_running_bounded_memory_and_exact_totals`).

## Design measures

* **Two collector threads.** `wan` (interface counters, router health) never
  waits on network, subprocesses or the database. `providers` (Bandix,
  nlbwmon, ubus Wi-Fi lists, aria2, flushes) may block on timeouts.
  `test_slow_provider_does_not_block_wan_sampling` keeps 1 s WAN sampling
  regular while Bandix answers after 2.4 s.
* **Flush never blocks sampling.** Accumulation and SQLite I/O use separate
  locks (`test_flush_io_does_not_block_sample_accumulation`).
* **Flash wear.** Samples are batched in RAM and written every 5 minutes
  (configurable 30 s – 1 h) in one transaction; one row per series per minute;
  idle devices write nothing. In the OpenWrt container a fresh install wrote
  ~170 KiB in its first minutes (schema creation); steady state is a few KiB
  per flush.
* **Bounded memory.** Live ring buffers (≤ 15 min), per-device state TTL
  (15 min), event/task tables capped (2000/1000 rows), session/rate-limit maps
  capped, request bodies ≤ 64 KiB, ≤ 32 worker threads, ≤ 8 live streams.
* **Subprocesses only at slow intervals**: ubus Wi-Fi lists every 15–30 s (max
  8 objects, 2 s timeout); vnStat only on request.
* **Crash safety.** WAL journal with `synchronous=NORMAL`; `SIGKILL` during
  operation leaves a database that passes `PRAGMA integrity_check`
  (`test_unexpected_shutdown_keeps_flushed_data`). Up to one flush interval
  of samples can be lost on power failure.
