The system status page separates background service, screen capture, and file tracking, with compact statistics and collapsed details. AGI visual-model review is retired; local OCR and privacy protection remain. Historical records and privacy-only legacy cleanup are retained. Code recovery point: `archive/vlm-before-retirement-20261001-212137`.

# Personal AI Monitor Core

This directory contains the runnable Python core for Personal AI Monitor, a local-first
macOS personal context recall project.

The current core validates the personal live-runtime loop:

```text
keyframe capture -> Privacy Firewall -> SQLite/FTS -> local recall -> optional HA Source Package -> deletion/revocation
```

It is not the final signed macOS app. The native menu bar Alpha shell lives in
`../apps/PersonalAIMonitorMenuBar`.

## 0.4.0 Source Overlay (2026-08-01)

The 0.4.0 release-candidate source adds the governed Context graph and the
Today / Find / Memory contract surface. Target schema 31 preserves historical
activity and adds only `context_activity_confirmation_links` and
`context_product_feedback` relative to schema 30. `ContextBundleV1`,
`MemoryCandidateV1`, `DeliveryReceiptV1` and report-context v2 remain compatible.

The Core feature flag `context_console_phase1_enabled` is default-off and is
separate from the native UI flag. `ha_auto_package_enabled`,
`context_memory_writer_enabled` and
`context_source_revocation_cascade_ready` must remain false during rollout.
The Phase 3 tool rehearses 28→31 only on a SQLite-backup copy and preserves a
sealed 0.3.22/schema-28 rollback set; this source overlay is not evidence that a
live database was migrated or that the installed runtime is 0.4.0.

## Current Contract Overlay (2026-07-26)

This overlay is normative when it conflicts with version-history bullets below.
It documents the source contract only; installed and live-runtime state still
requires separate verification.

- Screen capture is fail-closed. A new capture round requires explicit
  `session_state=unlocked` and `on_console=true`; unknown/missing state,
  helper failure, off-console users, and locked sessions do not capture.
- `delete-range` and retention apply the same provenance-graph lifecycle to all
  time-locatable PAM-derived records, including activity-session rollups. A
  dry-run preview and apply result must use the same selector/count contract.
- HA/Hermes is an optional analysis consumer, not a memory writer. The optional
  loopback AI Connector is also a read-only consumer, so older "HA only
  consumer" wording is superseded.
- The controlled PAM writer is the only path that may ever write mem0, but its
  v0.3.22 global gate reports `writer_enabled=false` and
  `source_revocation_cascade_ready=false` because a receipt-ID/provenance-based
  delete and retention revocation cascade is not yet closed-loop. New-candidate
  `memory-approval` preview/apply fail closed. If the writer is enabled in a
  future release, it must still require human confirmation,
  `self_or_user_owned`, complete provenance, and a verifiable receipt.
- Hermes/model execution has no terminal, file, or memory tools. Model-reported
  writes are untrusted claims, not write receipts.
- The PAM Obsidian memory ledger is frozen/read-only at the canonical iCloud
  default-vault path `默认仓库/✦ AI 工作台/PAM 长期记忆台账（已停更）.md`.
  Historical paths are discovery-only and must not be created or modified.
- Feishu labels a write as completed only for a controlled-writer event with a
  verified receipt. An unverified model claim may be surfaced only as
  analysis/advice, never as "write completed".
- Quick diagnostics return a `partial` health view. Skipped probes, batch
  errors, or partial-display failures cannot be presented as full green.
- High-impact real-world occurrence claims are separate from long-term-memory
  decisions. `activity-confirmation list` contract v4 groups source-bound clues
  and five-minute business episodes without deleting provenance. It adds question
  kind, origin, priority, episode id, safe signal types, suggested/user workstream
  and outcome, plus content-free priority/kind/funnel counts. v3 fields remain
  readable for compatibility. `report-context` contract v2 exposes only confirmed
  safe facts and content-free `complete|pending|partial|unavailable` confirmation
  completeness; unknown counts are null rather than a fabricated zero.
- Confirmation policy v7 still hard-filters task lists, PAM self-UI loops, meta
  discussion, conditional/negated completion wording, strong game reward/UI false
  positives, and pure historical/navigation surfaces. Fragmentary outlines,
  menu strings, connective-prefix fragments, and category-only subjects become
  `information_insufficient` cards without answer actions. Context-only and
  Unknown App signals never auto-confirm.
  Existing unresolved noise is system-reclassified with
  `answer=NULL`; provenance and audit state remain, user answers/calibration are
  unchanged, and no activity source is physically deleted.
- `activity-confirmation list` is read-only and must not regroup, commit, migrate,
  or delete orphan rows. Schema 28 adds business-episode, three-level answer, and
  content-free settlement metadata while preserving the private evidence tables;
  contract v4 exposes only content-free evidence availability and keeps
  `raw_access=false`. Non-interview subjects retain their optional
  `object` and `object_identified` fields.
- v0.3.20 keeps schema 28, the legacy activity-confirmation list contract v4,
  and report-context contract v2. Its cards contract v5 adds bounded cursor
  pages, a content-free revision, organized presentation fields, and explicit
  `pending_confirmation` / `information_insufficient` sections without
  returning `cleaned_contents` or full diagnostics on the first screen. The
  preceding compatibility hotfix lets the App accept a notification-eligible
  `explicit_claim` without an episode ID while keeping `business_episode` strict,
  shares verified-count/content-readability state across its three pending UI
  surfaces, and separates unavailable from delete-pending evidence copy without
  expanding raw access.
- v0.3.21 preserves those schema/contract boundaries and changes only the
  native shell's top-level menu-bar label. The per-second Timeline is removed
  from that label so startup can reach the main window; local countdowns inside
  open menu and window content remain.
- v0.3.22 keeps schema 28 and the v4/v5/v2 contracts. Confirmation policy v7
  routes fragmentary outlines, menu strings, and category-only subjects to
  `information_insufficient`; those cards have no answer actions. Cards v5
  continues to reject `cleaned_contents`, raw OCR, paths, and internal source
  fields while preserving the 12-card cursor and cheap revision paths.
- High-value episode evidence is promoted while its raw frame is still source-bound.
  Ordinary business-context samples retain cleaned structured signals only. Normal
  today+yesterday retention holds unresolved sources; explicit deletion, privacy
  reclassification, and terminal answers still win and release evidence.
- `activity-confirmation configure --mode compact|standard|max_recall` controls
  question volume, `manual-add` records a user-confirmed missed fact through bounded
  JSON stdin, and `settle --date` materializes the day and records an idempotent
  content-free revision. Group answers accept workstream/outcome only through
  bounded JSON stdin; none of these commands creates a fact from model inference.
- `answer-group` checks `member_revision` and updates all current members in one
  transaction. Stale membership returns a refresh response without a partial
  answer. A grouped answer counts at most once for each calibration feature,
  and a confirmed group produces one canonical fact across every consumer.
- Grouping never crosses dates or merges conflicting companies/interview rounds.
  App-attribution drift does not split the same concrete subject. Contract-v4
  business episodes use only strong occurrence/session anchors and at most a
  five-minute adjacent gap; ordinary business frames may enrich one nearby
  episode but cannot extend or bridge it. Legacy repeatable explicit-claim
  grouping remains read-compatible inside its bounded window. Incremental
  writers regroup every affected local day. Completion OCR prefix/containment
  variants merge only within 15 minutes when App attribution is compatible and
  the shorter normalized subject has at least 8 characters. Unknown subjects
  may form anonymous high-value questions; they are never auto-confirmed or
  merged across distinct sessions.
- Calibration rules are reversible and content-free. They use App/event/region
  and reason features plus counters, never raw screenshots, OCR, paths, chat
  bodies, company names, or project keywords.
- Only `pending` and `deferred` items may reference a source image in the private
  `confirmation_evidence.noindex` directory. Evidence files are local, mode
  `0600`, excluded from indexing and backup, and have no independent TTL while
  the question remains unresolved. Terminal answers, `ignored`, system rejection,
  source deletion, privacy reclassification, retention, and range deletion
  release the reference. A failed unlink remains `delete_pending` for idempotent
  reconciliation; paths, hashes,
  file names, and image bytes never enter public payloads.
- `clarify` is local and non-mutating with respect to occurrence state,
  calibration, and downstream facts. Under the evidence lock it reopens each
  available source image, reruns OCR and the current Privacy Firewall, and then
  combines only the newly cleaned result with a bounded +/-15-minute safe context
  and the latest sanitized note of at most 360 characters. The note is stored
  only for retries while the question is unresolved, is erased at terminal state,
  and is never occurrence evidence or an answer. `clarify` does not
  call Hermes. Pre-v0.3.16 cards are not backfilled with fabricated image evidence;
  they return `evidence_unavailable` when no retained source is available.
- `ignored` is a terminal “do not ask again” answer. It is not a statement that
  the event did not occur and not a `misrecognized` calibration signal; it stores
  no note, increments no calibration counter, and releases no downstream fact.
- A sanitized clarification note may guide context classification, including a
  strongly corroborated game UI, demo, or quoted-reference interpretation. It
  never supplies occurrence evidence. Policy-v5 context filtering retains
  provenance with `answer=NULL`, creates no calibration, and releases no fact.

## Current Capabilities

- SQLite local database and FTS search.
- Local daily review from the confirmed subset of Privacy-Firewall `clear`
  activity.
- Privacy Firewall for high-risk apps, domains, region-aware OCR keywords,
  secret-looking values, payment flows, and developer credential pages.
- Adult content protection for adult sites, Steam adult game contexts, and
  visible local adult media. These become content-free protected events with
  app/domain/title/file context suppressed. Domain coverage uses the local
  `adult_policy` snapshot generated from public NSFW blocklists as a hashed
  Bloom filter, plus compact local semantic terms for OCR/AX matching.
- `partial_redacted` activity keeps safe summaries when sensitive text only
  appears in peripheral regions such as tabs or sidebars.
- Cleaned timeline exports write the post-firewall full cleaned content for each
  activity record, not the short UI/search summary. HA Source Packages expose the
  same text as `cleaned_content`; this is post-firewall text, not raw OCR.
- Interview occurrence extraction requires explicit occurred/completed semantics.
  A generic interview mention, past experience, future scheduling, preparation,
  or discussion does not create an "interview happened" confirmation.
- Learning summary exports classify the same cleaned timeline into useful,
  low-signal, protected, duplicate, and gap events for local review and HA input.
- `ai-handoff status|package|export` exposes a narrower read-only package for
  external AI handoff. It shows normal clear safe summaries, project/signal
  groupings, and count-only privacy/gap/redaction diagnostics; it does not
  expose raw screenshots, raw OCR, SQLite raw tables, local paths, tokens, HA
  package bodies, or memory identifiers.
- Quality reports summarize cleaning status, partial/blocked/gap/duplicate
  counts, DocumentSignal matches, short/near-cap content,
  memory candidates, and local feedback metadata without reading raw images,
  raw OCR, or uncleaned text.
- Unlock-to-lock activity sessions summarize the local safe timeline into
  primary focus share, multi-display visible exposure, Top Apps, trusted browser
  domains, privacy-protected counts, and gap counts. macOS Screen Time details
  are not used as the data source.
- v0.3.6 improves activity-session attribution quality: secondary displays use
  safe window/display matching when available, context-only regions no longer
  compete as primary focus, media chrome is demoted for primary scoring but kept
  in visible exposure, and session/status payloads include quality explanations
  for Unknown and low-confidence shares.
- v0.3.7 finishes the P0.1 attribution pass: attribution health now derives
  main/secondary/unknown display classes from safe display attribution reasons
  instead of raw `display_id`, activity-session rollups carry reason counts, and
  secondary browser domains require a single safe matching browser signal.
- Local `quality_feedback` stores only target ids, issue type, expected status,
  target time metadata, and redacted notes. Time-range deletion cascades to
  matching feedback so old feedback does not point at deleted activity.
- Duplicate cleaning removes repeated safe content after the Privacy Firewall:
  exact duplicates become content-free duplicate events, while partial duplicates
  keep only newly changed cleaned lines.
- `review_required` input becomes a content-free GapEvent.
- Only `clear` and locally cleaned `partial_redacted` ExtractedActivity enter
  timeline, search, daily review, and HA Source Packages.
- HA Source Package, HASourceRecord, and SourceRevocationEvent support.
- Optional hourly HA analysis can generate the previous complete hour as an
  active clear-only package and obtain analysis/advice from a dedicated,
  tool-less Hermes profile. Retry/backoff/dead-letter handling must not block
  capture, and the result is not evidence of a memory write.
- Hermes handoff defaults to a memory-candidates-only safe wrapper so the agent
  receives compact candidate facts and diagnostics instead of full package text.
- Memory candidates are quality-gated for review, but v0.3.22 has no active write
  path. Eligibility alone is neither current authorization nor proof of a write;
  `review_only`, `ephemeral`, and workflow-only candidates remain non-writable.
  The stricter self-ownership, complete-provenance, manual-confirmation, receipt,
  and revocation gates remain prerequisites for any future re-enable.
- Feishu write-completion notifications require verified controlled-writer
  receipts and contain safe summaries. Because the v0.3.22 writer is disabled,
  no new PAM write-completion notification is expected; model claims without
  receipts remain explicitly labeled analysis-only.

### Historical Milestones

The following version bullets record earlier behavior. Editable-ledger,
Hermes-direct-write, or model-self-report semantics in them are superseded by
the Current Contract Overlay above.

- v0.1.7 also tightens single-evidence memory writes: a trusted work app alone
  is not enough for weak candidates; they need a real DocumentSignal or an
  explicit durable signal to enter the Hermes write input.
- v0.1.8 adds subject-ownership gating for Hermes memory writes. Third-party
  resumes, candidate profiles, external CV pages, and ambiguous person-profile
  material stay out of the write input; at most they can inform same-day
  workflow summaries.
- v0.1.9 keeps the old JSON fields for compatibility but makes the reporting
  vocabulary explicit: full candidate pool, displayed candidates, eligible
  pool, actual write input, single-evidence writable candidates, subject-risk
  candidates, and actual Hermes writes are separate counts.
- v0.1.9 tightens single-evidence writes again. A single item needs a strong
  anchor plus durable long-term value, and resume/interview material needs an
  explicit self/user-owned subject signal before it can enter the write input.
- v0.1.10 keeps HA latest status on real event time instead of metadata update
  time, makes export-cache auto cleanup explicitly disabled, and keeps runtime
  permission checks limited to known sensitive `core/data` paths.
- v0.1.11 adds local foreground-document understanding for PDF/DOCX/Markdown/text
  resume and JD workflows. Self-owned resume documents can produce safe summary
  candidates, while target-role JD, third-party resume, and ambiguous document
  material stays out of Hermes long-term memory writes.
- v0.1.12 adds the Obsidian-editable PAM long-term memory ledger. Hermes treats
  unclear subject ownership as a valid zero-write result, and confirmed ledger
  actions can delete or rewrite only PAM-sourced mem0 entries after id/hash
  checks and local backup.
- v0.1.13 makes that Obsidian ledger a pure Chinese numbered memory list. The
  system keeps detailed ids, hashes, metadata, and HA history in a separate
  Application Support detail file; deleting a list item deletes the matching
  PAM mem0 memory, and editing one line rewrites it after id/hash checks.
- v0.1.14 adds personal clinical medical protection. Personal checkup reports,
  medical records, lab/check reports, symptoms, diagnoses, medication, and
  medical consultation content become content-free protected events. Oura /
  OuraRing self-quant summaries and pet/veterinary contexts are explicitly
  excluded.
- v0.1.15 treats older HA automation dead letters as historical once a later
  hourly job has completed successfully. Hygiene strict health still blocks on
  current pending/failed jobs and unresolved newer dead letters.
- v0.1.16 adds a native Today Health Center to the menu bar app overview. It
  summarizes recording, privacy, coverage, HA automation, and data hygiene
  with action buttons, without reading raw screenshots, OCR source text, paths,
  or package contents.
- v0.1.17 adds a native read-only Memory tab. It combines `memory-ledger status`
  and `memory-audit --format json --limit 50`, shows only safe summaries and
  counts, and does not expose sync, delete, rewrite, or approval actions.
- v0.1.18 upgrades the Memory tab into a ledger-driven approval console. It
  previews and applies PAM memory delete/rewrite actions and safe HA candidate
  writes through core plans instead of letting Swift touch mem0 directly.
- v0.1.19 adds the read-only AI handoff package. It is designed for Codex,
  Hermes, or ChatGPT to read safe summaries without raw content or write
  capability.
- v0.1.20 renders that handoff package inside the native Review tab as an
  intelligent project review: overview, trust warnings, project groupings,
  suspected decisions, follow-ups, blockers, and safe samples.
- v0.1.21 adds a read-only Privacy Policy preview. It explains current Privacy
  Firewall rule categories and previews app/domain handling without writing
  firewall rules, reading raw sources, or changing retention.
- v0.1.22 adds a safe local diagnostics package. It summarizes recording,
  AI handoff, privacy policy, hygiene, HA automation, quality advice without exposing raw sources, local paths, tokens, or package bodies.
- v0.2.0 adds a manual loopback read-only AI connector. It exposes `/status`,
  `/package`, and `/diagnostics` over GET-only localhost HTTP and rejects
  write methods, non-loopback hosts, raw access, memory writes, and rule writes.
  The connector package is a lightweight clear-only handoff for responsive AI
  reads; full intelligent review remains in the native Review page and
  `ai-handoff package`.
- v0.2.1 keeps the native Review page reachable in compact windows and restores
  a stable menu-bar countdown mark without changing capture, connector, or
  privacy behavior.
- v0.2.2 moves the native Overview's common actions and today metrics to the
  top and makes busy/capturing states read as transient recording activity, not
  as a stopped service.
- v0.2.3 tightens long-term memory quality gates: ordinary single-evidence
  candidates become review-only, repeated safe summaries merge before Hermes
  write input, and diagnostics treats transient SQLite busy/runtime-permission
  hygiene findings as repairable warnings instead of capture failures.
- v0.3.0 promotes the AI connector to a managed manual service on
  `127.0.0.1:8766`: `start` and `stop` use a private PID file and logs, while
  `status`, `/status`, `/package`, and `/diagnostics` keep the same read-only
  GET-only boundary. It also adds attribution-health counters and diagnostic
  action classes without exposing raw sources.
- v0.3.1 adds quick/full diagnostics modes, probe timing, Connector
  `/diagnostics?mode=quick`, attribution-health v2, active-vs-ledger gap
  semantics, privacy profile dry-run previews, and recent memory-governance
  trends while keeping raw access and write actions disabled.
- v0.3.3 makes `decision-queue list` return user-facing question fields,
  available actions, recommended action, queue bucket, automatic-handled counts,
  and duplicate merging for the native question-card UI. Write actions still
  require dry-run preview and confirmation, while context-only and never-write
  stay local.
- v0.3.4 clarifies Feishu "记忆系统" copy for PAM memory reports: hourly Hermes
  messages are labeled as write-event notifications, not Obsidian ledger growth,
  and historically distinguished the then-editable `current_mem0_entries` from
  historical HA write-summary clues.

### Current Runtime Interfaces

- `memory-audit` is a read-only mem0 health check for PAM/Hermes memories. It
  reports weak single evidence, media/file-listing noise, unanchored items,
  third-party subject risk, JD/company requirements stored as personal
  positioning, duplicates, and missing metadata without deleting or rewriting
  memories.
- `memory-ledger status` provides historical read-only inspection. The former
  refresh/sync mutation path is frozen and must return a read-only/blocked
  result. The canonical document is in the iCloud Obsidian default vault at
  `默认仓库/✦ AI 工作台/PAM 长期记忆台账（已停更）.md`.
- `memory-approval status|candidates|records` may expose safe read-only
  diagnostics. Candidate-write `preview` and `apply` are deliberately blocked
  in v0.3.22 with `controlled_mem0_writer_disabled`; they must not create plans,
  mem0 writes, or write-completion audit claims.
- `activity-confirmation list` returns contract v4 grouped questions plus the
  one-release v3/legacy item list through a read-only path. v4 adds five-minute
  business episodes, three-level answers, manual omission capture, volume modes,
  funnel diagnostics, and settlement revisions. `answer-group` performs revision-checked atomic
  answers; `clarify` reruns OCR and the Privacy Firewall on locally retained
  evidence before it uses the latest sanitized local note. The note persists
  only for unresolved retries and is erased at terminal state. Clarification leaves
  occurrence state, calibration, and downstream facts unchanged. Legacy cards
  without source evidence fail safely with `evidence_unavailable`; legacy
  single-item `answer` remains available during compatibility.
- Daily Hermes quality advice, when enabled for the live runtime, is
  analysis-only. Hermes can read
  only `quality-report` and `get-package --memory-candidates-only`, then stores
  redacted suggestions for Codex review; it does not read SQLite tables, raw
  vaults, screenshots, logs, OCR source text, or local documents.
- Time-range deletion with search cleanup, package revocation, session-derived
  cleanup, and all other time-locatable PAM data selected through the same
  provenance lifecycle used by retention.
- Default retention keeps only today and yesterday, but holds unresolved
  confirmation sources until a terminal answer or explicit privacy/deletion
  action. Older eligible clear activity and HA package data expires through the
  same deletion path. `hygiene-cleanup` also
  treats old activity-data snapshots outside the live runtime as permanent
  delete candidates when live DB/HA health is normal, while non-activity source
  or configuration backups are retained.
- `data-hygiene` / `backup-hygiene` now also report old cleaned/learning files
  in `core/data/exports` as activity-derived export caches. They are not source
  backups; outside today+yesterday they enter dry-run cleanup candidates and
  metadata-only audit, but daemon auto cleanup does not delete them. They are
  deleted only by explicit `hygiene-cleanup --apply`.
- `hygiene-cleanup` checks runtime data permissions: sensitive directories stay
  `0700`, ordinary files stay `0600`, and existing executable helpers under
  `core/data/bin` keep owner-executable `0700`. The scan is limited to known
  sensitive runtime directories and common root runtime file types.
- Development dashboard on `127.0.0.1` when explicitly launched.
- Dashboard destructive actions require a short-lived, single-use confirmation
  code.
- ScreenCaptureKit keyframe capture with OCR cleanup.
- Multi-display keyframe capture: every connected display is captured, OCR
  processed, and passed through the local Privacy Firewall.
- Structured browser signals for Dia/Chrome/Arc via Chromium extension +
  native host, with AppleScript fallback for URL/domain attribution. Stored
  signals keep only redacted URL, domain, privacy/form flags, and risk tags.
  URL cleanup is centralized so userinfo, passwords, path, and query strings do
  not enter storage; extension signals are short-lived per browser/window/tab
  slots instead of one global latest file.
- Accessibility-first text extraction through a local Swift helper. AX text can
  feed the Privacy Firewall before OCR, while secure text fields fail closed.
- Foreground-only DocumentSignal extraction for local documents. The core stores
  HMAC document refs, redacted filenames, file type, visible location hints, and
  redacted summaries, never full paths or full document text. Documents that
  cannot be content-matched stay metadata-only for health/coverage and are not
  exposed as HA document context.
- v0.3.11 disables recursive high-recall document candidates. A separate
  `document-changes` layer watches only user-authorized roots, compares keyed
  line/paragraph/slide/sheet fingerprints locally, and exports only redacted
  summaries plus coverage. Authorized absolute root paths exist only in the
  private `0600` local runtime registry needed by FSEvents; SQLite, logs, CLI,
  AI packages, and Feishu never receive absolute paths, full text, complete
  diffs, or internal hashes.
- Multi-display context attribution keeps browser, AX, and document context on
  the display most likely to contain the frontmost window; secondary displays
  still get OCR but do not inherit the frontmost browser/document context.
- Raw vault cleanup on startup and each daemon round.
- Retention cleanup for old activity and HA package cache. `cleanup-retention`
  is a dry-run by default; real cleanup requires `--apply` plus the explicit
  `DELETE_EXPIRED_LOCAL_ACTIVITY` confirmation string, and `--days 0` is
  rejected.
- Local-only endpoint guard for learning text classification. `localhost`,
  `127.0.0.1`, and `::1` are allowed; LAN and cloud endpoints are rejected before
  any image or text request is sent.
- Swift helper prebuild support under local runtime data.
- LaunchAgent helper using the default label `app.personalai.monitor.core`.
- Status output reports LaunchAgent health, privacy cleaning counts, coverage
  health, display coverage, duplicate cleaning counts, and split structured-signal counts for extension browser signals, AppleScript
  fallback signals, semantic documents, metadata-only documents, DocumentSignal
  zero-count reasons, quality health, privacy profile,
  and HA learnability/candidate diagnostics.
- `soak status|snapshot` provides a content-free seven-day stability scorecard;
  `soak baseline`, `record-win`, and `record-drill` add only resource or
  user-confirmed acceptance evidence, never activity content.

## Quick Start

```bash
python3 -m activity_mvp init
python3 -m activity_mvp ingest-sample
python3 -m activity_mvp timeline
python3 -m activity_mvp daily-review
python3 -m activity_mvp document-changes status
python3 -m activity_mvp document-changes list \
  --from 2026-07-17T00:00:00+08:00 \
  --to 2026-07-18T00:00:00+08:00 \
  --limit 200
printf '%s\n' '{"query":"activity"}' | \
  python3 -m activity_mvp search --query-stdin
python3 -m activity_mvp capture-keyframes
python3 -m activity_mvp soak status --format markdown
```

## Common Commands

```bash
python3 -m activity_mvp package --from 2026-05-22T09:00:00+08:00 --to 2026-05-22T09:32:00+08:00
python3 -m activity_mvp list-packages
python3 -m activity_mvp revoke-package <package_id> --reason manual_sensitive_review
python3 -m activity_mvp delete-range --from 2026-05-22T09:00:00+08:00 --to 2026-05-22T09:10:00+08:00
python3 -m activity_mvp status
python3 -m activity_mvp configure-screen-capture --enabled off
python3 -m activity_mvp pause --minutes 30
python3 -m activity_mvp pause --manual
python3 -m activity_mvp resume
python3 -m activity_mvp quality-report --format markdown --out ~/Desktop/pam-quality.md
python3 -m activity_mvp ai-handoff status
python3 -m activity_mvp ai-handoff package --range today --format markdown
python3 -m activity_mvp ai-handoff export --range today --format markdown --out ~/Desktop/pam-ai-handoff.md
python3 -m activity_mvp quality-feedback list --from 2026-06-09T00:00:00+08:00 --to 2026-06-10T00:00:00+08:00
python3 -m activity_mvp configure-privacy-profile --profile personal_high_precision
python3 -m activity_mvp configure-quality-advice --auto on
python3 -m activity_mvp quality-advice status
python3 -m activity_mvp run-daemon --once
python3 -m activity_mvp run-daemon --dev-dashboard
python3 -m activity_mvp build-helpers
python3 -m activity_mvp install-browser-hosts
python3 -m activity_mvp install-launch-agent
python3 -m activity_mvp install-launch-agent
python3 -m activity_mvp uninstall-launch-agent
python3 -m activity_mvp cleanup-retention
python3 -m activity_mvp cleanup-retention --apply --confirm-retention-delete DELETE_EXPIRED_LOCAL_ACTIVITY
python3 -m activity_mvp data-hygiene
python3 -m activity_mvp backup-hygiene
python3 -m activity_mvp configure-ha-automation --auto on
python3 -m activity_mvp ha-automation-status
python3 -m activity_mvp validate-ha-automation
python3 -m activity_mvp run-ha-automation-once
python3 -m activity_mvp reclassify --dry-run
python3 -m activity_mvp reclassify
python3 tools/update_adult_policy_snapshot.py --allow-insecure-tls
```

`PAM_PRIVACY_PROFILE=personal_high_precision` is the local default.
`strict` remains available as a manual downgrade. The personal profile reduces
soft-privacy false positives in technical/AI-tool/code/Obsidian contexts while
keeping hard privacy blocks for password fields, payment pages, verification
codes, real secret assignments, private keys, bank-card/payment material, adult
content, private browsing, and password managers.

## Browser Extension Setup

The Chromium extension manifest contains a fixed public key, so its unpacked
extension ID is stable:

```text
neinjkcikcfdbafjffgadppbpafpjnbm
```

`install-browser-hosts` derives this ID automatically and writes it into the
Dia, Chrome, and Arc native messaging host manifests. Browser-side extension
loading still requires the browser's own developer-extension approval; after the
extension is loaded, native messages are accepted without copying the ID by hand.
See `../docs/BROWSER_EXTENSION_PRIVACY.md` for the permission-by-permission
privacy boundary.


## Development Dashboard

The HTTP dashboard is for development only:

```bash
python3 -m activity_mvp serve
```

Daemon mode does not start the dashboard unless `--dev-dashboard` is passed.
The dashboard refuses non-loopback `--host` values, checks the Host header, and
requires Origin / Referer on API calls. Dashboard deletion first obtains a
short-lived one-time confirmation code and then submits it with the delete request.

## Data Hygiene

```bash
python3 -m activity_mvp data-hygiene
python3 -m activity_mvp backup-hygiene
```

These are read-only reports of likely runtime, old-copy, backup locations, and
cleanup candidates. They do not remove anything. The today-and-yesterday runtime
retention window does not govern old deployment snapshots or manually copied
backups; deleting those copies requires a separate explicit action.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Runtime Data

Runtime files live under `data/` and are intentionally ignored by Git. Do not
commit SQLite databases, logs, raw vault files, debug state, HA package cache,
screenshots, OCR source text, or real activity records.
