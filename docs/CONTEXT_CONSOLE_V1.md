# PAM Context Console V1 — Phase 0 + Phase 1 Source Contract

This document describes the schema-30 **source candidate** for PAM's personal
context console. It is not an installation, rollout, live migration, Hermes or
Feishu validation, or a completed 14–21 day product experiment.

## 1. Product and trust boundary

PAM's owner-facing promise is to resume the correct task quickly while giving
AI only context that is trusted, source-linked, time-bounded, and revocable.
The implementation exposes two deliberately different projections:

| Projection | Allowed inputs | May contain privacy-cleaned local observation text | Eligible for trusted export / HA / `report-context` trusted body | Eligible for `MemoryCandidateV1` |
|---|---|---:|---:|---:|
| Owner-facing local observation | Privacy-firewalled activity, document metadata and screen clues, all explicitly marked `unverified` unless confirmed | Yes | No | No |
| Trusted context | Explicit user confirmation or a governed controlled adapter whose principal, key, registry labels, provenance, binding and state cap all validate | Only approved labels and state, never raw source body | Yes | Only when the verified context also carries `verification_scope=memory`, `memory_eligible=true`, owner subject, active P0/P1 provenance, and non-restricted privacy |
| Unverified clue | Screen/OCR, App/window/sidebars/tags/file-list hints and unresolved activity | Only in the local observation/clue partition after privacy filtering | Content-free clue event only | Never |

Screen/OCR and direct-submit/realtime screen heuristics are not completion
evidence. They persist as pending/clue observations and cannot independently
produce a `VerifiedContext`, trusted `ContextBundleV1` member, HA fact,
`report-context` trusted body, or memory candidate. A user answer or governed
controlled evidence is required to cross the trust boundary.

## 2. Evidence levels and state caps

| Level | Typical source | Maximum automatic effect |
|---|---|---|
| P0 | Explicit user confirmation or independently verified external delivery receipt | May verify the specifically bound state, subject to principal/registry/provenance checks |
| P1 | Governed local file/Git adapter with local verification | May verify the bound artifact state; operational evidence is never automatically memory-eligible |
| P2 | Structured AI lifecycle receipt | Execution/draft state only; `answer_ready` is capped at `draft_generated` |
| P3/P4 | Accessibility, browser metadata, App/window/screen/OCR clues | Observation/clue only |

File/Git/AI operational states can be useful to resume work, but two independent
operational sources still do not turn a task state into a durable person,
privacy, preference, relationship or other long-term memory fact.

## 3. Versioned contracts

### 3.1 `ContextReceiptV1`

A structured AI/tool lifecycle input. The parser and transaction boundary
enforce:

- a closed field set and exact contract version;
- 16 KiB maximum payload, bounded JSON depth/item count and at most eight
  evidence references;
- strict UTF-8, duplicate-key rejection, Unicode-normalized key collision
  rejection, and rejection of all Unicode `Cc`/`Cf` controls, including bidi
  controls and zero-width format characters;
- rejection of NUL, NaN/Infinity, paths/URLs, credentials, prompt/chat/
  assistant/transcript/command/diff/tool/screenshot/OCR/log content and free-form
  semantic fields;
- approved opaque project/workstream/object/version/subject references from the
  private label registry;
- authenticated HMAC-SHA256 principal/key, principal role and project scope;
- 15-minute maximum receipt age and two-minute future skew;
- idempotency scoped by contract type and source principal;
- canonical source-event identity independent of the caller's idempotency key;
- tombstone rejection, so a deleted logical source event cannot be revived by
  changing the idempotency key;
- same scoped key + same canonical payload is a safe replay; same key + a
  different payload is `idempotency_conflict` with no derived writes;
- final principal, key, label, registry, provenance, source-event and
  idempotency checks inside the same `BEGIN IMMEDIATE` write transaction before
  materialization.

Receipt bodies are accepted through stdin/private local channels. They must not
be placed in process arguments. AI lifecycle receipts cannot automatically
claim saved, committed, sent, published, installed, delivered or completed.

### 3.2 `ContextBundleV1`

The unified read model for resume, AI handoff and report adaptation.

- `verified_contexts` and `unverified_clues` are separate arrays;
- every verified item contains only public approved labels, stable state,
  validity/expiry/revocation status, privacy class and opaque source refs;
- each public `context_ref` is derived from that canonical public projection,
  never copied from or reversibly based on a `verified_ref`, rowid or database
  primary key; private membership keys remain only in relational tables;
- project, half-open time range and bounded local FTS query filters are
  supported;
- FTS indexes only approved registry labels and state, not raw source text;
- the Bundle hash covers a canonical stable public projection. It excludes
  `generated_at`, database row order and internal identifiers, so equal content
  produces the same hash across time and insertion order;
- a prepared delivery freezes the exact hash-matching payload in durable local
  storage; `fetch_prepared_delivery` returns that exact payload by immutable
  `delivery_ref` or public `bundle_ref` under the same shared authorization lock,
  and a documented hash recomputation must equal both manifest and delivery hash;
- `raw_access=false`; paths, raw JSON diagnostics, frame bodies, database row
  IDs and engineering fields are absent;
- Bundle reads take the same cross-process authorization lease as deletion:
  shared for read, exclusive for delete/revoke.

### 3.3 `MemoryCandidateV1`

A review candidate only. It contains no mem0 or direct memory-write capability.
Creation fails closed unless all of the following hold at the write boundary:

- the referenced `VerifiedContext` and every Observation/Evidence/Candidate link
  is active, unexpired and unretracted;
- the subject is the registered owner/self subject;
- provenance is complete and includes qualifying P0/P1 evidence;
- `verification_scope=memory` and `memory_eligible=true` were set by the
  governed evidence path;
- privacy is not `restricted_local`;
- the fact is not third-party, wrong-subject, screen/OCR-only, AI lifecycle-only,
  file/Git operational-only or otherwise operational state masquerading as a
  durable personal fact.

Creation, replay, active counts and every read/output surface reuse the same
eligibility predicate. A later scope, owner, subject, provenance, privacy,
expiry or revocation failure immediately removes the row from active counts and
outputs; replay terminalizes an old candidate as revoked rather than silently
reactivating it.

Every public memory result reports `direct_write=false`, `writer_enabled=false`
and `memory_write_actions=false`. Existing settings
`source_revocation_cascade_ready` and
`ha_auto_package_enabled` are forcibly kept false at governed startup for
schema 28, shadow schema 29, schema 30 and repeated startup until the governed
Memory Gateway is implemented and separately accepted.

### 3.4 `DeliveryReceiptV1`

Delivery state is separate from Bundle identity and from each immutable delivery
attempt.

```text
prepared -> consumed -> delivered
    |           |
    +---------> failed
```

- `prepared` is only a local frozen package;
- `consumed` requires an authenticated consumer ACK from a principal bound to
  the exact project, target and local/external target scope;
- `delivered` requires an independently authorized delivery-provider principal
  and provider receipt; consumer and provider identities/keys remain independent;
- every signed ACK binds the exact `delivery_ref`, `bundle_ref`, attempt number,
  Bundle hash, project, target, target scope, export policy, privacy-policy hash,
  principal role and ordered occurrence time;
- prepared/consumed never imply external delivery;
- failed attempts are retained. Retrying the same Bundle/target creates a new,
  monotonically numbered `delivery_ref`/attempt; an old ACK cannot cross an
  attempt boundary;
- receipt idempotency is scoped by delivery, principal and idempotency key;
- target registry policy must explicitly declare local/external scope, allowed
  privacy classes and `local_only` or `explicit_export`; an undeclared target
  fails closed and `restricted_local` is never externally exportable;
- local-only ACKs reject empty project, wildcard/unscoped, wrong-target and
  external consumer/provider principals;
- before any idempotent success, transition, attempt terminal state, principal,
  target/project labels, policy, manifest, frozen hash and every source dependency
  are rechecked inside the same immediate transaction. A failed/restricted,
  revoked or source-deleted attempt cannot be revived by an old key/digest/ACK.
- when upgrading an earlier v2 candidate, previously implicit target policies
  and unscoped delivery principals remain disabled until an explicit same-value,
  one-way declaration/binding is made. Legacy prepared/consumed attempts lacking
  immutable bindings are retained as locally failed history; delivered history
  is preserved and is not represented as remotely withdrawn.

## 4. Core graph and deletion closure

```text
Source event -> Observation -> Evidence -> ContextCandidate -> VerifiedContext
                                      |                 |             |
                                      +-----------------+-------------+
                                                        |
                                      MemoryCandidate / Bundle member
                                                        |
                                                  Delivery attempt
```

Manual deletion, source tombstoning, principal/key revoke, label revoke,
Observation/Evidence expiry and retention use the same dependency-closure
helpers. The closure:

1. tombstones the canonical source event;
2. revokes dependent Evidence, ContextCandidate and VerifiedContext rows;
3. removes the verified item from FTS;
4. revokes dependent MemoryCandidate rows;
5. restricts/revokes affected Bundle members and manifests;
6. restricts pending/consumed delivery attempts;
7. records a content-free withdrawal/restriction receipt for any previously
   externally delivered attempt.

An external delivery is never falsely described as remotely deleted. Its
historical `delivered` state remains, while the local withdrawal receipt states
that the source is now restricted/revoked. Delete/revoke operations are
idempotent and report failure rather than success if their transaction rolls
back.

## 5. Retention and product metrics

| Data class | Default retention / window | Coupling |
|---|---:|---|
| Unverified Observation/clue | Occurrence local date and the following local date (2 local natural days) | Independent graph closure; boundary is local midnight |
| VerifiedContext and necessary provenance | 90 local natural days | Independent of short activity retention |
| Product wins/quality metrics | 30 local natural days | Independent of short activity cleanup and component reset |
| Product experiment | 14-day minimum, extendable only for insufficient attempts through day 21 | Natural-day and attempt based; no real experiment auto-start |
| Runtime stability | Seven-day per-component windows | `capture`, `privacy`, `storage`, `context_fusion` reset independently |

All local-day policies use the persisted IANA timezone configured with
`context-console configure-timezone`; they never infer a UTC `timedelta` or the
host process timezone. A record occurring on local date `D` expires exactly at
local midnight starting `D+2`, `D+90`, or `D+30`, respectively. The instant
before that boundary remains retained; the boundary instant is expired. Each
product win stores its occurrence local date, expiry local date, and timezone,
so later activity cleanup or a component baseline does not alter the win.
The current configured timezone and every experiment timezone snapshot must
both remain valid IANA names. Missing or invalid values make product and
console status `unknown`, disable actions, and leave value-gate fields null;
there is no fallback to UTC, including when a caller supplies `local_today`.

A product experiment records the experiment reference, idempotent event ID,
local date, nullable `useful=true|false`, and nullable `resume_seconds`; no query
or project body is stored. A not-useful attempt remains in the denominator.
An exact event replay must match the stored IEEE float value; near-equal resume
times are conflicting payloads, not idempotent replays.
On day 14, fewer than 10 complete attempts yields `extended`/`pending`. With at
least 10 complete attempts, the value gate passes only when useful rate is
**at least 80%** and median resume time is **strictly below 30 seconds**. Exactly
80% passes; exactly 30 seconds does not. A complete day-14 result is persisted
as an immutable `passed`/`not_passed` terminal with a frozen evidence cutoff;
new attempts are rejected and cannot change its statistics. Only an
insufficient complete-attempt count extends through day 21, and the first
complete 10-attempt checkpoint becomes terminal. Day 21 is inclusive: fewer
than 10 complete attempts remains `extended`/`pending` during that natural day,
so a valid tenth attempt can still freeze the result. An explicit owner-confirmed
finalize on day 21, or the first status read from day 22 onward, yields
`insufficient`/`not_passed` with a day-21 evidence cutoff; later events cannot
rescue the result. Any missing usefulness or resume
evidence keeps the corresponding metric `null` and the experiment
`incomplete`/`pending`; missing evidence is never treated as an extension or
an invented conclusion.

The public experiment scorecard holds the same shared authorization lease as
the other trusted Phase 2 reads. Production time is sampled only after that
lease is granted, and the Core feature flag is checked again before returning
an available projection. A completed disable therefore returns an empty
`disabled` projection rather than a stale running experiment. The new
feature-flag, experiment start/finalize, handoff and feedback transaction
owners also roll back on process-control exceptions before rethrowing the
original exception; a later unrelated commit cannot persist their partial
writes.

`record_component_baseline` serializes generation increments with
`BEGIN IMMEDIATE` plus an atomic SQLite upsert. Capture/privacy/storage/
context-fusion changes reset only gates that explicitly depend on the named
component. UI/copy changes reset no component. Product wins and product
experiment rows are never cleared by a component baseline. `build_soak_report()`
and CLI status use the same component-window helper and generation values. A
missing component baseline remains `unbaselined`/`unknown`; the legacy global
resource comparison baseline is never substituted for a component baseline.
Aware non-UTC report end times are normalized to UTC before window calculation,
so report and runtime status emit identical effective-start representations.
The compatibility `campaign_started_at` field reflects only the capture
component and is `null` when capture is unbaselined.

## 6. Database busy and client state

New status reads have a bounded timeout and return a fail-closed envelope when
SQLite cannot be read:

```json
{
  "status": "unknown",
  "reason_code": "database_busy",
  "observed_at": "<UTC timestamp>",
  "last_successful_read_at": null,
  "counts": null,
  "service": "unknown",
  "raw_access": false
}
```

No busy/unreadable path reports healthy, zero counts or service=true. A real
last successful read may be reported when available; otherwise it remains null.

The Swift console follows the same rule. On success -> busy/unreadable it
clears trusted/verified/Bundle snapshots and disables actions; an old snapshot
cannot appear current. Recovery requires a fresh successful response before
actions/data return. Search, project selection, handoff rendering and feedback
authorization use monotonic generations. Once a feedback request crosses the
Core boundary, a content-free global checkpoint survives navigation: a
transport-unknown result permits only the exact stored idempotent request, and
a recorded result remains closed until a current experiment-status response is
accepted. Handoff `onAppear` callbacks carry an exact render generation, so an
older same-ref view cannot freeze a newer timer.

## 7. Schema 31, migration and rollback

Schema 29 is already occupied by the v0.4 shadow. Schema 30 establishes the
additive governed `context_*` graph. Phase 2 advances to schema 31 and adds only
`context_activity_confirmation_links` and `context_product_feedback`; it does
not reinterpret historical activity rows.

- **28 -> 31:** create a WAL-consistent SQLite backup inside the same
  continuous writer barrier used by migration, add schema-30 graph objects,
  then add the two schema-31 tables and their indexes.
  Screen-derived `evidence_confirmed` rows without an owner answer return to
  `pending`; every bare `confirmed + answer IS NULL` row becomes `invalidated`.
  Explicit owner answers are preserved, while associated settlement revisions,
  package caches and diagnostic counters are invalidated with the trust change.
- **v0.4 shadow 29 -> 31:** preserve the actual shadow tables and rows without
  interpreting, merging or rebuilding task-ledger JSON. Add the independent
  schema-30 graph followed by the two schema-31 tables.
- **30 -> 31:** add only the confirmation-link and product-feedback tables;
  unlinked legacy confirmations remain a bounded read-only compatibility
  source. Any link history, including revoked or expired links, is a durable
  tombstone that excludes the dual-written legacy row before candidate limits
  and FTS so governed content cannot duplicate or reappear after withdrawal.
- **31 repeated startup:** additive/idempotent initialization plus hard reset of
  HA auto-package, Context Memory writer and source-cascade rollout guards.
- **Backup identity:** a reusable migration backup must match schema version,
  device/inode identity and a content digest recorded in a `0600` identity
  sidecar. A path with stale or replaced content is rejected rather than
  silently reused.
- **Rollback:** stop writers, verify the private backup and identity sidecar,
  restore the complete SQLite backup, and restore the prior source/runtime as a
  separate operator action. The source candidate never performs an automatic
  destructive downgrade. Schema-31 downgrade triggers reject relabeling to an
  older user version.

No real database migration is performed by this source delivery.

## 8. Initial source integrations

- Existing file/document change commit path creates an operational
  Observation -> Evidence -> ContextCandidate -> VerifiedContext projection in
  the same SQLite write transaction as the source event.
- The projection contains opaque labels/state only, no path or document body,
  uses P1 operational evidence, and sets `memory_eligible=false`.
- Failure of the projection rolls back the source file event, preventing an
  acknowledged source event without its required context projection.
- AI lifecycle remains an explicit authenticated controlled input.
- No browser deep parsing, generic Hermes outbox, new VLM, clipboard, keyboard,
  audio or chat archive was added.

## 9. Feature flags and forbidden actions

The schema exists for source testing, but production rollout remains off:

```text
context_console_phase1_enabled=false
ha_auto_package_enabled=false
context_memory_writer_enabled=false
context_source_revocation_cascade_ready=false
```

This candidate must not be used to claim installation, deployment, live Core
migration, real Hermes/Feishu delivery, mem0 write, a completed 14–21 day
experiment, ten real recoveries, or installed-App acceptance.

## 10. Verification gates

Configure the local-day timezone explicitly before recording governed local-day metrics:

```bash
(cd core && python3 -m activity_mvp context-console configure-timezone --timezone Asia/Singapore)
```

Minimum source gates:

```bash
python3 tools/release_privacy_scan.py
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_context_console)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_core)
(cd core && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  tests.test_document_changes.DocumentChangeTests.test_debounced_saves_preserve_each_observed_content_version)
(cd core && python3 -m unittest discover -s tests)
(cd core && python3 -m activity_mvp cleanup-retention)
(cd core && python3 -m activity_mvp cleanup-retention --days 0) # must fail
git diff --check
```

Run the Swift interaction and release-build commands from
`docs/RELEASE_CHECKLIST.md` on a compatible macOS host with the specified SDK.
A Linux Swift source check is useful evidence, but is not a macOS release build,
installation or live acceptance result.

## 9. Group F P0 schema migration and rollback integrity contract

This section is the executable **source contract** for the Group F correction.
It applies to source code and a synthetic SQLite fixture only. Passing it does
not prove a real PAM database migration, an installed application refresh, a
live Core upgrade, a Hermes/飞书 delivery, or a Memory Gateway write.

### 9.1 Four external contracts and closed JSON

The four externally visible contracts remain separate and versioned:

1. `ContextReceiptV1` accepts an authenticated lifecycle receipt and can create
   only the state allowed by its source tier.
2. `ControlledEvidenceV1` accepts an authenticated, registry-bound controlled
   evidence receipt and applies its state cap, verification scope and memory
   eligibility at the same write boundary.
3. `ContextBundleV1` is a frozen read projection with separate trusted items and
   content-free unverified clues.
4. `DeliveryReceiptV1` advances one immutable delivery attempt only after its
   principal, target, bundle hash and transition are revalidated.

Each input contract is **closed JSON**: unknown fields, duplicate keys, unsafe
Unicode controls, oversized/deep structures and prohibited body categories are
rejected rather than silently ignored. This migration does not broaden any
contract or add an automatic writer.

### 9.2 Trust matrix and trust degradation

| Stored row before startup | Schema migration result | Owner answer fields |
|---|---|---|
| schema 28 `evidence_confirmed` with `answer IS NULL` | `pending`, local clue only, notifications disabled | remain null |
| schema 30 `confirmed` with `answer IS NULL` | `invalidated`, not a trusted confirmation | remain null; stale `answered_at` is cleared |
| legacy `evidence_confirmed` with `answer='occurred'` | `confirmed` | preserved byte-for-byte |
| legacy answered row with `not_occurred` / `misrecognized` / `ignored` / `defer` | `rejected` / `rejected` / `ignored` / `deferred` | preserved byte-for-byte |
| manual explicit owner confirmation with `answer='occurred'` | remains `confirmed` | preserved byte-for-byte |

This is a one-way **trust degradation** of state labels that were derived from
screen/OCR heuristics. It is not a rewrite of explicit owner answers. When a
row is degraded, the same transaction removes activity-confirmation settlement
revisions, invalidates active `distribution_packages` cache references, and
removes sampled-out and episode-diagnostic counters so no stale aggregate can
mix old and new trust semantics. Repeated startup is idempotent.

### 9.3 Candidate-table atomic rebuild state machine

The schema-30 candidate repair for `context_ingest_receipts` and
`context_delivery_receipts` runs under one explicit transaction (or one
savepoint inside the schema migration transaction):

```text
validate old shape
  -> rename to the fixed _v1 shadow name
  -> create the current table
  -> copy/transform all rows
  -> validate row count, bidirectional EXCEPT semantics, PK and scoped UNIQUE
  -> drop the shadow
  -> write the repair marker
  -> commit
```

Faults before or after rename, create, copy or drop roll back the entire
sequence. The old table, rows, receipt semantics and schema marker remain
visible; no empty replacement or stale half-table is committed. If both the
canonical and `_v1` names are already visible, startup fails closed because a
table name alone cannot identify which side is authoritative. It never guesses
and never drops either side. A successful retry performs the rebuild exactly
once; later startups do not re-enter a rebuild stage.

### 9.4 Backup, writer barrier and rollback

For schema 28 or schema 29 migration, `BEGIN IMMEDIATE` is the writer-barrier
linearization point. From that point through backup creation, schema changes,
commit or rollback, the same SQLite connection continuously excludes later
writers. A second read-only connection drives `sqlite3_backup` while the writer
barrier remains held.

Therefore:

- every writer committed before the barrier is present in both the rollback
  backup and the upgraded database;
- no writer can commit in a backup-to-migration gap;
- a later writer resumes only after migration commit/rollback;
- a migration fault rolls the source database back to its pre-migration schema
  and rows while leaving the validated rollback backup intact.

Backup metadata binds `schema_version`, database file identity, source-content
SHA-256 and backup-content SHA-256. A stale same-path backup from a different
database identity or different content is rejected and is not overwritten.
Backup and identity-sidecar publication use same-directory atomic no-clobber
links; a concurrently appearing backup or an orphan sidecar fails closed rather
than replacing evidence. A rollback backup must open read-only, return
`PRAGMA quick_check=ok`, match the expected schema and preserve representative
rows before it is considered usable.

The **schema 29 migration** is additive: the real v0.4 `task_episodes`,
`task_evidence_events`, `hermes_outbox`, `hermes_delivery_receipts`, their
representative rows and all reference-defined indexes remain unchanged while
new `context_*` objects are added. The synthetic test deliberately proves that
those rows are not reinterpreted into Context observations, evidence,
candidates, deliveries or memory candidates.

### 9.5 Privacy, hash, delivery, busy/null and lifecycle invariants

- The **privacy target policy** remains fail closed: local-only/restricted data
  cannot be exported to an external target without an explicitly allowed
  target scope and privacy class.
- The Bundle **hash projection** covers the canonical stable public projection,
  not generation time, row order, raw paths, internal row IDs or diagnostics.
- **delivery state** remains attempt-scoped (`prepared -> consumed -> delivered`
  or `failed`); schema migration cannot infer external delivery.
- **busy/null** behavior remains fail closed: busy, missing or unreadable
  database status is `unknown`; unavailable counts and trusted snapshots remain
  null/cleared rather than copied from an earlier successful read.
- **retention/cascade** behavior remains dependency-closed and idempotent across
  observation, evidence, candidate, verified context, FTS, bundle, delivery and
  memory-candidate projections. Delivered history is retained while local
  withdrawal/restriction evidence is content-free.

### 9.6 Flags and evidence layers

Every schema 28, real schema 29, schema 30, fresh startup and repeated startup
forces these settings to the current-stage safe state:

```text
ha_auto_package_enabled=false
context_memory_writer_enabled=false
context_source_revocation_cascade_ready=false
```

There is no automatic re-enable path in this source candidate.

Evidence must be described by layer:

- **source evidence**: patch, modified source, exact tests and synthetic SQLite
  state observations;
- **build evidence**: a reproducible build/typecheck run for a named source
  snapshot;
- **installed evidence**: inspection of an actually installed artifact;
- **live evidence**: observation of the running Core and its real data path;
- **Hermes/飞书 evidence**: an independently observed provider/consumer delivery
  receipt.

Source evidence cannot be promoted by wording into build evidence, installed
evidence, live evidence or Hermes/飞书 evidence. Group F explicitly prohibits
opening a real PAM database, installing or deploying an artifact, enabling HA
auto-package or a Memory writer, and claiming a real migration/rollback from a
synthetic SQLite fixture.

This synthetic SQLite fixture does not prove a real PAM database migration.
