# System Status Repair Scheduler — Plan

**Status:** shipped (all 6 phases complete, uncommitted in the working tree as of 2026-09-05). See [status_repair.md](../system/status_repair/status_repair.md) and [status_repair_tech.md](../system/status_repair/status_repair_tech.md) for the as-built feature docs — this plan is kept as the historical design record and updated below only where the shipped behavior meaningfully diverged from what was originally proposed.
**Created:** 2026-09-05
**Motivation:** transitional statuses are set before long-running background tasks and only cleared on task completion. The backend process dying mid-task (dev `--reload`, deploy SIGTERM) cancels the task via `asyncio.CancelledError`, which is a `BaseException` — so every `except Exception` error-fallback in the lifecycle is skipped and the row is left in its transitional state forever. Observed live 2026-09-01: env stuck at `activating` / "Starting container..." while its container was up and healthy (a hot-reload fired 0.6 s after `docker compose up -d`).

## 1. Root cause (shared across all domains)

- Long operations run under `create_task_with_error_logging` (fire-and-forget), never awaited by the request.
- All error handlers are `except Exception` (24 occurrences in `environment_lifecycle.py` alone, zero `except BaseException` / `CancelledError` anywhere) → task cancellation bypasses the `status = "error"` fallback.
- Nothing in the codebase queries for *transitional status + age*. The existing `environment_status_scheduler` only selects `status == "running"` — stuck envs are invisible to it (and to the suspension scheduler, which has the same filter).

The cron below is the systemic mitigation. A complementary (later) hardening: wrap lifecycle status-writes in `finally:` blocks so cancellation itself records a terminal state — reduces incidence but can never fully solve it (SIGKILL), so the reconciler is needed regardless.

## 2. Inventory of stuck-able statuses (priority order)

| # | Field | Table | Transitional values | Today's reconciler | Staleness signal |
|---|-------|-------|--------------------|--------------------|------------------|
| 1 | `AgentEnvironment.status` | `agent_environment` | `creating`, `building`, `starting`, `activating`, `rebuilding` | **none** | ⚠️ **none usable** — `updated_at` has no `onupdate` and the lifecycle never bumps it (0 writes in `environment_lifecycle.py`) |
| 2 | `Session.interaction_status = "pending_stream"` | `session` | waiting for `ENVIRONMENT_ACTIVATED` event; **event-only, no poller** — direct cascade of #1 | none | `updated_at` ✓ |
| 3 | `Session.interaction_status = "running"` | `session` | stream died before terminal event | defensive `clear_interaction_status()` helper exists (`session_service.py:433`) but is caller-invoked only | `streaming_started_at` ✓ (purpose-built: set at `session_service.py:916`, nulled at every clear site) |
| 4 | `SessionMessage.sent_to_agent_status = "pending"` | `message` | committed but never delivered to agent-env | manual `/session-recover` only | `Message.timestamp` ✓ |
| 5 | `InputTask.status = "in_progress"` | `input_task` | derived from #2/#3 (`input_task_service.py:1264-1344`) — two-level cascade env → session → task | none | `executed_at` ✓ + `TaskStatusHistory` |
| 6 | `ChannelTurnDelivery.role = "draft"` | `channel_turn_delivery` | streaming relay row never sealed/finalized | none | `updated_at` ✓ |
| 7 | `Session.pending_messages_count` | `session` | drifts from actual `pending` row count when a stream dies | none | recomputable from `message` |

Not stuck-prone (verified, leave alone): `critical_state` (terminal outcome, has `critical_since` + scheduler clears it), `Agent.pending_update` (stamp-attempt-before-work + backoff — **the reference crash-safe pattern**, `install_service.py:1537-1541`), `ChannelThreadBinding.pending_install` (already swept every 45 s), `FileUpload.temporary` (24 h GC), all expiry-based auth flows, `AIKnowledgeGitRepo.pending` (resting state), `InputTask.refining` / improvement-request `in_progress` (human-driven), `Session.status="active"` (resting default — do **not** touch it, only `interaction_status`).

Dead value: `initializing` has no writer anywhere — drop it from the model comment when touching the file.

## 3. Design

### One scheduler, per-domain repair passes

New `backend/app/services/system/status_repair_scheduler.py` (APScheduler `BackgroundScheduler`, same start/shutdown pair registered in `main.py` lifespan behind the `TESTING` gate as the other 17). Interval: **2 min**. Each pass is an independent function; a failure in one pass is logged and never blocks the others. Every repaired row gets one log line + the normal WS event fanout so open UIs update.

**Leader lock:** copy `install_service.sweep_leader_session` (`install_service.py:100-141`) — lock pinned to an explicit `engine.connect()` so mid-batch commits don't return the connection and strand the lock. **Do NOT copy `model_discovery_scheduler`** — its advisory lock leaks on pooled connections and the job permanently disables itself (two other schedulers already cite it as the pattern; don't make it four).

**Async bridge:** repairs that must spawn work outliving the tick (re-drain a session) use the `run_coroutine_threadsafe`-onto-captured-main-loop idiom from `channel_pending_scheduler.py`; pure DB repairs use `asyncio.run()` like `environment_status_scheduler`.

**Golden rule: verify, then repair.** Never flip a status on age alone when live evidence is checkable — a signal that pattern-matches "stuck" may be a genuinely slow operation.

### Pass A — environments (needs a migration first)

Prerequisite: **add `AgentEnvironment.status_changed_at: datetime`** + a central `_set_status(env, status, message)` helper in `environment_lifecycle.py` that stamps it; convert the ~15 direct `env.status = ...` writes to use it. Without this there is no honest staleness signal (`last_activity_at` is bumped by usage-intent, `last_health_check` only on success — both lie). Backfill: `now()` in the migration.

Then, for rows in a transitional status older than the threshold:

| Status | Threshold | Evidence check → repair |
|---|---|---|
| `activating`, `starting` | 10 min | ask adapter: container **running + healthy** → best-effort `_sync_dynamic_data` (it was skipped by the crash — this exact gap bit us on 2026-09-01), then `status="running"`. Container stopped/absent → `status="error"`, `status_message="activation lost (reconciled by status repair)"` |
| `creating`, `building`, `rebuilding` | 60 min (builds are legitimately long) | container running + healthy → `running`; otherwise `error` — a half-finished build is not resumable, the user re-triggers rebuild (which the stuck status was blocking: `admin_environment_service.py:45-53`, `environment_service.py:900`, auto-update allowlist `install_service.py:76`) |

**As shipped, this table's "otherwise error" is not literal age-plus-absence — it is gated on the heartbeat, and the container probe's "otherwise" branch is narrower than it reads above.** The naive reading ("container not running + healthy → error") would reap a *live* build: a legitimately in-progress `docker build` has no container running at all for most of its duration, since the image doesn't exist yet to start a container from. What actually ships:

1. **The threshold is measured against `status_changed_at` as a liveness heartbeat, not an elapsed-since-start clock.** `_touch_progress` stamps it on every intermediate `status_message` write ("Building template image...", "Installing custom packages...", ...), so "60 minutes" means 60 minutes with *no lifecycle progress at all*, not "started 60 minutes ago." A slow-but-alive build keeps resetting its own clock and is never a candidate.
2. **The container probe only fires once the heartbeat has already gone quiet past the threshold**, and even then it is three-way, not two-way: `running` (repair to `running`), `stopped`/absent (repair to `error`), or **inconclusive** (still booting, up-but-unhealthy, Docker daemon unreachable) — and inconclusive licenses no write at all, on any tick short of a 6× escalation window (`_INCONCLUSIVE_ESCALATION_FACTOR`). This exists because "no container yet" during a build is expected, not evidence of death, and it is the heartbeat — not the probe — that actually detects a dead build.
3. **A veto-only in-flight-build check** (`template_image_service.is_build_in_flight`) additionally defers any build-status candidate whose image build this same worker process knows is still running, closing the one gap the heartbeat itself cannot see into: a cold `docker build` legitimately writes no `status_message` for its entire duration. This check is process-local (see §6 below for the multi-worker caveat it does not close).

See [status_repair_tech.md](../system/status_repair/status_repair_tech.md#pass-a--environments-status_repair_environmentspy) for the full mechanics.

Emit `ENVIRONMENT_ACTIVATED` when repairing to `running` — that is what drains `pending_stream` sessions (`session_service.py:2410`), so Pass A fixing an env automatically un-wedges its sessions the normal way.

### Pass B — sessions

1. **`interaction_status="running"`** with `streaming_started_at` older than `STREAM_MAX_AGE` (default **120 min** — agent turns can legitimately run long; configurable in settings): call the existing `SessionService.clear_interaction_status(session_id, reason="stale stream reconciled")`. It is idempotent, nulls `streaming_started_at`, and emits both WS events. Leave `Session.status` alone.
2. **`interaction_status="pending_stream"`** with `updated_at` older than **15 min**: look at the env.
   - Env `running` **and** the oldest pending message is still within the resend window **and** this exact episode has not already been resent → re-enter the drain (same path as `handle_environment_activated`) so the pending messages actually go out.
   - Otherwise → clear `interaction_status` to `""`, keep the messages `pending` (visible in UI, recoverable via `/session-recover`). **Deliberately do not auto-resend hours-old messages to an agent** — surprise-executing a stale instruction is worse than a visible stuck message. *(Decision point — see §5; resolved as shipped, below.)*

**As shipped (resolving §5.1): the resend window is `[15, 30)` minutes, measured against the oldest pending message's own timestamp, and capped at one resend per stuck episode.** Three conditions all gate the re-entry (`status_repair_sessions.py:_resend_is_due`): the environment is `running`; the oldest undelivered user message is younger than `STATUS_REPAIR_PENDING_STREAM_MAX_AGE_MINUTES * 2` (`_RESEND_MAX_AGE_FACTOR = 2`, i.e. under 30 minutes at the default 15-minute threshold); and this pass has not already resent for this exact episode. The "already resent" check matters on its own: nothing else on the resend path writes the session row for the length of an entire `initiate_stream` call (an OAuth credential refresh, a credentials sync, a possible title-generation LLM call), so without an explicit claim the sweep would re-spawn a drain every tick, forever, for a row going nowhere. The claim is `session_metadata["status_repair_resent_for"] = <oldest_pending_message_timestamp>` — deliberately keyed by the message's own timestamp rather than a bare boolean, so a genuinely new stuck episode (a newer oldest-pending message) does not match the stale marker and still gets its own one resend. Deliberately **not** measured against the claim stamp itself (`updated_at`), which the resend action also bumps — a window measured against a stamp the action itself resets could never expire. Past the window, or after the one resend attempt, the session is cleared to idle as this section originally specified. See [status_repair_tech.md](../system/status_repair/status_repair_tech.md#pass-b--sessions-status_repair_sessionspy) for the full mechanics.
3. **`pending_messages_count` recompute** from `count(message where sent_to_agent_status='pending')` whenever a session row is touched by 1/2.

### Pass C — input tasks

For `status="in_progress"` where `executed_at` older than 30 min: re-derive via the existing derivation logic (`input_task_service.py:1264`) from the now-repaired session states; record the transition in `TaskStatusHistory` like any other change. Run after Pass B in the same tick.

### Pass D — channel turn deliveries

`role="draft"` older than 60 min → mark `status="diverged"` (or seal), so the relay bookkeeping stops considering the turn live. Also clear a stale `ChannelThreadBinding.status_message_id` progress notice if its turn is being reaped (a dead stream leaves a stale "working…" message in the external channel).

### Self-safety of the repairer

Every repair is a single-row read → verify → write in its own transaction, idempotent by construction (re-checks the status under the write). No stamp-before-work needed. Thresholds live in `config.py` settings with the defaults above.

## 4. Phases

1. **Migration + `_set_status`** — `status_changed_at` column, central setter, drop dead `initializing` from the comment.
2. **Scheduler skeleton** — file, leader lock (copied from `sweep_leader_session`), registration in `main.py`, TESTING gate, settings for thresholds.
3. **Pass A** (environments) — the incident class we actually hit.
4. **Pass B + C** (sessions + tasks).
5. **Pass D** (channel deliveries).
6. **Tests** — per `backend/tests/README.md`; the TESTING gate means repair functions must be directly invocable for tests (like the other schedulers' `_check_*` funcs).

## 5. Open decision points

1. **Auto-resend on `pending_stream` repair when env is running** (§3 Pass B.2): plan says yes if the env is up (it's the same drain the activation event would have run), no re-send when we had to clear to idle. Confirm the comfort level with a message being delivered up to ~15 min late.
2. **`STREAM_MAX_AGE` default** — 120 min proposed; long agent runs must never be reaped mid-flight. Could be per-agent later.
3. Repair-to-`error` vs repair-to-`stopped` for dead `creating`/`building` — `error` chosen for visibility.

## 6. Side findings from the sweep (out of scope here, worth tickets)

- `model_discovery_scheduler.py:53-70` advisory lock leak is **live** (known from earlier work; two schedulers cite it as the pattern to copy).
- `email/sending_scheduler.py` has no leader lock and no `sending` claim state → duplicate-send risk under multi-worker.
- `AppSyncPairing` has documented expiry but **no cleanup scheduler registered** — read-time enforcement only.
- Lifecycle hardening: `finally`-based terminal-status writes in `environment_lifecycle.py` would shrink the stuck window at the source.
- **Multi-worker hazard in Pass A's own build-in-flight veto — found during this build, safe today only by a pinned config value.** Pass A's heartbeat (`status_changed_at`) cannot see into a `docker build`'s own duration — nothing is written while it runs — and the veto that fills that gap (`template_image_service.is_build_in_flight`) is a **process-local** in-memory registry. In a deployment with more than one backend worker process, a build kicked off on worker 1 is invisible to worker 2; if worker 2 happens to hold the leader lock when a cold template build has been running for more than `STATUS_REPAIR_ENV_BUILDING_MAX_AGE_MINUTES` (60 min default), that worker sees no heartbeat, no in-flight marker it can see, an ambiguous-to-absent container (the image isn't built yet), and — once the container probe returns "gone" rather than merely inconclusive — reaps a build that is still legitimately running. This is exactly the "container gone" repair-to-`error` path in the table above, applied to a false positive.

  **Why this is safe today:** `docker-compose.yml` pins `command: ["fastapi", "run", "--workers", "1", "app/main.py"]`, so there is only ever one worker process and the veto's process-local registry is complete. `backend/Dockerfile`, however, defaults to `CMD ["fastapi", "run", "--workers", "4", "app/main.py"]` — one config change (removing or not carrying forward the compose override) away from this being a real multi-worker exposure, not merely a theoretical one.

  **Why no startup guard was added to catch a misconfiguration:** `--workers` is a CLI argument to `fastapi run`/`uvicorn`, and uvicorn's multiprocessing spawn model means the worker count is not introspectable from inside a running worker process — there is no in-process API that reports "how many siblings does this process have." `WEB_CONCURRENCY` is only consulted by uvicorn when `--workers` is *absent* from the command line, so an env-var-based check inside the app would read (and happily report) a reassuring "1 worker" in precisely the 4-worker case that matters, which is worse than no check at all. Closing this for real needs either a build registry that is not process-local (e.g. a DB row) or a genuine multi-worker liveness signal for `docker build`, both bigger than this sweep's scope.
