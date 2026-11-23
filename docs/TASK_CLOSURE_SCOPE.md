# Task closure authorization scope

`target_root` identifies the project. `authority.scope` identifies the authorized
write targets. The legacy scope string equal to the project root remains valid.
For narrower authorization, use exact absolute paths:

```json
{"files": ["/workspace/project/result.md"], "trees": ["/workspace/project/generated"]}
```

Either list may be omitted. The total must be 1–40 paths. `files` grants only the
named files, including outputs not yet created. `trees` grants the named directory
and its descendants. Paths must be canonical, inside `target_root`, and contain
no symlinks. Existing exact files and recovery reads must have one hard link;
an alias to the same inode is a technical limitation, never an implicit grant.
Paths are literal, never inferred from prose or expanded as globs.
Keep original semantic limits in the source and task instructions; this format
does not authenticate user consent or add an operating-system filesystem sandbox.
Explicit evidence identifies separate read inputs under the existing evidence
contract; it is not an expansion of write authorization.

All existing risk booleans still require exact boolean values. Actual personal
choice, cost, new permission, external action, and irreversible risk retain the
human gate. Missing metadata or unusable scope enters `blocked`, with recovery
assigned to the original executor. Existing `decision` and cancellation records
are never automatically released or migrated.

Registration, dispatch (also before creation and sending), and recovery use the
same scope check. Recovery files must be within the declared files or subtrees.
An invalid legacy row cannot dispatch merely because its state says `pending`.

The original executor may correct an unusable encoding by registering the same
stable key and a current native task belonging to the original owner, with a valid scope plus:

```json
{"scope_correction": {"revision": 3, "basis": "Original source confirms these exact targets and all existing limitations."}}
```

The source and every other authority field must stay equal, and the submitted
evidence must equal the original, still-current evidence. This operation only
accepts an original-owner, `blocked`, never-dispatched item at the exact current
revision. Caller identity must come from the native process and its current
hook task receipt; payload identity is insufficient. The current entry supports
Codex only; missing native identity is rejected. An unmatched key cannot create
a new correction item. It preserves the original issue, task, owner, state, evidence, choices,
and acceptance requirements, and saves both scope encodings and the source basis
in an event. Background ticks keep this declared blocker until an explicit
executor transition; format correction does not resume, send, execute, or accept the original task. The
caller must verify the semantic mapping against original authorization; the API
cannot infer equivalent permission from free text. Sending also rechecks the
exact issue/version/authorization binding and current evidence after preparation
and after waiting for the original desktop owner, before native side effects.


## Source-bound result acceptance

New Codex execution registrations require a version-1 requirements contract,
including native source hashes, mandatory conditions and file/JSON/reviewer
checks. The native source binding identifies the message; it does not prove
semantic comprehension or confer permission. Other platforms and historical
rows retain compatibility without retroactive goal certification.

The existing audit binds source coverage, actual outcomes and the whole final
response in one review. Missing or changed outputs invalidate the goal result.
Registered issue completion is reported separately from goal completion: the former only means no registered item remains open in the permitted scope, even without an audit. Goal completion still requires an effective audit and the original independent-review condition. Review
stages distinguish content, installation, runtime and natural observation.
Optional improvements do not extend the original work or prevent completion.

Explicit continuation keeps its original lineage, source requirements and
unresolved findings. Known semantic omissions retain their first identity and
can only resolve against a newly added mandatory item with the same condition
and source, or a later reviewed user removal. Audit commit rechecks both the
contract and omission history; no new background, secondary ledger or model
service is introduced. Existing cancellation, real choices, scope checks and
one corrective Stop remain unchanged. Hook metadata gaps and semantic review
fallibility remain explicit limitations.

Observation freshness is part of each time-sensitive requirement. A `freshness` rule binds a scope-checked JSON receipt path, its original `observed_at_key`, and `expires_at_key` and/or positive finite `max_age_seconds`. Runtime/natural review outcomes must carry it; file/JSON outcomes may use it when their original goal concerns current state. Audit time and file mtime never refresh an old observation. Every review and status checks expiry, rejects missing/non-finite/future timestamps, and binds the receipt bytes. New observation bytes need re-review; extending/removing the contract limit is a weakened requirement. Static artifacts retain content/version checks without artificial expiry.

The existing review event retains the earliest accepted deadline for each source-bound observation. Editing only its expiry cannot renew it, including after a waiting review or continuation. History is rechecked atomically before acceptance. Both result and freshness receipt paths pass the same source-retirement and temporary-access checks before any observation is read.
