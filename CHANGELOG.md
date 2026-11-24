# Public source changes

## 2026-11-24 — Local web workbench

- Run the public workbench in a browser with Python and the included fictional dataset. The default session keeps its drafts in a temporary directory.
- Moved the web source to `web/` and retired the native desktop source, packaging scripts and preview build workflow from the current distribution.

## 2026-11-23 — PSI workbench and file history

- Browse owner questions and processing records in the native app or the loopback workbench. Try the included synthetic fixture.
- Preserve workbench drafts across closing and reopening, and reconcile Codex handoff receipts before retrying.
- Refresh file history while its view is open, with coverage and date selection preserved.

## Initial public snapshot

- Publish the experimental native app, Python core, tests, and source artwork.
- Bundle the core source-admission dependencies with empty-deny defaults.
- Replace personal identity fixtures with synthetic examples.
- Remove the private memory translation catalogue, deployment notes, job
  registrations, local evidence, runtime data, and private Git history.
- Retain the original AGPL license and third-party notices.

This is a source publication, not an installed-app or natural-effectiveness
acceptance. Subsequent public releases should record their own validation.
