# v0.1.0-beta.2 development progress

Working document for the beta.2 cycle, kept so an interrupted session can
resume without repeating work. Remove or archive it after the release.

- Branch: `claude/bold-einstein-wrbcwb` (from `main` @ `a70d81d`)
- Target: `v0.1.0-beta.2` (not published; maintainer approval required)
- beta.1 release and tag are never modified.

## Milestones

| # | Milestone | State |
|---|-----------|-------|
| 1 | Repository inspection, plan, Firefox reproduction harness | done |
| 2 | Database reliability and diagnostic privacy | done |
| 3 | Automatic local diagnostics and one-click report | done (UI + API + tests) |
| 4 | Hardware compatibility and upgrade from beta.1 | done in code/docs; OpenWrt container upgrade test (`tests/openwrt/upgrade-beta1.sh`) being stabilised in CI |
| 5 | Firefox investigation (issue #2) | in progress (see below) |
| 6 | Manual update checker | done (browser-side; UI test with GitHub mocked) |
| 7 | Final testing, packaging, docs, PR | pending |

## Findings from inspection (milestone 1)

AW1000 snapshot (one production-mode snapshot, router uptime 676 s):

- `last_flush: null` with 40 pending buckets and zero rows. A flush runs
  `storage.flush_interval` (300 s) after **service** start, and an empty
  flush still sets `last_flush`, so the service had been running < 300 s
  when the snapshot was taken. The snapshot is consistent with normal start-up;
  it cannot prove or disprove persistence. Root cause of the ambiguity: the
  diagnostics export does not include service uptime (it is only in
  `/api/health`).
- Real gap found: the clock is trusted only after 600 s of router uptime.
  Until then samples sit in an in-memory pre-buffer that `stop()` does not
  persist, so a restart in the first 10 minutes after boot (the init script
  restarts BandWatch when `lan` comes up) discards those samples.
  `pending_buckets` also does not count the pre-buffer.
- The diagnostics export removes a few fields from nested objects (deny
  list). Collector `message`/`notes`/`extra`, storage paths, event messages
  and settings are passed through; provider error strings (e.g. aria2
  `faultString`) are copied verbatim. Needs an allow-list.

Firefox (issue #2): Firefox cannot be installed in the development container
(Playwright CDN and Mozilla hosts are blocked by the network policy; Ubuntu's
`firefox` is a snap stub). Investigation runs in the isolated workflow
`.github/workflows/firefox-investigation.yml` with the standalone reproduction
in `tests/firefox-repro/`. New hypothesis not covered by earlier attempts: the
dashboard's network-first **service worker** (every request of a controlled
page is dispatched to it). Variants isolate SSE, service worker, keep-alive and
context close vs navigation, each in a fresh browser.

## Done in milestones 2, 3, 6

- `store.py`: write tracking (`last_write` vs empty flush), flush error
  categories, WAL checkpoint on clean close, additive `health` table (schema
  stays 1 so beta.1 can open the DB after a rollback).
- `engine.py`: lifecycle (`first_start`/`upgrade`/`restart`/`after_reboot`,
  clean vs unclean previous stop, bounded history in `meta`), pre-buffer kept
  across a restart within the same boot, early first save 90 s after the clock
  is trusted, `persistence_status()`, sampling-delay counters, stricter Bandix
  direction check (ignores router/aria2 traffic, symmetric traffic; adds
  `inconclusive`; never auto-swaps).
- `selfmon.py`: bounded 15-min resource intervals (96 in memory, 31 days in DB).
- `report.py`: allow-listed report, check statuses, ZIP + SUMMARY.txt.
- Collector `Status.code` (reports never include provider messages).
- API: `/api/diagnostics/report.zip|report.json|checks` (auth required);
  `/api/diagnostics` now returns the sanitised report.
- UI: Health (report card, self-check, Database & history, resource use),
  Settings (report card, Updates, data export). CSP `connect-src` adds only
  `https://api.github.com`.
- Tests: `test_persistence.py`, `test_report_privacy.py`,
  `test_upgrade_beta1.py` (needs `BW_BETA1_TARBALL`; CI downloads and
  verifies it), UI checks for report download and update check.

## Done in milestone 4

- Version bumped to 0.1.0-beta.2 (`__init__`, sw.js cache, pyproject, feed
  Makefile VERSION file, issue template, release workflow default).
- `tests/test_hardware_compat.py`: ModemManager `_4`/`_6` sub-interfaces,
  modem reconnect, aria2 with/without secret, no ssl/_hashlib, AW1000-shaped
  demo report.
- **Bug fixed**: an NTP clock step while the clock was untrusted set the
  trusted flag without merging the pre-buffer (samples stranded, then lost).
  Regression test `test_ntp_step_while_untrusted_merges_the_prebuffer`
  (fails on the old code).
- Self-monitor made thread-safe (summaries read from web threads);
  `oldest_sample_time()` cached (min() over m1/m15 scans whole tables).
- Docs: UPGRADE.md, UPGRADE-SINHALA.md, release notes, CHANGELOG, README,
  COMPATIBILITY, HARDWARE-TESTING (first physical result), PERFORMANCE
  (snapshot + measured cost of diagnostics, `tests/bench_diagnostics.py`),
  SECURITY, SECURITY-AUDIT (D1-D3), TROUBLESHOOTING, INSTALL, traceability,
  tester README; screenshots `health-dark.png`, `settings-report-dark.png`.

## Firefox evidence so far (issue #2)

Isolated workflow `firefox-investigation.yml`, reproduction `tests/firefox-repro/`.

1. Run 45304144386: standalone server + minimal BandWatch variants (fetch, SSE,
   service worker, keep-alive vs Connection: close, close vs navigate):
   2 x 480 Firefox loads, **0 stalls**.
2. Run 45306909427: variants mirroring ui-test.js (5 demo servers, viewport
   per context, listeners): Firefox stalls with SW allowed **and blocked**;
   keep-open contexts (beta.1 workaround) stalled in 4/4 runs, real close in
   1/4. Chromium: 0 stalls. => SW is not the cause; keep-open makes it worse.
3. Run 45312047044 (page probe at the stall): the stalled page reports
   `readyState: complete`, load event at ~100-130 ms, all assets loaded, the
   dashboard rendered and polling continuing - while **Playwright still
   reports `about:blank`**. Always the first navigation of a new page. =>
   the browser and app are fine; Playwright's Firefox driver (Juggler) lost
   the navigation lifecycle events, and every later wait hangs on that
   phantom navigation.
4. Run (round 3, pending): does priming a new page (`goto('about:blank')`
   or 500 ms delay) before the first navigation avoid it?

## Resume instructions

1. `git fetch origin && git checkout claude/bold-einstein-wrbcwb && git pull`
2. Read this file's milestone table; continue with the first pending one.
3. Backend tests: `python -m pytest -q`. UI tests: `node tests/frontend/ui-test.js`
   (needs `npm install` in `tests/frontend`).
4. Firefox evidence: latest run of "Firefox stall investigation" workflow.

## Known failures

- Firefox UI job (issue #2) - inherited from beta.1, under investigation.
