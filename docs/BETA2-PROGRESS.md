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
| 2 | Database reliability and diagnostic privacy | pending |
| 3 | Automatic local diagnostics and one-click report | pending |
| 4 | Hardware compatibility and upgrade from beta.1 | pending |
| 5 | Firefox investigation (issue #2) | pending (CI results needed) |
| 6 | Manual update checker | pending |
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

## Resume instructions

1. `git fetch origin && git checkout claude/bold-einstein-wrbcwb && git pull`
2. Read this file's milestone table; continue with the first pending one.
3. Backend tests: `python -m pytest -q`. UI tests: `node tests/frontend/ui-test.js`
   (needs `npm install` in `tests/frontend`).
4. Firefox evidence: latest run of "Firefox stall investigation" workflow.

## Known failures

- Firefox UI job (issue #2) - inherited from beta.1, under investigation.
