# Agent Bundles & Installs — Implementation Plan

## Overview

Replace the current clone-based agent sharing model with a desktop-app-style **bundle / install** model. An agent becomes a versioned, publisher-owned **bundle** identified by a stable reverse-DNS ID (`io.<reversed-instance-host>.<slug>`). Other users **install** the bundle, getting their own running copy plus a separate, persistent **App Data** area scoped to `(user × bundle_id)` that survives uninstall and reattaches on reinstall — so publishers can ship safe updates without ever clobbering user state.

Three orthogonal subsystems land together:

- **App Data** — new persistent volume mounted at `/app/workspace/app-data/{storage,uploads,cache}`, keyed by `(user_id, bundle_id)`, survives install deletion.
- **Bundle / Install** — new `AgentBundle` + `AgentBundleRevision` entities; the existing `Agent` table becomes the "Install" record (publisher's working copy + every user copy). `AgentShare`, `CloneUpdateRequest`, and clone fields are removed.
- **Roles** — new `agent-user` / `agent-developer` / `admin` enum on `User`. All non-admin users default to `agent-user` (read-only catalog + conversation). `agent-developer` is admin-promoted and unlocks today's full UI.

```
┌────────────────────────┐         publish (snapshot)         ┌─────────────────────────┐
│  Publisher's Install   │ ───────────────────────────────►   │  AgentBundleRevision N  │
│  (regular Agent row)   │                                    │  /var/cinna/bundles/... │
└────────────────────────┘                                    └────────────┬────────────┘
                                                                           │ install / push-update
                                                                           ▼
                                                              ┌─────────────────────────┐
                                                              │   Other user's Install  │
                                                              │   workspace = bundle    │
                                                              │   app-data = persistent │
                                                              └─────────────────────────┘
```

---

## Architecture Overview

### System components

```
                        ┌────────────────────────────────────────────┐
                        │  Backend (FastAPI)                          │
                        │                                            │
                        │  AgentBundleService     RoleService        │
                        │  AgentInstallService    AppDataService     │
                        │  PublishService         CatalogService     │
                        │           │                  │             │
                        │           ▼                  ▼             │
                        │  AgentBundle / Revision   AppDataVolume    │
                        │  Agent (= Install)        UserRole         │
                        └─────┬────────────────────────────┬─────────┘
                              │                            │
                              ▼                            ▼
                  /var/cinna/bundles/             /var/cinna/app-data/
                  <bundle_id>/<rev>/              <user_id>/<bundle_id>/
                       (scripts, docs,                (storage/, uploads/,
                        knowledge, files,              cache/)
                        manifest.json)
                              │                            │
                              └─────────► Install env ◄────┘
                                          /app/workspace/
                                          ├── scripts/   (bundle)
                                          ├── docs/      (bundle)
                                          ├── knowledge/ (bundle)
                                          ├── files/     (bundle, static)
                                          ├── credentials/ (synced)
                                          └── app-data/  (per-user volume)
                                              ├── storage/
                                              ├── uploads/
                                              └── cache/
```

### Data flow — publish

```
agent-developer  ──► POST /agents/{id}/publish ──► PublishService
                                                       │
                                                       ├─ snapshot bundle folders from install workspace
                                                       │  (scripts/, docs/, knowledge/, files/,
                                                       │   workspace_requirements.txt, workspace_system_packages.txt)
                                                       │
                                                       ├─ write to /var/cinna/bundles/<bundle_id>/<rev>/
                                                       │
                                                       ├─ create AgentBundleRevision row
                                                       │  (revision_number, bundle_id, manifest, prompts)
                                                       │
                                                       └─ mark AgentBundle.latest_revision_id
```

### Data flow — install

```
agent-user  ──► POST /catalog/{bundle_id}/install ──► AgentInstallService
                                                          │
                                                          ├─ create Agent row with bundle_id + bundle_revision_id
                                                          ├─ create AgentEnvironment (existing flow)
                                                          ├─ ensure AppDataVolume(user_id, bundle_id)
                                                          │  - reuse if orphaned, else create
                                                          ├─ copy bundle revision content into env workspace
                                                          ├─ mount app-data volume at /app/workspace/app-data
                                                          └─ start environment
```

### Data flow — push update

```
publisher publishes new revision ──► PublishService.notify_installs()
                                          │
                                          └─► for each Install in (auto, manual):
                                                 ├─ auto: schedule replace_bundle_content() job
                                                 │     └─ replace bundle folders from new revision
                                                 │        preserve app-data
                                                 │        rebuild env
                                                 └─ manual: set pending_update flag + emit WS event
```

### Data flow — uninstall / reinstall

```
agent-user uninstalls Install
   ├─ delete AgentEnvironment (DOWN -v on bundle workspace volume)
   ├─ delete Agent (Install) row
   └─ AppDataVolume row marked orphaned=true (NOT deleted)

agent-user reinstalls same bundle_id
   └─ AppDataVolume(user_id, bundle_id) found orphaned → reattached, orphaned=false
```

---

## Data Models

### New: `AgentBundle`

Canonical bundle metadata, owned by a publisher. One row per published bundle on this instance.

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `id` | UUID | PK | |
| `bundle_id` | str | unique, indexed, max 255 | reverse-DNS form `io.<reversed-host>.<slug>` |
| `display_name` | str | not null, max 255 | initial name = publisher install name; mutable |
| `description` | str \| null | | last-published description |
| `publisher_user_id` | UUID | FK → user, ON DELETE RESTRICT | cannot delete user with published bundles |
| `latest_revision_id` | UUID \| null | FK → agent_bundle_revision, ON DELETE SET NULL | most recent published revision |
| `is_listed` | bool | default false | shown in instance catalog |
| `visibility` | str | default `"private"` | `"private"`, `"users"`, `"public"` |
| `default_install_mode` | str | default `"manual"` | `"manual"` or `"automatic"` for new installs (update mode) |
| `created_at` | datetime | | |
| `updated_at` | datetime | | |

Indexes: `ix_agent_bundle_publisher`, `ix_agent_bundle_listed_visibility` (partial: `is_listed = true`).

### New: `AgentBundleRevision`

Immutable snapshot of a bundle's content. Each `Publish` action creates one row.

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `id` | UUID | PK | |
| `bundle_id` | UUID | FK → agent_bundle, ON DELETE CASCADE | |
| `revision_number` | int | unique within bundle | monotonically increasing per bundle |
| `manifest` | JSON | not null | snapshot prompts, requirements summary, content hash, sdk config |
| `workflow_prompt` | str \| null | | snapshot copy from publisher install at publish time |
| `entrypoint_prompt` | str \| null | | |
| `refiner_prompt` | str (text) \| null | | |
| `agent_sdk_building` | str | | snapshot |
| `agent_sdk_conversation` | str | | snapshot |
| `model_override_building` | str \| null | | snapshot |
| `model_override_conversation` | str \| null | | snapshot |
| `required_credential_specs` | JSON | default `[]` | list of `{name, type, allow_sharing}` for prompting at install |
| `snapshot_path` | str | not null | absolute path under `BUNDLE_STORAGE_DIR` |
| `content_hash` | str | not null, max 64 | SHA-256 over snapshot tree for cache busting / dedup |
| `published_by_user_id` | UUID | FK → user, ON DELETE SET NULL | |
| `published_at` | datetime | not null | |
| `release_notes` | str \| null | (text) | optional changelog |

Indexes: `ix_revision_bundle`, unique `(bundle_id, revision_number)`.

Lifecycle: revisions are append-only; they are deleted only when the parent bundle is deleted (cascade). Old revisions can be GC'd later — out of scope.

### New: `AppDataVolume`

Per-user, per-bundle persistent storage record. Survives Install deletion.

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `id` | UUID | PK | |
| `user_id` | UUID | FK → user, ON DELETE CASCADE | |
| `bundle_id` | str | indexed, max 255 | reverse-DNS bundle identifier (NOT the bundle UUID — keeps stable across bundle deletes) |
| `volume_name` | str | not null, unique, max 255 | docker volume name e.g. `appdata_<user_id_hex_short>_<bundle_slug>` |
| `host_path` | str | not null | absolute path under `APP_DATA_STORAGE_DIR` |
| `size_bytes` | int | default 0 | last computed size, updated lazily |
| `last_size_check_at` | datetime \| null | | |
| `current_install_id` | UUID \| null | FK → agent (install), ON DELETE SET NULL | which install is currently using it (if any) |
| `is_orphaned` | bool | default false | true when no install references it |
| `created_at` | datetime | | |
| `updated_at` | datetime | | |

Unique constraint: `uq_app_data_user_bundle` on `(user_id, bundle_id)` — exactly one volume per user × bundle.

### Modified: `Agent` (now an "Install")

Drop clone fields, add bundle linkage. Existing `id`, `owner_id`, `user_workspace_id`, prompts, SDK fields, etc., remain.

**Add**:
| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `bundle_id` | str | not null, indexed, max 255 | reverse-DNS bundle identifier; auto-generated on creation |
| `bundle_uuid` | UUID \| null | FK → agent_bundle, ON DELETE SET NULL | links to bundle row when one exists; null for unpublished agents (the publisher's own draft install with no bundle yet) |
| `installed_revision_id` | UUID \| null | FK → agent_bundle_revision, ON DELETE SET NULL | which revision this install was created from / last synced to |
| `is_publisher_install` | bool | default false | true on the install owned by the bundle's publisher |
| `update_mode` | str | default `"manual"` | repurposed from clone field: `"manual"` or `"automatic"` |
| `pending_update` | bool | default false | repurposed |
| `pending_update_at` | datetime \| null | | repurposed |
| `last_sync_at` | datetime \| null | | repurposed |
| `last_update_status` | str \| null | | repurposed |

**Drop** (via migration, after move to new model):
- `is_clone`, `parent_agent_id`, `clone_mode`
- All clone-related indexes (`ix_agent_is_clone`, `ix_agent_parent`)
- The named FK `fk_agent_parent`
- `fk_agent_parent` self-referential constraint

Add unique constraint `uq_agent_bundle_id_per_publisher` on `(owner_id, bundle_id)` — one publisher install per bundle (everyone else's install of that bundle has a different `owner_id`). Plus a partial unique constraint on `bundle_uuid` where `is_publisher_install = true` — exactly one publisher install per bundle.

### New: `UserRole` (enum on `User`)

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `role` | str | default `"agent-user"`, max 32 | enum: `"agent-user"`, `"agent-developer"`, `"admin"` |

`is_superuser` continues to drive `admin` privileges (mapping: `admin` ⇔ `is_superuser=true`). The `role` field is for the agent-user / agent-developer distinction. Non-superusers default to `agent-user` on migration.

Migration: existing non-superuser rows → `role = 'agent-user'`; existing superusers → `role = 'admin'` (or simply mirror `is_superuser` and treat absent role as `agent-user` for non-supers).

### Removed entities (Phase 2 migration)

- `AgentShare` (table `agent_share`)
- `CloneUpdateRequest` (table `clone_update_request`)
- `AgentGuestShare` clone semantics — guest sharing model is preserved (token-based access to a user's install for unauthenticated viewers); only the clone-related plumbing is removed.

Migration drops these tables and their FKs after a one-shot translation step in the migration: every existing accepted clone agent row keeps its data but loses the `is_clone`/`parent_agent_id` link. (Per the brief: no backward compatibility — clones simply become standalone installs without a bundle reference.)

---

## Security Architecture

### Bundle ID generation & uniqueness

- Default format: `io.<reversed-host>.<short-uuid>` where:
  - `reversed-host` = `settings.FRONTEND_HOST` parsed → host → split on `.` → reversed → joined (e.g. `cinna.opencinna.io` → `io.opencinna.cinna`).
  - `short-uuid` = first 8 hex chars of the agent UUID (`uuid.uuid4().hex[:8]`).
- Final form e.g. `io.opencinna.cinna.a1b2c3d4`.
- Editable by `agent-developer` via a dedicated endpoint, with these rules:
  - Format check: `^[a-zA-Z0-9]([a-zA-Z0-9.\-]{1,253})$` (DNS-like; periods + dashes allowed).
  - Per-instance unique constraint at DB level on the `Agent.bundle_id` and `AgentBundle.bundle_id` columns.
  - Once the agent is published (a bundle row exists), changing `bundle_id` is **prohibited** — would silently orphan installed app-data.
  - Reverse-DNS conventions documented; not enforced beyond format check (publishers must agree their slugs make sense).

### Encryption

- No new encrypted fields. Bundle content (scripts, prompts, knowledge) is plain on disk under `BUNDLE_STORAGE_DIR` — same trust level as today's environment workspace.
- `required_credential_specs` carries metadata only (name, type, optional description). No secret values ever stored in bundles.
- App Data volumes contain whatever the agent writes; treated like today's workspace for encryption purposes (none at rest in this iteration; documented as user responsibility).

### Access control

- **Bundle CRUD**: only `agent-developer` (or `admin`) on the publisher install. Read access for `is_listed` bundles in the catalog respects `visibility`:
  - `private`: publisher only.
  - `users`: explicit allowlist via new `BundleAccessGrant` table (Phase 3).
  - `public`: all `agent-user`/`agent-developer` accounts on this instance.
- **Install lifecycle**: install owner only (the user who installed). Admins can list / force-uninstall via admin console.
- **App Data management**: owner only — no cross-user access, no admin override (matches "private profile" framing).
- **Publish action**: gated on `agent-developer` role + ownership of the install + at least one validated credential spec (placeholder allowed).
- **Role transitions**: `admin` only (existing `is_superuser` guard).

### Input validation

- `bundle_id` regex on creation/edit; reject reserved prefixes (`io.opencinna.system.*`).
- Bundle revision uploads (when we add `cinna-compose` later) will need stricter validation; for now, snapshot is server-side only.
- `BundleAccessGrant` email lookups validate the target user exists on this instance (no off-instance grants).

### Sensitive data exposure

- Public catalog response **must not** include the publisher's email or user UUID; only `display_name` and a publisher handle (truncated UUID or username when available).
- App Data tab shows only sizes and bundle IDs; never lists file contents.
- Bundle revisions never embed credential values, only `required_credential_specs` (names + types).

---

## Backend Implementation

### API Routes

#### Roles & permissions (Phase 3)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/v1/users/me/role` | CurrentUser | Returns current user role |
| `PATCH` | `/api/v1/users/{user_id}/role` | admin | Change role (`agent-user` ↔ `agent-developer`) |
| `GET` | `/api/v1/users/?role=agent-developer` | admin | List by role for promotion UI |

Existing `agents.*` endpoints are gated additionally on `agent-developer` for create/update/delete; `agent-user` may only call install / uninstall / list-installed / send-message.

#### Bundles & catalog (Phase 2)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/v1/agents/{agent_id}/publish` | agent-developer + owner | Snapshot install → create new revision |
| `GET` | `/api/v1/bundles` | agent-developer | List bundles owned by current user |
| `GET` | `/api/v1/bundles/{bundle_id}` | publisher / catalog viewer | Bundle detail (with revisions list) |
| `PATCH` | `/api/v1/bundles/{bundle_id}` | publisher | Update visibility, listing, default_install_mode, display_name |
| `DELETE` | `/api/v1/bundles/{bundle_id}` | publisher | Remove bundle (only if no foreign installs) |
| `GET` | `/api/v1/bundles/{bundle_id}/revisions` | publisher | List revisions |
| `POST` | `/api/v1/bundles/{bundle_id}/grants` | publisher | Add user grant (email lookup) |
| `DELETE` | `/api/v1/bundles/{bundle_id}/grants/{grant_id}` | publisher | Revoke grant |
| `GET` | `/api/v1/catalog` | any user | List bundles installable by current user (public + grants) |
| `POST` | `/api/v1/catalog/{bundle_id}/install` | agent-user / agent-developer | Install — creates Agent row + env + app-data |
| `POST` | `/api/v1/catalog/{bundle_id}/admin-install` | admin | Install for a specific user |

#### Installs (Phase 2)

Existing `/api/v1/agents/*` endpoints continue to work — `Agent` row IS the install. Add:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/v1/agents/{agent_id}/uninstall` | install owner | Delete env + Agent; mark app-data orphaned |
| `POST` | `/api/v1/agents/{agent_id}/apply-update` | install owner | Pull latest revision into install workspace |
| `POST` | `/api/v1/agents/{agent_id}/check-updates` | install owner | Returns `pending_update`, `installed_revision_number`, `latest_revision_number` |
| `PATCH` | `/api/v1/agents/{agent_id}/update-mode` | install owner | Toggle automatic/manual |
| `PATCH` | `/api/v1/agents/{agent_id}/bundle-id` | publisher install only, pre-publish | Edit bundle_id |

Drop:
- `POST /api/v1/agents/{id}/shares`
- `GET /api/v1/agents/{id}/shares`
- `GET /api/v1/agents/{id}/clones`
- `DELETE /api/v1/agents/{id}/shares/{id}` (revoke)
- `GET /api/v1/shares/pending`
- `POST /api/v1/shares/{id}/accept`
- `POST /api/v1/shares/{id}/decline`
- `POST /api/v1/agents/{id}/detach`
- `POST /api/v1/agents/{id}/shares/push-updates`
- `GET /api/v1/agents/{id}/update-requests`
- `POST /api/v1/update-requests/{id}/dismiss`

#### App Data (Phase 1)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/v1/users/me/app-data` | CurrentUser | List `AppDataVolume` rows for the user, with size + linked install + orphaned flag |
| `DELETE` | `/api/v1/users/me/app-data/{volume_id}` | CurrentUser | Wipe an orphaned volume |
| `POST` | `/api/v1/users/me/app-data/{volume_id}/recompute-size` | CurrentUser | Force size recompute (lazy by default) |

### Service Layer

#### `BundleService` — `backend/app/services/bundles/bundle_service.py`

- `generate_bundle_id(agent: Agent) -> str` — compose default reverse-DNS ID from `settings.FRONTEND_HOST` + `agent.id[:8]`.
- `get_bundle_by_id(session, bundle_id: str) -> AgentBundle | None`
- `list_publisher_bundles(session, user_id: UUID) -> list[AgentBundle]`
- `update_bundle(session, bundle: AgentBundle, *, visibility, is_listed, default_install_mode, display_name) -> AgentBundle`
- `delete_bundle(session, bundle: AgentBundle) -> None` — only if `count(installs) == 1` (just the publisher's).

#### `PublishService` — `backend/app/services/bundles/publish_service.py`

- `publish(session, install: Agent, release_notes: str | None) -> AgentBundleRevision`:
  1. Validate install is the publisher install (or first publish — promotes install to publisher install + creates `AgentBundle`).
  2. Read bundle folders from install env workspace (`scripts/`, `docs/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt`).
  3. Compute content hash (SHA-256 over canonical tree).
  4. Snapshot to `<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/`.
  5. Insert `AgentBundleRevision` with manifest: `{"prompts": {...}, "sdk": {...}, "content_hash": "..."}`.
  6. Bump `bundle.latest_revision_id`, fire `BUNDLE_PUBLISHED` event.
  7. Enqueue `notify_installs(bundle_id, revision_id)` — push update job per dependent install.
- `notify_installs(bundle_id, revision_id)`:
  - For each non-publisher install: if `update_mode == "automatic"` and the install env is suspended → schedule `apply_update()` immediately; else mark `pending_update=true`, `pending_update_at=now()`, emit `INSTALL_UPDATE_AVAILABLE` WebSocket event.
- `delete_revision(session, revision)` — admin only, refuses if any install references it.

#### `InstallService` — `backend/app/services/bundles/install_service.py`

- `install_bundle(session, user, bundle, ai_credentials, granted_credential_values) -> Agent`:
  1. Reuse or create `AppDataVolume(user_id, bundle.bundle_id)` — clear `is_orphaned`, set `current_install_id = new_agent.id` after step 4.
  2. Create new `Agent` row: copy fields from latest revision (prompts, SDK), link `bundle_uuid`, `bundle_id`, `installed_revision_id`, `update_mode = bundle.default_install_mode`, `is_publisher_install=False`.
  3. Create `AgentEnvironment` for the new install via existing `EnvironmentLifecycleManager.create_environment_instance`.
  4. Trigger workspace seed: copy bundle revision contents from `revision.snapshot_path` → install env workspace.
  5. Set up credentials: for each `required_credential_spec` either link an existing user credential or create a placeholder.
  6. Mount the app-data volume into compose (see "Volume mounting").
  7. Start environment.
- `apply_update(session, install: Agent) -> None`:
  - Stop env → replace bundle folders from `bundle.latest_revision.snapshot_path` (preserve `app-data/`, `credentials/`) → trigger rebuild → set `installed_revision_id`, clear `pending_update`, set `last_sync_at`.
- `uninstall(session, install: Agent) -> None`:
  - Delete env (DOWN -v on bundle workspace volume only — app-data volume is **not** removed).
  - Mark `AppDataVolume.is_orphaned = true`, `current_install_id = NULL`.
  - Delete `Agent` row.
- `check_for_updates(session, install: Agent) -> dict`.
- `set_update_mode(session, install, mode)`.

#### `AppDataService` — `backend/app/services/bundles/app_data_service.py`

- `get_or_create_volume(session, user_id, bundle_id) -> AppDataVolume`:
  1. Look up `(user_id, bundle_id)` — return if exists, mark non-orphaned.
  2. Else create `AppDataVolume`, allocate host path under `APP_DATA_STORAGE_DIR/<user_id>/<bundle_id>/`, create directory tree (`storage/`, `uploads/`, `cache/`).
  3. Create Docker named volume backing the host path (or use bind mount — see infra notes).
- `list_user_volumes(session, user_id) -> list[AppDataVolume]` — joined with current install for display.
- `wipe_volume(session, volume) -> None`:
  - Refuse if `is_orphaned == false` and current install env is running (require uninstall first).
  - Remove host directory + Docker volume.
  - Hard-delete row.
- `recompute_size(session, volume) -> int`:
  - Walk host path, sum sizes (skip via `os.scandir` for speed). Persist `size_bytes`, `last_size_check_at`.

#### `RoleService` — `backend/app/services/users/role_service.py`

- `set_role(session, target_user, new_role, by_admin)` — validates transitions, updates `User.role`, fires `USER_ROLE_CHANGED` event.
- `require_developer(user) -> None` — raises 403 if role not in `(agent-developer, admin)`.
- `require_user(user) -> None` — sanity check; `agent-user` permitted (everyone with valid auth).

#### `CatalogService` — `backend/app/services/bundles/catalog_service.py`

- `list_for_user(session, user) -> list[CatalogEntryPublic]`:
  - Returns `AgentBundle` rows where:
    - `visibility = 'public' AND is_listed = true`, OR
    - `visibility = 'users' AND is_listed = true AND exists BundleAccessGrant(bundle_id, user_id)`.
  - Each entry includes: bundle_id, display_name, description, publisher handle, latest revision metadata, install count, "already installed" flag.

### Volume Mounting Strategy

Modify `docker-compose.template.yml` for both templates to add an additional volume:

```yaml
volumes:
  - ${APP_DATA_HOST_PATH}:/app/workspace/app-data:rw
```

`EnvironmentLifecycleManager._generate_compose_file()` learns to substitute `${APP_DATA_HOST_PATH}` when generating compose — resolved from the install's `AppDataVolume.host_path`. If no `bundle_id` (legacy / unpublished early state) the variable is set to a per-environment empty directory under `<env_dir>/app-data/` so the agent always sees the path.

### Bundle storage layout (filesystem)

```
${BUNDLE_STORAGE_DIR}/                     # new config: defaults to <DATA_DIR>/bundles/
├── io.opencinna.cinna.a1b2c3d4/           # one dir per bundle_id
│   ├── 1/                                 # one dir per revision_number
│   │   ├── manifest.json
│   │   ├── scripts/
│   │   ├── docs/
│   │   ├── knowledge/
│   │   ├── files/
│   │   ├── workspace_requirements.txt
│   │   └── workspace_system_packages.txt
│   └── 2/
│       └── ...
└── io.opencinna.cinna.deadbeef/
    └── ...
```

```
${APP_DATA_STORAGE_DIR}/                   # new config: defaults to <DATA_DIR>/app-data/
├── <user_id_short>/
│   ├── <bundle_id>/
│   │   ├── storage/
│   │   ├── uploads/
│   │   └── cache/
│   └── <other_bundle_id>/
└── <other_user_id>/
```

Both directories created and managed by services with `os.makedirs(..., exist_ok=True)` and proper permissions (`mode=0o755`). Docker bind-mounts the per-install app-data path into the container.

### Background tasks

- **`PublishService.notify_installs`** — fired post-publish. Async fanout to N installs. Idempotent on `(install_id, revision_id)`.
- **`AppDataService.cleanup_orphaned`** — daily APScheduler job. Logs orphaned volumes older than 90 days but does NOT delete (deletion is user-driven). Surfaces via app-data tab.
- **`auto_apply_updates`** — extends existing `EnvironmentSuspensionScheduler` to call `InstallService.apply_update()` for installs with `update_mode='automatic'` and `pending_update=true` when env is about to suspend. Replaces today's `AgentCloneService.check_and_apply_automatic_updates`.

### Error handling

- **Publish failures** mid-snapshot: rollback `AgentBundleRevision` insert, leave any partial directory tree under `<snapshot_path>.tmp` for debugging; bundle's `latest_revision_id` unchanged.
- **Install failures** mid-flow: best-effort rollback (env delete, Install delete), but **never** delete the app-data volume — it's the user's data, even on first install where it was just created. Re-install reuses it.
- **bundle_id collision** on publish: 409 Conflict with explicit message; surface in publisher UI.
- **Apply update failures**: leave `installed_revision_id` unchanged, set `last_update_status='failed'`, fire `INSTALL_UPDATE_FAILED` event.
- **Role downgrade** (developer → user): existing agents stay owned by the user; building-mode sessions are blocked at the API layer; no auto-uninstall.

---

## Frontend Implementation

### Role-aware shell

- New top-level guard in `_layout/__root.tsx`: read `currentUser.role`, branch.
  - `agent-user` → mounts `AgentUserLayout` (simplified).
  - `agent-developer` → mounts existing layout (today's full UI).
  - `admin` → existing layout + admin nav (today's flow).

### `agent-user` UI (Phase 3)

New routes:

- `frontend/src/routes/_layout/index.tsx` — already a dashboard; for `agent-user` shows only their installed agents in chat-card form.
- `frontend/src/routes/_layout/catalog.tsx` — **App Catalog**:
  - Grid of bundle cards (name, description, publisher handle, "Install" button or "Open" if installed).
  - Filters: `Public`, `Shared with me`, `Already installed`.
  - Card components: `frontend/src/components/Catalog/CatalogGrid.tsx`, `CatalogCard.tsx`, `CatalogFilters.tsx`.
- `frontend/src/routes/_layout/install/$bundleId.tsx` — **Install Wizard**:
  - Step 1: Overview (display_name, description, publisher, what credentials are needed).
  - Step 2: Credentials — for each `required_credential_spec`, pick existing or create.
  - Step 3: AI credentials — same as today's accept-share wizard.
  - Step 4: Confirm + install.
  - Components: `frontend/src/components/Install/InstallWizard.tsx`, `WizardStepCredentials.tsx`, `WizardStepAICredentials.tsx`, `WizardStepConfirm.tsx`. (Reuse existing `AcceptShareWizard` building blocks — copy and adapt before deletion.)
- `frontend/src/routes/_layout/agent/$agentId.tsx` — **Install detail** (existing route, role-aware):
  - For `agent-user`: only conversation-mode session, list of grantable credentials, "Uninstall" button, "Update available" banner. No prompts editor, no scheduler-builder UI, no integrations tab.
  - For `agent-developer`: today's full tabbed UI plus a new "Bundle" tab.
- `frontend/src/routes/_layout/settings/app-data.tsx` — **Agents App Data tab**:
  - Lists all `AppDataVolume` rows for the user.
  - Columns: bundle_id, display_name (resolved), size, linked install (or "orphaned"), last activity.
  - Per-row actions: Recompute size, Wipe (only on orphaned).
  - Component: `frontend/src/components/Settings/AppDataTab.tsx`, `AppDataRow.tsx`.

### `agent-developer` UI additions (Phase 2)

- `frontend/src/components/Agents/AgentBundleTab.tsx` — new tab on the agent detail page:
  - Bundle ID display (with edit pre-publish).
  - Visibility toggle (private / users / public).
  - "Publish" button → opens `PublishDialog` (release notes optional).
  - Revisions list (revision_number, content_hash short, published_at, install count).
  - Grants table for `users` visibility — add by email (uses `AgentSharesService` patterns adapted to `BundleGrantsService`).
- `frontend/src/components/Agents/PublishDialog.tsx` — confirms snapshot scope and release notes.
- Replace `AgentSharingTab.tsx` with `AgentBundleTab.tsx`. Delete `ShareManagement/*`, `AcceptShareWizard/*`, `CloneManagement/*` (some logic ported into Install wizard before deletion).
- `frontend/src/components/Agents/UpdateAvailableBanner.tsx` — replaces `UpdateBanner.tsx`; shown on installs with `pending_update=true`. Action: apply now (calls `apply-update` endpoint).

### Admin additions (Phase 3)

- `frontend/src/routes/_layout/admin/users.tsx` — new tab "Roles":
  - Table of users with current role, promote/demote actions (calls `PATCH /users/{id}/role`).
  - Component: `RoleManagementTable.tsx`.

### State management

- New React Query keys:
  - `["bundles"]`, `["bundles", bundleId]`, `["bundles", bundleId, "revisions"]`, `["bundles", bundleId, "grants"]`
  - `["catalog"]`
  - `["app-data"]`, `["app-data", volumeId]`
  - `["currentUser"]` already exists; extend to expose `role`.
- Mutations:
  - `usePublishMutation` → invalidates `["bundles"]`, `["catalog"]`, install banners.
  - `useInstallMutation` → invalidates `["agents"]` and `["catalog"]`, navigates to install detail.
  - `useUninstallMutation` → invalidates `["agents"]`, `["app-data"]`.
  - `useApplyUpdateMutation` → invalidates `["agents", agentId]`, env tree.
  - `useChangeRoleMutation` → invalidates user lists.
- WebSocket events to subscribe in `eventService.ts`:
  - `INSTALL_UPDATE_AVAILABLE` — show banner.
  - `INSTALL_UPDATE_APPLIED` — clear banner, refresh env.
  - `BUNDLE_PUBLISHED` — refresh `["bundles"]` for the publisher.
  - `USER_ROLE_CHANGED` — force refetch `["currentUser"]` and re-route on demote.

### User flows

**Install a bundle (agent-user)**:

1. User opens Catalog, sees public bundles + bundles granted to them.
2. Clicks Install on a card → wizard opens.
3. Wizard step 1: overview. Step 2: credentials (placeholders ↔ existing). Step 3: AI credential pick. Step 4: confirm.
4. On submit: API call → install + env build kicked off.
5. Loading state with progress until env activated; redirect to install detail.

**Publish a bundle (agent-developer)**:

1. Developer opens an agent → Bundle tab.
2. Clicks "Publish".
3. PublishDialog prompts: release notes (optional), warning that current workspace state will be snapshotted.
4. Submits → snapshot taken; new revision row shown in list with "current" badge.
5. WS event clears any "draft" status.

**Apply an update (agent-user, manual mode)**:

1. WS event arrives → banner appears on install detail.
2. User reviews release notes (modal lists what's in the new revision).
3. Confirms → env stops, bundle folders replaced, env restarts. App data preserved.

**Wipe orphaned app data**:

1. User opens Settings → App Data.
2. Sees row with "orphaned" badge, e.g. "io.opencinna.cinna.deadbeef — 124 MB".
3. Clicks Wipe → confirmation modal ("This cannot be undone").
4. On confirm: row deleted, volume removed.

### Empty / loading / error states

- Catalog: empty state encourages user to "ask an admin" if no public bundles.
- Install wizard: all steps must validate before "Next"; final step shows summary.
- Update banner: shows install version vs latest, and warning if any of `agent-developer`'s notes mention breaking changes.
- App Data tab: empty state explains what App Data is.

---

## Database Migrations

Migration files use the existing `<rev>_<slug>.py` naming convention.

### Phase 1 — `add_app_data_volumes_and_bundle_id.py`

- Create `app_data_volume` table.
- Add `bundle_id` column to `agent` (NULLABLE during migration; backfill in same migration with auto-generated values; then ALTER NOT NULL).
- Add unique index on `agent.bundle_id` per `(owner_id, bundle_id)`.
- Backfill: for every existing `Agent`, generate `bundle_id` via the same algorithm as `BundleService.generate_bundle_id`.
- Downgrade: drop `bundle_id` and `app_data_volume`.

### Phase 2 — `add_bundles_and_drop_clone_tables.py`

- Create `agent_bundle`, `agent_bundle_revision`, `bundle_access_grant` tables.
- Add `bundle_uuid`, `installed_revision_id`, `is_publisher_install`, repurposed update fields' definitions to `agent` (already present in some form: `update_mode`, `pending_update`, etc. — keep). Drop `is_clone`, `parent_agent_id`, `clone_mode`.
- Drop `agent_share`, `clone_update_request` tables.
- Drop `fk_agent_parent`, `ix_agent_is_clone`, `ix_agent_parent`.
- Add partial unique index on `agent.bundle_uuid` where `is_publisher_install = true`.
- No data backfill into bundles — existing agents stay un-published until owner promotes them via the new "Publish" UI. They retain their workspace and behave like installs of an unpublished bundle (`bundle_uuid IS NULL`).
- Downgrade: complex; recreate share tables empty, re-add clone fields. Document downgrade-not-supported in the migration docstring.

### Phase 3 — `add_user_role.py`

- Add `role` column to `user` (default `'agent-user'`, NOT NULL).
- Backfill: `UPDATE user SET role = 'admin' WHERE is_superuser = TRUE;` rest stay `agent-user`.
- No structural change beyond column; behavior change is enforced by services.
- Downgrade: drop column.

---

## Bundle Manifest Format (current iteration)

`manifest.json` written into each revision snapshot:

```json
{
  "schema_version": 1,
  "bundle_id": "io.opencinna.cinna.a1b2c3d4",
  "revision_number": 3,
  "content_hash": "sha256:...",
  "published_at": "2026-05-06T12:34:56Z",
  "prompts": {
    "workflow": "...",
    "entrypoint": "...",
    "refiner": "..."
  },
  "sdk": {
    "building": "claude-code/anthropic",
    "conversation": "claude-code/anthropic",
    "model_override_building": null,
    "model_override_conversation": null
  },
  "required_credential_specs": [
    {"name": "gmail", "type": "imap", "description": "Gmail IMAP credentials"}
  ],
  "release_notes": "Fixed off-by-one in invoice parser"
}
```

This is intentionally minimal and forward-compatible with `cinna-compose` (a future feature that adds portability and version pinning on top of the same revisions table).

---

## Building Prompt Update

Modifications to `backend/app/env-templates/app_core_base/core/prompts/BUILDING_AGENT.md`:

- Replace the **Workspace Structure** section with the new convention:
  - `./scripts/`, `./docs/`, `./knowledge/`, `./files/` — **bundle-owned**, replaced on update; the building agent works here while developing the agent.
  - `./app-data/storage/`, `./app-data/uploads/`, `./app-data/cache/` — **per-user persistent**; never overwritten by updates; bundle agents must write all runtime state here.
  - `./credentials/` — synced from platform; same as today.
- New "Persistence Rules" subsection:
  - Conversation-mode runs SHOULD only write to `/tmp` or `./app-data/`. Writing to bundle-owned folders during conversation will be lost on the next update.
  - Building-mode runs MAY write anywhere; the publisher's working install is what gets snapshotted on publish.
- Update file-output guidance examples (currently `./files/`) to differentiate static assets (publisher-shipped) vs. runtime data (`./app-data/storage/`).
- Migration appendix block visible to existing agents on rebuild — explains the convention and asks the building agent to reorganize on user request.

No new prompt assembly logic is needed — `PromptGenerator` already loads the file from `/app/core/prompts/`.

---

## Error Handling & Edge Cases

| Case | Handling |
|------|----------|
| Publishing with empty workspace (no `scripts/`, no prompts) | Allowed; revision is just empty bundle folders. UI warning. |
| `bundle_id` collision when creating a new agent | Auto-generated form is UUID-derived → effectively zero collision risk; on the rare collision, regenerate with longer suffix. |
| User edits `bundle_id` after publish | API rejects with 409. UI hides the edit control once published. |
| Install of a bundle where required credential is missing in user's account | Wizard creates placeholder; agent can't run until filled. Same as today's clone flow. |
| Apply update while session is streaming | Defer until streaming ends (existing pattern in `EnvironmentLifecycleManager`). |
| Uninstall while env is running | Confirm dialog "stop and uninstall?"; service stops env then proceeds. |
| Wipe app-data while install exists (not orphaned) | Refused server-side; UI hides the action when `is_orphaned=false`. |
| Publisher demoted to `agent-user` mid-flow | Existing publish API check catches it; UI re-routes; published bundles continue to be installable by others. |
| App-data volume fails to mount (disk full, permission denied) | Env start fails with explicit `status_message`; status set to `error`. |
| Bundle deleted by publisher with foreign installs | API rejects with 409 + count of dependent installs. Provide a "force-delete" admin path that orphans all installs (flag `bundle_uuid=NULL`). |
| Two concurrent publishes | Per-bundle lock in `PublishService`; second publish queues. |
| Reading bundle_id from `FRONTEND_HOST` when host is `localhost` | Fall back to `localhost` reversal → `localhost.<short-uuid>` (acceptable for self-hosted dev). |
| Reinstall before old install is fully torn down | Service serializes per `(user_id, bundle_id)` via DB row lock on `AppDataVolume`. |

---

## UI/UX Considerations

- **Status colors** for installs:
  - `pending_update=true` → amber dot on agent card.
  - `last_update_status='failed'` → red dot with retry CTA.
  - `app-data orphaned` → muted gray "orphaned" badge in App Data tab.
- **Bundle ID display** uses a monospace font, copy-to-clipboard hover button (`navigator.clipboard.writeText`).
- **Catalog cards** show install count when public; hidden for private/grant bundles.
- **App Data tab** human-readable sizes (`KB`/`MB`/`GB`) using existing `formatBytes` util; refresh-size action uses optimistic UI.
- **Agent-user mode banner** on first login after migration: "Your account is now an Agent User. Ask your admin to enable Developer mode to build agents." Shown once, dismissible.
- **Install wizard accessibility**: each step is a separate route segment for back-button support; `aria-live` on validation errors.
- **Publish dialog warning**: "This will snapshot your current workspace, including any debug data in `scripts/` or `docs/`. Make sure you've cleaned up before publishing." Link to BUILDING_AGENT.md guidance.

---

## Integration Points

- **Agent Environments** (`docs/agents/agent_environments/`) — `EnvironmentLifecycleManager` learns about app-data mount; rebuild path preserves app-data; "replace bundle content" becomes a new lifecycle method called by `apply_update`.
- **Agent Environment Data Management** (`docs/agents/agent_environment_data_management/`) — data classification matrix gains "Bundle-owned (replaced on update)" and "App Data (persistent)" rows; **drops** the clone columns.
- **Agent Prompts** (`docs/agents/agent_prompts/`) — `BUILDING_AGENT.md` updated; `PromptGenerator` unchanged. `workflow_prompt` syncs are still per-install; on publish they're snapshotted into the revision manifest.
- **Agent Sharing** (`docs/agents/agent_sharing/`) — entire feature **deprecated**; docs replaced by new `agent_bundles/` feature docs (out-of-scope for this plan; will be drafted by feature documenter post-implementation).
- **Agent Credentials** (`docs/agents/agent_credentials/`) — sharing semantics simplified: `allow_sharing` retained for credentials granted at install time; `is_placeholder` retained.
- **AI Credentials** (`docs/application/ai_credentials/`) — `AICredentialShare` continues to back AI credential grants for installs (rename internally if appropriate, but not required).
- **Auth** (`docs/application/auth/`) — `User.role` is the new gate; `is_superuser` continues to determine `admin`.
- **Admin Console** — new "Roles" tab; existing admin agent-environments console picks up new bundle-aware columns.
- **Cinna CLI Integration** — CLI workflows that today push to a clone need to push to an install; bundle-folders are the editable surface during local dev. Out of scope for this plan but flagged for follow-up.
- **Agent Email Integration** — auto-share mode gets re-pointed to "auto-install" with the user's Install. The same email session model continues to work; only the `AgentShare`-creation step is replaced with `InstallService.install_bundle(...)` call.

### Client regeneration

After every backend change in this plan, regenerate the OpenAPI client:

```bash
source ./backend/.venv/bin/activate && make gen-client
```

Specifically the new types:
- `AgentBundlePublic`, `AgentBundleRevisionPublic`, `BundleAccessGrantPublic`, `CatalogEntryPublic`
- `AppDataVolumePublic`, `AppDataVolumeUsage`
- `UserPublic` extended with `role`
- `InstallRequest`, `PublishRequest`

Drop generated types: `AgentSharePublic`, `PendingSharePublic`, `AgentShareCreate`, `SetUpdateModeRequest` (rebuild against the new install-mode endpoint), `CloneUpdateRequestPublic`.

---

## Future Enhancements (Out of Scope)

- **`cinna-compose` portable bundle format** — YAML/zip serialization of `AgentBundleRevision` for git-hosted distribution and cross-instance import.
- **Versioning UX** — pin a version per install; rollback to a previous revision; semver-style version labels.
- **Filesystem enforcement** — mount bundle folders read-only at conversation mode container start (today's mount is r/w; would require an SDK-level "session-mode" hook to remount).
- **Global app store registry** — opencinna.io federated catalog with squatting protection and verified publisher handles.
- **Multi-developer bundles** — concurrent agent-developers sharing edit access to a single bundle (today we say "we can do this later"; access model is simpler now).
- **Bundle revision GC** — old revisions cleanup with reference counting.
- **App Data quotas** — per-user / per-bundle storage limits with hard / soft enforcement.
- **Bundle dependencies** — bundles requiring other bundles (e.g. shared knowledge libraries).
- **Cross-instance install** — transferring an install (with app-data) between Cinna instances.

---

## Summary Checklist

### Phase 1 — Foundations

**Backend**:
- [ ] Add `AppDataVolume` model with unique `(user_id, bundle_id)` constraint
- [ ] Add `bundle_id` column to `Agent`, backfill via auto-generation, set NOT NULL
- [ ] Implement `BundleService.generate_bundle_id()` using `FRONTEND_HOST` reverse-DNS
- [ ] Implement `AppDataService.get_or_create_volume()`, `wipe_volume()`, `recompute_size()`, `list_user_volumes()`
- [ ] Add `APP_DATA_STORAGE_DIR` and `BUNDLE_STORAGE_DIR` settings (defaulting under existing `DATA_DIR`)
- [ ] Update `EnvironmentLifecycleManager._generate_compose_file` to mount app-data volume at `/app/workspace/app-data`
- [ ] Update `docker-compose.template.yml` for both env templates with the new volume binding
- [ ] Add API endpoints: `GET /users/me/app-data`, `DELETE /users/me/app-data/{id}`, `POST /users/me/app-data/{id}/recompute-size`
- [ ] Add migration `add_app_data_volumes_and_bundle_id.py`
- [ ] Update `BUILDING_AGENT.md` with new workspace convention (bundle-owned vs. app-data)
- [ ] Add daily APScheduler job for orphaned-volume reporting (no auto-deletion)

**Frontend**:
- [ ] Add Settings → "App Data" tab with `AppDataTab.tsx`, `AppDataRow.tsx`
- [ ] Add humanized size + recompute + wipe controls
- [ ] Show bundle_id (monospace + copy) on agent detail page
- [ ] Regenerate OpenAPI client

**Testing & validation**:
- [ ] Verify app-data folder appears at `/app/workspace/app-data/{storage,uploads,cache}` after env start on existing agents
- [ ] Verify writes to app-data persist across env rebuild
- [ ] Verify volume reattaches after agent uninstall (Phase 2 feature) — exercised manually for now
- [ ] Verify `bundle_id` backfill produces unique values for all existing agents

### Phase 2 — Bundle / Install model

**Backend**:
- [ ] Add `AgentBundle`, `AgentBundleRevision`, `BundleAccessGrant` models
- [ ] Add `bundle_uuid`, `installed_revision_id`, `is_publisher_install` columns on `Agent`; drop `is_clone`, `parent_agent_id`, `clone_mode`
- [ ] Implement `BundleService` (CRUD on bundles), `PublishService` (snapshot + revision creation + notify_installs), `CatalogService` (visibility-aware listing), `InstallService` (install/uninstall/apply_update/check_for_updates)
- [ ] Add API endpoints for bundles, revisions, grants, catalog, install, uninstall, apply-update, check-updates, update-mode
- [ ] Drop API endpoints for shares, clones, push-updates, accept/decline, detach, update-requests
- [ ] Drop `AgentShareService`, `AgentCloneService`
- [ ] Replace `EnvironmentSuspensionScheduler` clone-update branch with `InstallService.apply_update()` call
- [ ] Replace email-integration auto-share with `InstallService.install_bundle()`
- [ ] Add migration `add_bundles_and_drop_clone_tables.py`
- [ ] Implement `replace_bundle_content()` on `EnvironmentLifecycleManager` (preserves app-data, replaces scripts/docs/knowledge/files/requirements)
- [ ] Wire WebSocket events: `BUNDLE_PUBLISHED`, `INSTALL_UPDATE_AVAILABLE`, `INSTALL_UPDATE_APPLIED`, `INSTALL_UPDATE_FAILED`

**Frontend**:
- [ ] Add `AgentBundleTab.tsx` to agent detail (developer view), replacing `AgentSharingTab.tsx`
- [ ] Add `PublishDialog.tsx`, revisions list, grants table
- [ ] Add Catalog route + `CatalogGrid.tsx`, `CatalogCard.tsx`, `CatalogFilters.tsx`
- [ ] Add Install Wizard route + 4-step components
- [ ] Add `UpdateAvailableBanner.tsx` to install detail
- [ ] Delete `ShareManagement/`, `AcceptShareWizard/`, `CloneManagement/`, `PendingAgentCard.tsx` (after porting reusable bits)
- [ ] WebSocket subscriptions for new events
- [ ] Regenerate OpenAPI client

**Testing & validation**:
- [ ] Verify publish snapshot writes correct content to `<bundle_id>/<rev>/`
- [ ] Verify second publish bumps `revision_number` and triggers `notify_installs` for all dependents
- [ ] Verify install copies bundle revision into env workspace, app-data attached
- [ ] Verify apply_update preserves app-data and updates `installed_revision_id`
- [ ] Verify uninstall marks app-data orphaned, reinstall reattaches it
- [ ] Verify removed share endpoints return 404
- [ ] Verify removed AgentShare table absent in fresh DB
- [ ] Verify catalog visibility rules (private / users / public)

### Phase 3 — Roles & agent-user UX

**Backend**:
- [ ] Add `role` column on `User`, default `'agent-user'`
- [ ] Implement `RoleService.set_role()` and `require_developer()` guards
- [ ] Add API endpoints for role read + admin role change
- [ ] Apply `require_developer()` to agent create/update/delete/sync-prompts and to publish flow
- [ ] Apply `require_developer()` to building-mode session start
- [ ] Add migration `add_user_role.py` with backfill (superusers → admin, rest → agent-user)
- [ ] Wire `USER_ROLE_CHANGED` WebSocket event

**Frontend**:
- [ ] Add `AgentUserLayout` with simplified nav (Catalog, Installed, Settings)
- [ ] Branch in `__root.tsx` based on `currentUser.role`
- [ ] Hide/disable agent-developer-only controls in agent detail for `agent-user`
- [ ] Add Admin → Roles tab with `RoleManagementTable.tsx`
- [ ] Add first-login banner explaining the role split
- [ ] WebSocket handler for `USER_ROLE_CHANGED` → refetch + re-route
- [ ] Regenerate OpenAPI client

**Testing & validation**:
- [ ] Verify all existing non-superuser accounts default to `agent-user` after migration
- [ ] Verify agent-user cannot create or edit agents (API + UI)
- [ ] Verify agent-user can install, chat, manage credentials, manage app-data
- [ ] Verify agent-developer can publish, edit, build
- [ ] Verify role downgrade mid-session terminates building access cleanly
- [ ] Verify admin role table shows correct counts and promotion works

### Cross-cutting

- [ ] Update `docs/README.md` Domain Map and Feature Registry: drop `agent_sharing`, add `agent_bundles`, add `agent_app_data`, add `roles`
- [ ] Document the new feature pair (`agent_bundles.md` + `_tech.md`) — to be authored by the feature documenter
- [ ] Add backend tests under `backend/tests/api/agents/` for bundle CRUD, publish, install, uninstall, apply-update, app-data lifecycle
- [ ] Add backend tests under `backend/tests/api/users/` for role transitions and access-control gating
- [ ] Verify OpenAPI client is regenerated after each phase: `bash scripts/generate-client.sh`

---

**Out-of-scope deletion summary** (Phase 2 cleanup): `agent_share`, `clone_update_request` tables; `AgentShareService`, `AgentCloneService`; `agent_shares.py` route; `ShareManagement/`, `AcceptShareWizard/`, `CloneManagement/`, `PendingAgentCard.tsx` components; clone-related fields and indexes on `agent`. Guest sharing is preserved.
