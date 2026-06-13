# Admin-Provisioned AI Credentials + Native Account-Config

## Purpose

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

- **Part A — Admin-provisioned AI credentials.** A superuser creates AI credentials *on behalf of* target users. The credential is owned by the user, so it participates automatically in all existing per-user plumbing (default resolution, environment creation, agent-log visibility, listing) with no extra wiring. The user can use the credential and set it as their default, but cannot edit, delete, or re-key it — those operations return `403`.
- **Part B — Native account-config endpoint.** A native-token-gated endpoint (`GET /api/v1/external/account-config`) returns the caller's own usable AI credentials with the *decrypted* API key, so Cinna Desktop and Cinna Mobile can auto-create local "LLM providers" and a suggested chat mode per credential on login, without the user having to copy-paste keys into the native app.

> **Frontend status:** The admin "LLM Providers" section is **implemented** — superusers can provision, list, filter, edit, set-default, and delete managed credentials from the web UI. The native-app side (Part B) remains a future task: only the backend `GET /external/account-config` endpoint exists; Cinna Desktop/Mobile provider auto-creation and the read-only badge on admin-managed rows in the user-facing AI Credentials card are not yet built.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **Admin-managed AI credential** | An `AICredential` row with `is_admin_managed=True` and `managed_by_id` set to the provisioning admin. Owned by the target user; read-only through the user-facing CRUD. |
| **Per-user provisioning** | The admin creates a separate, independent row per target user (`owner_id = target.id`). Keys are shared in the create request but each row is fully independent at rest. This is not key-sharing: the user is the owner. |
| **Account config** | The native-client bundle: a list of provider descriptors (with decrypted API keys) that Cinna Desktop / Mobile uses to create local LLM providers on login. |
| **Native token gate** | The `account-config` endpoint is only accessible to JWTs whose `client_kind` claim is `"desktop"` or `"mobile"`. Plain web-session JWTs are rejected `403`. |

---

## Part A — Admin Provisioning

### How it works

A superuser uses the `POST /api/v1/admin/llm-providers/` endpoint to create one `AICredential` per target user. The provisioning call accepts a list of `target_user_ids`; invalid or inactive users are returned in a `skipped` list rather than failing the whole call.

Each created row has:
- `owner_id = target_user.id` — the row participates in all existing per-user logic
- `is_admin_managed = True` — the single behavioral flag that makes the row read-only for the owner
- `managed_by_id = admin.id` — audit-only, never used in an access decision; SET NULL when the admin account is deleted so the user keeps their credential

### What the admin can set at provision time

| Option | Effect |
|--------|--------|
| `set_as_default = True` | Calls `set_default` for the target user, including profile auto-sync (`ai_credentials_encrypted`) — exactly what the user would do in Settings. |
| `set_user_sdk_defaults = True` | Wires the user's `default_sdk_conversation` / `default_sdk_building` and `default_ai_credential_*_id` to the new credential, using the same SDK composition as the Add Environment dialog (`claude-code` for Anthropic/MiniMax, `opencode/<provider>` for OpenAI/Google/OpenAI-Compatible). |
| `sdk_default_modes` | List of modes to wire: `"conversation"`, `"building"`, or both (default). Modes whose composed SDK engine is incompatible with the credential type are silently skipped. |

### User experience after provisioning

The user sees the credential in **Settings → AI Credentials** with `is_admin_managed: true` in the API projection. A lock/badge ("Managed by your administrator") is not yet rendered in the user-facing UI — that is a future task. No edit, delete, or re-key affordances appear regardless, because the backend enforces the read-only guard.

- User **can**: use the credential in environments, set it as their default, see it in model-override selectors.
- User **cannot**: update the name/key/base_url, change the model field, or delete it. Attempts return `403 "This credential is managed by your administrator and cannot be modified."`.

Because the row is owned by the user, **web environment creation resolves it automatically** via the existing default-resolution and credential-linking pipelines — no extra steps.

### Admin CRUD after provisioning

Through the `/admin/llm-providers/` surface, superusers can:
- **List** all admin-managed credentials fleet-wide (all superusers see all managed rows), optionally filtered by `?target_user_id=`
- **Update** a credential's key/name on behalf of its owner (the admin `admin_override=True` path bypasses the user read-only guard)
- **Delete** a managed credential — blocked `409` when one or more published bundles reference it as a publisher-provided AI credential, unless `force=true`
- **Set default** for the credential's owner-user

Mutations that reach a non-admin-managed row through this surface return `404`.

### Admin UI — "LLM Providers" section

The admin UI is accessed via **Admin menu → LLM Providers** (`/admin/llm-providers`) in the sidebar. The route is superuser-gated: non-superusers are redirected to `/` by `beforeLoad`.

**User flow:**

1. The page loads a fleet-wide table of all admin-managed credentials (ordered by created date descending). Each row shows: target user (owner display label), credential name, provider badge, default badge, and created date.
2. The admin can **filter** the table to a single target user via a `UserAllowlistPicker` in the page header. A "Clear filter" button appears when a filter is active.
3. Clicking **"Provision Credential"** opens a dialog (`ProvisionLlmProviderDialog`) with:
   - Name (free-form)
   - Provider type (dropdown over the selectable `AICredentialType` values: Anthropic, OpenAI, OpenAI Compatible, Google — MiniMax is temporarily disabled in the UI and no longer offered)
   - API key (password field)
   - Base URL — shown only for `openai_compatible` (required) and `google` (optional)
   - Model — shown only for `openai_compatible` (required)
   - Target Users — multi-select `UserAllowlistPicker`; at least one user required
   - "Set as default" toggle
   - "Set user SDK defaults" toggle
   - On success the dialog surfaces a success toast with the created count, plus per-target skip toasts with the skip reason (e.g., `user_not_found`)
4. Each row has a three-dot actions menu (`LlmProviderActionsMenu`) with:
   - **Edit** — opens an inline dialog; edits name, API key (leave blank to keep existing key), and base\_url/model if applicable
   - **Set as default** — disabled when `is_default` is already `true`
   - **Delete** — opens an `AlertDialog` confirmation before calling the delete endpoint

**Known limitation:** owner display labels in the table resolve from `GET /users/` (the first 100 users, via `UsersService.readUsers({ skip: 0, limit: 100 })`). Owners outside this first page fall back to a shortened UUID (`<first-8-chars>…`). The actively-filtered user is always labeled correctly because its label is carried from the `UserAllowlistPicker` selection. No id→label batch endpoint exists today to resolve all possible owners.

### Security audit

Every provision and mutation writes a `SecurityEvent` with `severity="medium"` containing credential ID, target user ID, type, and admin ID — but **never** key bytes.

On `POST /admin/llm-providers/`:
- One `admin.ai_credential.provision` event **per provisioned row** (scoped to the target user, for per-user audit trails)
- One `admin.ai_credential.provision_batch` event scoped to the admin (batch summary: counts + credential IDs)

---

## Part B — Native Account-Config Endpoint

### Purpose and rationale

Cinna Desktop and Cinna Mobile need the user's LLM provider API keys to make direct calls to the provider API (e.g., Anthropic). Without this endpoint, users must copy-paste keys from the web Settings into the native app — a friction point that breaks the "ready on login" goal.

This is a **deliberate, product-approved, scoped relaxation** of the platform's "keys never exposed" invariant. The relaxation is acceptable because:
1. The client already holds a desktop/mobile OAuth token (a privileged, device-bound credential)
2. The user's key is delivered only to their own device
3. The call is fully audited with a high-severity `SecurityEvent`
4. No intermediate proxy, cache, or log ever touches the key bytes

### What the endpoint returns

`GET /api/v1/external/account-config` returns a list of **provider descriptors**, one per owned AI credential, including:

| Field | Description |
|-------|-------------|
| `credential_id` | The platform credential row ID |
| `provider_type` | `anthropic`, `openai`, `google`, `openai_compatible`, `minimax` |
| `display_name` | Human-readable name — `"Claude"`, `"OpenAI"`, `"Gemini"`, `"MiniMax"`, or (for `openai_compatible`) the credential's own name |
| `descriptor_slug` | Stable slug for the local provider ID — `"claude"`, `"openai"`, `"gemini"`, `"minimax"`, `"openai-compatible"` |
| `api_key` | **Decrypted key** — the only endpoint in the platform to return this |
| `base_url` | Endpoint override (used by `openai_compatible` and `google`) |
| `model` | Suggested concrete model ID (see model resolution below) |
| `is_default` | Whether this is the user's default for its type |
| `is_admin_managed` | Whether this was provisioned by an admin |
| `default_chat_mode_label` | Label the native app should use for the auto-created chat mode (equals `display_name`) |
| `suggested_models` | Full list from `discovered_models` (the per-credential discovery cache) |

The response also carries `default_provider_credential_id` (the resolved conversation-default credential for the user, using the existing priority resolution) and `generated_at`.

### Model resolution for native clients

Native clients call the provider API directly with the decrypted key, so they need a **concrete, provider-usable model ID** — not an SDK-internal tier word (e.g., `"haiku"`, `"sonnet"` are Claude Code internal shortcuts that are not valid Anthropic API model IDs).

Resolution order:
1. `credential.model` when explicitly set (e.g., `openai_compatible` with a pinned model)
2. First entry in `credential.discovered_models` (the nightly-refreshed list of models this key can actually access), stripped of any `provider/` prefix
3. The model-catalog default for this provider/engine — but only when it is a **concrete ID**; tier words (`"haiku"`, `"sonnet"`, `"opus"`) are dropped and the field becomes `null`
4. `null` — the client falls back to its own default or lets the user pick from `suggested_models`

### Security boundary (all four constraints are enforced)

1. **Native-token gated.** `client_kind in {"desktop", "mobile"}` required. Web JWTs (`client_kind` absent) → `403`. Revoked desktop clients → `401` (via the existing `get_current_user` desktop revocation check).
2. **Strictly self-scoped.** Returns only `AICredential` rows with `owner_id == user.id`. Credentials shared *with* the user via `AICredentialShare` are deliberately excluded — they belong to another user.
3. **Audited.** Every successful call writes `SecurityEvent(event_type="external.account_config.read", severity="high", details={client_kind, external_client_id, provider_count, credential_ids})`. No key material is logged.
4. **No caching.** Response includes `Cache-Control: no-store`.

---

## Business Rules

### Admin provisioning

- **Superuser-only.** `get_current_active_superuser` dependency (same gate as Knowledge Sources and Admin Environments).
- **Per-user rows, not key sharing.** One independent `AICredential` row is created per target user. The key bytes from the provision request are used to create each row independently; the rows are not linked to each other.
- **Invalid targets skipped, not errored.** Unknown or inactive users appear in `skipped`; the remaining valid targets are provisioned.
- **One-default-per-type invariant preserved.** If `set_as_default=True` and the target user already has a default of that type, the existing default is unset (existing `set_default` behavior). The user ends up with one default.
- **Blast-radius gate on delete.** Deleting an admin-managed credential that is referenced by a published bundle as a publisher-provided credential is blocked `409` unless `force=true`. Reuses the existing `AICredentialInUseError` Tier-2 gate.
- **Admin deletion preserves user's credential.** When the provisioning admin's user account is deleted, `managed_by_id` is set to `NULL` (`SET NULL` FK) — the user keeps their credential.
- **Non-admin-managed rows are not reachable.** `update_managed`, `delete_managed`, `set_managed_default` return `404` if the credential exists but `is_admin_managed=False`.
- **`set_default` is open to the owner.** Setting an admin-managed credential as one's default is a read-only use, not a modification. The owner may do this freely.

### Native account-config

- Owner-only scope (shares excluded — see security boundary above).
- Empty credentials → `200` with `providers: []`.
- An undecryptable credential row is skipped (warning logged, call does not fail) so a single corrupt row cannot block login bootstrap.

---

## Architecture

```
Superuser (web)
   │
   │  POST /api/v1/admin/llm-providers/
   ▼
AdminAICredentialService.provision_for_users()
   ├── For each target_user_id: validate user exists + is active
   ├── AICredentialsService.create_credential(user_id=target.id, ...)
   │     [existing encryption, type validation, default-per-type logic]
   ├── Stamp is_admin_managed=True, managed_by_id=admin.id
   ├── Optional: AICredentialsService.set_default(credential.id, target.id)
   │     [profile auto-sync runs for the target user]
   └── Optional: write target.default_sdk_* + default_ai_credential_*_id

SecurityEvent("admin.ai_credential.provision")  [per row, scoped to target user]
SecurityEvent("admin.ai_credential.provision_batch")  [scoped to admin]


Native Client (Cinna Desktop / Mobile)
   │
   │  GET /api/v1/external/account-config  [desktop/mobile JWT]
   ▼
client_kind gate (403 for web JWTs)
   │
   ▼
ExternalAccountConfigService.build_config(user)
   ├── SELECT ai_credential WHERE owner_id = user.id ORDER BY created_at ASC
   ├── For each credential:
   │     AICredentialsService.decrypt_credential() → AICredentialData
   │     Map provider_type → (display_name, descriptor_slug)
   │     Resolve model (credential.model → discovered_models → catalog → None)
   │     Build AccountConfigProviderPublic(api_key=decrypted_key, ...)
   └── resolve_default_credential_for_sdk → default_provider_credential_id
   │
response.headers["Cache-Control"] = "no-store"
SecurityEvent("external.account_config.read", severity="high")
   [counts + credential ids, NO key material]
```

---

## Integration Points

- **[AI Credentials](ai_credentials.md)** — the reused core service: `create_credential`, `set_default`, `update_credential`, `delete_credential`, `decrypt_credential`, `resolve_default_credential_for_sdk`. Admin service delegates with `user_id = owner_id` so all per-user invariants run for the target.
- **[AI Credentials Tech](ai_credentials_tech.md)** — `AICredential` model with the two new columns; `AICredentialsService.update_credential` / `delete_credential` `admin_override` kwarg; `AICredentialPublic.is_admin_managed` projection; `AdminAICredentialPublic` (admin-only projection adds `owner_id` + `managed_by_id`).
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the `/external/` route namespace that the account-config endpoint extends. The same `ExternalAccountConfigService` sits under `services/external/`.
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the desktop/mobile JWTs with `client_kind` and `external_client_id` claims. The live revocation check in `get_current_user` ensures revoked device tokens are rejected `401` before the native gate runs.
- **[User Roles](../user_roles/user_roles.md)** — `get_current_active_superuser` gates the admin surface. Only superusers may provision credentials for other users.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the blast-radius gate on admin credential deletion (`AICredentialInUseError`, Tier-2, published bundle references) is the same mechanism used by the regular credential deletion guard.

---

*Last updated: 2026-06-13 — admin UI (LLM Providers section) shipped; native account-config backend-only*
