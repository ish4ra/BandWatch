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
| 4 | Hardware compatibility and upgrade from beta.1 | in progress: code-level upgrade test from the published beta.1 done; OpenWrt package upgrade test, docs pending |
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

## Firefox evidence so far

- Run https://github.com/ish4ra/BandWatch/actions/runs/45304144386: 2 Firefox
  runs x 8 variants x 30 loads = 480 loads, **zero stalls** (standalone server
  with fetch/SSE/service worker/keep-alive/Connection: close, context close and
  in-context navigation; BandWatch demo with SW allowed/blocked).
- Same commit, full UI suite (CI run 45304144223): stalled at the 3rd context
  (tablet) - `page.goto` waiting for `load` with 0 pending requests.
- Next experiment (pushed): variants mirroring ui-test.js exactly (5 demo
  servers, viewport per context, listeners, keep-open vs real close, SW
  allowed vs blocked).

## Resume instructions

1. `git fetch origin && git checkout claude/bold-einstein-wrbcwb && git pull`
2. Read this file's milestone table; continue with the first pending one.
3. Backend tests: `python -m pytest -q`. UI tests: `node tests/frontend/ui-test.js`
   (needs `npm install` in `tests/frontend`).
4. Firefox evidence: latest run of "Firefox stall investigation" workflow.

## Known failures

- Firefox UI job (issue #2) - inherited from beta.1, under investigation.
