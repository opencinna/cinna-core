# Agent Environment Critical State

## Purpose

When a Docker container starts healthy but a later provisioning step fails — a custom Python package install, system package install, or credential sync — the container stays running but the environment is flagged **critical**. This state is distinct from a true offline environment (`status="error"`) because the agent is reachable and sessions, chat, terminals, and logs all continue to work. Only CRON-scheduled runs are paused until the owner resolves the issue.

The feature exists because these failures were previously indistinguishable from a dead container: both looked like a red "Error" badge and nothing more. The owner had no detail, no context, and the running container confused the picture. Critical state separates the two axes: the container lifecycle status (green "Running") and the provisioning health (amber "Action required").

## Core Concepts

### Critical State vs Offline Error

| Dimension | Critical state | Offline error |
|-----------|---------------|---------------|
| Container running | Yes | No |
| `status` value | `running` | `error` |
| `critical_state` flag | `true` | `false` (cleared) |
| Status badge | Green "Running" | Red "Error" |
| Additional surface | Amber "Action required" block | None |
| Sessions / chat / terminal | Fully available | Unavailable |
| CRON scheduled runs | Blocked | Blocked (env not running) |
| Manual "Run now" | Blocked (returns HTTP 400) | Blocked (env not running) |

The key design decision is that `critical_state` is an **additional axis**, not a new `status` value. It coexists with `status="running"` so that everything that depends on a running container continues to work while the owner's attention is directed to the provisioning failure.

### The Alive-vs-Gone Distinction

When a provisioning step (package install, credential sync) raises an exception, the platform probes whether the container is still alive:

- **Container alive** — enter critical state; status stays `running`; environment remains usable
- **Container gone or probe error** — re-raise the original exception; existing `status="error"` offline path fires

This distinction is made by a liveness probe (`is_container_running()`) in the base adapter, called immediately after any provisioning exception.

### Persisted vs Transient

Critical state is **persisted** on the `AgentEnvironment` row (`critical_state`, `critical_cause`, `critical_since`). This contrasts with `model_health`, which is a transient signal recomputed per request. A provisioning failure is a historical event that cannot be recomputed from current state — it must be recorded when it happens and retained until the next successful setup clears it.

### Action Log

Every significant environment operation — rebuild, setup, package install, credential sync, cron-skip — writes an immutable row to the `agent_env_action_log` table. The log captures the full, untruncated error text (uv resolver output, exception strings) so the owner can see exactly what went wrong. The log is the data source for the "Show details" modal and serves as an append-only audit trail of env-operation history.

Action-log `detail` is operational text only. It must never contain secrets: credential-sync errors log only the failure reason (transport error type + message), never the credential payload.

## User Flows

### 1. Rebuild that partially fails — owner fixes it

1. Owner rebuilds an environment (e.g. added a new dependency to `workspace_requirements.txt`)
2. Docker container starts and becomes healthy
3. `uv install` fails because a package version does not exist
4. Platform detects the container is still alive after the failure
5. Environment enters critical state: `status` stays `running`, `critical_state=true`, `critical_cause="package_install_failed"`
6. An action-log row is written with the full `uv` resolver output as `detail`
7. A realtime `ENVIRONMENT_CRITICAL_STATE_CHANGED` event is emitted; the env card updates live
8. Owner receives an email: "Action needed for [instance name]" with agent name, instance name, a brief cause line ("Failed to install custom packages"), and a link to the agent page. Full detail is intentionally omitted from the email
9. Owner opens the agent page, sees the amber "Action required" block on the environment card
10. Owner clicks "Show details" — the action-log modal opens, showing the uv error with the exact missing package or version conflict
11. Owner edits `workspace_requirements.txt`, clicks Rebuild
12. This time setup completes; critical state is cleared; a `success` action-log row is recorded; the amber block disappears

### 2. Credential sync fails — owner investigates

1. Owner restarts an environment after rotating a credential value in the UI
2. Container starts; credential sync to the container fails (HTTP transport error)
3. Critical state entered: `critical_cause="credential_sync_failed"`
4. "Show details" modal shows the sanitized transport error (type + message, no secrets)
5. Owner checks the credential configuration, corrects it, and restarts — sync succeeds; critical state clears

### 3. CRON schedule skip

1. Owner's environment is in critical state
2. A due CRON schedule fires at the scheduled time
3. Scheduler detects `critical_state=true` on the active environment
4. Schedule is not dispatched; a `skipped` `AgentScheduleLog` entry is written with the reason
5. A `cron_skipped` action-log row is written to the environment log
6. Owner is notified via the `environment_critical` notification type (dedup-throttled: at most one email per 30 minutes per environment across both setup-failure and cron-skip paths)
7. `next_execution` is advanced so the scheduler does not re-fire the same due time every minute
8. Owner resolves the critical state (rebuilds successfully) — next due CRON fires normally

### 4. Offline supersedes critical

1. Owner's environment is in critical state (container running but package install failed)
2. An external event causes the container to become unreachable (host restart, Docker daemon issue)
3. The status scheduler confirms the container is down: sets `status="error"` and clears `critical_state`
4. A `ENVIRONMENT_STATUS_CHANGED` event and a `ENVIRONMENT_CRITICAL_STATE_CHANGED(critical_state=false)` event are emitted
5. The env card shows only the red "Error" badge — not both red and amber. Offline supersedes critical.

## Business Rules

- **Only CRON is gated.** Sessions, chat, terminals, logs, the env console, and the A2A/MCP surfaces are unaffected by critical state. The container is running; nothing that requires a running container is blocked.
- **Manual "Run now" is also blocked.** `AgentSchedulerService.execute_now` returns HTTP 400 when the environment is critical. The message tells the owner to resolve the environment issue first. This keeps the contract consistent with the CRON gate.
- **Fire-once email per transition.** The setup-failure email is gated by a process-local set (`_critical_warned_env_ids`). A persistently-critical environment that is rebuilt again does not re-email on each failed attempt. Clearing critical state removes the environment from the set so a future re-failure re-emails.
- **CRON-skip notifications are dedup-throttled.** The scheduler polls every minute; the `environment_id` dedup scope on the `environment_critical` notification type caps emails to one per 30 minutes per environment — shared with the setup-failure email to avoid cumulative spam.
- **`critical_since` records the onset.** It is stamped only on the `false → true` transition. Repeated failed rebuilds while already critical do not move it.
- **Offline supersedes critical.** When the health scheduler confirms the container is down, it clears `critical_state` in the same commit that writes `status="error"`. The env card shows only the red badge.
- **Recovery is automatic.** A subsequent successful rebuild or restart runs the same provisioning steps; if they all succeed, `_clear_critical_state` is called, the flag is cleared, and a `success` action-log row is recorded.
- **No secrets in action-log detail.** Credential-sync errors log only the transport failure reason, never the credential payload. This is enforced in the lifecycle by sanitizing before passing `detail` to `_enter_critical_state`.
- **No migration for the notification type.** `environment_critical` follows the notification catalog pattern: a catalog entry plus a built email template. No schema change, no backfill.
- **`emails_enabled` off or unconfirmed user.** Dispatch is silently skipped (existing email guards). The amber card and action log still work — only the email channel is suppressed.

## Integration Points

| System | Integration |
|--------|-------------|
| [Agent Environments lifecycle](agent_environments.md) | `_enter_critical_state` / `_clear_critical_state` helpers in `EnvironmentLifecycleManager`; the alive-vs-gone liveness probe in `_container_alive_else_raise`; called from `_setup_new_container` (package installs) and `_sync_dynamic_data` (credential sync). Recovery-clear on successful setup. |
| [Model Freshness](model_freshness.md) | Sibling amber-badge surface on the environment card. `ModelHealthBadge` (transient, per-request) and `EnvironmentCriticalBadge` (persisted, event-driven) follow the same amber-palette convention. Both can be visible simultaneously. Critical state is independent of model health. |
| [System Notifications](../../application/system_notifications/system_notifications.md) | New `environment_critical` catalog type with `dedup_scope="environment_id"`. Dispatched on the `false → true` transition (setup failure) and on each CRON skip (throttled). Per-user preference toggle in Settings → Notifications appears automatically. |
| [Agent Schedulers](../agent_schedulers/agent_schedulers.md) | CRON gating in `_poll_due_schedules`: critical env → skip + `AgentScheduleLog(status="skipped")` + `cron_skipped` action-log + notification + advance `next_execution`. Manual "Run now" (`execute_now`) returns HTTP 400. |
| [Realtime Events](../../application/realtime_events/event_bus_system.md) | `ENVIRONMENT_CRITICAL_STATE_CHANGED` event (payload: `environment_id`, `agent_id`, `instance_name`, `critical_state`, `cause`, `summary`). Emitted on both enter and clear. `AgentEnvironmentsTab` subscribes and invalidates both the env list and the action-log query. |

*Last updated: 2026-06-20*
