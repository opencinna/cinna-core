# Admin Agent Environments — Technical Details

## File Locations

### Backend

- `backend/app/api/routes/admin_environments.py` — API route handlers (list, bulk-rebuild, single-rebuild)
- `backend/app/services/environments/admin_environment_service.py` — `AdminEnvironmentService`
- `backend/app/services/environments/model_health_service.py` — `evaluate_environment` (called per row to populate `model_health_warning`)
- `backend/app/models/environments/environment.py` — admin response schemas and the two new `AgentEnvironment` columns
- `backend/app/core/config.py` — `ADMIN_BULK_REBUILD_CONCURRENCY`, `ADMIN_ENV_MAX_BULK_SIZE`, `ENV_TEMPLATES_DIR`
- `backend/app/alembic/versions/d40c20201e5b_add_admin_env_fields.py` — migration adding `last_build_at` and `current_image_tag`

### Frontend

- `frontend/src/routes/_layout/admin/agent-envs.tsx` — page route and query/mutation wiring
- `frontend/src/components/Admin/Environments/AdminEnvTable.tsx` — TanStack Table with row selection and bulk action bar
- `frontend/src/components/Admin/Environments/AdminEnvFiltersBar.tsx` — filter controls (template, status, stale/in-use toggles, search)
- `frontend/src/components/Admin/Environments/AdminEnvStaleBanner.tsx` — orange alert with "Select all stale" shortcut
- `frontend/src/components/Admin/Environments/AdminEnvBulkRebuildDialog.tsx` — confirm dialog grouped by template
- `frontend/src/components/Sidebar/AdminMenu.tsx` — sidebar dropdown that includes the Agent Environments link

## Database Models

### `AgentEnvironment` (existing table, additive columns)

Two nullable columns added by migration `d40c20201e5b`:

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `last_build_at` | `TIMESTAMP WITH TIME ZONE` | NULL | Written by `EnvironmentLifecycleManager.rebuild_environment()` on successful completion. Used by the admin console to display when an environment was last rebuilt. |
| `current_image_tag` | `VARCHAR(255)`, indexed | NULL | Written by `EnvironmentLifecycleManager._update_environment_config()` whenever a start or rebuild runs. Stores the full Docker image tag (e.g., `cinna-agent-python-env-advanced:a1b2c3d4e5f6`). Compared against the live expected tag to derive `is_stale`. NULL is treated as stale. |

The index on `current_image_tag` supports future filtering queries. No `AgentEnvironmentCreate` or `AgentEnvironmentUpdate` schema changes — these columns are system-managed.

### Admin Response Schemas (no database tables)

All schemas live in `backend/app/models/environments/environment.py` alongside the existing environment schemas.

**`AdminAgentEnvironmentPublic`** (inherits `AgentEnvironmentPublic`):

| Field | Type | Source |
|-------|------|--------|
| `agent_name` | `str` | Joined from `Agent.name` |
| `owner_id` | `uuid.UUID` | Joined from `User.id` (agent owner) |
| `owner_email` | `str` | Joined from `User.email` |
| `owner_username` | `str \| None` | Joined from `User.username` |
| `owner_workspace_id` | `uuid.UUID \| None` | `agent.user_workspace_id` |
| `current_image_tag` | `str \| None` | From `AgentEnvironment.current_image_tag` |
| `expected_image_tag` | `str \| None` | Computed via `TemplateImageService.get_image_tag(env_name)`. `None` when template directory is missing. |
| `template_hash_current` | `str \| None` | 12-char hash extracted from `current_image_tag` (the part after the colon) |
| `template_hash_expected` | `str \| None` | Computed via `TemplateImageService.compute_template_hash(env_name)` |
| `is_stale` | `bool` | `current_image_tag != expected_image_tag`, or `True` when either is `None` |
| `in_use` | `bool` | Derived (see service logic below) |
| `active_sessions_count` | `int` | Count of sessions with `last_message_at >= now() - 10min` |
| `last_build_at` | `datetime \| None` | From `AgentEnvironment.last_build_at` |
| `sync_active` | `bool` | From `AgentEnvironment.sync_active` |
| `model_health_warning` | `bool` | `True` when any mode's configured model is deprecated/unavailable. Computed by `evaluate_environment` per row. Distinct from `is_stale`: config health, not image-tag staleness. |
| `bundle_id` | `str \| None` | Reverse-DNS bundle identifier (`Agent.bundle_id`), populated only when `Agent.bundle_uuid IS NOT NULL`. `None` for standalone agents that were never published or installed from a bundle — even though every `Agent` row carries an internal `bundle_id` string, it names nothing actionable there |
| `is_publisher_install` | `bool` | From `Agent.is_publisher_install` |
| `update_mode` | `str \| None` | From `Agent.update_mode` (`"manual"` \| `"automatic"`); `None` for non-bundle agents |
| `installed_revision_number` / `installed_revision_version` | `int \| None` / `str \| None` | Resolved from `Agent.installed_revision_id` via the batched revision lookup (see service logic below) |
| `latest_revision_number` / `latest_revision_version` | `int \| None` / `str \| None` | Resolved from `AgentBundle.latest_revision_id` via the same batched lookup |
| `update_available` | `bool` | `bundle is not None and latest_revision_id is not None and agent.installed_revision_id != latest_revision_id and not agent.is_publisher_install`. Always `False` for publisher installs and non-bundle agents. A third staleness axis, distinct from `is_stale` (image tag → rebuild) and `model_health_warning` (model config → reconfigure); this one's remediation is apply-update. See [Agent Bundles & Installs](../../agents/agent_bundles/agent_bundles.md) |

**`AdminAgentEnvironmentsPublic`** (list response):

| Field | Type | Description |
|-------|------|-------------|
| `data` | `list[AdminAgentEnvironmentPublic]` | Paginated enriched rows |
| `count` | `int` | Total count after post-query filters (pre-pagination) |
| `stale_count` | `int` | Stale environments in the filtered set |
| `in_use_count` | `int` | In-use environments in the filtered set |
| `templates` | `list[AdminTemplateInfoPublic]` | Per-template summary (always unfiltered) |

**`AdminTemplateInfoPublic`**:

| Field | Type | Description |
|-------|------|-------------|
| `env_name` | `str` | Template directory name |
| `expected_image_tag` | `str \| None` | Current image tag for this template |
| `expected_hash` | `str \| None` | 12-char content hash |
| `total_envs` | `int` | Total environments using this template |
| `stale_envs` | `int` | Stale environments using this template |

**`AdminBulkRebuildRequest`**:

| Field | Constraint |
|-------|-----------|
| `environment_ids` | `list[uuid.UUID]`, `min_length=1`, `max_length=200` (schema-level cap; runtime cap is `settings.ADMIN_ENV_MAX_BULK_SIZE`) |

**`AdminBulkRebuildResponse`**:

| Field | Type | Description |
|-------|------|-------------|
| `queued_environment_ids` | `list[uuid.UUID]` | IDs for which a rebuild was scheduled |
| `skipped` | `list[AdminBulkSkipped]` | Environments that could not be queued |

**`AdminBulkSkipped`**:

| Field | Type | Values |
|-------|------|--------|
| `environment_id` | `uuid.UUID` | — |
| `reason` | `str` | `"not_found"` \| `"status_not_allowed"` |

## API Routes

Router prefix: `/api/v1/admin/agent-environments`
Tag: `admin-environments`
All routes use the `SuperUser` dependency (`Annotated[User, Depends(get_current_active_superuser)]`).

### `GET /`

List all environments with admin enrichment.

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `template` | `str \| None` | Exact match on `env_name` |
| `status` | `str \| None` | Exact match on `status` |
| `is_stale` | `bool \| None` | Filter by computed staleness (post-enrichment) |
| `in_use` | `bool \| None` | Filter by computed in-use flag (post-enrichment) |
| `update_available` | `bool \| None` | Filter by computed bundle `update_available` flag (post-enrichment) |
| `owner_id` | `uuid.UUID \| None` | Filter by agent owner user ID |
| `search` | `str \| None` | ILIKE match against agent name, instance name, owner email/username |
| `skip` | `int` | Pagination offset (default 0) |
| `limit` | `int` | Page size 1–500 (default 100, hard-capped at 500) |

**Response:** `AdminAgentEnvironmentsPublic`

Note: `is_stale`, `in_use`, and `update_available` filters are applied after enrichment because they are computed fields. `template`, `status`, `owner_id`, and `search` are pushed down to the SQL query.

### `POST /bulk-rebuild`

Queue background rebuild tasks for a selection of environments.

**Request body:** `AdminBulkRebuildRequest`
**Response:** `AdminBulkRebuildResponse`

Returns immediately. Progress arrives via `ENVIRONMENT_STATUS_CHANGED` WebSocket events. Emits one `SecurityEvent` per queued environment in a single batch commit.

If `len(environment_ids) > settings.ADMIN_ENV_MAX_BULK_SIZE`, returns HTTP 400.

### `POST /{env_id}/rebuild`

Trigger a rebuild for a single environment (admin path).

**Path parameter:** `env_id` (UUID)
**Response:** `Message`

Thin wrapper around the existing rebuild path. Uses `EnvironmentService.get_environment_with_access_check(..., is_superuser=True)` to bypass ownership check. Emits one `SecurityEvent` before scheduling the background task.

## Service Layer

`AdminEnvironmentService` (static methods only, no instantiation).

### `list_environments`

1. Executes a single SQL query joining `AgentEnvironment → Agent → User` (agent owner), **additionally LEFT JOINed to `AgentBundle` on `Agent.bundle_uuid`** so bundle enrichment needs no extra query.
2. Applies `template`, `status`, `owner_id`, and `search` filters at the SQL level.
3. Batch-loads recent session counts for all result environments in a second aggregated query (avoids N+1). Threshold: `last_message_at >= now() - 10min`.
4. Iterates result rows. Per unique `env_name`, computes `(expected_image_tag, expected_hash)` once via `TemplateImageService` and caches them in a local dict (`_tag_cache`).
5. Collects every `AgentBundleRevision` id referenced across the page — both `agent.installed_revision_id` and `bundle.latest_revision_id` — into one `set`, then resolves them all in a **single batched `IN` query** cached in `revisions_by_id: dict[uuid.UUID, AgentBundleRevision]`. Deliberately not `session.get()` per row: this list is fleet-wide and per-row lookups are exactly the N+1 that already bit the model-health rollup.
6. For each row: derives `is_stale` (tag comparison or NULL check), derives `in_use` via `_derive_in_use`, computes `model_health_warning` via `evaluate_environment`, computes `update_available` from the batched revision dict (`bundle is not None and latest_revision_id is not None and installed_revision_id != latest_revision_id and not agent.is_publisher_install`), builds `AdminAgentEnvironmentPublic` with the bundle fields (`bundle_id`, `is_publisher_install`, `update_mode`, `installed_revision_number`/`version`, `latest_revision_number`/`version`, `update_available`).
7. Applies `is_stale`, `in_use`, and `update_available` post-query filters.
8. Computes aggregate counts (`stale_count`, `in_use_count`) from the full filtered set before paginating.
9. Paginates with `skip`/`limit`.
10. Calls `list_templates` and attaches the result as `templates`.

### `list_templates`

Iterates directories under `settings.ENV_TEMPLATES_DIR`. Skips `app_core_base` and any directory without a `Dockerfile`. For each valid template directory: computes expected tag/hash, counts total and stale environments from the DB.

### `bulk_rebuild`

1. Validates each `env_id`: skip with `not_found` if missing; skip with `status_not_allowed` if status is in `_TRANSITIONAL_STATUSES`.
2. Builds `SecurityEvent` objects for all queued IDs and commits them in a single batch.
3. Creates an `asyncio.Semaphore(settings.ADMIN_BULK_REBUILD_CONCURRENCY)` and schedules `asyncio.create_task` for each queued ID wrapped in `_rebuild_with_semaphore`.
4. Returns `AdminBulkRebuildResponse` immediately.

### `_rebuild_env_background`

Opens a fresh database session via `create_session()` and calls `EnvironmentService.rebuild_environment(session, env_id)`. Errors are caught and logged per-environment; they do not propagate to stop other rebuilds.

### `_derive_in_use`

```
if env.sync_active:           → True
if env.status in {running, activating, starting, rebuilding}: → True
if sessions_count > 0:        → True
else:                         → False
```

### `_template_exists` (module-level helper)

Returns `True` if `settings.ENV_TEMPLATES_DIR / env_name / "Dockerfile"` exists. Used to detect orphaned environments whose template has been uninstalled.

## Lifecycle Manager Integration

`backend/app/services/environments/environment_lifecycle.py` writes the admin tracking fields:

- `_update_environment_config(db_session, instance_dir, environment, agent, image_tag)`: If `image_tag` is provided, sets `environment.current_image_tag = image_tag` before writing config files. This runs on every environment start and rebuild.
- `rebuild_environment(...)`: After the rebuild completes successfully (image rebuilt, container re-upped or stopped), sets `environment.last_build_at = datetime.now(UTC)`.

Both writes happen in the same database session as the rebuild, committed as part of the normal lifecycle state transition.

## Background Task Flow

```
POST /bulk-rebuild
  │
  ├── validate env IDs (session query)
  ├── batch-commit SecurityEvent rows
  │
  └── for each queued ID:
        asyncio.create_task(
          _rebuild_with_semaphore(env_id)
            └── asyncio.Semaphore(ADMIN_BULK_REBUILD_CONCURRENCY)
                  └── _rebuild_env_background(env_id)
                        └── with create_session() as bg_session:
                              EnvironmentService.rebuild_environment(bg_session, env_id)
                                ├── EnvironmentLifecycleManager.rebuild_environment(...)
                                │     ├── docker compose down + build + up
                                │     ├── env.current_image_tag = image_tag
                                │     └── env.last_build_at = datetime.now(UTC)
                                └── emit ENVIRONMENT_STATUS_CHANGED
```

Background tasks open their own database sessions (`create_session()`) so they do not share the request-scoped session that queued them.

## Security Event Audit Trail

Every admin-triggered rebuild (bulk or single) writes to the `security_events` table:

```json
{
  "event_type": "admin.environment.rebuild",
  "severity": "low",
  "user_id": "<actor superuser id>",
  "agent_id": "<agent id of the environment>",
  "environment_id": "<environment id>",
  "details": {
    "bulk": true,
    "initiator_user_id": "<actor superuser id as string>"
  }
}
```

For bulk operations, events are collected in memory and inserted with a single `session.add_all(audit_events); session.commit()`. A failure to persist audit events is logged as a warning but does not abort the rebuild.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `ADMIN_BULK_REBUILD_CONCURRENCY` | `4` | Maximum parallel environment rebuilds within a bulk request |
| `ADMIN_ENV_MAX_BULK_SIZE` | `200` | Maximum `environment_ids` per bulk-rebuild request |
| `ENV_TEMPLATES_DIR` | `"backend/app/env-templates"` | Filesystem path to the template directory root (used by `list_templates` and `_template_exists`) |

## Frontend Architecture

### Route: `_layout/admin/agent-envs`

- `beforeLoad`: redirects unauthenticated users to `/login`; redirects non-superusers to `/`.
- Renders a secondary guard inside the component body for any race-condition edge case.
- Sets page header to "Agent Environments / Rebuild environments after system updates".

### React Query Keys

| Key | Source endpoint |
|-----|----------------|
| `["admin", "agent-environments", filters]` | `GET /api/v1/admin/agent-environments/` |

Refetch interval: 60 seconds. Stale time: 30 seconds.

### WebSocket Integration

On mount, the route subscribes to `EventTypes.ENVIRONMENT_STATUS_CHANGED` via `eventService.subscribe()`. On each event, the list query is invalidated (`queryClient.invalidateQueries`). The subscription is torn down on unmount.

### Components

**`AdminEnvTable`**: TanStack Table (`useReactTable`) with:
- Checkbox column; rows in transitional statuses have `enableRowSelection = false` and render at 60% opacity.
- Columns: Agent (name + owner email), **Bundle** (`BundleCell` — bundle ID mono/truncated with tooltip; placed immediately after Agent), Instance, Template (badge), Status (`StatusBadge`), In use (`InUseBadge`), Stale (`StaleBadge`), Model Health (`ModelHealthCell` — amber indicator when `model_health_warning`), Current tag (`ImageTagCell`), Expected tag (`ImageTagCell`), Last built, Last activity.
- `BundleCell`: em dash when `bundle_id` is null; for a bundle row, shows the bundle ID plus an installed-version badge (`v1.4`, from `revisionLabel`); when `update_available` is true (never for publisher installs) an additional amber `→ v1.5` badge appears — deliberately amber and arrow-shaped rather than reusing `StaleBadge`'s styling, since bundle revision drift (apply-update) and image-tag staleness (rebuild) are different axes and must not read as the same problem. `revisionLabel` is imported from `frontend/src/utils/bundleRevision.ts`, the same helper used by `UpdateAvailableBanner` / `BundleInstallationCard` on the agent page.
- Bulk action bar (shown when `selectedRows.length > 0`): "N envs selected", "Rebuild Selected" button, "Clear" button.
- Delegates confirm dialog to `AdminEnvBulkRebuildDialog`.

**`AdminEnvFiltersBar`**: Template `<Select>` (populated from `data.templates`), Status `<Select>`, "Only stale" toggle `<Button>`, "Only in use" toggle `<Button>`, "Bundle update available" toggle `<Button>` (amber when active, matching the Bundle column's badge color and deliberately distinct from the stale toggle's orange — wired to the `update_available` query param via the same `true ↔ null` two-state toggle pattern as the stale/in-use buttons), debounced text search (350ms). Filter state is owned by the route component (`_layout/admin/agent-envs.tsx`).

**`AdminEnvStaleBanner`**: Renders only when `staleCount > 0`. Hidden when the stale filter is already active (would produce "N of N" noise). The "Select all stale" button sets `isStale = true` on the filter, causing only stale rows to appear so the header checkbox can select all.

**`AdminEnvBulkRebuildDialog`**: Confirm dialog. Shows running/stopped/suspended split. Groups selected environments by template with agent name, instance name, and owner email for each row.

### Status Badge Colors

| Status | Color |
|--------|-------|
| `running` | Emerald |
| `stopped` | Neutral |
| `suspended` | Slate |
| `error` | Red |
| `deprecated` | Muted |
| Transitional (`creating`, `building`, `initializing`, `starting`, `rebuilding`, `activating`) | Amber with pulsing spinner |

## Filesystem Dependency

`list_templates` and `_template_exists` read the filesystem at `settings.ENV_TEMPLATES_DIR`. The path defaults to `backend/app/env-templates` (relative to the backend working directory). In Docker deployments the path resolves inside the backend container. If the templates directory does not exist, `list_templates` returns an empty list. If a template directory is missing for an environment's `env_name`, that environment's `expected_image_tag` is returned as `None` and `is_stale` is `True`.
