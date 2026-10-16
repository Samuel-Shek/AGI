# Security Policy

## Supported Versions

Personal AI Monitor is pre-release software. Only the current `main` branch is considered
supported for security fixes until the first tagged Open Alpha.

## Reporting A Vulnerability

Do not open a public issue for vulnerabilities involving capture, deletion,
local dashboard access, HA Source Packages, secrets, raw keyframes, OCR text, or
privacy bypasses.

Use GitHub private security advisories when available. If you do not have access
to advisories for this repository, contact the maintainer through an existing
private channel.

## Security Boundaries

- Public positioning is keyframe-only.
- Runtime data under `core/data/` is sensitive and must not be committed.
- OCR/VLM source text must be filtered before any durable write.
- v0.3.16 may retain a source image only inside the Core-private
  `confirmation_evidence.noindex` directory while a source-bound confirmation is
  `pending` or `deferred`. It has no independent TTL, no external/raw read API,
  and local clarification must run new OCR through Privacy Firewall before use.
- Terminal answers, filtering, source/range/retention deletion, and privacy
  reclassification must release confirmation evidence. A failed unlink stays
  `delete_pending` for reconciliation and must not be reported as deleted.
- Daily review must use only `clear` extracted activity.
- HA Source Packages must expose redacted activity only and must never include
  raw capture data.
- The HTTP dashboard is development-only and must remain bound to `127.0.0.1`.
- Dashboard Host and Origin/Referer checks must stay local-only; non-loopback
  bind hosts are unsupported.
- VLM and learning text model endpoints must be loopback-only unless a future
  release adds an explicit cloud opt-in with a separate security review.
- Source Alpha LaunchAgent installs must keep VLM review disabled unless the
  user explicitly opts in with `install-launch-agent --enable-vlm`.
- Browser extension permissions and stored fields must stay aligned with
  [Browser Extension Privacy](docs/BROWSER_EXTENSION_PRIVACY.md).
- Daemon mode must not start the dashboard unless a developer explicitly enables
  it.
- The public threat model is maintained in
  [Threat Model](docs/THREAT_MODEL.md).

## Public Release Requirements

- Run the release privacy scan.
- Follow [Release Checklist](docs/RELEASE_CHECKLIST.md).
- Run a full Git history secret/privacy scan.
- Enable GitHub secret scanning and push protection.
- Build signed and notarized macOS release artifacts.
- Verify install and uninstall instructions on a clean macOS user account.
