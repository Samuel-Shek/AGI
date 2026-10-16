# Release Checklist

Personal AI Monitor is a personal local build. This checklist is the gate for a
`0.3.22` source/build/install/live-runtime refresh. Passing source checks alone
does not prove the installed app or running service was upgraded.

## Context Console Phase 0 + Phase 1 Candidate Gate

This source-only candidate uses additive schema 30. Schema 29 remains reserved
for the v0.4 shadow line. Before any later rollout, read
`docs/CONTEXT_CONSOLE_V1.md` and verify all of the following:

- `confirmed_activity_view.activities` and `trusted_activities` contain only
  explicit user confirmations; owner-local cleaned observations are exposed
  only under `owner_activities` and remain explicitly `unverified`;
- screen/OCR, App/window, sidebar/list/label, submit-area, and realtime UI
  heuristics do not materialize `state=confirmed` or enter HA, report-context
  trusted body, ContextBundle verified contexts, or MemoryCandidate;
- ContextReceipt/ControlledEvidence principal, key, registry, label, project,
  provenance, and source-event tombstone checks are repeated inside the same
  immediate write transaction as materialization;
- Bundle and Memory reads share the authorization lock used exclusively by
  delete/revoke; deterministic hashes exclude generation time and row order;
  public context refs contain no internal database identity, and prepared
  delivery exposes a durable frozen payload whose recomputed hash matches the
  manifest/delivery hash and is fetchable by public Bundle ref;
- every target explicitly declares local/external scope, export policy and
  allowed privacy classes; undeclared targets fail closed and
  `restricted_local` cannot be prepared for an external target;
- ACKs bind exact project/target/scope/attempt/Bundle/policy/role, local-only
  ACKs reject empty or external scopes, and replay rechecks the current terminal
  attempt plus all labels, policy, manifest and source dependencies;
- failed delivery creates a new monotonic attempt and an old ACK cannot cross
  attempts; delivered history is retained while local withdrawal/restriction
  receipts record later revocation;
- upgrading a v2 candidate leaves implicit target policy/unscoped principals
  fail closed, permits only explicit same-value one-way rebinding, terminalizes
  unbound prepared/consumed attempts locally, and preserves delivered history;
- operational AI/file/Git evidence does not become long-term memory merely
  because it is independently verified; MemoryCandidate creation, replay,
  active count and outputs share one owner/subject/scope/provenance/expiry/
  revocation predicate and always report `direct_write=false`,
  `writer_enabled=false`, `memory_write_actions=false`;
- `ha_auto_package_enabled`, `writer_enabled`, and
  `source_revocation_cascade_ready` are forced false on schema 28/29/30 and on
  repeated startup;
- every soak gate declares its component dependencies; effective start is the
  latest required component baseline, and a missing component remains
  `unbaselined`/`unknown` without global resource-baseline fallback;
- copy/UI changes reset no component; component reset generations increment
  atomically and never clear product experiments, attempts, or wins;
- local retention uses the persisted IANA timezone: unverified observations are
  2 local natural days, verified safe context 90, and product wins 30; short
  activity cleanup does not delete an unexpired product win; missing/invalid
  current or experiment-snapshot zones fail status closed without UTC fallback;
- product experiment day-14 readiness requires at least 10 complete attempts,
  useful rate `>=80%`, and median resume time `<30s`; a complete result freezes
  its terminal cutoff and rejects later attempts, only insufficient complete
  attempts extend through the inclusive day 21 (with automatic insufficiency
  beginning on day 22 unless the owner explicitly finalizes on day 21), exact
  replays require exact float equality, and
  missing usefulness/time remains null and pending/incomplete;
- database busy/missing/unreadable status is `unknown` with current
  `observed_at`, null counts/service, disabled actions, and no retained trusted
  Swift snapshot;
- the document-change adapter emits only opaque operational context and no
  path, file body, diff, or automatic Memory eligibility;
- schema-28 and real-shape shadow-29 synthetic migrations create content-bound
  private backups, downgrade unsafe screen-confirmed rows, and do not reinterpret
  the shadow task ledger.

Run the focused gates before the general source gate:

```bash
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_context_console)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_core)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_document_changes.DocumentChangeTests.test_debounced_saves_preserve_each_observed_content_version)
```

A source candidate is not an installed/live acceptance. Do not enable the new
feature, HA auto-packaging, or a Memory writer until a separate governed rollout
and Memory Gateway review is approved.

## Schema-30 Context Console Source-Candidate Gate

This gate applies only to the Phase 0 + Phase 1 source candidate described in
`docs/CONTEXT_CONSOLE_V1.md`. It does not authorize installation, deployment,
live database migration, HA/Feishu delivery, mem0 writes, or a real product
experiment.

Before accepting a patch or source package:

```bash
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_context_console)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_core)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_document_changes.DocumentChangeTests.test_debounced_saves_preserve_each_observed_content_version)
python3 tools/release_privacy_scan.py
git diff --check
```

Verify all of the following before considering the source candidate complete:

- owner-facing local observations and trusted export are separate versioned
  projections; unverified screen/OCR may remain locally visible after privacy
  filtering but never enters trusted HA, `report-context`, Bundle or Memory
  bodies;
- old schema-28 `evidence_confirmed` and non-manual `confirmed` rows without an
  explicit user answer are downgraded and their settlement/cache counters are
  invalidated;
- receipt, controlled evidence, Bundle/Memory reads, revoke and delete share the
  authorization lease and revalidate principal/key/registry/provenance inside
  the write transaction;
- source-event tombstones survive idempotency-key changes, while idempotency is
  scoped by contract/delivery and principal;
- Bundle hashes are stable across time and insertion order; public payloads
  contain no internal IDs/raw fields, prepared payloads are exactly frozen and
  fetchable by Bundle ref, and external target policy rejects `restricted_local`;
- Delivery ACKs bind exact project/target/scope/attempt/Bundle/policy/role and
  replay rechecks current liveness; failed history is retained and a retry
  creates a new monotonically numbered attempt that old ACKs cannot cross;
- all Unicode `Cc`/`Cf`, prohibited body categories, cross-object/time/subject
  evidence and non-independent dual evidence fail closed;
- operational AI/file/Git evidence cannot automatically create durable personal
  memory; creation/count/output use one eligibility predicate and
  `direct_write=false`, `writer_enabled=false`, `memory_write_actions=false`,
  `source_revocation_cascade_ready=false` and `ha_auto_package_enabled=false`;
- expiry, source deletion and label/principal revoke use the complete dependency
  closure through FTS, Bundle, Memory and Delivery; delivered external history
  remains delivered while a content-free local withdrawal record is added;
- product experiment denominators/ratings/times remain separate from component
  stability baselines; not-useful events stay in the denominator, 80% is
  inclusive, 30 seconds is strict, and missing metrics are null/pending rather
  than fabricated zero or eligibility;
- component report and CLI status expose identical generation/effective-window
  data; missing component baselines remain unknown even when a legacy resource
  comparison baseline exists;
- busy/unreadable database status is `unknown` with counts/actions unavailable.
  Swift success -> busy clears trusted/verified/Bundle snapshots and recovery
  requires a fresh successful read;
- the file/document adapter commits its operational Context graph in the same
  transaction without path/body data and with memory eligibility disabled;
- schema 28 and the real four-table v0.4 shadow schema 29 migrate additively to
  schema 30. Migration backup reuse is bound to current database identity and
  content digest, not only filename, permissions and schema version.

For clean-apply delivery evidence, apply `implementation.patch` to a fresh
export of commit `85706ef13da39fbd50b229b1435132cc34a51bb1`, run
`git apply --check`, `git diff --check`, the focused Python tests and privacy
scan, then compare every file under `modified-source/` byte-for-byte. Verify the
ZIP CRC and every entry in `SHA256SUMS`.

The macOS Swift interaction and release-build commands below remain mandatory
for release acceptance. Linux `swift run PAMInteractionModelChecks` is only a
source-level portability check and must not be labeled a macOS build or
installed/live validation.

## Phase 2 Context Console Source Gate

Phase 2 is a source-only candidate until the separate Phase 3 installation and
live-runtime gate is explicitly authorized. Before accepting a Phase 2 source
archive:

- rebuild it from the Phase 0/1 payload on commit
  `85706ef13da39fbd50b229b1435132cc34a51bb1` in a separate worktree;
- run Python test discovery without a hard-coded count;
- run every `test_phase2_*` Core case, including the 10k/100k bounded-search
  and confirmation-scan cases;
- generate one `ContextSnapshotV1` with the real Python CLI and a temporary
  empty database, then decode that exact JSON with
  `PAM_PHASE2_SNAPSHOT_FIXTURE=... swift run PAMInteractionModelChecks`;
- run `swift test`, the executable InteractionModel checks, a release build and
  `./scripts/build-macos-app.sh` with explicit dirty-development provenance;
- treat a Command Line Tools run that only builds/links XCTest as compile
  evidence, not as executed XCTest cases;
- confirm schema 31 adds only `context_activity_confirmation_links` and
  `context_product_feedback` as persistent domain tables;
- confirm Phase 2 public stdout and Swift public models reject `reason_code`,
  internal Context refs, database IDs, raw receipts, paths, URLs, raw JSON,
  secrets and raw chat/OCR/screenshot content;
- confirm `busy`, `unknown`, `unavailable` and `blocked` clear all current
  trusted surfaces and capabilities, and only `capturing`, `ready` or `running`
  can resolve to the normal menu state;
- compare dependency manifests and lock files byte-for-byte with the Phase 0/1
  source;
- run `git diff --check`, privacy/credential scans, ZIP CRC, path traversal,
  manifest and SHA-256 verification on the final source archive.

The Phase 2 source gate never authorizes copying to `/Applications`, launching
the candidate against live Core, opening a real PAM database, changing a
LaunchAgent, sending Hermes/Feishu traffic, committing, pushing, creating a PR
or deploying. Those operations require a separately authorized Phase 3 gate.

## Source And Local Build Gate

Run these checks before building or publishing source changes:

```bash
python3 tools/release_privacy_scan.py
(cd core && python3 -m unittest discover -s tests)
(cd core && python3 -m activity_mvp document-changes status)
env CLANG_MODULE_CACHE_PATH=/private/tmp/pam-release-clang-cache \
  swift run --package-path apps/PersonalAIMonitorMenuBar --disable-sandbox \
  --sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
  --cache-path /private/tmp/pam-release-swift-cache \
  --config-path /private/tmp/pam-release-swift-config \
  --security-path /private/tmp/pam-release-swift-security \
  --scratch-path /private/tmp/pam-release-swift-build \
  --manifest-cache none PAMInteractionModelChecks
env CLANG_MODULE_CACHE_PATH=/private/tmp/pam-release-clang-cache \
  swift build --package-path apps/PersonalAIMonitorMenuBar --disable-sandbox \
  --sdk /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
  --cache-path /private/tmp/pam-release-swift-cache \
  --config-path /private/tmp/pam-release-swift-config \
  --security-path /private/tmp/pam-release-swift-security \
  --scratch-path /private/tmp/pam-release-swift-build \
  --manifest-cache none -c release --product PersonalAIMonitorMenuBar
(cd core && python3 -m activity_mvp cleanup-retention)
(cd core && python3 -m activity_mvp cleanup-retention --days 0)  # must fail
git diff --check
git status --porcelain=v1 --untracked-files=normal  # must print nothing
./scripts/build-macos-app.sh
git ls-files --cached --others --exclude-standard
```

The build script must refuse a dirty tree, including untracked files. Do not
use `--allow-dirty-development` for a release or installed-runtime refresh; that
escape hatch is only for an explicitly labeled local diagnostic build.
For v0.3.22, also inspect the candidate diff and commit ancestry: any mixed
v0.4/schema-29 work, unrelated feature change, or artifact produced from such a
dirty tree blocks packaging even if the version files themselves say `0.3.22`.

On the current packaging host, plain `swift run` / `swift build` selects the
macOS 26.5 SDK whose Swift interfaces do not match the selected compiler. The
fixed MacOSX15.4 SDK and private temporary cache/scratch paths are selected
inside the clean-environment public wrapper, not by caller overrides. A command
run with the mismatched default SDK is not a passing Swift gate. The packaging
script forwards only its internally authenticated SDK and cache/scratch
boundaries to both the release product build and icon generator. The generated ICNS must also
round-trip through `iconutil -c iconset` with the required 16/32/128/256/512
representations before packaging continues.

The candidate file list must not contain runtime data, including:

- `core/data/`
- SQLite databases or WAL/SHM files
- logs, pid files, lock files, or debug tokens
- screenshots, raw vault files, temporary keyframes, confirmation-evidence
  files, or OCR source text
- HA Source Package cache files
- real activity records or private browser/native-host payloads

Before replacing an installed local runtime, verify:

- App "restore recording" / LaunchAgent reinstall preserves an existing
  `qwen3-vl:4b` VLM opt-in via `install-launch-agent --preserve-vlm`
- the menu bar "quit" path stops recording before exiting when the LaunchAgent
  is loaded
- HA Source Package UI copy says "HA 安全包" and the package exposes
  `raw_access=false`, `quarantine_status=clear_only`, `learning_digest`, and
  `learnability_diagnostics`
- `data-hygiene` and `backup-hygiene` remain read-only and only emit risk groups
  plus recommended actions
- capture requires explicit `unlocked` and `on_console=true`; locked,
  off-console, missing/unknown, and session-helper-error states fail closed
- document tracking is limited to explicit authorized roots; initial baseline
  emits no change events; hidden/build/dependency/temp paths and symlink escapes
  are ignored; public JSON contains no absolute path, source text, complete diff,
  or internal HMAC; Settings/CLI roots use only random `root_ref` handles, and
  invalid, duplicate, missing or partial control refs fail closed
- the private root registry is an exact v2 envelope with `format_version=2`,
  `authorization_revision`, and `roots`; its revision matches SQLite and every
  internal `root_id` is bound to the resolved authorized path. Legacy lists are
  accepted only for one-time ensure migration without changing root IDs,
  baselines, or checkpoints
- missing/unsafe/mismatched key, registry, authorization lock, revision, or path
  binding fails closed across status/list/daily-review/handoff/Connector with
  `totals=0`, `items=[]`, and an explicit authorization/control limitation;
  ordinary `--add-root` never silently repairs it. Only a user-confirmed
  `--reauthorize-root` may revoke unverifiable roots/events/baselines and grant
  the selected replacement root
- public reads and no-op/label-only configure hold the shared cross-process
  authorization lease; root configure, ensure, retention, and delete-range hold
  it exclusively while coordinating SQLite and registry replacement. Export-off
  waits for in-flight export readers and blocks all later document exports; a
  concurrent remove cannot be resurrected by an older sidecar snapshot
- FSEvents create/edit/atomic replace/rename/delete, restart replay and dropped/
  wrapped/root-changed reconciliation pass in a private temporary root; any gap
  remains `partial` until reconciliation completes
- `daily-review`, `ai-handoff package`, Connector `/package`, Today and Review
  agree on document-change totals and all require `raw_access=false`
- `activity-confirmation list` remains available while the memory writer is
  disabled and returns contract v4 `question_groups`,
  `pending_question_count`, `pending_source_count`, subject, trigger sentence,
  source count, `member_revision`, `question_kind`, `candidate_origin`,
  `priority`, `episode_id`, safe signal types, suggested/user workstream and
  outcome, and content-free evidence availability;
  legacy `items/pending_count` remains for one compatibility release, and the
  App falls back safely when paired with an older Core
- business episodes never cross dates and combine only continuously adjacent
  signals whose next-start/previous-end gap is no more than five minutes;
  conflicting explicit subjects or separate sessions remain distinct. Missing
  subject, context-only, and Unknown App high-value signals stay anonymous
  pending questions instead of being silently filtered
- preparation-only JD, question-bank, old-title, sidebar, and historical-chat
  fixtures never become an occurred-interview claim; standard mode stratifies
  ordinary business context across the day/app/priority to at most 12 items
- coarse calibration may rank and deduplicate, but a prior generic negative
  cannot reject a new high-value company, date, episode, or session
- `answer-group` checks `member_revision` and updates every current member in
  one transaction; changed membership returns stale/refresh with no partial
  submission; one grouped answer increments each calibration feature at most
  once regardless of source count
- a confirmed group produces one canonical fact in Today, Review, confirmed-only
  search, export, AI handoff, `report-context`, Hermes, and Feishu; pending,
  deferred, rejected, and ignored groups remain fail closed
- `answer-group` captures occurrence, workstream, and
  `completed|progressing|blocked|preparation_only|unknown` outcome in one
  revision-checked action. `manual-add` accepts only sanitized local omission
  text and creates one unique canonical fact without fabricating source evidence
- deleting one source keeps a group while members remain; deleting the last
  source removes it; existing calibration can still be revoked
- `clarify` takes a shared hold on source-bound evidence, re-runs local Apple
  Vision OCR, and sends the result through Privacy Firewall before combining it
  with post-firewall group text, a bounded +/-15-minute safe context, and an
  latest sanitized note of at most 360 characters. The note is retained only
  for unresolved retries, erased at terminal state, and is never occurrence
  evidence or an answer.
  Enrichment, split, filtering, and insufficient-information results do not
  confirm occurrence, update calibration, or invoke Hermes. Repeating the same
  safe result still produces a visible completion response with a refreshed
  generation time
- source images are promoted from the short-lived `raw_vault` only for clear
  high-value occurrence confirmation sources, under
  `confirmation_evidence.noindex` with directory mode `0700`, file mode `0600`,
  no indexing, and backup exclusion. The evidence has no independent TTL and
  no public raw read path
- current cards expose only `evidence_status=available|partial|unavailable`,
  `evidence_count`, and `available_source_count`; old cards without schema-27
  references report `evidence_unavailable` and never guess another screenshot.
  Every public activity-confirmation/status surface remains `raw_access=false`
- occurred, not-occurred, ignored, system-filtered, source-deleted, range-deleted,
  retention-expired, and privacy-reclassified sources release evidence refs and
  attempt immediate deletion. Unlink failure remains `delete_pending` with a
  content-free error and is retried by reconciliation; no UI or release evidence
  may call it deleted before the file is gone
- ordinary today-and-yesterday retention holds unresolved confirmation sources
  and does not silently remove their cards; explicit user deletion, privacy
  reclassification, system filtering, and terminal answers still take priority
- “忽略该问题” maps to the content-free `ignored/user_ignored` terminal audit
  marker for that question only. It records no denial, user-supplied correction
  reason, note, calibration, generalized rule, or downstream fact; the current
  App does not present `misrecognized` as an action
- confirmation policy v5 rejects conditional or explicitly negated completion
  wording and strong game reward/UI false positives while retaining a concrete
  statement that real work actually completed; reclassification keeps
  `answer=NULL` and does not update user calibration
- generic interview wording, past experience, future scheduling, preparation,
  reference material, or discussion creates no "interview occurred" question;
  an explicit completed-interview sentence preserves its actual trigger and
  extracts company/round when present without inventing missing identity
- `report-context` is the only activity-fact input for the Hermes/Feishu daily
  collector. Independent contract v2 reports
  `confirmation_completeness=complete|pending|partial|unavailable`, unresolved
  counts/categories and a settlement revision, but no company/question/OCR/raw
  details. A declared v2 payload with missing or invalid completeness fails
  closed; unavailable quantities remain unknown and must never become zero or
  fall back to direct `extracted_activities.activity_summary` SQL
- the 23:50 settle operation is idempotent and revision-bound. Pending or stale
  settlement makes the 23:59 report provisional/incomplete; later confirmed
  facts produce at most one content-hashed revision after a ten-minute debounce,
  and unchanged content never sends again
- deletion and retention call the same provenance-graph lifecycle, including
  activity-session rollups and all other time-locatable derived records; dry-run
  and apply use the same selector/count contract; only a newly generated,
  metadata-only deletion/revocation record or content-free replacement gap may
  remain
- Hermes/model execution has no terminal, file, or memory tools and cannot
  write mem0; the controlled writer is the only future-permitted path, but in
  v0.3.17 `writer_enabled=false` and `source_revocation_cascade_ready=false`
- `memory-approval` candidate-write preview/apply fail closed with
  `controlled_mem0_writer_disabled`, create no plan/mem0 entry, and do not emit
  a write-completion audit claim
- the old Obsidian ledger is `frozen_legacy`/read-only at the canonical iCloud
  default-vault path; refresh/sync/delete/rewrite mutation paths are blocked
- Feishu labels completion only for a verified controlled-writer event; with no
  verified write, any model self-report is explicitly analysis/advice and never
  a completion report
- quick diagnostics identify themselves as `partial`; skipped probes cannot
  produce a full-green result
- `soak status` returns content-free gate evidence with `raw_access=false`;
  missing days, lock/unlock cycles, attribution samples, resource baseline, user
  recovery wins, or safety drills remain `pending` rather than being guessed
- the stability campaign restarts at the v0.3.18 deployment baseline because
  confirmation grouping, policy revalidation, and native interaction changed;
  pre-upgrade history cannot pass or fail the new gates, and continuous observation requires both
  168 elapsed hours and seven local calendar days
- a successful build/install/restart never marks the seven-day soak gate passed
- manual UI acceptance covers 760x560 and 980x680 windows, light/dark appearance,
  keyboard-only operation, focus order, expansion state, and VoiceOver labels;
  explanatory/calibration/privacy sections start collapsed and question actions
  remain visible without requiring them to be opened

## v0.3.22 Confirmation Quality Hotfix Gate

Cut v0.3.22 from the installed v0.3.21 clean source commit `7d9c9a4c83c2`
plus only the bounded confirmation presentation/policy fix, regression guards,
version sync, and release documentation. v0.4/schema-29/Codex-shadow work must
remain outside the release tree. This hotfix performs no database migration.

Before packaging and again against a WAL-consistent copy of the live database:

1. Verify fragmentary outlines, connective-prefix fragments, menu strings, and
   category-only subjects appear only under `information_insufficient`, with an
   empty question and no answer options/actions.
2. Verify a concrete object such as `国家市场调研报告` remains answerable while
   the category-only `国家市场` does not become a send question.
3. Verify same-episode OCR variants for one canonical object merge, while
   different objects or episodes remain separate and all source/evidence links
   remain intact.
4. Verify cards v5 returns at most 12 cards, recursively rejects
   `cleaned_contents` and raw/internal fields, and first load does not request
   full diagnostics.
5. Verify the App still uses `LazyVStack`, parent-owned `groupKey` draft state,
   the cheap revision path, on-demand diagnostics, and the non-periodic
   top-level `MenuBarExtra` label.

Before applying policy v7 to live data, stop the App/Core and create a verified
`0600` WAL-consistent schema-28 backup. Revalidation must default to rollback;
compare its dry-run mapping/count audit before explicit apply. Only
`answer IS NULL` rows may change. Historical answers, calibration, source and
evidence references, and `raw_access=false` must remain unchanged.

At the deployment boundary start `soak baseline --version 0.3.22` as
`in_progress`. Short performance/UI smoke cannot mark the seven-day campaign
passed.

## Legacy v0.3.21 Menu-Bar Launch Hotfix Gate

Cut v0.3.21 from the installed v0.3.20 clean source commit `5e607f4fde98`
plus only the bounded menu-bar launch fix, regression guard, version sync, and
release documentation. v0.4/schema-29/Codex-shadow work must remain outside the
release tree. This hotfix performs no database migration or policy rewrite.

Before packaging and again against the installed artifact:

1. Inspect the `MenuBarExtra` scene and run the regression test: its top-level
   label must not contain a periodic `TimelineView`; the mark must use the
   existing `statusSnapshot`. Local countdown Timelines inside already-open menu
   and window content may remain.
2. Quit and launch the App three times. Every launch must show the main Today
   window, the menu-bar entry must open on click, and reopening the App must
   bring the window forward instead of leaving a hidden or unresponsive process.
3. After launch stabilizes, record multiple CPU and RSS/footprint samples. The
   process must not sustain one full core or show the previous multi-gigabyte
   growth. If a sample is unexpectedly high, capture a bounded process sample
   and verify the main thread is not looping through
   `MenuBarExtraController.updateButton` / `NSStatusBarButton setImage`.
4. Verify source/build/install/live Core version/hash alignment, code signature,
   LaunchAgent health, schema 28, activity-confirmation list/cards contracts
   v4/v5, report-context contract v2, and public `raw_access=false`.

Create a verified v0.3.20 rollback snapshot before replacing the installed App
or live Core: stop the App and LaunchAgent, confirm no PAM process holds the
database or confirmation evidence, create the database copy with Python's
SQLite backup API, and verify the copy read-only with `integrity_check=ok` and
schema 28. Back up the installed App, live Core without `data/`, LaunchAgent
plist, document-watch secret/registry sidecars, and confirmation-evidence
directory into one `0700` rollback root; database and private sidecar files must
remain `0600`. At the deployment boundary start
`soak baseline --version 0.3.21` as `in_progress`; repeated-launch smoke cannot
mark the seven-day campaign passed.

## Legacy v0.3.20 Pending-Confirmation Hotfix Gate

Cut v0.3.20 from the clean v0.3.19 release commit plus only this bounded
hotfix; v0.4/schema-29/Codex-shadow work must remain outside the release tree.
Before any live write, stop PAM, create and verify a `0600` WAL-consistent
backup, and record unresolved/answered/calibration/evidence counts.

On a real database copy and again at the stopped live boundary:

1. Run `activity-confirmation revalidate` without `--apply`; confirm it rolls
   back and reports only content-free mapping/reason counts. Apply only after
   the dry-run and backup agree.
2. Verify unresolved Solo OCR variants form one normal Chinese card, while PAM
   self-UI is system-filtered and missing/ambiguous objects appear only under
   `information_insufficient`. Different real publish objects remain separate.
3. Verify cards contract v5 returns at most 12 cards per cursor page, omits
   `cleaned_contents` and full diagnostics, preserves all source/evidence refs,
   and leaves old answers/calibration unchanged. Legacy list remains contract
   v4; report-context remains contract v2; all public surfaces keep
   `raw_access=false`.
4. Measure end-to-end cards API warm P95 below 300 ms and cold P95 below 1 s on
   current live scale. Scroll the installed UI continuously for 30 seconds and
   verify P95 frame below 16.7 ms, no hitch over 100 ms, stable off-screen
   drafts/expansion, and no full-card recomputation from a global one-second
   timer.

Complete the source/build/install/live/signature/LaunchAgent checks below and
start `soak baseline --version 0.3.20` as `in_progress`. Neither short smoke nor
the separate v0.4 Codex shadow may mark the seven-day campaign passed.

## Legacy v0.3.19 No-Migration Hotfix Gate

v0.3.19 must be cut from the clean v0.3.18 release line plus the bounded hotfix.
Do not package it from a v0.4/schema-29 branch or a worktree containing mixed,
uncommitted feature work. Record the exact clean source commit before building.

This hotfix does not migrate the database. Before and after refresh, verify
`meta.schema_version=28` and `PRAGMA integrity_check=ok`; activity-confirmation
must remain contract v4 and report-context must remain contract v2. Any attempt
to create schema 29, rewrite schema-28 data, or advertise a new contract blocks
the release.

Run one cross-layer smoke from the same deterministic unresolved fixture:

1. Decode a notification-eligible `explicit_claim` without an `episode_id` and
   verify it remains readable; a `business_episode` without an ID must still
   fail closed, and neither fixture may auto-confirm occurrence.
2. Feed one v4 payload with a verified pending count but blocked card content
   through the App. The sidebar badge, Today prompt, and Confirmations header
   must preserve the same blocked count while cards and manual-add remain closed.
   A genuinely unverified count must use the warning state rather than zero.
3. Verify the unresolved item is absent from Today/Review fact output and the
   Hermes/Feishu activity body, while only content-free completeness/count data
   crosses the consumer boundary.
4. Verify evidence copy and attention items keep `unavailable_count` separate
   from `delete_pending_count`, do not cross-contaminate their messages, and do
   not increment the activity-confirmation pending count.

Complete the five-layer verification below: source snapshot, built/installed
App, live Core, startup/process ownership, and Hermes/Feishu consumer must all
align on v0.3.19 and the unchanged schema/contract values. At the real
deployment boundary, record `soak baseline --version 0.3.19` and keep the new
seven-day campaign `in_progress`; this hotfix, a successful test run, build,
install, restart, or short smoke must not mark it passed.

## Legacy v0.3.18 Schema-28 And High-Recall Gate

Before a v0.3.18 Core opens the schema-27 live database, stop PAM processes,
create a timestamped SQLite online backup plus matching document-watch sidecars,
and verify `PRAGMA integrity_check=ok`, `meta.schema_version=27`, and mode `0600`.
The first new-Core open must create or validate the fixed WAL-consistent
`<database>.schema27-backup` and refuse to overwrite an invalid backup.

After migration, verify schema 28, activity-confirmation contract v4,
report-context contract v2, `raw_access=false`, unchanged old terminal
answers/calibration, anonymous high-value fixture recall, preparation-only zero
fact release, three-level answers, manual omission deduplication, pending
retention holds, and explicit deletion priority. Verify 23:50 settlement,
23:59 provisional/incomplete output, settle-before-refresh correction, ten-minute
debounce, content hash, and unchanged-content idempotency. Start a new
`soak baseline --version 0.3.18`; the scorecard remains `in_progress`.

Rollback requires all PAM processes stopped and the verified schema-27 database
and matching sidecars restored before any older Core starts.

## Legacy v0.3.17 In-Place Confirmation Gate

When the live database is already schema 27, stop PAM processes and create a
timestamped SQLite online backup before the new Core opens it. Verify
`PRAGMA integrity_check=ok`, `meta.schema_version=27`, and file mode `0600`.
After install, verify confirmation policy setting `5`, contract v3,
`raw_access=false`, and unchanged resolved answers/calibrations. Existing
unresolved rows must be revalidated transactionally: negated/game false positives
become system-filtered with `answer=NULL`, and compatible duplicate sources share
one stable group. Start a new `soak baseline --version 0.3.17`; the scorecard
remains `in_progress`.

## Legacy v0.3.16 Schema-27 Upgrade And Rollback Gate

v0.3.16 upgrades SQLite from schema 26 to schema 27 and activity-confirmation
from contract v2 to v3. Complete these steps in order; do not run v0.3.16
`status`, `ensure_ready`, `activity-confirmation`, or document commands against
live data before the pre-upgrade backup:

1. Stop the v0.3.15 LaunchAgent and installed app, then confirm no PAM process
   still owns the live database or confirmation-evidence directory.
2. While the database reports schema 26, create a timestamped SQLite online
   backup with the Python `sqlite3.Connection.backup` API. Open the backup
   read-only, run `PRAGMA integrity_check`, and verify `meta.schema_version=26`.
   Copy both `data/document_watch_secret.key` and
   `data/document_watch_roots.json` beside the SQLite backup when they exist.
   During severe recovery, stop all PAM processes and restore the database, key,
   and registry from the same snapshot; keep both sidecars at mode `0600`. Do not
   back up the authorization lock: let the matching Core recreate it safely after
   recovery. Never include key or registry contents in logs, source control, CLI
   output, or report payloads.
3. Install the single clean v0.3.16 app artifact and matching live Core while
   preserving existing VLM, privacy-profile, document-tracking, and export
   choices. Only then let the new Core open the live database. Before migrating,
   it must atomically create and validate, or reuse after validation,
   `<database>.schema26-backup` via the SQLite backup API, keep it at `0600`, and
   refuse upgrade if that fixed backup already exists but is invalid.
4. Verify `meta.schema_version=27`, activity-confirmation contract v3,
   `raw_access=false`, and `PRAGMA integrity_check=ok`. Resolved historical
   answers and calibration records must remain unchanged. Pre-v0.3.16 cards
   remain answerable but report `evidence_unavailable`; migration must not attach
   unrelated current images or rewrite their historical answer state.
5. Verify the evidence root is `confirmation_evidence.noindex`, is neither a
   symlink nor indexed, has mode `0700`, contains only `0600` files, and has the
   backup-exclusion marker. Public status may expose only content-free health,
   including `delete_pending_count`; it must not expose filenames, paths, hashes,
   image bytes, or pre-firewall OCR.
6. Record a new soak resource baseline at the deployment boundary with
   `soak baseline --version 0.3.16` and comparable CPU, RSS, and disk values
   captured from the last pre-upgrade runtime sample. Verify
   `campaign_started_at` is the deployment time and the scorecard is
   `in_progress`; an empty or copied old baseline is not acceptable.
7. Run live canaries for: a new pending source with available evidence; a useful
   note plus real source re-OCR; Privacy Firewall redaction/blocking; an old card
   with `evidence_unavailable`; concurrent clarify and terminal answer; ignored
   with no user-supplied reason/note/calibration/downstream fact; every release
   path; and a
   forced unlink failure that remains `delete_pending` until reconciliation
   actually removes it. Recheck one confirmed group appears exactly once in every
   downstream consumer.

Rollback requires stopping all PAM processes and restoring the verified
pre-v0.3.16 schema-26 database with its matching document-watch sidecars. Remove
or quarantine any schema-27 confirmation-evidence directory only through the
validated rollback procedure; do not leave raw evidence orphaned or reuse it
with the old Core. Never start a schema-26-or-older Core on a schema-27 database.
Downgrade guards remain defense in depth, not a substitute for this sequence.

## Five-Layer Local Release Verification

All five layers must agree before calling a local refresh complete.

### 1. Source snapshot

- `git status --porcelain=v1 --untracked-files=normal` is empty.
- `git rev-parse HEAD` is the intended commit.
- Python core/version metadata, app marketing version, browser extension,
  and handoff/connector versions all report `0.3.22`; report-context separately
  reports contract version `2`.
- Privacy scan, full unit tests, Swift build/typecheck, and `git diff --check`
  pass from that exact clean commit.

### 2. Built and installed app

Inspect both `build/Personal AI Monitor.app/Contents/Info.plist` and
`/Applications/Personal AI Monitor.app/Contents/Info.plist`:

- `CFBundleShortVersionString=0.3.22`
- `PAMGitRevision` equals the first 12 characters of the source commit
- `PAMGitDirty=false`, dirty reason is clean, and tracked/untracked counts are 0
- `PAMSourceTreeHash` is present and identical in build and installed app
- `PAMCoreCodeHash` is present and identical in build and installed app
- code signature verification passes for the installed bundle

The installed app binary and Info.plist must come from the same built artifact;
copying only one file is not a valid refresh.

### 3. Live core

Run `python3 -m activity_mvp status` from
`~/Library/Application Support/Personal AI Monitor/core` and verify:

- `runtime_identity.path_kind=application_support`
- `runtime_identity.core_version=0.3.22`
- `runtime_identity.core_code_hash` equals the installed app's
  `PAMCoreCodeHash`
- the data/database roots resolve under the intended Application Support live
  runtime, not the archive source or a compatibility wrapper
- `session_transition_evidence` reports the current source/reason and last valid
  state without user id, title, URL, OCR, display identifier, or local path
- `soak_scorecard` starts or continues the real seven-day observation and does
  not claim future gates passed from synthetic tests
- live SQLite reports schema 28, `activity-confirmation cards` reports contract
  v5, legacy `activity-confirmation list` reports contract v4, and
  `pending_question_count`/`pending_source_count` match the grouped and
  underlying source records without exposing raw content
- the App's sidebar badge, Today prompt, and Confirmations header share the same
  verified pending count/content-readability state; blocked content never
  silently becomes a zero-count state
- `report-context` reports contract v2 and a valid
  `confirmation_completeness`; unavailable numbers are null/unknown, settlement
  revision matches the current day revision when complete, and no unresolved
  company/question/OCR/evidence detail appears
- `confirmation_evidence_health.raw_access=false`; `delete_pending_count=0` in
  the release baseline, and any nonzero unavailable/error count is explained by
  a content-free, reproducible canary rather than ignored
- `document_changes.coverage` reports the authorized-root baseline,
  last event, gaps and `export_enabled=false` shadow state; controls expose only
  `root_ref`, never `root_id`, paths, or HMAC, and no authorization-unverifiable
  limitation is present in a healthy deployment
- after export is disabled, export-facing list, AI handoff, and Connector
  packages contain zero document items even while local Review remains available
- raw residue, latest capture-attempt errors, partial-display failures, and
  attention items are checked; an old last-success timestamp cannot hide a
  current failure

### 4. Startup and active process ownership

- `launchctl print gui/$UID/app.personalai.monitor.core` points to the intended
  Application Support core and the running PID belongs to that command.
- The foreground/login-item app process, when enabled, runs from
  `/Applications/Personal AI Monitor.app`, not `build/` or an old copy.
- LaunchAgent and login-item state are verified separately; one does not prove
  the other.
- Port `8765` is checked only if the development dashboard is explicitly
  enabled, and its listener PID/identity must belong to PAM. A foreign listener
  is `port_conflict`/`foreign_listener`, not a PAM orphan.
- The optional AI Connector remains loopback-only, GET-only, read-only, and off
  by default unless deliberately enabled.

### 5. Hermes / Feishu activity consumer

- The live `daily_report_context_lib.py` calls the matching live Core's
  `report-context --date ... --format json` command and validates version,
  date, `raw_access=false`, and `availability=available` before consuming any
  activity body.
- The live collector source contains no direct `extracted_activities`,
  `activity_summary`, or `sqlite3.connect` activity fallback.
- Codex / Claude session counts may remain content-free diagnostics, but prompt
  or session text cannot be used as a second activity-fact lane or copied into
  the daily payload.
- The deployed Hermes contract tests pass against the installed scripts, and a
  live-Core canary proves an unresolved Hofan/interview group is absent while a
  confirmed group is available exactly once. Clarification and ignored answers
  alone must leave the external payload unchanged.

Record the exact commit, build number, source-tree hash, core-code hash, and the
five verification results in the release evidence. Do not write “deployed”
when only source/build checks passed.

If the source is pushed, additionally confirm:

- remote `main` points to the local commit
- the working tree is clean and synced with `origin/main`
- GitHub Actions started and completed successfully
- CI ran the release privacy scan, core unit tests, and menu-bar app build

If CI fails, fix CI before calling the source update complete.

## GitHub Repository Settings (Only If Public Distribution Returns)

Keep these enabled for public maintenance:

- secret scanning
- push protection
- private vulnerability reporting or GitHub private security advisories
- issue and PR templates that ask users not to attach real screenshots,
  databases, logs, HA packages, raw OCR, tokens, or private activity data

## History And Secret Review

Before the first broad public announcement, run a full Git history review for
secrets and private data. Prefer a dedicated history scanner such as `gitleaks`
if available, and also review suspicious historical paths and binary additions.

The release privacy scan checks the current candidate tree. It is not a
replacement for a full history scan.

## Signed App Release Gate

Do not present the current app as a normal consumer download until these are
complete:

- signed and notarized macOS app artifact
- clean install and uninstall test on a fresh macOS user account
- user-facing migration path from source/runtime data to installed runtime data
- verified Screen Recording permission flow
- explicit VLM opt-in copy and controls
- updated threat model review for non-developer users

Until then, keep the artifact labeled as a personal local build. If it is shared
externally for testing, use "developer preview" wording and do not imply a
signed consumer release.

## Group F P0 source-only schema migration gate

This gate accepts only source evidence produced against the complete v2 parent.
It uses a synthetic SQLite fixture and does not prove a real PAM database
migration, installed evidence, live evidence, Hermes/飞书 delivery, or Memory
Gateway operation.

Run the exact migration tests independently before module/full gates:

```bash
cd core
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_context_console.ContextConsoleTests.test_candidate_schema30_rebuild_is_atomic_and_resumable_after_injected_fault \
  tests.test_context_console.ContextConsoleTests.test_schema29_backup_snapshot_and_upgrade_have_no_writer_gap \
  tests.test_context_console.ContextConsoleTests.test_schema_28_evidence_confirmed_screen_rows_are_downgraded \
  tests.test_context_console.ContextConsoleTests.test_schema_30_confirmed_without_answer_is_invalidated \
  tests.test_context_console.ContextConsoleTests.test_explicit_user_answered_confirmation_survives_migration \
  tests.test_context_console.ContextConsoleTests.test_schema_28_29_30_and_repeated_start_force_ha_auto_package_off \
  tests.test_context_console.ContextConsoleTests.test_real_v04_schema29_fixture_migrates_additively_without_reinterpretation \
  tests.test_context_console.ContextConsoleTests.test_stale_same_path_backup_from_different_database_identity_is_not_reused \
  tests.test_context_console.ContextConsoleTests.test_context_contract_docs_cover_trust_degradation_migration_and_rollback
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_activity_confirmations.ActivityConfirmationTests.test_schema_migration_preserves_explicit_owner_answer
```

Acceptance checks:

- the candidate-table state machine injects a real exception before and after
  rename, create, copy and drop for both receipt tables; every fault leaves the
  old PK/rows/semantics, no visible half-table, no repair marker advance and
  `quick_check=ok`; one retry succeeds and two further startups are no-op for
  rebuild;
- two real SQLite connections plus `threading.Event` demonstrate a single
  backup-to-migration writer barrier: a pre-barrier committed row is present in
  rollback and upgraded copies, a post-barrier writer cannot enter the gap, and
  a migration fault rolls back to schema 29 with the pre-barrier row intact;
- backup identity metadata and content hashes match; a stale same-path backup
  from another database identity, a concurrently appearing destination, or an
  orphan identity sidecar is rejected without overwriting the only rollback
  copy or its evidence;
- schema 28 screen-derived fake confirmation rows undergo trust degradation,
  schema 30 `confirmed + answer IS NULL` rows become `invalidated`, and explicit
  owner answers are preserved;
- settlement revision, package cache and diagnostic counters are invalidated in
  the same transaction as the trust change;
- the real v0.4 four-table/index fixture proves additive schema 29 migration
  without row, SQL-definition or index reinterpretation;
- the four contracts (`ContextReceiptV1`, `ControlledEvidenceV1`,
  `ContextBundleV1`, `DeliveryReceiptV1`) retain closed JSON parsing, the trust
  matrix, privacy target policy, hash projection and delivery state contracts;
- busy/null and retention/cascade behavior remain fail closed;
- every fresh/repeated startup forces
  `ha_auto_package_enabled=false`,
  `context_memory_writer_enabled=false`, and
  `context_source_revocation_cascade_ready=false`.

Then run module/full, privacy, compilation and patch gates:

```bash
cd core
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_context_console
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_activity_confirmations
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_v039_stability
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_core
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
cd ..
python3 tools/release_privacy_scan.py
git diff --check
```

Record source evidence, build evidence, installed evidence, live evidence and
Hermes/飞书 evidence as separate layers. For this source-only gate, the latter
three remain explicitly unverified. Do not open a real PAM database, perform an
installation/deployment, commit/push, enable an automatic writer, or describe a
synthetic rollback as a real-data rollback.
