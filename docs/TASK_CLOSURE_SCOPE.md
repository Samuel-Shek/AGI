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
