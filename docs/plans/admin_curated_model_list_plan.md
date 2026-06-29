# Admin-Curated Model List + Default Model on AI Credentials — Implementation Plan

## Overview

Extend the admin-provisioned AI credential feature so a superuser can define, per **Managed AI Credential**, two new pieces of model metadata and have them flow end-to-end:

- **`default_model`** — a single concrete model id the admin wants used by default with this credential.
- **`available_models`** — a curated list of concrete model ids that should be offered for selection with this credential.

These are set on the `ManagedAICredential` parent, reconciled onto each user's child `AICredential` row, and then consumed by (a) the **agent SDK / environment model resolution** and (b) the **native account-config endpoint** (`GET /external/account-config`) so Cinna Desktop/Mobile can offer proper model selection.

### Locked design decisions

1. **Admin-managed only.** Columns live on `AICredential` (so SDK + native paths read them uniformly) but are written **only** through the managed (admin) CRUD + reconcile. They are read-only for the owner, exactly like `is_admin_managed` today. Regular self-created credentials keep today's auto-discovery behavior (their new columns stay `NULL`).
2. **Credential default wins for the SDK.** When an environment uses such a credential and has **no explicit per-mode model override**, the credential's `default_model` overrides the catalog tier default in the lifecycle resolution (affects `MODEL_BUILDING` / `MODEL_CONVERSATION` for Claude Code and the `opencode.json` model field for OpenCode). Precedence: **env per-mode override → credential `default_model` → catalog tier default**.
3. **Curated wins, else discovered.** Wherever a selectable model list is shown (env model-override picker datalist, native `suggested_models`), use `available_models` when non-empty; otherwise fall back to the auto-discovered `discovered_models`.
4. **Single default model.** One `default_model` applies to both agent modes and the native chat mode (no per-mode split).

### High-level flow

```
Superuser (Admin → LLM Providers dialog)
   │  default_model + available_models  (typed, or pulled from Test Connection probe)
   ▼
ManagedAICredential (parent, source of truth)  ──reconcile──▶  AICredential children (per user)
                                                                    │
                  ┌─────────────────────────────────────────────────┼──────────────────────────────┐
                  ▼                                                   ▼                              ▼
   Environment model resolution                       GET /external/account-config         User Settings UI
   (resolve_model override fallback)                  (model + suggested_models)            (read-only display)
   → MODEL_BUILDING/CONVERSATION (Claude Code)        → native client model selection
   → opencode.json model (OpenCode)
```

---

## Architecture Overview

### Components touched

| Layer | File | Change |
|-------|------|--------|
| Models | `backend/app/models/credentials/managed_ai_credential.py` | Add `default_model` + `available_models` to table + Create/Update/Public DTOs |
| Models | `backend/app/models/credentials/ai_credential.py` | Add `default_model` + `available_models` columns; project on `AICredentialPublic` (read-only) + `AdminAICredentialPublic` |
| Migration | `backend/app/alembic/versions/<rev>_add_curated_models_to_ai_credential.py` | New migration, `down_revision = 'd3782dd039a5'` |
| Service (admin) | `backend/app/services/credentials/managed_ai_credentials_service.py` | Persist on parent create/update; write-through to children in `_add_child` + `_update_child_fields`; project in `_to_public` |
| Service (SDK) | `backend/app/services/environments/environment_lifecycle.py` + `sdk_constants.py` | Thread per-mode credential `default_model` into the bag; use as override fallback in `_generate_env_file` / `_generate_opencode_config_files` |
| Service (SDK) | `backend/app/services/environments/environment_service.py` | Capture per-mode `default_model` when resolving the per-mode credential into the bag |
| Service (health) | `backend/app/services/environments/model_health_service.py` | Apply the same credential-default precedence when computing the effective model (avoid false `unknown_model`/`stale_default` flags) |
| Service (native) | `backend/app/services/external/external_account_config_service.py` | New resolution chain for `model`; curated-else-discovered for `suggested_models` |
| Route | `backend/app/api/routes/admin_llm_providers.py` | No new endpoints; the existing create/update bodies carry the new fields automatically via DTOs |
| Frontend (admin) | `frontend/src/components/Admin/LlmProviders/ManagedCredentialDialog.tsx` | Add "Default model" + "Available models" inputs; helper to pull from Test Connection result |
| Frontend (env) | `frontend/src/components/Environments/EnvironmentConfigForm.tsx` | Prefer linked credential's `available_models` for the model-override datalist |
| Frontend (user) | `frontend/src/components/UserSettings/AICredentials.tsx` | Prefer `available_models` for the datalist; (optional) show curated default read-only |
| Client | `frontend/src/client/*` | Regenerate from OpenAPI |
| Docs | `docs/application/ai_credentials/admin_ai_credential_provisioning(.md/_tech.md)` + `model_freshness` cross-ref | Update |

### Storage format convention (important)

Both new fields store **bare concrete provider model ids** (no `provider/` prefix, no SDK tier words). Examples: `claude-sonnet-4-6`, `gpt-5.4-mini`, `gemini-2.5-pro`.

- On input, strip any leading `provider/` prefix (reuse `_strip_provider_prefix` from `model_catalog.py`) so storage is normalized.
- This format is directly consumable by: native clients (provider API ids), the Claude Code adapter (`options.model` accepts concrete ids), and the OpenCode config builder (`_build_provider_config` already handles both bare and prefixed model ids — it splits on `/`).

---

## Data Models

### `managed_ai_credential` (parent) — new columns

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `default_model` | `VARCHAR(255)` | nullable | Admin's preferred default model (bare concrete id). `NULL` = no curated default (catalog default applies). |
| `available_models` | `JSON` | nullable | Admin-curated list of selectable model ids. `NULL`/empty = fall back to per-credential auto-discovery. |

### `ai_credential` (child) — new columns

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `default_model` | `VARCHAR(255)` | nullable | Mirror of the parent value, written through by reconcile. Read by SDK resolution + native config. |
| `available_models` | `JSON` | nullable | Mirror of the parent value, written through by reconcile. Read by model pickers + native config. |

- **No FK / index changes.** These are plain non-secret scalar/JSON columns.
- **No backfill.** Existing rows get `NULL` (preserves current behavior: catalog default + discovered list).
- Distinct from the existing `model` column (openai_compatible's required model) and `discovered_models` (auto cron cache). `default_model` is the **admin choice**; `discovered_models` is **what the key can see**; `available_models` is the **admin-curated subset offered for selection**.

### DTO changes

**`ManagedAICredentialCreate` / `ManagedAICredentialUpdate`** — add:
- `default_model: str | None = None` (max 255)
- `available_models: list[str] | None = None`

**`ManagedAICredentialPublic`** — add `default_model: str | None`, `available_models: list[str] | None`.

**`AICredentialPublic`** (owner-facing, read-only) — add `default_model: str | None = None`, `available_models: list[str] | None = None`. These are non-secret and safe to expose; the owner cannot mutate them (no corresponding fields on `AICredentialCreate`/`AICredentialUpdate`).

**`AdminAICredentialPublic`** — inherits the two new fields from `AICredentialPublic`.

**`AccountConfigProviderPublic`** — **no new fields required.** The existing `model` (resolved default) and `suggested_models` (the list) already carry the data; only their resolution logic changes (see Native section). Optionally add a boolean `models_curated: bool` if the native team wants to distinguish admin-curated lists from discovered ones — leave out of MVP unless requested.

---

## Security Architecture

- **Write path is admin-only.** The two new fields are absent from `AICredentialCreate`/`AICredentialUpdate`, so user routes cannot set them. They are only ever written by `ManagedAICredentialsService` (superuser-gated via `get_current_active_superuser`) during reconcile. This matches the existing `is_admin_managed` read-only guard — no new guard code is needed because the fields simply aren't in the user-facing input schemas.
- **Non-secret.** Both are model identifiers, never key material. Safe to project on `AICredentialPublic` and to ship in account-config.
- **Account-config unchanged on the secret boundary.** The native endpoint still returns the decrypted key under the same native-token gate + audit + `no-store`; this change only affects the non-secret `model`/`suggested_models` fields.
- **Input validation.** Strip `provider/` prefixes and trim entries on the way in; drop empties; cap `available_models` length (e.g. 100) and per-entry length (255) to bound payload size. Reject nothing hard — curation is advisory.

---

## Backend Implementation

### 1. Migration

`backend/app/alembic/versions/<rev>_add_curated_models_to_ai_credential.py`

- `down_revision = 'd3782dd039a5'` (current single head — verified).
- `upgrade()`:
  - `op.add_column('managed_ai_credential', sa.Column('default_model', sa.String(length=255), nullable=True))`
  - `op.add_column('managed_ai_credential', sa.Column('available_models', sa.JSON(), nullable=True))`
  - `op.add_column('ai_credential', sa.Column('default_model', sa.String(length=255), nullable=True))`
  - `op.add_column('ai_credential', sa.Column('available_models', sa.JSON(), nullable=True))`
- `downgrade()`: drop the four columns in reverse.
- **Hand-trim the 6 spurious autogen type drifts** documented in prior migrations (`ai_credential.models_discovered_at`, `credential.service_uri`, `mcp_connector.allowed_user_ids`, `user_trusted_device.{expires,created,last_used}_at`) — do not let autogen include them.
- No data backfill.

### 2. Managed service write-through (`managed_ai_credentials_service.py`)

- **`create()`**: pass `default_model` / `available_models` into the new `ManagedAICredential(...)` constructor (normalize/strip first).
- **`update()`**: apply `data.default_model` / `data.available_models` to the parent before reconcile (treat the same way as `base_url`/`model` partial updates; for `available_models`, an explicitly-supplied empty list `[]` means "clear curation" — distinguish `None` (no change) from `[]` (clear)).
- **`_add_child()` / `_stamp_child()`**: after the child is created via `create_credential`, set `child.default_model = parent.default_model` and `child.available_models = parent.available_models` directly on the row (non-secret columns; no `update_credential` round-trip needed). Commit with the existing stamp.
- **`_update_child_fields()`**: extend the diff to detect `default_model` / `available_models` changes and write them directly on the child row (parallel to the existing `expiry` clear-through that bypasses `update_credential`). Count a change so the member shows up in `updated`. Idempotent: no write when equal.
- **`_to_public()`**: project `default_model` / `available_models` from the parent.
- These fields are **not** encrypted and **not** part of `_encrypt_key` / `AICredentialData` — they live as plain columns, exactly like `discovered_models`.

### 3. SDK / environment resolution (the "credential default wins" path)

The merged credential **bag** (`sdk_constants.make_empty_credential_bag()`) is shared across both modes; per-mode model resolution in the lifecycle picks by provider. Add **per-mode default-model carriers** so each mode can apply its own resolved credential's `default_model`.

- **`sdk_constants.py`**:
  - Add two keys to `make_empty_credential_bag()`: `"model_default_conversation": None`, `"model_default_building": None`.
  - (`apply_credential_to_bag` stays mode-agnostic for keys; the per-mode default is set by the resolver sites that know which mode a credential serves — see below.)
- **`environment_service.create_environment` (~L534–695)** and **`environment_lifecycle` reconfigure/rebuild bag-build sites (~L1600–1680, and the fallback-fill helpers `_fallback_fill_bag_for_sdk` / `_fallback_for_mode`)**: at each point where a **per-mode** credential (conversation vs building) is resolved into the bag, also capture that `AICredential.default_model` into `bag["model_default_conversation"]` / `bag["model_default_building"]`. The `AICredential` row is in scope at these resolution points (the bag is built from decrypted rows). For type-level-default fallbacks where only `AICredentialData` is available, look up the row's `default_model` alongside (the row is fetched to decrypt it).
- **`_generate_env_file` (Claude Code) `_resolve_mode_model` (~L1825)**: change the override fallback to
  `override = env_per_mode_override or model_default_for_mode`
  before calling `resolve_model(...)`. Thread `model_default_conversation` / `model_default_building` into `_generate_env_file` as params (mirroring `openai_compatible_model`).
- **`_generate_opencode_config_files._build_config` (~L2132)**: same change — `model_override = env_per_mode_override or model_default_for_mode`, then `resolve_model(...)`. Thread the two per-mode values into `_generate_opencode_config_files`.
- **MiniMax (`_generate_minimax_settings_files`)**: MiniMax is disabled in the UI and managed credentials don't expose it; leave as-is (it will simply never have a `default_model`). Optionally apply the same `override or default` for completeness, but not required for MVP.
- **Net effect**: `resolve_model`'s contract is unchanged (override honored verbatim). The credential default is injected *as* the override only when the env has no explicit per-mode override — preserving env overrides as the top precedence.

### 4. Model-health consistency (`model_health_service.py`)

`_evaluate_mode` recomputes the effective model to classify health. It must apply the **same** precedence (env override → credential `default_model` → catalog) so a perfectly valid admin default isn't flagged `unknown_model` / `stale_default`. Where `_evaluate_mode` resolves the effective model, consult the resolved credential's `default_model` before falling back to the catalog default. (If the credential default is a concrete id present in `discovered_models`, it classifies `ok` as expected.)

### 5. Native account-config (`external_account_config_service.py`)

- **`_to_provider`**: build `suggested_models` as `credential.available_models or credential.discovered_models or []` (curated wins, else discovered).
- **`_resolve_model`**: new precedence chain (single default wins early):
  1. `credential.default_model` (admin curated) — when set.
  2. `credential.model` (openai_compatible's required model) — when set.
  3. First entry of `available_models` (curated), prefix-stripped.
  4. First entry of `discovered_models`, prefix-stripped.
  5. Catalog default for the provider, **only if concrete** (`is_known_word` → drop tier words).
  6. `None`.
- Keep the existing skip-on-undecryptable-row behavior. No change to the audit/gate.

### 6. Route layer

No new endpoints. `POST`/`PATCH /admin/llm-providers/` already bind `ManagedAICredentialCreate`/`Update`; the two new optional fields ride those bodies. The reconcile result and `ManagedAICredentialPublic` carry them back automatically.

---

## Frontend Implementation

### Admin dialog — `ManagedCredentialDialog.tsx`

Add two fields to the form (shown for **all** managed provider types, not just openai_compatible):

- **Default model** — text input. Placeholder shows a provider-appropriate example. Optional. Help text: "The model used by default with this credential, across agents and native apps. Leave blank to use the platform default."
- **Available models** — a simple tag/multi-entry editor (comma- or newline-separated text input is acceptable for MVP; render parsed chips). Optional. Help text: "Models offered for selection with this credential. Leave empty to offer all auto-detected models."
- **"Use models from test"** affordance: after a successful **Test Connection** that returns `models`, show a button to populate `available_models` from `testResult.models` (and optionally set `default_model` to the first). This makes curation one click for the common case.
- Extend `baseFormSchema` with `default_model: z.string().optional()` and `available_models: z.array(z.string())` (or a string the submit handler parses). In `recordToFormData`, seed both from the edit record.
- In `onSubmit`, include `default_model` (trimmed, `undefined` if blank) and `available_models` (parsed, deduped, prefix-stripped client-side for nicer display; backend re-normalizes). For edit, send `available_models: []` to explicitly clear, `undefined` to leave unchanged — mirror the existing base_url/model clear semantics.

### Env model-override picker — `EnvironmentConfigForm.tsx`

The model-override `<datalist>` currently uses `selectedCredential?.discovered_models ?? []`. Change to prefer the curated list:
`const modelOptions = selectedCredential?.available_models?.length ? selectedCredential.available_models : (selectedCredential?.discovered_models ?? [])`.
Keep it a free-text input with datalist suggestions (do not hard-restrict) so the "credential default wins" runtime path remains the source of truth and power users keep flexibility.

### User Settings — `AICredentials.tsx`

- Same datalist preference change (`available_models` else `discovered_models`).
- Optional: render the admin `default_model` as a small read-only line on admin-managed credential cards ("Default model: …"), next to the existing "Managed" badge. Read-only — no edit control.

### Client regeneration

After backend DTO changes: `source ./backend/.venv/bin/activate && make gen-client` (or `bash scripts/generate-client.sh`). `AdminLlmProvidersService` and the credential public types pick up the new fields automatically.

---

## Error Handling & Edge Cases

- **`available_models` explicit-clear vs no-change**: `None` = leave unchanged; `[]` = clear curation (fall back to discovered). The `update()` partial-apply must distinguish these. Document in the service.
- **Default not in available list**: allowed. `default_model` need not be a member of `available_models` (admin may default to a model they didn't list for manual selection). No validation coupling.
- **Provider-prefixed input**: normalize on the way in (strip `provider/`). Avoids double-prefix bugs in the OpenCode builder.
- **Tier words as default**: if an admin types `sonnet`/`haiku`, store as-is; the Claude Code adapter resolves it. But native account-config drops tier words (`is_known_word`) → native `model` falls through to the next chain step. This is the same behavior as today's catalog tier-word handling; surface a soft hint in the dialog ("Use a concrete model id for native apps").
- **Mode mismatch**: a single `default_model` applied to both conversation (FAST) and building (BALANCED) is intentional per the locked decision. Note in docs that this overrides the fast/balanced tiering when set.
- **Reconcile idempotency**: equal `default_model`/`available_models` must not flag a child as `updated` or emit an audit event.
- **Self-created credentials**: columns stay `NULL`; every consumer falls back to today's behavior. No regression.
- **OpenAI-compatible**: `default_model` defaults conceptually to the existing `model` if the admin leaves it blank (native chain step 2 covers this); no special-casing needed.

---

## Integration Points

- **AI Credentials core** (`ai_credentials_service`): write-through bypasses `update_credential` for these non-secret columns (like the existing expiry clear-through). The decrypted `AICredentialData` is **not** extended.
- **Model catalog** (`model_catalog.py`): reused unchanged (`resolve_model`, `_strip_provider_prefix`, `is_known_word`). The credential default is injected as the `override` arg upstream.
- **Model freshness** (`model_health_service`): must mirror the new precedence (see Backend §4) to avoid false amber badges.
- **Native account-config / external agent access**: resolution-only change; confirm the `model` contract with the desktop/native team (the memo flag "confirm exact model contract" still applies).
- **Client regen** required after backend change.

---

## Future Enhancements (Out of Scope)

- Per-mode admin defaults (conversation vs building) — explicitly deferred (single default chosen).
- Hard-restricting env/native model selection to `available_models` (currently advisory suggestions).
- Letting regular users curate their own credential's default/available list.
- A `models_curated` flag on the native descriptor to visually distinguish curated vs discovered lists.
- Validating `default_model` against the provider's live model list at save time (today it's advisory; Test Connection already surfaces the live list).

---

## Summary Checklist

### Backend
- [ ] Add `default_model` (`VARCHAR(255)` null) + `available_models` (`JSON` null) to `ManagedAICredential` table.
- [ ] Add the same two columns to `AICredential` table.
- [ ] Add the fields to `ManagedAICredentialCreate`, `ManagedAICredentialUpdate`, `ManagedAICredentialPublic`.
- [ ] Project `default_model` / `available_models` on `AICredentialPublic` (read-only) and `AdminAICredentialPublic`.
- [ ] Migration `<rev>` with `down_revision='d3782dd039a5'`; trim the 6 spurious autogen drifts; no backfill.
- [ ] `managed_ai_credentials_service`: persist on `create`/`update`; write-through in `_add_child` + `_update_child_fields` (with `None` vs `[]` clear semantics); project in `_to_public`; normalize/strip + idempotent diff.
- [ ] `sdk_constants`: add `model_default_conversation` / `model_default_building` bag keys.
- [ ] `environment_service` + `environment_lifecycle`: capture per-mode credential `default_model` into the bag at all resolution sites (create, reconfigure, rebuild, fallback-fill).
- [ ] `_generate_env_file` + `_generate_opencode_config_files`: use `override = env_override or credential_default` before `resolve_model`.
- [ ] `model_health_service._evaluate_mode`: apply the same default precedence.
- [ ] `external_account_config_service`: new `_resolve_model` chain + curated-else-discovered `suggested_models`.

### Frontend
- [ ] `ManagedCredentialDialog`: add Default model + Available models inputs; "use models from Test Connection" helper; schema + submit (clear vs no-change) + edit seeding.
- [ ] `EnvironmentConfigForm`: prefer `available_models` over `discovered_models` for the model-override datalist.
- [ ] `AICredentials`: same datalist preference; optional read-only default-model line on managed cards.
- [ ] Regenerate the API client.

### Testing & validation
- [ ] Reconcile writes `default_model`/`available_models` to new + existing children; idempotent on no-op; `[]` clears, `None` leaves unchanged.
- [ ] Owner cannot set these via user CRUD (fields absent from input schema); they appear read-only on `AICredentialPublic`.
- [ ] Env using a managed credential with `default_model` and **no** per-mode override resolves to the credential default for both Claude Code (`MODEL_BUILDING`/`MODEL_CONVERSATION`) and OpenCode (`opencode.json` model).
- [ ] Env per-mode override still wins over the credential default.
- [ ] `model_health` does not flag a valid credential default as `unknown_model`.
- [ ] `GET /external/account-config` returns `model` = admin default and `suggested_models` = curated list (falls back to discovered when unset); tier-word defaults drop to the next chain step.
- [ ] Self-created credentials (NULL columns) behave exactly as before (regression).
- [ ] Full domain suites green: `tests/api/ai_credentials/`, `tests/api/external/`, and the environment/model-freshness tests.

---

*Plan authored for the cinna-core admin-provisioned AI credentials feature. Builds on `admin_ai_credential_provisioning` (parent/child reconcile, migration `d3782dd039a5`) and `model_freshness` (catalog + discovery).*
