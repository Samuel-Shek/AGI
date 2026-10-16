# Criticism collection and improvement review

AGI 0.9 adds the explicitly approved autonomous workflow in [AUTONOMY.md](AUTONOMY.md). Its fixed controller handles technical investigation and supported cross-project repairs; genuine human choices remain separate. The contract below describes the original pipeline where it is not superseded by that policy.

AGI 0.8 adds a separate owner-authorized feedback ledger at `core/data/feedback`. It does not modify the activity database or shared-memory lifecycle overlay. The existing memory-body grants remain independent.

## Contract

`python -m activity_mvp.feedback.cli` accepts fixed actions: `status`, `detail`, `configure`, `tick`, `pause`, `resume`, `stop`, `decision`, `notifications`, `notifications-ack`, `seed`, `verify-isolation`, `verify-model`, and the local operator-only `accept-release`. Structured operator input uses `--payload-stdin`. Status and detail are read-only and do not invoke models or source scanning. UI decisions bind id + current revision + explicit choice; defer remains pending. No endpoint accepts arbitrary commands, grants from model text, or automatic memory decisions.

The three source adapters authenticate before reading: local Codex owner conversations, Claude primary conversations, and the default Hermes local CLI/desktop/TUI sessions. Automated profiles, subagents and gateway sources without verified sender mapping are excluded and counted as incomplete identity coverage. History backfill is bounded to thirty days at enable time, plus the explicitly imported existing archive. Direct criticism candidates are filtered locally, then classified semantically through the approved Codex channel; the local detector is heuristic, not a claim of complete recall.

Assistant context is reduced locally to structural cues; assistant prose that could quote business or memory bodies is not automatically forwarded. Long-running Hermes sessions are included by recent message timestamp after identity admission. Messages and quotes have stable provenance and deduplication keys. A semantic relation is only a recurrence hypothesis. Confirmed post-installation failure requires a fixed verifier receipt; no new criticism or three synthetic passes cannot prove improvement. Behavior verification requires seven days and three distinct verified natural samples. Models never attest their own success.

## Scheduling and budgets

One launchd tick runs every five minutes, guarded by a process lock. Daily checks become due at 09:30 Asia/Shanghai, and weekly review on Sunday 18:00; missed work is coalesced on wake. The database remembers admissions and due slots. New messages take priority over historical backfill. Analysis has no local daily call cap. Each batch contains at most five messages and may run for fifteen minutes on the existing gpt-6-astra/max channel. A separate process lock prevents overlapping model invocations. The Codex account still enforces its actual service limits; AGI does not infer account balance from local call counts. Failures retry after 5/15/30/60 minutes; explicit account-limit errors wait at least one hour. Retry state survives restarts, and successful analysis clears it. One engineering job per day remains a hard limit, with at most two model revisions, each at most thirty minutes. Unknown usage remains recorded and is never refunded or erased. No account purchase, usage reset or provider fallback is attempted.

Pause/stop rejects new source reads, model work and repairs. Interrupted installation is reconciled, not blindly retried. A global AGI stop also writes the feedback stop marker. Notifications contain generic text and never raw criticism; a delivery request is not proof the user read it.

## Repair boundary

Automatic changes are limited to pure text parsing and journal formatting functions. Identity admission, file writes, all other AST nodes and function signatures must remain identical; imports, indirect calls and effectful operations are rejected. UI or other AGI repairs create a concrete decision instead. Only the installed allowlist in `feedback/repair.py` may be changed automatically. Grants, identity/authorization controls, model budgets, verifier, release machinery and shared-memory decisions are outside that allowlist. The model works with a restricted permission profile, while the controller owns dispatch, verification and publication. Real read/write/network denial checks are required before activation; a file-mode label is not OS isolation.

A candidate must have an actual assertion failure on the old source, pass on the candidate in the real OS sandbox, and pass the unchanged full gates. Model-written regression tests are excluded from the host suite and only run inside the sandbox. The host runs only the fixed original tests after pure-function admission; this preserves existing loopback connector tests without granting model tests access to local services. Clean build provenance, signing and paired source/App/Core are checked independently. Code rollback preserves current feedback/activity databases. Confirmed recurrence or failed bounded attempts disables additional automatic installation for the incident and creates a decision item.

## Journal

Obsidian contains a managed section plus a separate user annotation area. Existing human content is never blindly overwritten. Changes within the managed section create a merge candidate. Quotations, cause hypotheses, repair claims and validation gaps have separate sections. The prior history archive remains unchanged.

## Acceptance

Installation acceptance records source-specific coverage, isolated and live checks separately. Capability status must describe real verification, not merely prepared code. Pending natural usage, unavailable senders, interrupted jobs and failed model requests remain visible. No configuration file alone enables a claim that repairs or all-platform learning are proven.


主动功能检查与发布后单项通知见 [主动发现与经核验改进](PROACTIVE_IMPROVEMENT.md)。功能范围、暂停、原预算和自然验收保持独立。
