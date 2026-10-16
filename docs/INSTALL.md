# Development and installation

This is an experimental source release. Do not point a development checkout at
an existing production database or replace an installed service during setup.
No installer, credentials, runtime policies, or operating-system permissions are
provided by cloning this repository.

## Requirements

- macOS for the native app and platform-specific helpers.
- Python 3.12 or later. The core has no mandatory third-party Python packages.
- Swift 5.9 or later, with an Apple SDK or Xcode for native builds.

## Inspect without collecting

From the repository root:

```bash
python3 -I -S -B core/pam_isolated_launcher.py --help
python3 tools/release_privacy_scan.py
swift build --package-path apps/PersonalAIMonitorMenuBar
```

Build output is local development output. It is not a signed or notarized
release and must not be copied over an existing installed app as a shortcut.

## Core checks

Run the portable checks listed in `.github/workflows/ci.yml`. The complete test
collection is also retained, but some suites require the optional operator
integrations documented in [public source scope](PUBLIC_SOURCE_SCOPE.md).

The source-admission package reads its configuration from
`~/Library/Application Support/AGI/source-governance`. No configuration is
created automatically. An absent policy admits no personal sources; an absent
conversation grant permits no conversation reads. Keep any future policy file
private and outside version control.

Starting collection, enabling a model, writing memory, or granting repair
permissions are separate setup steps. Review the corresponding source and
privacy documentation before opting in. This release does not configure those
steps or install a background service for you.
