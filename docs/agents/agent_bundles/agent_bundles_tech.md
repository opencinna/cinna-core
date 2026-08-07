# Agent Bundles & Installs — Technical Reference

## File Locations

### Models
- `backend/app/models/bundles/agent_bundle.py` — `AgentBundle`, `AgentBundleBase`, `AgentBundlePublic`, `AgentBundleUpdate`, `AgentBundlesPublic`, `BundleVisibility`, `BundleInstallMode`
- `backend/app/models/bundles/agent_bundle_revision.py` — `AgentBundleRevision`, `AgentBundleRevisionPublic`, `AgentBundleRevisionsPublic`, `PublishRequest`
- `backend/app/models/bundles/bundle_access_grant.py` — `BundleAccessGrant`, `BundleAccessGrantPublic`, `BundleAccessGrantCreate`, `BundleAccessGrantsPublic`
- `backend/app/models/bundles/catalog.py` — `CatalogEntryPublic` (includes `user_install_pending_update: bool` — `True` when the viewer's consumer install exists and its `Agent.pending_update` is `True`; populated by `CatalogService._bundle_to_entry`), `CatalogPublic`, `InstallRequest`, `AdminInstallRequest`, `AICredentialSelections` (gains `use_publisher_ai: bool = False` in Phase 3 — UI hint only, backend ignores it), `SetUpdateModeRequest`, `EditBundleIdRequest`, `CheckUpdatesResponse` (includes `installed_version: str | None` and `latest_version: str | None` — the human-friendly `AgentBundleRevision.version` labels for each revision; null when unset, UI falls back to revision numbers; also includes `latest_release_notes: str | None` and `latest_published_at: datetime | None`, read straight off the resolved latest revision row — powers the "Latest available" panel on `BundleInstallationCard`), `InstallCredentialSelection` (Phase 3 — `mode` literal + optional `credential_id`), `InstallContextSpec` (Phase 3; gains `service_uri: str | None = None` for informational display on the install screen), `CatalogInstallContext` (Phase 3), `CredentialSpecDrift` (non-table Pydantic: `name: str`, `type: str`, `live_provided_by: Literal["user","publisher","template"]`, `snapshot_provided_by: Literal["user","publisher","template"]`, `drifted: bool`), `BundleCredentialDrift` (non-table Pydantic: `stale: bool`, `drift: list[CredentialSpecDrift]` — response shape for `GET /agents/{id}/bundle-credential-drift`). Both models are re-exported from `backend/app/models/__init__.py`.
- `backend/app/models/agents/agent.py` — `Agent` (the Install table): `bundle_id`, `bundle_uuid`, `installed_revision_id`, `is_publisher_install`, `update_mode`, `pending_update`, `pending_update_at`, `last_sync_at`, `last_update_status`, `last_update_attempt_at` (nullable, migration `04e32c2c255a`; stamped by the automatic-update sweep immediately before calling `apply_update`, so a crash mid-apply still records the attempt; drives the sweep's failure backoff; not exposed on `AgentPublic` — no UI need yet). `AgentPublic` additionally exposes `installed_revision_number` (monotonic int, the revision's sequence number) and `installed_revision_version` (publisher-entered string such as `"1.0"` or `"1.2"`; nullable for legacy revisions). The agent detail page header badge renders as `v{installed_revision_version || installed_revision_number}` — preferring the human-readable string and falling back to the integer.
- `backend/app/models/bundles/bundle_permissions.py` — non-table response schemas backing the Permissions management card: `BundlePermissionScopeCatalogEntry` (`name`, `description`), `BundlePermissionGrant` (`user_id`, `grant_id`, `scopes`), `BundlePermissionProducer` (`producer_agent_id`, `producer_agent_name`, `producer_ui_color_preset`, `credential_id`, `credential_name`, `identity_enabled`, `can_manage`, `owner_email`, `scope_catalog`, `grants` — the last two populated only when `can_manage`), `BundlePermissionUser` (`user_id`, `email`, `full_name`, `bundle_grant_id`), `BundlePermissionsOverview` (`bundle_uuid`, `visibility`, `bundle_access_applicable`, `bundle_grants: list[BundleAccessGrantPublic]`, `producers`, `users`, `show_card`). No DB table; re-exported from `backend/app/models/__init__.py`. Response shape of `GET /agents/{agent_id}/bundle-permissions-overview`.

### Services
- `backend/app/services/bundles/bundle_id_service.py` — `BundleIdService`
- `backend/app/services/bundles/bundle_service.py` — `BundleService`
- `backend/app/services/bundles/exceptions.py` — `BundleError` hierarchy (`BundleNotFoundError`, `BundleAccessDeniedError`, `BundleConflictError`, `BundleValidationError`, `RevisionNotFoundError`, `RevisionInUseError`, `GrantNotFoundError`); each subclass carries an `http_status` attribute used by the route layer
- `backend/app/services/bundles/publish_service.py` — `PublishService`
- `backend/app/services/bundles/credential_spec.py` — `ParsedCredentialSpec` (frozen dataclass: `name`, `type`, `provided_by`, `publisher_credential_id`, `template_data`, `template_private_fields`, `service_uri`); `parse_credential_spec` coalesces missing `service_uri` key to `None` (backward compatible with old revision JSON)
- `backend/app/services/bundles/install_service.py` — `InstallService`, `InstallError`
- `backend/app/services/bundles/schedule_sync.py` — `snapshot_schedules`, `sig`, `materialise`, `merge` — bundle schedule propagation helpers (publish snapshot → install materialise → apply-update merge)
- `backend/app/services/bundles/install_readiness_gate.py` — `InstallReadinessGate`, `GateResult`, `GateMissingItem` (Phase 4)
- `backend/app/services/bundles/catalog_service.py` — `CatalogService`
- `backend/app/services/bundles/app_data_service.py` — `AppDataService`
- `backend/app/services/bundles/app_data_orphan_scheduler.py` — daily orphan reporter
- `backend/app/services/environments/bundle_auto_update_scheduler.py` — periodic convergence sweep for automatic-mode installs (lives in `services/environments/` alongside the other lifecycle schedulers, not `services/bundles/`, mirroring `environment_suspension_scheduler.py`). See "Automatic Update Convergence" under [Services & Key Methods](#installservice) below
- `backend/app/services/bundles/bundle_permissions_service.py` — `BundlePermissionsService`, the read-only cross-domain orchestrator backing the Permissions management card; see [Services & Key Methods](#bundlepermissionsservice) below. Reuses `AgentApiTokenService.list_connected_producers` (`backend/app/services/agent_api/agent_api_token_service.py` — see [Agent REST API tech reference](../agent_api/agent_api_tech.md)) and `AgentApiGrantService.list_grants` / `.get_scope_catalog` (owner-gated, unchanged)

### API Routes
- `backend/app/api/routes/bundles.py` — bundle CRUD, revisions, grants
- `backend/app/api/routes/catalog.py` — catalog listing and install
- `backend/app/api/routes/installs.py` — publish, uninstall, apply-update, check-updates, update-mode, bundle-id edit
- `backend/app/api/routes/app_data.py` — user app-data volume management

### Frontend
- `frontend/src/routes/_layout/catalog.tsx` — `/catalog` layout shell (renders `<Outlet />`)
- `frontend/src/routes/_layout/catalog/index.tsx` — `/catalog` listing page (CatalogGrid + filters)
- `frontend/src/routes/_layout/catalog/agents.tsx` — `/catalog/agents` shell
- `frontend/src/routes/_layout/catalog/agents/install.tsx` — `/catalog/agents/install` shell
- `frontend/src/routes/_layout/catalog/agents/install/$bundleId.tsx` — `/catalog/agents/install/$bundleId` — renders `<InstallPage context={...} />` (Phase 3 rewrite; previously the Install Wizard route)
- `frontend/src/components/Catalog/CatalogGrid.tsx` — catalog grid
- `frontend/src/components/Catalog/CatalogCard.tsx` — single catalog entry card. The whole card body is clickable (navigates to the install page when not installed, or the agent detail page when installed). The footer button is "Quick Install" for uninstalled bundles (fires `useQuickInstall`, shows a `Loader2` spinner with "Installing…" label while pending), "Open" for installed bundles that are up-to-date, or an amber **"Update to v\<latest_version\>"** button when `user_install_pending_update` is `true` (clicking it calls `POST /agents/{id}/apply-update` via `InstallsService.applyUpdate`, shows an inline spinner with "Updating…" label, then on success shows a toast and invalidates the catalog query so the card reverts to "Open"). All footer buttons `stopPropagation` so they don't trigger the card-body navigation; card-body clicks are no-op while a Quick Install or update is in flight
- `frontend/src/components/Catalog/CatalogFilters.tsx` — filter controls
- `frontend/src/components/Install/InstallPage.tsx` — two-column install page container (left sticky agent header, right scrollable setup form, single Install button at bottom)
- `frontend/src/components/Install/InstallAgentHeaderCard.tsx` — left-column sticky card showing bundle icon, name, version, publisher, description, credential summary, Bundle ID
- `frontend/src/components/Install/InstallSetupForm.tsx` — right-column form; orchestrates AI section + service section + Install button; owns form state and submit logic. Post-install side effects (query invalidation, route-conflict toast, setup-status check, dashboard-vs-credentials redirect) are delegated to `useBundleInstallNavigation`
- `frontend/src/components/Install/InstallAICredentialSection.tsx` — renders publisher-provides info state OR the AI credential picker; replaces `WizardStepAICredentials` logic
- `frontend/src/components/Install/InstallServiceCredentialItem.tsx` — one shadcn/ui `Accordion` item per service credential spec; handles auto-prefill suggestion display and mode selection (`use_existing` / `skip` / pick-another)
- `frontend/src/components/Install/useInstallContext.ts` — React Query hook on `["catalog", bundleId, "install-context"]` fetching `GET /catalog/{bundle_id}/install-context`
- `frontend/src/components/Install/useQuickInstall.ts` — `useMutation` hook used by `CatalogCard`'s Quick Install button. Fetches `install-context`, builds the default `InstallCredentialSelection` payload (PBP → `publisher_provides`, suggested credential → `use_existing`, else → `skip`) and the default `AICredentialSelections` (publisher-AI flag forwarded when offered), then calls `CatalogService.installBundle`. Delegates post-install to `useBundleInstallNavigation`
- `frontend/src/components/Install/useBundleInstallNavigation.ts` — shared post-install side-effect hook used by both `InstallSetupForm` and `useQuickInstall`. Invalidates `["agents"]` and `["catalog"]`, fires the best-effort `AgentAppMcpRoutesService.checkRouteConflicts` warning toast, then calls `InstallsService.getSetupStatus` to branch: gate `ready` → navigate to `/` with `?selectAgentId=<install.id>`; anything else → `/agent/$agentId#credentials`
- `frontend/src/components/Agents/AgentBundleTab.tsx` — bundle management tab on agent detail page
- `frontend/src/components/Agents/CredentialProvisioningSection.tsx` — publisher-only half-width card on the bundle tab; only rendered when `agent.is_publisher_install === true`; lets the publisher set per-credential `provided_by` overrides (auto-save on change) and pick publisher AI credentials per mode with SDK-aware filtering (Phase 5)
- `frontend/src/components/Agents/BundlePermissionsCard.tsx` — unified publisher-facing "Permissions management" card; replaces the old inline Grants `UserAllowlistPicker` block in `AgentBundleTab.tsx`. See [Frontend Components](#agent-bundle-tab) below
- `frontend/src/components/Agents/BundlePermissionsAddUserModal.tsx` — "Add user" / "Edit permissions" modal used by `BundlePermissionsCard`; fans out to existing bundle-grant and agent-api-grant endpoints
- `frontend/src/components/Credentials/credentialTypes.ts` — shared credential-type metadata registry (`CREDENTIAL_TYPE_GROUPS` array + `getCredentialTypeMeta(type)` helper); single source of truth for the icon, label, and per-group badge palette of every `CredentialType`. Consumed by both the Add Credential picker and the display-only `<CredentialTypeBadge>`
- `frontend/src/components/Credentials/CredentialTypeBadge.tsx` — display-only `<span>` chip rendering the icon + label + palette for a credential type; reused on the publisher's credential provisioning panel (and any future surface that needs to surface a credential's type at a glance)
- `frontend/src/components/Agents/UpdateAvailableBanner.tsx` — pending update notification; displays the from→to version labels (e.g. `v1.0 → v1.1`) sourced from `CheckUpdatesResponse.installed_version` and `latest_version`, falling back to revision numbers when labels are null; the apply button reads "Update to v\<latest_version\>" when a version label is available. `revisionLabel` was extracted from this component into `frontend/src/utils/bundleRevision.ts` so `BundleInstallationCard` can reuse the identical formatting rule; the banner itself is otherwise unchanged and remains the page-level attention grabber
- `frontend/src/components/Agents/BundleInstallationCard.tsx` — new. Mounted in `AgentConfigTab.tsx`; renders `null` unless `agent.bundle_uuid && !agent.is_publisher_install` (D9). Deliberately **not** gated by `AgentConfigTab`'s `readOnly` prop — the update mode is the consumer's own preference, not publisher-authored content, so it stays editable even on an otherwise read-only config tab for a foreign install. Shows Bundle ID, installed version, the "Latest available" panel (release notes + published date via `CheckUpdatesResponse`, sharing its `["agent", agentId, "check-updates"]` query key with `UpdateAvailableBanner` so the two surfaces resolve to one request) or an explicit "Up to date" state, the manual/automatic update-mode `Select` (calls `InstallsService.setUpdateMode`), and an "Update now" button (calls `InstallsService.applyUpdate`) that stays visible in automatic mode too (D10)
- `frontend/src/utils/bundleRevision.ts` — new. Exports `revisionLabel(version, number)`, the shared `v<version>` / `rev <number>` formatting rule used by both `UpdateAvailableBanner` and `BundleInstallationCard`
- `frontend/src/components/Install/SetupNeededBanner.tsx` — banner on the agent detail page when the gate would block; queries `["agent", agentId, "setup-status"]`; renders amber alert (`needs_setup`) or destructive alert (`publisher_broken`); no action button — copy directs the user to the Credentials tab on the same page; subscribes to all three Phase 4 WS events; absent when `status === "ready"`
- `frontend/src/components/Agents/UninstallAgent.tsx` — destructive kebab menu item on the agent detail page for foreign installs; opens a confirmation dialog explaining that the install and environment are removed but per-bundle App Data is preserved and reattaches on reinstall; calls `InstallsService.uninstallInstall({ agentId })` → `POST /agents/{id}/uninstall`; on success navigates away to the agents list

Note: `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` (the dedicated setup page) has been deleted. The three backend endpoints it backed (`GET /agents/{id}/setup-status`, `GET /agents/{id}/setup-credentials`, `PUT /agents/{id}/setup-credentials/{credential_id}`) still exist and are used by the cinna CLI.

Deleted in Phase 3 (replaced by the single-page install):
- `InstallWizard.tsx`, `WizardStepOverview.tsx`, `WizardStepCredentials.tsx`, `WizardStepAICredentials.tsx`, `WizardStepConfirm.tsx`

## Database Schema

### `agent_bundle`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `bundle_id` | varchar(255) UNIQUE NOT NULL | Reverse-DNS string; stable identifier |
| `display_name` | varchar(255) NOT NULL | Refreshed from `display_name or install.name` on every publish (see `PublishService._publish_locked` step 7), not just the first — also settable via `PATCH /bundles/{uuid}` |
| `description` | text | Refreshed from `description if description is not None else install.description` on every publish, mirroring `display_name` |
| `publisher_user_id` | UUID FK → user ON DELETE RESTRICT | |
| `latest_revision_id` | UUID FK → agent_bundle_revision ON DELETE SET NULL | |
| `is_listed` | bool DEFAULT false | Shown in catalog |
| `visibility` | varchar(32) DEFAULT 'private' | `private`, `users`, `public` |
| `default_install_mode` | varchar(32) DEFAULT 'manual' | `manual`, `automatic` |
| `publisher_ai_credential_conversation_id` | UUID FK → ai_credential ON DELETE SET NULL NULLABLE | Publisher-provided AI for conversation mode. NULL = user provides at install time. |
| `publisher_ai_credential_building_id` | UUID FK → ai_credential ON DELETE SET NULL NULLABLE | Publisher-provided AI for building mode. NULL = user provides at install time. |
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
| `router_trigger_prompt` | text NULLABLE | Snapshot of `Agent.router_trigger_prompt` at publish time. Used by `InstallService` to auto-create an `AppAgentRoute` at install time and by `apply_update` to refresh auto-managed routes. NULL when the publisher left the field blank — installs from such revisions are marked degraded and get no auto-route |
| `agent_sdk_building` | varchar(128) | SDK selection snapshot |
| `agent_sdk_conversation` | varchar(128) | SDK selection snapshot |
| `model_override_building` | varchar(128) | Snapshot of the publisher's per-mode model pin. Restored onto the consumer's `AgentEnvironment` at install time (`InstallService._install_from_revision`) — a publisher-pinned override now outranks the installer's own saved `User.default_model_override_building` the same way it outranks it for the publisher (see `EnvironmentService.create_environment` resolution order in [Model Freshness](../agent_environments/model_freshness.md)). **Not imported when the mode's effective SDK resolves to `openai_compatible`** — `InstallService._importable_model_override` drops it in that case because an `openai_compatible` model id is only meaningful against the credential endpoint it was pinned on (see `InstallService` below). `apply_update` does NOT re-sync this onto an already-installed env — it reaches new installs only |
| `model_override_conversation` | varchar(128) | Same as `model_override_building`, conversation mode |
| `required_credential_specs` | JSON | `[{name, type, allow_sharing, allow_template_sharing, description, provided_by, publisher_credential_id, template_data?, template_private_fields?, service_uri?}]`. `provided_by` is `"user"`, `"publisher"`, or `"template"`; `publisher_credential_id` is a UUID string or null (only set when `provided_by="publisher"`); `template_data` and `template_private_fields` are present only when `provided_by="template"`; `service_uri` is a plaintext slot-id string or absent (coalesces to `None` on read — legacy-safe). Revisions written before Phase 1 lack these fields — readers default missing keys to `"user"` / `null` / absent |
| `schedules` | JSON | `[{name, cron_string, description, prompt, schedule_type, command, enabled}]` — snapshot of the publisher install's `AgentSchedule` rows at publish time. `next_execution`/`last_execution` are never stored; they are recomputed per-install on materialisation. Empty list `[]` on revisions created before bundle-scheduler propagation was introduced (fully backward compatible) |
| `plugin_specs` | JSON | `[{marketplace_name, plugin_name, version, commit_hash, conversation_mode, building_mode, disabled, config, snapshot_subdir}]` — snapshot of the publisher install's `AgentPluginLink` rows at publish time; plugin files live in the snapshot tree under `plugins/`. Empty list `[]` on revisions published before plugin propagation was introduced |
| `description` | text NULLABLE | Snapshot of `Agent.description` at publish time. `NULL` means "this snapshot did not carry the field" — restore skips the field rather than clobbering the consumer's current value (missing-key-tolerant). Added in migration `d9b3e1a7c45f` |
| `example_prompts` | JSON NULLABLE | Snapshot of `Agent.example_prompts` (list of strings). `NULL` = absent from older snapshots — restore skips. Added in migration `d9b3e1a7c45f` |
| `status_refresh_command` | varchar(1024) NULLABLE | Snapshot of `Agent.status_refresh_command`. `NULL` = absent from older snapshots — restore skips. Note: `STATUS.md` content is per-install App Data and is deliberately NOT snapshotted. Added in migration `d9b3e1a7c45f` |
| `agent_api_enabled` | bool NULLABLE | Snapshot of `Agent.agent_api_enabled`. The `agent_api/` workspace dir and `policy.yaml` also travel in the workspace tree; this flag restores the on/off switch. Per-install tokens and access grants are NOT snapshotted. `NULL` = absent from older snapshots. Added in migration `d9b3e1a7c45f` |
| `agent_api_identity_enabled` | bool NULLABLE | Snapshot of `Agent.agent_api_identity_enabled`. `NULL` = absent from older snapshots. Added in migration `d9b3e1a7c45f` |
| `a2a_config` | JSON NULLABLE | Snapshot of the full `Agent.a2a_config` dict (skills / version / generated_at / enabled). The A2A card (`skills` with LLM-generated descriptions) does not auto-regenerate on install, so the published capability contract is preserved here. Per-install `AgentAccessToken` rows are NOT snapshotted. `NULL` = absent from older snapshots. Added in migration `d9b3e1a7c45f` |
| `agent_sdk_config` | JSON NULLABLE | Snapshot of `Agent.agent_sdk_config` (tool approval allowlists). `NULL` = absent from older snapshots. Added in migration `d9b3e1a7c45f` |
| `webapp_enabled` | bool NULLABLE | Snapshot of `Agent.webapp_enabled`. `NULL` = absent from older snapshots. Added in migration `d9b3e1a7c45f` |
| `snapshot_path` | varchar(1024) NOT NULL | Absolute path under `BUNDLE_STORAGE_DIR` |
| `content_hash` | varchar(64) NOT NULL | SHA-256 hex over snapshot tree + manifest |
| `published_by_user_id` | UUID FK → user ON DELETE SET NULL | |
| `published_at` | timestamp NOT NULL | |
| `release_notes` | text | |
| `origin` | varchar(32) NOT NULL DEFAULT `'publish'` | Provenance discriminator: `"publish"` for catalog publishes (written by `PublishService`) or `"git"` for internal dirty-check baselines written by git operations (push, connect, checkout, pull). Module constants `REVISION_ORIGIN_PUBLISH = "publish"` and `REVISION_ORIGIN_GIT = "git"` in `backend/app/models/bundles/agent_bundle_revision.py`. Migration `878bc3f6579f`. Not exposed on `AgentBundleRevisionPublic` — filtering is server-side. |

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
| `last_update_status` | varchar(64) | `synced`, `failed`, or null. Set to `"degraded"` when the install has no `router_trigger_prompt` and no auto-route could be created |
| `last_update_attempt_at` | timestamp NULLABLE | Added in migration `04e32c2c255a` (`down_revision = e4c1b7d92f08`). Stamped and committed by `InstallService.sweep_automatic_updates` immediately before it calls `apply_update` on a given install, so a crash mid-apply still records the attempt. NULL means "never attempted by the sweep" — treated as backoff-eligible. Paired with `last_update_status = "failed"` it drives `BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS` retry backoff. Not exposed on `AgentPublic` |
| `publish_settings` | JSON DEFAULT `{}` | Publisher-only override map. Meaningful only on `is_publisher_install=True` rows. Shape: `{"credential_overrides": {"<spec_name>": {"provided_by": "user" \| "publisher" \| "template"}}}`. Added in Phase 5 migration `bb2cd3e4f5a6`; `"template"` value added with the template-sharing feature (migration `cc3de4f5a6b7`) |
| `router_trigger_prompt` | text NULLABLE | Natural-language description of when the App MCP router should pick this agent. Editable by the agent owner via `PATCH /agents/{id}/router-trigger-prompt` (no developer gate). Snapshotted into `AgentBundleRevision` at publish; propagated back onto the install's row and its auto-managed `AppAgentRoute` on apply-update |

Dropped columns (Phase 2 migration): `is_clone`, `parent_agent_id`, `clone_mode` and their indexes.

Unique constraints on `agent`:
- `uq_agent_bundle_id_per_publisher` on `(owner_id, bundle_id, is_publisher_install)` — ensures a user cannot hold two publisher installs or two consumer installs of the same bundle, while allowing them to hold one of each.
- `uq_agent_publisher_install_per_bundle` — partial unique index on `(bundle_uuid) WHERE is_publisher_install = true` — globally at most one publisher install per bundle.

## API Endpoints

### Bundle Management (`/api/v1/bundles`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/bundles/` | `require_developer` | List bundles owned by current user |
| `GET` | `/bundles/{bundle_uuid}` | CurrentUser | Detail; visibility-checked for non-publishers |
| `PATCH` | `/bundles/{bundle_uuid}` | `require_developer` + owner | Update display_name, visibility, is_listed, default_install_mode. Also accepts `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id` (Phase 1): non-null values are validated as AI credentials owned by the bundle publisher AND as having a `type` that matches the publisher install's env SDK provider for that mode (e.g. `opencode/anthropic` only accepts an `anthropic`-typed credential — strict full-SDK match via `sdk_constants.sdk_expected_credential_type`). Returns 400 on either validation failure; explicit `null` clears the field |
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
| `GET` | `/catalog/{bundle_id}/install-context` | CurrentUser | NEW (Phase 3). Returns `CatalogInstallContext` containing the `CatalogEntryPublic`, `ai_provided_by_publisher` flag, publisher AI credential name+type summaries (never secrets), and per-spec `InstallContextSpec` list each carrying `suggested_credential_id`/`suggested_credential_name` from the auto-prefill matcher (Tiers 0a/0b `service_uri` match run first when the spec has a `service_uri`) and the spec's `service_uri` (informational). 404 when bundle is not visible to the caller |
| `POST` | `/catalog/{bundle_id}/install` | CurrentUser | Body: `InstallRequest`. Accepts only the typed per-spec `InstallCredentialSelection` shape (`{mode, credential_id?}`) in `credentials`. The legacy `dict[str, str \| dict]` payload shim from Phase 3 was dropped in Phase 5; submitting the old format returns HTTP 422. Validation: `mode="use_existing"` rejected (HTTP 422) for a spec whose `provided_by="publisher"`; omitted spec keys treated as `mode="placeholder"` |
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
| `GET` | `/agents/{agent_id}/setup-status` | owner | Returns `SetupStatusResponse(status, missing[], setup_url)`. Omits `user_message` — frontend renders its own copy. Same gate scan as runtime (Phase 4) |
| `GET` | `/agents/{agent_id}/setup-credentials` | owner | Returns `list[SetupCredentialSummary(id, name, type, description, template_private_fields, template_prefilled_data)]` of incomplete user-owned placeholder credentials linked to this install. For template-materialised placeholders, decrypts the credential and surfaces non-private fields under `template_prefilled_data` so the setup page can render read-only context. Excludes publisher-shared rows (Phase 4) |
| `PUT` | `/agents/{agent_id}/setup-credentials/{credential_id}` | owner | Body: `CredentialUpdate`. Validates credential is owned by the install owner and linked to this install via `AgentCredentialLink`. Calls `CredentialsService.update_credential`, which flips `is_placeholder=False` only when `check_credential_completeness == "complete"` (so partial fills on template placeholders keep the gate engaged). Re-runs gate, emits `INSTALL_SETUP_COMPLETED` if newly ready. Returns `CredentialPublic`. 409 if credential is already non-placeholder (Phase 4) |
| `PATCH` | `/agents/{agent_id}/publish-settings` | `require_developer` + owner | Body: `PublishSettingsUpdate{credential_overrides?: {<spec_name>: {provided_by: "user"\|"publisher"\|"template"}}, ai_credentials?: {conversation_credential_id: uuid\|null, building_credential_id: uuid\|null}}`. Requires `is_publisher_install=True` (400 otherwise). Both top-level fields are partial — omitting one preserves it, sending it (even as empty) replaces it. `credential_overrides` keys must match the names of credentials currently linked to the install; `provided_by` must be `"user"`, `"publisher"`, or `"template"`. `ai_credentials` is the pre-publish draft used while the bundle row does not yet exist; each non-null id must reference an `AICredential` owned by the install owner AND have a `type` matching the install's env SDK provider for that mode (same strict full-SDK rule as `PATCH /bundles/{uuid}`). At first publish, `PublishService._apply_pre_publish_ai_drafts` transfers the draft onto the new `AgentBundle` row. Delegates validation to `InstallService.update_publish_settings`. Returns `AgentPublic` |
| `GET` | `/agents/{agent_id}/bundle-credential-drift` | `require_developer` + owner | Returns `BundleCredentialDrift{stale: bool, drift: list[CredentialSpecDrift]}`. Diffs each linked credential's live `provided_by` (recomputed from current `allow_sharing`/override via `PublishService.resolve_provided_by`) against the latest published revision's snapshot (read via `parse_credential_spec`). Publisher-install owner-only: returns 404 (not 403) for non-owners, non-publisher installs, and installs that have never published, to avoid existence leaks. `stale=False` with an empty drift list when there is nothing to compare against (no latest revision). Credentials removed since the last publish flip `stale=True` but do not emit a per-row `CredentialSpecDrift` entry (no live credential to render). The computation deduplicates linked credentials by name to avoid spurious drift rows from duplicate links. |
| `GET` | `/agents/{agent_id}/bundle-permissions-overview` | `require_developer` + publisher-install owner | Returns `BundlePermissionsOverview`. Read-only aggregator powering the Permissions management card: unions bundle catalog grants (when `visibility == "users"`) with the per-user capability scopes of every identity-enabled connected producer. Delegates to `BundlePermissionsService.build_overview`. Publisher-install owner-only: 404 (not 403) for non-owners, non-publisher installs, and missing agents — mirrors `get_bundle_credential_drift` to avoid existence leaks. The owner-gated producer reads (`AgentApiGrantService.list_grants` / `.get_scope_catalog`) run **only** for producers the caller can manage (`can_manage=True`); non-manageable connected producers come back with empty `grants`/`scope_catalog` and a populated `owner_email` for the read-only "Managed by" UI treatment. No new write routes — all mutations reuse the existing `POST/DELETE /bundles/{bundle_uuid}/grants[/{grant_id}]` and `POST/PUT/DELETE /agents/{producer_id}/agent-api/grants[/{grant_id}]` endpoints unchanged |

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
| `revision_install_count(session, revision_id)` | Single-revision install count — **excludes the publisher's working install** (`is_publisher_install=True`). Used by `POST /agents/{id}/publish` to wire the response |
| `list_revisions_with_install_counts(session, bundle)` | Returns `(revision, install_count)` pairs newest-first using one aggregated query (replaces the per-row count loop). **Filtered to `origin="publish"` only** — git baseline revisions (from push, connect, checkout, pull) are excluded so they never appear in the Revisions UI and do not affect the publish dialog's next-version suggestion. **Install counts exclude the publisher's own working install** (`is_publisher_install=True`) — that copy is the source of the revision, not an install of it |
| `update_bundle(session, bundle, data: AgentBundleUpdate)` | Validates visibility/install_mode values; also validates that any non-null `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id` references an `AICredential` owned by `bundle.publisher_user_id` AND has a `type` matching the publisher install's env SDK provider for that mode (looked up via `_publisher_env_for`; uses `sdk_constants.sdk_expected_credential_type` for the strict full-SDK comparison). Raises `BundleValidationError` (400) on either failure. Explicit `null` clears the publisher-provides state for that mode. The SDK check is skipped when the publisher install has no active environment yet — the runtime gate handles the missing-env path |
| `_publisher_env_for(session, bundle)` | Resolves the `AgentEnvironment` of the bundle's publisher install (the `Agent` row with `bundle_uuid=bundle.id AND is_publisher_install=True`). Returns `None` when the publisher install or its `active_environment_id` is missing — callers treat that as "skip the SDK check". Used by `update_bundle` and `PublishService._validate_publisher_ai_credentials_sdk` |
| `delete_revision(session, bundle, revision_id)` | Raises `RevisionNotFoundError` (404) if revision missing; `RevisionInUseError` (409) if any foreign install references it. Detaches publisher install(s), rewires `latest_revision_id` to the previous remaining `origin="publish"` revision (git baselines are excluded from this candidate set — a git baseline can never become `bundle.latest_revision_id`), removes the on-disk snapshot tree best-effort. **Single-transaction**: when the deletion empties the bundle (no revisions remain), the bundle row itself is also dropped in the same commit via `session.flush()` ordering — cascades clear grants and the publisher install's `bundle_uuid` (FK `ON DELETE SET NULL`), reverting it to an unpublished, rename-able state |
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
| `_snapshot_workspace_tree(env_workspace_root, dest)` | Full-tree capture into `dest/workspace/`. Iterates `iter_bundle_toplevel(env_workspace_root)` from `workspace_classification`; copies every bundle-owned top-level entry — including `webapp/`, `agent_api/`, and any custom top-level dir — while skipping `BUNDLE_EXCLUDED_TOPLEVEL` and runtime-name-denylisted entries. Top-level symlinks are skipped by `iter_bundle_toplevel`; nested symlinks are skipped by `safe_copytree`. `plugins/` routes through `_copy_plugins_tree` to strip derived files. When `env_workspace_root is None` (no active env), leaves `workspace/` empty. |
| `_assert_workspace_readable(env, env_workspace_root)` | Pre-flight guard called in `_publish_locked` before any snapshot work. Raises `ValueError` (mapped to 400 at the route) naming the env and path when an active env's `app/workspace/` directory is missing or unreadable. No-active-env (`env is None`) is allowed. An empty workspace dir (no bundle-owned entries) is NOT an error — that is handled by the empty-workspace-publish UI warning. |
| `_hash_tree_with_manifest(root, manifest)` | SHA-256 over sorted file paths + content + manifest body |
| `resolve_provided_by(credential, publisher_install)` | Single source of truth for `provided_by` resolution — public static method. Order: (1) `publisher_install.publish_settings["credential_overrides"][credential.name]["provided_by"]` if it equals `"user"`, `"publisher"`, or `"template"`; (2) inference — `allow_sharing=True` → `"publisher"`; else `allow_template_sharing=True` → `"template"`; else `"user"`. Used by both `_collect_credential_specs` (publish-time spec emission) and `CredentialsService.list_bundle_usages` (`GET /credentials/{id}/bundles` projection) so the two paths cannot disagree |
| `_collect_credential_specs(session, install)` | Reads linked `AgentCredentialLink` rows and emits the evolved per-spec shape: `{name, type, allow_sharing, allow_template_sharing, description, provided_by, publisher_credential_id, template_data?, template_private_fields?, service_uri?}`. Delegates `provided_by` resolution to `resolve_provided_by`. For `"template"` specs, calls `_template_payload_for` to attach `template_data` and `template_private_fields`. Emits `"service_uri": cred.service_uri` for every linked credential (None values are serialised as absent from the spec JSON). Because `service_uri` contributes to the manifest hash, stamping a new slot id on a linked credential and re-publishing yields a new `content_hash` → pending-update on existing installs (expected) |
| `_template_payload_for(session, cred)` | Returns `(template_data, template_private_fields)` for a template spec. Decryption failures raise `ValueError` rather than silently shipping an empty payload. For types in `_TEMPLATE_FORCE_PRIVATE_TYPES` (OAuth + Google service account) returns `({}, [])` regardless of UI state. Strips fields named in `cred.template_private_fields`, then applies the per-type templatable allowlist `_TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE` (e.g. `ssh_key` → only `host_aliases`) so private keys / refresh tokens / generated material can never leak into the bundle revision JSON |
| `_validate_publisher_provides(session, install)` | Called from `_publish_locked` before `_collect_credential_specs`. Walks linked credentials, resolves each via `resolve_provided_by`, and asserts the matching consent flag is set on the underlying `Credential` row (`provided_by="publisher"` requires `allow_sharing=True`; `provided_by="template"` requires `allow_template_sharing=True`). Security enforcement point: an override on a credential without the matching consent flag fails publish with a descriptive error |
| `compute_credential_spec_drift(session, install)` → `BundleCredentialDrift` | Diffs each credential currently linked to the publisher install against the latest published revision's `required_credential_specs`. Live side: calls `resolve_provided_by(cred, install)` per linked credential, deduplicating by credential name. Snapshot side: reads `revision.required_credential_specs`, parses each entry via `parse_credential_spec`, and indexes by credential name. Emits one `CredentialSpecDrift{name, type, live_provided_by, snapshot_provided_by, drifted}` per linked credential. A credential linked since the last publish (not in the snapshot) is treated as having snapshot `provided_by="user"` and flagged as drifted. Credentials present in the snapshot but no longer linked do not produce a `CredentialSpecDrift` row (no `type` to show), but do flip `stale=True`. Returns `BundleCredentialDrift(stale=False, drift=[])` immediately when the install is not a publisher install, has no `bundle_uuid`, or has never published a revision. Because this method reuses the same `resolve_provided_by` and `parse_credential_spec` as publish and install respectively, the result can never disagree with what publish actually writes or what install actually reads. |
| `_collect_schedule_specs(session, install)` | Snapshots the publisher install's `AgentSchedule` rows via `schedule_sync.snapshot_schedules`. The result is stored in both `manifest["schedules"]` and `revision.schedules`. Because the schedule snapshot is part of the manifest body that feeds `content_hash`, a schedule-only change produces a new hash so foreign installs see a pending update |
| `_validate_publisher_ai_credentials_sdk(session, install, bundle, env)` | Publish-time pre-flight. For each non-null `bundle.publisher_ai_credential_*_id`, looks up the `AICredential.type` and compares it against `env.agent_sdk_conversation` / `agent_sdk_building` via `sdk_constants.sdk_expected_credential_type`. Raises `ValueError` (mapped to 400 at the route) when a mismatch is found — last line of defence when a publisher changed either the env's SDK or the bundle's AI credential FK between `PATCH /bundles/{uuid}` validation and the publish call. No-op when `env is None` (snapshot-only publish with no workspace files) |
| `_apply_pre_publish_ai_drafts(session, install, bundle)` | First-publish-only helper. Reads `install.publish_settings["ai_credentials"]` (the pre-publish draft set via `PATCH /agents/{id}/publish-settings` while the bundle row didn't yet exist) and writes the resolved UUIDs onto `bundle.publisher_ai_credential_*_id`. Validates ownership defensively; mismatches are logged and skipped. After this point the bundle FK columns are the source of truth and the picker writes directly to `AgentBundle` via `PATCH /bundles/{uuid}` |

Publish flow detail:
1. Lock acquired on `bundle_id` string
2. Bundle row resolved or created (first publish). On first publish, `_apply_pre_publish_ai_drafts` transfers any pre-publish AI credential draft from `install.publish_settings["ai_credentials"]` onto the new bundle's FK columns
3. Next `revision_number = MAX(existing) + 1`
4. Pre-flight validators run **before** the `<rev>.tmp` directory is created (a failed pre-flight leaves no orphan temp dir):
   - `_assert_workspace_readable`: raises `ValueError` (400) when the publisher has an active env but its `app/workspace/` is missing or unreadable on disk
   - `_validate_publisher_ai_credentials_sdk`: rejects when a publisher AI credential's type no longer matches the env's per-mode SDK
   - `_ensure_publisher_plugin_files`: hard-blocks when any declared plugin's files are absent from the publisher env workspace
5. DB-bound collectors (`_collect_credential_specs`, `_collect_schedule_specs`) and `build_manifest` run on the event loop, then the filesystem-heavy work is handed off: `<rev>.tmp` directory created and all FS operations — `_snapshot_workspace_tree` (full `app/workspace/` tree capture into `tmp/workspace/`, schema_version 2), `manifest.json` write, `content_hash` computation, and the atomic `<rev>.tmp → <rev>` rename — are executed inside a sync helper `_write_snapshot_to_disk` dispatched via `await asyncio.to_thread(...)`. This keeps the asyncio event loop free for concurrent chat streaming, WebSocket traffic, and API requests while the snapshot runs. The sync `Session` is not passed into the thread; all DB work stays on the loop. The manifest's `prompts` block includes `router_trigger` from `install.router_trigger_prompt` alongside `workflow`, `entrypoint`, and `refiner`.
6. `AgentBundleRevision` row inserted with `origin="publish"` (module constant `REVISION_ORIGIN_PUBLISH`), `router_trigger_prompt=install.router_trigger_prompt`, and `schedules=schedule_specs`
7. `bundle.latest_revision_id` and `install.installed_revision_id` updated. **On every publish** (not just the first) `bundle.display_name = display_name or install.name` and `bundle.description = description if description is not None else install.description` are also rewritten — a publisher who renames their agent and republishes propagates the new name to the catalog and to any new install. This never touches the `Agent.name` of existing foreign installs (the revision snapshot never carries `name`)
8. `BUNDLE_PUBLISHED` event emitted
9. `notify_installs()` called

### `InstallService`

| Method | Notes |
|--------|-------|
| `install_bundle(session, user, bundle, request)` | Idempotent for consumer installs — returns the existing consumer install (`is_publisher_install=False`) if one already exists for `(owner_id, bundle_uuid)`. If the caller is the bundle's publisher and only a publisher install exists (no consumer copy yet), a fresh consumer install is created. Calls `_link_publisher_ai_credential` BEFORE the idempotent early-return so re-installs self-heal a deleted `AICredentialShare` |
| `install_bundle_for_email(session, publisher_agent_id, recipient_user_id)` | Auto-publishes on first call if bundle has no revisions |
| `admin_install(session, target_user, bundle, request)` | Thin wrapper over install_bundle |
| `_apply_revision_metadata(install, revision)` | Static helper shared by fresh install / checkout, apply-update, and git pull. Copies the 8 definitional metadata fields (`description`, `example_prompts`, `status_refresh_command`, `agent_api_enabled`, `agent_api_identity_enabled`, `a2a_config`, `agent_sdk_config`, `webapp_enabled`) from a revision onto an install. **Publisher-authoritative + missing-key-tolerant**: each field is written ONLY when the revision column is not `None` — a `NULL` column (older snapshot that predates the field) leaves the install's current value untouched. Deep-copies mutable JSON payloads so the install never aliases the immutable revision row. Tokens / grants / per-install UI prefs are NOT included |
| `_importable_model_override(override, effective_sdk)` | Static helper called independently per mode (conversation / building) from `_install_from_revision`, just before `AgentEnvironmentCreate` is built. Returns `override` unchanged unless the mode's effective SDK resolves to provider `openai_compatible` (parsed off the `"engine/provider"` SDK id string — a missing or engine-only value defaults to provider `anthropic`), in which case it returns `None` and the imported override is dropped. Reason: an `openai_compatible` model id names a model inside the *endpoint owner's* namespace, so a publisher's pin is not portable to the consumer's own `openai_compatible` credential — `resolve_model` (`model_catalog.py`) honours a truthy override before its `openai_compatible` branch and `model_health_service` reports `openai_compatible` as always `ok`, so an imported id would otherwise fail invisibly (hard provider error behind a green badge). Deliberately scoped to *imported* (publisher-authored) overrides only — `resolve_model` itself is untouched, so a user who pins a model on their own `openai_compatible` environment still has it win; `resolve_model` cannot tell who authored an override, so the same rule there would break that legitimate case |
| `apply_update(session, install)` | Stops env, calls `replace_bundle_content`, updates prompts + bookkeeping fields, calls `_apply_revision_metadata` to overwrite the 8 definitional metadata fields from the new revision (publisher-authoritative, missing-key-tolerant), refreshes the auto-managed App MCP route, merges schedules via `schedule_sync.merge`, restarts env, emits `INSTALL_UPDATE_APPLIED` event |
| `uninstall(session, install)` | Delegates to `AgentService.delete_agent` which handles orphaning |
| `set_update_mode(session, install, mode)` | |
| `check_for_updates(session, install)` | Returns `CheckUpdatesResponse`: `{pending_update, installed_revision_number, latest_revision_number, installed_version, latest_version, last_update_status, last_sync_at, update_mode, latest_release_notes, latest_published_at}`. `installed_version` and `latest_version` are the human-friendly `AgentBundleRevision.version` labels (e.g. `"1.0"`, `"1.1"`); both are nullable — `null` when the revision was created before version labels were introduced, with the UI falling back to revision numbers. `latest_release_notes` / `latest_published_at` are read straight off the resolved latest revision row (additive; no migration) and feed `BundleInstallationCard`'s "Latest available" panel |
| `edit_bundle_id(session, install, new_bundle_id)` | Raises 409 if already published; validates format and uniqueness |
| `_normalise_credentials_payload(credentials_raw, revision_specs)` | Phase 3 addition; legacy shim dropped in Phase 5. Now only the typed `dict[str, InstallCredentialSelection]` shape is accepted. Each value must be a dict (or `InstallCredentialSelection` instance) with a `mode` key in `{"use_existing", "placeholder", "publisher_provides", "skip"}`; a bare `str` or any other type returns HTTP 422. Unknown mode values are coerced to `"placeholder"` to avoid aborting installs from misconfigured clients |
| `_setup_install_credentials(session, install, revision, user_provided_data)` | Branches on each spec's `provided_by` field. `"template"` → calls `_materialise_template_credential` unless the installer opted in via `mode="use_existing"`; falls through to placeholder + `last_update_status="degraded"` on materialisation failure. `"publisher"` → calls `_try_link_publisher_credential`; on failure falls through to placeholder and marks install degraded. `"user"` (or missing field — backward compat) → consumes the normalised `InstallCredentialSelection` dict from `_normalise_credentials_payload`; `mode="use_existing"` for a publisher spec raises HTTP 422; omitted spec key treated as `mode="placeholder"` |
| `_materialise_template_credential(session, install, spec)` | Creates a fresh `Credential` row owned by the installer with `encrypted_data` initialised from `spec["template_data"]`, `is_placeholder=True`, `allow_sharing=False`, `allow_template_sharing=False`, and `template_private_fields` mirrored from the spec. Drops any field that would also appear in `template_private_fields` from the seeded data (defence in depth). Returns `True` on success; `False` for an unknown credential type so the caller falls back to the regular placeholder path |
| `_try_link_publisher_credential(session, install, publisher_credential_id_raw, spec_name)` | Validates the publisher's `Credential` row (exists, `allow_sharing=True`, owned by the bundle publisher), ensures a `CredentialShare` (publisher → installer) exists, inserts the `AgentCredentialLink`. Returns `True` on success, `False` on any validation failure |
| `_link_publisher_ai_credential(session, user, bundle)` | For each non-null `bundle.publisher_ai_credential_*_id`, idempotently creates an `AICredentialShare` (publisher → installer) via `ai_credentials_service.share_credential`. Skips share-with-self when the installer is the publisher. Failures are logged as warnings and do not abort the install |
| `update_publish_settings(session, install, *, credential_overrides, ai_credentials)` | Validates and persists a partial update to `install.publish_settings`. Both arguments are partial — `None` leaves that section untouched, a populated value replaces it. Asserts `is_publisher_install=True`, that override keys match currently-linked credential names, that each `provided_by` is `"user"`/`"publisher"`/`"template"`, that any non-null AI credential id is owned by the install owner, AND that the AI credential's `type` matches the install's env SDK provider for that mode (via `sdk_constants.sdk_expected_credential_type`). Raises `ValueError` (route maps to HTTP 400) on any validation failure. Implemented alongside `_validate_credential_overrides` and `_validate_ai_credentials_draft` helpers — the latter holds the SDK-vs-credential-type check |
| `list_setup_credentials(session, install)` | Returns `list[SetupCredentialSummary]` for the install's user-fillable placeholder credentials (owner-owned, linked, `is_placeholder=True`). For template-materialised rows, decrypts and surfaces non-private fields under `template_prefilled_data` so the setup page can render read-only context; decryption failures fall back to an empty prefilled dict so a corrupted credential still surfaces. Backs the `GET /agents/{id}/setup-credentials` route |

| `_materialise_schedules(session, install, revision)` | Thin wrapper over `schedule_sync.materialise` that creates `AgentSchedule` rows from `revision.schedules`. Called at the end of `_install_from_revision` (step 9). Best-effort — exceptions propagate to the call site which marks the install degraded |
| `_auto_create_app_mcp_route(session, install, revision, user)` | Creates an `AppAgentRoute` + self-assignment (`activate_for_myself=True`) for the installer using `revision.router_trigger_prompt` as the trigger. Skips (and marks install `last_update_status="degraded"`) when `router_trigger_prompt` is empty. Idempotent — skips when an `is_auto_managed=True` route already exists for this agent. Exceptions are caught at the call site so install never aborts |
| `_refresh_or_create_auto_route_on_update(session, install, revision)` | Apply-update hook. If an `is_auto_managed=True` route exists, refreshes `trigger_prompt` and `name` from the new revision. If no route exists and a manual (`is_auto_managed=False`) route is already present, does nothing. If neither exists and the revision has a trigger prompt, calls `_auto_create_app_mcp_route`. No longer the only recovery path for a route-less install — `AppAgentRouteService._create_auto_route_for_agent` mints the route as soon as the owner saves a trigger prompt |

Install flow (`_install_from_revision`):
1. `Agent` row created from revision prompts + SDK settings; `_apply_revision_metadata` is then called to overwrite the 8 definitional metadata fields from the revision (publisher-authoritative, missing-key-tolerant — `NULL` revision columns leave the `Agent` defaults in place)
2. Name uniqueness enforced (appends "(2)", "(3)" etc.)
3. AI credential resolution — before env creation, the resolution chain is applied for each mode: (a) `bundle.publisher_ai_credential_*_id` if non-null; (b) the installer's `request.ai_credential_selections` value; (c) `None`. `_link_publisher_ai_credential` is called first so the `AICredentialShare` row exists at env-create time when the bundle provides an AI credential
4. `AgentEnvironment` created via `EnvironmentService.create_environment` with the resolved credential ids, **the revision's full SDK block (`agent_sdk_*` and `model_override_*`)**, and `bundle_snapshot_path=revision.snapshot_path`. Before building `AgentEnvironmentCreate`, each mode's `model_override_*` passes through `InstallService._importable_model_override(override, effective_sdk)` — the effective per-mode SDK is resolved with the SAME fallback chain `create_environment` itself applies (revision's `agent_sdk_conversation`/`agent_sdk_building` → installer's `user.default_sdk_*` → `DEFAULT_SDK`; a `NULL` `agent_sdk_building` stays `NULL` rather than falling back, mirroring `create_environment`'s "building mode not needed" case), and the override is dropped when that mode's provider is `openai_compatible` (see `InstallService` below). Subject to that filter, the publisher's per-mode model overrides travel to every install, not just git checkout — `create_environment` still falls back to the installer's own `User.default_model_override_*` when the revision leaves them `NULL` (or when the filter suppressed them). The env service uses `ai_credentials_service.can_access_credential(user, cred)` (owner OR share recipient) rather than a strict `owner_id == user.id` check, so shared publisher AI credentials pass through at this step
5. Workspace seeding happens **inside the background env build**, not in the foreground install path. `create_environment` forwards `bundle_snapshot_path` to `_create_environment_background`, which calls `seed_workspace_from_bundle_snapshot` **after** `create_environment_instance` materialises the instance dir from the template and **before** `start_environment` boots the container. This ordering is required: seeding in the foreground (the historical bug) raced the async build and either no-op'd on a missing instance dir or was clobbered by the template materialisation, shipping installs with empty bundle-owned dirs (e.g. `scripts/` containing only the template README). The apply-update path (`replace_bundle_content`) is unaffected — it runs after the env is explicitly stopped, so it is already correctly ordered
6. `AppDataService.get_or_create_volume` called with `catalog_type="server"` (consumer path) or `catalog_type=None` (publisher install path); existing orphaned volume reattached when the key matches
7. `_setup_install_credentials` called — branches on `provided_by` per spec; PBP specs link the publisher's row; PBU specs create placeholders or link installer selections
8. `_auto_create_app_mcp_route` called — creates `AppAgentRoute` + self-assignment from the revision's `router_trigger_prompt`; marks install degraded when prompt is empty
9. `_materialise_schedules` called — creates `AgentSchedule` rows from `revision.schedules` via `schedule_sync.materialise`; best-effort (failure logs a warning and marks the install `last_update_status="degraded"` but does not abort the install). Created rows are ordinary `AgentSchedule` rows picked up by the background scheduler

#### Automatic Update Convergence

The suspension-time hook in `environment_suspension_scheduler.py` only applies a pending update on the *running → suspended* transition — an install whose environment was already suspended, stopped, or absent when a revision was published is never revisited by that hook. The methods below close that gap; both new entry points share one implementation.

| Method / constant | Notes |
|--------|-------|
| `AUTO_UPDATE_ALLOWED_ENV_STATUSES` | Module-level `frozenset({"suspended", "stopped"})` in `install_service.py`. Deliberately an allowlist — `running`, `error`, and every transitional status (`creating`, `building`, `initializing`, `starting`, `rebuilding`, `activating`) are always skipped by the sweep; only the suspension-time hook ever touches a running environment |
| `sweep_leader_session()` | Module-level context manager. Yields a `Session` bound to a dedicated `engine.connect()` connection holding a Postgres `pg_try_advisory_lock` on a fixed key (`BUNDLE_AUTO_UPDATE_LOCK_KEY`), or yields `None` if another process already holds it. Shared by **both** the periodic scheduler and the publish-time fast path so the two can never apply the same install concurrently. Under `settings.TESTING` it bypasses the advisory lock entirely and yields the patched test session, since there is no cross-process concurrency to guard against in tests |
| `InstallService.sweep_automatic_updates(session, *, bundle=None, limit=50)` | The shared implementation. Selects `Agent` rows where `is_publisher_install=False`, `update_mode="automatic"`, the bundle has a `latest_revision_id`, and `installed_revision_id IS DISTINCT FROM` it — selection is on **revision mismatch**, not `pending_update`, so a lost notification self-heals on the next sweep. `bundle` restricts the sweep to one bundle (publish fast path); `None` sweeps the whole fleet. One joined query (`Agent` ⋈ `AgentBundle`, LEFT JOIN `AgentEnvironment`) returns plain tuples (not ORM entities, to dodge `expire_on_commit` turning the batch back into per-row `SELECT`s). Per candidate: applies the `last_update_status="failed"` + `BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS` backoff (counted as `deferred`); skips envs outside `AUTO_UPDATE_ALLOWED_ENV_STATUSES` (counted as `skipped`); for installs with an environment, re-reads `AgentEnvironment.status` under `SELECT ... FOR UPDATE SKIP LOCKED` immediately before applying, to shrink the activation race between the batch query and the apply to the workspace copy itself; stamps and commits `install.last_update_attempt_at = now()` **before** calling `apply_update` so a mid-apply crash still lands in the backoff window; wraps each install's `apply_update` call in its own `try/except` so one failure never aborts the batch; stops once `limit` installs have been attempted, logging the remainder for the next run. Returns `{"applied": int, "skipped": int, "failed": int, "deferred": int}` |
| `InstallService._mark_update_failed(session, install_id)` | Best-effort failure bookkeeping called from the sweep's `except` block. `apply_update` already stamps `last_update_status="failed"` for errors raised inside its own try block, but the sweep's own guard clauses (e.g. the row-lock re-read) can raise earlier, before that stamp — this ensures `last_update_status`/`last_update_attempt_at` are always set on any sweep failure so the backoff gate engages instead of retrying every run forever. Swallows its own exceptions |
| `InstallService.sweep_bundle_updates_background(bundle_uuid)` | Bundle-scoped sweep for detached background use — the publish-time fast path. Takes its own `sweep_leader_session()` (never reuses the request session, since the task outlives the request); if the periodic sweep already holds the lock, this one skips entirely and logs that the periodic run will converge it instead. No-op when `BUNDLE_AUTO_UPDATE_ENABLED=False` |

**Scheduler module** — `backend/app/services/environments/bundle_auto_update_scheduler.py` follows the same shape as `environment_suspension_scheduler.py` / `model_discovery_scheduler.py`: a module-level `BackgroundScheduler`, a sync `run_bundle_auto_update()` entry that wraps `asyncio.run(_sweep_automatic_updates())`, and `start_scheduler()` / `shutdown_scheduler()`. `_sweep_automatic_updates()` opens a `sweep_leader_session()` and calls `InstallService.sweep_automatic_updates(session, limit=settings.BUNDLE_AUTO_UPDATE_BATCH_LIMIT)` fleet-wide (no `bundle` filter). Registered in `backend/app/main.py` inside the existing `if not settings.TESTING:` startup block alongside `start_suspension_scheduler()`, with the matching `shutdown_bundle_auto_update_scheduler()` call at shutdown — so, like every other scheduler in this codebase, it is **never registered under `settings.TESTING`**. Integration tests therefore reach the sweep logic only via a direct call to `InstallService.sweep_automatic_updates` / `sweep_bundle_updates_background`, or indirectly through the publish-time fast path — never through the periodic job itself.

**Publish-time fast path** — `PublishService.notify_installs` (after marking `pending_update=True` and emitting `INSTALL_UPDATE_AVAILABLE` on all foreign installs) schedules `InstallService.sweep_bundle_updates_background(bundle.id)` as a fire-and-forget task via `create_task_with_error_logging` (`app/utils.py`). Publish returns immediately regardless of sweep outcome — a sweep failure is logged but never fails the publish request.

### `CatalogService`

| Method | Notes |
|--------|-------|
| `list_for_user(session, user)` | Union of public listed, grant-visible, and publisher-own bundles |
| `get_for_user(session, bundle_id, user)` | Single entry; returns None if not visible |
| `user_can_see(session, bundle, user)` | Visibility check logic |
| `user_can_install(session, bundle, user)` | `user_can_see` AND `latest_revision_id IS NOT NULL` |
| `_bundle_to_entry(session, bundle, user)` | Resolves a `CatalogEntryPublic` from a bundle row: reads latest revision for `latest_version` / `latest_revision_number`; reads publisher `User` row for `publisher_name` and `publisher_email`; checks the calling user's **consumer** install row (`is_publisher_install=False`) for `is_installed` / `user_install_id` / `user_install_pending_update` (derived from the consumer install's `Agent.pending_update`). The publisher's own working copy is excluded — publishers see their own bundle as installable until they perform a separate consumer install |
| `build_install_context(session, bundle, user) -> CatalogInstallContext` | NEW (Phase 3). Runs the auto-prefill matcher per spec (see below), passing `service_uri=parsed.service_uri` into both the PBU and PBT matcher calls so Tier 0a/0b are applied when the spec has a slot id. Resolves publisher AI credential name+type summaries (no secret values), and returns `CatalogInstallContext`. Called by `GET /catalog/{bundle_id}/install-context` |

### `InstallReadinessGate` (Phase 4)

Stateless service in `backend/app/services/bundles/install_readiness_gate.py`. All methods are `@staticmethod`. Called at every user-message-to-LLM dispatch boundary; never mutates state.

**Dataclasses:**

- `GateResult` — `status: GateStatus`, `missing: list[GateMissingItem]`, `setup_url: str | None`, `user_message: str` (pre-rendered markdown, empty for `ready`).
- `GateMissingItem` — `spec_name: str`, `spec_type: str`, `reason: GateMissingReason`, `is_ai: bool`.
- `GateStatus` — `Literal["ready", "needs_setup", "publisher_broken"]`.
- `GateMissingReason` — `Literal["placeholder_empty", "publisher_credential_missing", "publisher_credential_unshared"]`.

| Method | Notes |
|--------|-------|
| `check(session, install) -> GateResult` | Full gate verdict including `user_message`. Calls `missing_for`, then selects status (`publisher_broken` trumps `needs_setup`), builds the setup URL, and formats the markdown reply |
| `missing_for(session, install) -> list[GateMissingItem]` | Leaner variant (no formatting). Public; used by `GET /setup-status`. Calls `_scan_service_credentials` + `_scan_ai_credentials` |
| `_format_user_message(missing, setup_url, status) -> str` | Renders the plain-text body used in `user_message`. No inline markdown link — the URL travels only in the structured `setup_url` field. `needs_setup` lead: "Setup needed before this agent can run. Open the agent's Credentials tab and fill in the missing values one by one." `publisher_broken` lead: "This bundle's publisher-provided credentials are unavailable. The publisher needs to fix this, or you can supply your own credentials from the agent's Credentials tab." Followed by a bullet list of `- {spec_name} ({spec_type})` items. Chat UI reads `install_setup_required` metadata and renders its own rich block; external clients (MCP, A2A) receive plain text + the `setup_url` field |
| `_build_setup_url(install_id) -> str` | Returns `f"{FRONTEND_HOST}/agent/{install_id}#credentials"` — points at the Credentials tab on the agent detail page (was `/agent/{install_id}/setup-credentials` before the dedicated setup page was removed) |
| `_scan_service_credentials(session, install)` | Walks `AgentCredentialLink` rows. Owner-match → checks `is_placeholder`. Foreign-owned → checks `allow_sharing` + active `CredentialShare` row |
| `_scan_ai_credentials(session, install)` | Checks `bundle.publisher_ai_credential_*_id` FKs. Verifies `AICredential` row exists and `AICredentialShare` exists for the installer (skipped when installer is the publisher) |

**Tech note — `publisher_credential_missing` is largely defensive:** `AgentCredentialLink.credential_id` has `ondelete="CASCADE"`, so deleting a `Credential` row also removes its links before the gate can ever scan them. `AgentBundle.publisher_ai_credential_*_id` has `ondelete="SET NULL"`, so deleting an `AICredential` nulls the bundle FK rather than leaving a dangling reference. In practice, the `publisher_credential_unshared` branch (sharing revoked or `CredentialShare` removed) is the realistic publisher-broken path. The `publisher_credential_missing` branch exists to handle any gap left by cross-service inconsistencies.

**Channel integration:**

| Channel | Insertion point | Non-ready action |
|---------|----------------|-----------------|
| Chat | `SessionService._maybe_short_circuit_with_gate`, called from `initiate_stream` before LLM dispatch | Persists `system`-role `Message` with `user_message`; emits stream event so chat UI renders it; fires `INSTALL_SETUP_REQUIRED` WS event; returns `action="setup_required"` |
| MCP | `MCPRequestHandler.handle_send_message`, after session resolution | Returns `{"response": user_message, "context_id": ..., "setup_url": ...}` directly; fires `INSTALL_SETUP_REQUIRED` WS event |
| A2A | `A2ARequestHandler.handle_message_send`, after session resolution | Synthesises completed Task with `TextPart(user_message)` + `DataPart({type:"cinna.setup_required", setup_url, missing})`; stamps `session.session_metadata["a2a_setup_required_terminated"]=True` so next inbound creates a new task |
| Webhook (session trigger) | `agent_webhook_service._fire_session`, before session creation | Writes invocation log with `status="setup_required"` and structured payload JSON-encoded into `error_message`; fires `INSTALL_SETUP_REQUIRED` (and `PUBLISHER_CREDENTIAL_BROKEN` for publisher-broken status) WS events; returns HTTP 200 with `{status, setup_url, missing}` body |
| Webhook (script trigger) | Skipped | Script triggers do not engage the LLM; gate is not applied |

### `CredentialsService` (relevant additions)

| Method | Notes |
|--------|-------|
| `find_match_for_spec(session, user_id, name, credential_type, *, service_uri=None, fall_back_to_type_only=True) -> Credential \| None` | Used by `CatalogService.build_install_context` to populate `suggested_credential_id` per spec. Full precedence when `service_uri` is a non-empty string: **(Tier 0a)** owned credential matching `service_uri + type` (newest by `id desc`); **(Tier 0b)** shared credential (via `CredentialShare`) matching `service_uri + type`. These tiers short-circuit the remaining tiers, including the PBT value-anchor check. When `service_uri` is None/empty, falls through to: (1) owned `(name, credential_type)` case-insensitive match; (2) shared `(name, credential_type)` case-insensitive match (via `CredentialShare`); (3) if `fall_back_to_type_only=True` (default) — exactly one owned credential of the matching type. Two or more type-only matches return `None` to avoid an ambiguous auto-suggestion. Never returns a credential whose `owner_id` differs from `user_id` unless tier (2) or (0b) applied |
| `update_credential` | Flips `is_placeholder=False` when the saved `credential_data` is non-empty (Phase 4 addition). Covers the setup page commit path so filling a placeholder automatically promotes it to a real credential |

### Auto-prefill Matching

When the install-context endpoint is called, `CatalogService.build_install_context` runs `CredentialsService.find_match_for_spec` once per **PBU and PBT** spec in the latest revision (PBP specs skip the matcher because the publisher's row is already linked). The `parsed.service_uri` from the spec is passed as the `service_uri` keyword argument. The matcher applies the following precedence:

0. **Tier 0a — `service_uri` owned**: if the spec has a non-empty `service_uri`, look for an owned credential with matching `service_uri + type` (newest by `id desc`). Short-circuits all remaining tiers on hit.
0. **Tier 0b — `service_uri` shared**: same lookup through `CredentialShare` — credential shared with the user where `service_uri + type` match. Short-circuits all remaining tiers on hit.
1. Owned credentials (`credential.owner_id == user.id`) matching `(name, type)` case-insensitively — most recent first
2. Credentials shared with the user (via `CredentialShare`) matching the same `(name, type)` — most recent first
3. **Type-only fallback (owned)**: if the user has exactly one owned credential of the matching `type`, return it. Two or more type matches return `None` (the UI surfaces the dropdown so the user picks explicitly)

When `service_uri` is `None` or empty on the spec, Tiers 0a and 0b are not attempted and the function is equivalent to the pre-feature behavior. Old revision JSON that lacks the `service_uri` key coalesces to `None` in `parse_credential_spec`.

Running the matcher for PBT specs gives the install page the option to reuse a previously-materialised template credential when a user uninstalls and then reinstalls a bundle — without it, the install service would always create a duplicate placeholder. When the installer accepts a PBT spec suggestion, the install request submits `mode="use_existing"` and `InstallService._setup_install_credentials` honours that, bypassing template materialisation.

The response carries only `suggested_credential_id` and `suggested_credential_name` (never the credential's secret data). The frontend shows the suggestion as a pre-selected option; the user must explicitly confirm or override it. Accepted suggestions are submitted as `mode="use_existing"` with the matched UUID.

### `BundlePermissionsService`

`backend/app/services/bundles/bundle_permissions_service.py`. One static method; keeps the cross-domain assembly out of the route layer.

| Method | Notes |
|--------|-------|
| `build_overview(session, install, current_user) -> BundlePermissionsOverview` | 1) Resolves `bundle = BundleService.get_bundle_by_uuid(session, install.bundle_uuid)` and `bundle_access_applicable = bundle.visibility == "users"`. 2) Reads `BundleService.list_grants(session, bundle)` only when `bundle_access_applicable`, building a `user_id → bundle_grant_id` map. 3) Calls `AgentApiTokenService.list_connected_producers(session, install.id, current_user.id, current_user.is_superuser)` (see [Agent REST API tech reference](../agent_api/agent_api_tech.md)) for the identity-enabled connected producers. 4) For each producer **where `row.can_manage` is true**, calls the existing owner-gated `AgentApiGrantService.list_grants` and `.get_scope_catalog` to populate `grants`/`scope_catalog`; for non-manageable producers these stay `[]` — **the owner-gated read is never invoked**, which is the structural enforcement point (see Security below). 5) Unions every user id referenced by the bundle grants or any manageable producer's grants, resolves all `User` rows in a single `IN`-query, and projects `BundlePermissionUser` rows (`bundle_grant_id` stamped from step 2). 6) Projects `bundle_grants` via the shared `BundleService.grant_to_public` helper using the already-resolved user cache (no extra lookups). 7) Sets `show_card = bundle_access_applicable or len(producers) > 0`. Pure read; never mutates. |

**Security note (structural, not just a check):** `grants` and `scope_catalog` are populated only inside the `if row.can_manage:` branch — for any producer the caller does not own (and isn't superuser for), the owner-gated `AgentApiGrantService` calls are skipped entirely rather than called-and-filtered. A publisher therefore cannot learn another owner's scope state through this surface even via a code defect downstream of the call, because the call itself never executes. This mirrors the route-level `can_manage` gate described in [API Endpoints](#install-operations-api-v1agents) above.

The install-context response never carries credential secrets — only `(name, type)` summary strings are returned for publisher AI credential fields (`ai_publisher_credential_summaries`).

## Bundle Storage Layout (Filesystem)

New revisions use schema_version 2 with a `workspace/` subtree. Legacy schema_version 1 revisions (flat allowlist layout) remain on disk and are fully readable by the seed and apply-update paths.

```
${BUNDLE_STORAGE_DIR}/                       # config: defaults to <DATA_DIR>/bundles/
├── io.opencinna.cinna.a1b2c3d4/            # one dir per bundle_id string
│   ├── 1/                                  # schema_version 1 (legacy flat layout)
│   │   ├── manifest.json                   #   "schema_version": 1
│   │   ├── scripts/                        #   allowlisted folders at snapshot root
│   │   ├── docs/
│   │   ├── knowledge/
│   │   ├── files/
│   │   ├── workspace_requirements.txt
│   │   └── workspace_system_packages.txt
│   ├── 2/                                  # schema_version 2 (full-tree workspace/ subtree)
│   │   ├── manifest.json                   #   "schema_version": 2
│   │   └── workspace/                      #   verbatim copy of app/workspace/ minus the denylist
│   │       ├── scripts/
│   │       ├── docs/
│   │       ├── knowledge/
│   │       ├── files/
│   │       ├── webapp/                     #   now captured (was silently dropped in v1)
│   │       ├── agent_api/                  #   now captured
│   │       ├── plugins/                    #   minus settings.json / manifest.json
│   │       ├── <any custom top-level dir>/ #   now captured
│   │       ├── workspace_requirements.txt
│   │       └── workspace_system_packages.txt
│   └── 3.tmp/                              # leftover from failed publish (debug)
└── io.opencinna.cinna.deadbeef/
    └── ...
```

## Manifest Format

Each revision writes `manifest.json` into the snapshot directory:

```json
{
  "schema_version": 2,
  "bundle_id": "io.opencinna.cinna.a1b2c3d4",
  "revision_number": 3,
  "version": "1.2",
  "content_hash": "sha256:<64-hex>",
  "published_at": "2026-05-06T12:34:56Z",
  "prompts": {
    "workflow": "...",
    "entrypoint": "...",
    "refiner": "...",
    "router_trigger": "..."
  },
  "sdk": {
    "building": "claude-code/anthropic",
    "conversation": "claude-code/anthropic",
    "model_override_building": null,
    "model_override_conversation": null
  },
  "required_credential_specs": [
    {
      "name": "gmail",
      "type": "imap",
      "allow_sharing": false,
      "allow_template_sharing": false,
      "description": null,
      "provided_by": "user",
      "publisher_credential_id": null
    },
    {
      "name": "crm",
      "type": "api_token",
      "allow_sharing": true,
      "allow_template_sharing": false,
      "description": null,
      "provided_by": "publisher",
      "publisher_credential_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    },
    {
      "name": "company-odoo",
      "type": "odoo",
      "allow_sharing": false,
      "allow_template_sharing": true,
      "description": "Production Odoo instance",
      "provided_by": "template",
      "publisher_credential_id": null,
      "template_data": {
        "url": "https://erp.example.com",
        "database_name": "production"
      },
      "template_private_fields": ["login", "api_token"]
    }
  ],
  "schedules": [
    {
      "name": "Daily data collection",
      "cron_string": "0 6 * * 1-5",
      "description": "Every weekday at 7 AM CET",
      "prompt": "Collect today's market data",
      "schedule_type": "static_prompt",
      "command": null,
      "enabled": true
    }
  ],
  "plugin_specs": [],
  "metadata": {
    "description": "Collects and summarises daily market data.",
    "example_prompts": ["Show me today's summary", "Compare last week vs this week"],
    "status_refresh_command": "/run:status",
    "agent_api_enabled": false,
    "agent_api_identity_enabled": false,
    "a2a_config": null,
    "agent_sdk_config": null,
    "webapp_enabled": false
  },
  "release_notes": "Fixed off-by-one in invoice parser"
}
```

The `metadata` block is top-level and additive (schema_version stays 2). All keys default to `null` when the agent has not configured the corresponding feature. The read side (`manifest_to_revision_fields`) uses `.get()` with `None` defaults on the `metadata` sub-dict, so manifests written before this block was added map all metadata columns to `NULL` — the restore side then skips those fields rather than clobbering the consumer's current value.

**Fields deliberately excluded from the manifest** (never snapshotted):
- `STATUS.md` content — per-install App Data, not bundle content.
- `AgentAccessToken` rows — per-install/per-user JWT secrets.
- `agent_api_token` / `agent_api_access_grant` rows — per-install secrets and per-user ACL.
- Schedule run-state (`last_execution` / `next_execution`) — already excluded; only definitions travel.
- Credential secret values — already excluded; only specs travel.
- Per-install UI preferences (`ui_color_preset`, `conversation_mode_ui`, `show_on_dashboard`) — belong to the installing user, not the definition.

The `content_hash` is SHA-256 over (sorted file paths + content + manifest body excluding `content_hash` itself). With the `workspace/` subtree, relative paths inside the hash simply become `workspace/...` — no special logic is required. This is the same value stored in `AgentBundleRevision.content_hash`.

`schema_version` is a JSON-internal field inside `manifest.json` (and mirrored in the `AgentBundleRevision.manifest` JSON column). There is no Alembic migration for this change — the `snapshot_path` and `content_hash` columns are unchanged; only the value written to the manifest JSON changes from `1` to `2` on new revisions.

The consumer reader dispatches on layout shape: when `snapshot_path/workspace/` is a directory the snapshot is v2; otherwise it is v1. Prefer dispatching on the manifest `schema_version` when the manifest is already loaded.

## Workspace Classification (Single Source of Truth)

`backend/app/services/environments/workspace_classification.py` is the single module that defines what counts as bundle content vs. per-user/runtime data. It replaced four divergent allowlists that previously drifted out of sync.

**Public API:**

| Symbol | Description |
|--------|-------------|
| `BUNDLE_EXCLUDED_TOPLEVEL` | `frozenset` of top-level `app/workspace/` names that are NEVER bundle content: `app-data`, `credentials`, `logs`, `databases`, `uploads`, `__init__.py` |
| `RUNTIME_NAME_DENYLIST` | `frozenset` of name patterns (any depth) that are per-env runtime/dotfile state: `.opencode`, `.cache` |
| `ENV_MIGRATION_EXTRA` | `frozenset` of names added by the `ENV_MIGRATION` profile on top of `BUNDLE_OWNED`: `credentials`, `uploads` |
| `PLUGIN_DERIVED_FILES` | `frozenset` of plugins-root derived filenames: `settings.json`, `manifest.json` — regenerated per consumer, never snapshotted |
| `safe_copytree(src, dst)` | `shutil.copytree` wrapper that never follows or copies symlinks at any depth. All workspace copy operations (publish, seed, apply-update, env migration) use this single primitive to prevent symlink-based denylist bypass and host-file exfiltration. Top-level symlinks are additionally skipped before reaching `safe_copytree` by the `iter_*` helpers |
| `iter_bundle_toplevel(workspace_root)` | Yields bundle-owned top-level entries — skips `BUNDLE_EXCLUDED_TOPLEVEL`, runtime-name-denylisted names, and symlinks |
| `iter_env_migration_toplevel(workspace_root)` | Yields `ENV_MIGRATION`-profile top-level entries — superset of `iter_bundle_toplevel` plus `credentials/` and `uploads/`; still skips `logs/`, `databases/`, `app-data/`, and symlinks |
| `snapshot_layout(snapshot_path)` | Returns `"v2_workspace"` when `snapshot_path/workspace/` exists and is a directory, else `"v1_flat"` |
| `is_bundle_owned_toplevel(name)` | True when `name` is not in `BUNDLE_EXCLUDED_TOPLEVEL` and not runtime-denylisted |
| `is_env_migration_toplevel(name)` | True when `name` passes `is_bundle_owned_toplevel` or is in `ENV_MIGRATION_EXTRA` |

**Two profiles:**

- **`BUNDLE_OWNED`** — used by publish (`_snapshot_workspace_tree`), install-seed (`seed_workspace_from_bundle_snapshot`), and apply-update (`replace_bundle_content`). Everything under `app/workspace/` except `BUNDLE_EXCLUDED_TOPLEVEL` and runtime-name-denylisted entries. `plugins/` keeps special derived-file exclusion and merge-on-seed semantics. `uploads/` is excluded.
- **`ENV_MIGRATION`** — used by `copy_env_to_env` and `copy_workspace_between_environments` (env switch). Superset of `BUNDLE_OWNED` plus `credentials/` and `uploads/`. `logs/`, `databases/`, and `app-data/` are still excluded. Same-user same-agent migration carries credentials and uploads across; this is the correct and intended behaviour for env switch/rebuild.

**v1/v2 dispatch in `workspace_copy.py`:**

`seed_workspace_from_bundle_snapshot` and `replace_bundle_content` call `snapshot_layout(snapshot_path)` to dispatch:
- v2: source is `snapshot_path/workspace/`; every top-level entry is copied/overwritten; `plugins/` is merged via `_seed_plugins_tree`.
- v1 (legacy): the frozen `_V1_FLAT_FOLDERS` / `_V1_FLAT_FILES` tuples (the exact pre-refactor allowlist) are used. v1 snapshots never trigger the apply-update prune pass (overwrite-only legacy semantics preserved).

`replace_bundle_content` additionally runs a prune pass on v2 snapshots: any install-workspace top-level entry that is not in the new snapshot, not in `BUNDLE_EXCLUDED_TOPLEVEL`, not runtime-denylisted, and not `plugins/` is removed. This implements the "snapshot is authoritative for bundle-owned top-level entries" rule (D5). The prune pass is best-effort per entry — a failed removal is logged and does not abort the update.

## Frontend Components

### Catalog
- `CatalogGrid` — renders a responsive grid of `CatalogCard` components; consumes `GET /catalog/` via React Query key `["catalog"]`
- `CatalogCard` — bundle name, description, publisher name + email (falling back to the truncated handle), version badge (`v<latest_version>`, falling back to `rev <n>`). The install-count badge has been removed from the card. "Install" button (or "Open" if already installed)
- `CatalogFilters` — filter by visibility, installed status

### Install Page (Phase 3 — replaces the Install Wizard)
- `InstallPage` — two-column layout (`lg+`: left sticky + right scrollable; `md` and below: stacked). Receives a `CatalogInstallContext` from the route and renders `InstallAgentHeaderCard` + `InstallSetupForm`
- `InstallAgentHeaderCard` — left-column sticky card; displays bundle icon, display name, version badge, publisher info, description, credential mode summary, and Bundle ID
- `InstallSetupForm` — right-column form container; orchestrates `InstallAICredentialSection` and `InstallServiceCredentialItem` list; owns form state (per-spec mode selections), constructs the `InstallRequest` payload, calls `POST /catalog/{bundle_id}/install`, and shows the env-progress display while the environment activates. On success it invalidates the agents/catalog queries and then calls `InstallsService.getSetupStatus({ agentId: install.id })` to decide where to land the user: `status === "ready"` → `navigate({ to: "/", search: { selectAgentId: install.id } })` with a "*<name>* installed — you can chat with it now." toast; anything else (including a failed status call) → `navigate({ to: "/agent/$agentId", hash: "credentials" })` with the legacy "Installed *<name>*" toast so `SetupNeededBanner` can take over
- Dashboard route (`frontend/src/routes/_layout/index.tsx`) — declares `validateSearch` to accept an optional `selectAgentId` string. The initial-selection `useEffect` consumes that param: when the matching agent appears in `agentsWithActiveEnv`, it sets `selectedAgentId`, switches to conversation mode, and then calls `navigate({ to: "/", search: {}, replace: true })` to strip the param so a manual refresh doesn't keep re-overriding the user's later picks. If the agent isn't in the list yet (post-install refetch race) the effect returns and re-runs when the agents query settles.
- `InstallAICredentialSection` — renders "Provided by publisher" info (with name+type summary) or the AI credential pickers when the user provides AI; refactored from the former `WizardStepAICredentials` logic
- `InstallServiceCredentialItem` — one shadcn/ui `Accordion` item per service credential spec. PBP items are collapsed by default. PBU and PBT items with an auto-prefill suggestion are collapsed with the suggestion pre-selected; items without a match are expanded and default to "skip — set up later" (PBU) or "create from publisher template" (PBT). The PBU and PBT bodies share a single `UserOrTemplateChoicesBody` picker: a radio group with the suggestion option (when present), the spec-default fallback (placeholder vs template), and a "pick another credential" option that opens a dropdown of every credential of the matching `type` the installer owns. Sends `mode="use_existing"` + `credential_id` when the suggestion or a user pick is accepted, `mode="skip"` for the fallback path (which routes to placeholder for PBU and template materialisation for PBT in `InstallService._setup_install_credentials`)
- `useInstallContext` — React Query hook; key `["catalog", bundleId, "install-context"]`; calls `GET /catalog/{bundle_id}/install-context`

### Agent Bundle Tab
- `AgentBundleTab` — rendered on agent detail page for `agent-developer` users. Mounts `CredentialProvisioningSection` and `BundlePermissionsCard` when `agent.is_publisher_install === true` (the latter additionally requires `isPublished && agent.bundle_uuid`). Queries `GET /agents/{id}/bundle-credential-drift` (key `["bundle-credential-drift", agentId]`) when `agent.is_publisher_install` is true. Two-card grid layout:
  - **Left — Bundle settings**: catalog-only settings, all post-publish (Visibility, Listed-in-catalog `Switch`, Default install update mode). When `visibility === "users"` the card shows a one-line hint pointing at the Permissions management card below instead of an inline allowlist picker — the picker itself was removed (subsumed by `BundlePermissionsCard`; see below). Pre-publish the card shows a placeholder pointing the user at the Publish action. The Bundle ID block and its edit modal have been removed — the bundle ID is set inside the publish dialog on the first publish and locked afterwards
  - **Right — Revisions**: header "Publish revision" button; once published, a compact Bundle ID row (label + monospace value + copy button) is shown at the top of the card body. Below it, when `credentialDrift?.stale === true` AND `driftedCredentials.length > 0` (only after the first publish), an **amber warning callout** is shown inside the Revisions card before the revision list. The callout heading is "Republish to apply credential sharing changes" and the body is a bullet list — one line per drifted credential: "Credentials `<name>` sharing differs from the latest published bundle (bundle: `<snapshot_label>`)". Labels come from `providedByLabel`. This warning is separate from the per-row hint in `CredentialProvisioningSection`; it gives an at-a-glance reminder even when the provisioning section is scrolled off screen. Below the optional drift warning: the latest 10 revisions list. Each revision renders as `v<version>` when present (with `(rev <n>)` in muted text) or `rev <n>` for legacy rows. Rows include `current` / `installed` badges, install count (publisher's own working install excluded from this count), release notes, copy-content-hash button (with hash preview tooltip), and a delete button. Delete is disabled (with tooltip) whenever the displayed install count is greater than `0`; clicking opens an `AlertDialog`
  - **Publish dialog** — owns four fields (three after the first publish): `Bundle ID` (only rendered on first publish; prefilled with `agent.bundle_id`, sent as `PublishRequest.bundle_id`), `Publish in Public Catalog` (`Switch`, only rendered on first publish; bound to local `publishToPublicCatalog` state, reset to `false` on dialog open), `Version` (always; defaults to `"1.0"` on first publish, otherwise `suggestNextVersion(previousRevision.version)` — increments the trailing numeric component), and optional release notes. Inline `Alert` shows publish errors. Submit posts to `POST /agents/{id}/publish`; the `publishMutation.onSuccess` handler is `async` and chains a `BundlesService.updateBundle({ bundleUuid: rev.bundle_id, requestBody: { visibility: "public", is_listed: true } })` call when the switch was on and the publish was the first one. The chained call uses `rev.bundle_id` from the publish response rather than `agent.bundle_uuid` (which is still stale at that moment), wraps the call in try/catch, and surfaces a "Published, but the bundle is still private — toggle visibility in Bundle settings." error toast on failure. Invalidates `["agent", id]`, `["bundles"]`, and `["catalog"]` after either outcome
- `UserAllowlistPicker` (`frontend/src/components/Common/UserAllowlistPicker.tsx`) — shared search-and-pill picker for selecting users by email/name. Used for bundle access grants, App MCP / identity assignments (`McpConnectorsCard`, `IdentityServerCard`), and credential sharing (`CredentialSharing`). Caller passes a list of `{id, userId, fallbackLabel?}` items and `onAdd`/`onRemove` callbacks; the component owns the search input, results dropdown, and pill rendering. It searches **server-side** via `UsersService.searchUsers` (`GET /users/search`) under React Query key `["user-search", q]` — available to any authenticated user (so non-admin owners work), not the admin-only `["users-list"]`/`GET /users/`. Pill labels come from `fallbackLabel` (the component no longer loads the full user list), so grants pass `g.user_email`. See [User Selector Pattern](../../development/frontend/user_selector_pattern.md)

### Credential Provisioning (Phase 5)
- `frontend/src/components/Credentials/providedByLabel.ts` — shared helper that maps a `ProvidedBy` value (`"user"` | `"publisher"` | `"template"`) to a human-readable label (`"user-provided"` | `"embedded (shared)"` | `"template"`). Used by both `CredentialProvisioningSection` (per-row republish hint) and `AgentBundleTab` (Revisions drift warning) so the two surfaces always use identical wording.
- `CredentialProvisioningSection` (`frontend/src/components/Agents/CredentialProvisioningSection.tsx`) — publisher-only card rendered inside `AgentBundleTab` when `agent.is_publisher_install === true`, wrapped in a `grid grid-cols-1 md:grid-cols-2 gap-6` so the card occupies half the row width on `md+` (mirrors the Bundle settings layout). Each row uses the bundle-settings pattern: descriptive cluster on the left, `w-[260px] shrink-0` Select on the right. Contains two subsections:
  1. **Service credentials** — one row per linked `Credential`. The left cluster leads with a `<CredentialTypeBadge>` (the prominent type chip pulled from `credentialTypes.ts`) and below it a "detected from \<credential name\>" caption, where the name is an outline `<Badge asChild>` linking to `/credential/$credentialId`. When the linked credential has both `allow_sharing=false` and `allow_template_sharing=false`, an amber warning with an `AlertTriangle` icon explains the row is locked to "User provides". **Drift hint:** when the drift query (see `["bundle-credential-drift", agentId]` below) returns a drifted row for this credential, an amber inline note below the caption reads: "Installers still receive the previously published setting (`<snapshot_label>`). Republish the bundle to apply `<live_label>`." using `providedByLabel` for both labels. The right Select offers `User provides` / `Embedded (shared)` / `Template (defaults + private)` — `Embedded (shared)` is disabled when `allow_sharing=false`; `Template (defaults + private)` is disabled when `allow_template_sharing=false` AND is not offered at all for `agent_api` credentials (template sharing is meaningless for connection credentials with no user-fillable private fields — the `SelectItem` for `template` is conditionally rendered only when `cred.type !== "agent_api"`). The Select **auto-saves on change** — there is no Save button — by PATCHing the publisher install via `InstallsService.updatePublishSettings` (i.e. `PATCH /agents/{id}/publish-settings`) with the merged override map and showing a "Credential override saved" success toast on settle. The mutation invalidates `["agent", agentId]` + `["bundles"]` + `["bundle-credential-drift", agentId]`.
  2. **AI credentials** — header + helper paragraph, then one row per mode. Each row's left cluster shows the mode icon (`MessageCircle` blue for Conversation, `Hammer` orange for Building, matching `Chat/ModeSwitchToggle.tsx`), the label `Conversation AI` / `Building AI`, and a "SDK in use: \<engine label\>" caption derived from `EnvironmentsService.getEnvironment(agent.active_environment_id)` via `extractEngine(env.agent_sdk_conversation | agent_sdk_building)` and `getEngineLabel()` (re-exported from `EnvironmentConfigForm`). The right Select **filters AI credential options by the env's strict per-mode SDK provider** — `sdkExpectedCredentialType(env.agent_sdk_conversation/building)` maps the full SDK id (e.g. `opencode/anthropic`) to its single accepted credential type (`anthropic`) and the dropdown only shows credentials whose `type` equals it. Falls back to `SDK_CREDENTIAL_COMPATIBILITY[engine]` only for SDK strings not in the strict map (forward-compat). The dropdown's `__none__` sentinel maps to "None — user provides" and clears the value. **Persistence depends on whether the bundle row exists**: post-publish (`bundle` defined) the selection writes the UUID via `BundlesService.updateBundle` into `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id`. Pre-publish (no bundle yet) the selection writes to a draft on the install via `InstallsService.updatePublishSettings({ ai_credentials: { conversation_credential_id, building_credential_id } })` — `PublishService._apply_pre_publish_ai_drafts` transfers the draft onto the bundle FK columns at first publish. Backend rejects any mismatched selection that bypasses the filter (e.g. via direct API call) with HTTP 400 at both PATCH paths and at publish. Auto-saves on change with a "AI credential saved" / "AI credential saved (will apply on first publish)" success toast (the legacy `Switch` and Save UI were removed). The pre-publish helper paragraph appends "Selections are saved as a draft and applied to the bundle on first publish.".

### Permissions Management
- `BundlePermissionsCard` (`frontend/src/components/Agents/BundlePermissionsCard.tsx`) — publisher-only card rendered inside `AgentBundleTab` below `CredentialProvisioningSection`, mounted only when `agent.is_publisher_install && isPublished && agent.bundle_uuid`. Wrapped in its own `grid grid-cols-1 md:grid-cols-2 gap-6` container (mirrors the `Bundle settings` / `Revisions` / `CredentialProvisioningSection` grids elsewhere on the tab), so the card renders at half-page width on `md+` rather than as a lone full-width block. Queries `["bundlePermissionsOverview", agent.id]` → `InstallsService.getBundlePermissionsOverview({ agentId })`, enabled when `agent.is_publisher_install && !!agent.bundle_uuid`. Renders nothing when `overview.show_card === false`.
  - **Row list** — otherwise renders one compact row per `overview.users[]` entry (`flex items-center justify-between gap-3 px-3 py-2 border rounded-lg`, mirroring the Revisions card's row shape) instead of a `<Table>`. Left: the user's name (falling back to email, then `user_id`) with an email caption shown only when both `full_name` and `email` are present. Right: one `ProducerScopeInline` chip cluster per `overview.producers[]` entry where `can_manage === true` (joins `producer.grants` by `user_id`; shows "+ assign scopes" when the user has no grant on that producer, an italic "no scopes" when the grant has an empty scope list, or the scope `Badge`s otherwise; clicking anywhere in the cluster opens `BundlePermissionsAddUserModal` in edit mode for that user), followed by an always-present **Edit** icon button (`Pencil`, opens the edit modal regardless of whether the user has any scopes — keeps editing discoverable for a zero-scope user) and a **Remove** icon button (`Trash2`).
  - **Non-manageable producers** — `overview.producers[]` entries with `can_manage === false` are never rendered as a per-user column or chip; they are listed once in the card header inside a dashed-border block, one line per producer with a `Lock` icon: "Also connected: `<producer_agent_name>` — managed by `<owner_email>` on its own page". The backend already returns empty `grants`/`scope_catalog` for these entries (see `BundlePermissionsService.build_overview` above); the frontend additionally filters on `producer.can_manage` before rendering, so a non-manageable producer's per-user data is never surfaced by either layer alone.
  - **Add user** button in the card header (hidden when `canAddAnything` — `bundleAccessApplicable || manageableProducers.length > 0` — is false) opens `BundlePermissionsAddUserModal` in add mode.
  - **Cascading remove** — clicking a row's Remove button stages that `BundlePermissionUser` as `removeTarget` and opens an `AlertDialog` (mirroring the "Delete revision" confirmation). On confirm, `removeUserMutation` builds one task per authority domain the user actually holds — `BundlesService.revokeGrant` when `user.bundle_grant_id` is set, plus `AgentApiService.deleteAgentApiGrant` for every manageable producer where a grant for that `user_id` is found — and runs them via `Promise.allSettled`. Fulfilled producer tasks are collected into `touchedProducerIds` for cache invalidation; any rejected task is collected into a `failures` list and surfaced as a single "Removed with issues — could not remove: `<labels>`" toast, otherwise "User access removed". On settle (success or partial failure) it invalidates `["bundlePermissionsOverview", agent.id]`, `["bundles", bundleUuid, "grants"]`, and `["agentApiGrants", producerId]` for each touched producer, then clears `removeTarget`.
- `BundlePermissionsAddUserModal` (`frontend/src/components/Agents/BundlePermissionsAddUserModal.tsx`) — single modal used both for "Add user" (fresh `UserAllowlistPicker` selection, `excludeUserIds` = users already in the list) and "Edit permissions" (fixed user via the `fixedUser` prop, no picker). **No Bundle access checkbox** — in add mode, when `bundleAccessApplicable && bundleUuid` and an email is available, `buildActions()` unconditionally stages a `BundlesService.addGrant` action (no opt-out); in edit mode, bundle access is never touched — `buildActions()` skips the bundle section entirely (`!isEdit` guard), since bundle-access revocation now happens only via the card's row-level Remove action, not per-field toggling in this modal. One `ProducerScopeBlock` per `producers.filter(p => p.can_manage)` reuses the catalog quick-add-chip + free-text-add + removable-assigned-chip pattern lifted from `AgentApiAccessScopesCard.tsx`; scopes are seeded from the existing grant on edit-open (`existingGrantFor`), empty on add. `buildActions()` diffs the form state against the existing grant state and emits one `PlannedAction` per section that actually changed (bundle grant add via `BundlesService.addGrant`; producer scope create/update via `AgentApiService.createAgentApiGrant`/`updateAgentApiGrant` depending on whether a grant already exists for that user+producer). Submit (`canSubmit` requires `(isEdit || selectedUser)` AND at least one planned action) runs the planned actions sequentially inside `handleSubmit`, accumulates per-action `{key, error}` failures without aborting the remaining actions, invalidates the overview query plus any touched domain query (`["bundles", bundleUuid, "grants"]`, `["agentApiGrants", producerAgentId]`), and shows a single summary toast ("User added" / "Permissions updated" on full success, "Saved with N issue(s) — see details" otherwise); failed sections render inline error text and the dialog stays open so the user can retry just that section.

### Setup Banner
- `SetupNeededBanner` — shown on the install/agent detail page (`frontend/src/routes/_layout/agent/$agentId.tsx`) above the tabs when `status !== "ready"`. Queries `["agent", agentId, "setup-status"]`. Renders an amber `Alert` for `needs_setup` (copy: "Open the Credentials tab below and fill in…") or a destructive `Alert` for `publisher_broken`. No action button — both variants direct the user to the Credentials tab on the same page. Subscribes to all three Phase 4 WS events via `useMultiEventSubscription` and invalidates the setup-status query on receipt so the banner appears/disappears in real time without a page reload.

The dedicated setup-credentials route (`frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx`) has been deleted. Users now fix credentials directly from the agent Credentials tab via the standard credential detail page (`/credential/$credentialId`). The three backend endpoints remain for cinna CLI use:
- `GET /agents/{id}/setup-status`
- `GET /agents/{id}/setup-credentials`
- `PUT /agents/{id}/setup-credentials/{credential_id}`

### Update Banner
- `UpdateAvailableBanner` — shown on install detail when `pending_update=true`; displays revision delta and release notes; "Apply now" button calls `POST /agents/{id}/apply-update`

## WebSocket Events

| Event | Direction | Payload | Notes |
|-------|-----------|---------|-------|
| `BUNDLE_PUBLISHED` | server → publisher | `{bundle_id, bundle_uuid, revision_number, revision_id}` | Refreshes `["bundles"]` query |
| `INSTALL_UPDATE_AVAILABLE` | server → install owner | `{agent_id, bundle_id, revision_number, release_notes, update_mode}` | Shows UpdateAvailableBanner |
| `INSTALL_UPDATE_APPLIED` | server → install owner | `{agent_id, bundle_id, revision_number}` | Clears banner, refreshes env |
| `INSTALL_UPDATE_FAILED` | server → install owner | `{agent_id, bundle_id, error}` | Shows error state on banner |
| `INSTALL_SETUP_REQUIRED` | server → install owner | `model_id = install id; meta: {agent_id, setup_url, status, missing_count}` | Emitted by any channel when the gate blocks. `SetupNeededBanner` invalidates `["agent", id, "setup-status"]` query on receipt (Phase 4) |
| `INSTALL_SETUP_COMPLETED` | server → install owner | `model_id = install id; meta: {agent_id, credential_id}` | Emitted by `PUT /agents/{id}/setup-credentials/{cred_id}` when the gate passes after the save. `SetupNeededBanner` invalidates setup-status query; the banner hides without a page refresh (Phase 4) |
| `PUBLISHER_CREDENTIAL_BROKEN` | server → install owner | `model_id = install id; meta: {agent_id, setup_url, missing_count}` | Emitted alongside `INSTALL_SETUP_REQUIRED` when the webhook gate detects `publisher_broken` status. `SetupNeededBanner` also handles this event (Phase 4) |

## React Query Keys

| Key | Source |
|-----|--------|
| `["bundles"]` | `GET /bundles/` |
| `["bundles", bundleUuid]` | `GET /bundles/{uuid}` |
| `["bundles", bundleUuid, "revisions"]` | `GET /bundles/{uuid}/revisions` |
| `["bundles", bundleUuid, "grants"]` | `GET /bundles/{uuid}/grants` |
| `["catalog"]` | `GET /catalog/` |
| `["catalog", bundleId]` | `GET /catalog/{bundle_id}` |
| `["catalog", bundleId, "install-context"]` | `GET /catalog/{bundle_id}/install-context` — fetched by `useInstallContext` hook |
| `["agent", agentId, "setup-status"]` | `GET /agents/{agent_id}/setup-status` — fetched by `SetupNeededBanner`; invalidated on `INSTALL_SETUP_REQUIRED`, `INSTALL_SETUP_COMPLETED`, `PUBLISHER_CREDENTIAL_BROKEN` WS events. Also consumed by the cinna CLI via the backend endpoint |
| `["agent", agentId, "setup-credentials"]` | `GET /agents/{agent_id}/setup-credentials` — backend endpoint used by the cinna CLI; no longer fetched by the web UI since the setup-credentials page was deleted |
| `["bundle-credential-drift", agentId]` | `GET /agents/{agent_id}/bundle-credential-drift` — fetched by both `AgentBundleTab` (Revisions drift warning) and `CredentialProvisioningSection` (per-row republish hint) when `agent.is_publisher_install === true`. Invalidated after the provisioning section saves a `provided_by` override change. Returns `BundleCredentialDrift`. |
| `["user-search", q]` | `GET /users/search` (server-side search used by the shared `UserAllowlistPicker` for the grants picker; min 2 chars, cached 30s) |
| `["bundlePermissionsOverview", agentId]` | `GET /agents/{agent_id}/bundle-permissions-overview` — fetched by `BundlePermissionsCard`; invalidated after every bundle-grant or producer-scope mutation made through the card or its Add-user modal |

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `BUNDLE_STORAGE_DIR` | `<DATA_DIR>/bundles/` | Root for all revision snapshots |
| `APP_DATA_STORAGE_DIR` | `<DATA_DIR>/app-data/` | Root for all app-data volumes |
| `HOST_APP_DATA_DIR` | `""` | Set in Docker-in-Docker; see AppDataService path translation |
| `BUNDLE_AUTO_UPDATE_ENABLED` | `True` | Master switch for the periodic bundle auto-update scheduler. When `False`, `start_scheduler()` never registers the job and `sweep_bundle_updates_background` (publish fast path) no-ops |
| `BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES` | `10` | Interval between periodic sweep runs |
| `BUNDLE_AUTO_UPDATE_BATCH_LIMIT` | `50` | Max installs attempted per sweep run (periodic or publish fast path); the remainder is picked up on the next run |
| `BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS` | `6` | How long a failed automatic install is deferred before the sweep retries it |

## Migrations

The full-workspace bundle snapshot change (schema_version 1 → 2) required **no Alembic migration**. `schema_version` is a JSON-internal field inside `manifest.json` and the `AgentBundleRevision.manifest` JSON column; the `snapshot_path` and `content_hash` column types and the overall table schema are unchanged.

| File | Description |
|------|-------------|
| `backend/app/alembic/versions/i7e8f9a0b1c2_add_version_to_agent_bundle_revision.py` | Adds nullable `version varchar(64)` column to `agent_bundle_revision`. `down_revision = h6d7e8f9a0b1`. Existing rows get NULL; the UI falls back to `rev <n>` for those rows |
| `backend/app/alembic/versions/aa1bc2d3e4f5_add_publisher_ai_credentials_to_agent_bundle.py` | Adds `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id` nullable UUID FK columns to `agent_bundle` (FK → `ai_credential.id`, `ON DELETE SET NULL`). `down_revision = i7e8f9a0b1c2`. No data backfill; downgrade drops both columns and their FK constraints |
| `backend/app/alembic/versions/bb2cd3e4f5a6_add_publish_settings_to_agent.py` | Adds `publish_settings` JSON column (default `{}`) to the `agent` table. `down_revision = aa1bc2d3e4f5`. No data backfill; existing rows are read as `{}` naturally. Downgrade drops the column |
| `backend/app/alembic/versions/cc3de4f5a6b7_add_template_sharing_to_credential.py` | Adds `allow_template_sharing` boolean (default `false`) and `template_private_fields` JSON list (default `[]`) to the `credential` table. `down_revision = bb2cd3e4f5a6`. Server defaults make the column add a no-op for existing rows; downgrade drops both columns |
| `backend/app/alembic/versions/dd4ef5a6b7c8_add_router_trigger_prompt_and_auto_managed.py` | Adds `router_trigger_prompt` text NULLABLE to both `agent` and `agent_bundle_revision`; adds `is_auto_managed` boolean (server default `false`) to `app_agent_route`. `down_revision = cc3de4f5a6b7`. No data backfill in this migration — existing rows get NULL / false naturally. A separate Phase 8 backfill script populates `router_trigger_prompt` and creates auto-routes for pre-existing **foreign** bundle installs only (`is_publisher_install=False AND bundle_uuid IS NOT NULL`) |
| `backend/app/alembic/versions/bcab2848714f_add_catalog_type_to_app_data_volume.py` | Adds nullable `catalog_type varchar` column to `app_data_volume`; drops `uq_app_data_user_bundle` on `(user_id, bundle_id)`; adds `uq_app_data_user_bundle_catalog` on `(user_id, bundle_id, catalog_type)`. Backfill: volumes whose paired agent has `is_publisher_install=True` → `NULL`; all other bundle-linked volumes → `"server"`; orphans with no paired agent → `"server"` |
| `backend/app/alembic/versions/ad1f9c2e4b73_scope_agent_bundle_unique_by_install_slot.py` | Replaces `uq_agent_bundle_id_per_publisher` on `(owner_id, bundle_id)` with a new constraint on `(owner_id, bundle_id, is_publisher_install)`. The partial unique index `uq_agent_publisher_install_per_bundle` is unchanged. This allows a publisher to hold both a publisher install and a consumer install of the same bundle simultaneously |
| `backend/app/alembic/versions/cd4ef5a6b7c8_add_schedules_to_agent_bundle_revision.py` | Adds `schedules JSON NOT NULL DEFAULT '[]'` column to `agent_bundle_revision`. Existing revisions are backfilled to `[]` (no schedules); fully backward compatible. Downgrade drops the column |
| `backend/app/alembic/versions/d9b3e1a7c45f_agent_bundle_revision_metadata.py` | Adds 8 nullable agent-row metadata columns to `agent_bundle_revision`: `description` (Text), `example_prompts` (JSON), `status_refresh_command` (String 1024), `agent_api_enabled` (Boolean), `agent_api_identity_enabled` (Boolean), `a2a_config` (JSON), `agent_sdk_config` (JSON), `webapp_enabled` (Boolean). All nullable with no server-side backfill — existing rows read as `NULL`, which the restore side treats as "do not overwrite". `down_revision = c8a4f1e09b27`. Downgrade drops all 8 columns |
| `backend/app/alembic/versions/878bc3f6579f_add_revision_origin.py` | Adds `origin varchar(32) NOT NULL server_default 'publish'` to `agent_bundle_revision`. `down_revision = d9b3e1a7c45f`. All existing rows (including any git baselines created before this migration) are backfilled to `'publish'`; they are indistinguishable from catalog publishes and that is acceptable — the git-versioning feature is new. Downgrade drops the column |
| `backend/app/alembic/versions/04e32c2c255a_add_last_update_attempt_at_to_agent.py` | Adds nullable `last_update_attempt_at timestamp` to `agent`. `down_revision = e4c1b7d92f08`. No backfill; NULL means "never attempted by the auto-update sweep". The autogenerate run also picked up unrelated drift from other in-flight work (`app_agent_route`/`user_app_agent_route` channel columns, session `channel_*` columns, `cli_device_login_request` timezone-awareness) — all removed by hand so this migration adds exactly one column. Downgrade drops the column |

## Runtime Cross-Service Rows Created at Install Time

| Row type | Created by | When |
|----------|-----------|------|
| `CredentialShare` (publisher → installer) | `InstallService._try_link_publisher_credential` | Each PBP service credential spec in the revision |
| `AgentCredentialLink` (install → publisher's Credential) | `InstallService._try_link_publisher_credential` | Same, after `CredentialShare` is ensured |
| `AICredentialShare` (publisher → installer) | `InstallService._link_publisher_ai_credential` | Each non-null `bundle.publisher_ai_credential_*_id`; also re-asserted on every idempotent re-install |
| `Credential` placeholder (is_placeholder=True) | `InstallService._setup_install_credentials` | PBU specs where the installer provides no selection, or when a PBP credential is unavailable (degraded mode) |
| `Credential` template placeholder (is_placeholder=True, encrypted_data seeded from `template_data`, `template_private_fields` mirrored from spec) | `InstallService._materialise_template_credential` | PBT specs (`provided_by="template"`) when the installer has not opted into `mode="use_existing"` |
| `AgentCredentialLink` (install → placeholder) | `InstallService._setup_install_credentials` | After placeholder is created |

All cross-service rows above are created in the existing `credential`, `credential_share`, `ai_credential_share`, and `agent_credential_link` tables. The template-sharing feature only adds two new columns to `credential` (see Migrations).

## Security

- `AgentBundlePublic` omits publisher email and raw UUID; only a truncated handle (`first 8 chars of UUID + "…"`) is exposed
- `CatalogEntryPublic` additionally surfaces `publisher_name`, `publisher_email`, and `latest_version` for the install/catalog UX. Catalog access is auth-gated to users who can already see the bundle row, so exposing the publisher's name + email matches the trust model of an internal-instance catalog. If a future deployment needs anonymised publishers, the resolver in `CatalogService._bundle_to_entry` is the single point to gate
- `required_credential_specs` contains only names and types; no secret values are ever stored
- Bundle deletion is blocked (`409`) until all foreign installs are removed
- Wipe of app-data volume is blocked (`409`) unless `is_orphaned = true`
- Bundle-id edit is blocked (`409`) after first publish to prevent orphaning installed app-data
- `GET /agents/{agent_id}/bundle-permissions-overview` is publisher-install owner-only — 404 (not 403) for non-owners, non-publisher installs, and missing agents, identical to `get_bundle_credential_drift`. Within the response, the owner-gated `AgentApiGrantService.list_grants` / `.get_scope_catalog` reads run **only** inside `BundlePermissionsService.build_overview`'s `if row.can_manage:` branch (`can_manage = is_superuser or producer.owner_id == current_user.id`, computed in `AgentApiTokenService.list_connected_producers`) — for any connected producer the caller does not own, the call is never made, so a bundle publisher can never read or write another owner's `agent_api_access_grant` rows through this surface, even indirectly. All writes the card performs go through the existing, unchanged, already owner-gated bundle-grant and agent-api-grant routes — no new write surface is introduced. The `agent_api` credential's `token` value is never read by `list_connected_producers`; only `producer_agent_id` is decrypted out of the credential data, and it is never serialized into the response
