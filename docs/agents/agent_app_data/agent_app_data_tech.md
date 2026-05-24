# Agent App Data — Technical Reference

## File Locations

### Model
- `backend/app/models/bundles/app_data_volume.py` — `AppDataVolume`, `AppDataVolumePublic`, `AppDataVolumesPublic`

### Service
- `backend/app/services/bundles/app_data_service.py` — `AppDataService`
- `backend/app/services/bundles/app_data_orphan_scheduler.py` — daily orphan reporter (APScheduler job)
- `backend/app/services/bundles/app_data_gc_scheduler.py` — on-disk orphan GC (APScheduler job, every 6h); started/stopped in `app/main.py` lifespan

### API Route
- `backend/app/api/routes/app_data.py` — mounted at `/api/v1/users/me/app-data`

### Frontend
- `frontend/src/components/UserSettings/AppData/AppDataTab.tsx` — Settings → App Data tab
- `frontend/src/components/UserSettings/AppData/AppDataRow.tsx` — single volume row with actions
- `frontend/src/routes/_layout/settings.tsx` — registers the `app-data` tab at `{ value: "app-data", title: "App Data" }`

## Database Schema

### `app_data_volume`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `user_id` | UUID FK → user ON DELETE CASCADE | |
| `bundle_id` | varchar(255) NOT NULL | Reverse-DNS string (NOT the bundle UUID) |
| `volume_name` | varchar(255) UNIQUE NOT NULL | `appdata_<user_id_hex[:8]>_<sanitized_bundle_id>` capped at 240 chars |
| `host_path` | varchar(1024) NOT NULL | Absolute path under `APP_DATA_STORAGE_DIR` on the host |
| `size_bytes` | int DEFAULT 0 | Lazily updated |
| `last_size_check_at` | timestamp | |
| `catalog_type` | varchar NULLABLE | `NULL` for publisher/owned installs; `"server"` for installs sourced from this instance's catalog. Plain string column — no enum — so future values (`"marketplace"`, `"remote:<host>"`) can be added without a schema change |
| `current_install_id` | UUID FK → agent ON DELETE SET NULL | Null when orphaned |
| `is_orphaned` | bool DEFAULT false | True when no install references this volume |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

Unique constraint: `uq_app_data_user_bundle_catalog` on `(user_id, bundle_id, catalog_type)`. Postgres treats `NULL` values as distinct in this constraint, so a publisher's `NULL` slot coexists with their consumer `"server"` slot without collision.

Indexes: `ix_app_data_volume_user_id`, `ix_app_data_volume_bundle_id`, `ix_app_data_volume_orphaned` (partial `WHERE is_orphaned = true`).

## API Endpoints

All mounted under `/api/v1/users/me/app-data`. Owner-only — no admin override.

| Method | Path | Response | Notes |
|--------|------|----------|-------|
| `GET` | `/users/me/app-data` | `AppDataVolumesPublic` | Lists volumes with resolved install name |
| `POST` | `/users/me/app-data/{volume_id}/recompute-size` | `AppDataVolumePublic` | Walks directory, updates `size_bytes` |
| `DELETE` | `/users/me/app-data/{volume_id}` | 204 | 409 if `is_orphaned = false` |

### `AppDataVolumePublic`
```python
id: UUID
bundle_id: str          # reverse-DNS string
volume_name: str
size_bytes: int
last_size_check_at: datetime | None
current_install_id: UUID | None
current_install_name: str | None  # resolved from Agent.name join
is_orphaned: bool
created_at: datetime
updated_at: datetime
```

## AppDataService Key Methods

| Method | Notes |
|--------|-------|
| `storage_root() -> Path` | `Path(settings.APP_DATA_STORAGE_DIR)` |
| `host_storage_root() -> Path` | Uses `HOST_APP_DATA_DIR` when set (Docker-in-Docker) |
| `host_path_for(user_id, bundle_id, catalog_type) -> Path` | `<host_root>/<user_id>/<bundle_id>` |
| `container_path_for(user_id, bundle_id, catalog_type) -> Path` | `<storage_root>/<user_id>/<bundle_id>` |
| `get_by_id(session, volume_id)` | |
| `get_for_user(session, volume_id, user_id)` | Returns None for wrong-owner (no 403 leak) |
| `get_by_user_bundle(session, user_id, bundle_id, catalog_type)` | Lookup by `(user_id, bundle_id, catalog_type)` |
| `get_install_name(session, volume)` | Resolves linked `Agent.name` |
| `list_user_volumes(session, user_id)` | Returns `[(volume, install_name|None)]` via outer join |
| `get_or_create_volume(session, user_id, bundle_id, catalog_type, current_install_id)` | Idempotent; reuses and un-orphans existing row by matching all three key fields; creates `storage/`, `uploads/`, `cache/` |
| `mark_orphaned(session, volume)` | Sets `is_orphaned=True`, clears `current_install_id` |
| `wipe_volume(session, volume)` | Raises ValueError if `is_orphaned=False`; best-effort `rmtree`; deletes row |
| `recompute_size(session, volume) -> int` | `os.scandir` walk; persists result |
| `find_orphans_older_than(session, days)` | Used by the daily reporter |
| `find_orphan_dirs(session, grace=1d) -> list[Path]` | Diffs the on-disk tree against the DB; returns container-side dirs with no DB row (deleted-user trees + bundle subtrees under live users with no matching volume). Skips dirs modified within `grace` and non-UUID top-level dirs. Read-only |
| `purge_orphan_dirs(session, grace=1d) -> (removed, failed)` | `rmtree`s every dir from `find_orphan_dirs`; best-effort, returns counts. Called by the GC scheduler |

### Path Translation (Docker-in-Docker)

When the backend runs inside a container and shells out to Docker on the host (the standard dev/prod setup), `HOST_APP_DATA_DIR` **must** be set so `host_path_for()` produces a path the host can bind-mount. The backend container sees app-data at `APP_DATA_STORAGE_DIR/<rel>`; the docker-compose side uses `HOST_APP_DATA_DIR/<rel>`. `_container_path_from_volume` translates the stored `host_path` back to the container-visible path for I/O operations (size walk, wipe). Only when the backend runs directly on the host (no container, no docker socket) are host and container paths identical and `HOST_APP_DATA_DIR` can be left empty.

### Directory Creation

`_ensure_directory_tree(container_path)` creates `storage/`, `uploads/`, `cache/` with `mode=0o755` using `mkdir(parents=True, exist_ok=True)`. Called on every `get_or_create_volume` call (idempotent) to handle out-of-band directory removal.

## Filesystem Layout

```
${APP_DATA_STORAGE_DIR}/              # container-side path
└── <user_uuid>/
    ├── io.opencinna.cinna.a1b2c3d4/  # bundle_id string used as dir name
    │   ├── storage/
    │   ├── uploads/
    │   └── cache/
    └── io.opencinna.cinna.deadbeef/
        ├── storage/
        ├── uploads/
        └── cache/
```

Host-side (docker-compose) bind mount in the generated compose file:
```yaml
volumes:
  - ${APP_DATA_HOST_PATH}:/app/workspace/app-data:rw
```

`APP_DATA_HOST_PATH` is substituted from `AppDataVolume.host_path` when `EnvironmentLifecycleManager._generate_compose_file()` renders the template. For agents without a bundle (legacy / unpublished), the variable falls back to a per-environment empty directory under `<env_dir>/app-data/`.

## Volume Name Format

`appdata_<user_id.hex[:8]>_<sanitized_bundle_id>` truncated to 240 characters.

Non-alphanumeric/dash/dot characters in `bundle_id` are replaced with `_`. Example:
- `user_id = 550e8400-e29b-41d4-a716-446655440000`
- `bundle_id = "io.opencinna.cinna.a1b2c3d4"`
- `volume_name = "appdata_550e8400_io.opencinna.cinna.a1b2c3d4"` (truncated if long)

## Daily Orphan Reporter

`app_data_orphan_scheduler.py` schedules a daily APScheduler job that calls `AppDataService.find_orphans_older_than(session, days=90)` and logs results. It does **not** delete volumes — deletion is always user-initiated via the Settings tab. The threshold of 90 days is a reporting boundary only; data is never auto-deleted.

## On-Disk Orphan GC

`app_data_gc_scheduler.py` schedules an APScheduler job (every `GC_INTERVAL_HOURS = 6`) that calls `AppDataService.purge_orphan_dirs(session)`. This is distinct from the orphan *reporter*: the reporter surfaces `AppDataVolume` rows flagged `is_orphaned` that still exist; the GC deletes on-disk directories that have **no row at all**.

The trigger is account/install deletion: `app_data_volume.user_id` is `ON DELETE CASCADE`, so deleting a `User` removes their volume rows but leaves the `<APP_DATA_STORAGE_DIR>/<user_id>/` tree on disk. Doing the recursive delete inline in `delete_user_me` / `delete_user` could stall the request on a large workspace, so cleanup is deferred to this job.

`find_orphan_dirs` first lists the UUID-named top-level dirs actually present on disk, then scopes its DB lookups to exactly those users — `select(User.id).where(User.id.in_(...))` and `select(AppDataVolume).where(AppDataVolume.user_id.in_(...))` — so cost tracks on-disk dirs rather than the full `user` / `app_data_volume` tables. Each live volume's container path (`_container_path_from_volume`) plus all of its `.parents` are flattened into one `live_dirs` set, making "a row equals or nests under this bundle dir" an O(1) membership test. It then walks the storage root:
- **Top-level `<user_id>` dir** whose name is a UUID not in the live user set → the whole tree is an orphan (deleted user).
- **Bundle subtree under a live user** not present in `live_dirs` → orphan. The `_<catalog_type>` consumer slot path nests under its parent bundle dir (so the parent lands in `live_dirs` via `.parents`), so a bundle dir hosting only a consumer slot is correctly retained.
- Non-UUID top-level dirs are skipped (never an operator's stray dir gets nuked).
- A `grace` window (`ORPHAN_DIR_GRACE = 1 day`) skips dirs whose mtime is newer than the cutoff, so an in-flight install (directory created before its row commits) is never reclaimed.

`purge_orphan_dirs` removes each result via the shared `_best_effort_rmtree` helper (also used by `wipe_volume`) — failures are logged, never raised — and returns `(removed, failed)`. Unit coverage: `backend/tests/unit/test_app_data_gc.py`.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `APP_DATA_STORAGE_DIR` | `/app/data/app-data` | Container-side root |
| `HOST_APP_DATA_DIR` | `""` (plain) / `./backend/data/agents/app-data` (compose) | Host-side root used when generating the agent compose's `${APP_DATA_HOST_PATH}` bind-mount source. Required whenever the backend runs in a container; the project `docker-compose.yml` provides the default. The override file pins it to an absolute `${PWD}/backend/data/agents/app-data` so the rendered agent compose has a path Docker can resolve regardless of CWD |

### docker-compose Wiring

The backend service in the project `docker-compose.yml` must bind-mount the host app-data root at `/app/data/app-data` so the same directory is visible from both sides:

- **Backend env** — `HOST_APP_DATA_DIR=${HOST_APP_DATA_DIR:-./backend/data/agents/app-data}`
- **Backend volume** — `${HOST_APP_DATA_DIR:-./backend/data/agents/app-data}:/app/data/app-data`
- **Override** — `docker-compose.override.yml` sets `HOST_APP_DATA_DIR: "${PWD}/backend/data/agents/app-data"` (absolute path so the agent-side compose, run from a different CWD on the host, resolves correctly)

Without these, `AppDataService.host_path_for()` falls back to the container-side path, which Docker on the host then refuses to bind-mount with `Mounts denied: ... is not shared from the host`.

## Security

- Route handlers call `AppDataService.get_for_user(session, volume_id, current_user.id)` which returns None for wrong-owner rows rather than 403 — avoids leaking existence of other users' volumes
- Wipe is server-side gated on `is_orphaned = true`; the UI check is additional protection but not relied on
- App-data tab never displays file contents, only sizes and bundle IDs
