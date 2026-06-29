# Admin-Provisioned AI Credentials + Native Account-Config Endpoint — Implementation Plan

**Scope of this document:** cinna-core **BACKEND only** (first phase). Frontend UI is a later phase; this plan covers the model/migration/service/route work plus the OpenAPI client-regen step that unblocks FE work.

**Status:** Draft for review. Several items are flagged in [Open Questions / Decisions](#open-questions--decisions) and need a product/architecture decision before implementation.

---

## 1. Overview

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

**Part A — Admin-provisioned AI credentials.** From a dedicated admin section ("LLM Providers"), a superuser/admin creates an `AICredential` *owned by a target user* (`owner_id = target.id`) and optionally sets it as that user's default + wires the user's default SDK preferences. Because the row is owned by the user, it automatically participates in *all* existing per-user plumbing (default resolution, environment linking, agent-log visibility, AI-credential listing). The credential is **read-only in the user's own UI** — the user can use it but cannot edit/delete/re-key it.

**Part B — Native account-config endpoint.** A native/desktop/mobile-token-gated endpoint (`GET /api/v1/external/account-config`) that returns, per usable AI credential of the authenticated user, a descriptor *including the decrypted api_key*, so Cinna Desktop/Mobile can auto-create local "LLM providers" + a default chat mode per credential on login. This is a **deliberate, scoped relaxation** of the platform's "keys never exposed" invariant, explicitly approved by the product owner, and must be native-token-gated, self-scoped, and audited.

### Core capabilities

- Admin creates an AI credential **for** a user (or N users via the user picker → one row per user).
- Admin can mark it the user's default and set the user's `default_sdk_*` / `default_ai_credential_*_id` preferences.
- Admin can list / update / delete / set-default the admin-managed credentials it provisioned.
- Admin-managed rows are immutable through the user-facing AI-credentials CRUD (guarded at the service layer).
- Native clients fetch one bundle of provider descriptors (with decrypted keys) for their own account, audited via `SecurityEvent`.

### High-level flow

```
Admin (superuser)                                  Target user (web)            Native client (Desktop/Mobile)
   │                                                     │                              │
   │ POST /admin/llm-providers/ {target_user_ids:[...]}  │                              │
   │   → AdminAICredentialService.provision_for_users    │                              │
   │      • create AICredential(owner_id=target,         │                              │
   │        is_admin_managed=True, managed_by_id=admin)  │                              │
   │      • optional set_default + set user default_sdk  │                              │
   │   → SecurityEvent("admin.ai_credential.provision")  │                              │
   │                                                     │                              │
   │                                          GET /ai-credentials/  (sees row,          │
   │                                            is_admin_managed=true → read-only UI)    │
   │                                          PATCH/DELETE on it → 403                   │
   │                                                     │                              │
   │                                          web env create resolves it (no extra work) │
   │                                                     │                              │
   │                                                     │   GET /external/account-config (desktop/mobile JWT)
   │                                                     │     → ExternalAccountConfigService.build_config
   │                                                     │        • own usable AI creds (admin-managed + self)
   │                                                     │        • decrypt api_key, map provider→display name
   │                                                     │     → SecurityEvent("external.account_config.read")
```

---

## 2. Architecture Overview

### Reuse-vs-new analysis (read this first)

The single most important design constraint, set by the product owner: **per-user provisioning, NOT key sharing.** The admin creates a row whose `owner_id` is the target user. Consequence: we reuse the entire existing per-user pipeline and add only:

| Concern | Decision | Why |
|---------|----------|-----|
| Default resolution (Anthropic>Google>OpenAI) | **REUSE** `resolve_default_credential_for_sdk` unchanged | Already keys off `owner_id` + `is_default` |
| Profile auto-sync (`ai_credentials_encrypted`, `default_*`) | **REUSE** `set_default` / `_sync_default_to_user_profile` | Already operate on `(session, credential_id, user_id)` |
| Environment linking / web env creation | **REUSE** — no change | Resolution is `owner_id`-scoped already |
| Agent-log visibility / listing | **REUSE** — automatic | Row is owned by the user |
| Encryption | **REUSE** `encrypt_field` / `decrypt_field` (Fernet) | Same `encrypted_data` column |
| Admin gating | **REUSE** `get_current_active_superuser` dep | Matches knowledge_sources / admin_environments precedent |
| User picker | **REUSE** `GET /users/search` + `UserAllowlistPicker` (FE later) | Per `docs/development/frontend/user_selector_pattern.md` |
| Audit | **REUSE** `SecurityEventService.create_event` | Matches admin_environments precedent |
| **Read-only guard** | **NEW** — block user CRUD on `is_admin_managed` rows | The only behavioral divergence |
| **Admin CRUD surface** | **NEW** — `/admin/llm-providers/*` routes + `AdminAICredentialService` | New admin resource |
| **Native config endpoint** | **NEW** — `GET /external/account-config` + service | New native surface |
| **Two columns** | **NEW** — `is_admin_managed`, `managed_by_id` on `ai_credential` | Flag + audit FK |

**Do NOT build a parallel resolution path.** Everything funnels through the existing `AICredentialsService`.

### Component map

```
backend/app/
├── models/credentials/ai_credential.py
│     + AICredential.is_admin_managed (col)
│     + AICredential.managed_by_id (col, FK→user.id SET NULL)
│     + AICredentialPublic.is_admin_managed (projection)
│     + AICredentialPublic.managed_by_id (projection, optional)
│     + AdminAICredentialCreate / AdminAICredentialProvisionResult (admin DTOs)
│     (Part B) NEW models/external/account_config.py:
│         AccountConfigProviderPublic, AccountConfigResponse
├── services/credentials/
│     ├── ai_credentials_service.py   (add read-only guard in update/delete)
│     └── admin_ai_credentials_service.py    NEW — AdminAICredentialService
├── services/external/
│     └── external_account_config_service.py NEW — ExternalAccountConfigService
├── api/routes/
│     ├── admin_llm_providers.py             NEW — superuser-gated CRUD
│     ├── external_agents.py OR external_account_config.py  — GET /external/account-config
│     └── ai_credentials.py                  (no route change; guard is in service)
└── alembic/versions/<rev>_add_admin_managed_ai_credential.py  NEW migration
```

---

## 3. Data Models

### 3.1 `ai_credential` table — two new columns

File: `backend/app/models/credentials/ai_credential.py`

Add to `class AICredential(AICredentialBase, table=True)`:

| Column | Type | Constraints / Default | Purpose |
|--------|------|------------------------|---------|
| `is_admin_managed` | `bool` | `nullable=False`, `server_default sa.false()`, default `False` | Marks the row as admin-provisioned → read-only for the owner. |
| `managed_by_id` | `uuid \| None` | `FK → user.id`, `ondelete="SET NULL"`, nullable, indexed | The admin who provisioned it, for audit. `SET NULL` so deleting the admin account does not delete the user's credential. |

Notes:
- `is_admin_managed` is the **single behavioral discriminator**. `managed_by_id` is audit-only — never used in access decisions.
- No new index strictly required for correctness, but add `ix_ai_credential_managed_by` on `managed_by_id` to make the admin's "what did I provision" listing cheap (see [Open Questions OQ-3](#open-questions--decisions) on listing strategy).

### 3.2 `AICredentialPublic` projection changes

Add to `class AICredentialPublic(AICredentialBase)`:

```
is_admin_managed: bool = False
managed_by_id: uuid.UUID | None = None   # see OQ-4: expose or not
```

Set in `AICredentialsService._to_public`. This lets the user's own UI render admin-managed rows read-only (hide edit/delete, show a "Managed by your administrator" badge).

### 3.3 New admin DTOs (`ai_credential.py`)

```
class AdminAICredentialCreate(SQLModel):
    # provider fields (mirror AICredentialCreate minus per-row name override rules)
    name: str
    type: AICredentialType
    api_key: str
    base_url: str | None = None
    model: str | None = None
    expiry_notification_date: datetime | None = None
    # provisioning targets + behavior
    target_user_ids: list[uuid.UUID]          # 1..N — one row created per user
    set_as_default: bool = False              # call set_default per row
    set_user_sdk_defaults: bool = False       # also wire user.default_sdk_* + default_ai_credential_*_id
    sdk_default_modes: list[str] = ["conversation", "building"]  # which mode prefs to set (see OQ-5)

class AdminAICredentialProvisionResult(SQLModel):
    created: list[AICredentialPublic]         # one per target user
    skipped: list[AdminProvisionSkip]         # users skipped (e.g. already has admin-managed of this type — see OQ-2)

class AdminProvisionSkip(SQLModel):
    user_id: uuid.UUID
    reason: str
```

### 3.4 New native config models

File: `backend/app/models/external/account_config.py` (new)

```
class AccountConfigProviderPublic(SQLModel):
    credential_id: uuid.UUID
    provider_type: AICredentialType        # anthropic | openai | google | openai_compatible | minimax
    display_name: str                       # e.g. "Claude" — provider→name map
    descriptor_slug: str                     # stable slug for local provider id, e.g. "anthropic" / "claude"
    base_url: str | None = None
    model: str | None = None                 # default/suggested model (credential.model or catalog default)
    api_key: str                             # *** DECRYPTED *** — security boundary, see §4
    is_default: bool
    is_admin_managed: bool
    # default chat mode descriptor
    default_chat_mode_label: str             # e.g. "Claude" — what the app names the chat mode
    suggested_models: list[str] = []         # from credential.discovered_models (non-secret)

class AccountConfigResponse(SQLModel):
    providers: list[AccountConfigProviderPublic]
    # user-level default routing so the app can pick precedence
    default_provider_credential_id: uuid.UUID | None = None   # resolved conversation default
    generated_at: datetime
```

Provider → display-name / slug map (constant in the service):

| `type` | `display_name` | `descriptor_slug` |
|--------|----------------|-------------------|
| `anthropic` | `"Claude"` | `"claude"` |
| `openai` | `"OpenAI"` | `"openai"` |
| `google` | `"Gemini"` | `"gemini"` |
| `openai_compatible` | `credential.name` (free-form) | `"openai-compatible"` |
| `minimax` | `"MiniMax"` | `"minimax"` |

---

## 4. Security Architecture

### 4.1 Encryption (unchanged)
- All credential secrets remain Fernet-encrypted at rest in `encrypted_data` via `backend/app/core/security.py` (`encrypt_field`/`decrypt_field`). No change.

### 4.2 Access control — Part A (admin provisioning)
- **Admin CRUD routes** are gated by `get_current_active_superuser` (matches `knowledge_sources.py` / `admin_environments.py`). See [OQ-1](#open-questions--decisions) on whether `admin` role (non-superuser) should also pass.
- The admin operates **on behalf of the target user**: `AdminAICredentialService` calls the existing `AICredentialsService` methods with `user_id = target_user.id` (the row owner), so all downstream invariants (one-default-per-type, profile sync) hold for the *target*, not the admin.
- **Read-only enforcement** (the new guard) lives in `AICredentialsService.update_credential` and `delete_credential`: if `credential.is_admin_managed` and the caller is going through the user-facing path, raise `403 "This credential is managed by your administrator and cannot be modified."` See [§6.3](#63-read-only-enforcement-points) for exact enforcement points and how the admin path bypasses it.

### 4.3 Access control — Part B (native config endpoint) — the scoped relaxation

This endpoint **returns decrypted API keys**. That is a deliberate, product-owner-approved exception to the "keys never exposed" rule (`ai_credentials.md` Business Rules). The boundary MUST be:

1. **Native-token gated.** Only callers whose JWT carries `client_kind in {"desktop", "mobile"}` may call it. A plain web-session JWT (no `client_kind`) is rejected `403`. Use `get_current_client_claims` (`deps.py:663`) to read the claim; combine with `CurrentUser`. The existing `get_current_user` already enforces live desktop-client revocation (`deps.py:59`), so a revoked device cannot call this with a stale token.
2. **Strictly self-scoped.** Returns ONLY `AICredential` rows the authenticated user can use: `owner_id == user.id` (covers both admin-managed and self-created). Decide whether to also include credentials shared *with* the user via `AICredentialShare` — see [OQ-6](#open-questions--decisions). Default recommendation: **owner-only** (do not leak another user's key bytes through a share into a device).
3. **Audited.** Every successful call writes a `SecurityEvent` (`event_type="external.account_config.read"`, `severity="high"`, `details={client_kind, external_client_id, provider_count, credential_ids:[...]}`). High severity because raw keys crossed the boundary.
4. **No proxy needed.** Because the key is delivered to the trusted client, no server-side LLM gateway is built. (This is the explicit trade-off: simplicity over key-confinement.)
5. **Transport.** HTTPS only (platform already terminates TLS at nginx). The response body contains secrets → ensure no intermediate caching: the route should set `Cache-Control: no-store`.

### 4.4 Input validation
- Admin create reuses `AICredentialsService._validate_credential_data` (per-type required fields: `openai_compatible` needs `base_url`+`model`).
- `target_user_ids`: validate each exists and is active; skip-or-fail policy is [OQ-2](#open-questions--decisions).
- Strict SDK-provider match when `set_user_sdk_defaults=True`: reuse `is_credential_compatible_with_sdk` / `sdk_expected_credential_type` (`sdk_constants.py`) so we don't wire an OpenAI key into an Anthropic SDK slot.

### 4.5 What gets logged
- Admin provision: `SecurityEvent` with NO key material — only `credential_id`, `target_user_id`, `type`, `managed_by_id`.
- Native config read: `SecurityEvent` with NO key material — only counts + ids.
- Standard app logging must never log decrypted keys (existing logging in `ai_credentials_service.py` already avoids this; preserve that).

---

## 5. (No frontend in this phase)

FE is explicitly out of scope for this plan. The only FE-facing deliverable is the **OpenAPI client regen** (§11) so a later FE phase can build:
- An admin "LLM Providers" section (precedent: Knowledge Sources / Plugin Marketplaces admin pages) with a `UserAllowlistPicker`.
- A read-only badge on admin-managed rows in `AICredentials.tsx`.

---

## 6. Backend Implementation

### 6.1 New service — `AdminAICredentialService`

File: `backend/app/services/credentials/admin_ai_credentials_service.py`
Singleton: `admin_ai_credentials_service` (matches `ai_credentials_service` pattern).

Key methods:

```
provision_for_users(session, admin: User, data: AdminAICredentialCreate) -> AdminAICredentialProvisionResult
    """For each target_user_id: create an AICredential owned by that user with
    is_admin_managed=True, managed_by_id=admin.id. Optionally set_default and
    set the user's default SDK preferences. Returns created + skipped lists.
    Emits one SecurityEvent per created row (or one batch event — OQ-7)."""

list_managed(session, admin: User, target_user_id: uuid.UUID | None = None) -> list[AICredentialPublic]
    """List admin-managed credentials. If target_user_id given, scope to that
    user; else all admin-managed rows (or only those managed_by this admin — OQ-3)."""

update_managed(session, admin: User, credential_id: uuid.UUID, data: AICredentialUpdate) -> AICredentialPublic
    """Admin edits a managed row. Bypasses the user read-only guard by calling
    the internal AICredentialsService update with an admin_override flag (§6.3).
    Re-syncs profile if the row is the owner's default."""

delete_managed(session, admin: User, credential_id: uuid.UUID, force: bool = False) -> None
    """Admin deletes a managed row. Reuses AICredentialsService.delete_credential
    with admin_override + the existing Tier-2 bundle blast-radius check."""

set_managed_default(session, admin: User, credential_id: uuid.UUID) -> AICredentialPublic
    """Set the row as the owner-user's default for its type (delegates to
    AICredentialsService.set_default with the row's owner_id)."""
```

Implementation detail: every method resolves `credential.owner_id` and calls the existing `AICredentialsService` with `user_id = credential.owner_id` so all per-user invariants run for the target user. A `managed`-only guard ensures the admin can't accidentally mutate a *self-created* (non-admin-managed) row of some user through this surface — `update_managed`/`delete_managed`/`set_managed_default` must 404 if `not credential.is_admin_managed`.

### 6.2 New service — `ExternalAccountConfigService`

File: `backend/app/services/external/external_account_config_service.py`
(Mirrors `ExternalAgentCatalogService` placement under `services/external/`.)

```
build_config(session, user: User) -> AccountConfigResponse
    """Load user's own usable AI credentials (owner_id == user.id),
    decrypt each via AICredentialsService.decrypt_credential, map provider→
    display-name/slug, attach discovered_models as suggested_models, compute the
    resolved conversation default (resolve_default_credential_for_sdk for the
    user's default_sdk_conversation engine, or the is_default row). Returns the
    response. Does NOT emit the audit event — the route does that so it can
    include request context (client_kind/external_client_id)."""
```

Reuse `AICredentialsService.decrypt_credential` (public method already) for the key bytes; reuse `resolve_default_credential_for_sdk` for the default determination.

### 6.3 Read-only enforcement points (the core guard)

Two enforcement points in `AICredentialsService`:

1. `update_credential(session, credential_id, user_id, data, *, admin_override: bool = False)`
   - After `get_credential(...)`, add: `if credential.is_admin_managed and not admin_override: raise HTTPException(403, "This credential is managed by your administrator and cannot be modified.")`
2. `delete_credential(session, credential_id, user_id, force=False, *, admin_override: bool = False)`
   - Same guard before the deletion logic.

The user-facing routes (`ai_credentials.py` `PATCH`/`DELETE`) call these **without** `admin_override` → users are blocked. `AdminAICredentialService` calls them **with** `admin_override=True` → admins pass.

**Also guard `set_default` for the user path?** Decision: NO. Setting an admin-managed credential as one's own default is a benign user action ("use my company key") and uses the row read-only. Leave `set_default` open to the owner. (Confirm in [OQ-8](#open-questions--decisions).)

**`update_user_ai_credentials` / legacy profile field:** unaffected — the legacy flat field is a sync *target*, not the admin-managed row.

### 6.4 New routes — Admin "LLM Providers"

File: `backend/app/api/routes/admin_llm_providers.py`
Register in `backend/app/api/main.py` (`api_router.include_router(admin_llm_providers.router)`), near the other admin routers.

```
router = APIRouter(prefix="/admin/llm-providers", tags=["admin-llm-providers"])
SuperUser = Annotated[User, Depends(get_current_active_superuser)]
```

| Method | Path | Auth gate | Request | Response | Status codes |
|--------|------|-----------|---------|----------|--------------|
| `POST` | `/admin/llm-providers/` | superuser | `AdminAICredentialCreate` | `AdminAICredentialProvisionResult` | `200` created; `400` validation (bad type fields / SDK mismatch); `404` if a target user doesn't exist (or skip per OQ-2); `403` non-superuser |
| `GET` | `/admin/llm-providers/` | superuser | query `?target_user_id=` optional | `list[AICredentialPublic]` (admin-managed only) | `200`; `403` |
| `GET` | `/admin/llm-providers/{credential_id}` | superuser | — | `AICredentialPublic` | `200`; `404` (missing or not admin-managed); `403` |
| `PATCH` | `/admin/llm-providers/{credential_id}` | superuser | `AICredentialUpdate` | `AICredentialPublic` | `200`; `400`; `404`; `403` |
| `DELETE` | `/admin/llm-providers/{credential_id}?force=bool` | superuser | — | `Message` | `200`; `409` Tier-2 bundle blast radius (reuse `AICredentialInUseError`); `404`; `403` |
| `POST` | `/admin/llm-providers/{credential_id}/set-default` | superuser | — | `AICredentialPublic` | `200`; `404`; `403` |

Each mutating route emits a `SecurityEvent` (matching `admin_environments.py` pattern, `await SecurityEventService.create_event(...)`).

Note on the user picker: the admin FE will call the existing `GET /users/search` (already shipped, see `user_selector_pattern.md`) to populate `target_user_ids`. No new search endpoint is needed.

### 6.5 New route — native account config

Placement: add to `backend/app/api/routes/external_agents.py` (the existing `/external` router, `tags=["external"]`) to keep the native surface cohesive. (Alternative: a small new `external_account_config.py` — [OQ-9](#open-questions--decisions).)

```
@router.get("/account-config", response_model=AccountConfigResponse)
async def get_account_config(
    *, session: SessionDep, current_user: CurrentUser,
    client_claims: CurrentClientClaims, response: Response,
) -> Any:
    client_kind, external_client_id = client_claims
    if client_kind not in {"desktop", "mobile"}:
        raise HTTPException(403, "This endpoint is only available to native clients")
    config = external_account_config_service.build_config(session, current_user)
    response.headers["Cache-Control"] = "no-store"
    await SecurityEventService.create_event(session=session, user_id=current_user.id,
        data=SecurityEventCreate(event_type="external.account_config.read", severity="high",
            details={"client_kind": client_kind, "external_client_id": external_client_id,
                     "provider_count": len(config.providers),
                     "credential_ids": [str(p.credential_id) for p in config.providers]}))
    return config
```

| Method | Path | Auth gate | Response | Status codes |
|--------|------|-----------|----------|--------------|
| `GET` | `/api/v1/external/account-config` | `CurrentUser` + `client_kind in {desktop,mobile}` | `AccountConfigResponse` (incl. decrypted keys) | `200`; `401` unauth / revoked desktop client (via `get_current_user`); `403` non-native token |

---

## 7. Database Migration

File: `backend/app/alembic/versions/<rev>_add_admin_managed_ai_credential.py`

Upgrade:
- `op.add_column("ai_credential", sa.Column("is_admin_managed", sa.Boolean(), nullable=False, server_default=sa.false()))`
- `op.add_column("ai_credential", sa.Column("managed_by_id", postgresql.UUID(as_uuid=True), nullable=True))`
- `op.create_foreign_key("fk_ai_credential_managed_by", "ai_credential", "user", ["managed_by_id"], ["id"], ondelete="SET NULL")`
- `op.create_index("ix_ai_credential_managed_by", "ai_credential", ["managed_by_id"])`
- (Optional) drop the `server_default` after backfill if you don't want a DB-level default lingering — not required; keep it for safety.

Downgrade: drop index, drop FK, drop both columns.

**Migration / multi-head risk:**
- Per agent memory, this repo has repeatedly had **multiple Alembic heads** (a 4-head situation that was merged; recent single heads `d3f0a1b2c4e5`, `e8f1a2b3c4d5` were noted as needing a merge at deploy). Before generating: run `alembic heads` and set `down_revision` to the **current single head**. If `alembic heads` shows >1, the implementer must add a merge migration first (do NOT just pick one head).
- No data backfill of real values needed — `is_admin_managed` defaults `False` for all existing rows (correct: nothing was admin-provisioned before).
- No seed concern.

---

## 8. Knowledge Repository Format
Not applicable.

---

## 9. Error Handling & Edge Cases

| Scenario | Handling |
|----------|----------|
| Admin provisions for a user who already has a self-created default of that type | `set_default` will unset the user's previous default (existing behavior). Surface clearly in FE later; consider warning. [OQ-2/OQ-8] |
| Admin provisions the same type for the same user twice | Two admin-managed rows of that type; only one can be default. Decide dedupe/skip policy ([OQ-2]). |
| User tries to PATCH/DELETE an admin-managed row | `403` with explicit message (the new guard). |
| User tries to set-default an admin-managed row | Allowed (read-only use). [OQ-8] |
| Admin deletes a managed row that a published bundle references (PBP) | Reuse `AICredentialInUseError` → `409` unless `force=true` (existing Tier-2 logic). |
| Admin account deleted later | `managed_by_id` → `NULL` (audit FK SET NULL); the user keeps their credential. `owner_id` is the user, unaffected. |
| Native endpoint called with a web JWT | `403`. |
| Native endpoint called with a revoked desktop client token | `401` via existing `get_current_user` desktop revocation check. |
| Target user has no credentials → native config | `200` with `providers: []`. |
| `openai_compatible` provisioned without base_url/model | `400` via `_validate_credential_data`. |
| Type/SDK mismatch when `set_user_sdk_defaults=True` | `400` via strict `is_credential_compatible_with_sdk`. |
| SDK default sync for a mode whose engine the credential isn't compatible with | Skip that mode (or `400`) — [OQ-5]. |

---

## 10. UI/UX Considerations
Out of scope (FE later). Note for the FE phase: admin-managed rows render with a lock/badge ("Managed by your administrator") and no edit/delete affordance, keyed off `AICredentialPublic.is_admin_managed`.

---

## 11. Integration Points

- **AI Credentials** (`ai_credentials_service.py`, `ai_credential.py`, routes) — the reused core; new guard + projection.
- **External Agent Access** (`external_agents.py`, `services/external/`) — host of the native config endpoint.
- **Desktop/Mobile Auth** (`deps.py:get_current_client_claims`, `DesktopAuthService`) — token gating + live revocation.
- **User roles / superuser** (`get_current_active_superuser`, `RoleService`) — admin gating.
- **Security events** (`SecurityEventService`, `SecurityEventCreate`) — audit.
- **Bundles** (`AICredentialInUseError` Tier-2) — admin delete blast radius.
- **OpenAPI client regen** — after backend lands, run:
  ```
  source ./backend/.venv/bin/activate && make gen-client
  ```
  to surface `AdminLlmProvidersService` + the new `account-config` types + `AICredentialPublic.is_admin_managed` for the later FE phase.

---

## 12. Future Enhancements (Out of Scope)

- Frontend admin "LLM Providers" section + read-only badges (next phase).
- Bulk re-key / rotation of an admin-managed credential across all its per-user rows (currently each row is independent).
- Org/workspace-scoped provisioning ("provision for all members of workspace X").
- A genuine server-side LLM proxy/gateway (explicitly NOT built — keys are delivered to the trusted native client by product decision).
- Native config delta/ETag so clients can poll cheaply.
- Including share-acquired credentials in native config (gated behind a decision — [OQ-6]).

---

## 13. Summary Checklist

### Backend — data model & migration
- [ ] Add `is_admin_managed` (bool, server_default false) + `managed_by_id` (uuid FK→user SET NULL, indexed) to `AICredential`.
- [ ] Project `is_admin_managed` (and `managed_by_id` per OQ-4) on `AICredentialPublic`; set in `_to_public`.
- [ ] Add `AdminAICredentialCreate`, `AdminAICredentialProvisionResult`, `AdminProvisionSkip` DTOs.
- [ ] Add `models/external/account_config.py` with `AccountConfigProviderPublic`, `AccountConfigResponse`.
- [ ] Create migration; verify single Alembic head first (add merge if needed).

### Backend — services
- [ ] Add `admin_override` kwarg + read-only guard to `AICredentialsService.update_credential` and `delete_credential`.
- [ ] New `AdminAICredentialService` (provision_for_users, list_managed, update_managed, delete_managed, set_managed_default) delegating to `AICredentialsService` with `user_id = owner_id`.
- [ ] New `ExternalAccountConfigService.build_config` (decrypt + provider→name map + default resolution).

### Backend — routes
- [ ] `admin_llm_providers.py`: POST / GET / GET{id} / PATCH{id} / DELETE{id} / set-default — superuser-gated, SecurityEvent on mutations.
- [ ] `GET /external/account-config` — native-token gated (`client_kind in {desktop,mobile}`), `no-store`, high-severity SecurityEvent.
- [ ] Register `admin_llm_providers.router` in `api/main.py`.

### Backend — security
- [ ] Confirm decrypted keys never hit logs.
- [ ] Confirm strict SDK-provider match when setting user SDK defaults.
- [ ] Confirm native endpoint rejects web JWTs and honors desktop revocation.

### Integration
- [ ] Run `make gen-client` to regenerate the FE OpenAPI client.

### Testing & validation (test surface to cover — write per `backend/tests/README.md`, API-only, scenario-based)
- [ ] Admin provisions for 1 user → row owned by user, `is_admin_managed=true`, `managed_by_id=admin`.
- [ ] Admin provisions for N users → N rows, one per user.
- [ ] Admin `set_as_default` → user's default set; profile auto-synced; `default_ai_credential_*_id` set when `set_user_sdk_defaults`.
- [ ] User PATCH/DELETE on an admin-managed row → `403`.
- [ ] User GET list shows `is_admin_managed=true` on the row.
- [ ] User web env creation resolves the admin-managed credential with no extra config (web parity).
- [ ] Admin PATCH/DELETE/set-default on managed row → succeeds (admin_override path); DELETE Tier-2 → `409` unless `force`.
- [ ] Admin mutating a *non*-admin-managed row via `/admin/llm-providers/{id}` → `404`.
- [ ] Non-superuser hitting `/admin/llm-providers/*` → `403`.
- [ ] `GET /external/account-config` with desktop JWT → `200`, providers carry decrypted `api_key`, correct display names, `no-store` header, SecurityEvent written.
- [ ] `GET /external/account-config` with web JWT → `403`; with revoked desktop client → `401`; empty creds → `200 []`.
- [ ] SecurityEvent rows for provision + native read contain ids/counts but NO key material.

---

## Open Questions / Decisions

These need a product/architecture decision rather than a guess:

- **OQ-1 — Admin gate: superuser only, or `admin` role too?** This plan uses `get_current_active_superuser` (matches knowledge_sources/admin_environments). The `user_roles` feature has a distinct `admin` role that is "paired with superuser". Confirm whether non-superuser `admin` should reach `/admin/llm-providers/*`. **Recommendation:** superuser-only for v1 (simplest, matches precedent).
- **OQ-2 — Duplicate provisioning policy.** If a target user already has an admin-managed credential of the same type, do we skip (record in `skipped`), create a second, or update-in-place? **Recommendation:** create a second row but never auto-set two defaults; expose `skipped` for genuinely invalid targets (missing/inactive user).
- **OQ-3 — `list_managed` scope.** All admin-managed rows fleet-wide, or only those `managed_by` the calling admin? With multiple admins, fleet-wide is more useful operationally; `managed_by_id` index supports both. **Recommendation:** fleet-wide (any superuser sees all admin-managed), `?target_user_id=` filter.
- **OQ-4 — Expose `managed_by_id` on `AICredentialPublic`?** The target user arguably shouldn't see *which* admin provisioned it; the admin section may want it. **Recommendation:** expose only on the admin GET responses, omit from the user-facing list (project `is_admin_managed` only on the shared `AICredentialPublic`, return `managed_by_id` via a separate admin-only field/model).
- **OQ-5 — `set_user_sdk_defaults` semantics.** Which of `default_sdk_conversation` / `default_sdk_building` + the matching `default_ai_credential_*_id` + `default_model_override_*` do we set, and what engine string (`opencode/anthropic` vs `claude-code`) do we compose from the credential type? Need the exact mapping rule (mirror `AddEnvironment` SDK composition). Also: skip vs `400` when the credential isn't compatible with the chosen engine for a mode.
- **OQ-6 — Native config: include shared credentials?** Owner-only is safest (don't push someone else's key bytes onto a device via a share). Confirm the product wants only `owner_id == user.id`. **Recommendation:** owner-only for v1.
- **OQ-7 — One SecurityEvent per provisioned row, or one batch event for the whole N-user provision call?** **Recommendation:** one per created row (clean per-user audit trail), plus optionally a batch summary.
- **OQ-8 — Can the owner set an admin-managed credential as their own default?** This plan allows it (read-only use). Confirm.
- **OQ-9 — Endpoint placement.** Put `GET /external/account-config` in `external_agents.py` (cohesive native surface) vs a dedicated `external_account_config.py`. **Recommendation:** new small file `external_account_config.py` under the same `/external` prefix for separation, since it's the only route that returns secrets and deserves an isolated, clearly-audited module.
- **OQ-10 — Default model suggestion source.** `AccountConfigProviderPublic.model`: use `credential.model` (only set for `openai_compatible`) or fall back to the model catalog's default per provider/engine? Need the catalog lookup (`model_catalog`) decision so native gets a sensible default model per provider.
- **OQ-11 — Quota / rate.** Should there be a cap on how many admin-managed credentials one user can be assigned, or a rate limit on the native config endpoint? Low priority; native endpoint is self-scoped + audited.
```
