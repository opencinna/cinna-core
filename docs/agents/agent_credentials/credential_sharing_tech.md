# Credential Sharing - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/credentials/credential_share.py` - CredentialShare table model, CredentialSharePublic, CredentialShareCreate, SharedCredentialPublic response models
- `backend/app/models/credentials/credential.py` - Adds `allow_sharing` and `allow_template_sharing` fields to CredentialBase, `template_private_fields` (JSON list[str]) to Credential / CredentialCreate / CredentialUpdate / CredentialPublic; `service_uri: str | None` to `CredentialBase` (flows through `CredentialCreate`, `CredentialPublic`, and the DB model automatically) and to `CredentialUpdate` (editable, nullable); not sensitive — appears in `CredentialPublic` without redaction; `share_count`, `is_shared`, `owner_email` on CredentialPublic; `CredentialAffectedAgent` (id, name, ui_color_preset); `CredentialDeletionImpact` (tier, affected_own_agents, direct_share_count, bundle_usages, bundle_pbp_usages, active_install_count). New fields on `CredentialPublic`: `category: str` (default `"mine"`), `agent_usage_count: int` (default `0`), `used_in_bundle: bool` (default `False`).
- New fields on `SharedCredentialPublic` (in `credential_share.py`): `category: str` (default `"mine"`), `source: str | None`, `agent_usage_count: int` (default `0`). The `used_in_bundle` field is always `False` for shared rows in the current implementation (bundle badge is an owner concept; kept for shape symmetry).
- `backend/app/models/credentials/ai_credential.py` - `AICredentialBundleUsage` (bundle_uuid, bundle_id, display_name, publisher_install_id, used_for_conversation, used_for_building); `AICredentialDeletionImpact` (tier, bundle_usages)

### Backend - Models (user search picker)
- `backend/app/models/users/user.py` - `UserSearchResult` (id / email / full_name) and `UsersSearchPublic` (data + count) — minimal projection returned by `GET /users/search` for the sharing pickers; re-exported from `models/__init__.py`

### Backend - Services
- `backend/app/services/credentials/credential_share_service.py` - Core direct-sharing logic: share, revoke, toggle, access checks
- `backend/app/services/users/user_service.py` - `search_users(session, query, exclude_user_id, limit)` — case-insensitive substring match on `email` / `full_name` over active users, ordered by email; excludes the requester when `exclude_user_id` is set (`None` includes them); backs `GET /users/search`
- `backend/app/services/credentials/credentials_service.py` - `link_credential_to_agent()` accepts shared credentials; `update_credential()` flips `is_placeholder=False` only when `check_credential_completeness == "complete"` (load-bearing for the template setup flow)
- `backend/app/services/bundles/publish_service.py` - `_collect_credential_specs`, `_validate_publisher_provides`, `_template_payload_for` produce/validate `provided_by="template"` specs with `template_data` + `template_private_fields`
- `backend/app/services/bundles/install_service.py` - `_setup_install_credentials` template branch + `_materialise_template_credential` create the installer-owned placeholder pre-seeded with template defaults
- `backend/app/services/bundles/install_readiness_gate.py` - Detects template-materialised placeholders via `is_placeholder=True` and returns `placeholder_empty` until completion

### Backend - Routes
- `backend/app/api/routes/credential_shares.py` - Sharing CRUD endpoints (share, list, revoke, toggle, shared-with-me)
- `backend/app/api/routes/credentials.py` - `_credential_to_public()` exposes `allow_template_sharing` + `template_private_fields`; new `GET /credentials/{id}/bundles` endpoint with `provided_by` per usage
- `backend/app/api/routes/installs.py` - `PATCH /agents/{id}/publish-settings` accepts `provided_by="template"`; `GET /agents/{id}/setup-credentials` returns `template_private_fields` + `template_prefilled_data` per placeholder; `update_setup_credential` re-runs the gate

### Frontend - Components
- `frontend/src/components/Credentials/CredentialSectionTabs.tsx` — segmented control with counts for `/credentials` (replaces the deleted `CredentialFilters.tsx`). Exports `CredentialSection = "mine" | "automatic" | "bundle"`. Labels: `My Credentials`, `Automatic Credentials`, `Bundle Credentials`. Hash navigation (`#my` / `#automatic` / `#bundle`) is unchanged.
- `frontend/src/components/Credentials/CredentialSharing.tsx` - Direct-sharing toggle, share dialog, shares list, "Used in Bundles" block filtered to `provided_by="publisher"` usages, "Used in Skills" block from `skill_pbp_usages`
- `frontend/src/components/Credentials/CredentialUsagesSheet.tsx` - The "Show all (N)" Sheet shell behind both usage lists; the caller supplies `BundleUsageRow` / `SkillUsageRow` children
- `frontend/src/components/Credentials/CredentialTemplateSharing.tsx` - "Share as Template" toggle, per-field private/template checkboxes (with form labels mirrored from `CredentialFields/`), force-private message for OAuth + service account, "Used in Bundles" block filtered to `provided_by="template"` usages
- `SharedWithMeCredentials.tsx` (deleted) — the standalone "Shared with Me" list section is gone. Shared credentials now appear under the My Credentials or Bundle Credentials section depending on their `category`; the "Shared" treatment lives in `credentialColumns.tsx`.
- `frontend/src/components/Credentials/credentialColumns.tsx` — `DataTable` column defs for `/credentials` (replaces the deleted `CredentialCard.tsx`, `columns.tsx`, `CredentialActionsMenu.tsx`). Columns: Name (type glyph via `getCredentialTypeMeta`, name link, key-status badge, incomplete and bundle icons), `facts` (renders `CredentialRowBadges`) and a `RowActionsMenu` (Copy ID; Delete on owned rows via `deleteCredentialMutationKey`). Headers are `sr-only` (table uses `hideHeader`). Row click opens `/credential/$id`.
- `frontend/src/components/Credentials/CredentialRowBadges.tsx` — right-aligned cluster: type badge, share-count badge (owned: "Shared with N people"; shared row: "Shared with you by owner") and agent-count badge (`agent_usage_count`), each in a fixed-width `CountSlot` (`hidden lg:flex`, so below 1024 px only the type badge shows). Count badges are `Popover` triggers (hover opens after 200 ms without taking focus and closes 150 ms after the pointer leaves; click, Enter, Space or tap opens with focus inside, Escape returns focus to the badge) whose content queries lazily (`staleTime` 30 s): `["credential-shares", id]` -> `GET /credentials/{id}/shares`; `["credential-deletion-impact", id]` -> `affected_own_agents` (owned rows only; shared rows show "Used by N of your agents"). `HOVER_LIMIT = 5`, then "and N more".
- `frontend/src/components/Pending/PendingCredentials.tsx` — loading skeleton for the table.
- `frontend/src/components/Credentials/CredentialFields/ApiTokenFields.tsx` - optional `service_uri` field for `api_token` credentials; helper text: "Audience/slot id shared across all per-user tokens for the same bundle; not secret."
- `frontend/src/components/Credentials/CredentialForms/GenericCredentialForm.tsx` - optional `service_uri` field surfaced for `api_token` (and optionally `agent_api`) credentials; not surfaced for other types
- `frontend/src/components/Agents/CredentialProvisioningSection.tsx` - Per-spec `User provides` / `Embedded (shared)` / `Template (defaults + private)` dropdown on the publisher install's Bundle tab
- `frontend/src/components/Install/InstallServiceCredentialItem.tsx` - Renders the spec's `provided_by` badge + body; `TemplateProvidedBody` lists the private fields the installer will need to fill in; optionally renders the spec's `service_uri` (informational, so the installer understands why a differently-named credential was auto-suggested)
- `frontend/src/components/Install/InstallSetupForm.tsx` - `initialChoiceForSpec` returns `"skip"` for template specs (the install service short-circuits into the template branch)

### Frontend - Routes
- `frontend/src/routes/_layout/credentials.tsx` - **Reworked**: renders `<CredentialSectionTabs />` over a `DataTable` (`onRowClick`, `itemLabel="credentials"`, `tableClassName`, per-column `meta.className`); search over name, notes, type and owner; client paging at 30. Fetches every page of the owned list (`["credentials", workspaceFilter, "all-pages"]`; previously capped at 100) and the shared list (`["credentials-shared-with-me"]`), merges into one categorized view-model and filters by `credential.category`. `GET /credentials/` orders by name, then id, so the all-pages read is stable. Per-section empty states plus no-match/error states. URL hash `#my` / `#automatic` / `#bundle` mirrors and initializes the active section. `DeleteCredential.tsx` takes an optional trigger and exposes `deleteCredentialMutationKey`; `AddCredential.tsx` takes an optional `trigger`.
- `frontend/src/routes/_layout/credential/$credentialId.tsx` - SharedCredentialView (read-only) vs OwnedCredentialView (full edit); owned view wraps `CredentialSharing` and `CredentialTemplateSharing` in a 2-column grid
- `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` - Setup page renders one card per placeholder; template placeholders show a read-only "pre-filled by publisher" panel and one input row per private field
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` - Fetches both owned and shared credentials, shows "Shared" indicator in dropdown

### Migrations
- `backend/app/alembic/versions/g7b8c9d0e1f2_add_credential_sharing.py` - `allow_sharing` column on credential table, new `credential_shares` table
- `backend/app/alembic/versions/cc3de4f5a6b7_add_template_sharing_to_credential.py` - `allow_template_sharing` (boolean, default `false`) and `template_private_fields` (JSON list, default `[]`) on credential table
- `backend/app/alembic/versions/3f8f2a2e7f23_add_credential_service_uri.py` - `service_uri` (TEXT, nullable) column on credential table; partial btree index `ix_credential_service_uri (service_uri) WHERE service_uri IS NOT NULL`. All existing rows backfill to NULL (legacy behavior unchanged). Downgrade drops the index then the column
- `backend/app/alembic/versions/3c3c37a5e144_add_credential_share_source.py` - Adds `source` (VARCHAR(20), nullable, no server default) to `credential_shares`. `down_revision = 65d1ef4899be`. No index, no FK, no data backfill — existing rows default to NULL (read everywhere as `"direct"`). Downgrade drops the column.

- `backend/app/alembic/versions/bfd8b2ff2e3c_credential_share_revocation.py` - Creates `credential_share_revocation`

### Router Registration
- `backend/app/api/main.py` - Added `credential_shares.router` import and registration

### Tests
- `backend/tests/api/agents/bundles/agents_bundles_template_sharing_test.py` - Eight scenario tests: CRUD persistence, publish spec shape, override validation, install materialisation, completion → ready, partial fill stays needs_setup, `use_existing` opt-out, re-publish guard when consent flag is revoked
- `backend/tests/api/agents/core/agents_credentials_categorization_test.py` - Categorization correctness tests: owned `agent_api`/`mcp_provider` → `"automatic"`; owned other type → `"mine"`; direct-shared → recipient `"mine"`; PBP-install-shared → recipient `"bundle"`; install idempotency + first-writer-wins; NULL source legacy → `"mine"`; agent-usage count recipient-scoped; `used_in_bundle` flag accuracy; `mcp_provider` folds into Automatic tab.

## Database Schema

### Modified Table: `credential`
- `allow_sharing` (boolean, default false) — direct-sharing consent flag
- `allow_template_sharing` (boolean, default false) — template-sharing consent flag (independent from `allow_sharing`)
- `template_private_fields` (JSON, default `[]`) — list of `credential_data` field names that must be supplied per-installer; only stored, never inferred
- `service_uri` (TEXT, nullable, default NULL) — plaintext audience/slot id used by the Tier 0a/0b matcher. NULL = legacy behavior, no change in matching. Non-secret; included in `CredentialPublic` without redaction. Added in migration `3f8f2a2e7f23`
- Index: `ix_credential_allow_sharing` (partial index for shareable credentials)
- Index: `ix_credential_service_uri` — partial btree on `(service_uri) WHERE service_uri IS NOT NULL`; keeps the index small since the vast majority of rows will remain NULL

### New Table: `credential_shares`
- `id` (UUID, PK)
- `credential_id` (UUID, FK → credential, CASCADE delete)
- `shared_with_user_id` (UUID, FK → user, CASCADE delete)
- `shared_by_user_id` (UUID, FK → user)
- `shared_at` (datetime)
- `access_level` (varchar, default 'read')
- `source` (varchar(20), nullable) — provenance marker: `"direct"` | `"bundle_install"` | `"skill_install"` | NULL (legacy, read as `"direct"`). Stamped at creation, never updated. Added in migration `3c3c37a5e144` (nullable, no server default, no index, no backfill); the skill source uses the existing column.
- Unique constraint: `(credential_id, shared_with_user_id)`

### New Table: `credential_share_revocation`
Records that the owner removed a person's share. Written by `revoke_credential_share` on `DELETE /credentials/{id}/shares/{share_id}` for any source (`INSERT ... ON CONFLICT DO NOTHING`); cleared only by the owner's direct re-share via `share_credential`. Read by `provision-local` (answers `not_shareable`) and by the cloud bundle/skill install path (`CredentialProvisioner`: publisher credential treated as unavailable, giving `publisher_unavailable` / placeholder). Turning sharing off neither writes nor clears rows. Migration `bfd8b2ff2e3c`.

### Share and deletion-impact payload additions
- `CredentialSharePublic` gains `agent_usages [{id, name, skill_display_names}]` (only agents with at least one owner skill), `direct_agent_use_count`, and `local_skill_usages [{package_uuid, package_id, display_name, revision_number, host_agent_name?}]` (`host_agent_name` only when the recipient published that desktop agent).
- `CredentialDeletionImpact` gains `local_install_share_count`, `active_local_skill_install_count`, `local_install_skill_usages`. Active local skill installs make delete Tier 2 (409 unless `force=true`).

### Bundle Revision Spec (no DB column — JSON inside `agent_bundle_revision.required_credential_specs`)

Each entry the publish flow emits:
- `name`, `type`, `description` — credential metadata
- `provided_by` — `"user"` | `"publisher"` | `"template"`
- `publisher_credential_id` — populated only when `provided_by="publisher"`
- `template_data` — non-private credential_data values; only present when `provided_by="template"`
- `template_private_fields` — list of fields the installer must supply; only present when `provided_by="template"`
- `service_uri` — optional plaintext slot id; only present when the publisher's linked credential has a non-null `service_uri`. Steers the Tier 0a/0b matcher at install time. Never secret; coalesces to `None` when missing from old revision JSON (backward compatible)

## API Endpoints

### User Search for Sharing Pickers (`backend/app/api/routes/users.py`)
- `GET /api/v1/users/search?q=&limit=&include_self=` - Case-insensitive substring search on `email` / `full_name` for the sharing pickers. Available to **any authenticated user** (not superuser-gated like `GET /users/`), so non-admin owners (agent-developers) can find recipients. Returns a minimal `UsersSearchPublic` projection (`UserSearchResult`: id / email / full_name only) — never the full `UserPublic` payload. Requires `q` of at least 2 characters (shorter → empty list); `limit` clamped to 1-25. Excludes the requester by default; `include_self=true` keeps them in results (for owner-self pickers like the Agent REST API Access & Scopes card). Declared **before** `GET /users/{user_id}` so the literal `/search` path is matched first. Backed by `UserService.search_users`.
- `GET /api/v1/credentials/{credential_id}/shares` - List all shares for a credential
- `DELETE /api/v1/credentials/{credential_id}/shares/{share_id}` - Revoke specific share
- `GET /api/v1/credentials/shared-with-me` - Get credentials shared with current user
- `PATCH /api/v1/credentials/{credential_id}/sharing` - Enable/disable sharing

### Updated Credential Endpoints (`backend/app/api/routes/credentials.py`)
- `GET /api/v1/credentials/{id}` - Allows viewing if user owns OR has share (returns `is_shared=true` for shared)
- `GET /api/v1/credentials/{id}/deletion-impact` - Returns a `CredentialDeletionImpact` classifying the blast radius of deleting this credential (Tier 0 / 1 / 2). Owner-only; returns 404 when the credential does not exist or the requester is not the owner (no existence leak).
- `DELETE /api/v1/credentials/{id}?force=` - Deletes the credential. Blocked with HTTP 409 at Tier 2 (PBP in a published bundle or catalog skill with active foreign installs) unless `force=true` is passed; Tier 0 and Tier 1 always proceed. The 409 body is the serialised `CredentialDeletionImpact` so the frontend can render affected bundles and skills and offer force delete.
- `GET /api/v1/credentials/{id}/bundles` - Lists bundles whose publisher install has this credential linked; each entry resolves `provided_by` via `_resolve_provided_by_for_usage()` (override map → consent-flag inference) so the frontend can split usages between the Sharing and Share-as-Template cards
- All endpoints return `share_count`, `is_shared`, `owner_email`, `allow_template_sharing`, `template_private_fields` in CredentialPublic response via `_credential_to_public()` helper

### AI Credential Deletion-Impact Endpoints (`backend/app/api/routes/ai_credentials.py`)
- `GET /api/v1/ai-credentials/{credential_id}/deletion-impact` - Returns an `AICredentialDeletionImpact` classifying the blast radius (Tier 0 or Tier 2 only). Owner-only; 404 for missing or not-owned.
- `DELETE /api/v1/ai-credentials/{credential_id}?force=` - Deletes the AI credential. Blocked with HTTP 409 at Tier 2 unless `force=true`; the 409 body is the serialised `AICredentialDeletionImpact`.

### Install Setup Endpoints (`backend/app/api/routes/installs.py`)
- `PATCH /api/v1/agents/{id}/publish-settings` - Accepts `provided_by` ∈ {`"user"`, `"publisher"`, `"template"`} per linked credential
- `GET /api/v1/agents/{id}/setup-credentials` - Returns `SetupCredentialSummary` with `template_private_fields` + `template_prefilled_data` for template-materialised placeholders
- `PUT /api/v1/agents/{id}/setup-credentials/{credential_id}` - Persists user-supplied data; re-runs `InstallReadinessGate.check()` and emits `INSTALL_SETUP_COMPLETED` when the gate flips to `ready`

## Services & Key Methods

### CredentialShareService (`backend/app/services/credentials/credential_share_service.py`)
- `share_credential(..., source="direct")` - Create share with validations (ownership, allow_sharing, target exists, not self, not duplicate); stamps `source="direct"` on the new `CredentialShare` row. The direct-sharing UI passes the default; no client-supplied source is accepted.
- `revoke_credential_share()` - Async. Deletes the share record with an ownership check, then calls `CredentialsService.unlink_credential_from_revoked_recipients` for that recipient
- `get_shares_by_credential()` - List shares with resolved user emails
- `get_credentials_shared_with_me()` - Query shares where user is recipient; enriched to compute `category` via `classify_credential_category(is_owned=False, ...)` from `share.source`, populate `agent_usage_count` via batched `get_agent_usage_counts` (recipient-scoped), and carry `source` on `SharedCredentialPublic`.
- `get_share_count_for_credential()` - Count shares for a credential
- `update_credential_sharing()` - Async. Toggles allow_sharing; auto-revokes all shares when disabled, then unlinks every recipient the same way. `CredentialsService.update_credential()` gives an explicit `allow_sharing=false` in the generic `PUT /credentials/{id}` the identical revocation semantics, so no update path can leave a usable shared credential linked in a recipient environment.
- `can_user_access_credential()` - Check if user owns OR has share
- `delete_all_shares_for_credential()` - Bulk delete for credential deletion

### CredentialsService (`backend/app/services/credentials/credentials_service.py`)
- `classify_credential_category(*, is_owned, credential_type, share_source)` — **the categorization SSOT**. Pure static method. Holds `AUTOMATIC_TYPES = {CredentialType.AGENT_API, CredentialType.MCP_PROVIDER}`. Rules: owned + automatic type → `"automatic"`; owned + other type → `"mine"`; shared + `source="bundle_install"` → `"bundle"`; shared + `source="skill_install"` → `"automatic"` (the one case where a *received* share is automatic — a catalog skill install created it, no hand action did); shared + `source in {"direct", None}` → `"mine"`. A received share is classified by provenance alone, never by type. NULL is coalesced to `"direct"`. Both the `/credentials` projection and `get_credentials_shared_with_me` delegate to this function — the frontend never re-derives category.
- `get_agent_usage_counts(session, credential_ids, owner_scope=None)` — **batched** count helper. One `GROUP BY` query over `AgentCredentialLink` for the whole page, returning `{credential_id: count}`. For shared credentials, pass `owner_scope=recipient_id` to count only the recipient's own agents. Avoids per-row N+1.
- `get_used_in_bundle_flags(session, *, owner_id, credential_ids)` — **batched** boolean helper. Single `DISTINCT credential_id` query over the `list_bundle_usages` join, returning the subset of ids that appear in ≥1 of the owner's bundles. Owner-scoped (only the owner's published bundles count).
- `link_credential_to_agent()` - Allows linking shared credentials (not just owned)
- `unlink_credential_from_revoked_recipients(session, credential_id, recipient_user_ids)` — the revocation counterpart. Deletes every `AgentCredentialLink` to the credential on agents owned by a revoked recipient, commits, then fires `event_credential_unshared` per agent. Returns the unlinked agent ids. Nothing else re-checks share access at materialisation time (`get_agent_credentials` joins the link table only), so this is what makes revocation reach the container
- `update_credential()` - Persists `allow_template_sharing` + `template_private_fields`; flips `is_placeholder=False` only when `check_credential_completeness == "complete"` (so partial fills on template placeholders keep the gate engaged); rejects non-`list[str]` `template_private_fields` payloads; also persists `service_uri` (editable, nullable)
- `check_credential_completeness()` - Per-type required-field check the placeholder-flip relies on
- `find_match_for_spec(session, user_id, spec_name, spec_type, *, service_uri=None, ...)` — install-time auto-prefill matcher. Full precedence order when `service_uri` is a non-empty string: **(Tier 0a)** owned credential with matching `service_uri` + `type` (newest by `id desc`); **(Tier 0b)** shared credential (via `CredentialShare`) with matching `service_uri` + `type`; these tiers short-circuit even the PBT value-anchor check. When `service_uri` is `None` or empty, Tiers 0a/0b are bypassed and the remaining tiers run unchanged. Remaining tiers: (1) owned name+type; (2) shared name+type; (3) type-only fallback (PBU only); PBT value-anchor runs after name tiers for PBT specs
- `find_slot_match(session, *, user_id, credential_type, service_uri)` — tiers 0a+0b on their own, extracted so `CredentialProvisioner` can reuse exactly the slot half of the matcher. Owned first, then shared, newest `id` first within each; placeholders are candidates on purpose (a slot placeholder from an earlier install is reused, not duplicated, which is what makes a skill install idempotent); an empty `service_uri` never matches. **This is the only tier anything auto-links** — the name and type-only tiers stay suggestion-only.
- `get_deletion_impact(session, credential_id, requester_id)` - Classifies deletion blast radius into Tier 0 / 1 / 2. Owner-only: raises `ValueError("Credential not found")` for missing or non-owned rows (route maps to 404). Composes three signals: (1) own agents via `get_affected_agents` (each row includes `ui_color_preset` for badge rendering); (2) direct share count via `CredentialShareService.get_share_count_for_credential`; (3) all bundle usages via `list_bundle_usages` (any provisioning mode). The full `list_bundle_usages` result is returned as `bundle_usages` (informational, all modes); the `"publisher"`-mode subset is returned separately as `bundle_pbp_usages` and is the sole driver of the Tier-2 block. The `active_install_count` is scoped to foreign installs of the PBP bundle(s) only — direct-share linkers are excluded so they are not double-counted with `direct_share_count`. A fourth signal does the same for catalog skills: `SkillCredentialRequirements.publisher_usages_of_credential` returns `skill_pbp_usages` (the requester's packages whose frozen revision specs name this credential as publisher-provided) plus **every** revision id of those packages, and `active_skill_install_count` counts the distinct foreign agents that link the credential **and** hold a `source=catalog` plugin link pinned to one of them — scoped by install for the same reason as the bundle half, but at package granularity, because upgrading never releases a slot a later revision dropped, so an installer past the providing revision keeps both the share and the link. Tier 2 requires (PBP usage AND `active_install_count > 0`) **or** (skill PBP usage AND `active_skill_install_count > 0`).
- `delete_credential(session, credential_id, owner_id, force=False)` - Raises `CredentialInUseError` (HTTP 409) at Tier 2 unless `force=True`. The error carries the full `CredentialDeletionImpact` so the route can serialise it as the 409 body without a second lookup.
- `list_bundle_usages(*, credential_id, requester_id)` - Owner-only listing of bundles whose publisher install links this credential. Resolves each entry's `provided_by` via `PublishService.resolve_provided_by` so the projection matches what the publish-time spec collector would emit. Raises `ValueError("Credential not found")` for missing or non-owned rows (route maps to 404 to avoid leaking existence). Backs `GET /credentials/{id}/bundles`

### AICredentialsService (`backend/app/services/credentials/ai_credentials_service.py`)
- `get_deletion_impact(session, credential_id, user_id)` - Tier 0 or Tier 2 for AI credentials. Tier 2 when any `AgentBundle` has `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id` pointing at this credential. Owner-only (404 for missing or not-owned) via `_get_owned_credential_or_404`.
- `delete_credential(session, credential_id, user_id, force=False)` - Raises `AICredentialInUseError` at Tier 2 unless `force=True`. On forced delete, PostgreSQL `ON DELETE SET NULL` nulls the bundle FKs, degrading affected bundles to "user provides".
- `_get_owned_credential_or_404(session, credential_id, user_id)` - Returns 404 (not 403) when the credential exists but is not owned by `user_id`, preventing existence leaks on owner-only deletion paths. The general `get_credential` still returns 403 for non-owners; only the deletion paths use this stricter helper.

### PublishService (`backend/app/services/bundles/publish_service.py`)
- `resolve_provided_by(credential, publisher_install)` - Public static method that is the single source of truth for `provided_by` resolution. Order: publisher override → `allow_sharing` → `allow_template_sharing` → `"user"`. Used by three paths so they cannot disagree: (1) publish-time spec emission (`_collect_credential_specs`); (2) `CredentialsService.list_bundle_usages` (`GET /credentials/{id}/bundles` projection); (3) `compute_credential_spec_drift` (live side of the publish-vs-snapshot diff).
- `compute_credential_spec_drift(session, install)` → `BundleCredentialDrift` — diffs each linked credential's live `provided_by` (via `resolve_provided_by`) against the latest published revision's snapshot (via `parse_credential_spec` from `credential_spec.py`). Returns `BundleCredentialDrift{stale: bool, drift: list[CredentialSpecDrift]}`. `stale` is also set when credentials have been removed from the publisher install since the last publish. Used by `GET /agents/{agent_id}/bundle-credential-drift` (publisher-install owner-only, `require_developer`-gated, 404 leak-safe). See [Agent Bundles — Credential sharing drift detection](../agent_bundles/agent_bundles.md) for the full business-logic description.
- `_collect_credential_specs()` - Reads linked credentials and emits the per-spec shape; delegates `provided_by` to `resolve_provided_by`; for template specs attaches `template_data` + `template_private_fields` via `_template_payload_for`
- `_template_payload_for()` - Decrypts the credential, strips private fields, applies the per-type templatable allowlist (e.g. `ssh_key` → only `host_aliases`); raises `ValueError` on decrypt failure rather than silently shipping an empty payload
- `_validate_publisher_provides()` - Pre-publish guard: rejects publish when a `"publisher"` spec has no shareable backing credential or a `"template"` spec has `allow_template_sharing=False`
- `_TEMPLATE_FORCE_PRIVATE_TYPES` - Hard-strips `credential_data` from template payloads for OAuth + service account types regardless of UI state (defence in depth)
- `_TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE` - Per-type allowlist applied after the publisher's private-field filter

### CredentialProvisioner (`backend/app/services/credentials/credential_provisioner.py`)

**The one writer of install-time shares, links and placeholder credentials**, for bundles and catalog skills alike. The two differ in policy, not mechanism, and every difference is a field of `ProvisionPolicy` (`BUNDLE_INSTALL_POLICY` / `SKILL_INSTALL_POLICY`): the share source, whether a slot match auto-links, how a placeholder is named and whether it is stamped with the slot and workspace, and whether install-form selections are honoured. Synchronous, flushes, **never commits** — the caller owns the transaction.

- `provision(session, *, agent, specs, publisher, policy, user_selections=None)` → `ProvisionReport` — runs the bundle loop or the skill decision tree per policy. `report.changed` tells the caller whether to sync credentials to environments; `report.degraded` records a publisher or template spec that could not be honoured as published.
- `preview(session, *, agent, specs, publisher, policy)` — the same decision tree, read-only, for the skill install preview. Slot-matching policies only; bundles have no preview.
- `release_skill_slots(session, *, agent, released, retained)` — uninstall's half: unlinks installer-owned **placeholders** carrying a released `(type, slot)` that no retained spec also declares, and deletes one that no agent links any more. Real and shared credentials are never touched, and bundle-claimed ones are skipped.
- `_share_and_link_publisher_credential()` — ensures a `CredentialShare` exists for the installer, stamping the policy's source on **insert only**. **Idempotent / first-writer-wins**: an existing row (prior direct share, prior install) keeps its `source`, so a prior `source="direct"` share keeps the credential in My Credentials for that recipient. No share at all when the installer *is* the publisher.
- `_usable_publisher_credential()` — the trust boundary, re-checked live at every install: the credential still exists, still has `allow_sharing`, and **is owned by the package publisher** (`PublisherBoundary`). A boundary whose `user_id` is `None` still runs the check and so rejects everything; passing no boundary at all skips it — the distinction a bare `uuid | None` could not express, and the bundle path needs both.
- `_materialise_template()` — a fresh `Credential` owned by the installer, `encrypted_data` seeded from the spec's non-private `template_data`, `is_placeholder=True`, sharing off, `template_private_fields` mirrored so the setup page can highlight what is still missing.
- `_ensure_link()` — the single link-insert path, which is what makes a reinstall never produce a second link.
- `bundle_claimed_credential_ids(session, *, agent, linked_credentials)` — which of an agent's linked credentials its installed bundle revision claims, by `publisher_credential_id`, by spec name (or `"<name> (placeholder)"`), or — for a spec no linked credential answers either way — by type. It deliberately **over**-claims: under-claiming would drop a blocker from the readiness gate or release a placeholder the bundle depends on.

### InstallService (`backend/app/services/bundles/install_service.py`)
- `_setup_install_credentials()` - Owns the transaction and maps a `ProvisioningSelectionError` to HTTP 422; the per-spec loop itself is `CredentialProvisioner.provision(..., policy=BUNDLE_INSTALL_POLICY)`. Bundle behaviour is unchanged by the extraction — the bundle install suites pass without a single edit. Sets `last_update_status="degraded"` when the report is degraded
- `update_publish_settings()` - Validates and persists partial updates to `install.publish_settings` (`credential_overrides` map and pre-publish AI credential draft); enforces publisher-install scope, that override keys reference linked credential names, that each `provided_by` is one of `"user"`/`"publisher"`/`"template"`, and that AI credential ids are owned by the install owner. Backs `PATCH /agents/{id}/publish-settings`
- `list_setup_credentials()` - Returns `SetupCredentialSummary` items for placeholder credentials linked to the install. For template-materialised rows, decrypts and surfaces non-private fields under `template_prefilled_data` (decryption failures fall back to `{}` so the row still surfaces). Backs `GET /agents/{id}/setup-credentials`

### InstallReadinessGate (`backend/app/services/bundles/install_readiness_gate.py`)
- `check()` / `_scan_service_credentials()` - Returns `placeholder_empty` for any installer-owned `is_placeholder=True` credential, including template-materialised rows; same pathway as PBU placeholders
- `_drop_skill_provisioned()` - Drops from the missing list any item whose credential a **catalog skill** on the agent provisioned, unless the agent's bundle revision claims it too. An unfilled skill slot is a warning on the skill's Addons row, not a refusal on every channel. Called only when the list is already non-empty, so a ready agent pays no extra query, and every `GateMissingItem` now carries an internal `credential_id` for it (never serialised — the dispatcher payload and the setup-status response list their keys explicitly). Imports `SkillSlotIndex` lazily: the skills domain sits above bundles in the import graph

## Frontend Components

### CredentialSharing (`frontend/src/components/Credentials/CredentialSharing.tsx`)
- Direct-sharing toggle in the CardHeader corner (matches the `WebappShareCard` pattern; the `EmailIntegrationCard` component this originally also matched was deleted along with per-agent Email Integration — see [Email Integration](../../application/email_integration/email_integration.md#capabilities-removed-in-this-refactor)); body collapses when disabled
- Sharing UI is the shared **`UserAllowlistPicker`** (pill UX, same as MCP route / identity / bundle-grant sharing) rendered inline when sharing is enabled — no separate "Share" dialog or row-style shares list. `selected` is the existing `CredentialShare` rows mapped to `{ id: share.id, userId: share.shared_with_user_id, fallbackLabel: share.shared_with_email }`; `onAdd(u)` shares via `shareMutation` (by `u.email`), `onRemove(item)` revokes by `item.id`. The picker searches server-side via `GET /users/search` (key `["user-search", q]`) and excludes already-selected users by `userId`. See [User Selector Pattern](../../development/frontend/user_selector_pattern.md).
- **Counter freshness:** the share / revoke / disable mutations all invalidate `["credential-with-data", id]` in addition to `["credential", id]` and `["credentials"]` (via the shared `invalidateShareCaches()` helper). The detail page's "Shared with N users" header reads `share_count` from the `credential-with-data` query, so without this invalidation the counter went stale until a full reload (the root cause of the "counter doesn't update right away" bug — fixed with invalidation, not a websocket event, since the share originates from the same client viewing the counter).
- Confirmation dialog when disabling sharing with active shares. When the credential is PBP in published bundles or catalog skills, the dialog shows a destructive alert listing the affected bundles, skills and install counts so the publisher can see the blast radius before confirming.
- Fetches `GET /credentials/{id}/deletion-impact` (shared query key `["credential-deletion-impact", id]`) **un-gated** — the card body's "Used in Skills" list reads it, so it can no longer wait for a dialog to open. The two dialogs it also feeds therefore open with the data already warm. `DeleteCredential` keeps its `enabled: isOpen` gate deliberately: it renders once per row in the credentials list, where an un-gated read would be one request per row, and on the detail page it reads the warm cache entry anyway.
- "Used in Bundles" block listing usages where `provided_by="publisher"` (from `list_credential_bundle_usages`); each entry deep-links to `/agent/{publisher_install_id}#bundle`
- "Used in Skills" block listing `skill_pbp_usages`; each entry deep-links to `/catalog/skills/{package_uuid}`. Both blocks cap at five with a "Show all" `CredentialUsagesSheet` (one shell, rows supplied by the caller), and both hide only once known to be empty, so a failed read never reads as "used nowhere"
- Early-returns `null` when `useRole().isAgentUser` is true, so the entire card is hidden from `agent-user` accounts

### UnshareCredentialDialog (`frontend/src/components/Credentials/CredentialUsageRows.tsx` and `CredentialSharing.tsx`)
- Opens on removing a person only when that person has usages (agents, skills, skills on their computer); otherwise removal is immediate. The sharing-off confirm lists the same usages plus a re-enable note.

### DeleteCredential (`frontend/src/components/Credentials/DeleteCredential.tsx`)
- Service credential delete dialog used from the credential detail page and credential card dropdown menu
- Fetches `GET /credentials/{id}/deletion-impact` (query key `["credential-deletion-impact", id]`) when the dialog opens
- Tier 0: lists affected own agents as colored badge chips (`ui_color_preset` from `CredentialAffectedAgent` → `getColorPreset()`) with a Bot icon, one chip per agent
- Tier 1: shows a destructive alert "N users will lose access to this credential immediately"
- Tier 2: shows a destructive alert describing the broken installs, lists affected bundles from `bundle_pbp_usages` with "Open" links to the publisher install's Bundle tab, and replaces the "Delete" button with "Force delete & break installs" (passes `force=true`)
- All tiers: when `bundle_usages` (all-modes) is non-empty, a "Used in bundles" section is rendered below the tier-specific alert showing every bundle the credential belongs to, with its provisioning-mode label (`Shared with installers` / `Template` / `User-provided`) and an "Open" deep-link; this is informational and does not change tier/block logic
- Adds a sentence for active local skill installs (`active_local_skill_install_count`, `local_install_skill_usages`)
- On a 409 race (a non-forced delete that comes back 409 mid-dialog): invalidates the impact query and shows an inline error toast instead of a generic error

### DeleteAICredentialDialog (`frontend/src/components/UserSettings/DeleteAICredentialDialog.tsx`)
- AI credential delete dialog used from Settings → AI Credentials
- Fetches `GET /ai-credentials/{id}/deletion-impact` (query key `["ai-credential-deletion-impact", id]`) when open
- Tier 2 only (no Tier 1 path): shows a destructive alert describing bundle degradation, lists affected bundles (with conversation/building usage line), and shows "Force delete & degrade bundles" button (passes `force=true`)
- Tier 0: no extra warning, standard delete confirmation

### CredentialTemplateSharing (`frontend/src/components/Credentials/CredentialTemplateSharing.tsx`)
- Template-sharing toggle in the CardHeader corner; body collapses when disabled
- Per-credential-type field schema (`FIELDS_BY_TYPE`) — fixed list rendered regardless of which fields the publisher has filled in; labels mirrored from `CredentialFields/` form labels via `FIELD_LABELS_BY_TYPE`
- Per-field private/shared checkboxes laid out in a `grid-cols-[auto_1fr_auto]` grid; status text reads "private - user has to provide" / "shared - will be copied"
- Default-private fields seeded the first time the publisher enables template sharing (`DEFAULT_PRIVATE_FIELDS_BY_TYPE`)
- Force-private types (`FORCE_PRIVATE_TYPES`: OAuth + service account) render an info paragraph explaining only the credential's name and notes ship; no field checkboxes
- SSH key only exposes `host_aliases` (per the backend allowlist)
- Amber warning when `allowTemplate=true` AND `privateFields=[]` AND `fieldNames>0`
- "Used in Bundles" block listing usages where `provided_by="template"`; deep-links to `/agent/{publisher_install_id}#bundle`

### CredentialProvisioningSection (`frontend/src/components/Agents/CredentialProvisioningSection.tsx`)
- Per-spec `provided_by` dropdown on the publisher install's Bundle tab: `User provides` / `Embedded (shared)` / `Template (defaults + private)`
- Each option disabled until the corresponding consent flag is set on the credential

### Install Screen Components
- `frontend/src/components/Install/InstallServiceCredentialItem.tsx` - Renders the `provided_by` badge ("publisher-provided" / "template" / "user-provided"); `TemplateProvidedBody` lists the private fields the installer will need to fill in after install
- `frontend/src/components/Install/InstallSetupForm.tsx` - `initialChoiceForSpec` returns `mode="skip"` for template specs; the install service short-circuits into the template branch when `wants_existing` is false

### Setup Page (`frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx`)
- One card per placeholder; template placeholders detected via `credential.template_private_fields.length > 0`
- Read-only "Pre-filled by publisher" panel shows `template_prefilled_data` entries
- One input row per private field with the key fixed; non-template placeholders fall back to the legacy add/remove key/value editor
- Save button disabled until every private field has a non-empty value

### SSH Key Edit View (`frontend/src/components/Credentials/CredentialForms/SSHKeyEditView.tsx`)
- "Private key is encrypted..." Alert wraps content in a `<p>` so the shadcn `AlertDescription` (which uses `display: grid`) keeps the sentence inline instead of breaking into stacked grid items

### SharedWithMeCredentials (deleted)
- Previously displayed credentials shared with the current user as a standalone list section. Deleted as part of the filter-tabs feature. Shared credentials now appear inline in the My Credentials and Bundle Credentials sections based on their `category`.

### Credential Detail Route (`frontend/src/routes/_layout/credential/$credentialId.tsx`)
- Detects `is_shared` flag to switch between SharedCredentialView (read-only) and OwnedCredentialView (full edit)
- Owned view wraps `CredentialSharing` and `CredentialTemplateSharing` in a `grid grid-cols-1 lg:grid-cols-2 gap-6` container
- Header shows "Shared" badge and owner email for shared credentials
- Delete button hidden for shared credentials

### AgentCredentialsTab (`frontend/src/components/Agents/AgentCredentialsTab.tsx`)
- Fetches both owned credentials and credentials shared with user
- "Add Credential" modal replaced the legacy text dropdown with a searchable, credential-type-grouped badge picker (icon + credential name per badge) backed by `CREDENTIAL_TYPE_GROUPS` and `getCredentialTypeMeta` from `frontend/src/components/Credentials/credentialTypes.ts`
- Shows "Shared" indicator (owner email tooltip) in the badge picker
- Displays "Shared" badge in table for linked shared credentials

## State Management

### Query Keys
- `["credentials"]` - User's owned credentials list
- `["credential", credentialId]` - Single credential detail
- `["credential-with-data", credentialId]` - Decrypted credential payload (used by template-sharing UI to read field schema)
- `["credential-shares", credentialId]` - Shares for a credential
- `["credential-bundle-usages", credentialId]` - `GET /credentials/{id}/bundles` response, filtered client-side by `provided_by` for each card
- `["credential-deletion-impact", credentialId]` - `GET /credentials/{id}/deletion-impact` response; shared between `DeleteCredential` and the disable-sharing dialog in `CredentialSharing` so either entry point warms the same cache
- `["ai-credential-deletion-impact", credentialId]` - `GET /ai-credentials/{id}/deletion-impact` response; used by `DeleteAICredentialDialog`
- `["credentials-shared-with-me"]` - Credentials shared with current user
- `["user-search", query]` - `GET /users/search` results for the share dialog's user picker; enabled only when the dialog is open and the trimmed query is ≥2 chars
- `["agent", agentId, "setup-status"]` / `["agent", agentId, "setup-credentials"]` - Driven by the install setup page

### Mutations
- `shareCredential` - Create new share
- `revokeCredentialShare` - Delete share
- `updateCredentialSharing` - Toggle allow_sharing
- `updateCredential` - Used by `CredentialTemplateSharing` to persist `allow_template_sharing` + `template_private_fields`
- `updatePublishSettings` - Per-spec `provided_by` override map on the publisher install
- `updateSetupCredential` - PUT setup-credentials/{id} from the setup page

## Security

### Validation Rules
- Owner-only operations: share, revoke, toggle `allow_sharing` / `allow_template_sharing`
- Direct share requires `allow_sharing=true`
- Template publish requires `allow_template_sharing=true` (enforced by `PublishService._validate_publisher_provides`)
- Cannot share with non-existent users, yourself, or create duplicates
- `update_credential` rejects malformed `template_private_fields` payloads (must be `list[str]`)
- `CredentialInUseError` — raised by `CredentialsService.delete_credential` at Tier 2; carries `impact: CredentialDeletionImpact`. The route maps it to HTTP 409 and serialises `impact.model_dump(mode="json")` as the response `detail`.
- `AICredentialInUseError` — raised by `AICredentialsService.delete_credential` at Tier 2; carries `impact: AICredentialDeletionImpact`. Same HTTP 409 mapping.

### Access Control
- `CredentialShareService.can_user_access_credential()` - Returns true if owner OR has share
- `CredentialsService.link_credential_to_agent()` - Allows linking owned OR shared credentials to agents
- `GET /credentials/{id}/bundles` - 403 unless requester owns the credential (or is superuser)
- Share recipients get read-only access (can use, cannot see values)
- Credential values (encrypted_data) never exposed to share recipients
- Revoking share immediately removes access, deletes the recipient's `AgentCredentialLink` rows and re-syncs their running environments
- Disabling sharing is destructive (revokes all shares with warning) and unlinks every recipient

### Template Privacy Layers (defence in depth)
1. **Frontend filter** — only fields the publisher leaves unchecked are sent as `template_private_fields=[]` candidates
2. **Publisher private-fields filter** — `_template_payload_for` strips fields named in `template_private_fields` from the decrypted blob
3. **Per-type templatable allowlist** — `_TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE` (e.g. `ssh_key` → only `host_aliases`) drops anything outside the allowlist
4. **Force-private types** — `_TEMPLATE_FORCE_PRIVATE_TYPES` (OAuth + service account) zeros out `template_data` regardless of UI state; only credential `notes` ride through via `spec.description`
5. **Materialised row defaults** — `_materialise_template_credential` always writes `allow_sharing=False` and `allow_template_sharing=False` on the installer's row; downstream re-sharing requires an explicit toggle

## Group Shares

Added with [User Groups](../../application/user_groups/user_groups_tech.md) (migration `7896e6f51c03`).

- Table `credential_group_share` (`backend/app/models/credentials/credential_group_share.py`): credential x group, unique pair, shared-by user. Models `CredentialGroupShareCreate`, `CredentialGroupSharePublic`, `CredentialGroupSharesPublic`.
- Endpoints in `backend/app/api/routes/credential_shares.py`: `POST` / `GET /api/v1/credentials/{credential_id}/group-shares`, `DELETE /api/v1/credentials/{credential_id}/group-shares/{group_id}`. Errors via `CredentialGroupShareError` (carries status and detail). Owner only.
- `CredentialShareService`: `share_with_group`, `list_group_shares`, `revoke_group_share` / `remove_group_share`, `group_shared_credential_ids_subquery`, `group_shared_ids`, `via_groups`, `users_without_access(session, credential_id, user_ids)` (owner, direct share or any group share keeps access), `get_group_share_count_for_credential`, `get_group_member_count_for_credential`, `delete_group_shares_for_credential`.
- `CredentialsService.unlink_credential_from_revoked_recipients` filters recipients through `users_without_access` first (D8), then deletes links and emits `event_credential_unshared` re-syncs.
- Group shares never touch `credential_share_revocation` or `CredentialShare.source` (D7).
- Read paths widened to group members: shared-with-me, `get_credential_for_use`, `skill_credential_requirements`, `install_readiness_gate`, `desktop_credential_service` (source `None` = direct).
- `CredentialPublic.group_share_count` on owned rows.
