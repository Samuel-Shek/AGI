The system status page separates background service, screen capture, and file tracking, with compact statistics and collapsed details. AGI visual-model review is retired; local OCR and privacy protection remain. Historical records and privacy-only legacy cleanup are retained. Code recovery point: `archive/vlm-before-retirement-20261001-212137`.

# Personal AI Monitor Menu Bar

This is the native macOS shell for the personal local Personal AI Monitor
runtime. It wraps the local `personal-ai-monitor-core` command channel and does
not read SQLite directly.

v0.3.22 uses six permanent destinations grouped by purpose: `今天 / 待确认 / 回顾`
for daily work and `系统状态 / 隐私与数据 / 设置` for management. The confirmation
badge is independent of the disabled long-term-memory writer. High-impact
occurrence questions are answered locally and unresolved items cannot enter
Today, Review, AI handoff, or Hermes/Feishu report facts. Advanced Settings now
contains only low-frequency AI Connector, and memory-governance
controls, and its entire header row is keyboard- and VoiceOver-accessible.
This schema-28 hotfix preserves the legacy list contract v4 and adds the
cards-only contract v5 with 12-card cursor pages, semantic sections, and a
cheap revision endpoint. It accepts a
notification-eligible `explicit_claim` without an episode ID while still
requiring one for `business_episode`, shares verified-count/content-readability
state across the sidebar badge, Today prompt, and Confirmations page, and keeps
unavailable-evidence copy separate from delete-pending cleanup copy.
The v0.3.21 shell hotfix removes the per-second Timeline from the top-level
menu-bar label so launch can reach the main window without continuously
rebuilding the status-bar button. Countdown Timelines inside already-open menu
and window content remain local to those views.
The v0.3.22 confirmation-quality hotfix preserves that shell fix and the
existing lazy 12-card paging, revision refresh, parent-owned draft state, and
on-demand diagnostics. Information-insufficient cards show only organized
title/evidence and never expose answer actions; cards v5 rejects legacy
`cleaned_contents`.
The Confirmations page is action-first: it shows the subject, exact question,
post-firewall trigger sentence, time and grouped-clue count before any explanatory
material. Full cleaned recognition text, feedback explanation, recent calibration,
and privacy notes are collapsed by default.
v0.3.7 adds reason summaries to the `时间` attribution-quality area, using the
core's safe `explanation_lines` and reason counts for Unknown/low-confidence
causes without adding correction buttons.
v0.3.6 adds attribution-quality prompts to the `时间` view: Unknown share,
low-confidence share, explanation lines, and secondary-screen media exposure.
v0.3.5 added the `时间` view and Overview card for unlock-to-lock activity
sessions: duration, primary activity share, media/work split, Top Apps, trusted
browser domains, privacy counts, and gap counts from the local safe timeline
rather than macOS Screen Time private data.
v0.3.3 turns `待决策` into Chinese question cards: each item explains what the
system found, why it is unsure, and which choices are available. It also hides
non-actionable write buttons and folds duplicate/noisy/hard-blocked items into
an automatic-handled count. Historically, v0.3.2 added the first-level `待决策`
queue and a dry-run write-preview design; v0.3.9 supersedes that write behavior
by keeping the controlled writer and all new-candidate preview/apply paths
fail-closed.
v0.3.1 makes diagnostics fast by default, adds attribution-health v2 cards,
shows memory-governance trends, and previews privacy profile changes before
confirmation. v0.3.0 adds menu-managed start/stop/status for the read-only AI Connector,
keeps it stopped by default on `127.0.0.1:8766`, and surfaces memory governance,
attribution, diagnostics action classes, and privacy profile confirmation in
the native app. v0.2.3 tightens long-term memory quality gates and makes
diagnostics less prone to treating transient SQLite write windows as product failures. v0.2.2 moves Overview's daily controls and metrics to the top and labels
transient write/capture states without treating them as stopped recording.
v0.2.1 keeps the Review page scrollable in compact windows and uses a stable
menu-bar countdown mark. v0.2.0 added a manual loopback read-only AI Connector
for external AI handoff. v0.1.22 keeps the Review page directly readable and adds a safe Diagnostics
Center on Overview. v0.1.21 added a read-only Privacy
Policy preview. The app calls the core `ai-handoff package` command for project
review, `privacy-policy status|preview` for firewall effects, and
`diagnostics status --mode quick` for a partial local-health snapshot. The connector uses GET-only localhost
HTTP and exposes only safe handoff/diagnostic packages. The app does not read
SQLite directly, render raw activity details, expose local paths, or write
privacy rules.

## Current UI Contract (2026-07-20)

- Recording UI may show active capture only when the backend explicitly reports
  `unlocked` and `on_console=true`. Missing/unknown/error/off-console state is a
  stopped or attention state, never inferred as safe to capture.
- Running shows Pause, paused shows Continue Recording, stopped shows Start
  Recording, and locked or unknown states show an explanation with no invalid
  action. Capture Now, Refresh, and Stop Service are secondary menu actions.
- Today reads its activity count, duration, and primary activity only from the
  core `confirmed_activity` contract. If that contract is absent or unsafe, the
  page says confirmed facts are unavailable and never falls back to an
  unconfirmed session summary.
- `待确认` prefers contract v4 `question_groups` and falls back safely to v3/legacy
  fields when connected to an older Core. Each v4 card confirms occurrence,
  business workstream, and one fixed outcome; the page also exposes a content-free
  funnel/filter diagnostic, a manual missed-fact entry, and compact/standard/max-recall settings.
  Each card leads with the identified
  subject (or explicitly says company/round is unknown), date/time, source count,
  exact question, and actual post-firewall trigger sentence.
- Policy v7 still filters task lists, PAM self-UI loops, conditional/negated
  completion wording, and strong game reward/UI false positives. Fragmentary,
  menu-like, connective-prefix, and category-only subjects appear only as
  `information_insufficient` cards without answer actions; none becomes a fact.
- Full cleaned recognition content is deduplicated, annotated with occurrence
  counts, and collapsed by default. This disclosure never means raw screenshots
  or pre-filter OCR. Feedback behavior, recent calibration, and privacy notes are
  below the questions and collapsed by default.
- The clarification field starts expanded. Its single `确认并重新梳理` action is
  placed beside the field on wide cards and below it on narrow cards; Return uses
  the same guarded request. Primary occurrence actions are `发生了`, `没有发生`,
  and `忽略该问题`; `暂时跳过` is secondary. Only `没有发生` opens the
  negative-reason choices. `忽略该问题` is a direct terminal dismissal: it does
  not record a denial, correction reason, note, or calibration. Group answers are
  atomic and stale membership refreshes instead of partially submitting.
- Clarification loads per card and may rewrite the question, split mixed subjects,
  or state what is still unknown. For a v0.3.16+ card with available evidence, Core
  locally reopens the retained source image and reruns OCR plus the Privacy
  Firewall before applying the latest sanitized local note. The note survives
  only unresolved retries and is erased when the question reaches a terminal
  state. The App never receives
  image bytes, raw OCR, a file name, hash, or local path; `raw_access` remains
  false. Old cards without retained evidence show `evidence_unavailable` rather
  than a false “re-read completed” result. Clarification does not itself answer,
  calibrate, or expose the group to Today/Review/external consumers.
- High-value episode reminders are content-free and fire only after the episode
  has been quiet for five minutes. A separate content-free 22:30 reminder is
  scheduled while pending questions remain; notification bodies never include
  company names, question text, OCR, or workstream details.
- Quick diagnostics, Trust & Soak, quality, and runtime live under System Status;
  permissions, authorized roots, privacy policy, and deletion live under Privacy
  & Data. Advanced Settings contains only AI Connector, and read-only memory
  governance. The old Obsidian ledger stays frozen and read-only; no
  rewrite/delete/sync actions are exposed.
- Hermes/model execution has no terminal, file, or memory tools and cannot write
  mem0 directly. The controlled writer is the only future-permitted write path,
  but v0.3.17 reports `writer_enabled=false` and
  `source_revocation_cascade_ready=false` until receipt-ID/provenance-based
  deletion and retention revocation are closed-loop. New-candidate `memory-approval`
  preview/apply must fail closed, and the UI must not present a write as
  available or completed.
- AI Connector remains optional, loopback-only, GET-only, and read-only. It is a
  supported consumer alongside optional HA analysis; neither path expands raw
  access.

## Build

From the repository root:

```bash
env CLANG_MODULE_CACHE_PATH=/private/tmp/pam-release-clang-cache \
  swift run --package-path apps/PersonalAIMonitorMenuBar --disable-sandbox \
  --sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
  --cache-path /private/tmp/pam-release-swift-cache \
  --config-path /private/tmp/pam-release-swift-config \
  --security-path /private/tmp/pam-release-swift-security \
  --scratch-path /private/tmp/pam-release-swift-build \
  --manifest-cache none PAMInteractionModelChecks
env SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
  ./scripts/build-macos-app.sh
open "build/Personal AI Monitor.app"
```

The explicit MacOSX15.4 SDK is required on the current packaging host; the
default SDK/compiler combination is not an accepted release gate.

Build metadata is deterministic:

- `CFBundleShortVersionString` is fixed to `0.5.1` by the public release wrapper;
  caller-provided version overrides are not release evidence.
- System Status includes the content-free seven-day Trust & Soak section. It
  never displays raw screenshots, OCR, full URLs, window titles, or local paths,
  and ordinary pages do not prompt for control drills.
- `CFBundleVersion` defaults to the local build timestamp `YYYYMMDDHHMM` and can
  be overridden with `PAM_BUILD_NUMBER`.
- The app bundle also stores build time, Git revision, and dirty-source state,
  and the native UI shows the version/build string.

For development, the app finds the core at `core/` relative to the
repository root. Override with:

```bash
PERSONAL_AI_MONITOR_CORE_DIR=/path/to/core open "build/Personal AI Monitor.app"
```

When the app is copied to `/Applications`, it no longer has a repository-adjacent
`core/` directory. In that installed shape it uses
`~/Library/Application Support/Personal AI Monitor/core` unless
`PERSONAL_AI_MONITOR_CORE_DIR` points elsewhere. Building the app and installing
or refreshing that live core runtime are separate steps.

LaunchAgent installs use the personal high-precision privacy profile by default:

```bash
cd core
python3 -m activity_mvp install-launch-agent
```



## Current Scope

- Generated macOS App Icon bundled into the source-built app, with a matching
  simplified menu-bar glyph.
- Native menu-bar app with six first-level destinations: Today, Confirmations,
  Review, System Status, Privacy & Data, and Settings. The old memory-write
  Decision Queue is not a permanent navigation item.
- Today is a compact surface for recording status and confirmed daily facts.
  Unresolved occurrence claims appear only in Confirmations and cannot populate
  Today activity text or counts.
- Today has at most one primary recording control. Capture Now, Refresh, and
  Stop Service are in the More menu.
- Review combines time-range selection, search, activity timeline, AI recap,
  and daily summary. Full logs, AI handoff, quality report, and other exports
  are in the More menu.
- Privacy & Data shows Screen Recording permission, privacy scope, authorized
  document roots, retention, data location, and limited time-range deletion.
- Advanced Settings contains AI Connector, and read-only memory governance.
  Its entire heading row expands or collapses with mouse, keyboard, and
  VoiceOver. Four control drills are not prompted in the ordinary interface,
  while the core statistics remain intact under System Status.
- Existing memory-ledger entries cannot be rewritten or deleted, and v0.3.17
  new-candidate write preview/apply remains fail-closed while the controlled
  writer gate is disabled.
- Device-owner confirmation before any time-range deletion.
- `personal_high_precision` is the default privacy profile. `strict` remains a
  manual downgrade, while hard privacy blocks stay enforced.

Signed and notarized public packaging is no longer the active product track.
