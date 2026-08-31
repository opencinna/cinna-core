# Admin-Provisioned AI Credentials + Native Account-Config

## Purpose

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

- **Part A — Admin-provisioned AI credentials.** A superuser creates a single **Managed AI Credential** parent record and assigns it to one or more target users. The parent record is the canonical source of truth (name, type, encrypted key, default flags). The service reconciles the desired target-user set into per-user `AICredential` child rows, which participate automatically in all existing per-user plumbing. Users can use their child credential and set it as their default, but cannot edit, delete, or re-key it — those operations return `403`.
- **Part B — Native account-config endpoint.** A native-token-gated endpoint (`GET /api/v1/external/account-config`) returns the caller's own usable AI credentials with the *decrypted* API key, so Cinna Desktop and Cinna Mobile can auto-create local "LLM providers" and a suggested chat mode per credential on login, without the user having to copy-paste keys into the native app.

> **Frontend status:** The admin "LLM Providers" section is **implemented** — superusers can provision, list, filter, edit, set-default, delete, and set an admin-curated model list (default model + available models) from the web UI. The user-facing AI Credentials card renders admin-managed child credentials with a **"Managed" badge** and a read-only **Default model** line when a curated default is set. The native-app side (Part B) backend is complete; Cinna Desktop/Mobile provider auto-creation is not yet built.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **Managed AI Credential (parent)** | A `ManagedAICredential` row owned by a superuser. Holds the canonical config (name, type, Fernet-encrypted key, base_url/model, default flags) and is reconciled into child rows. Membership is derived from the children — there is no `target_user_ids` column on the parent. |
| **Child credential** | An ordinary `AICredential` row with `is_admin_managed=True`, `managed_credential_id` pointing at the parent, and `owner_id` set to the target user. Participates in all existing per-user plumbing; read-only through the user-facing CRUD. |
| **Reconcile** | The diff-and-converge operation: Add (desired − current) creates children; Remove (current − desired) deletes them (Tier-2 blast-radius gated); Update (intersection) writes changed parent fields and/or rotated key through to each child and applies/clears the default flag. Idempotent — a no-op desired set with unchanged fields produces empty added/removed/updated lists. |
| **Account config** | The native-client bundle: a list of provider descriptors (with decrypted API keys) that Cinna Desktop / Mobile uses to create local LLM providers on login. |
| **Native token gate** | The `account-config` endpoint is only accessible to JWTs whose `client_kind` claim is `"desktop"` or `"mobile"`. Plain web-session JWTs are rejected `403`. |

---

## Part A — Admin Provisioning

### How it works

A superuser uses `POST /api/v1/admin/llm-providers/` to create a **parent record** and reconcile it into one child `AICredential` per valid target user. Subsequent edits (via `PATCH`) update the parent and re-reconcile: the service diffs the new desired membership against the existing children and adds, removes, or updates as needed.

The parent record stores:
- Its own Fernet-encrypted canonical key — so adding new members or rotating the key never requires re-entering the secret
- `managed_by_id` (audit FK, SET NULL on admin deletion — the parent stays fleet-manageable by any superuser)
- The desired default flags (`set_as_default`, `set_user_sdk_defaults`, `sdk_default_modes`)

Each child row carries:
- `owner_id = target_user.id` — the row participates in all existing per-user logic
- `is_admin_managed = True` — the single behavioral flag that makes the row read-only for the owner
- `managed_by_id = parent.managed_by_id` — audit-only, SET NULL when the admin account is deleted
- `managed_credential_id = parent.id` — structural link; SET NULL if the parent is ever deleted out-of-band (children degrade to plain `is_admin_managed` orphans rather than vanishing)

Membership is **derived** from the children (those whose `managed_credential_id` points at the parent). There is no `target_user_ids` column on the parent row — the desired set is supplied at create/update time and reconciled, not stored separately.

### What the admin can set

| Option | Effect |
|--------|--------|
| `set_as_default = True` | Calls `set_default` for each member, including profile auto-sync (`ai_credentials_encrypted`). A PATCH that changes this flag applies it (or clears it) for every existing member in the Update pass of the reconcile. |
| `set_user_sdk_defaults = True` | Wires each owner's `default_sdk_conversation` / `default_sdk_building` and `default_ai_credential_*_id` to their child, using the same SDK composition as the Add Environment dialog (`claude-code` for Anthropic/MiniMax, `opencode/<provider>` for OpenAI/Google/OpenAI-Compatible). |
| `sdk_default_modes` | Modes to wire: `"conversation"`, `"building"`, or both (default). Modes incompatible with the credential type are silently skipped. |
| `default_model` | Admin-curated preferred model ID (bare concrete id, e.g. `claude-sonnet-4-6`). Reconciled onto each child row. When set and the environment has no per-mode override, this model is used instead of the catalog tier default. `NULL` = use the catalog tier default. |
| `available_models` | Admin-curated list of selectable concrete model IDs reconciled onto each child row. When non-empty, model-picker datalists and the native `suggested_models` field show this list instead of the auto-discovered list. `NULL`/empty on the credential = fall back to `discovered_models`. |

### Reconcile semantics

| Pass | Logic | Per-child failures |
|------|-------|--------------------|
| **Add** (desired − current) | Validate user exists + is active; decrypt parent key; create child via `ai_credentials_service.create_credential`; stamp markers + parent link; apply optional default/SDK wiring. | Unknown/inactive user → `skipped`. Committed child with failed post-create wiring is retained as a member (not demoted to skipped). |
| **Remove** (current − desired) | Delete child via `ai_credentials_service.delete_credential(admin_override=True)`. Tier-2 bundle blast-radius triggers `AICredentialInUseError`. | In-use child → `blocked` (member stays) unless `force=True`. |
| **Update** (current ∩ desired) | Diff parent scalars against child; write changed fields/key through; apply/clear `set_as_default`. Only mutated children appear in `updated`; unchanged children produce no audit event. | Failed update → `skipped`. |

The response is a `ManagedAICredentialReconcileResult` carrying the parent public record plus `added`, `removed`, `updated`, `updated_count`, `skipped`, and `blocked` lists.

### User experience after provisioning

The user sees the credential in **Settings → AI Credentials** with `is_admin_managed: true` in the API projection. The row renders a **"Managed" badge** (shield icon, tooltip "Managed by your administrator — you can use it and set it as default, but it can't be edited or deleted here"), and the **Edit and Delete buttons are not rendered** for it (the "Set as default" star stays). The backend enforces the read-only guard regardless (`403` on edit/delete/re-key), so the UI gating is defense-in-depth, not the sole protection.

- User **can**: use the credential in environments, set it as their default, see it in model-override selectors.
- User **cannot**: update the name/key/base_url, change the model field, or delete it. Attempts return `403 "This credential is managed by your administrator and cannot be modified."`.

Because each child row is owned by the user, **web environment creation resolves it automatically** via the existing default-resolution and credential-linking pipelines — no extra steps.

### Admin-Curated Model List

Admins can attach two non-secret model metadata fields to a parent record. These are reconciled onto every child `AICredential` row and consumed across three surfaces:

**What each field controls:**

- **`default_model`** — a single concrete model id (e.g. `claude-sonnet-4-6`, `gpt-5.4-mini`). Stored without any `provider/` prefix (the service normalizes on write). When set, it overrides the model-catalog tier default for all environments that link this credential and have no explicit per-mode model override.
- **`available_models`** — an ordered, deduplicated list of concrete model ids the admin wants offered for selection. When non-empty on the child row, model-picker datalists in the Environment Config dialog and the user's AI Credentials view use this list instead of `discovered_models`.

**Precedence rules (SDK / agent environments):**

1. Per-mode environment override (`model_override_building` / `model_override_conversation`) — always wins, whether the installer set it directly (env reconfigure) or it arrived pre-pinned from a bundle publisher at install time (see [Agent Bundles](../../agents/agent_bundles/agent_bundles.md)). A publisher-pinned override is never imported for a mode whose SDK resolves to `openai_compatible` — that mode falls straight through to the admin-curated `default_model` (if any) or the catalog default, same as if no override existed.
2. Admin-curated `default_model` on the linked credential — when set and no env override exists.
3. Model-catalog tier default (`haiku` / `sonnet` / concrete ID per provider).

A publisher-pinned override therefore outranks an admin-curated `default_model` on the installer's own credential, the same as a manually-set one.

**Precedence rules (native account-config, `GET /external/account-config`):**

1. `default_model` (admin curated) — when set and not an SDK tier word (tier words are not valid Anthropic API model IDs).
2. `credential.model` (openai_compatible's required model) — when set.
3. First entry of `available_models` (curated), prefix-stripped.
4. First entry of `discovered_models`, prefix-stripped.
5. Catalog default — only when it is a concrete ID; tier words are dropped.
6. `null` — client falls back to its own default.

**User experience:**

- The user's AI Credentials card shows the `default_model` as a read-only line ("Default model: …") on admin-managed credential entries.
- The user cannot edit `default_model` or `available_models` — they are absent from `AICredentialCreate`/`AICredentialUpdate`. Attempts to set them via user routes simply don't work (fields not accepted).
- Model pickers (env config, user settings) show the curated list when it is non-empty; otherwise the auto-discovered list applies.

**None vs [] semantics for `available_models` on update:**

- `None` (field omitted in the PATCH body) = no change to the stored curation.
- `[]` (explicit empty list) = clear curation; fall back to `discovered_models` for all consumers.

**Normalization on write (service-side):**

- `provider/` prefixes stripped (e.g. `anthropic/claude-sonnet-4-6` → `claude-sonnet-4-6`).
- Entries trimmed, deduplicated (order-preserving), blank entries dropped.
- Capped at 100 entries, 255 characters per entry.
- Reconcile to children is idempotent: a child whose values already match the parent is not counted as `updated` and emits no audit event.

**Model health (amber badge):**

The model-health service mirrors the same precedence. A valid admin-curated `default_model` is never falsely flagged as `unknown_model` or `stale_default`. Note that `has_override` in the health signal is keyed only on the env's `model_override_*` column (not the credential default) — regardless of whether it was set by the installer or arrived pre-pinned from a bundle publisher — so the badge CTA stays accurate: it never tells the user to "clear an override" the env doesn't actually have.

### Admin CRUD

Through the `/admin/llm-providers/` surface, superusers can:
- **Create** a parent record and provision its initial member set (reconcile result returned)
- **List** all parent records fleet-wide, optionally filtered by `?managed_by_id=` (the managing admin) and/or `?target_user_id=` (records that include this user as a member)
- **Get** a single parent record by ID
- **Update** (PATCH) the parent's name/key/base_url/model/default-flags and/or membership; reconcile runs automatically; `?force=` overrides the Tier-2 block on removed members
- **Delete** the parent and all its children — blocked `409` (with `blocked` list) when any child is referenced by a published bundle, unless `?force=true`
- **Set default for all** — calls `set_default` on every current member's child and stamps `set_as_default=True` on the parent

### Admin UI — "LLM Providers" section

The admin UI is accessed via **Admin menu → LLM Providers** (`/admin/llm-providers`) in the sidebar. The route is superuser-gated: non-superusers are redirected to `/` by `beforeLoad`.

**User flow:**

1. The page loads a fleet-wide table of all parent records, sorted by name. Each row shows: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | Shared with (inline member chips — name + email, no per-member default badge) | Created.
2. The admin can **filter** the table to records that have a specific user as a member via a `UserAllowlistPicker` toggle-panel in the page header. A filled dot on the Filter button indicates an active filter; a "Clear filter" button appears inside the panel.
3. Clicking **"Provision Credential"** opens a unified dialog (`ManagedCredentialDialog` in `create` mode) with:
   - Name (free-form; auto-suggested as `"<Provider> Key"` until the admin types their own)
   - Provider type (dropdown: Anthropic, OpenAI, OpenAI Compatible, Google — MiniMax not offered in the UI)
   - API key (password field; required on create)
   - Base URL — shown only for `openai_compatible` (required) and `google` (optional)
   - Model — shown only for `openai_compatible` (required)
   - **Default model** — optional text input; the concrete model ID used by default for all environments and native apps using this credential (leave blank to use the platform catalog default); a **"View available models ↗"** link to the provider's official models reference appears next to the label (Anthropic → platform.claude.com models overview; Google → ai.google.dev Gemini API models; OpenAI → developers.openai.com models; omitted for OpenAI Compatible)
   - **Available models** — optional multi-line/comma editor; the curated list of model IDs offered for selection; leave empty to offer all auto-detected models
   - **"Fill top 10 models"** button — auto-runs Test Connection first if no fresh successful result exists, then fills "Available models" with the top 10 discovered models (deduped, provider-prefix stripped) and auto-sets "Default model" using provider-specific logic: Google → `gemini-flash-latest` (fixed alias); Anthropic → highest-version Sonnet found in the model list, falling back to the first model; OpenAI / OpenAI Compatible → first model in the list
   - Target Users — multi-select `UserAllowlistPicker`; at least one user required
   - "Set as default" toggle
   - "Set user SDK defaults" toggle
   - "Test Connection" button (probes the entered key without persisting; surfaces model count or skip reason)
   - On submit, the dialog surfaces a reconcile summary toast (`+N added, −N removed, ~N updated`) plus per-user skip/blocked toasts
4. Each row has a three-dot actions menu (`LlmProviderActionsMenu`) with:
   - **Edit** — opens `ManagedCredentialDialog` in `edit` mode; same fields as create; API key field blank means "keep stored key for all members"; member add/remove via `UserAllowlistPicker` pre-seeded from current `record.members`; provider type is immutable after creation
   - **Set default for all** — calls the `/set-default` endpoint for every current member
   - **Delete** — opens an `AlertDialog`; on `409` (blocked members) escalates to a force-delete confirmation listing blocked users by name

### Security audit

All events contain counts/IDs but **never** key bytes.

On parent create (`POST /`):
- One `admin.ai_credential.provision` event **per added child** (scoped to the child owner — per-user audit trail)
- One `admin.managed_ai_credential.create` event scoped to the admin (batch summary: `added_count`, `removed_count`, `updated_count`, `skipped_count`, `blocked_count`)

On parent update (`PATCH /{id}`):
- `admin.ai_credential.provision` per newly-added child
- `admin.ai_credential.delete` per removed child
- `admin.ai_credential.update` per actually-mutated child (no event on unchanged members)
- `admin.managed_ai_credential.update` scoped to the admin

On set-default (`POST /{id}/set-default`):
- `admin.ai_credential.set_default` per member child (scoped to each owner)

On parent delete (`DELETE /{id}`):
- `admin.ai_credential.delete` per removed child
- `admin.managed_ai_credential.delete` scoped to the admin

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone; it is replaced by the parent-level `admin.managed_ai_credential.*` events.

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
| `suggested_models` | `available_models` when non-empty (admin curated); otherwise `discovered_models` (the per-credential discovery cache) |

The response also carries `default_provider_credential_id` (the resolved conversation-default credential for the user, using the existing priority resolution) and `generated_at`.

### Model resolution for native clients

Native clients call the provider API directly with the decrypted key, so they need a **concrete, provider-usable model ID** — not an SDK-internal tier word (e.g., `"haiku"`, `"sonnet"` are Claude Code internal shortcuts that are not valid Anthropic API model IDs).

Resolution order (the admin curated `default_model` wins early):
1. `credential.default_model` (admin curated) — when set and not an SDK tier word; stripped of any `provider/` prefix
2. `credential.model` when explicitly set (e.g., `openai_compatible` with a pinned model)
3. First entry in `credential.available_models` (admin curated list), prefix-stripped
4. First entry in `credential.discovered_models` (the nightly-refreshed list of models this key can actually access), stripped of any `provider/` prefix
5. The model-catalog default for this provider/engine — but only when it is a **concrete ID**; tier words (`"haiku"`, `"sonnet"`, `"opus"`) are dropped and the field becomes `null`
6. `null` — the client falls back to its own default or lets the user pick from `suggested_models`

The `suggested_models` field on the native response returns `credential.available_models` when non-empty, otherwise `credential.discovered_models`.

### Security boundary (all four constraints are enforced)

1. **Native-token gated.** `client_kind in {"desktop", "mobile"}` required. Web JWTs (`client_kind` absent) → `403`. Revoked desktop clients → `401` (via the existing `get_current_user` desktop revocation check).
2. **Strictly self-scoped.** Returns only `AICredential` rows with `owner_id == user.id`. Credentials shared *with* the user via `AICredentialShare` are deliberately excluded — they belong to another user.
3. **Audited.** Every successful call writes `SecurityEvent(event_type="external.account_config.read", severity="high", details={client_kind, external_client_id, provider_count, credential_ids})`. No key material is logged.
4. **No caching.** Response includes `Cache-Control: no-store`.

---

## Business Rules

### Admin provisioning

- **Superuser-only.** `get_current_active_superuser` dependency (same gate as Knowledge Sources and Admin Environments).
- **Parent record is the source of truth.** Membership is derived from children. The parent holds its own encrypted key so adding a member or rotating the key never requires re-entering the secret.
- **Children are ordinary per-user rows.** Each child is an `AICredential` with `is_admin_managed=True` and a `managed_credential_id` FK to the parent. All existing per-user plumbing (default resolution, environment creation, agent-log visibility) applies unchanged.
- **Invalid targets skipped, not errored.** Unknown or inactive users appear in `skipped`; the successfully reconciled members proceed.
- **Reconcile is idempotent.** Supplying the same desired set with unchanged parent scalars produces empty added/removed/updated lists and emits no child-level audit events.
- **One-default-per-type invariant preserved.** If `set_as_default=True` and the target user already has a default of that type, the existing default is unset (existing `set_default` behavior). The user ends up with exactly one default.
- **Blast-radius gate on member removal.** Removing a member whose child is referenced by a published bundle is blocked (appended to `blocked`) unless `force=True`. The parent and blocked members stay intact on a non-forced delete.
- **Admin deletion preserves children.** When the managing admin's user account is deleted, both `ManagedAICredential.managed_by_id` and `AICredential.managed_by_id` are set to `NULL` (SET NULL FK) — the parent record stays fleet-manageable by any superuser, and each user keeps their child credential.
- **Out-of-band parent deletion degrades gracefully.** If a parent row is ever deleted outside the service path, its children's `managed_credential_id` becomes NULL (SET NULL FK) — they degrade to plain `is_admin_managed` orphans rather than disappearing.
- **`set_default` is open to the owner.** Setting an admin-managed credential as one's default is a read-only use, not a modification. The owner may do this freely through the user-facing CRUD.

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
ManagedAICredentialsService.create(session, admin, ManagedAICredentialCreate)
   ├── Validate + Fernet-encrypt canonical key (reuses _validate_credential_data)
   ├── INSERT ManagedAICredential (parent row) — managed_by_id=admin.id
   └── reconcile(desired=target_user_ids, apply_fields=False, key_rotated=False)
         ├── Add pass (desired − current):
         │     validate user active → _add_child()
         │       AICredentialsService.create_credential(owner=target.id, ...)
         │       _stamp_child(is_admin_managed, managed_by_id, managed_credential_id)
         │       [optional] set_default / _apply_sdk_defaults for the owner
         ├── (Remove/Update passes are no-ops on first create)
         └── Return ManagedAICredentialReconcileResult(record, added, skipped)

SecurityEvent("admin.ai_credential.provision")  [per added child, scoped to owner]
SecurityEvent("admin.managed_ai_credential.create")  [scoped to admin]


Superuser (web)
   │
   │  PATCH /api/v1/admin/llm-providers/{id}?force=
   ▼
ManagedAICredentialsService.update(session, admin, id, ManagedAICredentialUpdate)
   ├── Apply changed scalars to parent; rotate encrypted_data if api_key supplied
   └── reconcile(desired=new_target_user_ids_or_current, apply_fields=True, key_rotated=...)
         ├── Add pass   → add new members
         ├── Remove pass → delete children (Tier-2 gate → blocked list if force=False)
         └── Update pass → _update_child_fields diff:
               name / base_url / model / key (if rotated) → update_credential(admin_override)
               set_as_default toggle → set_default / _clear_child_default
               [no change → not in updated list, no event emitted]

SecurityEvent per mutated child + admin.managed_ai_credential.update


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

- **[AI Credentials](ai_credentials.md)** — the reused core service: `create_credential`, `set_default`, `update_credential`, `delete_credential`, `decrypt_credential`, `resolve_default_credential_for_sdk`. `ManagedAICredentialsService` delegates every per-child operation with `user_id = owner_id` so all per-user invariants (one-default-per-type, profile auto-sync, SDK-default wiring) run for the target user.
- **[AI Credentials Tech](ai_credentials_tech.md)** — `AICredential` model with three managed-credential columns (`is_admin_managed`, `managed_by_id`, `managed_credential_id`); `AICredentialsService.update_credential` / `delete_credential` `admin_override` kwarg; `AICredentialPublic.is_admin_managed` projection.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the `/external/` route namespace that the account-config endpoint extends. The same `ExternalAccountConfigService` sits under `services/external/`.
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the desktop/mobile JWTs with `client_kind` and `external_client_id` claims. The live revocation check in `get_current_user` ensures revoked device tokens are rejected `401` before the native gate runs.
- **[User Roles](../user_roles/user_roles.md)** — `get_current_active_superuser` gates the admin surface. Only superusers may create or manage parent records.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the blast-radius gate on child credential removal (`AICredentialInUseError`, Tier-2, published bundle references) is the same mechanism used by the regular credential deletion guard.

---

*Last updated: 2026-06-14 — admin-curated model list (`default_model` + `available_models`) shipped; migration `c1a4b2d3e5f6`*
