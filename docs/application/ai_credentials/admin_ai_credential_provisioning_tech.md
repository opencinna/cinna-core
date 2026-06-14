# Admin-Provisioned AI Credentials + Native Account-Config — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/managed_ai_credential.py` — `ManagedAICredential` (parent table); `ManagedAICredentialCreate`, `ManagedAICredentialUpdate`, `ManagedAICredentialPublic` (admin DTOs); `ManagedAICredentialMember`, `ManagedAICredentialReconcileResult`, `ManagedReconcileSkip`, `ManagedReconcileBlock`
- `backend/app/models/credentials/ai_credential.py` — `AICredential` (child link: `managed_credential_id` FK, ON DELETE SET NULL; existing `is_admin_managed` and `managed_by_id` columns unchanged); `AICredentialPublic` (`is_admin_managed` projection)
- `backend/app/models/external/account_config.py` — `AccountConfigProviderPublic`, `AccountConfigResponse` (native-config response models)

**Routes:**
- `backend/app/api/routes/admin_llm_providers.py` — parent-oriented `POST/GET/PATCH/DELETE /admin/llm-providers/`, `POST /admin/llm-providers/{id}/set-default`, `POST /admin/llm-providers/test-connection`; superuser-gated
- `backend/app/api/routes/external_account_config.py` — `GET /external/account-config`; native-token-gated
- `backend/app/api/main.py` — both routers registered (`admin_llm_providers.router`, `external_account_config.router`)

**Services:**
- `backend/app/services/credentials/managed_ai_credentials_service.py` — `ManagedAICredentialsService` (singleton: `managed_ai_credentials_service`); owns parent CRUD + reconcile
- `backend/app/services/credentials/admin_ai_credentials_service.py` — `AdminAICredentialService` (legacy singleton: `admin_ai_credentials_service`); **no longer wired to any route**; retained but superseded
- `backend/app/services/external/external_account_config_service.py` — `ExternalAccountConfigService` (singleton: `external_account_config_service`)
- `backend/app/services/credentials/ai_credentials_service.py` — `update_credential` and `delete_credential` extended with `admin_override: bool = False` kwarg; `_to_public` projects `is_admin_managed`; `_clear_user_profile_for_type` used by `_clear_child_default`

**Migrations:**
- `backend/app/alembic/versions/d3782dd039a5_add_managed_ai_credential.py` — creates `managed_ai_credential` table; adds `ai_credential.managed_credential_id` FK (ON DELETE SET NULL); `down_revision = '2f2d8e49501d'`; schema-only (no data backfill)
- `backend/app/alembic/versions/2f2d8e49501d_add_admin_managed_ai_credential.py` — earlier migration that added `is_admin_managed` and `managed_by_id` to `ai_credential`

### Frontend

**Route:**
- `frontend/src/routes/_layout/admin/llm-providers.tsx` — `AdminLlmProviders` page component; `beforeLoad` redirects unauthenticated users to `/login` and non-superusers to `/`; registered at `/admin/llm-providers`; client-side pagination (10 per page); filter by `target_user_id` via `UserAllowlistPicker` toggle-panel

**Sidebar entry:**
- `frontend/src/components/Sidebar/AdminMenu.tsx` — "LLM Providers" item with `KeyRound` icon in the Admin dropdown

**Components under `frontend/src/components/Admin/LlmProviders/`:**
- `ManagedCredentialDialog.tsx` — unified create + edit dialog. In `create` mode it provides its own trigger button and manages open state internally; in `edit` mode it is fully controlled by the actions menu. Provider type is immutable after creation (`disabled` on the Select). API key field is blank in edit mode (blank = keep stored key for all members). Member add/remove via `UserAllowlistPicker` pre-seeded from `record.members`. Test Connection probes via `POST /test-connection` (resolves stored parent key when `api_key` is blank and `record.has_api_key` is true). Reconcile result surfaced as per-user skip/blocked toasts + summary toast.
- `LlmProvidersTable.tsx` — renders `ManagedAICredentialPublic[]`; columns: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | Shared with (member chips: `full_name <email>` or `email`) | Created; no per-member default badge; member labels resolved inline from `record.members` (members carry their own `email`/`full_name` — no separate user-fetch needed)
- `LlmProviderActionsMenu.tsx` — three-dot menu per parent row: Edit (opens `ManagedCredentialDialog` in `edit` mode), Set default for all (calls `/set-default`), Delete (with two-stage `AlertDialog`: first confirm, then if `409` escalates to a force-delete confirmation listing blocked members by name)
- `providerTypes.ts` — `PROVIDER_TYPE_OPTIONS` array (Anthropic/OpenAI/OpenAI Compatible/Google — MiniMax omitted); `getProviderTypeLabel` helper; `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]`; `managedCredentialsQueryKey(targetUserId?)` factory

**Generated client services used:**
- `AdminLlmProvidersService` — `createManagedAiCredential`, `listManagedAiCredentials`, `getManagedAiCredential`, `updateManagedAiCredential`, `deleteManagedAiCredential`, `setManagedAiCredentialDefault`, `testManagedAiCredentialConnection`

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
| `target_user_ids` | `list[uuid.UUID]` | Min 1; deduplicated preserving order |
| `set_as_default` | `bool` | Default `False` |
| `set_user_sdk_defaults` | `bool` | Default `False` |
| `sdk_default_modes` | `list[str]` | Default `["conversation", "building"]` |

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
| `POST` | `/admin/llm-providers/test-connection?managed_credential_id=` | `AICredentialTestRequest` | `AICredentialTestResult` | When `api_key` blank and `managed_credential_id` given, probes via stored parent key |

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

### `reconcile()` — the heart

```
reconcile(
    session, admin, parent, desired_user_ids,
    *, apply_fields, force, key_rotated
) -> ManagedAICredentialReconcileResult
```

- `desired_user_ids` — deduplicated; compared against `_current_members(parent)` (children keyed by `owner_id`)
- `apply_fields=False` — skip the Update pass (used on create, since there are no pre-existing members to update)
- `key_rotated=True` — forces key write-through in the Update pass even when other scalars are unchanged
- `force=True` — passes `force` to `delete_credential`; blocked members do not block the Remove pass

### `_update_child_fields()` — Update pass detail

Diffs parent scalars against the child (and the child's decrypted data). Only writes when something changed (idempotency). Returns `True` iff the child was mutated.

Clear-through limitation: `update_credential` treats `None` as "leave unchanged", so `base_url`/`model` cannot be cleared back to `None` (for `openai_compatible` both are required anyway). `expiry_notification_date` is cleared directly on the child row when the parent value is `None`.

Admin-curated model metadata write-through: `default_model` and `available_models` are written directly on the child row (bypassing `update_credential` — they are non-secret plain columns, not part of `AICredentialData`). Idempotent: the child is only mutated and flagged as `updated` when the stored values differ from the parent's. `available_models` differentiates `None` (no change) from `[]` (explicit clear) by comparing the exact stored list values — the parent itself carries `None` vs `[]` as the source of truth.

Default flag logic:
- `set_as_default=True` and `child.is_default=False` → `set_default(child, owner)`
- `set_as_default=False` and `child.is_default=True` → `_clear_child_default(child)` (clears profile blob + SDK-default pointers that reference this child)

### `_stamp_child()` — curated model metadata write-through on create

After `create_credential` creates the child, `_stamp_child` also writes `default_model` and `available_models` from the parent directly onto the child row (same pattern as the structural markers). This ensures newly-added members inherit the current curation immediately without a separate reconcile.

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
| `external.account_config.read` | Calling user | `high` | `GET /external/account-config` (successful call only) |

`external.account_config.read` details: `{client_kind, external_client_id, provider_count, credential_ids}`.

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone.

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

*Last updated: 2026-06-14 — admin-curated model list (`default_model` + `available_models`) added via migration `c1a4b2d3e5f6`; SDK resolution, model-health, and native config updated accordingly*
