---
feature: status_repair
domain: system
one_liner: "Background sweep that finds database rows stuck in a transitional status after a crash and repairs them only once live evidence confirms they are actually dead."
docs:
  tech: status_repair_tech.md
---
# System Status Repair Scheduler

## Purpose

Automatically reconciles database rows across four domains — agent environments, sessions, input tasks, and channel turn deliveries — that got stuck in a **transitional status** because the background task that owned them was killed before it could finish. Without this, a stuck row is invisible to every other scheduler, blocks the user's own way out (e.g. rebuild is refused while an environment is transitional), and in one case (channel turn deliveries) actively corrupts an unrelated later operation.

## Core Concepts

- **Transitional status** — a status value that means "a background operation currently owns this row and will clear it when it finishes": `AgentEnvironment.status` in `creating` / `building` / `rebuilding` / `starting` / `activating`; `Session.interaction_status` in `running` / `pending_stream`; `InputTask.status = in_progress`; `ChannelTurnDelivery.role = draft`.
- **Why rows get abandoned** — long operations run fire-and-forget (`create_task_with_error_logging`), never awaited by the original request, and clear the transitional status on completion. When the backend process goes away mid-operation (a dev `--reload`, a deploy `SIGTERM`), that task is cancelled with `asyncio.CancelledError` — a `BaseException`, not an `Exception` — so every `except Exception` fallback that would have written a terminal status (`error`, cleared interaction status, etc.) is skipped entirely. The row keeps its transitional value forever. Observed live on 2026-09-01: an environment stuck at `activating` while its container was already up and healthy.
- **The status-repair sweep** — a scheduled job that runs every 2 minutes, looks for rows that have been transitional for longer than a per-domain threshold, and repairs them. Single-leader across backend workers via a Postgres advisory lock, so only one worker acts per tick.
- **Five ordered passes, one sweep** — Pass A (environments), Pass B (sessions), Pass B2 (orphaned streams — agent turns whose backend process is gone), Pass C (input tasks), Pass D (channel turn deliveries). A failure in one pass is logged and does not block the others.
- **Stream heartbeat.** A live turn writes a `stream_heartbeat_at` timestamp roughly every 30 s into the metadata of both the in-progress agent message and its session (`services/sessions/stream_heartbeat.py`), on whichever backend worker is running it. Pass B2 reads that timestamp back: older than the minimum age → the writer is gone, reap; fresher → still alive (possibly on a sibling worker), leave it; missing (a legacy row written before the heartbeat existed) → fall back to Pass B's much longer age bound instead of being reaped early. Because the heartbeat is DB state rather than in-process memory, the verdict holds across every worker. This lets a crashed turn be sealed in a couple of minutes — see [A2A Protocol / Crash Recovery](../../application/a2a_integration/a2a_protocol/a2a_protocol.md#crash-recovery) for the client-facing side of the same mechanism.
- **Golden rule: verify, then repair.** A threshold only decides when a row is worth *looking at*. What gets written is decided by live evidence — is the container actually running, is the session actually streaming — never by age alone. A row that looks stuck may just be a slow, legitimate operation.
- **Liveness heartbeat, not a start time.** Environment repair depends on `AgentEnvironment.status_changed_at`, which is stamped on *every* lifecycle status write and every intermediate progress message ("Building template image...", "Installing custom packages...", "Syncing credentials..."). This makes the column read "no progress for N minutes," not "started N minutes ago" — a slow-but-alive build keeps pushing the clock forward and is never reaped, while a genuinely dead operation goes silent, because the write and the work are literally the same thread of execution. A dead operation cannot fake a heartbeat.
- **Claim token.** Because the heartbeat is stamped unconditionally (even when a status is re-asserted — e.g. a user manually retries an activation that is already stuck), a status *value* alone is not proof a row is still the same abandoned operation. Every pass therefore re-verifies a `(status, timestamp)` pair immediately before writing, so a row re-claimed by a fresh, legitimate operation between the read and the write is left alone.
- **Cross-pass hand-off, in memory only.** Passes within one sweep tick share a scratchpad recording what earlier passes just did. This exists because two of this sweep's own repairs manufacture evidence a later pass would otherwise misread as "abandoned" — see Business Rules.

## User Stories / Flows

1. **Environment stuck after a backend restart mid-activation.** A user clicks "Activate," the backend restarts seconds later, and the environment is left showing "Starting container..." forever even though the container comes up fine. Within the 10-minute activation threshold, the sweep asks the container adapter directly: if it reports running and healthy, the environment is moved to `running` (after best-effort re-syncing prompts/credentials/plugins the crash skipped) and an activation event fires so any messages parked waiting for it are delivered. If the container is confirmed gone, the environment moves to `error` with an explanatory message so the user can retry.
2. **A build is interrupted.** An environment stuck `building`/`creating`/`rebuilding` gets a much wider 60-minute window (image builds are legitimately slow). Past that, the same running/gone logic applies; a half-finished build is never resumed — the environment goes to `error` and the user re-triggers the build, which the stuck status was blocking in the first place.
3. **Chat shows a permanent "sending..." spinner.** A stream that never received a terminal event (completed/interrupted/error) leaves a session showing as actively streaming forever. A fresh stream heartbeat keeps it alive past the 120-minute default — a legitimate multi-hour agent turn is never reaped mid-flight — but only up to a 12-hour hard cap: a stream whose environment connection hangs without ever closing keeps beating with no real progress, and past 12 hours the sweep clears the session regardless (the agent row itself stays `streaming_in_progress` until that turn actually ends).
4. **A message is stuck "waiting for the environment."** A session parked waiting for its environment to come up, whose activation was lost, is examined after 15 minutes. If the environment is now actually running and the parked message is recent enough (see thresholds below), the sweep quietly re-delivers it — the same delivery the lost activation event would have made, just late. If the environment isn't up, or the message is stale, or a resend was already tried once for this exact message, the session is cleared to idle with the message left visibly `pending` and recoverable via manual session recovery — the platform never silently fires off an hours-old instruction.
5. **A task board entry is frozen at "In Progress."** A task's execution state is derived from its sessions' states, not owned directly. After Pass B repairs those session states, a task still marked `in_progress` 30 minutes after its last execution start is re-evaluated against the platform's existing derivation logic and moved to whatever it should now be (completed, blocked, error) — recorded as a normal status change with full history.
6. **A Google Chat thread is stuck saying "working…"** A channel conversation whose backend turn died leaves an unfinished, unsealed delivery record and (usually) a stale "working…" progress message in the external thread. After 60 minutes with no live evidence the turn is still going, the sweep seals the record and rewrites the external notice to "This turn was interrupted and did not finish." — never deleted (a deleted message becomes a confusing "message removed" tombstone in the thread).
7. **An A2A client polls `tasks/get` after a backend crash and gets "working" forever.** The backend process that owned the turn died mid-stream; nothing is left to ever write a terminal status. Because the turn was beating a heartbeat into the agent message and session every 30 s, the sweep recognizes it as stale within a couple of minutes (rather than waiting for Pass B's 120-minute blanket age bound) and seals the message `aborted` and the session back to idle — `tasks/get` then reports `failed`, and the client's poll loop ends instead of running forever. A turn that died before it wrote any agent message (the agent message is created on the turn's first text, thinking or tool event) has nothing to seal; clearing its session also marks the turn's delivered user message as cut off, so `tasks/get` reports `failed` for it too, never `completed`.

## Business Rules

### Per-pass thresholds (defaults)

| Pass | Row / condition | Looks after | Live evidence checked | Outcome |
|---|---|---|---|---|
| A — environments | `activating`, `starting` | 10 min quiet | container adapter probe (running+healthy / stopped / inconclusive) | `running` (+ best-effort resync) or `error` |
| A — environments | `creating`, `building`, `rebuilding` | 60 min quiet | same probe, plus a veto-only check for an in-flight image build | `running` or `error` |
| B — sessions | `interaction_status = running` | 120 min since stream start | a fresh DB stream heartbeat keeps it alive past 120 min, up to a 12 h hard cap | cleared to idle |
| B — sessions | `interaction_status = pending_stream` | 15 min since last claim | environment status + age of the oldest undelivered message | re-delivered once, or cleared to idle |
| B2 — orphaned streams | agent message `streaming_in_progress`, or session `interaction_status = running` | 2 min since the last stream heartbeat; no heartbeat (legacy row) falls back to B's 120 min | the DB stream heartbeat (stale / fresh / missing — see Core Concepts) | message sealed `aborted`; session cleared to idle (a turn with no agent message gets its user message marked cut off) |
| C — input tasks | `status = in_progress` | 30 min since execution started | re-derives from the (now-repaired) session states | re-derived status, with full history |
| D — channel deliveries | `role = draft` | 60 min since last write | the bound session's interaction status + a process-local live-relay check | sealed + marked diverged; external notice settled |

An unreadable/inconclusive environment probe is **never** treated as evidence of death on its own — the row is simply left for the next tick. Only if a row stays quiet for **6× its own threshold** while the probe keeps coming back inconclusive does the sweep finally treat the operation as lost (a container that is permanently up but never healthy — e.g. after an auth-token rotation the crash missed — would otherwise stay transitional, and blocking the user's rebuild forever is not actually the safe choice it looks like).

### Ordering is load-bearing — but not for the obvious reason

Environments (A) are repaired before sessions (B), sessions before orphaned streams (B2), and B2 before both input tasks (C) and channel deliveries (D). For C this is a real hand-off: it re-derives from session rows B (and now B2) have just committed, so running it first would derive from stale data. B2 sits directly after B and before C for the same reason — it seals sessions that never even reached B's simple age bound, and C's re-derivation needs to see the result.

For A→B and B→D it is the opposite of a hand-off — it exists because the *effect* of an earlier pass's repair is **not yet visible** to the next one:
- Repairing an environment to `running` fires an activation event that drains its parked sessions, but that drain happens on a detached background task and does not write anything to the session row for several steps — so moments after Pass A's repair, an affected session still looks exactly as abandoned as it did before. Pass A records which environments it just drained so Pass B can recognize and skip their sessions rather than acting on them a second time.
- Pass B can clear a very long-running session without cancelling the underlying stream (the threshold is the safeguard, not proof the stream stopped). Pass D uses that same "not running" signal as its evidence that a channel turn is over — so a turn Pass B just cleared (but which is still actually writing) would be wrongly sealed by Pass D in the same tick. Pass B records every session it touched so Pass D leaves those alone until the next tick.

### The resend window is deliberately narrow, and measured on the message, not the retry

When a parked session's environment comes back up, the sweep will re-enter delivery for it — but only if the *oldest undelivered message* is no more than 2× the pending-stream threshold old (15–30 minutes at defaults), and only once per stuck episode. Delivering a message a few minutes late is a reasonable recovery; delivering an hours-old instruction as a surprise action against a live agent is judged worse than leaving a visibly stuck, user-recoverable message. Past the window — or after one resend attempt that didn't resolve the episode — the session is cleared to idle instead, with its message(s) left `pending`.

### Channel deliveries get two status writes, and both matter

A reaped `draft` delivery is marked **both** sealed and diverged, not just sealed. It has to stop looking like the most recent unfinished turn (which the platform's own "adopt the next completion into the oldest open draft" logic would otherwise wrongly attribute a *later*, unrelated turn's answer to), and it has to stop looking like a normal delivered message subject to the platform's answer-divergence check (which would otherwise flag a false mismatch against the reaped content). One write closes each hole.

### Known behavioral exposure (by design, not a bug)

A task a human manually set to `in_progress`, whose connected sessions are simply sitting idle rather than actively streaming, will be re-derived and can move to a completed state after 30 minutes with no explicit trigger from any stream event. This is the platform's own existing status-derivation logic — the sweep adds no new judgment, it just re-runs it on a schedule — but it is a real status change on a human-owned row. It is always recorded with full history and a system-authored reason, so it is visible and explicable, never silent.

### What is intentionally not covered

- A cold, multi-worker-only hazard: a very long image build produces no heartbeat for its whole duration (the one operation the heartbeat design cannot see into), and the safeguard for it (an in-flight-build check) only works within a single worker process. This is currently safe only because the platform is deployed with a single backend worker — see the tech doc's multi-worker caveat for the reasoning behind not adding a startup guard.
- Several less-common paths (a build-in-flight veto, notice settlement, a mid-probe re-claim race, a task with zero connected sessions, and a live-relay veto) are implemented and reasoned about but not yet covered by automated tests — see the tech doc's Test Surface section.

## Architecture Overview

```
Backend restart / crash mid-operation
        │  (asyncio.CancelledError skips every except-Exception fallback)
        ▼
Row stays transitional forever ──────────────► invisible to every other
  AgentEnvironment.status                       scheduler, and blocks the
  Session.interaction_status                    user's own way out (e.g.
  InputTask.status                               rebuild refused)
  ChannelTurnDelivery.role
        │
        │  every 2 minutes, one leader worker (Postgres advisory lock)
        ▼
┌─────────────────────────── one sweep tick ────────────────────────────┐
│  Pass A: environments   → probes the container, verify-then-repair     │
│           │  records which environments it drained                    │
│           ▼                                                            │
│  Pass B: sessions       → skips sessions Pass A just drained;          │
│           │               clears stale streams / re-enters delivery    │
│           │  records which sessions it touched                        │
│           ▼                                                            │
│  Pass B2: orphaned streams → DB stream heartbeat says the writer is    │
│           │                  gone — seals sooner than B's 120-min      │
│           │                  bound; missing heartbeat falls back       │
│           │  records which sessions it cleared, same as B             │
│           ▼                                        ▼                  │
│  Pass C: input tasks    Pass D: channel deliveries (skips sessions     │
│  (re-derives from        Pass B / B2 just touched; seals abandoned     │
│   the now-repaired        drafts; settles the external "working…"     │
│   session states)         notice)                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Integration Points

- **[Agent Environments](../../agents/agent_environments/agent_environments.md)** — Pass A repairs `AgentEnvironment.status`; reuses the lifecycle's own bring-up path (`_sync_dynamic_data`) so a rescued environment isn't just marked running but actually resynced.
- **[Agent Environment Critical State](../../agents/agent_environments/agent_env_critical_state.md)** — a distinct mechanism for a different failure shape (container up, a provisioning step failed) that coexists with `status="running"`; the status-repair sweep only ever touches rows still in a *transitional* status and never interacts with `critical_state`.
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Pass B repairs `Session.interaction_status` and re-derives `pending_messages_count`; reuses the platform's own session-clearing and message-delivery entry points rather than writing session state directly. Pass B2 seals an agent message left `streaming_in_progress` by a dead process to `status="aborted"`, the same status a live process seals on an unrequested cancel.
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — Pass B2 is the backstop of A2A crash recovery: a turn whose backend process died leaves `tasks/get` reporting `working` until this pass's stream-heartbeat evidence lets it seal the row and the session, after which polling clients see `failed`.
- **[Input Tasks](../../application/input_tasks/input_tasks.md)** — Pass C re-derives a task's execution status using the platform's own derivation logic and records every change through the normal status-history mechanism, exactly like a user- or event-triggered status change.
- **[Server Channels](../../application/server_channels/server_channels.md)** — Pass D repairs abandoned `ChannelTurnDelivery` draft rows and settles the external channel's stale progress notice, protecting the channel's own turn-adoption and answer-divergence logic from being fed a lost turn's leftovers.
