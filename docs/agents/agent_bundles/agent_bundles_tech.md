# Agent Bundles & Installs — Technical Reference

## File Locations

### Models
- `backend/app/models/bundles/agent_bundle.py` — `AgentBundle`, `AgentBundleBase`, `AgentBundlePublic`, `AgentBundleUpdate`, `AgentBundlesPublic`, `BundleVisibility`, `BundleInstallMode`
- `backend/app/models/bundles/agent_bundle_revision.py` — `AgentBundleRevision`, `AgentBundleRevisionPublic`, `AgentBundleRevisionsPublic`, `PublishRequest`
- `backend/app/models/bundles/bundle_access_grant.py` — `BundleAccessGrant`, `BundleAccessGrantPublic`, `BundleAccessGrantCreate`, `BundleAccessGrantsPublic`
- `backend/app/models/bundles/catalog.py` — `CatalogEntryPublic`, `CatalogPublic`, `InstallRequest`, `AdminInstallRequest`, `AICredentialSelections` (gains `use_publisher_ai: bool = False` in Phase 3 — UI hint only, backend ignores it), `SetUpdateModeRequest`, `EditBundleIdRequest`, `CheckUpdatesResponse`, `InstallCredentialSelection` (Phase 3 — `mode` literal + optional `credential_id`), `InstallContextSpec` (Phase 3), `CatalogInstallContext` (Phase 3)
- `backend/app/models/agents/agent.py` — `Agent` (the Install table): `bundle_id`, `bundle_uuid`, `installed_revision_id`, `is_publisher_install`, `update_mode`, `pending_update`, `pending_update_at`, `last_sync_at`, `last_update_status`

### Services
- `backend/app/services/bundles/bundle_id_service.py` — `BundleIdService`
- `backend/app/services/bundles/bundle_service.py` — `BundleService`
- `backend/app/services/bundles/exceptions.py` — `BundleError` hierarchy (`BundleNotFoundError`, `BundleAccessDeniedError`, `BundleConflictError`, `BundleValidationError`, `RevisionNotFoundError`, `RevisionInUseError`, `GrantNotFoundError`); each subclass carries an `http_status` attribute used by the route layer
- `backend/app/services/bundles/publish_service.py` — `PublishService`
- `backend/app/services/bundles/install_service.py` — `InstallService`, `InstallError`
- `backend/app/services/bundles/install_readiness_gate.py` — `InstallReadinessGate`, `GateResult`, `GateMissingItem` (Phase 4)
- `backend/app/services/bundles/catalog_service.py` — `CatalogService`
- `backend/app/services/bundles/app_data_service.py` — `AppDataService`
- `backend/app/services/bundles/app_data_orphan_scheduler.py` — daily orphan reporter

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
- `frontend/src/components/Catalog/CatalogCard.tsx` — single catalog entry card
- `frontend/src/components/Catalog/CatalogFilters.tsx` — filter controls
- `frontend/src/components/Install/InstallPage.tsx` — two-column install page container (left sticky agent header, right scrollable setup form, single Install button at bottom)
- `frontend/src/components/Install/InstallAgentHeaderCard.tsx` — left-column sticky card showing bundle icon, name, version, publisher, description, credential summary, Bundle ID
- `frontend/src/components/Install/InstallSetupForm.tsx` — right-column form; orchestrates AI section + service section + Install button; owns form state and submit logic
- `frontend/src/components/Install/InstallAICredentialSection.tsx` — renders publisher-provides info state OR the AI credential picker; replaces `WizardStepAICredentials` logic
- `frontend/src/components/Install/InstallServiceCredentialItem.tsx` — one shadcn/ui `Accordion` item per service credential spec; handles auto-prefill suggestion display and mode selection (`use_existing` / `skip` / pick-another)
- `frontend/src/components/Install/useInstallContext.ts` — React Query hook on `["catalog", bundleId, "install-context"]` fetching `GET /catalog/{bundle_id}/install-context`
- `frontend/src/components/Agents/AgentBundleTab.tsx` — bundle management tab on agent detail page
- `frontend/src/components/Agents/CredentialProvisioningSection.tsx` — publisher-only half-width card on the bundle tab; only rendered when `agent.is_publisher_install === true`; lets the publisher set per-credential `provided_by` overrides (auto-save on change) and pick publisher AI credentials per mode with SDK-aware filtering (Phase 5)
- `frontend/src/components/Credentials/credentialTypes.ts` — shared credential-type metadata registry (`CREDENTIAL_TYPE_GROUPS` array + `getCredentialTypeMeta(type)` helper); single source of truth for the icon, label, and per-group badge palette of every `CredentialType`. Consumed by both the Add Credential picker and the display-only `<CredentialTypeBadge>`
- `frontend/src/components/Credentials/CredentialTypeBadge.tsx` — display-only `<span>` chip rendering the icon + label + palette for a credential type; reused on the publisher's credential provisioning panel (and any future surface that needs to surface a credential's type at a glance)
- `frontend/src/components/Agents/UpdateAvailableBanner.tsx` — pending update notification
- `frontend/src/components/Install/SetupNeededBanner.tsx` — banner on the agent detail page when the gate would block; queries `["agent", agentId, "setup-status"]`; renders amber alert (`needs_setup`) or destructive alert (`publisher_broken`); subscribes to all three Phase 4 WS events; absent when `status === "ready"` (Phase 4)
- `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` — focused full-width form listing incomplete user-owned placeholder credentials; one card per credential with name, type badge, description, and generic key/value editor; save calls `InstallsService.updateSetupCredential`; shows a "Setup complete" card when `status === "ready"`; includes a `// TODO: per-type credential forms` for a follow-up wiring `CredentialForms` components (Phase 4)

Deleted in Phase 3 (replaced by the single-page install):
- `InstallWizard.tsx`, `WizardStepOverview.tsx`, `WizardStepCredentials.tsx`, `WizardStepAICredentials.tsx`, `WizardStepConfirm.tsx`

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
| `agent_sdk_building` | varchar(128) | SDK selection snapshot |
| `agent_sdk_conversation` | varchar(128) | SDK selection snapshot |
| `model_override_building` | varchar(128) | |
| `model_override_conversation` | varchar(128) | |
| `required_credential_specs` | JSON | `[{name, type, allow_sharing, description, provided_by, publisher_credential_id}]`. `provided_by` is `"user"` or `"publisher"`; `publisher_credential_id` is a UUID string or null. Revisions written before Phase 1 lack these two fields — readers default them to `"user"` / `null` respectively |
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
| `publish_settings` | JSON DEFAULT `{}` | Publisher-only override map. Meaningful only on `is_publisher_install=True` rows. Shape: `{"credential_overrides": {"<spec_name>": {"provided_by": "user" \| "publisher"}}}`. Added in Phase 5 migration `bb2cd3e4f5a6` |

Dropped columns (Phase 2 migration): `is_clone`, `parent_agent_id`, `clone_mode` and their indexes.

## API Endpoints

### Bundle Management (`/api/v1/bundles`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/bundles/` | `require_developer` | List bundles owned by current user |
| `GET` | `/bundles/{bundle_uuid}` | CurrentUser | Detail; visibility-checked for non-publishers |
| `PATCH` | `/bundles/{bundle_uuid}` | `require_developer` + owner | Update display_name, visibility, is_listed, default_install_mode. Also accepts `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id` (Phase 1): non-null values are validated as AI credentials owned by the bundle publisher (400 on failure); explicit `null` clears the field |
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
| `GET` | `/catalog/{bundle_id}/install-context` | CurrentUser | NEW (Phase 3). Returns `CatalogInstallContext` containing the `CatalogEntryPublic`, `ai_provided_by_publisher` flag, publisher AI credential name+type summaries (never secrets), and per-spec `InstallContextSpec` list each carrying `suggested_credential_id`/`suggested_credential_name` from the auto-prefill matcher. 404 when bundle is not visible to the caller |
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
| `GET` | `/agents/{agent_id}/setup-credentials` | owner | Returns `list[SetupCredentialSummary(id, name, type, description)]` of incomplete user-owned placeholder credentials linked to this install. Excludes publisher-shared rows (Phase 4) |
| `PUT` | `/agents/{agent_id}/setup-credentials/{credential_id}` | owner | Body: `CredentialUpdate`. Validates credential is owned by the install owner and linked to this install via `AgentCredentialLink`. Calls `CredentialsService.update_credential` (which flips `is_placeholder=False` when data is non-empty), re-runs gate, emits `INSTALL_SETUP_COMPLETED` if newly ready. Returns `CredentialPublic`. 409 if credential is already non-placeholder (Phase 4) |
| `PATCH` | `/agents/{agent_id}/publish-settings` | `require_developer` + owner | Body: `PublishSettingsUpdate{credential_overrides: {<spec_name>: {provided_by: "user"\|"publisher"}}}`. Requires `is_publisher_install=True` (400 otherwise). Validates each override key is the name of a credential currently linked to the install; validates each `provided_by` is `"user"` or `"publisher"`. Replaces the override map wholesale — omitting a key returns that spec to inference. Returns `AgentPublic`. (Phase 5) |

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
| `update_bundle(session, bundle, data: AgentBundleUpdate)` | Validates visibility/install_mode values; also validates that any non-null `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id` references an `AICredential` owned by `bundle.publisher_user_id` (raises `BundleValidationError` 400 otherwise). Explicit `null` clears the publisher-provides state for that mode. |
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
| `_collect_credential_specs(session, install)` | Reads linked `AgentCredentialLink` rows and emits the evolved per-spec shape: `{name, type, allow_sharing, description, provided_by, publisher_credential_id}`. Resolution order per spec: (1) consult `install.publish_settings["credential_overrides"][cred.name]["provided_by"]` if an override entry exists; (2) infer from `Credential.allow_sharing` — `True` → `"publisher"` + `publisher_credential_id=cred.id`; otherwise `"user"` + `null` |
| `_validate_publisher_provides(session, install)` | Called from `_publish_locked` before `_collect_credential_specs`. Asserts every spec that would be emitted as `provided_by="publisher"` — whether derived from a publisher override or inferred from `allow_sharing` — is backed by a `Credential` with `allow_sharing=True`. This is the security enforcement point: an override to `"publisher"` on a non-shareable credential fails publish with a descriptive error |

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
| `install_bundle(session, user, bundle, request)` | Idempotent; returns existing install if present. Calls `_link_publisher_ai_credential` BEFORE the idempotent early-return so re-installs self-heal a deleted `AICredentialShare` |
| `install_bundle_for_email(session, publisher_agent_id, recipient_user_id)` | Auto-publishes on first call if bundle has no revisions |
| `admin_install(session, target_user, bundle, request)` | Thin wrapper over install_bundle |
| `apply_update(session, install)` | Stops env, calls `replace_bundle_content`, updates prompts + bookkeeping fields, emits event |
| `uninstall(session, install)` | Delegates to `AgentService.delete_agent` which handles orphaning |
| `set_update_mode(session, install, mode)` | |
| `check_for_updates(session, install)` | Returns `{pending_update, installed_revision_number, latest_revision_number, last_update_status, last_sync_at, update_mode}` |
| `edit_bundle_id(session, install, new_bundle_id)` | Raises 409 if already published; validates format and uniqueness |
| `_normalise_credentials_payload(credentials_raw, revision_specs)` | Phase 3 addition; legacy shim dropped in Phase 5. Now only the typed `dict[str, InstallCredentialSelection]` shape is accepted. Each value must be a dict (or `InstallCredentialSelection` instance) with a `mode` key in `{"use_existing", "placeholder", "publisher_provides", "skip"}`; a bare `str` or any other type returns HTTP 422. Unknown mode values are coerced to `"placeholder"` to avoid aborting installs from misconfigured clients |
| `_setup_install_credentials(session, install, revision, user_provided_data)` | Branches on each spec's `provided_by` field. `"publisher"` → calls `_try_link_publisher_credential`; on failure falls through to placeholder and marks install degraded. `"user"` (or missing field — backward compat) → consumes the normalised `InstallCredentialSelection` dict from `_normalise_credentials_payload`; `mode="use_existing"` for a publisher spec raises HTTP 422; omitted spec key treated as `mode="placeholder"` |
| `_try_link_publisher_credential(session, install, publisher_credential_id_raw, spec_name)` | Validates the publisher's `Credential` row (exists, `allow_sharing=True`, owned by the bundle publisher), ensures a `CredentialShare` (publisher → installer) exists, inserts the `AgentCredentialLink`. Returns `True` on success, `False` on any validation failure |
| `_link_publisher_ai_credential(session, user, bundle)` | For each non-null `bundle.publisher_ai_credential_*_id`, idempotently creates an `AICredentialShare` (publisher → installer) via `ai_credentials_service.share_credential`. Skips share-with-self when the installer is the publisher. Failures are logged as warnings and do not abort the install |

Install flow (`_install_from_revision`):
1. `Agent` row created from revision prompts + SDK settings
2. Name uniqueness enforced (appends "(2)", "(3)" etc.)
3. AI credential resolution — before env creation, the resolution chain is applied for each mode: (a) `bundle.publisher_ai_credential_*_id` if non-null; (b) the installer's `request.ai_credential_selections` value; (c) `None`. `_link_publisher_ai_credential` is called first so the `AICredentialShare` row exists at env-create time when the bundle provides an AI credential
4. `AgentEnvironment` created via `EnvironmentService.create_environment` with the resolved credential ids. The env service uses `ai_credentials_service.can_access_credential(user, cred)` (owner OR share recipient) rather than a strict `owner_id == user.id` check, so shared publisher AI credentials pass through at this step
5. Workspace seeded from `revision.snapshot_path` via `seed_workspace_from_bundle_snapshot`
6. `AppDataService.get_or_create_volume` called; existing orphaned volume reattached
7. `_setup_install_credentials` called — branches on `provided_by` per spec; PBP specs link the publisher's row; PBU specs create placeholders or link installer selections

### `CatalogService`

| Method | Notes |
|--------|-------|
| `list_for_user(session, user)` | Union of public listed, grant-visible, and publisher-own bundles |
| `get_for_user(session, bundle_id, user)` | Single entry; returns None if not visible |
| `user_can_see(session, bundle, user)` | Visibility check logic |
| `user_can_install(session, bundle, user)` | `user_can_see` AND `latest_revision_id IS NOT NULL` |
| `_bundle_to_entry(session, bundle, user)` | Resolves a `CatalogEntryPublic` from a bundle row: reads latest revision for `latest_version` / `latest_revision_number`; reads publisher `User` row for `publisher_name` and `publisher_email`; checks the calling user's install row for `is_installed` / `user_install_id` |
| `build_install_context(session, bundle, user) -> CatalogInstallContext` | NEW (Phase 3). Runs the auto-prefill matcher per spec (see below), resolves publisher AI credential name+type summaries (no secret values), and returns `CatalogInstallContext`. Called by `GET /catalog/{bundle_id}/install-context` |

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
| `_format_user_message(missing, setup_url, status) -> str` | Renders the markdown used in `user_message`. Used by all channels (chat, MCP, A2A). Plain text plus one inline markdown link pointing at `setup_url`, chosen to render usefully in MCP non-rich output and A2A SSE streams |
| `_build_setup_url(install_id) -> str` | `f"{FRONTEND_HOST}/agent/{install_id}/setup-credentials"` — single source of truth for the setup URL |
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
| `find_match_for_spec(session, user_id, name, type) -> Credential \| None` | NEW (Phase 3). Case-insensitive `(name, type)` match across the user's owned `Credential` rows and credentials shared with them via `CredentialShare`. Preference order: owned before shared, then most-recent within each group. Used by `CatalogService.build_install_context` to populate `suggested_credential_id` per spec. Never returns a credential whose `owner_id` is someone other than the user unless that credential has a `CredentialShare` row for the user |
| `update_credential` | Flips `is_placeholder=False` when the saved `credential_data` is non-empty (Phase 4 addition). Covers the setup page commit path so filling a placeholder automatically promotes it to a real credential |

### Auto-prefill Matching

When the install-context endpoint is called, `CatalogService.build_install_context` runs `CredentialsService.find_match_for_spec` once per PBU spec in the latest revision. The matcher applies the following precedence:

1. Owned credentials (`credential.owner_id == user.id`) matching `(name, type)` case-insensitively — most recent first
2. Credentials shared with the user (via `CredentialShare`) matching the same `(name, type)` — most recent first

The response carries only `suggested_credential_id` and `suggested_credential_name` (never the credential's secret data). The frontend shows the suggestion as a pre-selected option; the user must explicitly confirm or override it. Accepted suggestions are submitted as `mode="use_existing"` with the matched UUID.

The install-context response never carries credential secrets — only `(name, type)` summary strings are returned for publisher AI credential fields (`ai_publisher_credential_summaries`).

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
    {
      "name": "gmail",
      "type": "imap",
      "allow_sharing": false,
      "description": null,
      "provided_by": "user",
      "publisher_credential_id": null
    },
    {
      "name": "crm",
      "type": "api_token",
      "allow_sharing": true,
      "description": null,
      "provided_by": "publisher",
      "publisher_credential_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    }
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

### Install Page (Phase 3 — replaces the Install Wizard)
- `InstallPage` — two-column layout (`lg+`: left sticky + right scrollable; `md` and below: stacked). Receives a `CatalogInstallContext` from the route and renders `InstallAgentHeaderCard` + `InstallSetupForm`
- `InstallAgentHeaderCard` — left-column sticky card; displays bundle icon, display name, version badge, publisher info, description, credential mode summary, and Bundle ID
- `InstallSetupForm` — right-column form container; orchestrates `InstallAICredentialSection` and `InstallServiceCredentialItem` list; owns form state (per-spec mode selections), constructs the `InstallRequest` payload, calls `POST /catalog/{bundle_id}/install`, and shows the env-progress display while the environment activates before redirecting to the install detail page
- `InstallAICredentialSection` — renders "Provided by publisher" info (with name+type summary) or the AI credential pickers when the user provides AI; refactored from the former `WizardStepAICredentials` logic
- `InstallServiceCredentialItem` — one shadcn/ui `Accordion` item per service credential spec. PBP items are collapsed by default. PBU items with an auto-prefill suggestion are collapsed with the suggestion pre-selected; PBU items without a match are expanded and default to "skip — set up later". Sends `mode="use_existing"` + `credential_id` when the suggestion or a user pick is accepted, `mode="placeholder"` or `mode="skip"` otherwise
- `useInstallContext` — React Query hook; key `["catalog", bundleId, "install-context"]`; calls `GET /catalog/{bundle_id}/install-context`

### Agent Bundle Tab
- `AgentBundleTab` — rendered on agent detail page for `agent-developer` users. Mounts `CredentialProvisioningSection` when `agent.is_publisher_install === true`. Two-card grid layout:
  - **Left — Bundle settings**: catalog-only settings, all post-publish (Visibility, the user-allowlist picker when `visibility = "users"`, Listed-in-catalog `Switch`, Default install update mode). Pre-publish the card shows a placeholder pointing the user at the Publish action. The Bundle ID block and its edit modal have been removed — the bundle ID is set inside the publish dialog on the first publish and locked afterwards
  - **Right — Revisions**: header "Publish revision" button; once published, a compact Bundle ID row (label + monospace value + copy button) is shown at the top of the body, followed by the latest 10 revisions list. Each revision renders as `v<version>` when present (with `(rev <n>)` in muted text) or `rev <n>` for legacy rows. Rows include `current` / `installed` badges, install count, release notes, copy-content-hash button (with hash preview tooltip), and a delete button. Delete is disabled (with tooltip) whenever the row has more than the publisher's own install on it; clicking opens an `AlertDialog`
  - **Publish dialog** — owns three fields: `Bundle ID` (only rendered on first publish; prefilled with `agent.bundle_id`, sent as `PublishRequest.bundle_id`), `Version` (always; defaults to `"1.0"` on first publish, otherwise `suggestNextVersion(previousRevision.version)` — increments the trailing numeric component), and optional release notes. Inline `Alert` shows publish errors. Submit posts to `POST /agents/{id}/publish`
- `UserAllowlistPicker` (`frontend/src/components/Common/UserAllowlistPicker.tsx`) — shared search-and-pill picker for selecting users by email/name. Used for bundle access grants and for shared-with-users assignments in `McpConnectorsCard`. Caller passes a list of `{id, userId, fallbackLabel?}` items and `onAdd`/`onRemove` callbacks; the component owns the search input, results dropdown, and pill rendering, and fetches `/users` via React Query under key `["users-list"]`

### Credential Provisioning (Phase 5)
- `CredentialProvisioningSection` (`frontend/src/components/Agents/CredentialProvisioningSection.tsx`) — publisher-only card rendered inside `AgentBundleTab` when `agent.is_publisher_install === true`, wrapped in a `grid grid-cols-1 md:grid-cols-2 gap-6` so the card occupies half the row width on `md+` (mirrors the Bundle settings layout). Each row uses the bundle-settings pattern: descriptive cluster on the left, `w-[260px] shrink-0` Select on the right. Contains two subsections:
  1. **Service credentials** — one row per linked `Credential`. The left cluster leads with a `<CredentialTypeBadge>` (the prominent type chip pulled from `credentialTypes.ts`) and below it a "detected from \<credential name\>" caption, where the name is an outline `<Badge asChild>` linking to `/credential/$credentialId`. When the linked credential has `allow_sharing=false`, an amber `text-amber-700 dark:text-amber-300` warning with an `AlertTriangle` icon explains the row is locked to "User provides". The right Select offers `User provides` / `Embedded (shared)` (the latter labelled "Embedded (shared)" in copy and disabled when `allow_sharing=false`). The Select **auto-saves on change** — there is no Save button — by PATCHing the publisher install via `InstallsService.updatePublishSettings` (i.e. `PATCH /agents/{id}/publish-settings`) with the merged override map and showing a "Credential override saved" success toast on settle. The mutation invalidates `["agent", agentId]` + `["bundles"]`.
  2. **AI credentials** — header + helper paragraph, then one row per mode. Each row's left cluster shows the mode icon (`MessageCircle` blue for Conversation, `Hammer` orange for Building, matching `Chat/ModeSwitchToggle.tsx`), the label `Conversation AI` / `Building AI`, and a "SDK in use: \<engine label\>" caption derived from `EnvironmentsService.getEnvironment(agent.active_environment_id)` via `extractEngine(env.agent_sdk_conversation | agent_sdk_building)` and `getEngineLabel()` (re-exported from `EnvironmentConfigForm`). The right Select **filters AI credential options by `SDK_CREDENTIAL_COMPATIBILITY[engine]`** — e.g. `claude-code` only lists `anthropic` / `minimax` credentials; `opencode` lists `anthropic`/`openai`/`openai_compatible`/`google`. The dropdown's `__none__` sentinel maps to "None — user provides" and clears the FK; selecting a credential writes its UUID via `BundlesService.updateBundle` into `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id`. Auto-saves on change with a "AI credential saved" success toast (the legacy `Switch` and Save UI were removed). When the bundle is not yet published, the section shows an italic placeholder explaining publish is required first.

### Setup Banner (Phase 4)
- `SetupNeededBanner` — shown on the install/agent detail page (`frontend/src/routes/_layout/agent/$agentId.tsx`) above the chat surface when `status !== "ready"`. Queries `["agent", agentId, "setup-status"]`. Renders an amber `Alert` with an "Open setup" button for `needs_setup` and a destructive `Alert` for `publisher_broken`. Subscribes to all three Phase 4 WS events via `useMultiEventSubscription` and invalidates the setup-status query on receipt so the banner appears/disappears in real time.

### Setup Credentials Page (Phase 4)
- `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` — full-width form accessible via the `SetupNeededBanner`. Lists incomplete user-owned placeholder credentials (one card per credential). Each card shows the name, type badge, optional description, and a generic key/value editor. Save calls `InstallsService.updateSetupCredential` and invalidates both `setup-status` and `setup-credentials` queries. When `status === "ready"` the page shows a "Setup complete — close this tab and return to your chat" card. When `status === "publisher_broken"` the page shows a publisher-contact message instead of the credential form. Per-type credential forms (reusing `CredentialForms` components) are deferred — the file has a `// TODO: per-type credential forms` marker.

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
| `["agent", agentId, "setup-status"]` | `GET /agents/{agent_id}/setup-status` — fetched by `SetupNeededBanner` and the setup-credentials page; invalidated on `INSTALL_SETUP_REQUIRED`, `INSTALL_SETUP_COMPLETED`, `PUBLISHER_CREDENTIAL_BROKEN` WS events and after `PUT /setup-credentials/{id}` (Phase 4) |
| `["agent", agentId, "setup-credentials"]` | `GET /agents/{agent_id}/setup-credentials` — fetched by the setup-credentials page; invalidated after `PUT /setup-credentials/{id}` (Phase 4) |
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
| `backend/app/alembic/versions/aa1bc2d3e4f5_add_publisher_ai_credentials_to_agent_bundle.py` | Adds `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id` nullable UUID FK columns to `agent_bundle` (FK → `ai_credential.id`, `ON DELETE SET NULL`). `down_revision = i7e8f9a0b1c2`. No data backfill; downgrade drops both columns and their FK constraints |
| `backend/app/alembic/versions/bb2cd3e4f5a6_add_publish_settings_to_agent.py` | Adds `publish_settings` JSON column (default `{}`) to the `agent` table. `down_revision = aa1bc2d3e4f5`. No data backfill; existing rows are read as `{}` naturally. Downgrade drops the column |

## Runtime Cross-Service Rows Created at Install Time

| Row type | Created by | When |
|----------|-----------|------|
| `CredentialShare` (publisher → installer) | `InstallService._try_link_publisher_credential` | Each PBP service credential spec in the revision |
| `AgentCredentialLink` (install → publisher's Credential) | `InstallService._try_link_publisher_credential` | Same, after `CredentialShare` is ensured |
| `AICredentialShare` (publisher → installer) | `InstallService._link_publisher_ai_credential` | Each non-null `bundle.publisher_ai_credential_*_id`; also re-asserted on every idempotent re-install |
| `Credential` placeholder (is_placeholder=True) | `InstallService._setup_install_credentials` | PBU specs where the installer provides no selection, or when a PBP credential is unavailable (degraded mode) |
| `AgentCredentialLink` (install → placeholder) | `InstallService._setup_install_credentials` | After placeholder is created |

No new migrations were introduced in Phase 2. All rows above are created in the existing `credential`, `credential_share`, `ai_credential_share`, and `agent_credential_link` tables.

## Security

- `AgentBundlePublic` omits publisher email and raw UUID; only a truncated handle (`first 8 chars of UUID + "…"`) is exposed
- `CatalogEntryPublic` additionally surfaces `publisher_name`, `publisher_email`, and `latest_version` for the install/catalog UX. Catalog access is auth-gated to users who can already see the bundle row, so exposing the publisher's name + email matches the trust model of an internal-instance catalog. If a future deployment needs anonymised publishers, the resolver in `CatalogService._bundle_to_entry` is the single point to gate
- `required_credential_specs` contains only names and types; no secret values are ever stored
- Bundle deletion is blocked (`409`) until all foreign installs are removed
- Wipe of app-data volume is blocked (`409`) unless `is_orphaned = true`
- Bundle-id edit is blocked (`409`) after first publish to prevent orphaning installed app-data
