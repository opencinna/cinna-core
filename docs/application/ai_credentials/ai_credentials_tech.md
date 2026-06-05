# AI Credentials Management - Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/ai_credential.py` - `AICredential` (table), `AICredentialCreate`, `AICredentialUpdate`, `AICredentialPublic`, `AICredentialsPublic`, `AICredentialType` (enum), `AICredentialData`, `AICredentialTestRequest`, `AICredentialTestResult`, `AffectedEnvironmentPublic`, `SharedUserPublic`, `AffectedEnvironmentsPublic`
- `backend/app/models/credentials/ai_credential_share.py` - `AICredentialShare` (table), `AICredentialSharePublic`, `AICredentialShareCreate`
- `backend/app/models/users/user.py` - `ai_credentials_encrypted` field (sync target)

**Routes:**
- `backend/app/api/routes/ai_credentials.py` - CRUD + set-default + affected-environments + resolve-default + test-connection endpoints
- `backend/app/api/main.py` - Router registration

**Services:**
- `backend/app/services/credentials/ai_credentials_service.py` - `AICredentialsService` (singleton: `ai_credentials_service`)
- `backend/app/services/credentials/model_discovery_service.py` - `probe_models` (DB-free shared dispatch), `discover_models_for_credential`, `test_connection`, `refresh_all_credentials`, `dispatch_model_deprecation_notifications`
- `backend/app/services/credentials/model_discovery_scheduler.py` - APScheduler daily cron with Postgres advisory-lock single-leader guard
- `backend/app/services/environments/environment_service.py` - `SDK_API_KEY_MAP`, strict `_validate_sdk_credential_compatibility`
- `backend/app/services/environments/sdk_constants.py` - `SDK_TO_CREDENTIAL_TYPE`, `SDK_CREDENTIAL_COMPATIBILITY`, `sdk_expected_credential_type`, `is_credential_compatible_with_sdk`
- `backend/app/services/bundles/install_service.py` - AI credential provision handling in installs/shares
- `backend/app/services/credentials/credential_share_service.py` - Clone/install AI credential setup
- `backend/app/services/environments/environment_lifecycle.py` - Credential type detection and `.env` generation

**Utilities:**
- `backend/app/utils.py:163` - `detect_anthropic_credential_type()` function
- `backend/app/core/security.py` - `encrypt_field()`, `decrypt_field()` (Fernet encryption)
- `backend/app/services/credentials/ai_credentials_service.py` - `_sync_default_to_user_profile()` for profile sync

**Templates:**
- `backend/app/env-templates/python-env-advanced/docker-compose.template.yml` - Container env var pass-through

**Migrations:**
- `backend/app/alembic/versions/h8c9d0e1f2g3_add_ai_credentials_table.py` - Core table
- `backend/app/alembic/versions/i9d0e1f2g3h4_add_ai_credential_shares.py` - Shares table
- `backend/app/alembic/versions/j0e1f2g3h4i5_add_share_ai_credentials.py` - Agent share credential fields
- `backend/app/alembic/versions/k1f2g3h4i5j6_add_env_ai_credentials.py` - Environment credential fields
- `backend/app/alembic/versions/67bd39e7e42c_add_expiry_notification_date_to_ai_.py` - Expiry date field
- `backend/app/alembic/versions/581dd9e44be1_add_discovered_models_to_ai_credential.py` - Per-credential model discovery cache columns

### Frontend

**Components:**
- `frontend/src/components/UserSettings/AICredentials.tsx` - Main credentials list with expiry badges, set-default, delete actions
- `frontend/src/components/UserSettings/AICredentialDialog.tsx` - Add/edit dialog with type selector, auto-fill expiry
- `frontend/src/components/UserSettings/AnthropicCredentialsModal.tsx` - Instructions modal for Anthropic API Key / OAuth setup
- `frontend/src/components/UserSettings/AffectedEnvironmentsDialog.tsx` - Post-update rebuild dialog
- `frontend/src/components/Environments/AddEnvironment.tsx` - Environment creation dialog with compact summary rows + `EnvModeEditDialog` modal for SDK/credential/model selection per mode
- `frontend/src/components/Install/InstallAICredentialSection.tsx` - AI credential selection step in the install wizard
- `frontend/src/components/Common/RelativeTime.tsx` - Extended with badge/color-code support for expiry display

**Client (auto-generated):**
- `frontend/src/client/sdk.gen.ts` - `AiCredentialsService`
- `frontend/src/client/types.gen.ts` - `AICredentialPublic` (includes `is_oauth_token: bool`), `AICredentialCreate`, `AICredentialUpdate`, `AICredentialsPublic`, `AICredentialType`

## Database Schema

### Table: `ai_credential`

- `id` (UUID, PK)
- `owner_id` (UUID, FK → user.id, CASCADE)
- `name` (VARCHAR 255)
- `type` (VARCHAR 50) - "anthropic" | "minimax" | "openai" | "openai_compatible" | "google"
- `encrypted_data` (TEXT) - Fernet-encrypted JSON
- `is_default` (BOOLEAN)
- `expiry_notification_date` (DATETIME, nullable) - Informational expiry reminder
- `created_at`, `updated_at` (DATETIME)
- `discovered_models` (JSON, nullable) — list of model IDs the key can access; `null` until the discovery cron runs. Added by migration `581dd9e44be1`.
- `models_discovered_at` (timestamptz, nullable) — timestamp of last SUCCESSFUL discovery.
- `models_discovery_error` (TEXT, nullable) — coarse failure reason code (e.g. `"oauth_token_unsupported"`, `"invalid_key"`). `null` when healthy.

Indexes: `ix_ai_credential_owner_type` (owner_id, type), `ix_ai_credential_owner_default` (owner_id, is_default)

`discovered_models`, `models_discovered_at`, and `models_discovery_error` are exposed on
`AICredentialPublic` (non-secret). The UI uses `discovered_models` as `<datalist>` suggestions
for model-override inputs.

### User fields for AI Functions SDK routing (on `user` table)

- `default_ai_functions_sdk` (VARCHAR 50, default `'system'`) - `"system"` or `"personal:anthropic"` (validated against `VALID_AI_FUNCTIONS_SDK_OPTIONS`; any value without the `personal:` prefix causes `default_ai_functions_credential_id` to be cleared)
- `default_ai_functions_credential_id` (UUID, nullable) - optional pin to a specific `AICredential`; `null` means use the default for type

### Table: `ai_credential_shares`

- `id` (UUID, PK)
- `ai_credential_id` (UUID, FK → ai_credential.id, CASCADE)
- `shared_with_user_id` (UUID, FK → user.id, CASCADE)
- `shared_by_user_id` (UUID, FK → user.id)
- `shared_at` (DATETIME)

Indexes: `ix_ai_credential_shares_credential` (ai_credential_id), `ix_ai_credential_shares_recipient` (shared_with_user_id)

### Environment Credential Fields (on `agent_environment`)

- `use_default_ai_credentials` (BOOLEAN, default true)
- `conversation_ai_credential_id` (UUID, nullable)
- `building_ai_credential_id` (UUID, nullable)

### Agent Share Credential Fields (on `agent_share`)

- `provide_ai_credentials` (BOOLEAN, default false)
- `conversation_ai_credential_id` (UUID, nullable)
- `building_ai_credential_id` (UUID, nullable)

## API Endpoints

**File:** `backend/app/api/routes/ai_credentials.py`

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/ai-credentials/` | List user's AI credentials |
| `POST` | `/api/v1/ai-credentials/` | Create new AI credential |
| `POST` | `/api/v1/ai-credentials/test-connection` | Test a key and (Edit case) force-refresh discovered models |
| `GET` | `/api/v1/ai-credentials/{credential_id}` | Get credential details |
| `PATCH` | `/api/v1/ai-credentials/{credential_id}` | Update credential |
| `DELETE` | `/api/v1/ai-credentials/{credential_id}` | Delete credential |
| `GET` | `/api/v1/ai-credentials/resolve-default/{sdk_engine}` | Resolve best default credential for SDK engine (prioritized) |
| `POST` | `/api/v1/ai-credentials/{credential_id}/set-default` | Set as default, sync to user profile |
| `GET` | `/api/v1/ai-credentials/{credential_id}/affected-environments` | Get environments using this credential |

### `POST /api/v1/ai-credentials/test-connection`

**Request — `AICredentialTestRequest`:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `type` | `AICredentialType` | Yes | Provider type |
| `api_key` | `string \| null` | No | Plaintext key (Add form, or re-typed Edit key) |
| `base_url` | `string \| null` | No | Used for `openai_compatible` and `google` |
| `credential_id` | `UUID \| null` | No | Present for the Edit case; owner-scoped |

**Key resolution (in order):** `request.api_key` → decrypt stored credential by `credential_id` → HTTP 422 if neither yields a key.

**Response — `AICredentialTestResult`:**

| Field | Type | Notes |
|-------|------|-------|
| `success` | `bool` | `true` on working key or benign skip |
| `models` | `list[str]` | Discovered model IDs (empty on skip or failure) |
| `model_count` | `int` | `len(models)` |
| `error` | `string \| null` | Populated ONLY when `success=False`; value: `"invalid_key"` |
| `skip_reason` | `string \| null` | Populated ONLY when `success=True` but listing not applicable; values: `"oauth_token_unsupported"` / `"no_list_endpoint"` / `"no_base_url"` |

`error` and `skip_reason` are mutually exclusive. `success=True` with both `null` means a clean listing.

**Error responses:** 422 if no key can be resolved; 404 if `credential_id` row is missing; 403 if `credential_id` belongs to another user.

## Services & Key Methods

### `AICredentialsService` (`backend/app/services/credentials/ai_credentials_service.py`)

**Core CRUD:**
- `list_credentials(session, user_id)` - List all credentials for user
- `get_credential(session, credential_id, user_id)` - Get with ownership check
- `create_credential(session, user_id, data)` - Create, encrypt, auto-detect Anthropic type for expiry
- `update_credential(session, credential_id, user_id, data)` - Update, re-encrypt if key changed
- `delete_credential(session, credential_id, user_id)` - Delete, clear profile if was default

**Default Management:**
- `set_default(session, credential_id, user_id)` - Unset previous default, set new, sync to profile
- `get_default_for_type(session, user_id, cred_type)` - Get default credential for type
- `resolve_default_credential_for_sdk(session, user_id, sdk_engine)` - Find best default credential for an SDK engine using prioritized resolution (Anthropic > Google > OpenAI > other compatible types by created_at ASC)

**Sharing:**
- `share_credential(session, credential_id, owner_id, recipient_id)` - Create share link
- `can_access_credential(session, credential_id, user_id)` - Check ownership or share access
- `get_credential_for_use(session, credential_id, user_id)` - Return decrypted data if accessible
- `revoke_share(session, credential_id, recipient_id)` - Remove share link
- `list_shared_with_me(session, user_id)` - List credentials shared with user

**Affected Environments:**
- `get_affected_environments()` - Query environments linked to a credential with usage type

**Internal:**
- `_decrypt_credential(credential)` - Decrypt to `AICredentialData`
- `_sync_default_to_user_profile(session, user, credential)` - Auto-sync values to user fields
- `_clear_user_profile_for_type(session, user, cred_type)` - Clear profile on default deletion

### Model Discovery Service (`model_discovery_service.py`)

Polls each provider's native model-listing endpoint and caches the result on the `AICredential` row. All blocking HTTP I/O runs via `anyio.to_thread.run_sync`.

#### `probe_models(cred_type, api_key, base_url=None) -> ProbeResult`

DB-free async function. **The single dispatch path used by both the discovery cron and the Test Connection endpoint.** Returns a `ProbeResult(ok, models, reason)`.

**Per-type behavior:**

| Type | Endpoint | Notes |
|------|----------|-------|
| `anthropic` | `GET https://api.anthropic.com/v1/models` | Skips OAuth tokens (`sk-ant-oat*`) — `reason="oauth_token_unsupported"` |
| `openai` | `GET https://api.openai.com/v1/models` | |
| `google` | `google.genai.Client.models.list()` | Strips `models/` prefix from returned names |
| `openai_compatible` | `GET {base_url}/models` | Skips when no `base_url` — `reason="no_base_url"` |
| `minimax` | — | Always skips — `reason="no_list_endpoint"` |

HTTP 401/403 → `ok=False`, `reason="invalid_key"`. On success: deduplicates (preserving order), `ok=True`, `reason=None`.

#### `discover_models_for_credential(session, credential) -> list[str]`

Decrypts the credential, calls `probe_models`, maps the result onto the credential row, and returns the effective model list. Used by the cron; the caller commits.

#### `test_connection(session, user_id, request) -> AICredentialTestResult`

Service entry point for `POST /ai-credentials/test-connection`. Resolves the key from request or stored credential, calls `probe_models`, splits the `ProbeResult.reason` into `error` (failure) / `skip_reason` (benign skip) for the unambiguous response contract.

**Persistence (Edit case only, `credential_id` present):** writes `discovered_models` / `models_discovered_at` / `models_discovery_error` onto the stored row and commits — this is the manual force-refresh path. For the Add case (no row yet) nothing is persisted.

#### `refresh_all_credentials(session) -> int`

Cron batch entry point. Iterates all `AICredential` rows using `discover_models_for_credential`. Failure-isolated: one bad key never aborts the batch. On exception: records the exception class name as `models_discovery_error`. Returns count of successfully refreshed credentials.

### Model Discovery Scheduler (`model_discovery_scheduler.py`)

Daily APScheduler cron (`BackgroundScheduler`, `interval` trigger). A Postgres session-level
advisory lock (`pg_try_advisory_lock`) enforces single-leader execution across gunicorn/uvicorn
workers: the first worker to acquire the lock runs the batch; others skip.

After each discovery batch, the scheduler calls `dispatch_model_deprecation_notifications`
which evaluates model health for every environment and sends `model_deprecated` system
notifications on newly-flagged environments. See
[model_freshness_tech.md](../../agents/agent_environments/model_freshness_tech.md) for details.

Configured via `MODEL_DISCOVERY_ENABLED` (default `True`) and
`MODEL_DISCOVERY_INTERVAL_HOURS` (default `24`) in `backend/app/core/config.py`.

### Auto-Sync Mapping

| Credential Type | User Profile Fields |
|----------------|-------------------|
| `anthropic` | `anthropic_api_key` |
| `minimax` | `minimax_api_key` |
| `openai_compatible` | `openai_compatible_api_key`, `openai_compatible_base_url`, `openai_compatible_model` |

### Environment Service (`backend/app/services/environments/environment_service.py`)

- `SDK_API_KEY_MAP` - Maps legacy SDK IDs to API key field names
- `_validate_sdk_credential_compatibility(sdk_id, credential)` - Strict full-SDK provider match (e.g. `opencode/anthropic` only accepts `anthropic`-typed credentials). Raises `EnvironmentCredentialError` (400) on mismatch. Re-exports its lookup via `sdk_constants.sdk_expected_credential_type`
- `create_environment()` - Resolves default or validates linked credentials per SDK type

### SDK Constants (`backend/app/services/environments/sdk_constants.py`)

- `SDK_TO_CREDENTIAL_TYPE` - Single source of truth: maps full SDK IDs (e.g. `opencode/anthropic`, `opencode/openai`) to their required `AICredentialType`
- `SDK_CREDENTIAL_COMPATIBILITY` - Engine-wide credential type lists. Used only for forward-compat fallback (unmapped SDK ids) and for `resolve_default_credential_for_sdk` priority ranking — NOT for strict validation
- `sdk_expected_credential_type(sdk_id)` - Helper returning the exact `AICredentialType` for a full SDK id, or `None` for unmapped ids
- `is_credential_compatible_with_sdk(sdk_id, cred_type)` - Strict boolean check used by the bundle-level validators

### Where the strict match is enforced

- `EnvironmentService.create_environment` (env create / update path)
- `BundleService.update_bundle` (`PATCH /bundles/{uuid}` for `publisher_ai_credential_*_id`)
- `InstallService._validate_ai_credentials_draft` (`PATCH /agents/{id}/publish-settings` pre-publish draft)
- `PublishService._validate_publisher_ai_credentials_sdk` (publish-time pre-flight)
- `EnvironmentLifecycleManager` (rebuild path — uses `SDK_TO_CREDENTIAL_TYPE` to route the right key into the right provider slot)
- `frontend/src/components/Agents/CredentialProvisioningSection.tsx` (publisher AI credential dropdown filter — strict full-SDK match via `sdkExpectedCredentialType`)

### Install / Credential Share Service (`backend/app/services/credentials/credential_share_service.py`)

- Handles AI credential provision during installs and shares. If `provide_ai_credentials=true`: creates `AICredentialShare` links and links the clone environment. If false: uses recipient's selected or default credentials.

## Frontend Components

### `AICredentials.tsx` - Main Settings UI

- Credentials list card with compact rows: name, default star icon, expiry badge, type label, set-default/edit/delete buttons
- Set default via star icon; triggers `AffectedEnvironmentsDialog` after success
- Add button opens `AICredentialDialog`
- SDK Preferences card with compact summary rows for Conversation and Building modes:
  - Each row shows: mode icon, engine name (bold), resolved credential name (muted), model override if set
  - Pencil button opens `SDKModeEditDialog` modal with SDK engine, credential, and model override fields
  - Resolved default indicator shown in modal when "Use Default" is selected (calls `resolve-default/{sdk_engine}` endpoint)
  - AI Functions section (below separator, app-level): provider dropdown + conditional credential picker

### `SDKModeEditDialog` (inline in `AICredentials.tsx`)

- Modal dialog for editing a single mode's SDK preferences (conversation or building)
- Three-step cascading form: SDK Engine → Credential (with resolved default indicator) → Model Override
- Saves only that mode's values; closes on success

### `AICredentialDialog.tsx` - Add/Edit Dialog

- Name input, type selector (disabled when editing), API key (password field)
- Supported types: `anthropic`, `minimax`, `openai`, `openai_compatible`, `google`
- OpenAI Compatible: additional base_url and model inputs
- Google: optional base_url
- "Set as default" checkbox
- Expiry notification date field with auto-fill for Anthropic OAuth tokens
- Anthropic info banner with "Instructions" button opening `AnthropicCredentialsModal`
- **Test Connection button** (footer, between Cancel and Create/Update): calls `POST /ai-credentials/test-connection`; shows an inline alert with the result above the footer. On success shows "Connection successful — N models available." On a benign skip (`oauth_token_unsupported`, `no_list_endpoint`, etc.) shows a type-specific informative note. On failure shows "Connection failed — the provider rejected this key." In the Edit case a successful test invalidates `["aiCredentialsList"]` so refreshed `discovered_models` appear in model-override datalists immediately.

## State Management

**Query Keys:**
- `["aiCredentialsList"]` - List of named credentials
- `["aiCredentialsStatus"]` - User's credential status (has_* flags)
- `["resolveDefaultCredential", sdkEngine]` - Resolved default credential for a given SDK engine

**Mutations:**
- Create/Update/Delete invalidate both query keys
- Set default also invalidates `["aiCredentialsStatus"]`
- Test Connection (Edit, on success): invalidates `["aiCredentialsList"]` to surface refreshed `discovered_models`

## Encryption

- Fernet encryption (PBKDF2-HMAC-SHA256) via `backend/app/core/security.py`
- Credential data stored as encrypted JSON: `{"api_key": "...", "base_url": "...", "model": "..."}`
- Decryption only when needed: set-default sync, environment generation, credential use

## Security

- All routes require authentication (`CurrentUser` dependency)
- Ownership validation on all CRUD operations
- CASCADE delete when user is deleted
- API keys never exposed in responses (`has_api_key: true` pattern)
- Shared credentials: read-only access for recipients
- Expiry date is informational only, not enforced

---

*Last updated: 2026-06-05 — added Test Connection endpoint + probe_models shared dispatch*
