# Open Source Safety

Personal AI Monitor is open source so users can inspect how local activity data is handled.
That openness must not make it easier to normalize surveillance.

## Project Position

- Personal use only.
- Voluntary capture only.
- Visible running state.
- Local-first by default.
- No employee monitoring or team admin dashboard.
- No stealth mode.
- No default cloud sync.

## Competitor Boundaries

Competitor review produced several explicit non-goals:

- Do not copy employee-monitoring, team-management, or child-monitoring product
  language from time-tracking tools.
- Do not copy hidden-running or hidden-icon patterns.
- Do not copy cloud-first Rewind-style storage or default remote sync.
- Do not copy audio lifelogging, meeting transcription, keyboard logging, or
  clipboard capture into the public Alpha.
- Do not market full-video retention as the value proposition; the value is
  safe local self-review after filtering and deletion.

The positive lessons to copy are visible status, local-first storage,
Off-the-record pause, clear cascade deletion, coverage health, plain-language
service status, app/domain exclusion, and auditability.

## Repository Hygiene

Never commit:

- runtime databases
- raw keyframes
- screenshots
- `confirmation_evidence.noindex` runtime contents
- OCR source text
- logs
- HA Source Package cache
- access tokens or credentials
- real activity records

Before public release, verify browser-extension permissions, local-only model
endpoint guards, and the source Alpha release gate. The browser extension
permission boundary is documented in
[Browser Extension Privacy](BROWSER_EXTENSION_PRIVACY.md).

Run before release:

```bash
python3 tools/release_privacy_scan.py
```

Also follow [Release Checklist](RELEASE_CHECKLIST.md) and run a full Git history
scan before broad public announcement.

Security assumptions and abuse paths are tracked in
[Threat Model](THREAT_MODEL.md).

## Issue And PR Rules

Public issues must use synthetic reproduction data. Maintainers should close or
redact issues that include real screenshots, logs, databases, HA packages, or
private activity content.

Security reports should use private security advisories, not public issues.

## Fork Policy

The project cannot technically prevent all harmful forks, but it can make the
intended boundary explicit:

- AGPL-3.0-only license.
- Anti-surveillance Code of Conduct.
- Safety language in README, docs, and issue templates.
- No team-monitoring roadmap.
