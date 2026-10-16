# Roadmap

Personal AI Monitor is now a personal local product for this Mac. The roadmap
optimizes for high recall, diagnosability, and a safe Hermes/Codex optimization
loop instead of a public release default.

## Current Contract Gate (2026-07-22)

The version bullets below are retained as implementation history. Where they
conflict with this gate, they are superseded and must not be used as current
authorization or deployment proof.

- Capture only on explicit `unlocked` plus `on_console=true`; unknown, missing,
  helper-error, locked, and off-console states fail closed.
- The helper derives that explicit state from CGSession first and the local
  `IOConsoleLocked` registry property as a bounded fallback. A missing CGSession
  lock key alone is never treated as unlocked; any explicit locked signal wins.
- Deletion and retention share one provenance-graph selector and cascade,
  including activity sessions and other time-locatable derived records.
- HA/Hermes and the optional loopback AI Connector are read-only analysis
  consumers. Hermes/model execution has no terminal, file, or memory tools.
- The controlled PAM writer is the only future-permitted long-term-memory write
  path, but v0.3.17 reports `writer_enabled=false` and
  `source_revocation_cascade_ready=false` until receipt-ID/provenance-based
  delete and retention revocation are closed-loop. New-candidate
  `memory-approval` preview/apply fail closed; workflow-only, third-party, and
  ambiguous candidates remain non-writable in any future design.
- The old Obsidian memory ledger is frozen read-only in the iCloud default
  vault. Feishu may label completion only for verified controlled-writer events;
  while the v0.3.17 writer is disabled, any unverified model claim remains
  analysis/advice and is not a write-completion event.
- Quick diagnostics are `partial`; full health requires full probes.
- v0.3.20 retains the legacy activity-confirmation list contract v4,
  report-context contract v2, and SQLite schema 28. Its cards contract v5 adds
  cursor paging, a cheap revision, organized presentation fields, and a separate
  low-attention information-insufficient section. It also accepts a
  notification-eligible `explicit_claim`
  without an episode ID while keeping `business_episode` strict, and shares one
  verified-count/content-readability state across the App's badge, Today prompt,
  and Confirmations page. Adjacent signals no more than five minutes apart form a
  business episode; repeated clues remain revision-checked and public results
  expose only safe text and content-free evidence availability with
  `raw_access=false`.
- v0.3.21 preserves all v0.3.20 data and confirmation contracts and changes
  only the native shell's top-level menu-bar label: its periodic Timeline is
  removed so startup can reach the main window without a continuous status-bar
  update loop. Local countdowns inside open content remain.
- v0.3.22 preserves schema 28 and the v4/v5/v2 contracts while policy v7 routes
  fragmentary, menu-like, or category-only subjects to
  `information_insufficient`. These cards have no answer actions, cards v5
  continues to omit `cleaned_contents`, and existing lazy paging/revision paths
  remain unchanged.
- High-value interview, meeting, call, application, submission, send, and
  publish signals become occurrence questions only when their subject is safely
  identified. Missing or ambiguous objects remain information-insufficient;
  context-only preparation remains non-occurrence. Standard mode
  asks roughly 10-20 questions per day and stratifies ordinary business context
  to at most 12; coarse calibration may rank or deduplicate but never silently
  reject a new high-value subject/date/session.
- Each v4 card records occurrence, workstream, and outcome. Manual omission
  capture creates a sanitized local fact path. `report-context` marks
  confirmation completeness as complete, pending, partial, or unavailable;
  unavailable quantities remain unknown rather than becoming zero.
- Incremental confirmation writes regroup the complete affected local day.
  Completion OCR prefix variants merge only under conservative App, 15-minute,
  and minimum-subject-length guards. Policy v7 revalidates unresolved rows and
  filters explicit negated-completion or strong game reward/UI false positives
  without a user answer or calibration; fragments without a concrete object
  remain information-insufficient and expose no answer action.
- Local clarification may re-open only the source image retained for that
  `pending/deferred` question, run Apple Vision OCR again, and send the result
  through Privacy Firewall. Pre-v0.3.16 cards without a source reference report
  `evidence_unavailable` rather than guessing another screenshot.
- Confirmation evidence has no independent TTL. It is released by terminal
  answers, system filtering, source/range/retention deletion, or privacy
  reclassification; failed unlink remains `delete_pending` for reconciliation.
  `ignored` is terminal but means only “remove this question”: no denial,
  explanation, calibration, generalized rule, or downstream fact is created.
- The v0.3.22 deployment starts a new seven-day soak baseline and stays
  `in_progress`; source tests and earlier observation history cannot pass it.

## v0.3.9 Trust & Soak Gate

- The active product milestone is seven days of reliable personal use, not new
  capture scope. `status` exposes content-free session evidence, severity/scope,
  guided remediation, recheck state, and a machine-readable soak scorecard.
- `soak status|snapshot|baseline|record-win|record-drill` measures the nine
  acceptance gates. A build or short restart cannot mark the seven-day gate as
  passed.
- Main-display attribution may fall back to ScreenCaptureKit window metadata
  only when the frontmost probe is unknown. Secondary displays remain isolated
  and are ranked by their own safe window overlap; no cross-display inheritance.
- v0.4 activity graph work stays blocked until the soak gate passes. The writer,
  cloud sync, audio, clipboard, keylogging, team monitoring, and general-purpose
  or archive-style raw image retention remain out of scope. The v0.3.17
  source-bound pending/deferred confirmation evidence above is the only bounded
  exception and has no gallery, export, or external read path.

## Current Core

- Local SQLite timeline and FTS search.
- Keyframe-only capture path for all connected displays.
- Privacy Firewall for high-risk apps, domains, keywords, secret-looking values,
  payment flows, and credential pages.
- Content-free gap events for uncertain input.
- Local daily review generated from `clear` activity only.
- Optional HA Source Package validation, revocation, and retention cleanup.
- Development-only HTTP dashboard with destructive-action confirmation.
- Today-and-yesterday default retention cleanup.
- Native macOS menu bar Alpha shell around `personal-ai-monitor-core`.
- Generated macOS App Icon for source-built app bundles.
- Simplified native control window with overview, search, review, and privacy
  sections.
- Privacy boundaries, Screen Recording permission state, local data folder
  access, and install/uninstall guide shortcuts.
- Native start/stop controls for the local LaunchAgent recording service.
- Native pause presets for 15 minutes, until next lock, today, and until
  manual resume.
- Quick delete-range presets for recent 15 minutes, recent 1 hour, today, and yesterday.
- Device-owner confirmation before time-range deletion from the native app.
- Time-range deletion now reports cascade cleanup across search, summaries,
  HA package caches, source records, coverage, and provenance references.
- Status output includes coverage health, plain-language explanations, and
  attention items for service errors, residue, recent failures, VLM queue
  pressure, and stuck backlog hotspots.
- Restricted VLM pending backlog with lock-screen drain: no new capture while
  locked, short TTL raw image queue, hard job/byte caps, and `ac_only` default
  power policy. The live runtime preserves the existing local `qwen3-vl:4b`
  opt-in during reinstall.
- Loopback-only model endpoint guard for VLM and learning text classification.
- Browser extension permission audit in the release privacy scan, with the
  permission boundary documented for local installs.
- Quality-report and local quality-feedback loop for measuring cleaning
  status, VLM review health, DocumentSignal coverage, memory candidates, and
  user-marked false clear/false block/over-redaction issues without storing raw
  screenshots, raw OCR, or uncleaned text.
- Privacy profile defaults to `personal_high_precision`; `strict` remains a
  manual downgrade mode. Hard privacy blocks still cover passwords, verification
  codes, real secret assignments, private keys, payment/bank material, adult
  content, private browsing, and password managers.
- DocumentSignal keeps explicit file/app integrations and adds high-recall
  scoring across safe local document roots, especially Obsidian Markdown,
  resume/interview/job materials, recent documents, file/dir names, OCR/AX
  visible text, and local text snippets. Metadata-only and low-score candidates
  stay out of timeline and HA document context.
- Daily Hermes quality advice reads only `quality-report` and
  `get-package --memory-candidates-only`, stores redacted suggestion summaries,
  and leaves code/rule changes to Codex after review.
- Hermes activity analysis is tool-less and analysis-only. PAM quality-gates
  candidates into eligible, review-only, and ephemeral review buckets, but none
  is a current write input while the v0.3.17 writer gate is disabled.
- A future write-report loop may start only after a re-enabled controlled writer
  obtains a verified mem0 receipt. The current version has no verified new write
  to report to Feishu. Independently, old activity-data snapshots outside
  today+yesterday remain strict cleanup candidates except unresolved
  confirmation sources, which ordinary retention holds until a terminal answer
  or explicit privacy/deletion action. Non-activity source/config backups are
  retained.
- v0.1.16 makes the native Overview more daily-usable with a Today Health Center
  for recording, privacy, coverage, HA/VLM automation, and data hygiene.
- v0.1.17 makes long-term memory governance visible in the native app with a
  read-only Memory tab for ledger status, memory-audit counts, safe issue
  summaries, ledger opening, and read-only audit export.
- Historical v0.1.18 turned that Memory tab into a ledger-driven approval
  console for previewed PAM memory delete/rewrite actions and safe HA candidate
  writes. Those mutation paths are superseded: the ledger is frozen and v0.3.13
  candidate-write preview/apply fail closed.
- v0.1.19 adds a read-only AI handoff package for Codex, Hermes, or ChatGPT.
  It exposes normal clear safe summaries, project/signal groupings, and
  count-only privacy/gap/redaction diagnostics with no raw access or write
  capability.
- v0.1.20 brings the AI handoff package into the native Review page so daily
  project progress, decisions, follow-ups, blockers, and low-confidence warnings
  are readable without exporting a file first.
- v0.1.21 adds a read-only Privacy Policy preview so the user can understand
  hard-block apps, communication apps, high-risk domain hints, and app/domain
  effects without writing rules from the native app.
- v0.1.22 adds a safe local Diagnostics Center and `diagnostics status` package
  that explains which layer needs attention without exposing raw sources, local
  paths, tokens, or package bodies.
- v0.2.0 adds the manual loopback read-only AI connector. External AI tools can
  GET `/status`, `/package`, and `/diagnostics` from localhost, but cannot call
  write methods, raw data, memory writes, deletion, approval, or rule changes.
  `/package` is intentionally lightweight so live AI handoff stays responsive.
- v0.2.1 fixes native Review-page reachability in compact windows and restores
  a stable menu-bar countdown mark without expanding capture or connector scope.
- v0.2.2 moves common actions and today metrics to the top of native Overview
  and clarifies busy/capturing status text without changing capture cadence.
- v0.2.3 makes long-term memory quality stricter: ordinary single-evidence
  candidates are review-only, repeated DocumentSignal/trusted-app summaries
  merge before controlled-writer review, role-requirement/noisy UI contexts stay out
  of direct writes, and diagnostics downgrades transient SQLite/data hygiene
  findings to clear warnings.
- v0.3.0 makes the connector a managed but still manual menu-bar capability:
  default stopped, loopback-only `127.0.0.1:8766`, GET-only, no memory/rule/delete
  actions, PID-safe start/stop, attribution-health counters, diagnostic action
  classes, and profile-level privacy switching with confirmation.
- v0.3.1 stabilizes the new observability layer: fast diagnostics for the app
  and connector, probe timing in full diagnostics, attribution health v2 for
  Unknown/fallback/extension/Accessibility signals, active-vs-ledger gap wording,
  profile dry-run previews before switching, and recent memory-governance trends.
- v0.3.2 adds a first-level `待决策` queue in the native app so uncertain safe
  memory candidates and AI Review signals wait for explicit user decisions
  instead of silently staying ambiguous.
- v0.3.3 turns that queue into plain-language question cards, hides unavailable
  actions, and folds duplicate/noisy/hard-blocked items out of the main list.
- v0.3.5 adds unlock-to-lock activity sessions backed by PAM's local safe
  timeline: primary focus share, multi-display visible exposure, Top Apps,
  trusted browser domains, privacy/gap counts, `activity-sessions` CLI, and
  status summaries without reading macOS Screen Time private data.
- v0.3.6 tightens P0 attribution accuracy for those sessions: safer display/window
  App matching for secondary monitors, context-only/noise region scoring, no
  cross-display browser inheritance, and Unknown/low-confidence/media-exposure
  explanations in CLI/status/native UI.
- v0.3.7 finishes the P0.1 accuracy cleanup: attribution health no longer treats
  all `sck-display-*` ids as secondary by default, session quality uses safe
  display attribution reason/confidence, and secondary browser domain attribution
  requires one unambiguous safe browser signal.

## Competitor Feedback Priorities

The next product iteration absorbs competitor lessons without copying their
surveillance or cloud-first risks:

- From Recall, ManicTime, and Screenity: make recording state, pause state,
  privacy protection, errors, and deletion controls obvious in the primary UI.
- From ActivityWatch and ManicTime: build the low-sensitivity facts layer first:
  app, window, URL, AFK, virtual desktop, coverage health, and project tag seeds.
- From ActivityWatch-style browser watchers: replace long-term AppleScript
  fallback with browser URL, private-mode, risky-domain, and login/payment page
  signals that feed the Privacy Firewall.
- From screenpipe and TimeScroll: prefer structured Accessibility or
  browser-extension text where reliable, and keep OCR as the fallback for
  inaccessible regions, visual state, and review.
- From Familiar: keep raw keyframes short-lived and prefer text-only retention
  with deletable provenance after Privacy Firewall clearance.
- From Windrecorder and Pensieve: schedule OCR, maintenance, compression, and
  index cleanup during low-load windows with visible backlog/degradation status;
  current lock-screen VLM drain is the first constrained version of this idea.
- From screenpipe-style agent access: keep the v0.1.19 `ai-handoff` package
  narrow and read-only: `clear_only` safe activity, content-free
  gap/privacy/duplicate events, no raw images, OCR source text, paths, or
  sensitive window details.

## Next Personal Upgrades

- Developer dashboard disabled by default in daemon mode.
- Privacy control panel can later graduate from v0.1.21 read-only preview to
  explicit app/domain rule management, but only with preview, confirmation, and
  audit gates.
- Coverage health view refinements for per-display trends and longer windows.
- Diagnostics can later grow into guided repair flows, but only after each
  repair action has preview, confirmation, and audit output.
- Memory-audit medical-risk items remain read-only historical review findings.
  The frozen ledger cannot mutate memory; any future repair path must be a
  separate controlled, previewed, explicitly confirmed operation.
- Re-enabling the controlled mem0 writer requires receipt-ID and complete
  provenance persistence plus exact delete/retention revocation coverage,
  explicit tests, and verified no-write behavior on any missing prerequisite.
- The v0.2.0 AI connector can later become a managed launch service or MCP
  wrapper, but it should remain opt-in, loopback-only, GET-only by default, and
  backed by the same privacy scans.
- Better per-display window attribution and summaries.
- Structured-signal hardening for browser extension install health,
  Accessibility fallback confidence, and longer-window DocumentSignal quality
  trends.
- More complete deletion recomputation for overlapping summaries.
- Release privacy scan remains a local upgrade gate.
- [Release checklist](RELEASE_CHECKLIST.md) remains the local safety checklist
  for privacy scan, tests, app build, sensitive-file audit, and installed-app
  verification.
- Read-only data and backup hygiene reports for old copies, backup cleanup
  candidates, and runtime locations.

## If Publication Is Reconsidered Later

- Re-run privacy and secret scans over the full Git history.
- Enable GitHub secret scanning and push protection.
- Keep real activity data, logs, screenshots, raw vault contents, and HA package
  caches out of the repository.
- Publish AGPL-3.0-only license and safety policy.
- Add signed and notarized GitHub Release app artifact.
- Add user-facing installation and uninstall documentation.
- Keep the [threat model review](THREAT_MODEL.md) current for non-developer
  users.

## Explicitly Out Of Scope For The First Public Version

- Paid accounts, billing, or hosted service.
- Team dashboards or employee monitoring.
- Default cloud sync.
- Audio recording, keyboard logging, clipboard reading, or chat transcript
  capture.
- Short video buffering, unless the privacy model is redesigned and reviewed.
- Hidden running state, employee monitoring features, team admin dashboards, or
  default cloud sync.
