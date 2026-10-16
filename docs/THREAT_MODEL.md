# Threat Model

Personal AI Monitor handles screen-derived personal context. The security goal
is to help one person recall their own work without creating a durable or
shareable surveillance record.

This model covers the source Open Alpha. It does not claim consumer-grade
hardening for a signed public app release.

## Intended Use And Non-Goals

Personal AI Monitor is for voluntary local recall on the user's own Mac.

It is not designed for:

- employee monitoring
- classroom or child monitoring
- team dashboards
- hidden recording
- default cloud sync
- general-purpose or archive-style raw screenshot or video retention; the only
  exception is source-bound local evidence while a confirmation remains
  `pending` or `deferred`

## Assets

- Raw keyframes and temporary OCR input.
- Source-bound confirmation evidence and its private schema-27 reference ledger.
- Browser, Accessibility, and DocumentSignal context.
- Authorized-root file events and non-reconstructable structural fingerprints.
- Redacted extracted activity, search index rows, and daily review output.
- Privacy events, gap events, duplicate events, and coverage records.
- HA Source Package cache and revocation records.
- Deletion records and local retention state.
- LaunchAgent plist, daemon settings, native host manifests, and runtime logs.
- Local model endpoints and any local VLM backlog files.

## Entry Points

- macOS ScreenCaptureKit and OCR helpers.
- Accessibility helper and foreground document inspection.
- FSEvents helper and authorized-root baseline/reconciliation.
- Chromium browser extension and native messaging host.
- CLI commands under `python3 -m activity_mvp`.
- LaunchAgent daemon mode.
- Development dashboard on `127.0.0.1` when explicitly started.
- HA Source Package export and local timeline/learning summary export.
- Unit tests, sample data, release scripts, and app build scripts.

## Trust Boundaries

| Boundary | Existing control | Residual risk |
| --- | --- | --- |
| macOS screen capture permission -> core | macOS Screen Recording permission and visible native status. | A user can still grant permission without understanding sensitivity. |
| Raw keyframe/OCR -> durable storage | Privacy Firewall runs before timeline/search/HA surfaces. Ordinary raw files are deleted after processing; only a clear `pending/deferred` source may be promoted into the private confirmation-evidence vault. | Classification can be imperfect, so uncertain input must fail closed. |
| Confirmation evidence -> clarification/public interfaces | A source-bound reference, evidence lock, local re-OCR, and a second Privacy Firewall pass precede use. Public contract v4 exposes only post-firewall text and content-free availability with `raw_access=false`; report-context v2 exports only unresolved counts/categories/completeness. | A local file can remain sensitive while a high-value occurrence question is unresolved, so retention holds, explicit deletion priority, lifecycle release, and deletion health must stay visible. |
| Browser extension -> native host | Fixed extension ID, native messaging manifest, sanitizer, short TTL, user-only file permissions. | Broad browser permissions look scary and must be documented clearly. |
| Local dashboard -> browser | Bind host must be loopback, no CORS, Host and Origin/Referer checks, destructive confirmation code. | A malicious local process on the same Mac is out of scope for strong isolation. |
| VLM/local model -> core | Model endpoints must be loopback; non-loopback endpoints are rejected before upload. | A local model can be slow, unavailable, or return invalid output, causing gaps. |
| App -> core command channel | Native app shells out to core commands and does not read SQLite directly. | Source Alpha is not signed/notarized; users must trust their local checkout. |
| Authorized roots -> document change layer | Explicit roots, no symlink traversal, ignore rules, keyed fingerprints, content-free protection, and no raw path export. | FSEvents proves a local change, not who made it; sync and atomic-save patterns require honest confidence and reconciliation. |
| Export -> external tools | HA packages require `raw_access=false` and `source=extracted_only`. | External provider logs are outside local deletion once a package is sent. |
| Git repository -> public internet | Runtime data is ignored; release privacy scan and CI check candidate files. | Full Git history still needs separate secret/privacy review before broad launch. |

## Primary Abuse Paths

### 1. Sensitive screen content reaches search or export

Impact: High. This could expose credentials, private conversations, payment
flows, or adult content in durable local views or exported packages.

Likelihood: Medium. The system intentionally processes screen-derived data, so
the main defense is correct filtering.

Existing mitigations:

- high-risk apps, domains, keywords, payment flows, and credential pages are
  blocked or redacted
- `review_required` becomes a content-free gap event
- HA Source Packages validate `raw_access=false` and `source=extracted_only`
- raw keyframes and OCR source text are not committed or exported
- VLM failures fail closed

Residual work:

- keep expanding tests for newly discovered sensitive flows
- keep the browser extension and document signal fields metadata-oriented
- do not market the Alpha as a complete life log

### 2. Browser extension becomes perceived or used as browsing surveillance

Impact: Medium to high. Browser URLs and form context are sensitive even when
redacted.

Likelihood: Medium. The extension needs broad permissions to work across sites.

Existing mitigations:

- extension sends low-sensitivity signals only
- native host sanitizes URL, domain, private/form flags, and risk tags
- signals expire quickly and are local-only
- input values, cookies, bookmarks, downloads, clipboard, full body text, and
  full history are not read or stored

Residual work:

- keep browser privacy documentation prominent
- keep extension install opt-in
- avoid adding full-page text capture without a separate privacy review

### 3. Local dashboard is reached by an unwanted webpage

Impact: Medium. A malicious webpage could try to trigger local API actions.

Likelihood: Low to medium in source Alpha, because the dashboard is disabled by
default in daemon mode.

Existing mitigations:

- dashboard binds to `127.0.0.1`
- non-loopback bind hosts are rejected
- CORS is not enabled
- Host and Origin/Referer checks are local-only
- destructive deletion requires a short-lived confirmation code

Residual work:

- keep the dashboard developer-only
- use native app controls for public workflows
- do not add remote dashboard access without a new auth design

### 4. VLM backlog retains raw image files longer than expected

Impact: Medium to high. Pending VLM frames are raw visual material even if kept
inside a restricted local queue.

Likelihood: Medium when a user opts in to local VLM review and the model is slow
or unavailable.

Existing mitigations:

- public source install keeps VLM review disabled by default
- pending jobs are local, restricted, capped by count/bytes, and processed under
  power/memory policy
- endpoint guard rejects LAN and cloud model endpoints
- successful parseable review deletes the temporary image
- deletion and retention cleanup clear related local derived state

Residual work:

- keep UI copy clear when VLM is disabled, degraded, or cooling down
- treat VLM as optional review assistance, not a core dependency
- do not enable cloud VLM without a separate security review and explicit opt-in

### 5. Confirmation evidence outlives its question or leaks through an interface

Impact: High. A retained source image can contain private visual material even
though public outputs are cleaned.

Likelihood: Low to medium. Evidence exists only for a small, explicit class of
unresolved questions, but a crash, concurrent clarification, or failed unlink can
delay deletion.

Existing mitigations:

- only Privacy-Firewall `clear` sources that create `pending/deferred` questions
  may be promoted from the short-lived raw vault
- `confirmation_evidence.noindex` is Core-private, mode `0700`; image files and
  the evidence lock are `0600`, indexing is disabled, and macOS backup exclusion
  is applied
- evidence has no independent TTL but is reference-bound: terminal answers,
  system filtering, source/range/retention deletion, and privacy reclassification
  release it
- clarify holds the evidence lock, reruns local OCR plus Privacy Firewall, and
  public App/Connector/HA/Hermes surfaces remain `raw_access=false`
- unlink failure remains `delete_pending` and content-free health reports
  attention until reconciliation actually removes the file
- pre-v0.3.16 cards remain `evidence_unavailable`; no heuristic screenshot match
  or historical backfill is allowed

Residual work:

- exercise concurrent clarify/delete and forced-unlink-failure canaries in every
  release
- keep the private evidence vault out of Git, sync, exports, logs, support
  bundles, and general-purpose image browsing
- monitor retained bytes and every nonzero `delete_pending` or error count

### 6. Public repository accidentally includes private runtime data

Impact: High. A database, log, screenshot, HA package, or token in Git would be a
public data exposure.

Likelihood: Low after current controls, but the impact warrants repeated gates.

Existing mitigations:

- runtime paths are ignored
- release privacy scan checks the candidate tree
- CI runs privacy scan and tests
- issue templates and safety docs tell users not to attach real data

Residual work:

- run a full Git history secret/privacy scan before broad announcement
- keep GitHub secret scanning and push protection enabled
- close or redact public issues that include private runtime artifacts

### 7. A fork repackages the project as hidden monitoring software

Impact: High for misuse victims and for project trust.

Likelihood: Medium for any open-source capture tool.

Existing mitigations:

- AGPL-3.0-only license
- anti-surveillance positioning in README and safety docs
- no stealth mode, no team dashboard, no employee-monitoring roadmap
- visible recording state is treated as a product requirement

Residual work:

- reject contributions that normalize covert or workplace surveillance
- keep public language centered on voluntary personal recall
- avoid adding hidden-start or silent-running convenience features

## Source Alpha Limits

The source Alpha is useful for developers and technical users, but it is not a
finished consumer security boundary:

- the macOS app is not signed or notarized
- source installs depend on the user's Python and local permissions
- clean install/uninstall has not been packaged for ordinary users
- local malware on the user's Mac is out of scope
- Time Machine, iCloud, external sync tools, and manually copied backups are out
  of local deletion scope

These limits must remain visible until the signed public app release gate is
complete.
