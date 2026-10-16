# AGI

[中文说明](README.zh-CN.md)

AGI is an experimental macOS app for reviewing local work, tracking file changes,
and managing evidence-backed improvement candidates. It combines a SwiftUI app
with a Python core and evolved from Personal AI Monitor.

The name is a project name. This repository does not demonstrate artificial
general intelligence, reliable autonomous repair, or proven long-term benefit.

## What is included

- A native Mac app for recent activity, file history, decisions, and system status.
- File tracking with coverage checks and explicit uncertainty about authorship.
- Local source admission, revocation, privacy filtering, and audit records.
- Optional conversation reflection and controlled improvement workflows.
- Core, Swift, and synthetic integration tests; browser-extension source.

Screen capture requires explicit consent. Reflection, model use, memory writes,
and software changes have separate controls. Missing source configuration admits
no personal sources. No private policies, conversation history, credentials,
runtime databases, screenshots, or installed binaries are included.

## Start with the source

You need macOS, Python 3.12 or later, and Swift 5.9 or later. See
[development and installation](docs/INSTALL.md) for safe commands that use a new
local data directory. There is no signed, notarized installer in this release.

```bash
git clone https://github.com/Samuel-Shek/AGI.git
cd AGI
python3 -I -S -B core/pam_isolated_launcher.py --help
swift build --package-path apps/PersonalAIMonitorMenuBar
```

The Python core uses the standard library. The source-admission helpers needed
for core imports are bundled under `core/activity_mvp/source_governance`.
The original operator-specific memory, scheduler, backup, and deployment adapters
remain optional integrations. They require independently configured dependencies
and grants; missing prerequisites must keep those operations disabled. See
[public source scope](docs/PUBLIC_SOURCE_SCOPE.md).

## Development and privacy

Use synthetic data for tests and issues. Never attach real conversations,
screenshots, databases, tokens, or personal activity logs. Run the
[release privacy scan](tools/release_privacy_scan.py) before publishing changes.
See [security](SECURITY.md), [privacy](docs/PRIVACY.md), and
[contributing](CONTRIBUTING.md).

Portable checks cover selected core boundaries and the evolution engine.
The complete operator-integration and installed-app acceptance procedures are
separate; a passing subset does not certify every workflow. The project remains
experimental, especially its natural repair and reflection outcomes.

## License

[AGPL-3.0-only](LICENSE). Third-party notices and the separately licensed Hermes
component are retained in [NOTICE](NOTICE) and
[the vendor provenance](core/evolution/ai_ceo/vendor/PROVENANCE.json).
