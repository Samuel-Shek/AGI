# Bounded schedule amendment protocol

This optional operator integration changes only the necessary next trigger of an
existing task. It does not create a new task or grant permission to change its
owner, prompt, model, budget, notification policy, or acceptance criteria.

A local operator must supply the task registration and its original authority.
Prepare binds the current sources, exact task, authorized time window, and main
controller state. The final check and append share the existing control gate.
Pause, stop, capability withdrawal, source drift, or an expired time window must
reject the append. External scheduler calls stay outside that gate.

After the one authorized scheduling call, read its original receipt and current
next trigger before committing the local result. Unknown outcomes must be
reconciled using that same call, never replayed.

No personal task registrations or prior deployment acceptance results are
shipped here. The public source does not pre-authorize a scheduler or task.
