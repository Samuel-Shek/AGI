# Privacy and Safety Model

Personal AI Monitor handles highly sensitive local activity data. Privacy behavior is the
product contract, not an optional implementation detail.

## Intended Use

Personal AI Monitor is for personal, voluntary, local context recall on the user's own
Mac. It must not be adapted into silent monitoring of another person, employee
surveillance, classroom monitoring, or a default cloud-sync product.

## Product Trust Surfaces

Competitor review reinforced that privacy is a visible product feature, not a
settings footnote. The first public version should treat these as primary
surfaces:

- visible recording, pause, privacy-protection, and error state
- one-click Off-the-record pause for 15 minutes, until next lock, today, or
  until manual resume, plus clear resume behavior
- app/domain exclusion controls
- destructive deletion with local device-owner confirmation and cascade cleanup
  summaries
- coverage health that explains captured, protected, dropped, and gap counts
- no hidden mode, no team dashboard, and no default cloud sync

## First Public Version Boundary

- Capture mode is keyframe-only.
- No short video buffering.
- No audio recording.
- No keyboard logging.
- No clipboard reading.
- No chat transcript capture.
- No default cloud upload or sync.
- HA/Hermes export is optional and disabled by default.
- Local VLM review is optional and disabled by default for source Open Alpha
  installs; users must opt in before daemon mode queues images for VLM review.
- VLM and local learning text model endpoints must be loopback-only. Environment
  variables cannot redirect keyframes or cleaned learning text to LAN or cloud
  endpoints in the default product boundary.
- Browser extension signals are optional and low-sensitivity. See
  [Browser Extension Privacy](BROWSER_EXTENSION_PRIVACY.md) for the permission
  boundary and the fields that are never read or stored.

## Data Boundaries

- Raw keyframes stay local and temporary. Ordinary frames are deleted after each
  capture round; VLM-suspect frames may remain in their restricted local
  processing queue. A frame may be promoted into the separate
  `confirmation_evidence.noindex` vault only when it is bound to an
  activity-confirmation item that remains `pending` or `deferred`.
- Pre-filter OCR/VLM text must not be written to SQLite, logs, caches, HA
  packages, daily review output, or cloud requests.
- Only `clear` and locally cleaned `partial_redacted` extracted activities can
  enter timeline, search, daily review, or HA Source Package output. Text removed
  by partial redaction must not enter durable output.
- HA Source Package / HA 安全包 is complete cleaned structured data for the
  selected range, not a raw full mirror. It can include safe activities plus
  content-free protected, gap, duplicate, coverage, and learnability diagnostics,
  but never raw screenshots, raw OCR, document originals, tokens, or local paths.
- `review_required` content becomes a content-free gap event in the MVP.
- v0.3.18 activity-confirmation contract v4 may internally reopen a locally
  retained source image when the user requests clarification. OCR is rerun for
  that request and its output must pass the current Privacy Firewall before it
  may influence the rewritten question. Pre-filter OCR remains memory-only and
  must not be written to SQLite, logs, caches, or response payloads.
- Schema 28 preserves private evidence-file metadata and source references and
  adds only cleaned business-episode, priority, safe-signal, and three-level
  answer fields. Public
  confirmation records may expose only content-free availability such as
  `available`, `partial`, or `unavailable`; they never expose image bytes, raw
  OCR, full window text, file names, hashes, absolute paths, chat bodies, or
  secret data, and every public response retains `raw_access=false`.
- Grouped cards retain source-level provenance but never expose it as raw data.
  “Complete recognition content” means complete post-firewall cleaned text only;
  duplicate cleaned text may be collapsed with a count. Local clarification may
  combine newly cleaned OCR with the group, a bounded +/-15-minute safe context,
  and the latest sanitized local note. That note exists only while the question
  remains unresolved and is erased at terminal state. It cannot call Hermes, record an
  answer/calibration, or release a fact downstream. A pre-v0.3.16 card without a
  retained source returns `evidence_unavailable`; it must not claim to have
  re-read a screenshot. The macOS App sends this private note to Core over a
  bounded stdin pipe; the note itself is never placed in process arguments.
- In v0.3.17 the sanitized note may guide context classification, such as
  identifying a strongly corroborated game reward screen, but it never supplies
  occurrence evidence. A policy-v5 context filter keeps `answer=NULL`, records no
  calibration, exposes no note text, and releases no downstream fact. Grouping
  and OCR-prefix deduplication run only on post-firewall safe metadata/text and do
  not broaden raw-data access.
- Reversible calibration stores only content-free App/event/region/reason
  features and aggregate counters. User correction text remains source-bound
  and is deleted with the originating activity; it is never copied into a
  generalized rule.
- Only high-value occurrence questions retain source-bound local evidence.
  Ordinary business-attribution sampling stores cleaned structured signals and
  never promotes a source image. Manual omission text is sanitized and must not
  contain secrets, credentials, private-message bodies, or local paths.
- Pending, deferred, rejected, and ignored high-impact occurrence claims fail
  closed and are absent from AI handoff and Hermes/Feishu report bodies.
  `report-context` contract v2 exposes only completeness, counts, and broad
  categories for unresolved work; it never exports company, question, OCR, or
  evidence details. An unavailable producer reports unknown, never a fabricated
  zero.
- Ordinary retention may not silently delete unresolved confirmation sources.
  Explicit user deletion, privacy reclassification, system filtering, and
  terminal answers still win and release source-bound evidence immediately.
- `blocked` content is dropped and represented only by privacy metadata.
- The durable layer should move toward text-only retention: cleared summaries,
  activity facts, provenance, and coverage statistics, not reusable raw images.

## Local Recall

Timeline, search, and daily review are local views over `clear` activity and
locally cleaned `partial_redacted` safe summaries. They are not a complete record
of the user's life, and they must not claim to cover activity outside this Mac.

Daily review must include a source marker such as `local_clear_activity_only` so
downstream users know it was built from filtered local activity rather than raw
screenshots or cloud analysis.

## HA Source Packages

Every readable HA Source Package must satisfy these invariants:

- `raw_access=false`
- `source=extracted_only`
- `target=ha`
- `source_status=active`
- current `privacy_firewall_version`
- current `redaction_policy_version`

The historical SQLite table name `distribution_packages` is kept for
compatibility. It stores HA Source Package records; it does not imply multi-agent
distribution.

## Local Interfaces

The HTTP dashboard is a development-only surface:

- bind only to `127.0.0.1`
- reject non-loopback bind hosts and non-loopback Host headers
- no CORS
- local Origin/Referer checks
- destructive actions require a short-lived, single-use confirmation code
- daemon mode must not start the dashboard unless explicitly requested

The public macOS app should use a native local command channel. Destructive
actions should use Touch ID, passcode, or equivalent device-owner confirmation.

## Retention and Deletion

By default, cleared local activity and HA Source Package data is retained only
for today and yesterday. Older local derived data should expire through the same
cascade cleanup path as manual deletion.

Deletion should cascade through local derived data, search indexes, HA Source
Package cache files, source records, and coverage records. Local deletion cannot
guarantee deletion of external provider request logs if a clear package was
already sent outside the program. It also cannot remove Time Machine, iCloud,
third-party sync, or manually copied backups outside the program's local runtime
tree.

`cleanup-retention` must be safe by default: without `--apply` it only reports
what would be deleted, `--days 0` is rejected, and live cleanup requires the
explicit `DELETE_EXPIRED_LOCAL_ACTIVITY` confirmation string.

The today-and-yesterday retention window applies to the current runtime data
tree, not to old source checkouts, deployment snapshots, or manually copied
backup folders. Those copies can contain SQLite databases or HA package caches
that bypass normal runtime deletion until the user reviews and removes them.
`data-hygiene` and `backup-hygiene` are read-only diagnostics for that review;
they group risks and recommend actions, but must not delete backup files without
a separate explicit confirmation.

Confirmation evidence has no independent time-based TTL while its question is
`pending` or `deferred`; otherwise a slow response could make the promised
re-analysis impossible. It is still temporary and source-bound: an occurrence
answer, `ignored`, system rejection, source deletion, privacy reclassification,
retention cleanup, or time-range deletion releases the reference immediately.
The file deletion is idempotent. If unlink fails, the private ledger remains
`delete_pending`, health reports content-free attention, and startup/reconciliation
retries the deletion. A failed unlink must never be reported as deleted, and the
path or error detail must never enter public output.

Pause is a data boundary: while paused, capture triggers must not save raw
keyframes, OCR text, activity summaries, search index rows, or HA package input.

Runtime data such as SQLite files, logs, raw vault files, debug state, HA package
cache, screenshots, and OCR source text must never be committed to Git.
