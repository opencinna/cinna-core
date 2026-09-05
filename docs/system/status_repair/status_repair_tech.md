# System Status Repair Scheduler — Technical Reference

See [status_repair.md](status_repair.md) for the business logic, per-pass thresholds, and ordering rationale. This file covers implementation.

## File Locations

### Backend — Services

- `backend/app/services/system/status_repair_scheduler.py` — `BackgroundScheduler` setup, leader-lock context manager, main-loop bridge, pass registry, `start_scheduler()` / `shutdown_scheduler()`.
- `backend/app/services/system/status_repair_context.py` — `RepairContext` dataclass, the per-tick cross-pass scratchpad.
- `backend/app/services/system/status_repair_environments.py` — Pass A.
- `backend/app/services/system/status_repair_sessions.py` — Pass B.
- `backend/app/services/system/status_repair_tasks.py` — Pass C.
- `backend/app/services/system/status_repair_channels.py` — Pass D.
- `backend/app/services/system/__init__.py` — empty package marker.

### Backend — Models & existing services touched

- `backend/app/models/environments/environment.py` — `AgentEnvironment.status_changed_at` column (added by this feature).
- `backend/app/services/environments/environment_lifecycle.py:_set_status()` and `:_touch_progress()` — the sole writers of `AgentEnvironment.status` / `status_message`; both stamp `status_changed_at` unconditionally. `_set_status` was pre-existing but is now the enforced single writer; `_touch_progress` is new, and every direct `status_message = ...` write in the lifecycle (~20 sites) was converted to call it.
- `backend/app/services/environments/admin_environment_service.py` — `_TRANSITIONAL_STATUSES`, the pre-existing frozenset of transitional status values, imported (not re-derived) by Pass A.
- `backend/app/services/environments/template_image_service.py:is_build_in_flight()` — process-local, veto-only check Pass A uses to avoid reaping a cold `docker build` that has written no heartbeat.
- `backend/app/services/sessions/session_service.py` — `SessionService.clear_interaction_status()` / `initiate_stream()` — reused, not reimplemented, by Pass B.
- `backend/app/services/tasks/input_task_service.py` — `InputTaskService.compute_status_from_sessions()` / `update_task_status()` — reused by Pass C.
- `backend/app/services/server_channels/channel_outbound_service.py:_binding_thread_key()` / `ChannelOutboundService.set_status()` — reused by Pass D to resolve and rewrite the external progress notice.
- `backend/app/services/server_channels/channel_stream_relay.py` — `ChannelStreamRegistry`, reused by Pass D's process-local live-relay veto.

### Configuration

- `backend/app/core/config.py` — `STATUS_REPAIR_*` settings block.

### Wiring

- `backend/app/main.py` — imports `start_scheduler`/`shutdown_scheduler` as `start_status_repair_scheduler`/`shutdown_status_repair_scheduler`; called in the `lifespan` startup/shutdown blocks alongside the platform's other 17 schedulers, behind `if not settings.TESTING`.

### Migration

- `backend/app/alembic/versions/68aab27946e5_add_agent_environment_status_changed_at.py` — `down_revision = 'c9a2f5b1d604'`.

### Tests

- `backend/tests/api/agents/sessions/agents_status_repair_test.py` — Pass A, B, and the B→C hand-off.
- `backend/tests/api/server_channels/server_channels_status_repair_test.py` — Pass D and the B→D hand-off.
- `backend/tests/architecture/environment_status_writer_test.py` — AST-based drift test enforcing the single-writer invariant `_set_status`/`_touch_progress` depend on.

## Database Schema

### `agent_environment.status_changed_at`

Added by migration `68aab27946e5`. `TIMESTAMPTZ`, `NOT NULL`, `server_default=now()` (so existing rows backfill to deploy time rather than looking instantly stale). Written only by `_set_status()` and `_touch_progress()` in `environment_lifecycle.py` — enforced by `environment_status_writer_test.py`, which AST-scans `app/services` and `app/api` for any other assignment to `.status` / `.status_message` on a variable named like an environment (`environment`, `env`, `target_env`, `source_env`, `fresh_env`, `agent_env`, `agent_environment`) and fails if one is found outside a two-entry allowlist (the two writer functions, plus one documented value-copy site in `agent_status_service.py:_ensure_environment_running`).

No other schema changes. Sessions, input tasks, and channel turn deliveries are read/repaired through existing columns (`interaction_status` + `streaming_started_at`/`updated_at`; `status` + `updated_at`/`executed_at`; `role`/`status` + `updated_at`).

## The Scheduler & Leader Lock

`status_repair_scheduler.py` runs an APScheduler `BackgroundScheduler` on a fixed interval (`STATUS_REPAIR_INTERVAL_MINUTES`, default 2) with `max_instances=1`. Two independent gates control whether it starts at all: `main.py` only calls `start_status_repair_scheduler()` when `not settings.TESTING` (same gate as the platform's other 17 schedulers), and `start_scheduler()` itself additionally checks `settings.STATUS_REPAIR_ENABLED` (default `True`) and logs+returns if it is off.

**Leader lock** (`repair_leader_session()`, a context manager): copied from `install_service.sweep_leader_session`, deliberately **not** from `model_discovery_scheduler` (that one takes the advisory lock on a pooled `Session` and leaks it — a live bug two other schedulers already cite as the anti-pattern). The lock is taken on an explicit `engine.connect()` (`pg_try_advisory_lock`), never on a pooled `Session`, because the sweep commits once per repaired row; an engine-bound session would return its connection to the pool at every commit, stranding the lock on a connection the matching `pg_advisory_unlock` can no longer reach. Under `settings.TESTING` the context manager instead yields the patched test-transaction session directly (no advisory lock — no cross-process concurrency to guard in tests).

**Main-loop bridge.** `run_status_repair()` (the APScheduler job function, runs on a worker thread) submits `_run_repair_tick()` via `asyncio.run_coroutine_threadsafe()` onto the application's main event loop, captured at startup into the module-level `_main_loop`. This is required, not incidental: Pass A's repair emits `ENVIRONMENT_ACTIVATED`, and `event_service` dispatches handlers as `asyncio` tasks on whatever loop is *currently running* — under a throwaway `asyncio.run()` loop those tasks would be created and then killed the instant the sweep returns. Same idiom as `channel_pending_scheduler`. The worker thread blocks on `future.result(timeout=SWEEP_WAIT_TIMEOUT_SECONDS)` (120s constant, not tied to the tick interval, so a large `STATUS_REPAIR_INTERVAL_MINUTES` can never turn shutdown into a multi-hour hang). A timeout logs a warning and returns; the advisory lock (not `max_instances`) is what actually prevents two overlapping sweeps from double-repairing the same row.

**Shutdown** (`shutdown_scheduler()`) calls `scheduler.shutdown(wait=False)` — deliberately not waiting, since the worker thread can only unblock when its coroutine finishes on the main loop being shut down. In-flight repairs are safe to abandon: every repair commits per-row and re-checks its claim under the write, so a later process re-runs them idempotently.

**Pass registry.** `REPAIR_PASSES: list[tuple[str, RepairPass]]` is an explicit literal list of `(name, async_function)` pairs, populated by importing each pass function — never by passes self-registering on import, so ordering is never an artifact of import order. `run_repair_passes()` iterates it, catching and logging any pass's exception (then rolling back the shared session before continuing) so one broken domain never blocks the others.

## The `RepairContext` Seam

`status_repair_context.py` defines `RepairContext(session, drained_environment_ids, sessions_in_motion)` — a plain dataclass, created fresh per tick, discarded after it (never persisted, never carried across ticks). It exists to close two races that are otherwise invisible from inside the pass that would suffer them:

1. **Pass A → Pass B.** Pass A repairing an environment to `running` emits `ENVIRONMENT_ACTIVATED`, but `event_service._call_backend_handlers` dispatches handlers via `create_task_with_error_logging` — fire-and-forget — and even once `handle_environment_activated` runs, neither it nor `initiate_stream`'s env-running branch writes the session row; that only happens when the `STREAM_STARTED` handler lands several task hops later. Pass A calls `ctx.note_drained_environment(env_id)`; Pass B calls `ctx.is_environment_draining(env_id)` and skips (recording the session into `sessions_in_motion` too, so Pass D also knows to leave it) rather than acting on a session that merely hasn't been written yet.
2. **Pass B → Pass D.** Pass B can clear a long-running session's `interaction_status` *without* cancelling the stream behind it (the age threshold is the safeguard, not proof of death). Pass D reads that same column as its evidence a channel turn is over. Pass B calls `ctx.note_session_in_motion(session_id)` for every session it clears or re-drains; Pass D calls `ctx.is_session_in_motion(session_id)` and defers to the next tick rather than sealing a delivery whose "not running" evidence Pass B manufactured moments earlier in the same tick.

This is also the pass functions' entire test surface: because the `TESTING` gate keeps the scheduler itself from ever running under pytest, a test builds `RepairContext(session=db)` directly and awaits one pass against it.

## Pass A — Environments (`status_repair_environments.py`)

**Candidates.** All `AgentEnvironment` rows whose `status` is in `_TRANSITIONAL_STATUSES` (imported from `admin_environment_service`, not re-derived), loaded as plain `(id, status, status_changed_at)` tuples — a `_Claim` — never as ORM objects, so one broken row's identity-map object can't poison the next candidate.

**Threshold split.** `_ACTIVATION_STATUSES = {"activating", "starting"}` get `STATUS_REPAIR_ENV_ACTIVATING_MAX_AGE_MINUTES` (default 10); every other transitional status (`_BUILD_STATUSES`, defined as `_TRANSITIONAL_STATUSES - _ACTIVATION_STATUSES` so a status added later without updating this file lands in the conservative bucket) gets `STATUS_REPAIR_ENV_BUILDING_MAX_AGE_MINUTES` (default 60).

**Claim re-verification (`_claim_still_held`)** compares both `status` and `status_changed_at` against the original claim immediately before every write — status alone is insufficient because `_set_status` stamps unconditionally, so a user manually retrying a stuck `starting` environment re-claims it with the identical status string and a fresh timestamp.

**Build-in-flight veto.** For `_BUILD_STATUSES` candidates only, `template_image_service.is_build_in_flight(env_name)` is checked before probing; a positive match defers the row unconditionally. This is the documented, deliberate gap-filler for the one operation that writes no heartbeat during its own execution (`docker build`) — see the plan doc §6 for why this veto is process-local and therefore not a complete fix in a multi-worker deployment.

**Container probe (`_probe`)**, via `EnvironmentService.get_lifecycle_manager().get_adapter(environment).get_status()`, which already folds in the health check: `"running"` → licenses repair to `running`; `"stopped"` → licenses repair to `error`; anything else (still booting, up-but-unhealthy, daemon unreachable) → `_PROBE_INCONCLUSIVE`, which alone never licenses a write. An environment with no allocated port (`environment.config["port"] is None`) is treated as hard evidence of `_PROBE_GONE` without invoking the adapter at all (allocating one to probe would leak a port from the in-memory pool).

**Escalation.** `_INCONCLUSIVE_ESCALATION_FACTOR = 6` (a module constant, not a setting — a statement about the lifecycle's own behavior, not a tuning knob). A row that stays inconclusive past 6× its own threshold is treated as lost anyway (probe outcome forced to `_PROBE_GONE`) — deliberately, because leaving a transitional row forever is not the safe option it looks like: it is exactly what blocks the user's rebuild. This escalation can fire for every transitional row in the deployment on the same tick if the Docker daemon itself is unreachable for that long, which is accepted as a rare, self-explanatory event (logged per-row at `WARNING`).

**Repair to running (`_repair_to_running`)**: closes the leader session's read transaction, then runs `_resync_on_own_session()` — calling `EnvironmentLifecycleManager._sync_dynamic_data()` on its **own** fresh `create_session()`, not the sweep's pinned leader connection, because the resync is a multi-second-to-minutes container round-trip with its own progress commits. This is the "crash skipped this step" gap that caused the 2026-09-01 incident: an environment repaired to `running` without a resync would be up but running stale prompts/credentials. After the resync, the row is re-read, the claim is re-checked (status only, since the resync itself legitimately moved `status_changed_at`), and `_set_status(environment, "running", ...)` is written along with `last_health_check` **and** `last_activity_at` (both, deliberately — `environment_suspension_scheduler` selects on `status=="running"` and would otherwise suspend a rescued environment immediately, racing the drain the repair is about to trigger). `ENVIRONMENT_ACTIVATED` is then emitted (not a generic status-changed event) specifically because that is what `SessionService.handle_environment_activated` listens for to drain `pending_stream` sessions.

**Repair to error (`_repair_to_error`)**: `_set_status(environment, "error", message)` plus `environment.config["last_error"] = message` (the field every other lifecycle error path also populates, so the UI shows the repair's own message rather than a stale unrelated one), then emits `ENVIRONMENT_STATUS_CHANGED`.

## Pass B — Sessions (`status_repair_sessions.py`)

**Candidates.** `Session` rows with `interaction_status` in `{"running", "pending_stream"}`. Claim stamp is `streaming_started_at` for `running` (nulled at every clear site, so a session missing it never recorded a start and is skipped — no honest age to measure) and `updated_at` for `pending_stream` (bumped by every `update_interaction_status` write).

**`running` repair (B.1).** No environment probe — a stream older than `STATUS_REPAIR_STREAM_MAX_AGE_MINUTES` (default 120) is treated as over regardless of container state; the threshold itself is the safeguard against reaping a legitimate long turn. Repaired via `SessionService.clear_interaction_status(session_id, reason="reconciled by status repair")` (reused, not reimplemented — it is already idempotent, nulls `streaming_started_at`, and emits both websocket events an open chat window needs), followed by `_recount_pending()`.

**`pending_stream` repair (B.2).** Threshold `STATUS_REPAIR_PENDING_STREAM_MAX_AGE_MINUTES` (default 15). `_resend_is_due()` gates re-entry on three independent conditions: the bound environment's status is `"running"`; there is an oldest pending user message and it is younger than `_max_quiet_time(status) * _RESEND_MAX_AGE_FACTOR` (`_RESEND_MAX_AGE_FACTOR = 2`, a constant — at defaults this yields a `[15, 30)` minute resend window, deliberately expressed as a multiple of the threshold rather than a fixed number so raising the threshold cannot silently widen the window into hours); and this pass has not already resent for this exact episode. The window is measured against **the oldest pending message's own timestamp**, never the claim stamp — because `_stamp_resend_claim()` bumps the claim stamp (`updated_at`) as part of claiming the row, so a window measured against it could never expire.

If due: `_stamp_resend_claim()` (the sweep's one stamp-before-work site) bumps `updated_at` and writes `session_metadata["status_repair_resent_for"] = <oldest_pending_message_iso_timestamp>` — keyed by that timestamp specifically so a genuinely new stuck episode (a newer oldest-pending message) does not match the old marker and gets its own single resend without any explicit clearing. Then `_recount_pending()` and `_spawn_drain()` — the latter calls `create_task_with_error_logging(SessionService.initiate_stream(session_id=..., get_fresh_db_session=create_session), task_name=f"status_repair_drain_{session_id}")`, fire-and-forget on the main loop, exactly the shape `handle_environment_activated` uses.

If not due (env not running, nothing pending, past the window, or already resent this episode): cleared to idle via the same `_clear_to_idle()` path as B.1, leaving messages `pending` and recoverable via `/session-recover`.

**`_recount_pending()`** re-derives `pending_messages_count` from `count(message where role='user' and sent_to_agent_status='pending')` — the same predicate `MessageService.collect_pending_messages` uses — whenever B.1 or B.2 touches a row. Deliberately does **not** bump `updated_at` (that column is the `pending_stream` claim token; forging a heartbeat on a pure count-correction would hide the row from the next tick for no reason).

**Pass A skip.** For `pending_stream` claims, `ctx.is_environment_draining(env_id)` short-circuits the repair entirely (see RepairContext section above), recording the session into `sessions_in_motion` even though nothing was written, since Pass D still needs to know this pass looked at it.

## Pass C — Input Tasks (`status_repair_tasks.py`)

**Candidates.** `InputTask` rows with `status = IN_PROGRESS`, `executed_at IS NOT NULL`, and `executed_at < now - STATUS_REPAIR_TASK_MAX_AGE_MINUTES` (default 30). The age predicate and a `_MAX_CANDIDATES_PER_TICK = 200` cap (a constant, not a setting — it bounds sweep cost, not behavior) both live in SQL, ordered oldest-`executed_at`-first, because every legitimately long-running task in a deployment is a candidate on every tick forever; nothing is skipped permanently, just deferred to a later tick past the cap.

**Repair.** Adds no new judgment: calls the platform's existing `InputTaskService.compute_status_from_sessions(session, task_id)` and, if the result differs from `IN_PROGRESS`, writes it via `InputTaskService.update_task_status(..., changed_by_system=True, reason="Re-derived from session state by status repair")` — deliberately **not** the lower-level `sync_task_status_from_sessions`/`update_status`, because `update_task_status` is what writes the immutable `TaskStatusHistory` row, posts the activity-feed system comment, validates the transition, and emits `TASK_STATUS_CHANGED` — the same audit trail every other status change gets. If `compute_status_from_sessions` returns `None` (no connected sessions at all), the row is left alone — inventing a terminal status with nothing to derive it from would violate "verify, then repair." This no-sessions path is not covered by a test.

**Idempotence** comes from the derivation itself, not a claim check: once repaired, a task is no longer `IN_PROGRESS` and drops out of the candidate query on the next tick.

## Pass D — Channel Turn Deliveries (`status_repair_channels.py`)

**Candidates.** `ChannelTurnDelivery` rows with `role = CHANNEL_DELIVERY_DRAFT` and `updated_at < now - STATUS_REPAIR_CHANNEL_DRAFT_MAX_AGE_MINUTES` (default 60), the one pass whose age filter is fully in SQL (this table has one threshold and grows unboundedly, unlike the small transitional sets the other passes scan).

**Evidence chain, in order** (`_read_and_prepare`): (1) the claim (`role`, `updated_at`) still holds; (2) the bound `ChannelThreadBinding`'s `session_id`, if any, is **not** in `ctx.is_session_in_motion(...)` (Pass B's hand-off — see RepairContext section); (3) the bound session's `interaction_status` is not `"running"` (by this point in the tick, thanks to Pass B running first, "still running" reliably means "still running"); (4) `_relay_is_live(session_id)` — a process-local, veto-only check against `ChannelStreamRegistry.get(session_id)`, using the registry's own `evictable` predicate (`spent or (retired and not stopped)`) rather than the narrower `spent` alone, specifically because a turn cancelled mid-stream (client disconnect, `/stop`, environment restart) never calls `stop()` and would never appear `spent` — an `evictable`-based check is what lets this pass ever repair a cancelled-turn draft at all. A registry lookup that raises is treated as "live" (fails toward not-repairing).

**Repair (`_seal_abandoned_draft`)**, one transaction: `delivery.role = CHANNEL_DELIVERY_SEALED`, `delivery.status = CHANNEL_DELIVERY_DIVERGED` (sealing alone would still let the row's `session_message_id IS NULL` state get it wrongly adopted as a *later* turn's final row; marking it diverged also excludes it from the prefix-based answer-divergence check, which only considers `sealed` rows still marked `delivered`) — and, only if the binding's `status_message_id` still matches the one read during evidence-gathering (guards against a race where a new turn already posted a fresh notice), nulls `binding.status_message_id` in the same commit.

**Notice settlement (`_settle_stale_notice`)**, strictly after the DB commit and fully best-effort (broad `except`, because a known-broken-typing exception (`ChannelError`) has previously been wrong for at least one real channel adapter): resolves the channel + thread key via `channel_outbound_service._binding_thread_key()`, expunges the `ServerChannel` instance and closes the transaction before the network round-trip, then calls `ChannelOutboundService.set_status(channel, thread_key, message_id, text="This turn was interrupted and did not finish.")` — `set_status` (rewrite), never `clear_status` (delete) or the persisting `set_binding_status` variant, because a reaped turn has something honest to put in the notice slot and deleting the message would leave a platform-specific tombstone in the thread instead. This settlement path is not covered by a test.

## Settings

`backend/app/core/config.py`, all under a shared comment block:

| Setting | Default | Used by |
|---|---|---|
| `STATUS_REPAIR_ENABLED` | `True` | Kill switch checked inside `start_scheduler()` (separate from the `TESTING` gate in `main.py`) |
| `STATUS_REPAIR_INTERVAL_MINUTES` | `2` | Sweep tick interval |
| `STATUS_REPAIR_ENV_ACTIVATING_MAX_AGE_MINUTES` | `10` | Pass A — `activating`/`starting` |
| `STATUS_REPAIR_ENV_BUILDING_MAX_AGE_MINUTES` | `60` | Pass A — `creating`/`building`/`rebuilding` |
| `STATUS_REPAIR_STREAM_MAX_AGE_MINUTES` | `120` | Pass B — `interaction_status="running"` |
| `STATUS_REPAIR_PENDING_STREAM_MAX_AGE_MINUTES` | `15` | Pass B — `interaction_status="pending_stream"` |
| `STATUS_REPAIR_TASK_MAX_AGE_MINUTES` | `30` | Pass C |
| `STATUS_REPAIR_CHANNEL_DRAFT_MAX_AGE_MINUTES` | `60` | Pass D |

Three related values are deliberately **module-level constants, not settings**, because each encodes a statement about the lifecycle rather than a tuning knob an operator should adjust independently of its threshold: `_INCONCLUSIVE_ESCALATION_FACTOR = 6` (`status_repair_environments.py`), `_RESEND_MAX_AGE_FACTOR = 2` (`status_repair_sessions.py`), `_MAX_CANDIDATES_PER_TICK = 200` (`status_repair_tasks.py`).

## Cross-Domain Coupling (private symbols reached)

This is the reconciler's main architectural cost — it verifies against live internals in four other domains rather than through a public API, because no public "is this thing actually still alive" seam exists yet in most of them:

- `app.services.environments.admin_environment_service._TRANSITIONAL_STATUSES`
- `app.services.environments.environment_lifecycle._set_status` (also relied on indirectly: the whole heartbeat design assumes it and `_touch_progress` are still the only writers, which is what `environment_status_writer_test.py` pins)
- `app.services.environments.environment_lifecycle.EnvironmentLifecycleManager._sync_dynamic_data` (reached via `EnvironmentService.get_lifecycle_manager()`, per the project's invariant of never instantiating the lifecycle class directly)
- `app.services.server_channels.channel_outbound_service._binding_thread_key`
- `app.services.server_channels.channel_stream_relay.ChannelStreamRegistry`

## Test Surface

- `backend/tests/api/agents/sessions/agents_status_repair_test.py` — Pass A (healthy-container repair to running, dead-container repair to error, inconclusive-probe no-op), Pass B (stale `running` clear + pending-count recompute, `pending_stream` clear-to-idle, resend-once-then-converge), the Pass A→B skip, and the Pass B→C hand-off.
- `backend/tests/api/server_channels/server_channels_status_repair_test.py` — Pass D (stale draft with no live session sealed as diverged) and the Pass B→D skip.
- `backend/tests/architecture/environment_status_writer_test.py` — the structural single-writer invariant Pass A's heartbeat reasoning depends on.

**Not covered by a test today** (called out here rather than left implicit):
- Pass A's build-in-flight veto (`template_image_service.is_build_in_flight`)
- Pass D's notice-settlement path (`_settle_stale_notice` / `ChannelOutboundService.set_status`)
- Pass A's mid-probe re-claim race (a fresh, legitimate operation re-claiming the row between the probe and the write)
- Pass C's no-connected-sessions skip (`compute_status_from_sessions` returning `None`)
- Pass D's `_relay_is_live` veto

**A vacuity note on the test suite itself:** `test_stuck_environment_with_inconclusive_probe_repairs_nothing` only actually proves something in combination with its two sibling Pass A tests in the same file (the healthy-repair and dead-repair cases) — read in isolation, it would pass identically against a Pass A implementation that always did nothing.

## Related

- Plan doc (design rationale, side findings, multi-worker caveat): [docs/plans/system_status_repair_plan.md](../../plans/system_status_repair_plan.md)
