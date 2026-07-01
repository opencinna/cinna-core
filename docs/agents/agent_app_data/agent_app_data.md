# Agent App Data

## Purpose

Provide each user with a persistent, private storage area for every bundle they install. App Data is keyed by `(user_id, bundle_id, catalog_type)` and survives the install lifecycle — specifically it is **not** deleted when a user uninstalls a bundle. Reinstalling the same bundle reattaches the same App Data so runtime state (user preferences, downloaded files, cached data) carries forward across updates and reinstalls.

## Core Concepts

- **App Data Volume** (`AppDataVolume`) — one row per `(user_id, bundle_id, catalog_type)` triple; backed by a bind-mounted host directory
- **Lifecycle independence** — the volume row and its on-disk data outlive the `Agent` (Install) row; uninstall marks it orphaned, not deleted
- **Orphaned volume** — `is_orphaned = true`; no install currently references it. User can wipe orphaned volumes from Settings; non-orphaned volumes are protected
- **Reattachment** — when the same bundle is reinstalled, `AppDataService.get_or_create_volume` finds the orphaned row by matching all three fields `(user_id, bundle_id, catalog_type)`, clears `is_orphaned`, and links the new install. A consumer reinstall reattaches the previous consumer volume, not the publisher's volume
- **Container mount point** — `/app/workspace/app-data` inside the agent's Docker environment, with four sub-directories: `storage/`, `uploads/` (the destination for all user file uploads — chat attachments, task attachments, MCP `get_file_upload_url`), `cache/`, `memory/` (personal per-install agent memory injected into system prompts)

## User Stories / Flows

### App Data Created on First Install

1. User installs a bundle for the first time
2. `InstallService._install_from_revision` calls `AppDataService.get_or_create_volume(session, user_id, bundle_id, catalog_type)` — consumer installs pass `catalog_type="server"`; publisher installs pass `catalog_type=None`
3. Directory `<APP_DATA_STORAGE_DIR>/<user_id>/<bundle_id>/` is created with `storage/`, `uploads/`, `cache/` sub-directories
4. `AppDataVolume` row inserted (or reused if previously orphaned)
5. Agent environment is started; `app-data/` is bind-mounted into the container at `/app/workspace/app-data`

### App Data Survives Uninstall

1. User uninstalls the agent (calls `POST /agents/{id}/uninstall`)
2. `AgentService.delete_agent` calls `AppDataService.mark_orphaned` before deleting the `Agent` row
3. `AppDataVolume.is_orphaned` set to `true`; `current_install_id` cleared
4. On-disk directory is **not** deleted
5. Volume appears in **Settings → App Data** tab with an "orphaned" badge

### Reinstall Reattaches App Data

1. User installs the same bundle again
2. `AppDataService.get_or_create_volume` finds the existing row by `(user_id, bundle_id, catalog_type)` — the same `catalog_type` used at first install (`"server"` for a consumer reinstall)
3. `is_orphaned` cleared; `current_install_id` set to the new install ID
4. Container starts with the same bind-mount — all previous data is available

### Viewing App Data (Settings → App Data tab)

1. User opens Settings → App Data
2. Frontend calls `GET /users/me/app-data`
3. List shows: bundle_id, linked install name (or "orphaned"), size in human-readable format, last size check timestamp
4. Per-row actions:
   - **Refresh size** — calls `POST /users/me/app-data/{id}/recompute-size`; walks the directory with `os.scandir`, updates `size_bytes`
   - **Wipe** (orphaned only) — calls `DELETE /users/me/app-data/{id}`; removes the on-disk directory and the row; shows confirmation modal

### Apply Update Preserves App Data

When `InstallService.apply_update` runs:
1. Environment is stopped
2. `replace_bundle_content` overwrites bundle-owned folders (`scripts/`, `docs/`, `knowledge/`, `files/`, requirements files)
3. `app-data/` is **not touched** — the bind mount path is not in the set of replaced folders
4. Environment restarts with the same app-data mount

## Business Rules

- **Owner-only access** — no admin override; admins cannot read or manage another user's app-data (matches "private profile" framing)
- **Wipe requires orphaned status** — `AppDataService.wipe_volume` raises an error if `is_orphaned = false`. The UI hides the Wipe button for attached volumes. This prevents accidental data loss while an install is running
- **Unique per user × bundle × catalog_type** — `uq_app_data_user_bundle_catalog` unique constraint on `(user_id, bundle_id, catalog_type)` ensures exactly one volume row per triple. Postgres treats `NULL` values as distinct in unique constraints, which is intentional: the publisher's slot (`catalog_type=NULL`) coexists with their consumer slot (`catalog_type="server"`) without colliding

### `catalog_type` field

`catalog_type` is a plain nullable string column that records where an App Data volume originated:

- `NULL` — publisher or owned installs that have no bundle-catalog origin (the publisher's working copy)
- `"server"` — consumer installs sourced from this instance's catalog

The column is a plain string rather than a database enum so future values (e.g. `"marketplace"`, `"remote:<host>"`) can be added without a schema change. The backfill rule for existing rows: volumes whose paired agent has `is_publisher_install=True` receive `NULL`; all other bundle-linked volumes receive `"server"`; orphaned volumes with no paired agent receive `"server"`.
- **`bundle_id` column stores the string, not the UUID** — this keeps the row stable if the `AgentBundle` row is deleted (publisher deletes their bundle); orphaned volumes do not lose their bundle_id
- **Directory creation on `get_or_create`** — `storage/`, `uploads/`, `cache/`, `memory/` are created with mode 0o755 every time the volume is touched to handle the case where a subdirectory was manually removed
- **Size is lazy** — `size_bytes` is not updated in real-time; users trigger a recompute manually from the Settings tab
- **Daily orphan report** — a daily APScheduler job (`app_data_orphan_scheduler.py`) logs orphaned volumes older than 90 days but does NOT delete them; deletion is always user-driven
- **On-disk GC after account/install deletion** — deleting a user (or hard-deleting an install) drops the `AppDataVolume` rows via FK cascade, but the cascade never touches the filesystem. Rather than block account deletion on a potentially slow recursive delete, an APScheduler job (`app_data_gc_scheduler.py`, every 6h) reclaims the leftover directories out-of-band: it diffs the on-disk tree against the DB and `rmtree`s any directory with no remaining DB representation — a whole `<user_id>/` tree when the user is gone, or an individual bundle subtree under a live user that no volume row maps to. Directories modified within a 1-day grace window are skipped so an in-flight install (directory written before its row commits) is never reclaimed; non-UUID top-level directories are left untouched

## Architecture Overview

```
/app/workspace/                     (env container)
├── scripts/        ← bundle-owned (replaced on update)
├── docs/           ← bundle-owned
├── knowledge/      ← bundle-owned
├── files/          ← bundle-owned
├── credentials/    ← synced from platform
└── app-data/       ← bind-mounted from AppDataVolume.host_path
    ├── storage/    ← for structured user data (DBs, JSON, CSVs)
    ├── uploads/    ← for files the user provides at runtime
    ├── cache/      ← for cached downloads, processed files
    └── memory/     ← personal per-install agent memory (*.md), injected into system prompts

Host filesystem:
${APP_DATA_STORAGE_DIR}/
└── <user_id>/
    └── <bundle_id>/
        ├── storage/
        ├── uploads/
        ├── cache/
        └── memory/
```

## Integration Points

| Feature | Relationship |
|---------|-------------|
| [Agent Bundles & Installs](../agent_bundles/agent_bundles.md) | Every install triggers `get_or_create_volume`; uninstall calls `mark_orphaned` |
| [Agent Environments](../agent_environments/agent_environments.md) | `EnvironmentLifecycleManager` resolves `AppDataVolume.host_path` and passes it to `_generate_compose_file` as `APP_DATA_HOST_PATH`; the docker-compose template bind-mounts it at `/app/workspace/app-data` |
| [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md) | App Data is the "persistent per-user" classification; never overwritten by bundle updates or rebuilds |
| [User Roles](../../application/user_roles/user_roles.md) | App Data management is available to all authenticated users regardless of role |
