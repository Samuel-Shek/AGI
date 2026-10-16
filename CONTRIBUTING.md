# Contributing

Personal AI Monitor is privacy-sensitive personal software. Contributions must preserve
the local-first boundary and must not expand the project into surveillance,
employee monitoring, classroom monitoring, or team administration.

## Development

```bash
cd core
python3 -m unittest discover -s tests
```

Before opening a pull request:

- run the test suite
- run `python3 tools/release_privacy_scan.py` from the repository root
- do not commit runtime data from `core/data/`
- do not add real screenshots, logs, OCR text, HA packages, tokens, local paths,
  account data, or private chat content
- update `docs/PRIVACY.md` if capture, filtering, deletion, dashboard, daily
  review, or HA package behavior changes
- use synthetic examples that cannot be mistaken for real credentials

## Pull Requests

Keep changes focused. Privacy hardening, local recall quality, and deletion
correctness take priority over new capture surface area.

## Issue Hygiene

Do not attach screenshots, real databases, real logs, OCR output, raw keyframes,
or HA package files to public issues. Use synthetic reproduction data.
