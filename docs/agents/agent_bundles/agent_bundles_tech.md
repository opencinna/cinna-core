# Agent Bundles & Installs — Technical Reference

## File Locations

### Models
- `backend/app/models/bundles/agent_bundle.py` — `AgentBundle`, `AgentBundleBase`, `AgentBundlePublic`, `AgentBundleUpdate`, `AgentBundlesPublic`, `BundleVisibility`, `BundleInstallMode`
- `backend/app/models/bundles/agent_bundle_revision.py` — `AgentBundleRevision`, `AgentBundleRevisionPublic`, `AgentBundleRevisionsPublic`, `PublishRequest`
- `backend/app/models/bundles/bundle_access_grant.py` — `BundleAccessGrant`, `BundleAccessGrantPublic`, `BundleAccessGrantCreate`, `BundleAccessGrantsPublic`
- `backend/app/models/bundles/catalog.py` — `CatalogEntryPublic`, `CatalogPublic`, `InstallRequest`, `AdminInstallRequest`, `AICredentialSelections`, `SetUpdateModeRequest`, `EditBundleIdRequest`, `CheckUpdatesResponse`
- `backend/app/models/agents/agent.py` — `Agent` (the Install table): `bundle_id`, `bundle_uuid`, `installed_revision_id`, `is_publisher_install`, `update_mode`, `pending_update`, `pending_update_at`, `last_sync_at`, `last_update_status`

### Services
- `backend/app/services/bundles/bundle_id_service.py` — `BundleIdService`
- `backend/app/services/bundles/bundle_service.py` — `BundleService`
- `backend/app/services/bundles/exceptions.py` — `BundleError` hierarchy (`BundleNotFoundError`, `BundleAccessDeniedError`, `BundleConflictError`, `BundleValidationError`, `RevisionNotFoundError`, `RevisionInUseError`, `GrantNotFoundError`); each subclass carries an `http_status` attribute used by the route layer
- `backend/app/services/bundles/publish_service.py` — `PublishService`
- `backend/app/services/bundles/install_service.py` — `InstallService`, `InstallError`
- `backend/app/services/bundles/catalog_service.py` — `CatalogService`
- `backend/app/services/bundles/app_data_service.py` — `AppDataService`
- `backend/app/services/bundles/app_data_orphan_scheduler.py` — daily orphan reporter

### API Routes
- `backend/app/api/routes/bundles.py` — bundle CRUD, revisions, grants
- `backend/app/api/routes/catalog.py` — catalog listing and install
- `backend/app/api/routes/installs.py` — publish, uninstall, apply-update, check-updates, update-mode, bundle-id edit
- `backend/app/api/routes/app_data.py` — user app-data volume management

### Frontend
- `frontend/src/routes/_layout/catalog.tsx` — Catalog page route
- `frontend/src/routes/_layout/install/$bundleId.tsx` — Install Wizard route
- `frontend/src/components/Catalog/CatalogGrid.tsx` — catalog grid
- `frontend/src/components/Catalog/CatalogCard.tsx` — single catalog entry card
- `frontend/src/components/Catalog/CatalogFilters.tsx` — filter controls
- `frontend/src/components/Install/InstallWizard.tsx` — 4-step install wizard container
- `frontend/src/components/Install/WizardStepOverview.tsx`
- `frontend/src/components/Install/WizardStepCredentials.tsx`
- `frontend/src/components/Install/WizardStepAICredentials.tsx`
- `frontend/src/components/Install/WizardStepConfirm.tsx`
- `frontend/src/components/Agents/AgentBundleTab.tsx` — bundle management tab on agent detail page
- `frontend/src/components/Agents/UpdateAvailableBanner.tsx` — pending update notification

## Database Schema

### `agent_bundle`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `bundle_id` | varchar(255) UNIQUE NOT NULL | Reverse-DNS string; stable identifier |
| `display_name` | varchar(255) NOT NULL | |
| `description` | text | |
| `publisher_user_id` | UUID FK → user ON DELETE RESTRICT | |
| `latest_revision_id` | UUID FK → agent_bundle_revision ON DELETE SET NULL | |
| `is_listed` | bool DEFAULT false | Shown in catalog |
| `visibility` | varchar(32) DEFAULT 'private' | `private`, `users`, `public` |
| `default_install_mode` | varchar(32) DEFAULT 'manual' | `manual`, `automatic` |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

Indexes: `ix_agent_bundle_publisher` on `publisher_user_id`; `ix_agent_bundle_listed_visibility` partial on `(is_listed, visibility) WHERE is_listed = true`.

### `agent_bundle_revision`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `bundle_id` | UUID FK → agent_bundle ON DELETE CASCADE | Note: this is the bundle UUID, not the string ID |
| `revision_number` | int NOT NULL | Monotonically increasing per bundle |
| `version` | varchar(64) NULLABLE | User-entered human-friendly version label (e.g. `1.0`, `1.1`); independent from `revision_number`. NULL on revisions created before the field was introduced |
| `manifest` | JSON | Mirror of manifest.json on disk |
| `workflow_prompt` | text | Snapshot from publisher install |
| `entrypoint_prompt` | text | Snapshot from publisher install |
| `refiner_prompt` | text | Snapshot from publisher install |
| `agent_sdk_building` | varchar(128) | SDK selection snapshot |
| `agent_sdk_conversation` | varchar(128) | SDK selection snapshot |
| `model_override_building` | varchar(128) | |
| `model_override_conversation` | varchar(128) | |
| `required_credential_specs` | JSON | `[{name, type, allow_sharing, description}]` |
| `snapshot_path` | varchar(1024) NOT NULL | Absolute path under `BUNDLE_STORAGE_DIR` |
| `content_hash` | varchar(64) NOT NULL | SHA-256 hex over snapshot tree + manifest |
| `published_by_user_id` | UUID FK → user ON DELETE SET NULL | |
| `published_at` | timestamp NOT NULL | |
| `release_notes` | text | |

Unique constraint: `uq_revision_bundle_number` on `(bundle_id, revision_number)`. Index: `ix_revision_bundle` on `bundle_id`.

### `bundle_access_grant`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `bundle_id` | UUID FK → agent_bundle ON DELETE CASCADE | |
| `user_id` | UUID FK → user ON DELETE CASCADE | |
| `granted_by_user_id` | UUID FK → user ON DELETE SET NULL | |
| `created_at` | timestamp | |

Unique constraint: `uq_bundle_grant_bundle_user` on `(bundle_id, user_id)`.

### `agent` (modified for Install model)

New columns (added in Phase 2 migration):

| Column | Type | Notes |
|--------|------|-------|
| `bundle_id` | varchar(255) NOT NULL | Reverse-DNS string; auto-generated on creation |
| `bundle_uuid` | UUID FK → agent_bundle ON DELETE SET NULL | null for unpublished agents |
| `installed_revision_id` | UUID FK → agent_bundle_revision ON DELETE SET NULL | Which revision this install was created from |
| `is_publisher_install` | bool DEFAULT false | True on the publisher's own copy |
| `update_mode` | varchar(32) DEFAULT 'manual' | `manual` or `automatic` |
| `pending_update` | bool DEFAULT false | |
| `pending_update_at` | timestamp | |
| `last_sync_at` | timestamp | |
| `last_update_status` | varchar(64) | `synced`, `failed`, or null |

Dropped columns (Phase 2 migration): `is_clone`, `parent_agent_id`, `clone_mode` and their indexes.

## API Endpoints

### Bundle Management (`/api/v1/bundles`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/bundles/` | `require_developer` | List bundles owned by current user |
| `GET` | `/bundles/{bundle_uuid}` | CurrentUser | Detail; visibility-checked for non-publishers |
| `PATCH` | `/bundles/{bundle_uuid}` | `require_developer` + owner | Update display_name, visibility, is_listed, default_install_mode |
| `DELETE` | `/bundles/{bundle_uuid}` | `require_developer` + owner | Rejected with 409 if foreign installs exist |
| `GET` | `/bundles/{bundle_uuid}/revisions` | `require_developer` + owner | List all revisions (descending) |
| `DELETE` | `/bundles/{bundle_uuid}/revisions/{revision_id}` | `require_developer` + owner | 404 if revision does not belong to bundle, 409 if any foreign install still references it |
| `GET` | `/bundles/{bundle_uuid}/grants` | `require_developer` + owner | |
| `POST` | `/bundles/{bundle_uuid}/grants` | `require_developer` + owner | Body: `{email: str}`; resolves to user on this instance |
| `DELETE` | `/bundles/{bundle_uuid}/grants/{grant_id}` | `require_developer` + owner | |

### Catalog & Install (`/api/v1/catalog`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/catalog/` | CurrentUser | Visibility-aware list |
| `GET` | `/catalog/{bundle_id}` | CurrentUser | Single entry (404 if not visible) |
| `POST` | `/catalog/{bundle_id}/install` | CurrentUser | Body: `InstallRequest`; idempotent |
| `POST` | `/catalog/{bundle_id}/admin-install` | superuser | Body: `AdminInstallRequest` (includes `target_user_id`) |

### Install Operations (`/api/v1/agents`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/agents/{agent_id}/publish` | `require_developer` + owner | Body: `PublishRequest` |
| `POST` | `/agents/{agent_id}/uninstall` | owner | 400 if `is_publisher_install` |
| `POST` | `/agents/{agent_id}/apply-update` | owner | Stops env, replaces bundle content, restarts |
| `POST` | `/agents/{agent_id}/check-updates` | owner | Returns `CheckUpdatesResponse` |
| `PATCH` | `/agents/{agent_id}/update-mode` | owner | Body: `{update_mode: "manual"|"automatic"}` |
| `PATCH` | `/agents/{agent_id}/bundle-id` | `require_developer` + owner | 409 if already published |

### App Data (`/api/v1/users/me/app-data`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/users/me/app-data` | CurrentUser | List all volumes with size and install name |
| `POST` | `/users/me/app-data/{id}/recompute-size` | CurrentUser + owner | Walk directory, update size_bytes |
| `DELETE` | `/users/me/app-data/{id}` | CurrentUser + owner | 409 if not orphaned |

## Services & Key Methods

### `BundleIdService`

| Method | Notes |
|--------|-------|
| `reversed_host_prefix() -> str` | Derives prefix from `settings.FRONTEND_HOST` |
| `generate_bundle_id(agent_id) -> str` | `<prefix>.<agent_id.hex[:8]>` |
| `is_valid_format(bundle_id) -> bool` | Regex: `^[a-zA-Z0-9][a-zA-Z0-9.\-]{1,253}$` |
| `is_reserved(bundle_id) -> bool` | Checks `io.opencinna.system.*` prefix |

### `BundleService`

| Method | Notes |
|--------|-------|
| `get_bundle_by_uuid(session, uuid)` | |
| `get_bundle_by_id(session, bundle_id: str)` | Lookup by string bundle_id |
| `list_publisher_bundles(session, user_id)` | |
| `install_count(session, bundle_uuid)` | Total installs including publisher |
| `foreign_install_count(session, bundle)` | Excludes publisher install |
| `latest_revision(session, bundle)` | |
| `create_bundle(session, bundle_id, publisher_user_id, display_name, description)` | Used by PublishService on first publish |
| `get_for_publisher(session, bundle_uuid, user)` | Resolves a bundle owned by `user` (or any superuser); raises `BundleNotFoundError` (404) or `BundleAccessDeniedError` (403). Replaces the old `_resolve_bundle_for_publisher` route helper |
| `revision_install_count(session, revision_id)` | Single-revision install count — used by `POST /agents/{id}/publish` to wire the response |
| `list_revisions_with_install_counts(session, bundle)` | Returns `(revision, install_count)` pairs newest-first using one aggregated query (replaces the per-row count loop) |
| `update_bundle(session, bundle, data: AgentBundleUpdate)` | Validates visibility/install_mode values; raises `BundleValidationError` (400) on bad input |
| `delete_revision(session, bundle, revision_id)` | Raises `RevisionNotFoundError` (404) if revision missing; `RevisionInUseError` (409) if any foreign install references it. Detaches publisher install(s), rewires `latest_revision_id` to the previous remaining revision when one exists, removes the on-disk snapshot tree best-effort. **Single-transaction**: when the deletion empties the bundle (no revisions remain), the bundle row itself is also dropped in the same commit via `session.flush()` ordering — cascades clear grants and the publisher install's `bundle_uuid` (FK `ON DELETE SET NULL`), reverting it to an unpublished, rename-able state |
| `delete_bundle(session, bundle)` | Raises `BundleConflictError` (409) if foreign installs exist |
| `list_grants(session, bundle)` | |
| `grant_access(session, bundle, target_user, granted_by_user_id)` | Idempotent |
| `revoke_grant(session, bundle, grant_id)` | |
| `user_has_grant(session, bundle, user_id)` | |

### `PublishService`

| Method | Notes |
|--------|-------|
| `publish(session, install, publisher_user_id, release_notes, display_name, description, bundle_id_override, version)` | Async; acquires per-bundle lock. `bundle_id_override` is honoured only on the first publish (delegates to `InstallService.edit_bundle_id` for validation/uniqueness; rejected with 409 once a bundle row exists). `version` is stored on the new revision and surfaced via `AgentBundleRevisionPublic.version` |
| `notify_installs(session, bundle, revision)` | Marks all foreign installs `pending_update=True`, emits `INSTALL_UPDATE_AVAILABLE` events |
| `_copy_bundle_tree(env_workspace_root, dest)` | Copies `scripts/`, `docs/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt` |
| `_hash_tree_with_manifest(root, manifest)` | SHA-256 over sorted file paths + content + manifest body |
| `_collect_credential_specs(session, install)` | Reads linked `AgentCredentialLink` rows |

Publish flow detail:
1. Lock acquired on `bundle_id` string
2. Bundle row resolved or created (first publish)
3. Next `revision_number = MAX(existing) + 1`
4. Snapshot written to `<tmp_dir>` then renamed to `<snapshot_dir>`
5. `AgentBundleRevision` row inserted
6. `bundle.latest_revision_id` and `install.installed_revision_id` updated
7. `BUNDLE_PUBLISHED` event emitted
8. `notify_installs()` called

### `InstallService`

| Method | Notes |
|--------|-------|
| `install_bundle(session, user, bundle, request)` | Idempotent; returns existing install if present |
| `install_bundle_for_email(session, publisher_agent_id, recipient_user_id)` | Auto-publishes on first call if bundle has no revisions |
| `admin_install(session, target_user, bundle, request)` | Thin wrapper over install_bundle |
| `apply_update(session, install)` | Stops env, calls `replace_bundle_content`, updates prompts + bookkeeping fields, emits event |
| `uninstall(session, install)` | Delegates to `AgentService.delete_agent` which handles orphaning |
| `set_update_mode(session, install, mode)` | |
| `check_for_updates(session, install)` | Returns `{pending_update, installed_revision_number, latest_revision_number, last_update_status, last_sync_at, update_mode}` |
| `edit_bundle_id(session, install, new_bundle_id)` | Raises 409 if already published; validates format and uniqueness |

Install flow (`_install_from_revision`):
1. `Agent` row created from revision prompts + SDK settings
2. Name uniqueness enforced (appends "(2)", "(3)" etc.)
3. `AgentEnvironment` created via `EnvironmentService.create_environment`
4. Workspace seeded from `revision.snapshot_path` via `seed_workspace_from_bundle_snapshot`
5. `AppDataService.get_or_create_volume` called; existing orphaned volume reattached
6. Credentials set up (placeholders created or existing credentials linked)

### `CatalogService`

| Method | Notes |
|--------|-------|
| `list_for_user(session, user)` | Union of public listed, grant-visible, and publisher-own bundles |
| `get_for_user(session, bundle_id, user)` | Single entry; returns None if not visible |
| `user_can_see(session, bundle, user)` | Visibility check logic |
| `user_can_install(session, bundle, user)` | `user_can_see` AND `latest_revision_id IS NOT NULL` |
| `_bundle_to_entry(session, bundle, user)` | Resolves a `CatalogEntryPublic` from a bundle row: reads latest revision for `latest_version` / `latest_revision_number`; reads publisher `User` row for `publisher_name` and `publisher_email`; checks the calling user's install row for `is_installed` / `user_install_id` |

## Bundle Storage Layout (Filesystem)

```
${BUNDLE_STORAGE_DIR}/                       # config: defaults to <DATA_DIR>/bundles/
├── io.opencinna.cinna.a1b2c3d4/            # one dir per bundle_id string
│   ├── 1/                                  # one dir per revision_number
│   │   ├── manifest.json
│   │   ├── scripts/
│   │   ├── docs/
│   │   ├── knowledge/
│   │   ├── files/
│   │   ├── workspace_requirements.txt
│   │   └── workspace_system_packages.txt
│   ├── 2/
│   │   └── ...
│   └── 3.tmp/                              # leftover from failed publish (debug)
└── io.opencinna.cinna.deadbeef/
    └── ...
```

## Manifest Format

Each revision writes `manifest.json` into the snapshot directory:

```json
{
  "schema_version": 1,
  "bundle_id": "io.opencinna.cinna.a1b2c3d4",
  "revision_number": 3,
  "version": "1.2",
  "content_hash": "sha256:<64-hex>",
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
    {"name": "gmail", "type": "imap", "allow_sharing": false, "description": null}
  ],
  "release_notes": "Fixed off-by-one in invoice parser"
}
```

The `content_hash` is SHA-256 over (sorted file paths + content + manifest body excluding `content_hash` itself). This is the same value stored in `AgentBundleRevision.content_hash`.

## Frontend Components

### Catalog
- `CatalogGrid` — renders a responsive grid of `CatalogCard` components; consumes `GET /catalog/` via React Query key `["catalog"]`
- `CatalogCard` — bundle name, description, publisher name + email (falling back to the truncated handle), version badge (`v<latest_version>`, falling back to `rev <n>`). The install-count badge has been removed from the card. "Install" button (or "Open" if already installed)
- `CatalogFilters` — filter by visibility, installed status

### Install Wizard
- `InstallWizard` — 4-step container; separate route segment per step for back-button support
- `WizardStepOverview` — displays bundle metadata and required credentials summary
- `WizardStepCredentials` — per-spec picker: link existing credential by UUID or accept placeholder
- `WizardStepAICredentials` — conversation + building AI credential selection (reuses existing credential picker)
- `WizardStepConfirm` — summary + submit; calls `POST /catalog/{bundle_id}/install`; navigates to install detail on success

### Agent Bundle Tab
- `AgentBundleTab` — rendered on agent detail page for `agent-developer` users. Two-card grid layout:
  - **Left — Bundle settings**: catalog-only settings, all post-publish (Visibility, the user-allowlist picker when `visibility = "users"`, Listed-in-catalog `Switch`, Default install update mode). Pre-publish the card shows a placeholder pointing the user at the Publish action. The Bundle ID block and its edit modal have been removed — the bundle ID is set inside the publish dialog on the first publish and locked afterwards
  - **Right — Revisions**: header "Publish revision" button; once published, a compact Bundle ID row (label + monospace value + copy button) is shown at the top of the body, followed by the latest 10 revisions list. Each revision renders as `v<version>` when present (with `(rev <n>)` in muted text) or `rev <n>` for legacy rows. Rows include `current` / `installed` badges, install count, release notes, copy-content-hash button (with hash preview tooltip), and a delete button. Delete is disabled (with tooltip) whenever the row has more than the publisher's own install on it; clicking opens an `AlertDialog`
  - **Publish dialog** — owns three fields: `Bundle ID` (only rendered on first publish; prefilled with `agent.bundle_id`, sent as `PublishRequest.bundle_id`), `Version` (always; defaults to `"1.0"` on first publish, otherwise `suggestNextVersion(previousRevision.version)` — increments the trailing numeric component), and optional release notes. Inline `Alert` shows publish errors. Submit posts to `POST /agents/{id}/publish`
- `UserAllowlistPicker` (`frontend/src/components/Common/UserAllowlistPicker.tsx`) — shared search-and-pill picker for selecting users by email/name. Used for bundle access grants and for shared-with-users assignments in `McpConnectorsCard`. Caller passes a list of `{id, userId, fallbackLabel?}` items and `onAdd`/`onRemove` callbacks; the component owns the search input, results dropdown, and pill rendering, and fetches `/users` via React Query under key `["users-list"]`

### Update Banner
- `UpdateAvailableBanner` — shown on install detail when `pending_update=true`; displays revision delta and release notes; "Apply now" button calls `POST /agents/{id}/apply-update`

## WebSocket Events

| Event | Direction | Payload | Notes |
|-------|-----------|---------|-------|
| `BUNDLE_PUBLISHED` | server → publisher | `{bundle_id, bundle_uuid, revision_number, revision_id}` | Refreshes `["bundles"]` query |
| `INSTALL_UPDATE_AVAILABLE` | server → install owner | `{agent_id, bundle_id, revision_number, release_notes, update_mode}` | Shows UpdateAvailableBanner |
| `INSTALL_UPDATE_APPLIED` | server → install owner | `{agent_id, bundle_id, revision_number}` | Clears banner, refreshes env |
| `INSTALL_UPDATE_FAILED` | server → install owner | `{agent_id, bundle_id, error}` | Shows error state on banner |

## React Query Keys

| Key | Source |
|-----|--------|
| `["bundles"]` | `GET /bundles/` |
| `["bundles", bundleUuid]` | `GET /bundles/{uuid}` |
| `["bundles", bundleUuid, "revisions"]` | `GET /bundles/{uuid}/revisions` |
| `["bundles", bundleUuid, "grants"]` | `GET /bundles/{uuid}/grants` |
| `["catalog"]` | `GET /catalog/` |
| `["catalog", bundleId]` | `GET /catalog/{bundle_id}` |
| `["users-list"]` | `GET /users/` (shared by `UserAllowlistPicker` for the grants picker; cached 30s) |

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `BUNDLE_STORAGE_DIR` | `<DATA_DIR>/bundles/` | Root for all revision snapshots |
| `APP_DATA_STORAGE_DIR` | `<DATA_DIR>/app-data/` | Root for all app-data volumes |
| `HOST_APP_DATA_DIR` | `""` | Set in Docker-in-Docker; see AppDataService path translation |

## Migrations

| File | Description |
|------|-------------|
| `backend/app/alembic/versions/i7e8f9a0b1c2_add_version_to_agent_bundle_revision.py` | Adds nullable `version varchar(64)` column to `agent_bundle_revision`. `down_revision = h6d7e8f9a0b1`. Existing rows get NULL; the UI falls back to `rev <n>` for those rows |

## Security

- `AgentBundlePublic` omits publisher email and raw UUID; only a truncated handle (`first 8 chars of UUID + "…"`) is exposed
- `CatalogEntryPublic` additionally surfaces `publisher_name`, `publisher_email`, and `latest_version` for the install/catalog UX. Catalog access is auth-gated to users who can already see the bundle row, so exposing the publisher's name + email matches the trust model of an internal-instance catalog. If a future deployment needs anonymised publishers, the resolver in `CatalogService._bundle_to_entry` is the single point to gate
- `required_credential_specs` contains only names and types; no secret values are ever stored
- Bundle deletion is blocked (`409`) until all foreign installs are removed
- Wipe of app-data volume is blocked (`409`) unless `is_orphaned = true`
- Bundle-id edit is blocked (`409`) after first publish to prevent orphaning installed app-data
