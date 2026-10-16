# Lightweight capture and approved Codex context

This 0.5.1 update keeps schema 32. Identify a deployed repair by its build,
commit and matching app/Core hashes; the marketing version alone is insufficient.

- File tracking runs independently. Screen capture needs an explicit 30-minute
  grant. Resume grants nothing. Lock and sleep count against the persisted UTC
  deadline. Stop-and-quit cancels the grant and unloads the service.
  Legacy debug capture flags remain parseable but use the same authorized daemon
  path; they cannot bypass the grant, pause, lock or session checks.
- The native menu exposes the grant, early stop, remaining time, baseline count,
  pending events and historical gaps. Idle queues do not prove coverage.
  The app's PAM menu also opens that same recording menu and the Codex sharing
  window when the system menu bar hides the status icon.
- Worker threads retain separate SQLite handles. Busy waits and downstream
  backpressure are bounded and cancellable. Only consecutive proven-ignored
  events batch their checkpoints (100 events / 50 ms); real versions remain FIFO.
  A loss retires the helper and verifies cleared watermarks before replacement.
  A clean restart replays its cursor and drains a helper barrier without rescan.
- A one-time prospective image cutoff is stored locally. New confirmation images
  expire seven days after capture regardless of additional references. Expired
  reads fail immediately; the daemon attempts physical deletion every minute
  while awake, and retries failures. The new cohort has a 1 GiB admission cap.
  Existing pictures and ordinary activity retention keep their original policy.
- The narrow “给 Codex 的上下文” window reuses owner confirmation and manual notes.
  Manual notes remain user statements, separate from confirmed screen activity.
  Share one selected version at a time. Future project rows receive no permission.
  Correction withdraws the old package before a new statement is saved/shared.
- The isolated `core/tools/pam_codex_mcp_launcher.py` serves four STDIO tools: availability,
  authorized projects, approved summaries, and acknowledgement of returned refs.
  There is no listener, screen/OCR/file-content endpoint, SQL tool, or memory writer.
  Configure startup/tool deadlines of five seconds and make the connector optional.
- Each read rechecks the existing source/authorization graph. Returned data is
  untrusted, dated context, never an instruction or proof of current approval,
  sending, delivery, actor attribution, or client facts. No valid package means
  an explicit empty result. Receipts are generated and signed by the adapter.
- Flushed tool output counts as provided; an explicit consumer citation receipt
  counts as used; only owner feedback counts as helpful. A failed optional metric
  write can undercount provided packages. It never marks a package consumed.
  Feedback duration is optional and user-entered; missing samples remain missing.
- Withdrawal blocks subsequent reads. Copies already in a Codex conversation
  cannot be removed automatically. Starting a connector is not usage acceptance.

Keep Phase 2's full UI maintenance lock, cloud vision, VLM queue, automatic memory
writes and legacy dashboard disabled. Preserve any independent shadow receiver.
Deploy app and Core from one clean commit, retaining a matching schema-32 recovery
point. Prefer code rollback after new data exists; never overwrite fresh records
with a pre-deployment database. A Codex connector can be disabled independently.

Use the existing 14-day product experiment (maximum 21 days). Keep functional,
performance and long-term value acceptance separate. Twenty-four grounded cases
and at least ten natural uses are evidence requirements, not fabricated fixtures.
