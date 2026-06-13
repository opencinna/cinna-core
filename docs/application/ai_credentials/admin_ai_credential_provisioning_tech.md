# Admin-Provisioned AI Credentials + Native Account-Config — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/ai_credential.py` — `AICredential` (two new columns: `is_admin_managed`, `managed_by_id`); `AICredentialPublic` (new `is_admin_managed` projection); `AdminAICredentialPublic` (admin-only projection adds `owner_id`, `managed_by_id`); `AdminAICredentialCreate`, `AdminAICredentialProvisionResult`, `AdminProvisionSkip` (admin DTOs)
- `backend/app/models/external/account_config.py` — `AccountConfigProviderPublic`, `AccountConfigResponse` (native-config response models)

**Routes:**
- `backend/app/api/routes/admin_llm_providers.py` — `POST/GET/PATCH/DELETE /admin/llm-providers/`, `POST /admin/llm-providers/{id}/set-default`; superuser-gated
- `backend/app/api/routes/external_account_config.py` — `GET /external/account-config`; native-token-gated
- `backend/app/api/main.py` — both routers registered (`admin_llm_providers.router`, `external_account_config.router`)

**Services:**
- `backend/app/services/credentials/admin_ai_credentials_service.py` — `AdminAICredentialService` (singleton: `admin_ai_credentials_service`)
- `backend/app/services/external/external_account_config_service.py` — `ExternalAccountConfigService` (singleton: `external_account_config_service`)
- `backend/app/services/credentials/ai_credentials_service.py` — `update_credential` and `delete_credential` extended with `admin_override: bool = False` kwarg; `_to_public` projects `is_admin_managed`

**Migration:**
- `backend/app/alembic/versions/2f2d8e49501d_add_admin_managed_ai_credential.py` — adds `is_admin_managed` and `managed_by_id` columns to `ai_credential`; `down_revision = 'd3f0a1b2c4e5'`

### Frontend

**Route:**
- `frontend/src/routes/_layout/admin/llm-providers.tsx` — `AdminLlmProviders` page component; `beforeLoad` redirects unauthenticated users to `/login` and non-superusers to `/`; registered at `/admin/llm-providers`

**Sidebar entry:**
- `frontend/src/components/Sidebar/AdminMenu.tsx` — "LLM Providers" item with `KeyRound` icon in the Admin dropdown

**Components under `frontend/src/components/Admin/LlmProviders/`:**
- `LlmProvidersTable.tsx` — renders `AdminAICredentialPublic[]`; columns: Target User, Name, Provider (badge), Default (badge), Created, row-actions; owner labels resolved from the `ownerLabels` prop; unlabeled owners fall back to `<first-8-chars>…`
- `ProvisionLlmProviderDialog.tsx` — provision form; provider-conditional fields (base\_url for `openai_compatible`/`google`, model for `openai_compatible`); multi-select `UserAllowlistPicker` for `target_user_ids`; `set_as_default` and `set_user_sdk_defaults` toggles; calls `AdminLlmProvidersService.provisionAiCredentials`; surfaces per-target skip reasons in toast
- `LlmProviderActionsMenu.tsx` — three-dot menu per row: edit (name, api\_key, conditional base\_url/model; blank api\_key keeps existing key), set-default (disabled when `is_default`), delete (with `AlertDialog` confirm); calls `updateManagedAiCredential`, `setManagedAiCredentialDefault`, `deleteManagedAiCredential`
- `providerTypes.ts` — `PROVIDER_TYPE_OPTIONS` array (value/label/description for all five `AICredentialType` values); `getProviderTypeLabel` helper; `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]`; `managedCredentialsQueryKey(targetUserId?)` factory

**Generated client services used:**
- `AdminLlmProvidersService` — `provisionAiCredentials`, `listManagedAiCredentials`, `getManagedAiCredential`, `updateManagedAiCredential`, `deleteManagedAiCredential`, `setManagedAiCredentialDefault`
- `UsersService.readUsers` — fetches the first 100 users to build the `ownerLabels` map for the table

**Still pending (not yet implemented):**
- Read-only badge on admin-managed rows in `frontend/src/components/UserSettings/AICredentials.tsx` (keyed off `AICredentialPublic.is_admin_managed`)
- Native-app (Cinna Desktop / Mobile) provider and chat-mode auto-creation driven by `GET /external/account-config`

---

## Database Schema Changes

### `ai_credential` table — two new columns (migration `2f2d8e49501d`)

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `is_admin_managed` | `BOOLEAN` | `NOT NULL`, `server_default false` | Behavioral discriminator: row is read-only for owner when `True` |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` `ON DELETE SET NULL`, index `ix_ai_credential_managed_by_id` | Audit-only. Which admin provisioned this row; `NULL` when no admin did, or when the admin account was later deleted |

No data backfill needed — `server_default false` correctly marks all pre-existing rows as not admin-managed.

Downgrade: drops FK, drops index, drops both columns.

### `AccountConfigProviderPublic` (new model, no table)

File: `backend/app/models/external/account_config.py`

| Field | Type | Notes |
|-------|------|-------|
| `credential_id` | `uuid.UUID` | The source `AICredential.id` |
| `provider_type` | `AICredentialType` | `anthropic` / `openai` / `google` / `openai_compatible` / `minimax` |
| `display_name` | `str` | Human-readable — `"Claude"`, `"OpenAI"`, `"Gemini"`, `"MiniMax"`, or the credential's own name for `openai_compatible` |
| `descriptor_slug` | `str` | Stable slug: `"claude"` / `"openai"` / `"gemini"` / `"minimax"` / `"openai-compatible"` |
| `base_url` | `str \| None` | Endpoint override (for `openai_compatible` and `google`) |
| `model` | `str \| None` | Suggested concrete model ID (see model resolution) |
| `api_key` | `str` | **Decrypted key** — the security boundary |
| `is_default` | `bool` | Whether this is the user's default for its type |
| `is_admin_managed` | `bool` | Whether the row was admin-provisioned |
| `default_chat_mode_label` | `str` | Same as `display_name` — label the native app uses for the auto-created chat mode |
| `suggested_models` | `list[str]` | From `credential.discovered_models`; empty if not yet discovered |

### `AccountConfigResponse` (new model, no table)

| Field | Type | Notes |
|-------|------|-------|
| `providers` | `list[AccountConfigProviderPublic]` | All owned credentials; empty when user has none |
| `default_provider_credential_id` | `uuid.UUID \| None` | Resolved conversation-default credential for the user |
| `generated_at` | `datetime` | UTC timestamp when the bundle was assembled |

---

## Admin DTOs

### `AdminAICredentialCreate`

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | 1–255 chars |
| `type` | `AICredentialType` | Provider type |
| `api_key` | `str` | Plaintext key (min length 1); the same key is written into each per-user row |
| `base_url` | `str \| None` | Required for `openai_compatible`; optional for `google` |
| `model` | `str \| None` | Required for `openai_compatible` |
| `expiry_notification_date` | `datetime \| None` | Informational expiry reminder |
| `target_user_ids` | `list[uuid.UUID]` | One or more target users; min length 1; deduplicated preserving order |
| `set_as_default` | `bool` | Default `False`; calls `set_default` per row if `True` |
| `set_user_sdk_defaults` | `bool` | Default `False`; wires `default_sdk_*` + `default_ai_credential_*_id` if `True` |
| `sdk_default_modes` | `list[str]` | Default `["conversation", "building"]`; unsupported values are skipped |

### `AdminAICredentialPublic`

Extends `AICredentialPublic` with two admin-only fields:
- `owner_id: uuid.UUID` — which user owns this row
- `managed_by_id: uuid.UUID | None` — which admin provisioned it (null when admin account was deleted)

These fields are deliberately absent from `AICredentialPublic` (the user-facing projection) to avoid leaking the admin identity.

### `AdminAICredentialProvisionResult`

| Field | Type | Notes |
|-------|------|-------|
| `created` | `list[AdminAICredentialPublic]` | One entry per successfully provisioned target |
| `skipped` | `list[AdminProvisionSkip]` | Users that were skipped |

### `AdminProvisionSkip`

| Field | Type | Values |
|-------|------|--------|
| `user_id` | `uuid.UUID` | The skipped target |
| `reason` | `str` | `"user_not_found"` / `"user_inactive"` |

---

## API Endpoints

### Admin LLM Providers (`/api/v1/admin/llm-providers/`)

**File:** `backend/app/api/routes/admin_llm_providers.py`
**Auth gate:** `get_current_active_superuser` — `403` for anyone else

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `POST` | `/admin/llm-providers/` | `AdminAICredentialCreate` | `AdminAICredentialProvisionResult` | Creates one row per valid target; emits per-row + batch `SecurityEvent` |
| `GET` | `/admin/llm-providers/` | query `?target_user_id=` optional | `list[AdminAICredentialPublic]` | Fleet-wide, ordered by `created_at DESC` |
| `GET` | `/admin/llm-providers/{credential_id}` | — | `AdminAICredentialPublic` | `404` if not admin-managed |
| `PATCH` | `/admin/llm-providers/{credential_id}` | `AICredentialUpdate` | `AdminAICredentialPublic` | Emits `admin.ai_credential.update` |
| `DELETE` | `/admin/llm-providers/{credential_id}?force=bool` | — | `Message` | `409` on Tier-2 blast-radius unless `force`; emits `admin.ai_credential.delete` |
| `POST` | `/admin/llm-providers/{credential_id}/set-default` | — | `AdminAICredentialPublic` | Sets as the owner-user's default; emits `admin.ai_credential.set_default` |

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

## Services

### `AdminAICredentialService` (`admin_ai_credentials_service.py`)

Singleton: `admin_ai_credentials_service`

**Key methods:**

| Method | Description |
|--------|-------------|
| `provision_for_users(session, admin, data)` | Creates one admin-managed `AICredential` per valid target user. Returns `AdminAICredentialProvisionResult`. Invalid/inactive users go to `skipped`. Per-type field validation delegates to `AICredentialsService.create_credential` (fails the whole call if bad). |
| `list_managed(session, admin, target_user_id=None)` | Returns all admin-managed rows fleet-wide, optionally scoped to one user. |
| `get_managed(session, admin, credential_id)` | Returns one admin-managed row; `404` if not found or `is_admin_managed=False`. |
| `update_managed(session, admin, credential_id, data)` | Updates on behalf of the owner, passing `admin_override=True` to bypass the user read-only guard. |
| `delete_managed(session, admin, credential_id, force=False)` | Deletes with the Tier-2 blast-radius gate; passes `admin_override=True`. |
| `set_managed_default(session, admin, credential_id)` | Sets the row as the owner-user's default by delegating to `AICredentialsService.set_default(credential.id, credential.owner_id)`. |

**Implementation note:** every method that calls into `AICredentialsService` does so with `user_id = credential.owner_id` (not the admin's ID), so all per-user invariants (one-default-per-type, profile auto-sync, `default_sdk_*` wiring) run for the **target user**.

**`_provision_one` flow (internal):**
1. Call `AICredentialsService.create_credential(session, target.id, AICredentialCreate(...))` — handles encryption, type validation, expiry auto-detection for Anthropic OAuth tokens
2. Fetch the new row; stamp `is_admin_managed=True`, `managed_by_id=admin.id`; commit
3. If `set_as_default`: call `AICredentialsService.set_default(credential.id, target.id)` — runs profile auto-sync for the target
4. If `set_user_sdk_defaults`: call `_apply_sdk_defaults` — writes `target.default_sdk_conversation/building` and `target.default_ai_credential_*_id` using the `_TYPE_TO_SDK_ENGINE` map

**`_TYPE_TO_SDK_ENGINE` map:**
| `AICredentialType` | Composed SDK engine |
|-------------------|---------------------|
| `ANTHROPIC` | `"claude-code/anthropic"` |
| `MINIMAX` | `"claude-code/minimax"` |
| `OPENAI` | `"opencode/openai"` |
| `GOOGLE` | `"opencode/google"` |
| `OPENAI_COMPATIBLE` | `"opencode/openai_compatible"` |

### `ExternalAccountConfigService` (`external_account_config_service.py`)

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

**`_resolve_model(cred_type, credential_model, discovered_models) -> str | None`**

Resolution chain (native clients call the provider API directly — must be a concrete ID):
1. `credential.model` if set (always concrete; only `openai_compatible` has it stored)
2. `_strip_provider_prefix(discovered_models[0])` if the list is non-empty
3. `resolve_model(engine, provider, mode="building", ...)` from `model_catalog` — but only if `not is_known_word(result)` (drops tier words like `"haiku"`, `"sonnet"` that are Claude Code internal shortcuts, not Anthropic API model IDs)
4. `None`

**`_resolve_default_credential_id(session, user) -> uuid.UUID | None`**

Reads `user.default_sdk_conversation` (falls back to `"claude-code"` if not set), takes the engine-prefix (`split("/")[0]`), then calls `AICredentialsService.resolve_default_credential_for_sdk(session, user.id, engine_prefix)`.

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

User-facing routes in `ai_credentials.py` call these **without** `admin_override` → users blocked. `AdminAICredentialService` calls them **with** `admin_override=True` → admins pass.

`set_default` is NOT guarded — setting an admin-managed credential as one's default is a read-only use (beneficial for the user), not a modification.

---

## `AICredentialPublic` Projection

`AICredentialsService._to_public(credential, session)` projects `is_admin_managed` from the row:

```
is_admin_managed=credential.is_admin_managed,
```

`managed_by_id` is deliberately NOT projected on `AICredentialPublic` (user-facing). It is only available on `AdminAICredentialPublic` (admin-only surface) to avoid leaking the admin identity to the credential owner.

---

## Security Events

All audit events contain counts/IDs but **never** key bytes.

| Event type | Scope | Severity | Emitted by |
|------------|-------|----------|------------|
| `admin.ai_credential.provision` | Target user | `medium` | `POST /admin/llm-providers/` — one per provisioned row |
| `admin.ai_credential.provision_batch` | Admin | `medium` | `POST /admin/llm-providers/` — one per call |
| `admin.ai_credential.update` | Target user | `medium` | `PATCH /admin/llm-providers/{id}` |
| `admin.ai_credential.delete` | Target user | `medium` | `DELETE /admin/llm-providers/{id}` |
| `admin.ai_credential.set_default` | Target user | `medium` | `POST /admin/llm-providers/{id}/set-default` |
| `external.account_config.read` | Calling user | `high` | `GET /external/account-config` (successful call only) |

`external.account_config.read` details: `{client_kind, external_client_id, provider_count, credential_ids}`.

---

## Dependencies

| Dependency | Used by |
|-----------|--------|
| `get_current_active_superuser` | All `/admin/llm-providers/` routes |
| `CurrentUser` | `/external/account-config` |
| `CurrentClientClaims` | `/external/account-config` (reads `client_kind`, `external_client_id` from JWT) |
| `SessionDep` | All routes |

`CurrentClientClaims` is defined in `backend/app/api/deps.py` and returns `(client_kind, external_client_id)` from the JWT. It is shared with the rest of the external A2A surface (used there for client attribution).

---

## Model Catalog Integration

`ExternalAccountConfigService._resolve_model` imports from `backend/app/services/environments/model_catalog.py`:
- `resolve_model(engine, provider, mode, override, openai_compatible_model)` — returns the catalog default for a provider/engine combination
- `is_known_word(model_string)` — returns `True` for SDK-internal tier words (`"haiku"`, `"sonnet"`, `"opus"`) that cannot be used as Anthropic API model IDs
- `_strip_provider_prefix(model_string)` — strips the `provider/` prefix from discovered model IDs (e.g., `"anthropic/claude-3-5-sonnet-..."` → `"claude-3-5-sonnet-..."`)

---

*Last updated: 2026-06-13 — admin UI (LLM Providers section) shipped; native account-config backend-only*
