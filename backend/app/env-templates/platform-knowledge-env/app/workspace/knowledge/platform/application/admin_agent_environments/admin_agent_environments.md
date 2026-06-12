# Admin Agent Environments

## Purpose

Give platform superusers a single console to inspect and maintain all agent environments across the entire fleet. The primary operational use-case is: a system update has been deployed (new `app_core_base` code, updated template image), and the admin needs to identify which environments are running old images and bring them all up to date without touching each agent individually.

## Core Concepts

### Staleness

An environment is **stale** when its `current_image_tag` column differs from the tag that `TemplateImageService` would compute today for its template. In plain terms: the Docker image the environment was last built against is not the latest available image for that template.

Two additional cases are always treated as stale:
- `current_image_tag` is `NULL` — the environment predates the migration (never been rebuilt since the admin fields were added) or was created before the tracking column existed.
- The template directory for the environment's `env_name` no longer exists on the server — the row shows `expected_image_tag = null` and the rebuild action is disabled with a "template missing" indicator.

### Model Health Warning

A separate, independent signal from image-tag staleness. An environment has a **model health warning** when any of its configured AI models are deprecated, retired, or unavailable to its credential. The warning is a **configuration** problem (remediation: edit the model override or restart to pick up the current catalog default); it is not fixed by a Docker image rebuild.

`AdminAgentEnvironmentPublic.model_health_warning` (`bool`) appears as a column in the admin table beside `is_stale`. It is computed cheaply per row by `evaluate_environment` during `list_environments`. See [Model Freshness](../../agents/agent_environments/model_freshness.md) for the full feature description.

### In-Use

An environment is **in use** when any of the following is true:
1. `sync_active = true` — a CLI Mutagen sync tunnel is connected.
2. Status is one of `running`, `activating`, `starting`, or `rebuilding`.
3. At least one session sent a message within the last 10 minutes (checked via `last_message_at`).

The admin console surfaces this so that administrators can decide whether to rebuild immediately or wait for activity to stop.

### Bulk Rebuild Cap

A single bulk-rebuild request accepts at most `ADMIN_ENV_MAX_BULK_SIZE` environment IDs (default 200). The UI enforces this on submission; the backend enforces it again independently. Requests exceeding the cap are rejected with HTTP 400. For larger fleets, the admin runs the rebuild in multiple batches.

## User Flows

### Flow 1 — Post-Deployment Audit

1. Admin opens sidebar > Admin > Agent Environments.
2. Page loads. If any environments are behind the current template image, an orange alert banner appears: "N of M environments are behind the current template image."
3. Admin clicks "Select all stale". The filter switches to stale-only, allowing the table header checkbox to select all visible stale rows.
4. Bulk action bar appears: "N envs selected — Rebuild Selected".
5. Admin clicks "Rebuild Selected". A confirm dialog opens, showing the selected environments grouped by template and split by current status (running / stopped / suspended).
6. Admin confirms. A toast shows "Rebuild queued for N environments." and the dialog closes.
7. Status badges in the table transition to amber "rebuilding" in real time as each rebuild starts. Rows return to their final state (running / stopped) when complete.

### Flow 2 — Targeted Rebuild

1. Admin uses the template dropdown to filter to a specific template (e.g., `python-env-advanced`).
2. Admin applies the status filter to narrow further.
3. Admin ticks individual rows and clicks "Rebuild Selected".
4. Confirm dialog lists the specific environments. Admin confirms.

### Flow 3 — Single-Environment Rebuild (row action)

Not yet present in the current table UI — individual rebuilds are triggered via the `/admin/agent-environments/{env_id}/rebuild` endpoint, which the admin can reach programmatically. (A row kebab menu was planned in the implementation draft but is not part of the current UI implementation.)

### Flow 4 — Diagnosing an Environment

1. Admin uses the search box to find environments by agent name, instance name, or owner email.
2. Stale badge tooltip shows `current: <short-hash>` and `expected: <short-hash>` side by side.
3. Image tag cells show the first 12 characters of the hash; hovering reveals the full tag, and a copy-to-clipboard icon appears on hover.

## Business Rules

- All admin environment endpoints require `is_superuser = true`. Non-superusers receive HTTP 403 before any data is returned.
- The admin page at `/admin/agent-envs` performs a client-side `beforeLoad` guard checking `user.is_superuser`. Non-superusers are redirected to `/`.
- Environments in a transitional status (`creating`, `building`, `initializing`, `starting`, `rebuilding`, `activating`) cannot be rebuilt and are skipped in bulk operations (returned in the `skipped` list with reason `status_not_allowed`).
- Environments not found by UUID are returned in the `skipped` list with reason `not_found`. The rest of the batch continues.
- Bulk rebuilds run concurrently but are throttled by `ADMIN_BULK_REBUILD_CONCURRENCY` (default 4) to avoid overwhelming the Docker daemon.
- The endpoint returns immediately with `queued_environment_ids` and `skipped`. Real-time progress arrives via the existing `ENVIRONMENT_STATUS_CHANGED` WebSocket events already subscribed by the admin page.
- A `SecurityEvent` row (`event_type = "admin.environment.rebuild"`) is written for every rebuild an admin triggers, recording `env_id`, `agent_id`, `initiator_user_id`, and whether it was a bulk operation.

## Integration Points

| System | Integration |
|--------|-------------|
| [Agent Environments](../../agents/agent_environments/agent_environments.md) | `EnvironmentLifecycleManager.rebuild_environment()` is the single entry point for all rebuild operations. Admin-triggered rebuilds use this path unchanged. |
| [Agent Environment Core](../../agents/agent_environment_core/agent_environment_core.md) | The `current_image_tag` field is written inside `_update_environment_config()` during any start or rebuild; `last_build_at` is written on rebuild completion. |
| [Realtime Events](../realtime_events/event_bus_system.md) | The admin page subscribes to `ENVIRONMENT_STATUS_CHANGED` to update table rows in real time as rebuilds progress. |
| [Model Freshness](../../agents/agent_environments/model_freshness.md) | The `model_health_warning` column is computed by `evaluate_environment` per row during `list_environments`. Distinct from `is_stale`: different cause, different remediation. |
| Security Events | Each admin-triggered rebuild emits a `SecurityEvent` row for audit purposes. |
| Sidebar | The "Agent Environments" entry in `AdminMenu.tsx` (between Users and Knowledge Sources) is only rendered for superusers. |

## Edge Cases

- **Template directory missing**: Row is displayed with `expected_image_tag = null` and `is_stale = true`. The rebuild action is disabled; the UI shows a "template missing" tooltip.
- **Agent deleted mid-rebuild**: The lifecycle manager raises an error and marks the environment `error`. The `ENVIRONMENT_STATUS_CHANGED` event updates the row.
- **Env already rebuilding**: Returned in `skipped` with `status_not_allowed`. No double-rebuild.
- **Bulk > 200 items**: HTTP 400 with a descriptive error message. Selection is preserved so the admin can split it.
- **NULL `current_image_tag`** (pre-migration environments): Always treated as stale. Next rebuild or start populates the column.
- **Docker daemon unreachable**: All rebuilds in the batch fail; each environment transitions to `error` status with a message in `status_message`. The bulk endpoint still returns `queued_environment_ids` as queued.
