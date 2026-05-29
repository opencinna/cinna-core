# Credential Sharing - Technical Details

## File Locations

### Backend - Models
- `backend/app/models/credentials/credential_share.py` - CredentialShare table model, CredentialSharePublic, CredentialShareCreate, SharedCredentialPublic response models
- `backend/app/models/credentials/credential.py` - Adds `allow_sharing` and `allow_template_sharing` fields to CredentialBase, `template_private_fields` (JSON list[str]) to Credential / CredentialCreate / CredentialUpdate / CredentialPublic; `share_count`, `is_shared`, `owner_email` on CredentialPublic; `CredentialAffectedAgent` (id, name); `CredentialDeletionImpact` (tier, affected_own_agents, direct_share_count, bundle_pbp_usages, active_install_count)
- `backend/app/models/credentials/ai_credential.py` - `AICredentialBundleUsage` (bundle_uuid, bundle_id, display_name, publisher_install_id, used_for_conversation, used_for_building); `AICredentialDeletionImpact` (tier, bundle_usages)

### Backend - Services
- `backend/app/services/credentials/credential_share_service.py` - Core direct-sharing logic: share, revoke, toggle, access checks
- `backend/app/services/credentials/credentials_service.py` - `link_credential_to_agent()` accepts shared credentials; `update_credential()` flips `is_placeholder=False` only when `check_credential_completeness == "complete"` (load-bearing for the template setup flow)
- `backend/app/services/bundles/publish_service.py` - `_collect_credential_specs`, `_validate_publisher_provides`, `_template_payload_for` produce/validate `provided_by="template"` specs with `template_data` + `template_private_fields`
- `backend/app/services/bundles/install_service.py` - `_setup_install_credentials` template branch + `_materialise_template_credential` create the installer-owned placeholder pre-seeded with template defaults
- `backend/app/services/bundles/install_readiness_gate.py` - Detects template-materialised placeholders via `is_placeholder=True` and returns `placeholder_empty` until completion

### Backend - Routes
- `backend/app/api/routes/credential_shares.py` - Sharing CRUD endpoints (share, list, revoke, toggle, shared-with-me)
- `backend/app/api/routes/credentials.py` - `_credential_to_public()` exposes `allow_template_sharing` + `template_private_fields`; new `GET /credentials/{id}/bundles` endpoint with `provided_by` per usage
- `backend/app/api/routes/installs.py` - `PATCH /agents/{id}/publish-settings` accepts `provided_by="template"`; `GET /agents/{id}/setup-credentials` returns `template_private_fields` + `template_prefilled_data` per placeholder; `update_setup_credential` re-runs the gate

### Frontend - Components
- `frontend/src/components/Credentials/CredentialSharing.tsx` - Direct-sharing toggle, share dialog, shares list, "Used in Bundles" block filtered to `provided_by="publisher"` usages
- `frontend/src/components/Credentials/CredentialTemplateSharing.tsx` - "Share as Template" toggle, per-field private/template checkboxes (with form labels mirrored from `CredentialFields/`), force-private message for OAuth + service account, "Used in Bundles" block filtered to `provided_by="template"` usages
- `frontend/src/components/Credentials/SharedWithMeCredentials.tsx` - "Shared with Me" credential list with owner email and share date
- `frontend/src/components/Credentials/CredentialCard.tsx` - "Shareable" badge, user count badge for active shares
- `frontend/src/components/Agents/CredentialProvisioningSection.tsx` - Per-spec `User provides` / `Embedded (shared)` / `Template (defaults + private)` dropdown on the publisher install's Bundle tab
- `frontend/src/components/Install/InstallServiceCredentialItem.tsx` - Renders the spec's `provided_by` badge + body; `TemplateProvidedBody` lists the private fields the installer will need to fill in
- `frontend/src/components/Install/InstallSetupForm.tsx` - `initialChoiceForSpec` returns `"skip"` for template specs (the install service short-circuits into the template branch)

### Frontend - Routes
- `frontend/src/routes/_layout/credentials.tsx` - "My Credentials" and "Shared with Me" sections
- `frontend/src/routes/_layout/credential/$credentialId.tsx` - SharedCredentialView (read-only) vs OwnedCredentialView (full edit); owned view wraps `CredentialSharing` and `CredentialTemplateSharing` in a 2-column grid
- `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` - Setup page renders one card per placeholder; template placeholders show a read-only "pre-filled by publisher" panel and one input row per private field
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` - Fetches both owned and shared credentials, shows "Shared" indicator in dropdown

### Migrations
- `backend/app/alembic/versions/g7b8c9d0e1f2_add_credential_sharing.py` - `allow_sharing` column on credential table, new `credential_shares` table
- `backend/app/alembic/versions/cc3de4f5a6b7_add_template_sharing_to_credential.py` - `allow_template_sharing` (boolean, default `false`) and `template_private_fields` (JSON list, default `[]`) on credential table

### Router Registration
- `backend/app/api/main.py` - Added `credential_shares.router` import and registration

### Tests
- `backend/tests/api/agents/agents_bundles_template_sharing_test.py` - Eight scenario tests: CRUD persistence, publish spec shape, override validation, install materialisation, completion → ready, partial fill stays needs_setup, `use_existing` opt-out, re-publish guard when consent flag is revoked

## Database Schema

### Modified Table: `credential`
- `allow_sharing` (boolean, default false) — direct-sharing consent flag
- `allow_template_sharing` (boolean, default false) — template-sharing consent flag (independent from `allow_sharing`)
- `template_private_fields` (JSON, default `[]`) — list of `credential_data` field names that must be supplied per-installer; only stored, never inferred
- Index: `ix_credential_allow_sharing` (partial index for shareable credentials)

### New Table: `credential_shares`
- `id` (UUID, PK)
- `credential_id` (UUID, FK → credential, CASCADE delete)
- `shared_with_user_id` (UUID, FK → user, CASCADE delete)
- `shared_by_user_id` (UUID, FK → user)
- `shared_at` (datetime)
- `access_level` (varchar, default 'read')
- Unique constraint: `(credential_id, shared_with_user_id)`

### Bundle Revision Spec (no DB column — JSON inside `agent_bundle_revision.required_credential_specs`)

Each entry the publish flow emits:
- `name`, `type`, `description` — credential metadata
- `provided_by` — `"user"` | `"publisher"` | `"template"`
- `publisher_credential_id` — populated only when `provided_by="publisher"`
- `template_data` — non-private credential_data values; only present when `provided_by="template"`
- `template_private_fields` — list of fields the installer must supply; only present when `provided_by="template"`

## API Endpoints

### Credential Sharing (`backend/app/api/routes/credential_shares.py`)
- `POST /api/v1/credentials/{credential_id}/shares` - Share credential with user by email
- `GET /api/v1/credentials/{credential_id}/shares` - List all shares for a credential
- `DELETE /api/v1/credentials/{credential_id}/shares/{share_id}` - Revoke specific share
- `GET /api/v1/credentials/shared-with-me` - Get credentials shared with current user
- `PATCH /api/v1/credentials/{credential_id}/sharing` - Enable/disable sharing

### Updated Credential Endpoints (`backend/app/api/routes/credentials.py`)
- `GET /api/v1/credentials/{id}` - Allows viewing if user owns OR has share (returns `is_shared=true` for shared)
- `GET /api/v1/credentials/{id}/deletion-impact` - Returns a `CredentialDeletionImpact` classifying the blast radius of deleting this credential (Tier 0 / 1 / 2). Owner-only; returns 404 when the credential does not exist or the requester is not the owner (no existence leak).
- `DELETE /api/v1/credentials/{id}?force=` - Deletes the credential. Blocked with HTTP 409 at Tier 2 (PBP in a published bundle with active foreign installs) unless `force=true` is passed; Tier 0 and Tier 1 always proceed. The 409 body is the serialised `CredentialDeletionImpact` so the frontend can render affected bundles and offer force delete.
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
- `share_credential()` - Create share with validations (ownership, allow_sharing, target exists, not self, not duplicate)
- `revoke_credential_share()` - Delete share record with ownership check
- `get_shares_by_credential()` - List shares with resolved user emails
- `get_credentials_shared_with_me()` - Query shares where user is recipient
- `get_share_count_for_credential()` - Count shares for a credential
- `update_credential_sharing()` - Toggle allow_sharing; auto-revokes all shares when disabled
- `can_user_access_credential()` - Check if user owns OR has share
- `delete_all_shares_for_credential()` - Bulk delete for credential deletion

### CredentialsService (`backend/app/services/credentials/credentials_service.py`)
- `link_credential_to_agent()` - Allows linking shared credentials (not just owned)
- `update_credential()` - Persists `allow_template_sharing` + `template_private_fields`; flips `is_placeholder=False` only when `check_credential_completeness == "complete"` (so partial fills on template placeholders keep the gate engaged); rejects non-`list[str]` `template_private_fields` payloads
- `check_credential_completeness()` - Per-type required-field check the placeholder-flip relies on
- `get_deletion_impact(session, credential_id, requester_id)` - Classifies deletion blast radius into Tier 0 / 1 / 2. Owner-only: raises `ValueError("Credential not found")` for missing or non-owned rows (route maps to 404). Composes three signals: (1) own agents via `get_affected_agents`; (2) direct share count via `CredentialShareService.get_share_count_for_credential`; (3) PBP bundle usages filtered from `list_bundle_usages`. The `active_install_count` is scoped to foreign installs of the PBP bundle(s) only — direct-share linkers are excluded so they are not double-counted with `direct_share_count`. Tier 2 requires both PBP usage AND `active_install_count > 0`.
- `delete_credential(session, credential_id, owner_id, force=False)` - Raises `CredentialInUseError` (HTTP 409) at Tier 2 unless `force=True`. The error carries the full `CredentialDeletionImpact` so the route can serialise it as the 409 body without a second lookup.
- `list_bundle_usages(*, credential_id, requester_id)` - Owner-only listing of bundles whose publisher install links this credential. Resolves each entry's `provided_by` via `PublishService.resolve_provided_by` so the projection matches what the publish-time spec collector would emit. Raises `ValueError("Credential not found")` for missing or non-owned rows (route maps to 404 to avoid leaking existence). Backs `GET /credentials/{id}/bundles`

### AICredentialsService (`backend/app/services/credentials/ai_credentials_service.py`)
- `get_deletion_impact(session, credential_id, user_id)` - Tier 0 or Tier 2 for AI credentials. Tier 2 when any `AgentBundle` has `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id` pointing at this credential. Owner-only (404 for missing or not-owned) via `_get_owned_credential_or_404`.
- `delete_credential(session, credential_id, user_id, force=False)` - Raises `AICredentialInUseError` at Tier 2 unless `force=True`. On forced delete, PostgreSQL `ON DELETE SET NULL` nulls the bundle FKs, degrading affected bundles to "user provides".
- `_get_owned_credential_or_404(session, credential_id, user_id)` - Returns 404 (not 403) when the credential exists but is not owned by `user_id`, preventing existence leaks on owner-only deletion paths. The general `get_credential` still returns 403 for non-owners; only the deletion paths use this stricter helper.

### PublishService (`backend/app/services/bundles/publish_service.py`)
- `resolve_provided_by(credential, publisher_install)` - Public static method that is the single source of truth for `provided_by` resolution. Order: publisher override → `allow_sharing` → `allow_template_sharing` → `"user"`. Used by both publish-time spec emission and `CredentialsService.list_bundle_usages` so the two paths cannot disagree
- `_collect_credential_specs()` - Reads linked credentials and emits the per-spec shape; delegates `provided_by` to `resolve_provided_by`; for template specs attaches `template_data` + `template_private_fields` via `_template_payload_for`
- `_template_payload_for()` - Decrypts the credential, strips private fields, applies the per-type templatable allowlist (e.g. `ssh_key` → only `host_aliases`); raises `ValueError` on decrypt failure rather than silently shipping an empty payload
- `_validate_publisher_provides()` - Pre-publish guard: rejects publish when a `"publisher"` spec has no shareable backing credential or a `"template"` spec has `allow_template_sharing=False`
- `_TEMPLATE_FORCE_PRIVATE_TYPES` - Hard-strips `credential_data` from template payloads for OAuth + service account types regardless of UI state (defence in depth)
- `_TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE` - Per-type allowlist applied after the publisher's private-field filter

### InstallService (`backend/app/services/bundles/install_service.py`)
- `_setup_install_credentials()` - Walks `required_credential_specs`; for `provided_by="template"` calls `_materialise_template_credential` unless the installer opted in with `mode="use_existing"`
- `_materialise_template_credential()` - Creates a fresh `Credential` row owned by the installer with `encrypted_data` seeded from `spec["template_data"]`, `is_placeholder=True`, `allow_sharing=False`, `allow_template_sharing=False`, and `template_private_fields` mirrored from the spec; sets `last_update_status="degraded"` on materialisation fallback
- `_try_link_publisher_credential()` - Existing PBP path
- `update_publish_settings()` - Validates and persists partial updates to `install.publish_settings` (`credential_overrides` map and pre-publish AI credential draft); enforces publisher-install scope, that override keys reference linked credential names, that each `provided_by` is one of `"user"`/`"publisher"`/`"template"`, and that AI credential ids are owned by the install owner. Backs `PATCH /agents/{id}/publish-settings`
- `list_setup_credentials()` - Returns `SetupCredentialSummary` items for placeholder credentials linked to the install. For template-materialised rows, decrypts and surfaces non-private fields under `template_prefilled_data` (decryption failures fall back to `{}` so the row still surfaces). Backs `GET /agents/{id}/setup-credentials`

### InstallReadinessGate (`backend/app/services/bundles/install_readiness_gate.py`)
- `check()` / `_scan_service_credentials()` - Returns `placeholder_empty` for any installer-owned `is_placeholder=True` credential, including template-materialised rows; same pathway as PBU placeholders

## Frontend Components

### CredentialSharing (`frontend/src/components/Credentials/CredentialSharing.tsx`)
- Direct-sharing toggle in the CardHeader corner (matches `EmailIntegrationCard` / `WebappShareCard` pattern); body collapses when disabled
- "Share" button opens dialog with email input form
- List of current shares with revoke buttons
- Confirmation dialog when disabling sharing with active shares. The dialog also fetches `GET /credentials/{id}/deletion-impact` (shared query key `["credential-deletion-impact", id]`) when open; when the credential is PBP in published bundles, the dialog shows a destructive alert listing the affected bundles and install count so the publisher can see the blast radius before confirming.
- "Used in Bundles" block listing usages where `provided_by="publisher"`; each entry deep-links to `/agent/{publisher_install_id}#bundle`
- Early-returns `null` when `useRole().isAgentUser` is true, so the entire card is hidden from `agent-user` accounts

### DeleteCredential (`frontend/src/components/Credentials/DeleteCredential.tsx`)
- Service credential delete dialog used from the credential detail page and credential card dropdown menu
- Fetches `GET /credentials/{id}/deletion-impact` (query key `["credential-deletion-impact", id]`) when the dialog opens
- Tier 0: lists affected own agents by name if any
- Tier 1: shows a destructive alert "N users will lose access to this credential immediately"
- Tier 2: shows a destructive alert describing the broken installs, lists affected bundles with "Open" links to the publisher install's Bundle tab, and replaces the "Delete" button with "Force delete & break installs" (passes `force=true`)
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

### SharedWithMeCredentials (`frontend/src/components/Credentials/SharedWithMeCredentials.tsx`)
- Displays credentials shared with current user
- Shows owner email and share date
- Blue "Shared" badge to distinguish from owned credentials

### Credential Detail Route (`frontend/src/routes/_layout/credential/$credentialId.tsx`)
- Detects `is_shared` flag to switch between SharedCredentialView (read-only) and OwnedCredentialView (full edit)
- Owned view wraps `CredentialSharing` and `CredentialTemplateSharing` in a `grid grid-cols-1 lg:grid-cols-2 gap-6` container
- Header shows "Shared" badge and owner email for shared credentials
- Delete button hidden for shared credentials

### AgentCredentialsTab (`frontend/src/components/Agents/AgentCredentialsTab.tsx`)
- Fetches both owned credentials and credentials shared with user
- Shows "Shared" indicator in credential selection dropdown
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
- Revoking share immediately removes access
- Disabling sharing is destructive (revokes all shares with warning)

### Template Privacy Layers (defence in depth)
1. **Frontend filter** — only fields the publisher leaves unchecked are sent as `template_private_fields=[]` candidates
2. **Publisher private-fields filter** — `_template_payload_for` strips fields named in `template_private_fields` from the decrypted blob
3. **Per-type templatable allowlist** — `_TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE` (e.g. `ssh_key` → only `host_aliases`) drops anything outside the allowlist
4. **Force-private types** — `_TEMPLATE_FORCE_PRIVATE_TYPES` (OAuth + service account) zeros out `template_data` regardless of UI state; only credential `notes` ride through via `spec.description`
5. **Materialised row defaults** — `_materialise_template_credential` always writes `allow_sharing=False` and `allow_template_sharing=False` on the installer's row; downstream re-sharing requires an explicit toggle
