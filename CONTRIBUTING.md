# Contributing to BandWatch

Thanks for helping! BandWatch aims to stay small, accurate and private.

## Ground rules

* **Accuracy first.** Never fabricate or interpolate traffic as if it were
  measured. Missing data is a gap, not zero. Keep WAN, per-device and aria2
  measurements separate.
* **No new runtime dependencies.** Python standard library only on the
  router; no frameworks or CDNs in the frontend.
* **Read-only integrations.** Never change router, Bandix, vnStat or aria2
  configuration.
* **Privacy.** Do not commit real router dumps, MAC/IP addresses, hostnames,
  configuration files or credentials. Use synthetic fixtures (see
  `tests/fixtures/README.md`).

## Workflow

1. Fork and create a branch from `develop`.
2. Run the demo: `python3 -m bandwatch demo`.
3. Add tests for your change; run `python3 -m pytest`, `ruff check .`,
   and for UI work `node tests/frontend/ui-test.js`.
4. Open a pull request against `develop` (or `main` until a `develop` branch exists)
   and describe how you tested it.
   Hardware results are very welcome — use the hardware report template.

## Style

* Python: readable, small functions, `ruff` clean, comments explain *why*.
* JavaScript: vanilla ES modules, escape all dynamic text with `esc()`,
  no inline scripts/styles (the CSP blocks them).
* Shell: POSIX `sh` compatible with BusyBox ash; `shellcheck` clean.

## Reporting bugs

Use the issue templates and attach the output of `bandwatch-diag`
(already sanitised — please review it before uploading).
