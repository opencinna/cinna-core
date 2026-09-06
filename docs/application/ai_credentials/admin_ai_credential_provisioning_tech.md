# Admin-Provisioned AI Credentials + Native Account-Config — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/managed_ai_credential.py` — `ManagedAICredential` (parent table); `ManagedAICredentialCreate`, `ManagedAICredentialUpdate`, `ManagedAICredentialPublic` (admin DTOs); `ManagedAICredentialMember`, `ManagedAICredentialReconcileResult`, `ManagedReconcileSkip`, `ManagedReconcileBlock`
- `backend/app/models/credentials/ai_credential.py` — `AICredential` (child link: `managed_credential_id` FK, ON DELETE SET NULL; existing `is_admin_managed` and `managed_by_id` columns unchanged); `AICredentialPublic` (`is_admin_managed` projection)
- `backend/app/models/external/account_config.py` — `AccountConfigProviderPublic`, `AccountConfigResponse` (native-config response models)

**Routes:**
- `backend/app/api/routes/admin_llm_providers.py` — parent-oriented `POST/GET/PATCH/DELETE /admin/llm-providers/`, `POST /admin/llm-providers/{id}/set-default`, `POST /admin/llm-providers/{id}/apply-to-existing`, `POST /admin/llm-providers/test-connection`; superuser-gated. `_conflict_409(exc)` maps `ManagedCredentialConflictError` to a structured `409`
- `backend/app/api/routes/external_account_config.py` — `GET /external/account-config`; native-token-gated
- `backend/app/api/main.py` — both routers registered (`admin_llm_providers.router`, `external_account_config.router`)

**Services:**
- `backend/app/services/credentials/managed_ai_credentials_service.py` — `ManagedAICredentialsService` (singleton: `managed_ai_credentials_service`); owns parent CRUD + reconcile + `add_members` + `apply_to_existing`; defines `ManagedCredentialConflictError` and the `MemberAddition` dataclass
- `backend/app/services/users/account_provisioning_service.py` — `AccountProvisioningService.on_account_created` / `provision_explicit` / `on_account_deactivated`, plus the shared `_guarded` net and `_provision` body; `ProvisioningReport`, `ProvisionedCredential`, `ProvisioningSkip` dataclasses; `EVENT_AUTO_PROVISION` / `EVENT_AUTO_PROVISION_FAILED` constants
- `backend/app/services/users/user_service.py` — `UserService.create_account`, the single place a `User` row is built; calls `on_account_created` after the commit. See [Auth — tech](../auth/auth_tech.md#the-account-creation-chokepoint)
- `backend/app/services/credentials/admin_ai_credentials_service.py` — `AdminAICredentialService` (legacy singleton: `admin_ai_credentials_service`); **no longer wired to any route**; retained but superseded
- `backend/app/services/external/external_account_config_service.py` — `ExternalAccountConfigService` (singleton: `external_account_config_service`)
- `backend/app/services/credentials/ai_credentials_service.py` — `update_credential` and `delete_credential` extended with `admin_override: bool = False` kwarg; `_to_public` projects `is_admin_managed`; `_clear_user_profile_for_type` used by `_clear_child_default`

**Migrations:**
- `backend/app/alembic/versions/d3782dd039a5_add_managed_ai_credential.py` — creates `managed_ai_credential` table; adds `ai_credential.managed_credential_id` FK (ON DELETE SET NULL); `down_revision = '2f2d8e49501d'`; schema-only (no data backfill)
- `backend/app/alembic/versions/2f2d8e49501d_add_admin_managed_ai_credential.py` — earlier migration that added `is_admin_managed` and `managed_by_id` to `ai_credential`
- `backend/app/alembic/versions/b71863b32aa1_add_auto_provision_to_managed_ai_.py` — adds `auto_provision_roles`, `model_override_conversation`, `model_override_building` to `managed_ai_credential`; `down_revision = '1d737d7ef0a0'`; schema-only. Autogenerate also proposed three `cli_device_login_request` timestamp alterations — unrelated pre-existing model-vs-DB drift, deliberately excluded (applying it would drop the timezone from live rows)

### Frontend

**Route:**
- `frontend/src/routes/_layout/admin/ai-credentials.tsx` — `AdminAiCredentials` page component; `beforeLoad` redirects unauthenticated users to `/login` and non-superusers to `/`; registered at `/admin/ai-credentials`; client-side pagination (10 per page); filter by `target_user_id` via `UserAllowlistPicker` toggle-panel
- `frontend/src/routes/_layout/admin/llm-providers.tsx` — reduced to a `beforeLoad` redirect stub → `/admin/ai-credentials` (UI rename, phase 4). Deliberately carries **no auth guards of its own**: the target route runs them, and duplicating them would be a second place to keep them in step

**Sidebar entry:**
- `frontend/src/components/Sidebar/AdminMenu.tsx` — "AI Credentials" item, `Sparkles` icon, linking to `/admin/ai-credentials`

**Components under `frontend/src/components/Admin/LlmProviders/`:**
- `ManagedCredentialDialog.tsx` — unified create + edit dialog. In `create` mode it provides its own trigger button and manages open state internally; in `edit` mode it is fully controlled by the actions menu. Provider type is immutable after creation (`disabled` on the Select). API key field is blank in edit mode (blank = keep stored key for all members). Member add/remove via `UserAllowlistPicker` pre-seeded from `record.members`. Test Connection probes via `POST /test-connection` (resolves stored parent key when `api_key` is blank and `record.has_api_key` is true). Reconcile result surfaced as per-user skip/blocked toasts + summary toast. **Phase 2:** an `openedWithRef` snapshot of the seeded form values, against which every PATCH field is diffed — only changed fields travel, because the payload is absolute and resubmitting an untouched field asserts a value the admin never chose (for the auto-provision fields that is how a rename comes back as a 409 for a slot conflict the admin did not introduce). `membershipDirty` does the same for `target_user_ids`. New controls: **Modes to wire** + per-mode **model override** inputs (with `ListModelsButton`) inside the `set_user_sdk_defaults` block, and an **Auto-provision for new users** role checkbox group with the inline 409 alert. The create-time "at least one target user" guard became "at least one target user *or* auto-provision role".
- `LlmProvidersTable.tsx` — renders `ManagedAICredentialPublic[]`; columns: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | Auto (`AutoProvisionCell` — role chips via `userRoleLabel`, or an outline "Off" badge when the list is empty) | Shared with (member chips: `full_name <email>` or `email`) | Created; no per-member default badge; member labels resolved inline from `record.members` (members carry their own `email`/`full_name` — no separate user-fetch needed)
- `LlmProviderActionsMenu.tsx` — three-dot menu per parent row: Edit (opens `ManagedCredentialDialog` in `edit` mode), Set default for all (calls `/set-default`), **Apply to existing users** (two separate mutations — `previewMutation` with `dryRun: true`, fired unconditionally on open, and `applyMutation` — rather than one with a flag, so the preview stays on screen while the commit is in flight), Delete (with two-stage `AlertDialog`: first confirm, then if `409` escalates to a force-delete confirmation listing blocked members by name)
- `providerTypes.ts` — `PROVIDER_TYPE_OPTIONS` array (Anthropic/OpenAI/OpenAI Compatible/Google — MiniMax omitted); `getProviderTypeLabel` helper; `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]` — **deliberately not renamed with the page**: it is a cache key, not a route path, and three surfaces (this page, the auto-provision matrix, the invite wizard) depend on producing the *same* string. Renaming it splits the cache silently — no error, just two lists that stop agreeing; `managedCredentialsQueryKey(targetUserId?)` factory. Phase 2 adds `SDK_MODE_OPTIONS` / `sdkModeLabel`, the `AutoProvisionConflict` interface, `parseAutoProvisionConflict(error)` (checks every field, not just `code`, so a future 409 shape cannot render a sentence containing `undefined`), `describeAutoProvisionConflict(conflict)` (recomposes the message with display labels), and `findAutoProvisionConflict(records, record, role)` — the advisory client-side mirror of the backend rule
- `frontend/src/components/Admin/AutoProvisionedCredentialsMatrix.tsx` — **new.** Credentials × roles checkbox matrix rendered at the foot of the Access Policy card's *New users* section. Reads `managedCredentialsQueryKey()` (the same key the AI Credentials page uses, `staleTime` 30s); each toggle is a `PATCH` carrying only `auto_provision_roles`; the whole grid disables while a toggle is in flight (a second click would be a second full reconcile and a second audit event for one intended change); the 409 renders as a destructive `Alert` under the table
- `frontend/src/utils/userRoles.ts` — **new.** `USER_ROLE_OPTIONS` (capability order: agent-user → agent-developer → admin) and `userRoleLabel(role)`. Takes a bare `string`, not `UserRoleValue`, so a server that learns a fourth role renders its identifier instead of crashing. `AccessPolicyCard`, `LlmProvidersTable`, `LlmProviderActionsMenu`, the dialog and the matrix all read from here
- `frontend/src/components/Common/ListModelsButton.tsx` — gains an optional `probeModels?: () => Promise<AICredentialTestResult>` prop. When given, `credentialId` / `credentialType` are unused and the caller gates the button with `disabled`. Needed because the admin dialog holds a *parent record on a different endpoint*, or a key typed into the form and never persisted — neither is an `AICredential` id. The dialog passes a probe that does **not** go through the shared Test Connection mutation, so opening the picker never repaints the Test Connection banner and the picker's own Retry cannot re-enter a pending mutation

**Generated client services used:**
- `AdminLlmProvidersService` — `createManagedAiCredential`, `listManagedAiCredentials`, `getManagedAiCredential`, `updateManagedAiCredential`, `deleteManagedAiCredential`, `setManagedAiCredentialDefault`, `applyManagedAiCredentialToExisting`, `testManagedAiCredentialConnection`

**Also implemented (admin-curated model list):**
- `ManagedCredentialDialog.tsx` — "Default model" text input (with a "View available models ↗" external link next to the label, pointing to the provider's official models docs — `PROVIDER_MODELS_DOC_URL` map; omitted for `openai_compatible`) + "Available models" multi-line textarea; **"Fill top 10 models"** button (replaces the old "Use models from test" label): auto-runs Test Connection if no fresh successful result is cached, then fills "Available models" with the top 10 discovered models and auto-sets "Default model" via `pickDefaultModel` (Google → `GOOGLE_DEFAULT_MODEL = "gemini-flash-latest"`; Anthropic → `pickHighestSonnet` highest version Sonnet from the list; OpenAI/OpenAI Compatible → first model); edit-mode seeding; `None` vs `[]` clear semantics on submit; `stripProviderPrefix` + `parseAvailableModels` client-side normalization for display
- `EnvironmentConfigForm.tsx` — model-override `<datalist>` prefers `available_models` over `discovered_models` when non-empty
- `AICredentials.tsx` — model-override `<datalist>` same preference; read-only "Default model: …" line rendered on admin-managed credential entries when `default_model` is set

**Still pending (not yet implemented):**
- Native-app (Cinna Desktop / Mobile) provider and chat-mode auto-creation driven by `GET /external/account-config`

---

## Database Schema Changes

### `managed_ai_credential` table (new — migration `d3782dd039a5`)

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | `UUID` | PK | Parent record identity |
| `name` | `VARCHAR(255)` | NOT NULL | Human-readable label |
| `type` | `VARCHAR(50)` | NOT NULL | `AICredentialType` enum value |
| `encrypted_data` | `TEXT` | NOT NULL | Fernet-encrypted JSON `{api_key, base_url?, model?}` — canonical key |
| `base_url` | `VARCHAR(500)` | nullable | Non-secret mirror for projection/UI |
| `model` | `VARCHAR(255)` | nullable | Non-secret mirror for projection/UI |
| `default_model` | `VARCHAR(255)` | nullable | Admin-curated default model (bare concrete id). Added by migration `c1a4b2d3e5f6`. |
| `available_models` | `JSON` | nullable | Admin-curated selectable model list. Added by migration `c1a4b2d3e5f6`. |
| `set_as_default` | `BOOLEAN` | NOT NULL, server_default false | Whether each child is set as its owner's default |
| `set_user_sdk_defaults` | `BOOLEAN` | NOT NULL, server_default false | Whether each owner's SDK-default pointers are wired |
| `sdk_default_modes` | `JSON` | NOT NULL, server_default `["conversation","building"]` | Modes to wire |
| `auto_provision_roles` | `JSON` | NOT NULL, server_default `'[]'::json` | Roles whose newly created accounts receive this credential. Added by migration `b71863b32aa1`. The empty backfill is the only safe one — a permissive default would hand a company key to the next person who signs up. |
| `model_override_conversation` | `VARCHAR(255)` | nullable | Model pinned on each member's `User.default_model_override_conversation` for the conversation mode. Added by `b71863b32aa1`. |
| `model_override_building` | `VARCHAR(255)` | nullable | Same for building. Added by `b71863b32aa1`. |
| `expiry_notification_date` | `TIMESTAMP` | nullable | Informational expiry reminder |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE SET NULL, index `ix_managed_ai_credential_managed_by` | Which admin owns/manages this record; NULL when the admin account is deleted |
| `created_at` | `TIMESTAMP` | NOT NULL | Creation time (UTC) |
| `updated_at` | `TIMESTAMP` | NOT NULL | Last update time (UTC) |

### `ai_credential` table — columns added across two migrations

**Migration `d3782dd039a5`:**

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `managed_credential_id` | `UUID` | nullable, FK → `managed_ai_credential.id` ON DELETE SET NULL, index `ix_ai_credential_managed_credential` | Structural link to the parent. NULL = not a managed child, or parent was deleted out-of-band |

**Migration `c1a4b2d3e5f6`:**

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `default_model` | `VARCHAR(255)` | nullable | Mirror of parent value, written through by reconcile. Read by SDK resolution + native config. NULL for self-created credentials. |
| `available_models` | `JSON` | nullable | Mirror of parent value, written through by reconcile. Read by model pickers + native config. NULL for self-created credentials. |

The `is_admin_managed` and `managed_by_id` columns already exist from migration `2f2d8e49501d`.

No data backfill in any migration — existing rows get `NULL` (preserves current behavior: catalog default + discovered list).

Downgrade for `d3782dd039a5`: drops FK `fk_ai_credential_managed_credential`, drops index `ix_ai_credential_managed_credential`, drops column `managed_credential_id`, drops index `ix_managed_ai_credential_managed_by`, drops table `managed_ai_credential`.

Downgrade for `c1a4b2d3e5f6`: drops `ai_credential.available_models`, `ai_credential.default_model`, `managed_ai_credential.available_models`, `managed_ai_credential.default_model` (in that order).

Downgrade for `b71863b32aa1`: drops `model_override_building`, `model_override_conversation`, `auto_provision_roles` (in that order).

---

## Parent DTOs

### `ManagedAICredentialCreate`

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | 1–255 chars |
| `type` | `AICredentialType` | Provider type; immutable after creation |
| `api_key` | `str` | Plaintext key (min length 1); encrypted into the parent row; written to children at add time |
| `base_url` | `str \| None` | Required for `openai_compatible`; optional for `google`; max 500 chars |
| `model` | `str \| None` | Required for `openai_compatible`; max 255 chars |
| `default_model` | `str \| None` | Admin-curated default model (bare concrete id, max 255); normalized server-side (strip `provider/` prefix, trim) |
| `available_models` | `list[str] \| None` | Admin-curated selectable model list; normalized server-side; `None` = no curation |
| `expiry_notification_date` | `datetime \| None` | Informational expiry reminder |
| `target_user_ids` | `list[uuid.UUID]` | **May be empty** (phase 2 dropped the `min_length=1`): an auto-provision-only credential legitimately starts with no members. Deduplicated preserving order |
| `set_as_default` | `bool` | Default `False` |
| `set_user_sdk_defaults` | `bool` | Default `False` |
| `sdk_default_modes` | `list[str]` | Default `["conversation", "building"]`. **Not validated** — see Known Gaps in the [business doc](admin_ai_credential_provisioning.md#known-gaps) |
| `auto_provision_roles` | `list[str]` | Default `[]`. Normalized + validated against `VALID_AUTO_PROVISION_ROLES` (`= tuple(VALID_USER_ROLES)`); an unknown entry is a `400` |
| `model_override_conversation` | `str \| None` | Max 255; normalized through `_normalize_default_model` (trim, strip `provider/`, blank → `None`) |
| `model_override_building` | `str \| None` | Same |

### `ManagedAICredentialUpdate`

All fields optional (partial update). Omitting `api_key` keeps the stored key. Omitting `target_user_ids` leaves membership unchanged. Omitting `available_models` leaves the stored curation unchanged; sending `[]` clears it.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str \| None` | |
| `api_key` | `str \| None` | Non-None triggers key rotation + Update pass for all current members |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | `None` = no change; blank string normalized to `None` (effectively clears). Normalized server-side. |
| `available_models` | `list[str] \| None` | `None` = no change; `[]` = explicit clear (fall back to `discovered_models`). Normalized server-side. |
| `expiry_notification_date` | `datetime \| None` | |
| `target_user_ids` | `list[uuid.UUID] \| None` | When supplied, the reconcile diff is against this list |
| `set_as_default` | `bool \| None` | |
| `set_user_sdk_defaults` | `bool \| None` | |
| `sdk_default_modes` | `list[str] \| None` | |
| `auto_provision_roles` | `list[str] \| None` | `None` = no change; `[]` = stop auto-provisioning (**not** a revoke — existing members keep their credential) |
| `model_override_conversation` | `str \| None` | Three-valued: omitted/`null` = unchanged; `""` = clear to NULL **and** retract the pin from members still carrying the dropped value; a model id = set + write through. This is why the field is not `str \| None` with `min_length=1` — the empty string is meaningful here, not malformed |
| `model_override_building` | `str \| None` | Same |

### `ManagedAICredentialPublic`

Never includes `encrypted_data` or key material.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `uuid.UUID` | |
| `name` | `str` | |
| `type` | `AICredentialType` | |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | Admin-curated default model (bare concrete id) |
| `available_models` | `list[str] \| None` | Admin-curated selectable model list |
| `set_as_default` | `bool` | |
| `set_user_sdk_defaults` | `bool` | |
| `sdk_default_modes` | `list[str]` | |
| `auto_provision_roles` | `list[str]` | Roles whose new accounts receive this credential |
| `model_override_conversation` | `str \| None` | |
| `model_override_building` | `str \| None` | |
| `expiry_notification_date` | `datetime \| None` | |
| `managed_by_id` | `uuid.UUID \| None` | Which admin manages this; NULL when that admin was deleted |
| `has_api_key` | `bool` | Always `True` — a parent always holds a key |
| `is_oauth_token` | `bool` | Derived from the stored key prefix (`sk-ant-oat`) for Anthropic; `False` for all other types |
| `members` | `list[ManagedAICredentialMember]` | One entry per current child credential |
| `member_count` | `int` | `len(members)` |
| `created_at` | `datetime` | |
| `updated_at` | `datetime` | |

### `ManagedAICredentialMember`

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | Owner of the child credential |
| `email` | `str` | Owner's email |
| `full_name` | `str \| None` | Owner's full name |
| `child_credential_id` | `uuid.UUID` | The `AICredential.id` |
| `is_default` | `bool` | Whether this child is the owner's default for its type |

### `ManagedAICredentialReconcileResult`

| Field | Type | Notes |
|-------|------|-------|
| `record` | `ManagedAICredentialPublic` | The parent record as it stands after reconcile |
| `added` | `list[ManagedAICredentialMember]` | Newly created children |
| `removed` | `list[uuid.UUID]` | Owner IDs whose children were successfully deleted |
| `updated` | `list[ManagedAICredentialMember]` | Members whose child was actually mutated this reconcile (empty on no-op) |
| `updated_count` | `int` | `len(updated)` — convenience scalar |
| `skipped` | `list[ManagedReconcileSkip]` | Users skipped (unknown/inactive/provision_failed/update_failed) |
| `blocked` | `list[ManagedReconcileBlock]` | Members whose removal was blocked by Tier-2 blast-radius (in_use_bundle) |

### `ManagedReconcileSkip`

| Field | Type | Values |
|-------|------|--------|
| `user_id` | `uuid.UUID` | The skipped target |
| `reason` | `str` | `"user_not_found"` / `"user_inactive"` / `"provision_failed"` / `"update_failed"` |

### `ManagedReconcileBlock`

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | The blocked member |
| `reason` | `str` | `"in_use_bundle"` / `"remove_failed"` |
| `impact` | `dict \| None` | Deletion-impact payload from `AICredentialInUseError.impact` |

### `ManagedAICredentialApplyCandidate`

A user who *would* receive the credential on apply-to-existing. Deliberately **not** a `ManagedAICredentialMember`: a member is identified by the child credential it owns, and on a dry run no child exists — inventing a placeholder id would be a lie the frontend could not distinguish from a real member.

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | |
| `email` | `str` | |
| `full_name` | `str \| None` | |
| `role` | `str` | The role that matched `auto_provision_roles` |

### `ManagedAICredentialApplyResult`

Subclasses `ManagedAICredentialReconcileResult` (so `record` / `added` / `skipped` mean exactly what they do elsewhere) and adds:

| Field | Type | Notes |
|-------|------|-------|
| `dry_run` | `bool` | `True` when nothing was written |
| `candidate_count` | `int` | Populated on **both** paths so the confirm dialog and the result toast quote the same number |
| `candidates` | `list[ManagedAICredentialApplyCandidate]` | Populated on a dry run only; empty on a real run |
| `defaults_overwrite_count` | `int` | Candidates who lose *something* to this grant, counted **once** per candidate however many slots they lose. Covers **both** default axes: `set_user_sdk_defaults` (a claimed mode whose credential pointer **or** model override is non-NULL) **and** `set_as_default` (the candidate already holds a default `AICredential` of the parent's type). `0` only when the record wires no defaults at all — **not** whenever `set_user_sdk_defaults` is false |

### `MemberAddition` (dataclass, service-internal)

What `add_members` returns: `added: list[ManagedAICredentialMember]` + `skipped: list[ManagedReconcileSkip]`. Deliberately **not** a `ManagedAICredentialReconcileResult` — that shape carries `removed`/`blocked`/`updated`, which an add-only operation can never populate, and a `record` projection all three callers throw away. Building it would have put `_to_public`'s per-member user lookup and its parent-key decrypt on the signup request path, for a value nobody reads.

### `ManagedCredentialConflictError` (exception)

Raised by `_validate_auto_provision_uniqueness`. Carries `conflicting_name`, `conflicting_id`, `role`, `mode`; `_conflict_409` in the route turns it into:

```json
{"code": "auto_provision_conflict", "message": "…",
 "conflicting_credential_id": "…", "conflicting_credential_name": "…",
 "role": "agent-developer", "mode": "building"}
```

Structured rather than prose because the frontend highlights the offending role/mode cell and links to the other record, and it cannot do either from a sentence.

---

## API Endpoints

### Admin LLM Providers (`/api/v1/admin/llm-providers`)

**File:** `backend/app/api/routes/admin_llm_providers.py`
**Auth gate:** `get_current_active_superuser` — `403` for anyone else

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `POST` | `/admin/llm-providers/` | `ManagedAICredentialCreate` | `ManagedAICredentialReconcileResult` | Create parent + initial reconcile; bad per-type payload → `400` |
| `GET` | `/admin/llm-providers/` | query `?managed_by_id=` & `?target_user_id=` (both optional) | `list[ManagedAICredentialPublic]` | Fleet-wide, ordered by `created_at DESC`; filtered when params supplied |
| `GET` | `/admin/llm-providers/{id}` | — | `ManagedAICredentialPublic` | `404` if not found |
| `PATCH` | `/admin/llm-providers/{id}?force=` | `ManagedAICredentialUpdate` | `ManagedAICredentialReconcileResult` | Update parent + re-reconcile; `force` overrides Tier-2 block on removed members |
| `DELETE` | `/admin/llm-providers/{id}?force=` | — | `Message` | `409` with `blocked` list when any child is in use and `force` is absent |
| `POST` | `/admin/llm-providers/{id}/set-default` | — | `ManagedAICredentialPublic` | Sets every current member's child as their default; stamps `set_as_default=True` on parent |
| `POST` | `/admin/llm-providers/{id}/apply-to-existing?dry_run=` | — | `ManagedAICredentialApplyResult` | Add-only grant to every active account whose role is in `auto_provision_roles`, minus current members. `dry_run=true` writes nothing and emits no audit events |
| `POST` | `/admin/llm-providers/test-connection?managed_credential_id=` | `AICredentialTestRequest` | `AICredentialTestResult` | When `api_key` blank and `managed_credential_id` given, probes via stored parent key |

`POST /` and `PATCH /{id}` additionally answer `409` with the `auto_provision_conflict` body above when the request **newly** claims a `(role, mode)` default slot another record already owns.

### Native Account-Config (`/api/v1/external/account-config`)

**File:** `backend/app/api/routes/external_account_config.py`
**Auth gate:** `CurrentUser` (standard JWT) + `client_kind in {"desktop", "mobile"}` (native gate)

| Method | Path | Auth gate | Response | Notes |
|--------|------|-----------|----------|-------|
| `GET` | `/external/account-config` | native JWT only | `AccountConfigResponse` | `403` for web JWTs; `401` for revoked desktop clients; `Cache-Control: no-store`; high-severity audit event |

**Status codes:**
- `200` — authenticated native client (providers list may be empty)
- `401` — unauthenticated, or revoked desktop/mobile client (via `get_current_user` revocation check)
- `403` — valid JWT but `client_kind` is absent or not in `{"desktop", "mobile"}`

---

## `ManagedAICredentialsService` (`managed_ai_credentials_service.py`)

Singleton: `managed_ai_credentials_service`

### Key public methods

| Method | Description |
|--------|-------------|
| `create(session, admin, data)` | Validate + encrypt canonical key; INSERT parent; call `reconcile(apply_fields=False, key_rotated=False)`. Returns `ManagedAICredentialReconcileResult`. |
| `update(session, admin, id, data, force)` | Apply scalar changes to parent (rotate `encrypted_data` if `api_key` supplied); call `reconcile(apply_fields=True, key_rotated=...)`. Omitting `target_user_ids` uses the current membership as desired. |
| `delete(session, admin, id, force)` | Reconcile to empty desired set (Tier-2 gated); if blocked and not `force`, abort (parent stays); else `DELETE` parent row. |
| `set_default_all(session, admin, id)` | `set_default` for every current member; stamp `parent.set_as_default=True`. Returns `ManagedAICredentialPublic`. |
| `list(session, admin, managed_by_id, target_user_id)` | Fleet-wide list, optional filters. |
| `get(session, admin, id)` | Single parent record; `404` if not found. |
| `resolve_test_key(session, id)` | Decrypt parent key for the Test Connection blank-api_key case. `404` if not found. |
| `add_members(session, *, parent, user_ids, actor)` | The Add pass on its own → `MemberAddition`. Idempotent (existing members are neither re-added nor reported). `actor` is **keyword-only with no default**: `None` means a system-initiated grant, and a route reaching it would be writing an unattributed grant, so "who did this" must be a decision at every call site. Does not affect what is written to the child — children are stamped with the parent's managing admin either way. Three callers: `reconcile`, `apply_to_existing`, `AccountProvisioningService`. |
| `apply_to_existing(session, admin, id, *, dry_run)` | → `ManagedAICredentialApplyResult`. Desired set = active users with `role ∈ auto_provision_roles`, minus current members. `dry_run` returns candidates and writes nothing. |

### `reconcile()` — the heart

```
reconcile(
    session, admin, parent, desired_user_ids,
    *, apply_fields, force, key_rotated, cleared_overrides=None
) -> ManagedAICredentialReconcileResult
```

- `desired_user_ids` — deduplicated; compared against `_current_members(parent)` (children keyed by `owner_id`)
- `apply_fields=False` — skip the Update pass (used on create, since there are no pre-existing members to update)
- `key_rotated=True` — forces key write-through in the Update pass even when other scalars are unchanged
- `force=True` — passes `force` to `delete_credential`; blocked members do not block the Remove pass
- `cleared_overrides` — `mode → the model override this request dropped`. Only `update()` can know it: once the parent row is written, a stored `None` cannot say whether it was never set or has just been retracted, so the *request* carries the transition down
- The Add pass is **delegated to `add_members`**, not duplicated, so a change to how a member is created cannot land in one of three places
- The Remove pass captures `_modes_pointing_at(owner, child.id)` **before** the delete and calls `_release_model_overrides` after it — afterwards the pointer is already NULL (`ondelete="SET NULL"`) and which slots the child owned is unrecoverable

### Session repair on per-member failures (`add_members`, both reconcile passes, `_add_child`)

Four handlers in this service catch a per-member failure, record it, and carry on. Every one of them calls `restore_session(session)` (`backend/app/utils.py`) **first**, then logs from pre-snapshotted locals, then appends its entry:

| Handler | Records | Why the rollback is required |
|---------|---------|------------------------------|
| `add_members` per-user `except` | `skipped(reason="provision_failed")` | A statement-level failure leaves the transaction aborted; the log call is itself a query, so a handler that logs first throws from inside itself and the exception escapes every net written to catch it |
| reconcile **Remove** pass `except` (after the `AICredentialInUseError` arm) | `blocked(reason="remove_failed")` | `reconcile` ends at `_to_public`, which queries. Recording a block and continuing on an aborted transaction turned a per-member problem into a `500` out of `PATCH /admin/llm-providers/{id}` |
| reconcile **Update** pass `except` | `skipped(reason="update_failed")` | Same |
| `_add_child` post-commit wiring `except` | Nothing — the member is **retained** | `_stamp_child` has already committed the child. Without the rollback the exception escaped into `add_members`, which reported the user `provision_failed` while their child row sat committed and they were a member by every definition this service uses — and on the auto-provision path wrote an `auto_provision_failed` security event into the feed of someone who *had* received the credential |

`HTTPException` is re-raised in each of these, unrolled-back: type-validation errors are the caller's problem, not a per-member skip.

Rolling back is safe in all four: the child is committed by `_stamp_child`, and `set_default`, `_apply_sdk_defaults`, `delete_credential`, `_release_model_overrides` and `_update_child_fields` each commit their own work, so nothing pending belongs to anyone but the failed attempt.

### `_update_child_fields()` — Update pass detail

Diffs parent scalars against the child (and the child's decrypted data). Only writes when something changed (idempotency). Returns `True` iff the child was mutated.

Clear-through limitation: `update_credential` treats `None` as "leave unchanged", so `base_url`/`model` cannot be cleared back to `None` (for `openai_compatible` both are required anyway). `expiry_notification_date` is cleared directly on the child row when the parent value is `None`.

Admin-curated model metadata write-through: `default_model` and `available_models` are written directly on the child row (bypassing `update_credential` — they are non-secret plain columns, not part of `AICredentialData`). Idempotent: the child is only mutated and flagged as `updated` when the stored values differ from the parent's. `available_models` differentiates `None` (no change) from `[]` (explicit clear) by comparing the exact stored list values — the parent itself carries `None` vs `[]` as the source of truth.

Default flag logic:
- `set_as_default=True` and `child.is_default=False` → `set_default(child, owner)`
- `set_as_default=False` and `child.is_default=True` → `_clear_child_default(child)` (clears profile blob + SDK-default pointers that reference this child)

### `_stamp_child()` — curated model metadata write-through on create

After `create_credential` creates the child, `_stamp_child` also writes `default_model` and `available_models` from the parent directly onto the child row (same pattern as the structural markers). This ensures newly-added members inherit the current curation immediately without a separate reconcile.

### Auto-provision uniqueness (`_claimed_slots` / `_validate_auto_provision_uniqueness`)

`_claimed_slots(roles, modes, set_user_sdk_defaults) -> set[tuple[str, str]]` — the `(role, mode)` default slots a configuration lays claim to. Empty unless the record does all three things: auto-provisions to a role, wires SDK defaults, and claims a mode. Modes outside `{"conversation", "building"}` are filtered out, because a mode that wires nothing cannot be fought over (which is also the only reason an unvalidated `sdk_default_modes` typo does not produce a phantom collision).

```
_validate_auto_provision_uniqueness(
    session, parent_id, roles, modes, set_user_sdk_defaults,
    *, previously_claimed=None
) -> None                      # raises ManagedCredentialConflictError
```

- **Scoped to the transition, not the state.** Only `claimed - previously_claimed` can raise. Renaming a record already sitting in a conflicting configuration must not 409 on a collision the admin did not introduce.
- Validating the effective *end state* reads as safer and is not: it makes a stale absolute payload indistinguishable from a deliberate edit, so a client rebuilding its request from an open-time snapshot gets refused for someone else's change — and a validator taught to tolerate that would also have masked the membership clobber `target_user_ids` had.
- `parent_id` is the record being written (excluded from the search), or `None` on create, where `previously_claimed` is empty and every slot is new.
- `create()` validates **before** the row is inserted, so a conflict never leaves a half-configured parent behind.
- `update()` reads `previously_claimed` and `previous_overrides` off the parent **before writing a single field** — both are transitions, and neither is recoverable once the new values are in. It then validates against the *effective* values (submitted where given, stored otherwise), so a PATCH that introduces a conflict cannot half-apply.

### Model-override write paths

Three functions write `User.default_model_override_<mode>`, and the differences between them are the contract:

| Function | Runs when | Behaviour |
|---|---|---|
| `_apply_sdk_defaults` | A member is **added** (from `_add_child` only) — the credential pointer is moving onto a different credential | Writes the override **unconditionally** for every mode the parent claims, including back to `NULL` when the parent has none. A reset, not a wipe: whatever sat in the slot described another credential and may name a model this provider does not serve |
| `_sync_model_overrides` | **Every** update against a slot the child already occupies | Writes only while the pointer still names this child, and only when the parent has an opinion. A stored `None` means "no opinion", not "clear theirs". `cleared_overrides[mode]` is the third case: retract the member's pin, but **only when it still equals the dropped value** — a member who picked their own model keeps it. Returns `True` iff the owner row was written |
| `_clear_child_default` / `_release_model_overrides` | The child's default is cleared, or the child is deleted | Tear the override down with the pointer it belongs to. `_clear_child_default` clears pointer + `default_sdk_<mode>` + override; `_release_model_overrides` (the reconcile Remove path, and therefore `DELETE /{id}`) clears the override only — the two knowingly disagree about `default_sdk_<mode>`, see Known Gaps |

Asymmetry worth restating: *setting* an override overwrites a model the member picked; *clearing* one only retracts this record's own value. So a member's own pick, once overwritten by a set, is not restored by the later clear. A mode dropped from `sdk_default_modes` is not visited at all — neither its override nor its pointer is torn down.

Shared constants: `_MODE_POINTER_ATTR` and `_MODE_OVERRIDE_ATTR` map `mode → User` attribute name; membership of these dicts is also the validity test for a mode string coming off the JSON column. `_modes_pointing_at(owner, child_id)` is the one place "does this slot belong to this child" is answered.

### `_count_default_overwrites(session, parent, candidates)`

Backs `defaults_overwrite_count` so the confirm dialog can say what the action costs, not only what it gives. Counts a candidate **once** however many things they lose — the admin's question is "how many people does this disturb", not "how many columns move".

It must look at **both** of the axes `_add_child` writes, because they are independent flags applied independently:

| Axis | What is counted | Why |
|------|-----------------|-----|
| `set_user_sdk_defaults` | For each claimed mode, `default_ai_credential_<mode>_id` **or** `default_model_override_<mode>` is non-NULL | `_apply_sdk_defaults` resets the slot wholesale on a claim, so a member with no pointer but a model they picked for that mode still loses something. Skipped entirely when `_TYPE_TO_SDK_ENGINE` has no entry for the parent's type — `_apply_sdk_defaults` returns immediately for such a type, so counting it would promise a change that will not happen |
| `set_as_default` | The candidate owns an `AICredential` of the parent's type with `is_default=True` | `ai_credentials_service.set_default` unsets whatever the owner's current default of that type is and rewrites the legacy per-type profile blob. Mirrors that service's own unset query |

The `set_as_default` axis is resolved by a **single `in_()` query** over the whole candidate list, not one query per candidate — this runs inside a dry run the admin is waiting on. The SDK axis is evaluated from the already-loaded `User` rows.

Returns `0` only when the record wires no defaults at all — the genuinely harmless configuration, and the only one the dialog is entitled to describe as free.

**The bug this shape exists to prevent.** The first version opened with `if not parent.set_user_sdk_defaults: return 0`. A record whose only default-writing flag was `set_as_default` therefore previewed as `candidate_count=N, defaults_overwrite_count=0`, the dialog's cost paragraph was gated on the same flag and said nothing at all, and confirming it silently stripped every candidate's own default credential.

**Not counted, on purpose: `default_sdk_<mode>`.** The engine string is never NULL, so including it would make the count equal `candidate_count` for every record that claims a mode. This is why the zero-copy in the dialog is worded as "nobody has picked a credential or a model for these slots yet" rather than the broader "no existing choice is replaced" — an OpenAI record claiming the conversation slot does move everyone's engine, and the broader sentence would be an overclaim. Do not widen it.

**Distinct from the `(role, mode)` uniqueness rule.** `_claimed_slots` / `_validate_auto_provision_uniqueness` still ignore `set_as_default` (Known Gap 5, accepted debt): two records may both claim to be their members' default-for-type and neither `409`s. That the *preview* counts the axis does not change what the *validator* refuses. The two questions are separate — the admin is told what the second record costs, and then allowed to confirm it — and collapsing them into one is the same reasoning error the counter's original bug was made of.

### `_normalize_auto_provision_roles(value)`

`None` passes through as "no change". Otherwise trimmed, de-duplicated order-preservingly, and checked against `VALID_AUTO_PROVISION_ROLES` — an unknown role raises `400` rather than being silently dropped, because a mistyped role would otherwise save successfully and then do nothing at the next signup with no clue why. There is deliberately **no** counterpart for `sdk_default_modes`.

### `_TYPE_TO_SDK_ENGINE` map

| `AICredentialType` | Composed SDK engine |
|-------------------|---------------------|
| `ANTHROPIC` | `"claude-code/anthropic"` |
| `MINIMAX` | `"claude-code/minimax"` |
| `OPENAI` | `"opencode/openai"` |
| `GOOGLE` | `"opencode/google"` |
| `OPENAI_COMPATIBLE` | `"opencode/openai_compatible"` |

---

---

## `AccountProvisioningService` (`services/users/account_provisioning_service.py`)

Static-method service. `on_account_created` is called by `UserService.create_account` after the account row is committed; `provision_explicit` is called by `InvitationService.invite`, after the same chokepoint has committed the row with `skip_auto_provision=True`.

| Method | Description |
|--------|-------------|
| `on_account_created(session, user, origin) -> ProvisioningReport` | Grants every managed credential whose `auto_provision_roles` contains `user.role`. **Never raises**, unconditionally — including when handed a session whose transaction is already aborted. Returns a value, never an error; its production caller ignores it, which is what lets tests assert on failures without the production path branching on one |
| `provision_explicit(session, user, origin, *, managed_credential_ids, actor) -> ProvisioningReport` | The invitation wizard's entry point, and the only sanctioned way an explicit list is granted at account creation. Same net, same body, same report as the automatic path — `_guarded` and `_provision` are shared, so the two cannot drift. `managed_credential_ids=None` selects the automatic set by the same predicate; `[]` grants nothing. `actor` is the acting superuser and is **required** (keyword-only, no default), and reaches both `add_members` and `details.actor` |
| `_guarded(session, user, origin, run, *, label)` | The outer never-fail net, factored out so the two entry points cannot each forget `_restore_session` or log `user.id` before the repair. `label` (`"automatic provisioning"` / the invite path's own) is required and keyword-only for the same reason `add_members`' `actor` is: with a default, the invite path would silently report itself as automatic provisioning and an admin debugging a failed invitation would grep for a string that is never written |
| `_provision(session, user, origin, *, parent_ids, actor, label)` | The shared body. `parent_ids is None` selects the automatic set; a list selects exactly those records, deduplicated, with any id that no longer names a record reported as a `managed_credential_not_found` skip rather than dropped. Everything after the selection — the per-parent guard, the skip reporting, the audit events — is identical for both entry points. The inactive short-circuit runs *before* the selection and is the one place they differ: it reports one `user_inactive` skip per **requested** credential, which only the explicit path can name |
| `on_account_deactivated(session, user) -> None` | Intentional no-op in phase 2, wired and named now. Shared managed credentials are one key held by many people, so deactivating one holder must not revoke it — there is nothing to undo. Phase 5 (per-user minting) is where a deactivated user's own key gains a provider-side revoke |

Result dataclasses: `ProvisioningReport(added, skipped)`, `ProvisionedCredential(managed_credential_id, child_credential_id)`, `ProvisioningSkip(managed_credential_id, reason)`. `reason` is a stable machine string — the reconcile skip reasons (`user_not_found`, `user_inactive`, `provision_failed`), `add_members_failed` when the call itself raised, or `managed_credential_not_found`, which only the explicit-list path can produce and is therefore the one most likely to be missing from a hand-written frontend map.

The invite route projects this report into its own narrow `InviteProvisioningSummary(added_count, skipped: list[InviteProvisioningSkip], provisioning_failed)` rather than returning `ManagedAICredentialReconcileResult`: that shape carries `removed`/`blocked`/`updated`, which an add-only grant can never populate, and a `record` projection whose construction costs a per-member user lookup and a key decrypt.

`provisioning_failed` carries `ProvisioningReport.failed`, which `_guarded` sets when the outer net fires. Without it a total failure — a raise in `_provision`'s prologue, before any credential has been selected — returns a report that is byte-identical to a deliberate grant of nothing (empty `added`, empty `skipped`), and the wizard renders both as "No AI credentials were granted". It is deliberately not a skip entry: a skip names a credential, and this failure can happen before any credential has been named. The two "nothing to do" returns inside `_provision` (an inactive account, an empty explicit id list) stay `failed=False` — they are outcomes, not failures. The inactive one is nevertheless not *empty*: it returns one `user_inactive` skip per requested credential, for the same reason `failed` exists, since an admin who ticked three credentials and invited a deactivated account would otherwise see exactly the screen of an admin who ticked none. That state is now reachable — re-inviting an account an administrator deliberately deactivated no longer reactivates it (`InviteUserRequest.is_active` is `bool | None`, and `None` means the submission did not state one). No security event is written for these skips: a medium-severity row per credential, in the feed of an account that has done nothing, is noise about an outcome that was never in doubt. The wizard renders them as one amber line, not N identical skip lines.

**Two error-handling rules, both non-obvious enough to state:**

- **Repair the session before touching it.** A failed attempt can leave the transaction aborted, and in that state *every* session operation raises — including the lazy attribute load behind an innocent-looking `logger.warning("… %s", user.id)`. So identifiers are snapshotted into locals while the session is known good, `_restore_session` runs first, and only then does anything get logged. The first parent in the loop hides this (`user` is still fresh from `create_account`'s refresh); it is the second, after a child credential has committed and expired everything, that bites.
- **Roll back unconditionally.** `AccountProvisioningService._restore_session` is a thin wrapper over the shared `restore_session(session)` in `backend/app/utils.py` (shared with `ManagedAICredentialsService`, the other end of this same path); it deliberately does **not** guard on `session.is_active`. That predicate detects only half the problem: a *flush* failure deactivates the `SessionTransaction`, but a *statement* failure (a `select` that errors, a lock timeout, a serialization failure, a dropped connection) leaves Postgres' transaction aborted while `is_active` stays `True` — so the guard would skip exactly the case that needs the rollback, and the caller's next commit (`register_user` commits again to send its confirmation email) dies with "current transaction is aborted". The rollback is safe by construction: the account row is committed before the call and `add_members` commits each child individually, so anything still pending belongs to the failed attempt. The helper lives in `app.utils` rather than on either service precisely because it is a pure session-lifecycle concern with no domain knowledge, and two copies is how one of them ends up guarded on `is_active` again. Whether rolling back is *safe* is left to each caller — `restore_session` never decides that, and never raises.

Other structural choices: the outer `try` in `on_account_created` wraps the prologue (deferred import, parents query, role filter) that the per-parent guards do not cover; `user_id` is pre-bound to `None` and snapshotted *inside* the `try` because reading `user.id` is itself a session operation. Parents are filtered in Python (the table holds a handful of rows, and a portable JSON-containment predicate over a `json` — not `jsonb` — column is more machinery than the saving is worth), with an `isinstance(…, list)` guard so a hand-edited non-list row cannot raise on the signup path. Inactive accounts return an empty report before any parent is looked at — no children, no skips, no medium-severity events in the feed of an account nobody can sign into.

Security events are constructed and committed **directly** here rather than through `SecurityEventService.create_event`: that method is `async` (its body awaits nothing, but the signature is), and this path is synchronous and called from inside both sync and async routes, so there is no loop to schedule it on. `_emit` is best-effort and never raises — an audit row that cannot be written must not break an account creation the caller was told could not fail.

---

## Admin-Curated Model Normalization (`_normalize_default_model` / `_normalize_available_models`)

Both helpers live on `ManagedAICredentialsService` and are called at `create()` and `update()` time before the values are stored on the parent.

**`_normalize_default_model(value)`**
- Strips any `provider/` prefix via `_strip_provider_prefix` from `model_catalog.py`.
- Trims whitespace; caps at 255 characters.
- Blank or all-whitespace input returns `None`.

**`_normalize_available_models(value)`**
- `None` input returns `None` unchanged (distinguishes "no change" from an explicit empty list).
- A list is processed entry-by-entry: strip `provider/` prefix, trim, drop blanks and duplicates (order-preserving), cap each entry at 255 characters.
- Caps the output list at 100 entries.
- An all-blank input list returns `[]` (explicit clear).

These caps bound payload size and prevent prefix-collision bugs in the OpenCode config builder.

---

## SDK / Environment Resolution (`environment_lifecycle.py`, `sdk_constants.py`)

### Per-mode credential-default bag carriers

`make_empty_credential_bag()` (in `sdk_constants.py`) includes two extra keys:

```python
"model_default_conversation": None,
"model_default_building": None,
```

These are filled during `_update_environment_config` in `environment_lifecycle.py` by `_set_mode_default_model_in_bag(bag, mode, cred_row)`, which reads `cred_row.default_model` and writes it to the appropriate bag key when non-empty.

**Resolution sites:** `_resolve_assigned_credential_into_bag` (for credentials explicitly linked to the environment's `conversation_ai_credential_id` / `building_ai_credential_id`) and `_fallback_fill_bag_for_sdk` (for type-level-default fallbacks). Both sites call `_set_mode_default_model_in_bag` after resolving the credential row, so every code path that fills the bag also carries the credential's `default_model`.

**Important note (create vs reconfigure):** `environment_service.create_environment` builds its bag for API-key extraction only; it does NOT populate the `model_default_*` carriers. The credential `default_model` is re-resolved inside `_update_environment_config` (which all lifecycle paths — create, reconfigure, rebuild — go through), so the net effect is correct.

### Override injection (`_generate_env_file`, `_generate_opencode_config_files`)

Both file-generation methods receive `model_default_conversation` and `model_default_building` as parameters. For each mode, the inner `_resolve_mode_model` function applies:

```python
credential_default = model_default_building if mode == "building" else model_default_conversation
override = env_override or credential_default
return resolve_model(engine, provider, mode, override, ...)
```

This means:
- If the environment has an explicit `model_override_building` / `model_override_conversation`, that wins.
- If not, the credential's `default_model` is injected as the override into `resolve_model`.
- `resolve_model`'s contract is unchanged; the credential default is just a source for the `override` argument.

**For Claude Code:** the resolved model is written to `MODEL_BUILDING` / `MODEL_CONVERSATION` env vars (picked up by the `claude_code_sdk_adapter` as `options.model`).

**For OpenCode:** the resolved model is written to the `opencode.json` `model` field in `_build_config`.

---

## Model Health Consistency (`model_health_service.py`)

`_evaluate_mode` recomputes the effective model to classify health. It now mirrors the same precedence as the lifecycle:

```python
credential_default = getattr(credential, "default_model", None)
if not isinstance(credential_default, str) or not credential_default.strip():
    credential_default = None
override = env_override or credential_default
effective_model = resolve_model(engine, provider, mode, override, ...)
```

This prevents a valid admin-curated `default_model` from being falsely classified as `unknown_model` or `stale_default`.

**`has_override` is keyed on `env_override` only** (not on `credential_default`). This keeps the badge CTA accurate: the `frozen_override` cause means "the user pinned a model they should edit/clear", not "an admin set a default". When the effective model comes from a credential default, the cause will be `stale_default` (if the model is unavailable), which maps to "Restart to use the current model" — the right action for a credential default that has gone stale.

---

## `ExternalAccountConfigService` (`external_account_config_service.py`)

Singleton: `external_account_config_service`

**`build_config(session, user) -> AccountConfigResponse`**

1. `SELECT AICredential WHERE owner_id == user.id ORDER BY created_at ASC`
2. For each credential: `AICredentialsService.decrypt_credential(credential) → AICredentialData`; then `_to_provider(credential)` to build an `AccountConfigProviderPublic`
3. A credential that fails decryption is skipped (warning log, no crash) so a single corrupt row cannot block login bootstrap
4. Calls `_resolve_default_credential_id(session, user)` for the `default_provider_credential_id` field

**`_to_provider(credential)` — provider map:**

| `AICredentialType` | `display_name` | `descriptor_slug` |
|--------------------|----------------|-------------------|
| `ANTHROPIC` | `"Claude"` | `"claude"` |
| `OPENAI` | `"OpenAI"` | `"openai"` |
| `GOOGLE` | `"Gemini"` | `"gemini"` |
| `OPENAI_COMPATIBLE` | `credential.name` (free-form) | `"openai-compatible"` |
| `MINIMAX` | `"MiniMax"` | `"minimax"` |

**`_to_provider(credential, data)`** builds `suggested_models` as:

```python
suggested_models = credential.available_models or credential.discovered_models or []
```

Curated wins; if curated is `None`/empty, falls back to `discovered_models`.

**`_resolve_model(cred_type, credential_model, discovered_models, default_model, available_models) -> str | None`**

Resolution chain (native clients call the provider API directly — must be a concrete ID):
1. `default_model` (admin curated) — when set and `not is_known_word(default_model)` (tier words are dropped because the native client can't use "haiku" against the provider API); prefix-stripped.
2. `credential_model` if set (always concrete; only `openai_compatible` has it stored)
3. `_strip_provider_prefix(available_models[0])` if `available_models` is non-empty
4. `_strip_provider_prefix(discovered_models[0])` if the list is non-empty
5. `resolve_model(engine, provider, mode="building", ...)` from `model_catalog` — but only if `not is_known_word(result)` (drops tier words like `"haiku"`, `"sonnet"` that are Claude Code internal shortcuts, not Anthropic API model IDs)
6. `None`

---

## `AICredential` Child Columns

**File:** `backend/app/models/credentials/ai_credential.py`

Five columns relevant to the managed-credential feature:

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `is_admin_managed` | `BOOLEAN` | NOT NULL, server_default false | Behavioral discriminator: row is read-only for owner when `True` |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE SET NULL | Audit-only. Which admin provisioned this row; NULL when not admin-provisioned, or when the admin account was later deleted |
| `managed_credential_id` | `UUID` | nullable, FK → `managed_ai_credential.id` ON DELETE SET NULL | Structural link to parent. NULL = not a managed child, or parent deleted out-of-band |
| `default_model` | `VARCHAR(255)` | nullable | Mirror of parent's admin-curated default model. NULL for self-created credentials. Written through by reconcile; read by SDK resolution and native config. |
| `available_models` | `JSON` | nullable | Mirror of parent's admin-curated selectable model list. NULL for self-created credentials. Written through by reconcile; read by model pickers and native config. |

`default_model` and `available_models` are **absent** from `AICredentialCreate` and `AICredentialUpdate` — users cannot set them through the user-facing CRUD. They are projected read-only on `AICredentialPublic` so the owner UI and SDK resolution can read them.

---

## Read-Only Guard in `AICredentialsService`

**File:** `backend/app/services/credentials/ai_credentials_service.py`

Two methods gained an `admin_override: bool = False` keyword argument:

```
update_credential(session, credential_id, user_id, data, *, admin_override=False)
    # After fetching the row:
    if credential.is_admin_managed and not admin_override:
        raise HTTPException(403, "This credential is managed by your administrator and cannot be modified.")

delete_credential(session, credential_id, user_id, force=False, *, admin_override=False)
    # Same guard
```

User-facing routes in `ai_credentials.py` call these **without** `admin_override` → users blocked. `ManagedAICredentialsService` calls them **with** `admin_override=True` → reconcile passes.

`set_default` is NOT guarded — setting an admin-managed credential as one's default is a read-only use (beneficial for the user), not a modification.

---

## `AICredentialPublic` Projection

`AICredentialsService._to_public(credential, session)` projects `is_admin_managed` from the row:

```
is_admin_managed=credential.is_admin_managed,
```

`managed_by_id` and `managed_credential_id` are deliberately NOT projected on `AICredentialPublic` (user-facing) to avoid leaking admin identity or internal parent ID to the credential owner.

---

## `AccountConfigProviderPublic` (no table)

File: `backend/app/models/external/account_config.py`

| Field | Type | Notes |
|-------|------|-------|
| `credential_id` | `uuid.UUID` | The source `AICredential.id` |
| `provider_type` | `AICredentialType` | |
| `display_name` | `str` | Human-readable — `"Claude"`, `"OpenAI"`, `"Gemini"`, `"MiniMax"`, or the credential's own name for `openai_compatible` |
| `descriptor_slug` | `str` | Stable slug: `"claude"` / `"openai"` / `"gemini"` / `"minimax"` / `"openai-compatible"` |
| `base_url` | `str \| None` | Endpoint override (for `openai_compatible` and `google`) |
| `model` | `str \| None` | Suggested concrete model ID (see model resolution) |
| `api_key` | `str` | **Decrypted key** — the security boundary |
| `is_default` | `bool` | Whether this is the user's default for its type |
| `is_admin_managed` | `bool` | Whether the row was admin-provisioned |
| `default_chat_mode_label` | `str` | Same as `display_name` — label the native app uses for the auto-created chat mode |
| `suggested_models` | `list[str]` | `credential.available_models` when non-empty (admin curated); otherwise `credential.discovered_models`; empty if neither is set |

## `AccountConfigResponse` (no table)

| Field | Type | Notes |
|-------|------|-------|
| `providers` | `list[AccountConfigProviderPublic]` | All owned credentials; empty when user has none |
| `default_provider_credential_id` | `uuid.UUID \| None` | Resolved conversation-default credential for the user |
| `generated_at` | `datetime` | UTC timestamp when the bundle was assembled |

---

## Security Events

All audit events contain counts/IDs but **never** key bytes.

| Event type | Scope | Severity | Emitted by |
|------------|-------|----------|------------|
| `admin.ai_credential.provision` | Child owner | `medium` | Per added child — `POST /` and `PATCH /{id}` |
| `admin.ai_credential.update` | Child owner | `medium` | Per mutated child — `PATCH /{id}` (no-op children emit no event) |
| `admin.ai_credential.delete` | Child owner | `medium` | Per removed child — `PATCH /{id}` and `DELETE /{id}` |
| `admin.ai_credential.set_default` | Child owner | `medium` | Per member — `POST /{id}/set-default` |
| `admin.managed_ai_credential.create` | Admin | `medium` | `POST /` — one per call |
| `admin.managed_ai_credential.update` | Admin | `medium` | `PATCH /{id}` — one per call |
| `admin.managed_ai_credential.delete` | Admin | `medium` | `DELETE /{id}` — one per call |
| `admin.managed_ai_credential.apply_to_existing` | Admin | `medium` | `POST /{id}/apply-to-existing` — real run only; a dry run emits nothing |
| `admin.ai_credential.auto_provision` | **New owner** | `low` | `AccountProvisioningService`, per child granted at account creation. `details = {managed_credential_id, child_credential_id, target_user_id, origin, role, managed_by_id, actor: "system"}` |
| `admin.ai_credential.auto_provision_failed` | **New owner** | `medium` | `AccountProvisioningService`, per parent that could not be granted. `details = {managed_credential_id, target_user_id, origin, role, reason, actor: "system"}` |
| `external.account_config.read` | Calling user | `high` | `GET /external/account-config` (successful call only) |

`external.account_config.read` details: `{client_kind, external_client_id, provider_count, credential_ids}`.

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone.

Automatic grants are **not** emitted by the admin route — they have no acting admin. They are siblings of `admin.ai_credential.provision` in the same namespace (different actor) so a reader of the security feed can tell an automatic grant from an admin's deliberate one without decoding the details blob. No key material is recorded in either.

---

## Model Catalog Integration

`ExternalAccountConfigService._resolve_model` imports from `backend/app/services/environments/model_catalog.py`:
- `resolve_model(engine, provider, mode, override, openai_compatible_model)` — returns the catalog default for a provider/engine combination
- `is_known_word(model_string)` — returns `True` for SDK-internal tier words (`"haiku"`, `"sonnet"`, `"opus"`) that cannot be used as Anthropic API model IDs
- `_strip_provider_prefix(model_string)` — strips the `provider/` prefix from discovered model IDs (e.g., `"anthropic/claude-3-5-sonnet-..."` → `"claude-3-5-sonnet-..."`)

---

## Dependencies

| Dependency | Used by |
|-----------|--------|
| `get_current_active_superuser` | All `/admin/llm-providers/` routes |
| `CurrentUser` | `/external/account-config` |
| `CurrentClientClaims` | `/external/account-config` (reads `client_kind`, `external_client_id` from JWT) |
| `SessionDep` | All routes |

`CurrentClientClaims` is defined in `backend/app/api/deps.py` and returns `(client_kind, external_client_id)` from the JWT. It is shared with the rest of the external A2A surface.

---

*Last updated: 2026-09-06 — zero-touch onboarding phase 4 renamed the admin UI route to `/admin/ai-credentials` (backend prefix, tag, service and cache key unchanged). Phase 2: `auto_provision_roles` + per-mode model overrides (migration `b71863b32aa1`), `add_members` / `apply_to_existing`, `(role, mode)` slot-conflict validation, `AccountProvisioningService`*
