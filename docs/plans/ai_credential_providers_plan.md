# AI Credential Providers — Implementation Plan

**Status**: All seven phases complete. Backend, both migrations, routes, regenerated client, frontend (11/11 surfaces at design review), and docs (§13) are landed.
**Feature name**: `ai-credential-providers`
**Supersedes the provisioning half of**: the admin AI credential provisioning and zero-touch onboarding work, as documented in `docs/application/ai_credentials/admin_ai_credential_provisioning.md` (their plan files are no longer in `docs/plans/`)
**Docs to update on completion**: `docs/application/ai_credentials/admin_ai_credential_provisioning.md` + `_tech.md`, `docs/application/ai_credentials/provider_adapters_tech.md`, `docs/README.md`

---

## 1. Overview

Today one record is both a credential and its own factory. `ManagedAICredential` holds a key
*and* the rule that hands that key out (`auto_provision_roles`), which is why
`AccountProvisioningService.on_account_created` has to scan credentials looking for a policy
field, and why "give this one key to every new agent-developer" is expressed as a property of a
key rather than as a rule.

This feature splits the two:

- **Provider** — *a source of keys plus the rule for who gets one.* Holds the secret, the vendor
  type, the auto-provision roles, and all the wiring policy (defaults, SDK modes, model
  overrides). Two kinds: `fixed_key` (one pasted key everyone shares) and `minted` (each person
  gets their own key created at the provider — OpenAI only).
- **Managed AI credential** — *the key that exists and who holds it.* Keeps its member list;
  when it belongs to a provider it is read-only for the key and every wiring flag.

A provider owns exactly one managed credential. Manual managed credentials keep working with
no provider at all.

```
                    ┌──────────────────────────────────────────┐
                    │  Provider  (ai_provider)                 │
                    │  kind: fixed_key | minted                │
                    │  type: anthropic | openai | …            │
                    │  secret: fixed key OR org Admin API key  │
                    │  rule:   auto_provision_roles            │
                    │  policy: set_as_default,                 │
                    │          set_user_sdk_defaults,          │
                    │          sdk_default_modes,              │
                    │          model_override_*, default_model │
                    └───────────────────┬──────────────────────┘
                                        │ owns exactly one (RESTRICT)
                                        ▼
                    ┌──────────────────────────────────────────┐
                    │  ManagedAICredential                     │
                    │  provider_id (nullable → manual record)  │
                    │  members: ManagedAICredentialMembership  │
                    └───────────────────┬──────────────────────┘
                                        │ one child per member
                                        ▼
                    ┌──────────────────────────────────────────┐
                    │  AICredential (owner_id = the user)       │
                    │  fixed_key → copy of the provider's key   │
                    │  minted    → that user's own minted key   │
                    └──────────────────────────────────────────┘
```

Nothing downstream of `AICredential` changes. The per-user child row is what makes the key
usable — `User.default_ai_credential_<mode>_id` points at it, `agent_environment` links it,
`/external/account-config` ships it to Desktop, and deleting it is how one person is revoked.

### Two scenarios this is built for

1. **Per-user spend tracking.** Admin creates an OpenAI project with a spend limit in the OpenAI
   console, connects it as a `minted` provider, sets `auto_provision_roles = [every role]`. Every
   new account gets its own minted key, so usage is attributable per person.
2. **An extra key for one role.** Admin creates a `fixed_key` provider of type `anthropic` with
   `auto_provision_roles = ["agent-developer"]` and `set_as_default = false`. Developers get the
   Anthropic key *in addition* to their OpenAI default; it appears in their settings and in
   Desktop and is selectable per agent, and it claims no default slot, so it does not collide
   with scenario 1.

---

## 2. Architecture Overview

### 2.1 Vocabulary (enforced in code and copy)

| Term | Means | Identifier |
|---|---|---|
| **Provider** | The new entity: a key source + provisioning rule | `AIProvider`, `ai_provider`, `/admin/ai-providers` |
| **Type** | The vendor: `anthropic`, `openai`, `google`, `minimax`, `openai_compatible` | `AICredentialType` |
| **Adapter** | The server-side module that knows how to talk to a type | `app/services/ai_providers/*`, `ProviderAdapterPublic` |
| **Managed credential** | The credential record and its member list | `ManagedAICredential` |
| **Member** | One person holding a key from a managed credential | `ManagedAICredentialMembership` |

"Provider" never means the vendor in user-visible copy after this change. The adapters endpoint
keeps its shape; only the labels around it change. This is the single most likely thing to
regress, so §12 adds an architecture test for it.

### 2.2 Component map

```
Admin UI  /admin/ai-credentials#providers ─────► AdminAiProvidersService (generated client)
          /admin/ai-credentials#managed-credentials ─► AdminLlmProvidersService (unchanged path)
          Invite wizard, Provisioning step ────► AdminUsersService.inviteUser({provider_ids})
                                                          │
Backend   app/api/routes/admin_ai_providers.py  ◄─────────┘
              │
              ├─► AIProvidersService              (CRUD, verify, rotate, apply-to-existing, delete gate)
              │       │
              │       └─► ManagedAICredentialsService  (owns the credential + members, unchanged role)
              │               └─► ai_credentials_service (per-user child rows, unchanged)
              │
              ├─► provisioning_policy.resolve_policy()   NEW — the single read path for wiring policy
              │
              └─► KeyProvisioningService          (minting/revocation, reads the secret via the provider)

Provisioning  AccountProvisioningService.on_account_created   → scans PROVIDERS, not credentials
              AccountProvisioningService.provision_explicit   → takes provider ids
```

### 2.3 Integration points

- `AccountProvisioningService` (`backend/app/services/users/account_provisioning_service.py`) —
  the auto-provision scan changes its source table.
- `InvitationService` / `InviteUserRequest` — `managed_credential_ids` → `provider_ids`.
- `KeyProvisioningService` — reads the minting secret through `provider_id` instead of
  `provider_admin_credential_id`.
- `ManagedAICredentialsService` — every read of a wiring field goes through the policy resolver.
- `model_discovery_service`, `external_account_config_service`, `ai_credential_shares`,
  `agent_environment` credential linking, bundle publisher wiring — **must remain unable to
  reach `ai_provider`**. That isolation is the load-bearing argument of
  `provider_admin_credential.py`'s module docstring and it survives this change; §4.3 states how
  it is re-verified now that the table also holds ordinary model keys.

---

## 3. Data Models

### 3.1 `ai_provider` — renamed and extended from `provider_admin_credential`

The existing table is **renamed, not replaced**. It already carries name, type, an encrypted
secret, a JSON config, verification state and `created_by_id`; the new columns are the rule and
the wiring policy. Renaming keeps every existing minted setup working with no data movement.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | UUID | no | uuid4 | PK, unchanged |
| `name` | varchar(255) | no | — | unchanged |
| `kind` | varchar(16) | no | `'minted'` (server default, see migration) | `fixed_key` \| `minted` |
| `provider_type` | varchar(50) | no | — | `AICredentialType`. Unchanged column; the DTO exposes it as `type` |
| `encrypted_secret` | text | no | — | Fernet. **Dual meaning by `kind`**: a model API key for `fixed_key`, an org administration secret for `minted`. See §4.1 |
| `config` | json | no | `'{}'` | `AIProviderConfig`: `organization_id`, `project_id` (minted only) |
| `base_url` | varchar(500) | yes | NULL | `fixed_key` + `openai_compatible` |
| `model` | varchar(255) | yes | NULL | `fixed_key` + types whose adapter sets `requires_model` |
| `auto_provision_roles` | json | no | `'[]'` | **The rule.** Moved from `managed_ai_credential` |
| `set_as_default` | bool | no | false | Moved |
| `set_user_sdk_defaults` | bool | no | false | Moved |
| `sdk_default_modes` | json | no | `'["conversation","building"]'` | Moved |
| `default_model` | varchar(255) | yes | NULL | Moved |
| `available_models` | json | yes | NULL | Moved |
| `model_override_conversation` | varchar(255) | yes | NULL | Moved. `""` in a request clears it (existing three-state contract) |
| `model_override_building` | varchar(255) | yes | NULL | Moved |
| `expiry_notification_date` | timestamptz | yes | NULL | Moved — it describes the key, and the provider owns the key |
| `last_verified_at` | timestamptz | yes | NULL | unchanged |
| `last_verify_error` | text | yes | NULL | unchanged |
| `created_by_id` | UUID | yes | NULL | FK `user.id` **ON DELETE SET NULL** — unchanged and for the unchanged reason (deleting the admin who pasted the secret must never destroy the ability to revoke what it minted) |
| `created_at` / `updated_at` | timestamptz | no | now | unchanged |

Indexes: keep `ix_provider_admin_credential_type` renamed to `ix_ai_provider_type`. Add
`ix_ai_provider_auto_provision` on `kind` — not on the JSON column; the auto-provision scan reads
every provider row (there are tens, not thousands) and filters roles in Python, exactly as
`AccountProvisioningService` does today.

**Invariants enforced in the service, not the schema** (schema-level CHECKs would need a
migration to change and these are policy):
- `kind = 'minted'` requires the adapter's `supports_minting` to be true. Only OpenAI qualifies
  (`backend/app/services/ai_providers/anthropic.py` has no `mint` — Anthropic's API cannot create
  keys, permanently). Every other type is `fixed_key`-only, and that is a normal path, not a
  degraded one.
- `kind = 'minted'` requires `config.project_id`, and the project must carry a spend limit —
  already enforced at mint time by `openai.py:195`, and additionally checked by `verify`.
- `kind = 'fixed_key'` requires `base_url` when the adapter sets `requires_base_url`, and `model`
  when it sets `requires_model`. Same authority as the existing credential create route.

### 3.2 `managed_ai_credential` — changes

| Change | Column | Notes |
|---|---|---|
| **renamed** | `provider_admin_credential_id` → `provider_id` | FK `ai_provider.id`, **ON DELETE RESTRICT** (see below) |
| **dropped** | `auto_provision_roles` | The rule lives on the provider. A managed credential is no longer a factory |
| **dropped** | `provisioning_mode` | Derived: no provider → `shared`; `fixed_key` → `shared`; `minted` → `minted`. The `ManagedAICredentialPublic.provisioning_mode` field stays on the wire, computed |
| **kept, shadowed** | `encrypted_data` | NULL on provider-owned rows; the provider is the only source of truth for the key |
| **kept, shadowed** | `set_as_default`, `set_user_sdk_defaults`, `sdk_default_modes`, `default_model`, `available_models`, `model_override_*`, `expiry_notification_date` | Still the real values for **manual** records, which keep every control they have today. Ignored on provider-owned rows |

**Why RESTRICT and not SET NULL.** Everywhere else in this domain the FK is SET NULL with a
service-level gate, because losing a pointer should degrade a capability rather than delete data.
Here the opposite is true: a provider-owned credential whose `provider_id` went NULL would
silently become a *manual* credential — fully editable, its shadowed columns suddenly load-bearing
and holding stale defaults, its key column NULL. That is a worse state than a refused delete. The
service deletes the credential first and the provider second (§5.4); RESTRICT is the backstop
under that ordering.

**Why the columns are shadowed rather than mirrored.** Copying the provider's policy onto the
credential row would make every existing read site work unchanged — and would create a second
copy of a number the provider owns, which is the exact anti-pattern
`ProviderAdminCredentialConfig` already documents for spend limits. A stale copy displayed as the
active policy is worse than no copy. §5.1 introduces one resolver instead, and §12 adds a test
that no other site reads those fields directly.

### 3.3 `ProvisioningPolicy` — a value object, not a table

```
ProvisioningPolicy (frozen dataclass, app/services/credentials/provisioning_policy.py)
    source: Literal["provider", "manual"]
    provider_id: UUID | None
    set_as_default: bool
    set_user_sdk_defaults: bool
    sdk_default_modes: list[str]
    default_model: str | None
    available_models: list[str] | None
    model_override_conversation: str | None
    model_override_building: str | None
    expiry_notification_date: datetime | None
    provisioning_mode: ProvisioningMode
```

`resolve_policy(session, parent) -> ProvisioningPolicy` is the **only** legal way to read any of
those facts about a managed credential. It returns the provider's values when `parent.provider_id`
is set and the credential's own columns when it is not. Batch form
`resolve_policies(session, parents)` for list endpoints, so the providers are fetched once.

### 3.4 Unchanged

`ManagedAICredentialMembership` (statuses `not_applicable`, `pending`, `minting`, `provisioned`,
`failed`, `suspended`), `AICredential`, `AICredentialShare`, and the whole mint/revoke machinery
in `KeyProvisioningService` keep their shapes. Only the lookup of the minting secret moves.

---

## 4. Security Architecture

### 4.1 The dual-meaning secret column

`encrypted_secret` now holds two categorically different things depending on `kind`. This is the
one place the change weakens an argument the codebase makes explicitly, so it is handled head-on:

- **`provider_admin_credential.py`'s module docstring must be rewritten, not left standing.** It
  currently states that the table holds a secret that "does not grant access to a model, it grants
  the power to create and destroy keys". That is false for `fixed_key` rows. Leaving it is exactly
  the failure mode recorded across this codebase — a comment that quantifies or claims
  completeness, with no test behind it. The rewritten docstring states the dual meaning, keys it
  to `kind`, and keeps the isolation argument, which is unaffected.
- **The isolation argument survives and is re-verified.** `model_discovery_service` selects from
  `ai_credential`; `external_account_config_service` reads rows a user owns from `ai_credential`;
  sharing, environment linking, bundle publisher wiring and blast-radius counts are all foreign
  keys pinned to `ai_credential.id`; the environment credential bag is a fixed slot dict. None of
  them can reach `ai_provider`, before or after this change. §12 adds a test asserting no query in
  `app/` selects `AIProvider` outside the provider service and the mint service.

### 4.2 Access control

Superuser-only, end to end. Every route on `/admin/ai-providers` takes
`get_current_active_superuser`, matching `admin_provider_credentials.py`. There is no user-facing
provider surface and no user-facing projection of one.

### 4.3 What is never exposed

- `AIProviderPublic` carries `has_secret: bool`, never the value, copying
  `ProviderAdminCredentialPublic` and `MailServerConfigPublic`. There is no reveal endpoint.
- A provider's fixed key is decrypted only to write a child `AICredential`, and only inside
  `ManagedAICredentialsService`.
- Security events (`admin.ai_provider.created|updated|rotated|deleted`,
  `admin.ai_credential.auto_provision`, `admin.ai_credential.auto_provision_failed`) never carry
  key material, provider responses, or anything but ids and coarse reason codes.
- Rotation logs the fact, never the before/after value.

### 4.4 Rate limiting and provider contact

`verify` and `test-connection` contact the provider and are superuser-only; keep the existing
behaviour. **Nothing on the account-creation path contacts a provider** — invariant 2 of
`AccountProvisioningService` is unchanged: a `fixed_key` grant is a database write, and a minted
grant writes a `pending` membership row and stops. A provider timeout must never become a failed
signup.

**Correction (Phase 4).** This paragraph used to say a minted grant "enqueues
`KeyProvisioningService.converge` as a background task". It does not, and nothing on the request
path does: `key_provisioning_scheduler.py:56` is the only caller of `converge` in `app/`, and it is
a periodic single-leader sweep that picks the `pending` row up on a later tick. The invariant the
paragraph asserts is unaffected — nothing is awaited on the request path either way — but the
mechanism was misdescribed, and the misdescription had already been repeated onward.

---

## 5. Backend Implementation

### 5.1 New module — `backend/app/services/credentials/provisioning_policy.py`

- `ProvisioningPolicy` frozen dataclass (§3.3).
- `resolve_policy(session, parent)` / `resolve_policies(session, parents)`.
- `policy_from_provider(provider)` / `policy_from_manual(parent)` — the two constructors, so the
  fallback is one branch in one place.

Call sites to convert in `managed_ai_credentials_service.py`: `_stamp_child`, `_model_override_for`,
`_apply_sdk_defaults`, `_sync_model_overrides`, `_add_child`, `_update_child_fields`,
`_claimed_slots`, `reconcile`, `apply_to_existing`, `set_default_all`, `_to_public`,
`_decrypt_parent`, `resolve_test_key`.

### 5.2 New service — `backend/app/services/credentials/ai_providers_service.py`

| Method | Purpose |
|---|---|
| `create(session, data, actor)` | Validates kind/type/shape against the adapter; encrypts the secret; creates the provider **and its one managed credential** in the same transaction; returns both |
| `update(session, provider_id, data, actor)` | Partial update. Any wiring-policy change re-applies to every existing member (§5.3). Three-state `model_override_*` contract preserved verbatim from `ManagedAICredentialUpdate` |
| `rotate_key(session, provider_id, api_key, actor)` | `fixed_key` only. Re-encrypts on the provider and writes the new key through to every member's child `AICredential`. Refused on `minted` — there is nothing here to rotate; rotating a minted member's key is a per-member re-mint |
| `verify(session, provider_id)` | `minted`: existing admin-API + spend-limit check. `fixed_key`: the existing `test-connection` call for the type. Writes `last_verified_at` / `last_verify_error` |
| `apply_to_existing(session, provider_id, actor)` | The explicit "grant this to everyone who already matches the roles" action, moved off the credential. Adds members for existing active accounts whose role is in `auto_provision_roles` and who are not already members |
| `delete(session, provider_id, force, actor)` | The gate — §5.4 |
| `list / get` | Projection with `owned_credential_id`, `member_count`, `key_state_summary` |

`ManagedAICredentialsService` keeps its role unchanged (it owns the credential and its members)
and gains: refusal of any write to a shadowed field on a provider-owned record (400, naming the
provider), and refusal of manual creation with a non-null `provider_id`.

### 5.3 Policy edits re-apply to existing members

This is a stated requirement, and the machinery already exists — it just has to be reached from
the provider. `AIProvidersService.update` calls into
`ManagedAICredentialsService.reconcile` / `_update_child_fields` / `_sync_model_overrides` for the
owned credential, with `cleared_overrides` carried down so a cleared override actually retracts
members' pins rather than being cosmetic. A change to `default_model` / `available_models` writes
through to every child row; a change to `model_override_*` writes through to every member whose
default still points at their child.

### 5.4 Provider deletion gate

Modeled on the existing credential-deletion blast-radius gate (409 unless `force`):

1. `DELETE /admin/ai-providers/{id}` with no `force` → if the owned credential has **any**
   membership row, respond **409** with an impact body: member count, minted-key count
   (`holds_provider_key`), and the affected users' labels.
2. With `force=true` → delete in order: revoke every minted key at the provider
   (`KeyProvisioningService.schedule_revocations`, and a revoke that cannot be scheduled is
   recorded as a durable event with its ref — the existing mint invariant, unchanged), delete the
   child `AICredential` rows, delete the memberships, delete the managed credential, then delete
   the provider. RESTRICT guarantees an ordering bug surfaces as a database error, not as an
   orphan.
3. A provider whose credential has zero members deletes without a confirm.

### 5.5 Default-slot conflict resolution

Two rules, at two different times.

**Write time (configuration).** `_validate_auto_provision_uniqueness` moves to
`AIProvidersService` and guards **provider vs provider** only: two providers may not both claim
the same `(role, mode)` default slot, because the outcome would otherwise depend on row order.
The existing transition-scoped semantics are preserved verbatim — only slots a request *newly*
claims can 409, so editing a provider that already sits in a conflicting configuration does not
fail on a collision the admin did not introduce. `_claimed_slots` still returns empty unless a
provider does all three things (auto-provisions, wires SDK defaults, claims a mode), so an
"extra key" provider with `set_as_default = false` never collides with anything.

**Run time (a real user's slot).** `_apply_sdk_defaults` currently claims the slot
unconditionally. It becomes: **the incumbent wins.**

- Slot empty → claim it.
- Slot already held by any credential → do **not** claim; record a skip on the reconcile result.

That single rule delivers what was asked — manual credentials beat provider-provisioned ones, and
between two manual the first created wins — because the incumbent is by construction the earlier
one. This situation is normal and expected (a user pastes their own key, then a provider grants
them one); it is resolved, never prevented, and provisioning still creates the credential and the
membership.

The escape hatch stays explicit: `apply_to_existing` and the existing `set-default-all` action are
deliberate admin acts and **do** overwrite. Automatic provisioning never steals a default.

### 5.6 `AccountProvisioningService` changes

- `on_account_created` scans `AIProvider` for `auto_provision_roles ∋ user.role` instead of
  scanning managed credentials, then grants through the owned credential's `add_members` — the
  same shared `_provision` / `_guarded` body, so an invited and a Google-arriving `agent-user`
  cannot diverge. Both invariants hold unchanged: account creation never fails because
  provisioning failed, and no third party is contacted on this path.
- `provision_explicit(provider_ids)` replaces `provision_explicit(managed_credential_ids)`.
- Skip reason `managed_credential_not_found` becomes `provider_not_found`; `InviteProvisioningSkip`
  gains `provider_id` in place of `managed_credential_id`. The frontend's reason→copy map must
  gain the new value — `frontend/src/components/Admin/InviteSuccessPanel.tsx:240` reads
  `SKIP_REASON_COPY[skip.reason] ?? skip.reason`, so a reason it has never heard of renders the
  literal token `provider_not_found` inside an admin-facing sentence.
- Role changes on existing accounts still provision nothing. Auto-provisioning stays
  creation-time only; `apply_to_existing` is the explicit path.

### 5.7 API routes

**New — `backend/app/api/routes/admin_ai_providers.py`**, prefix `/admin/ai-providers`,
tag `admin-ai-providers` (generated service `AdminAiProvidersService`). Replaces
`admin_provider_credentials.py`, which is deleted.

| Method | Path | Body / params | Response |
|---|---|---|---|
| POST | `/` | `AIProviderCreate` | `AIProviderPublic` |
| GET | `/` | — | `list[AIProviderPublic]` |
| GET | `/{provider_id}` | — | `AIProviderPublic` |
| PATCH | `/{provider_id}` | `AIProviderUpdate` | `AIProviderPublic` (409 on slot conflict) |
| DELETE | `/{provider_id}` | `?force=bool` | 409 `AIProviderDeleteImpact` unless forced |
| POST | `/{provider_id}/verify` | — | `AIProviderVerifyResult` |
| POST | `/{provider_id}/rotate-key` | `{api_key}` | `AIProviderPublic` (400 on `minted`) |
| POST | `/{provider_id}/apply-to-existing` | — | `ManagedAICredentialReconcileResult` |
| GET | `/adapters` | — | `ProviderAdaptersPublic` — moved from `admin_provider_credentials.py` unchanged |

**Changed — `admin_llm_providers.py`** (`/admin/llm-providers`, the managed-credential router).
Path and tag are kept so the Managed Credentials surface does not churn. `ManagedAICredentialCreate`
loses `auto_provision_roles` and `provider_admin_credential_id`; `provisioning_mode` becomes
create-time-invalid (a manual record is always `shared`). `ManagedAICredentialUpdate` refuses any
shadowed field on a provider-owned record with a 400 naming the provider. `ManagedAICredentialPublic`
gains `provider_id`, `provider_name`, `is_provider_owned`, and computes `provisioning_mode` and
`has_api_key` through the policy resolver.

**Removed — `POST /admin/llm-providers/{id}/apply-to-existing`.** Redundant with
`POST /admin/ai-providers/{id}/apply-to-existing` above: for a provider-owned record it ran the
identical `ManagedAICredentialsService.apply_to_existing` call, and for a manual record — which can
never carry `auto_provision_roles` — it always answered `candidate_count: 0`. Cut rather than kept
as an honest no-op, on the precedent this table's own `rotate-key` row sets: rotating a `minted`
provider is a 400, not an accepted rotation that rotates nothing. `ManagedAICredentialsService
.apply_to_existing` is unaffected — `AIProvidersService.apply_to_existing` still delegates to it,
and it remains the reconcile both routes ever ran.

**Changed — the invite route.** `InviteUserRequest.managed_credential_ids` → `provider_ids`,
tri-state preserved exactly (`null` = whatever the role would get anyway, `[]` = the admin
deliberately unticked everything, a list = exactly these).

**Decided in Phase 4 — a declined default slot is disclosed on the wire.** A grant that lands
while the owner already holds a default in that mode produces a `ManagedDefaultSlotSkip`, and it is
now carried up: `MemberAddition.default_slot_skips` → `ProvisioningReport.default_slot_skips`
(`ProvisioningDefaultSlotSkip`, carrying `provider_id` + `mode`) →
`InviteProvisioningSummary.default_slot_skips` (`InviteProvisioningDefaultSlotSkip`, same two
fields). Without it the admin is told "granted" while the member's SDK wiring silently did not
happen.

**Shaped as disclosure, not as an error**, in the same sense as `adopted_existing_account`: the
grant succeeded and the person holds a usable key. So it does **not** set `provisioning_failed` and
it does **not** join `skipped`, which means "this person did not receive the key" and would render a
successful grant as a failure. §6.6's copy map has to render it as a note beside a success.

**Why it is an ordinary path rather than the exotic one Phase 3 assumed.** Phase 3 described it as
reachable only through an invite ticking two providers that wire the same mode with
`auto_provision_roles = []`. The real producer is much plainer: `invitation_service.py:962` calls
`provision_explicit` unconditionally, *after* the adoption branch at `:939`. A re-invite of an
account that already exists — an interrupted earlier attempt, or the passwordless account a server
channel created for an inbound sender — therefore runs explicit provisioning against somebody who
may already hold a default, from an earlier provider grant or from a key their own owner pasted.
**One ticked provider is enough.** §5.5's uniqueness rule does not protect that path, because it
guards *configuration* — two providers claiming the same `(role, mode)` — not one provider meeting
one person's occupied slot.

The credential already sitting in the slot is deliberately **not** carried up: it is an
`ai_credential` id belonging to the invited person's own configuration, and the wizard has no name
for it. Provider and mode are what an administrator can act on.

---

## 6. Frontend Surfaces (inventory — composition decided by `cinna-core.ui.design`)

> Composition, density, placement and pattern for every surface below are decided by
> `cinna-core.ui.design`, which appends a `## UI Specification` to this plan. That specification is
> a **gate**: no frontend code before it exists. The list here is intent, data and actions only.

### 6.1 `/admin/ai-credentials` — hash navigation
- **Exists**: `frontend/src/routes/_layout/admin/ai-credentials.tsx` (tab state is local `useState`).
- **Intent**: the admin wants to link to, and come back to, a specific tab.
- **Change**: replace the local tab state with the existing `frontend/src/components/Common/HashTabs.tsx`
  (as `frontend/src/routes/_layout/settings.tsx` uses it). Tabs: `managed-credentials`, `providers`.
- **Constraint**: the page already latches `?newCredentialFor=` out of the URL and strips it. The
  hash and that search param must coexist — stripping the search must not clear the hash.

### 6.2 Providers tab
- **New surface**, replacing the "Provider Keys" tab
  (the `Admin/ProviderAdminCredentials/` directory, deleted in Phase 6).
- **Intent**: the admin wants to see where keys come from, who automatically gets one, and whether
  the connection still works.
- **Data**: name, kind, type, auto-provision roles, member count, key-state summary, whether
  defaults are wired and for which modes, `organization_id` / `project_id` (so two providers into
  the same org are visible rather than silent), last verified / last error.
- **Actions**: create, edit, verify, rotate key (`fixed_key` only), apply to existing users, delete.
- **API**: `AdminAiProvidersService.*`.

### 6.3 Provider create/edit dialog
- **New surface**.
- **Intent**: the admin wants to say where keys come from and who gets one.
- **Data/inputs**: name; **kind** (`fixed_key` / `minted`, with `minted` unavailable and *explained*
  for types whose adapter cannot mint); **type** (fed from `GET /admin/ai-providers/adapters`, never
  a hardcoded client list); the secret; `organization_id` / `project_id` for minted; `base_url` /
  `model` where the adapter requires them; auto-provision roles; `set_as_default`;
  `set_user_sdk_defaults` + modes; `default_model` / `available_models`; per-mode model overrides.
- **Contract gap**: the dialog must state that editing policy re-applies to existing members, and
  the slot-conflict 409 must render as copy naming the other provider, not a raw error.
- **Reuses**: the existing model-picker (`ListModelsButton`) and role-picker patterns.

### 6.4 Managed credential dialog — now conditionally read-only
- **Exists**: `frontend/src/components/Admin/LlmProviders/ManagedCredentialDialog.tsx`.
- **Intent**: unchanged for manual records; for provider-owned records the admin wants to see what
  the provider decided and manage *who holds it*.
- **Change**: on `is_provider_owned`, the key and every wiring control render read-only with
  "Managed by provider *X*" and a link to that provider; membership stays editable.
- **Anti-pattern risk** (`docs/development/frontend/ui_ux_guidelines.md` §4): a dialog that shows
  disabled controls with no explanation. The read-only state must say who owns the value and where
  to change it.

### 6.5 Managed credentials table
- **Exists**: `frontend/src/components/Admin/LlmProviders/LlmProvidersTable.tsx` (already one row
  per record with members as chips — unchanged).
- **Change**: the "Auto-provision" column moves to the Providers tab; a "Source" column
  (provider name, or "Manual") replaces it.

### 6.6 Invite wizard, Provisioning step
- **Exists**: `frontend/src/components/Admin/InviteUserDialog.tsx` (`:104-117`, `:490-508`).
- **Intent**: the admin wants to see, and adjust, what this person will get before sending.
- **Change**: lists **providers**, preselected where `auto_provision_roles` includes the invited
  role — the same predicate every other arrival path runs, now read off the provider. Tri-state
  submission preserved. The skip-reason copy map gains `provider_not_found`.

### 6.7 State management
- New query key `["admin", "ai-providers"]`; the existing
  `PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY` is retired.
- Any provider mutation invalidates both the provider list and
  `managedCredentialsQueryKey(...)` — a policy edit changes member rows.
- The managed list's existing in-flight poll (`hasKeyInFlight`, 10s) is unchanged and must also
  run after a provider `apply-to-existing` on a minted provider.

---

## 7. Database Migration

Single Alembic revision, down-revision = current head. Run `docker compose exec backend alembic heads`
before writing it; do not assume.

**Upgrade**
1. `ALTER TABLE provider_admin_credential RENAME TO ai_provider`; rename its index and FK
   constraints to match.
2. Add the new columns (§3.1) with server defaults; `kind` server default `'minted'`, because every
   pre-existing row in this table is an org admin secret.
3. `ALTER TABLE managed_ai_credential RENAME COLUMN provider_admin_credential_id TO provider_id`;
   drop and recreate the FK as `ON DELETE RESTRICT`; rename its index.
4. **Backfill, in this order:**
   a. For each existing `ai_provider` row (all `minted`), count the managed credentials pointing at
      it. `0` → create an empty minted managed credential so the 1:1 invariant holds. `1` → nothing
      to do. `>1` → **split**: keep the first, and for each additional credential insert a new
      provider row copying name (suffixed with the credential's name), type, secret, config, and
      repoint that credential. This duplicates the admin secret, which is the accepted consequence
      of storing it on the provider.
   b. For each `managed_ai_credential` with `provider_id IS NULL` and non-empty
      `auto_provision_roles`: insert a `fixed_key` provider carrying that record's name, type, key
      (moved from `encrypted_data`), `base_url`, `model`, and **all** of its wiring policy; set the
      credential's `provider_id`; NULL its `encrypted_data`.
   c. Copy each minted parent's wiring policy up onto its provider (step a already established the
      1:1 pairing).
5. Drop `managed_ai_credential.auto_provision_roles` and `managed_ai_credential.provisioning_mode`.
6. **Fail loudly.** Runs after steps 1-3's renames but before the backfill moves a single row. Five
   non-overlapping shapes abort the migration, each naming the offending id: an `auto_provision_roles`
   that is not a JSON array (checked first, because every later check calls `json_array_length` on it
   and would otherwise surface a bare Postgres type error with no id in it); a `shared` credential with
   non-empty `auto_provision_roles` but `encrypted_data IS NULL`, which auto-provisions by role but
   holds no key to build a provider from; a `minted` credential with `provider_id IS NULL`, with no
   provider admin credential to mint through; a `shared` credential with `provider_id IS NOT NULL`,
   whose pointer says "mint" while its mode says "copy one key" — either reading rewrites what its
   members hold; and a provider or auto-provisioning credential whose type resolves to no adapter,
   checked against the adapter registry rather than a type list written into the migration so the
   check cannot drift from the adapters as they change. Silence here means an admin discovers a
   company key stopped being handed out weeks later. One test per shape in
   `backend/tests/migrations/ai_provider_split_test.py`:
   `test_a_role_list_that_is_not_a_list_aborts_the_migration`,
   `test_a_rule_with_no_key_behind_it_aborts_the_migration`,
   `test_a_minted_credential_with_nothing_to_mint_through_aborts`,
   `test_a_shared_credential_pointing_at_an_admin_secret_aborts`,
   `test_a_type_no_adapter_serves_aborts_the_migration`.

**Downgrade**: reverse the renames, re-add the dropped columns, copy policy back down from the
provider onto its credential, restore `encrypted_data` from the provider for `fixed_key` rows, and
drop the providers created by step 4a/4b. Providers created *after* the migration cannot be
represented downgrade-side; the downgrade raises if it finds one whose credential has members.

---

## 8. Error Handling & Edge Cases

| Case | Behaviour |
|---|---|
| Two providers claim the same `(role, mode)` slot | 409 at write time, naming the other provider and the role/mode |
| A user already has a default in a slot a provider would claim | The provider grants the credential and does **not** claim the slot. Reported as a skip on the reconcile result, not as an error |
| `minted` chosen for a type that cannot mint | 400 from `AIProvidersService._validate_shape`. The wizard should not offer it; **`ProviderAdapterPublic.supports_minting`** is the authority — one term, a fact about the vendor's API. (`can_mint_now`, the conjunction this row used to name, is deleted: see the §12 row on the adapters projection.) |
| Minting into an uncapped OpenAI project | Refused by `openai.py`, surfaced as a `failed` membership with a coarse reason. Unchanged |
| Provider deleted while it has members | 409 with the impact body; `force=true` revokes and deletes in the §5.4 order |
| Provider's secret revoked at the provider | `verify` records `last_verify_error`; existing child keys keep working (they are separate keys) for `minted`, and stop working for `fixed_key`. The list surfaces the error |
| Rotation on a `minted` provider | 400 — nothing here to rotate. Silently accepting it is how an admin comes to believe they rolled a key they did not |
| A shadowed field edited on a provider-owned credential | 400 naming the provider |
| Provisioning fails during account creation | Unchanged: recorded in the report, written to the security feed, logged at warning; the account is still created. `_restore_session` runs before anything is logged |
| Invite ticks a provider that no longer exists | `provider_not_found` skip; the invitation still goes out |
| A `minted` provider auto-provisions at signup | The membership is created `pending` and `KeyProvisioningService.converge` runs in the background. The signup request never waits on OpenAI |

---

## 9. UI/UX Considerations (business meaning only — the rest belongs to the UI Specification)

- **Kind vocabulary**: "Fixed key — everyone shares one key you paste" / "Per-user keys — each
  person gets their own, created in your provider account". The second is what makes per-user spend
  tracking possible, and that is the reason an admin picks it; say so.
- **`set_as_default` is the "extra key" switch.** A provider with auto-provision on and defaults off
  grants an additional key that does not displace anyone's default. This is a headline
  configuration, not an edge case.
- **Membership status vocabulary** is unchanged and already has copy: `not_applicable`, `pending`,
  `minting`, `provisioned`, `failed`, `suspended`.
- **Spend limit is read, never written** — it lives in the provider's own console. Cinna refuses to
  mint into an uncapped project and says so; it never displays a locally stored limit.
- **Destructive confirmation** for provider deletion must name the people who lose a key and say
  whether keys will be revoked at the provider.
- **Accessibility**: the read-only state of a provider-owned credential must be conveyed in text,
  not by disabled styling alone.

---

## 10. Future Enhancements (out of scope)

- Sharing one org admin secret across several providers (the "organisation connection" record).
  Deliberately not built: the secret lives on the provider by decision. Revisit if two OpenAI
  projects under one org becomes a real configuration.
- Re-provisioning on role change. Creation-time only, by decision.
- Per-provider spend reporting / usage attribution surfaces.
- Provider-scoped key scopes (the OpenAI scope vocabulary is not authoritative anywhere; minted
  keys are documented as broad).
- Group-based rules beyond roles (IdP group mapping).

---

## 11. Phases

| Phase | Content | Gate |
|---|---|---|
| **1** | Migration + models (§3, §7) | Migration runs up and down on a seeded DB |
| **2** | `provisioning_policy` resolver + `AIProvidersService` + `ManagedAICredentialsService` refactor + conflict rules (§5.1, §5.2, §5.3, §5.5) | Domain tests green |
| **3** | `AccountProvisioningService` + invite (§5.6) | Invite and signup provisioning tests green |
| **4** | Routes + client regen (§5.7) — `bash scripts/generate-client.sh`. Also decides §5.7's one open item inherited from Phase 3: whether `InviteProvisioningSummary` carries declined default slots | `npx tsc --noEmit` clean for touched files |
| **5** | **`cinna-core.ui.design`** produces the `## UI Specification` | Specification appended to this plan |
| **6** | Frontend (§6), built to the specification. Two copy items the backend already produces: the reason→copy map **must** gain `provider_not_found` (§5.6), and declined default slots need copy if Phase 4 put them on the wire (§5.7) | `cinna-core.ui.review` returns PASS |
| **7** | Docs (§13) + full backend suite | Suite green, docs reference-checked |

---

## 12. Testing & Validation Tasks

- Migration: up and down against a database holding all five shapes — a minted parent, two minted
  parents on one admin credential (the split), a shared parent with roles, a shared parent without
  roles, and an admin credential with no parents.
- Migration fails loudly on each unclassifiable shape.
- **[Phase 2 — done]** Policy resolver returns the provider's values for provider-owned records and
  the credential's own for manual ones; a change on the provider is visible immediately with no
  second copy anywhere. — `tests/unit/test_provisioning_policy.py` (the branch, per field, against a
  credential whose own columns deliberately disagree with the provider's), plus the session
  round-trip and the "one copy" assertion in
  `tests/api/ai_credentials/ai_providers_service_test.py::test_a_policy_edit_writes_through_to_every_existing_member`.
- **[Phase 2 — done]** **Architecture test**: no module outside `provisioning_policy.py` reads
  `ManagedAICredential.{set_as_default, set_user_sdk_defaults, sdk_default_modes, default_model, available_models, model_override_*, expiry_notification_date}` directly.
  — `tests/architecture/managed_credential_shadowed_fields_test.py`. Reads only, not writes: a manual
  record's columns are still written, and the provider-owned path refuses those writes at the edge
  (`ManagedAICredentialsService._refuse_shadowed_writes`, whose field list the same file pins).
- **[Phase 2 — done, one row still xfail]** **Architecture test**: no query in `app/` selects
  `AIProvider` outside `ai_providers_service.py` and `key_provisioning_service.py` (the isolation
  claim of §4.1). — `tests/architecture/ai_provider_isolation_test.py`. The live gate passes with
  three named, phase-tagged exceptions (`account_provisioning_service` → Phase 3;
  `provider_admin_credentials_service` and `admin_provider_credentials` → Phase 4), asserted by
  equality so a landed phase fails the file instead of leaving a stale excuse. The exception-free
  form is a second test, `xfail(strict=True)`, which turns green — and therefore fails — the day
  Phase 4 lands.
- **[Phase 2 — done]** Policy edit re-applies to existing members: `default_model`,
  `available_models` and both `model_override_*` write through; a cleared override retracts members'
  pins and only those it wrote. — `ai_providers_service_test.py::test_a_policy_edit_writes_through_to_every_existing_member`
  and `::test_clearing_a_providers_override_retracts_the_pins_it_wrote`.
- **[Phase 2 — done]** Rotation on a `fixed_key` provider re-keys every member's child row; rotation
  on `minted` is a 400. — `ai_providers_service_test.py::test_rotating_a_fixed_key_provider_re_keys_every_member`
  and `::test_rotating_a_minted_provider_is_refused_rather_than_accepted`.
- **[Phase 2 — done]** Slot conflict: provider-vs-provider 409 at write time, scoped to newly claimed
  slots; an auto-provisioning provider with `set_as_default=false` never collides. —
  `ai_providers_service_test.py::test_two_providers_cannot_claim_the_same_role_and_mode_default`,
  `::test_an_update_that_would_introduce_a_conflict_is_refused_and_applies_nothing` and
  `::test_set_as_default_is_not_covered_by_the_uniqueness_rule`. The service raises
  `AIProviderConflictError`; the 409 mapping is the route's, in Phase 4.
- **[Phase 2 — done]** Incumbent wins: a user with a manual default keeps it when a provider grants
  them a credential, and the grant still happens; `apply_to_existing` and `set-default-all` do
  overwrite. — `ai_providers_service_test.py::test_automatic_provisioning_does_not_take_a_default_somebody_holds`,
  its control `::test_automatic_provisioning_still_claims_an_empty_slot`,
  `::test_apply_to_existing_is_the_escape_hatch_and_does_overwrite`, and
  `::test_a_declined_slot_is_reported_as_a_slot_skip_not_a_skipped_member`.
  **Two narrowings of §5.5, both deliberate:**
  (a) *`reconcile` claims too.* The rule is `claim_held_slots`, keyword-only on `_apply_sdk_defaults`
  and `add_members`, and it is **automatic provisioning** that declines — `AccountProvisioningService`
  and the minted materialise that follows it. `reconcile` passes True, because a superuser typing a
  member into `target_user_ids` has always claimed the slot for a *manual* record, and §3.2 keeps
  every control a manual record has today. Flipping reconcile to incumbent-wins was tried and breaks
  `managed_ai_credential_auto_provision_test.py::test_claiming_a_slot_resets_the_override_but_holding_one_does_not_wipe_it`,
  which is that control asserted.
  (b) *The skip rides the grant result, not the reconcile result.* §5.5 says "record a skip on the
  reconcile result"; given (a), that field would be empty on every response it ever appeared in — a
  worse lie than not offering it. `ManagedDefaultSlotSkip` is carried on `MemberAddition`, the
  add-only shape the automatic path returns. **Phase 4 gave it a wire surface** — see the Phase 4
  entry below — so this note's original "and giving it one belongs to Phase 3" no longer describes
  anything owed.
- **[Phase 2 — done]** Delete gate: 409 with impact, forced delete revokes minted keys and removes
  rows in order, RESTRICT holds if the order is wrong. —
  `ai_providers_service_test.py::test_deleting_a_provider_with_members_is_refused_then_forced` and
  `::test_a_provider_nobody_holds_deletes_without_a_confirmation` for the gate and the ordered
  removal; `minted_ai_credentials_test.py::test_disconnecting_a_provider_organisation_is_refused_while_keys_live`
  for the minted force, which asserts the key was destroyed at the provider. RESTRICT itself is
  already pinned by `tests/migrations/ai_provider_split_test.py::test_a_provider_cannot_be_deleted_out_from_under_its_credential`
  and is not duplicated here.
  **Ordering note:** §5.4's step order (revoke, then the rows, then the provider) is implemented as
  written, but with `revoke_now` **awaited** rather than `schedule_revocations` fired. A *scheduled*
  revocation runs after `session.delete(provider)` has committed, finds no administration secret,
  and records every key as `revoke_failed` while it stays live at the vendor. `AIProvidersService.delete`
  is `async` as a result, and clears the handles afterwards so the credential delete cannot schedule
  a second, doomed attempt at the same key. Residual, stated in the method's docstring: a forced
  delete that then fails to remove a member leaves that member holding a revoked key and answers 409;
  the retry completes the removal.
- **[Phase 2 — added after review]** `api_key` is refused on a provider-owned credential, and
  `_decrypt_parent` **prefers** the provider rather than falling back to it. Without both, a
  `PATCH /admin/llm-providers/{id}` carrying `api_key` wrote `encrypted_data` on a record whose key
  the provider owns, and the next `rotate_key` re-keyed every member from that stale copy while
  reporting success — the rotation refusal defeated through the other door. —
  `ai_providers_service_test.py::test_a_provider_owned_credential_refuses_a_key_of_its_own`.
- **[Phase 2 — added after review]** §5.2's "refusal of manual creation with a non-null `provider_id`",
  which had been missed. Two shapes: a credential holding its own key *and* pointing at a provider
  (for a `minted` provider that is the shape §7 step 6 aborts the migration on, creatable through a
  live route), and a **second** credential on a provider that already owns one — invisible, because
  `owned_credential` resolves the older row, while still handing out keys and rewriting the
  provider's wiring under its existing members. —
  `ai_providers_service_test.py::test_a_manual_credential_cannot_be_created_against_a_provider`.
- **[Phase 2 — added after review]** The escape hatch works for **minted** providers.
  `claim_held_slots` never reached `materialise_minted_child`, so for every minted provider
  `apply_to_existing` silently stopped overwriting — the provider kind §1 scenario 1 is built on.
  The intent is now persisted on the membership row
  (`claim_held_default_slots`, migration `45938a69aee7`), because the grant and the wiring happen on
  different requests. — `ai_providers_service_test.py::test_the_escape_hatch_overwrites_for_a_minted_provider_too`,
  which asserts both directions in one test.
- **[Phase 2 — added after review]** `AIProvidersService.create` writes the pair in one transaction
  (a `flush` to order the inserts — nothing declares a relationship, so the unit of work emitted the
  child first and tripped the FK — then one `commit`). Two commits made the 1:1 invariant breakable
  by the method that exists to hold it.
- **[Phase 2 — added after review]** A provider `base_url`/`model` edit re-encrypts the `fixed_key`
  envelope. `_add_child` builds a new member's credential out of that envelope while the write-through
  diffs the credential's columns, so members added before and after an edit held different shapes. —
  `ai_providers_service_test.py::test_editing_a_providers_base_url_reaches_members_added_afterwards`.
- **[Phase 2 — added after review]** `AIProvidersService._claimed_slots` and
  `ProvisioningPolicy.claimed_slots` are asserted to agree across seven configurations. Two
  implementations of one rule, previously only claimed equal in prose. —
  `tests/unit/test_provisioning_policy.py::test_the_services_prospective_claim_matches_the_resolved_one`.
- **[Phase 3 — done]** Auto-provision at signup and at invite grant the same set with the same
  events; a minted provider never blocks the request. —
  `tests/api/users/users_auto_provision_origins_test.py::test_an_invite_and_a_signup_of_the_same_role_are_granted_the_same_set`,
  which runs both arrivals inside the outbound-HTTP guard (proved armed), against three providers —
  a `fixed_key` and a `minted` one for `agent-user` and a third for `agent-developer` only, so
  "the same set" means *the role's* set rather than "everything configured". It asserts the minted
  grant is a `pending` membership holding no key on both paths, and that the two security feeds
  carry the same event type against the same provider ids while differing in exactly the two fields
  that must differ, `origin` and `actor`.
- **[Phase 3 — done]** The tri-state of `provider_ids` survives: `null` grants the role's automatic
  set, `[]` grants nothing, a list grants exactly those and *not* the automatic set as well. All
  three run against one setup whose second provider auto-provisions nobody, because read one at a
  time each shape is satisfiable by accident. —
  `users_invitation_lifecycle_test.py::test_the_provisioning_tri_state_grants_exactly_what_the_admin_stated`.
  Verified by mutation: softening `if provider_ids is None` to `if not provider_ids` fails it.
- **[Phase 3 — done]** The `provider_not_found` skip is produced, alongside a real
  `provision_failed` on the same request. —
  `users_invitation_lifecycle_test.py::test_a_provisioning_failure_does_not_cost_the_invitation`.
  Its second producer — a provider that exists but owns no credential to grant through, which the
  superseded `/admin/provider-credentials` route can still create — is
  `::test_a_provider_with_no_credential_to_grant_through_is_reported`, which also pins that the
  grantable half of the same request still lands. Its *rendering* is Phase 6: the wizard's
  reason→copy map still carries `managed_credential_not_found` and must gain `provider_not_found`,
  or the skip renders the literal token `provider_not_found` — `InviteSuccessPanel.tsx:240` falls
  back to `skip.reason`, not to an empty string.
- **[Phase 3 — added after review]** A failure on the *second* granted provider must not be
  reported as the whole of provisioning falling over. `parent.id` and `parent.managed_by_id` were
  read **above** the per-provider `try`; `add_members` commits and `expire_on_commit` is on, so from
  the second provider onward those reads are a refresh `SELECT`, and a failure there escaped to
  `_guarded` — which answers `provisioning_failed=True` with empty lists and disowns every grant
  already made. Both reads moved inside the net, and the audit event tolerates a `None`
  `managed_credential_id` (the provider still names the grant). —
  `users_invitation_lifecycle_test.py::test_a_failure_on_the_second_provider_keeps_what_the_first_one_granted`,
  which injects at the primary-key refresh rather than the prologue lookup and asserts *which*
  provider was skipped with *which* reason, so an injection that drifts fails instead of passing on
  the wrong failure.
- **[Phase 3 — added after review]** The skip log line said "Auto-provisioning" on both paths,
  which is the failure `_LABEL_AUTOMATIC` / `_LABEL_INVITE` exist to prevent — an admin debugging a
  failed invitation greps for a string that was never written. It uses `label` now, like its
  sibling handler.
- **[Phase 3 — done]** A role change on an existing account provisions nothing — the promotion and
  the grant are separate decisions, and `apply_to_existing` is the explicit path. The control is
  that the provider covers the role by the end, so "nothing was granted" can only mean "nothing
  re-ran". — `users_auto_provision_origins_test.py::test_a_role_change_on_an_existing_account_provisions_nothing`.
- **[Phase 3 — done]** The isolation test's `PENDING` entry for
  `account_provisioning_service` is deleted with the violation it excused; the module reaches
  providers through `AIProvidersService.auto_provision_targets` / `grant_targets`. Two entries
  remain, both Phase 4.
- **[Phase 4 — done]** The routes the earlier phases' service tests could only reach through a seam.
  `tests/api/ai_credentials/admin_ai_providers_test.py` replaces
  `provider_admin_credentials_test.py` and carries its security assertions across to
  `/admin/ai-providers`: the secret in no response body (asserted on the raw key set, so an extra
  key the response model never declared is visible), superuser-only on every endpoint including
  `/adapters`, the isolation invariant made executable against the four generic credential surfaces,
  and the two structural router tests — every route declares a `response_model`, and every declared
  model is in a reviewed set of secret-free projections, enumerated over `router.routes` so a tenth
  endpoint cannot be added without somebody deciding in writing that its shape is safe.
- **[Phase 7 — reconciled]** The adapters-projection test's *meaning* changed with `can_mint_now`'s
  deletion, and this row is what it says now.
  `admin_ai_providers_test.py::test_provider_adapters_says_which_provider_can_mint` no longer asserts
  a policy answer; it reads the projection into a `by_type` map, connects a `fixed_key` provider and a
  `minted` one, reads it again, and asserts `after == by_type`. That equality is a **property of the
  projection** — *no field here may become a function of what is connected* — and it is not
  recoverable from the per-field assertions above it, which is exactly why the test carries the
  reasoning in its docstring and why [provider_adapters_tech](../application/ai_credentials/provider_adapters_tech.md#can_mint_now-is-gone-and-why-it-had-to-go)
  now records it where a maintainer will find it. It is the defect `can_mint_now` had, made
  executable: that field answered `false` for the *first* provider of a type, which is the case §1's
  per-user-keys scenario starts from. The same test asserts the retired key is absent from the payload
  under any name, so it cannot come back silently.
- **[Phase 4 — done]** The three §12 rows that needed a route to exercise them: the delete gate
  (409 carrying `AIProviderDeleteImpact` — named people, not a count — then `force=true` revoking and
  removing rows in §5.4's order), rotation as a 400 on a `minted` provider, and the slot-conflict
  409 rendered as a real envelope with `code`/`conflicting_credential_id`/`conflicting_credential_name`/
  `role`/`mode`, plus the transition scoping surviving the route (renaming a provider already sitting
  in a conflicting configuration does not 409).
- **[Phase 4 — added after review]** A provider-owned managed credential **cannot be deleted on its
  own** (400 naming the provider). The FK is RESTRICT in one direction only, so
  `DELETE /admin/llm-providers/{id}` left the `ai_provider` row standing with its
  `auto_provision_roles` intact — and `auto_provision_targets` drops a provider that owns no
  credential *silently*, with no skip, no log and no event, so every later signup for those roles
  got nothing for ever while the admin surface showed the rule as active. `AIProvidersService.delete`
  reaches the same method through a keyword-only `allow_provider_owned`. —
  `ai_providers_service_test.py::test_a_provider_owned_credential_cannot_be_deleted_out_from_under_it`,
  which also pins the control that the provider's own delete still removes both.
- **[Phase 4 — added after review]** The **forced** provider delete produced that same orphan from
  inside the gate. `ManagedAICredentialsService.delete(force=True)` reports blocked members *and
  deletes the parent anyway*, so `AIProvidersService.delete` refusing on `result.blocked` deleted the
  credential, answered 409, and left the provider. The predicate is now the row — 409 only when the
  credential genuinely survived, where RESTRICT would otherwise trip. That second 409 also gained a
  `code` (`ai_provider_members_not_removed`), because one endpoint answering two 409 shapes, only one
  of which carries `code`, hands a client `undefined` half the time. —
  `minted_ai_credentials_test.py::test_a_member_cannot_be_removed_while_their_mint_is_in_flight`.
- **[Phase 4 — added after review]** `base_url` and `model` are refused on a provider-owned
  credential alongside `api_key`. They are not §3.2 shadowed *policy* columns — so they are added to
  `_refuse_shadowed_writes`' local tuple, not to `SHADOWED_FIELDS`, which the architecture test pins
  field-for-field — but for a `fixed_key` provider they live in the provider's encrypted envelope,
  which `_add_child` builds each new member's credential from. An edit here reached today's members
  and was silently reverted for every member added afterwards, and by the next `rotate_key`. —
  `ai_providers_service_test.py::test_the_key_shape_of_a_provider_owned_credential_is_refused_too`.
- **[Phase 4 — added after review]** `extra="forbid"` reaches the **nested** `config` object via a
  request-only `AIProviderConfigInput`, kept separate from the response shape so a legacy stored
  config with a stray key cannot raise on read. `organization_id` is spelled the American way while
  this codebase's prose spells it the British way throughout, so `organisation_id` is the likeliest
  typo on the surface — and it lands on the pair of fields that select which project keys are minted
  into. Dropped silently, the provider verifies as uncapped and refuses to mint with nothing saying a
  field was thrown away. —
  `admin_ai_providers_test.py::test_a_misspelled_config_field_is_refused_not_dropped`.
- **[Phase 4 — done]** A stray `api_key` on a **provider** create is refused rather than swallowed.
  `AIProviderCreate` has exactly one secret field and for a `minted` provider it *is* the
  administration key, so `api_key` — the spelling every other credential surface uses, and the
  obvious thing to reach for — has no slot at all; ignored, the request answers 200 and the admin
  believes they pasted something that was never stored. `AIProviderCreate` and `AIProviderUpdate`
  therefore forbid unknown keys too, which also covers `secret` on the update (replacing a
  `fixed_key` provider's key is `POST /{id}/rotate-key`, and a silently-ignored `secret` would look
  exactly like the rotation that endpoint exists to perform). —
  `admin_ai_providers_test.py::test_a_stray_member_key_on_a_provider_create_is_refused_not_ignored`,
  with the control that the identical body without the stray key still creates the provider.
- **[Phase 4 — done]** The retired managed-credential create fields are **refused, not ignored**.
  Removing a field from a Pydantic model does not refuse it, it ignores it, so
  `ManagedAICredentialCreate` and `ManagedAICredentialUpdate` set `extra="forbid"`: a client still
  sending `auto_provision_roles`, `provisioning_mode` or `provider_admin_credential_id` gets a 422
  naming the field instead of a 200 that changes nothing. This *strengthens* Phase 1's 400 rather
  than trading it away, and it closes the documented `[]` gap — `auto_provision_roles: []` on a
  provider-owned record used to be accepted, write nothing anywhere, and leave the admin believing
  they had stopped that provider auto-provisioning. —
  `admin_ai_providers_test.py::test_the_retired_managed_credential_fields_are_refused_not_ignored`.
- **[Phase 4 — done]** The declined default slot, end to end through the invite route rather than as
  an internal field: a re-invite of an existing account that already holds a default gets the key and
  reports `default_slot_skips` naming the provider and the mode, with `provisioning_failed` false and
  `skipped` empty. Both negatives are asserted, because the whole risk in this shape is it being read
  as a grant that failed. —
  `users_invitation_lifecycle_test.py::test_a_provider_whose_wiring_a_reinvited_account_already_holds_is_disclosed_not_failed`.
- Verify that no comment or docstring added in this feature makes a behavioural or counting claim
  without a test behind it — the recurring failure mode in this domain.

---

## 13. Documentation Tasks

**[Phase 7 — done]** All four documents written, plus the mirror regenerated. The reference checker
is clean on every file touched.

- **[done]** `docs/application/ai_credentials/admin_ai_credential_provisioning.md` — rewritten around
  the provider/credential split. §1's two configurations are in it as *"The two configurations this
  is built for"*, ahead of the concepts table, because they are what an admin chooses between.
- **[done]** `docs/application/ai_credentials/admin_ai_credential_provisioning_tech.md` — the
  `ai_provider` table, `AIProvidersService`, the policy resolver and the two architecture tests that
  make it enforceable, both conflict rules at both times, the provider delete gate and both of its
  409 shapes, the provider-owned credential's delete refusal, and **both migrations as a chain**
  (`c23d6b59a8f5` then `45938a69aee7`).
- **[done]** `docs/application/ai_credentials/provider_adapters_tech.md` — the vocabulary rule stated
  at the top of the file rather than left implicit, `GET /admin/ai-providers/adapters` under its real
  path, and `can_mint_now`'s removal recorded with the property that replaced it.
- **[done]** `docs/README.md` — glossary entries for **AI Provider** and **Managed AI credential**;
  the **Admin-managed AI credential** entry now says where it sits in the chain. No counter added to
  the Domain Map.
- **[done, and it was not hand-maintenance]** the platform-knowledge env template's knowledge tree —
  the api_reference files are **generated from `frontend/openapi.json`**, and the `application/`
  mirror is a subset copy of `docs/` (every non-`_tech` file), both by
  `.cinna-core-kit/scripts/sync_platform_knowledge.py`, which rmtree's and recreates the target. So
  the fix was: rewrite the `docs/` sources first, then run the sync. That deleted
  `admin_provider_credentials.md` and `admin_provider_adapters.md`, wrote `admin_ai_providers.md`,
  and rebuilt the index. Nothing there is hand-edited.
- **[done]** The staleness of a **running** environment's copy is documented as behaviour, not as a
  caveat: `/app/core` is a per-environment template **copy**, not baked into the image, so an
  environment created before this change keeps its stale reference until it is rebuilt — its agent
  will call the deleted routes and get a 404, *after* the feature looks finished. Stated in both the
  business doc (Known Gaps) and the tech doc (its own section).
- Rewrite the `provider_admin_credential.py` module docstring in place (§4.1). It is the one comment
  in this change that becomes false if left alone. — **Done in Phase 1.** It now states the dual
  meaning, keys it to `kind`, and keeps the isolation argument.

**What Phase 2 added that Phase 7 has to write up** (recorded here so it does not have to be
re-derived from the diff):

- `provisioning_policy.py` — the resolver is the single read path, and the shadowed columns are the
  reason. Belongs in `admin_ai_credential_provisioning_tech.md` alongside the two architecture tests
  that make it enforceable.
- `AIProvidersService` — CRUD, `verify` (two different questions by `kind`), `rotate_key`
  (`fixed_key` only), `apply_to_existing`, and the ordered force-delete including the ordering note
  in §12 above: `delete` is `async` and awaits the revocations, because the thing being deleted is
  the secret they need.
- The **two conflict rules at two different times**, and the two narrowings §12 records: `reconcile`
  claims a held slot (a manual record's control, kept), and the slot skip rides `MemberAddition`
  rather than the reconcile result.
- The **dual-envelope secret**: a `fixed_key` provider's `encrypted_secret` holds a credential
  envelope (`{api_key, base_url, model}`) and a `minted` one holds a bare administration secret.
  This is the concrete shape behind §4.1's prose and is the thing a future reader will get wrong.
- The refusals a user can hit today: `auto_provision_roles` on a managed credential (400, points at
  the provider), a shadowed field on a provider-owned record (400, names the provider), rotation on
  a `minted` provider (400).
- `docs/README.md` glossary: **Provider** and **Managed AI credential** as §13 already asks, plus the
  fact that "provider" no longer means the vendor — `Type` does. §2.1 is the vocabulary table to
  copy from.

**What Phase 3 added that Phase 7 has to write up:**

- The invite wizard's submission is `provider_ids`, not `managed_credential_ids`, and a **manual**
  managed credential is therefore no longer grantable at invite time — the wizard lists key
  *sources*. The tri-state is unchanged and is the thing to state precisely: `null` = the role's
  automatic set, `[]` = the admin unticked everything, a list = exactly those.
- The skip vocabulary lost `managed_credential_not_found` and gained `provider_not_found`, and
  `InviteProvisioningSkip` is keyed by `provider_id`. Both are wire-visible.
- `AccountProvisioningService` no longer queries `ai_provider`: `AIProvidersService.auto_provision_targets`
  (the role predicate) and `AIProvidersService.grant_targets` (an explicit list) return
  provider-and-credential pairs, which is what lets §4.1's isolation hold with the account-creation
  path still auto-provisioning. A provider that owns no credential yields no target and reads back
  as `provider_not_found`.
- The auto-provision security events (`admin.ai_credential.auto_provision` / `…_failed`) now carry
  `provider_id` alongside `managed_credential_id` in `details`.
- The two entry points still share `_provision`, and there is now a test that says so behaviourally
  rather than structurally (§12).
- **Not** added, deliberately: a `ProvisioningReport` field for `ManagedDefaultSlotSkip`. §12's
  Phase 2 note said giving it one belonged to Phase 3; §5.6 does not ask for it, and a field that
  would be empty on virtually every response it appeared in is the same lie §12 rejected when it
  declined to carry the skip on the reconcile result. The two comments that forward-referenced
  Phase 3 for it (`ManagedDefaultSlotSkip`, `tests/utils/ai_provider_admin.grant_members_automatically`)
  now record the decision instead. The review of Phase 3 argued the value is reachable, and it is
  right that it is: not on the automatic path (two providers wiring the same mode for one role are
  refused at write time by §5.5), but through an invite that ticks two providers which wire the same
  mode with `auto_provision_roles = []` — `_claimed_slots` returns empty for those, so no 409 fires,
  the second grant lands and its wiring is silently declined. **Surfacing it is a wire change**
  (`InviteProvisioningSummary` + the generated client + copy), which is Phase 4's and Phase 6's, not
  §5.6's; an internal-only `ProvisioningReport` field would have no reader and no HTTP surface to
  test through. Recorded here so the decision is made with the scenario in hand rather than
  re-derived.

**What Phase 4 added that Phase 7 has to write up:**

- `/admin/ai-providers` is the provider surface and `/admin/provider-admin-credentials` is **gone**,
  together with `admin_provider_credentials.py`, `provider_admin_credentials_service.py` and the four
  `ProviderAdminCredential*` DTOs. `/admin/provider-adapters` moved to `GET /admin/ai-providers/adapters`
  unchanged in shape — an adapter still describes a **type**, which is §2.1's point and the thing most
  likely to be re-confused.
- `/admin/llm-providers` keeps its path and tag, and its create now makes **manual records only**. The
  three fields it lost are `auto_provision_roles`, `provisioning_mode` and `provider_admin_credential_id`,
  and the models forbid unknown keys so that losing them is a refusal rather than a silent accept.
  `ManagedAICredentialPublic` renamed `provider_admin_credential_id` → `provider_id` and gained
  `provider_name` and `is_provider_owned`; `provisioning_mode` and `has_api_key` stay computed.
- **A null reconcile result on `PATCH /admin/ai-providers/{id}` is not an error.** It means the
  provider owns no managed credential — a *lone* row, only ever creatable by the deleted route — so
  there is nothing to re-apply to and nobody to tell. The response is the provider projection either
  way and the per-member events are simply not written. Refusing instead would make a legacy row
  un-renameable.
- The `(role, mode)` conflict envelope moved routers verbatim, and `admin_ai_providers._conflict_409`
  keeps the `conflicting_credential_*` key names because one dialog parses them.
  **Correction (Phase 7): `admin_llm_providers.py` kept no dead handlers.** The rule and its envelope
  left that module entirely — it has no `_conflict_409`, no `auto_provision_conflict` mapping and no
  `ManagedCredentialConflictError` import; what remains is a docstring paragraph pointing at the new
  home. This line's earlier wording was carried into the docs pass and corrected there too.
- The **declined default slot** is on the wire — see the rewritten §5.7. Phase 6 has **two** mandatory
  copy items, not one: `provider_not_found` in the skip-reason map, and this disclosure rendered as a
  note beside a successful grant rather than as a failure.
- `key_provisioning_service` reads the minting secret through `AIProvidersService.decrypt_secret` now.
  **Correction (Phase 7): `AIProvidersService.connected_types` does not exist.** This line described
  the adapters route's read of "which types have a provider", which was the second term of
  `can_mint_now`; both the field and the helper were removed, and `list_provider_adapters` reads the
  registry alone. `grep -rn connected_types backend/app` finds nothing. The isolation point the line
  was making still holds and is made by the isolation test, which now has no exception list.
- The isolation architecture test has **no exception list at all** any more. Its `PENDING` table and
  its `xfail(strict=True)` twin are both deleted, which is what they were built to make happen.

---

## UI Specification

_Produced by `cinna-core.ui.design` on 2026-09-07 against `docs/development/frontend/ui_ux_guidelines.md`.
Composition decisions here **override** §6's frontend section; data and API decisions stay with the plan.
Every placement cites a §2 row of the guidelines; every pattern cites a §3 pattern._

**Read this first — the vocabulary rule this specification is built to protect (§2.1).** After this
change **Provider** is the entity that holds a key and the rule for handing it out, and **Type** is
the vendor. Three pieces of live copy break that rule today and are named as required edits below:
the managed-credentials table header `Provider` (the vendor column) and `Default provider` (which is
`set_as_default`, not a vendor at all), and `describeAutoProvisionConflict`'s sentence, which says
"credential" where it must now say "provider". The wire keys are untouched — `conflicting_credential_id`
and `conflicting_credential_name` keep their names by Phase 4's decision; only what a person reads changes.

### Surfaces

| # | Surface | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---------|------|-------|--------------------|---------|--------|--------------|
| S1 | `/admin/ai-credentials` hash navigation | route `routes/_layout/admin/ai-credentials.tsx` | Manage-list (host) | Complex configuration → tabs | **P8** (`HashTabs`) | 2 tabs; 1 primary button + 1 filter button in the page header | checklist |
| S2 | Providers tab — the provider list | `/admin/ai-credentials#providers` | Manage-list | Manage list → route; "a table that needs its columns also needs search or pagination" | **P3** rows at tab width (plain P3 row shape) | ≤ 1 badge, 2-fact meta, ≤ 3 flags, **1 inline action** (Verify), menu 3–4 items | **checklist + screenshots** |
| S3 | Connect provider (create) | Providers tab › header button | Create | Create > 3 fields **and** shape depends on type → wizard, type is step 1 | **P6** (3 steps), step 1 uses **P4**'s pill picker | 3 steps; step 1: 2 blocks, step 2: ≤ 5, step 3: 5 blocks (2 behind Advanced) | **checklist + screenshots** |
| S4 | Edit provider | Providers tab › row `⋯` | Edit | Edit with 2–3 sections → `Sheet` (§5) | Sheet, sections per **P7**'s section rule | 3 sections, 14 fields, explicit Save/Cancel | **checklist + screenshots** |
| S5 | Replace key | Providers tab › row `⋯` | Edit (one field) | Edit, one section ≤ 8 fields → `Dialog` | **P2** dialog half | 1 field, 1 primary button | checklist |
| S6 | Delete provider — impact confirm | Providers tab › row `⋯` | Manage-list (destructive) | Confirmation; impact data is fetched | `AlertDialog`, escalate-in-place | named members in `max-h-[40vh]`, 1 destructive button | checklist |
| S7 | Managed credential dialog — provider-owned branch | Managed credentials tab › row `⋯` › Edit | Edit | Edit, one section → `Dialog` | **P2**-style form dialog | provider-owned: 4 blocks; manual: 8 blocks | checklist |
| S8 | Managed credentials table — Source column | `/admin/ai-credentials#managed-credentials` | Manage-list | Manage list → route with `DataTable` | **P9** | 9 columns (unchanged count), row menu 2–3 items | checklist |
| S9 | Invite wizard — Provisioning step | `Admin/InviteUserDialog.tsx` step 2 | Create | Wizard step; selection checklist, not a list in a card | **P6** step (existing) | 1 checklist, `max-h-[40vh]`, 2 muted facts per item | checklist |
| S10 | Invite success panel — skips and declined default slots | `Admin/InviteSuccessPanel.tsx` | Create (result) | Result feedback that still needs the admin | **P6** success panel (existing) | 2 new copy families, both `text-xs text-muted-foreground` | checklist |
| S11 | Company AI providers card (Access tab) | `/admin/server-configuration#access` grid | Manage-list preview | Card, half-width; list in a card capped at 5 | **P5** + the segmented-multi-toggle row | 3 blocks, 5 rows, 1 control per row | checklist |

S11 is **not in §6**. It is in this specification because Phase 4 broke it: see "Where §6's inventory
was incomplete" below.

---

### S1 — `/admin/ai-credentials` hash navigation

- **Intent:** the admin wants to link to, and come back to, a specific tab of this page.
- **Layout:** replace the local `useState<"managed" | "provider-keys">` (`ai-credentials.tsx:159`)
  with `Common/HashTabs.tsx`, as `routes/_layout/settings.tsx` and
  `routes/_layout/admin/server-configuration.tsx` use it. Tab values: `managed-credentials`
  (default, `tabs[0]`) and `providers`. Tab titles: **"Managed credentials"** and **"Providers"**
  (the old "Provider keys" title dies with the old tab).
- **The §6.1 constraint, made concrete.** The page latches `?newCredentialFor=` and then strips it
  with `navigate({ to: "/admin/ai-credentials", search: {}, replace: true })`
  (`ai-credentials.tsx:126-130`). That call states no `hash`, so it drops the one `HashTabs` wrote.
  The strip must carry the current hash forward — read it at the moment of the navigate
  (`window.location.hash.slice(1) || undefined`) and pass it as `hash`. Both directions must work:
  arriving at `#providers` and then dismissing a hand-off must leave the admin on `#providers`, and
  arriving with `?newCredentialFor=` and no hash must not invent one.
- **New primitive:** `HashTabs` gains an optional `onTabChange?: (value: string) => void`, fired
  from a `useEffect` on `activeTab` so it also reports the initial tab on a deep link and reports
  hash-change navigations. Extracted for this route, which needs the active tab in the **page
  header**: the header carries the tab's own primary button (`ManagedCredentialDialog mode="create"`
  vs the S3 wizard trigger) and the Filter button, which belongs to the managed tab only. Without
  the callback the route cannot keep the convention every other admin route follows, and the
  alternative — a toolbar row inside each tab — would put the create button in a different place
  here than on `/admin/users`.
- **States:** unchanged; each tab owns its own.
- **Anti-patterns:** none touched.
- **Screenshots:** none. The tab strip keeps its position, count and host; only the state store and
  one label change. §9 "a new route or tab is added" does not fire — `Providers` occupies the slot
  `Provider keys` had.

---

### S2 — Providers tab (the provider list)

- **Intent:** the admin wants to see where keys come from, who automatically gets one, and whether
  the connection still works.
- **Placement:** the tab body, not a card. §2 "Card width": *"A table that genuinely needs its
  columns also needs search or pagination, which makes it a Manage-list route, not a card."* The
  provider list needs neither — there are tens of providers at most, and the plan's own index
  decision (§3.1) says the scan reads every row. So it is neither a card (R4 would cap it at 5 with
  a "Show all" it has nowhere to send) nor a `DataTable` (a 7-column grid of Yes/No badges and role
  chips is A10's heavy row rebuilt as a table).
- **Layout:** one sentence of `text-sm text-muted-foreground max-w-3xl` above the list — *"Where AI
  keys come from, and who automatically gets one. A provider holds the key and the rule for handing
  it out; the vendor it talks to is its **type**."* — then a `div className="rounded-md border p-2"`
  holding a `ListRowGroup` of `ListRow`s. The border is the sibling tab's wrapper
  (`LlmProvidersTable.tsx:76`), reused so the two tabs frame their content identically. It is **not**
  a `Card`: it has no header, so R15 does not apply to it; the `TabsTrigger` label is its title.
  Skeleton by reference: `Common/PreviewList.tsx` for the four states, `Agents/ScheduleRow.tsx` for
  the row.
- **Siblings:** the sibling tab (Managed credentials) is a `<Table>` with client-side pagination and
  a user filter. This surface deliberately differs, by the §2 "Card width" row quoted above: the
  credentials table earns its columns through pagination and a filter, and the provider list has
  neither. Both tabs share the same outer `rounded-md border` frame and the same page header.
- **Row shape:** the **plain P3 row**. Left to right:
  - `status` dot — the health question. `on` when `last_verified_at` is set and `last_verify_error`
    is null, label "Verified {date}"; `error` when `last_verify_error` is set, label "Last check
    failed"; `off` when never verified, label "Never verified".
  - `icon` — one neutral tile, `KeyRound` for `kind === "fixed_key"` and `UsersRound` for
    `kind === "minted"`, in the `h-6 w-6` tinted span P3's skeleton shows. **No brand marks** — none
    are shipped, which is the case P3 names.
  - `title` — `provider.name`.
  - `badges` — exactly one, `h-5`, carrying the **kind**: "Fixed key" / "Per-user keys". Kind is the
    word the admin scans this list by (it is the feature's headline distinction), which is what §2
    "Row height" reserves the title-line `Badge` for. The 1024 yardstick that forbids badges applies
    to a ~276 px half-width card row; this row is full tab width.
  - `meta` — two facts joined by ` · `: the **type label**, then the rule.
    Type label comes from `useProviderAdapters().adapterFor(type)?.label`, never from the hardcoded
    `PROVIDER_TYPE_OPTIONS` (§6.3: the adapters endpoint is the authority, and the hardcoded map
    omits `minimax`). The rule is `"Automatic for Agent User, Agent Developer"` from
    `auto_provision_roles` via `userRoleLabel`, or `"Not automatic"` when the list is empty. These
    two facts are what tell two providers apart and are §6.2's first two questions; the metadata
    line earns its height here.
  - `flags`, in order:
    1. **The wiring flag, always present when `auto_provision_roles` is non-empty** — one flag, two
       faces, and this is where §9's headline configuration becomes visible:
       `set_as_default === true` → `Star`, tone neutral, label *"Becomes each recipient's default
       key"* (append *", and wires their SDK defaults for Conversation, Building"* when
       `set_user_sdk_defaults`);
       `set_as_default === false` → `Plus`, tone neutral, label *"Extra key — granted in addition to
       what they already have, and displaces nobody's default."*
    2. `TriangleAlert`, tone `error`, only when `key_state_summary.failed > 0` — *"{n} members' keys
       failed to create. Open the credential to retry."*
    3. `RowInfo`, last, carrying: `"{member_count} members"`; the key-state summary rendered through
       the existing `membershipStatusMeta` admin labels (`"3 key created · 1 creating key"`);
       `config.organization_id` and `config.project_id` when set (so two providers into one
       organisation are visible rather than silent, per §6.2); `"Last verified {datetime}"` or
       `"Never verified"`; and `last_verify_error` verbatim when present.
  - **Inline actions: 1.** `Verify` — a `h-7 w-7` ghost icon button (`BadgeCheck`, `h-3.5 w-3.5`) — the glyph the existing Verify action already uses
    with a `Tooltip`. It is the reason an admin opens this tab in the third of §6.2's three cases,
    and the codebase already treats Verify as a row action rather than a dialog control
    (`ProviderAdminCredentialsTable.tsx:213-242`). No `OnOffToggle` — a provider has no enabled flag.
    Per-row pending freezes this button and the `⋯` trigger for that row only, tracked as a
    `Set<id>` and not off `mutation.variables` (§3 P3 States, and the reason is written out in
    `CompanyAiCredentialsCard.tsx:104-119`).
  - **Menu (`RowActionsMenu label={\`the provider ${name}\`}`):** `Edit provider` (S4) ·
    `Apply to existing users` · `Replace key` (**rendered only when `kind === "fixed_key"`**;
    omitted, not disabled, for minted — a disabled item cannot carry the explanation, and the
    explanation lives in S4's Key section instead) · `DropdownMenuSeparator` ·
    `Delete provider` (`variant="destructive"`, last, opens S6). 4 items for a fixed-key provider,
    3 for a minted one.
- **Verify result — copy, and the §9 spend-limit rule.** The result is a **toast**, never inline
  state (§6 "Result feedback that stays: never"); the row's dot and `RowInfo` carry the durable
  answer after the query invalidates. Reuse the proven wording at
  `ProviderAdminCredentialsTable.tsx:66-76`:
  - `ok && checked_spend_limit && spend_limit_enforcing` → success, *"The connection works and the
    project has a monthly spend limit."*
  - `ok && checked_spend_limit && !spend_limit_enforcing` → **error** toast (it is a blocker, not a
    success): *"The key works, but the project has no monthly spend limit. Set one in the
    {Type} console — keys are not created in an uncapped project."*
  - `ok && !checked_spend_limit` (every `fixed_key` provider) → success, *"The key works."* No
    spend-limit sentence at all: `checked_spend_limit === false` means the question does not apply,
    and rendering it as "no limit" would be the false negative the field exists to prevent.
  - `!ok` → error toast carrying `error`.
  **`spend_limit_cents` is never rendered anywhere.** §9 says Cinna never displays a limit; the
  number is a live read of a value the provider's console owns, and a number on screen in Cinna
  reads as a Cinna setting whether or not it was stored. The surface says *whether* a cap is
  enforcing and where to change it. Nothing in this feature writes a limit.
- **Interaction model:** form/dialog throughout — every mutation on this tab is a menu item opening
  a dialog or sheet, or the one inline Verify. No auto-saving control on the row (§2 "Interaction
  model").
- **States:**
  - loading — 3 × `Skeleton className="h-[48px] w-full rounded-md"`;
  - error — `Common/QueryErrorAlert.tsx` with `errorFallback="Couldn't load the AI providers."` and
    a Retry, gated on `data === undefined` so a failed **background** refetch does not blank a list
    that is on screen (the bug recorded in `project_ui_build_onboarding_admin`);
  - empty — *"No key sources yet. Connect a provider to hand every new account a working key on day
    one."* plus the header's `Connect provider` button, which satisfies §2's "empty state names the
    primary action";
  - pending — per row, as above.
- **Polling (§6.7).** The providers query refetches every 10 s while any provider's
  `key_state_summary` holds an in-flight status, and stops when it settles — the same rule
  `hasKeyInFlight` already applies to the managed list. The predicate is the second consumer of one
  idea, so it is **extracted**, not copied: add `hasProviderKeyInFlight(provider: AIProviderPublic)`
  beside `hasKeyInFlight` in `Admin/LlmProviders/MemberKeyStatus.tsx`, both reading
  `membershipStatusMeta(...).inFlight` from `utils/keyProvisioning.ts`. It must be live after
  `apply-to-existing` on a minted provider, which is exactly §6.7's requirement.
- **Query keys:** `["admin", "ai-providers"]` for the list; `PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY`
  is deleted with the tab it served. Every provider mutation invalidates both that key and
  `MANAGED_CREDENTIALS_QUERY_PREFIX` (§6.7).
- **Components:** `ListRowGroup` / `ListRow` / `RowFlag` / `RowInfo` / `RowActionsMenu`,
  `QueryErrorAlert`, `Skeleton`, `Badge`, `Tooltip`, `useProviderAdapters`.
  New: `Admin/AiProviders/ProviderRow.tsx` (the row, shared by nothing else yet — it has no Sheet
  twin because the list is not capped), `Admin/AiProviders/ProvidersTab.tsx`.
- **Anti-patterns:** replaces `ProviderAdminCredentials/ProviderAdminCredentialsTable.tsx`, an A10
  instance (a `<Table>` whose row cluster is three visible controls and whose state is two badge
  columns). Both files in `ProviderAdminCredentials/` are deleted, not migrated. A10's open list
  loses one entry.
- **Screenshots — required.** §9 "a pattern is used in a new host": this is the first P3 list
  rendered at full tab width rather than inside a card, and the composition question it raises —
  whether a one-line row with a badge, a two-fact meta line and three flags still reads at 1440
  where the name has 700 px of room and the flags cluster is pinned right — cannot be settled from
  the JSX. **2 captures, both of the host:**
  `node frontend/scripts/ui-shot.mjs --url "/admin/ai-credentials#providers" --out providers-tab --width 1440,1024`.
  If the dev database holds no providers, capture the empty state and say so; do not create one.

---

### S3 — Connect provider (create wizard)

- **Intent:** the admin wants to say where keys come from and who gets one.
- **Story split.** §6.3 asks for one "create/edit dialog". This specification **splits it**, because
  §1 requires it: "A surface that serves two stories at once is two surfaces", and the two stories
  do not share fields. Create decides `kind`, `type` and the secret — none of which
  `AIProviderUpdate` accepts (they are absent from the model on purpose, and `secret` is absent
  because replacing a key is `POST /{id}/rotate-key`). Edit carries fourteen fields Create does not
  need to ask for. §1: "When they differ, do not force the create story through the edit form."
- **Placement:** a `Dialog sm:max-w-lg`, opened from the tab's page-header button
  `Connect provider` (`Plus` icon) — the same trigger shape
  `ProviderAdminCredentialDialog.tsx:372-379` uses today.
- **Pattern: P6, three steps.** Stepper header verbatim in the reference's markup
  (`InviteUserDialog.tsx:311-329` — one `<p className="text-xs text-muted-foreground">`, active span
  `font-medium text-foreground`, literal `" · "` separator): **`1 Source · 2 Key · 3 Who gets one`**.
  One `<form>` with `hidden` `<fieldset>` panes, per the reference — it keeps typed values and
  per-step Enter semantics, and P6 names that variant as conformant. Footer: `Cancel`/`Next` on
  step 1–2, `Back`/`Connect provider` on step 3.
- **Step 1 — Source (2 blocks).**
  1. **Type** — the P4 pill picker, not a `<Select>`: §1 Create, *"If the form's shape depends on a
     type, choosing the type is step 1 (tile/pill picker) — never a `<Select>` at the top of a long
     form"*, and the shape does depend on it (`requires_base_url`, `requires_model`,
     `admin_config_schema`, `supports_minting`). Pills are
     `inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium`
     (`Credentials/AddCredential.tsx`), one per adapter from `GET /admin/ai-providers/adapters`,
     labelled `adapter.label`. No search input — the list is five items, and P4's search is for the
     credential catalogue's dozens.
  2. **Kind** — two tiles, radio semantics, each with its label and a `<p className="text-xs
     text-muted-foreground">` carrying §9's sentence verbatim:
     - **Fixed key** — *"Everyone shares one key you paste."*
     - **Per-user keys** — *"Each person gets their own, created in your provider account. This is
       what makes per-user spend tracking possible."* The second sentence is not decoration: §9 says
       per-user spend tracking is *why* an admin picks this, so the surface says so.
     When the chosen type's adapter has `supports_minting === false`, the **Per-user keys** tile is
     unavailable **and the reason is written next to it in text**, not left as a grey tile:
     *"{Type} cannot create keys through its API, so a {Type} provider shares one pasted key. That
     is a normal setup, not a limitation of this server."* The sentence is composed from the
     adapter's own `label` and `supports_minting`, so a sixth adapter needs no copy change.
     This is the §6.4 anti-pattern risk applied preventively at its other instance.
  - **`can_mint_now` is deliberately not the gate here.** See "Contract gaps" below; the wizard
    gates on `supports_minting`.
- **Step 2 — Key (≤ 5 blocks).** All fields are rendered generically from the chosen adapter, never
  from a client-side type switch:
  1. The secret — `Input type="password" autoComplete="off"`, required. Label and helper text key
     off `kind`: **"API key"** / *"The model API key every member will hold a copy of."* for
     `fixed_key`; **"Administration key"** / *"The organisation key Cinna uses to create each
     person's key. It is never handed to a member and never shown again after saving."* for `minted`.
  2. `Base URL` when `adapter.requires_base_url`; `Model` when `adapter.requires_model`. Required
     markers follow the adapter, not a hardcoded type list.
  3. For `minted`: one `Input` per entry of `adminConfigFields(adapter)` — the generic
     `admin_config_schema` renderer that already exists in `useProviderAdapters.ts:88` and is used
     verbatim at `ProviderAdminCredentialDialog.tsx:315-336`. `organization_id` / `project_id` are
     never named in the client; they arrive as schema fields. (Phase 4 made a misspelled config key
     a 422 rather than a silent drop, so a field this renderer gets wrong now fails loudly.)
  4. For `minted`, a `text-xs text-muted-foreground` sentence under the config fields:
     *"Set a monthly spend limit on this project in your {Type} console. Cinna checks that a limit
     is in force and refuses to create keys in an uncapped project. It never sets the limit and
     never stores a copy of it."* — §9's rule stated where the admin can act on it. **No spend-limit
     input, anywhere.**
- **Step 3 — Who gets one (5 blocks, 2 of them behind one disclosure).**
  1. `Auto-provision for new accounts` — a wrapping `Checkbox` row over `USER_ROLE_OPTIONS`, the
     shape `ManagedCredentialDialog.tsx:1565-1587` uses today. Helper: *"Applied when an account is
     created. Changing someone's role later never grants or revokes a key — use **Apply to existing
     users** for accounts that already exist."*
  2. `Make it their default key` — `Switch`. Helper carries §9's headline configuration in full:
     *"Off means an extra key: everyone matching the roles above still receives it, it appears in
     their settings and in Desktop and is selectable per agent, and it displaces nobody's existing
     default."*
  3. `Wire it into their SDK defaults` — `Switch`, gating block 4.
  4. Modes block, rendered only while 3 is on: two `Checkbox`es (`Conversation`, `Building` from
     `SDK_MODE_OPTIONS`) and, **per ticked mode**, one `ModelOverrideField` with its
     `ListModelsButton` — the existing component at `ManagedCredentialDialog.tsx:384-445`, which is a
     `Popover`, not a `Dialog`, and therefore does not trip R8. Keeping the override with the mode
     it belongs to is why the overrides are here and not under Advanced.
  5. `Advanced` — one disclosure (§1 Create: "advanced options collapsed behind one 'Advanced'
     disclosure"), holding `Default model` (with its "View available models" link) and
     `Available models` (`Textarea rows={3}`, parsed by the existing `parseAvailableModels`).
     `expiry_notification_date` is **not** offered at create time — it describes a key an admin has
     just pasted and has no sensible value yet; it lives in S4.
- **Interaction model:** form, one `Connect provider` submit. The whole wizard is disabled while
  submitting (P6 States).
- **Errors:**
  - Per-field `FormMessage` from zod, per step; `Next` validates only its own step's fields.
  - The slot-conflict **409 renders as copy naming the other provider**, never a raw error
    (§6.3's contract gap). Reuse `parseAutoProvisionConflict` unchanged — it is a pure envelope
    parser and Phase 4 kept the key names deliberately — and **rewrite**
    `describeAutoProvisionConflict` (`providerTypes.ts:160-169`), which currently reads *"…already
    sets the {mode} default for auto-provisioned {Role} accounts. Only one **credential** can own a
    role's default for a mode…"*. New sentence: *"**"{name}"** already sets the {Mode} default for
    auto-provisioned {Role} accounts. Only one provider can own a role's default for a mode — drop
    the role, or turn off that mode, on one of the two."* It renders as an
    `Alert variant="destructive"` inside step 3, under the roles block, where the offending control
    is — the placement `ManagedCredentialDialog.tsx:1588-1595` already uses. Both call sites of the
    helper (this one and S11) get the new sentence, which is the point of it being one function.
  - Everything else: `handleError` → error toast.
- **States:** adapters loading → the type pills render as `Skeleton` pills and `Next` is disabled
  (`useProviderAdapters` already answers `false` to both minting predicates while loading — "loading
  is not a yes"); adapters error → an inline `text-sm text-destructive` banner and a disabled `Next`,
  as `ProviderAdminCredentialDialog.tsx:338-343` does; empty adapters is unreachable (the registry
  always has at least one).
- **Components:** `Dialog`, `Checkbox`, `Switch`, `Input`, `Textarea`, `Alert`, `LoadingButton`,
  `ListModelsButton`, `useProviderAdapters`, `adminConfigFields`. New:
  `Admin/AiProviders/ConnectProviderDialog.tsx`.
- **Anti-patterns:** A7 (type `<Select>` at the top of a shape-dependent form) is what step 1 exists
  to avoid; the superseded `ManagedCredentialDialog` had exactly that shape for `type` and
  `provisioning_mode` and it is not carried over.
- **Screenshots — required.** §9: "a stepper with > 2 steps". **2 captures:**
  `--url "/admin/ai-credentials#providers" --click "text=Connect provider" --element "[role=dialog]" --out connect-provider-step1 --width 1440,1024`.
  Step 3 is the denser pane and the one carrying the "extra key" copy; it is reachable only after a
  name, a type, a kind and a secret are typed. If `ui-shot.mjs` cannot type them, report **"required
  but not capturable"** for step 3 and review that pane from the JSX. Do not create a provider to
  reach it.

---

### S4 — Edit provider (Sheet)

- **Intent:** the admin wants to change the rule, the wiring, or the shape of an existing key source
  — and to be told, before saving, that the change reaches people who already hold the key.
- **Placement:** a right `Sheet`, `className="w-full sm:max-w-lg"` (the `AllHandoversSheet.tsx:46`
  shape). §5: *"`Sheet` | Edit with 2–3 sections"*. Fourteen editable fields is four times a form
  dialog's ≤ 8, and §1 Edit puts "the full form, grouped into titled sections" in a Sheet at this
  size. Sections are `<section>` blocks with an `h3.text-sm.font-medium` and a one-line description,
  separated by `Separator` — P7's section skeleton, applied inside a Sheet.
- **Sections (3):**
  1. **Details** — `Name` (`Input`). Below it, a **text** line, not a disabled control:
     *"{Type} · {Kind label}. A provider's type and kind are fixed when it is connected."* Stating
     an immutable fact as prose costs nothing and avoids the §6.4 anti-pattern at its third instance.
  2. **Key** — for a `fixed_key` provider: `Base URL` and `Model` where the adapter requires them,
     and a muted line *"Replace the key itself from this provider's ⋯ menu — **Replace key**. It
     re-keys every member."* For a `minted` provider: the `adminConfigFields(adapter)` inputs
     (organisation, project) and the §9 spend-limit sentence from S3 step 2, plus *"Per-user keys are
     created and revoked one at a time, so there is no single key here to replace."* — which is where
     the omitted `Replace key` menu item is explained (§8: rotation on a minted provider is a 400,
     and *"silently accepting it is how an admin comes to believe they rolled a key they did not"*).
     **The Sheet has no Replace-key trigger of its own.** A `Dialog` opened from a `Sheet` is a third
     disclosure level and fails R8; the row → Dialog path is level 2. The codebase already splits
     this way — `ProviderAdminCredentialsTable` keeps Verify in the row menu and out of the dialog.
  3. **Provisioning policy** — the same five blocks as S3 step 3, in the same order, with the same
     copy, seeded from the record. Plus `expiry_notification_date` inside the Advanced disclosure.
     At the head of the section, an `Alert` (default variant, not destructive — it is a consequence,
     not a warning): **"Changes here re-apply to everyone who already holds this key."** with
     *"Model and override changes are written through to every member; clearing an override unpins
     the members it pinned."* This is §6.3's stated contract gap and it is **mandatory copy**: §5.3
     is the behaviour and this sentence is its only reader.
- **Interaction model: form.** Explicit `Cancel` / `Save changes` in `SheetFooter`, dirty-tracked.
  Not auto-save: the two conditions the Channels Sheet auto-saves under do not hold here — null does
  not mean "inherit" for any of these fields, and every write re-applies policy to every member, so
  a per-keystroke save would re-run §5.3 repeatedly against live accounts. Submit is a **diff PATCH**
  against a snapshot taken on open, the `openedWithRef` idiom at
  `ManagedCredentialDialog.tsx:541-543` / `:985-1044`, so an untouched field is omitted and the
  three-state `model_override_*` contract (omitted = leave, `""` = clear and unpin, a value = set)
  survives intact.
- **States:** loading — the Sheet opens from a row whose record is already in hand, so no load;
  saving — footer buttons disabled, `LoadingButton`; error — the 409 conflict `Alert` inside the
  Provisioning policy section (same helper, same rewritten sentence as S3); other errors → toast.
- **Components:** `Sheet`, `Separator`, `Switch`, `Checkbox`, `Input`, `Textarea`, `Alert`,
  `LoadingButton`, `ListModelsButton`, `ModelOverrideField`. New:
  `Admin/AiProviders/EditProviderSheet.tsx`.
- **Screenshots — required.** §9: "expand/collapse", and a three-section Sheet is a composition this
  codebase has no instance of (the Channels Configure Sheet has two and no disclosure). **1 capture**,
  at the width where a Sheet is tightest:
  `--url "/admin/ai-credentials#providers" --click "[aria-label^='Actions for the provider']" --click "text=Edit provider" --element "[role=dialog]" --out edit-provider --width 1024`.
  If the dev database holds no provider, report "required but not capturable" and review from the JSX.

---

### S5 — Replace key (rotate)

- **Intent:** the admin has a new key from the vendor and wants every member to hold it.
- **Placement:** `Dialog sm:max-w-md` from the row's `⋯` (level 2). One field → §5's dialog row.
- **Layout:** title `Replace the key on "{name}"?`; description *"Every member's copy is replaced
  immediately. {n} people hold this key. The old key is not disabled at {Type} — remove it there if
  you no longer want it live."* One `Input type="password" autoComplete="off"` labelled `New API key`.
  Footer: `Cancel` + `Replace key` (primary, `LoadingButton`). The dialog **is** the confirmation —
  §2 Confirmation's requirement is that the surface names the entity and ends in one button, and an
  `AlertDialog` cannot hold the input.
- **Rendered only for `kind === "fixed_key"`** — the menu item does not exist for minted providers
  (S2), so the 400 at §8 is unreachable from the UI rather than caught after the fact.
- **States:** pending on the submit button; error → toast; success → toast *"Key replaced for {n}
  members."* and invalidate both query keys (§6.7).
- **Components:** `Dialog`, `Input`, `LoadingButton`. New:
  `Admin/AiProviders/ReplaceProviderKeyDialog.tsx`.
- **Screenshots:** none — a one-field form dialog is §9's first "not required" case.

---

### S6 — Delete provider (impact confirm)

- **Intent:** the admin wants this key source gone and needs to know who it costs before pressing.
- **Placement:** `AlertDialog` from the row's `⋯`. §2 Confirmation allows a plain `Dialog` when the
  confirm must show fetched impact data, but both existing instances of exactly this shape in this
  area use `AlertDialog` and escalate in place (`LlmProviderActionsMenu.tsx:436-501` and
  `ProviderAdminCredentialsTable.tsx:251-303`); matching them is worth more than exercising the
  exception.
- **Flow.** Selecting `Delete provider` fires `DELETE /{id}` **without** `force` and opens the
  confirm. A 200 (nobody holds a key — §5.4 step 3) closes it with a toast; there was nothing to
  confirm. A 409 fills the confirm with the `AIProviderDeleteImpact` body. The unforced delete is
  the probe, and it is safe by construction — this is the escalate-in-place flow
  `ProviderAdminCredentialActions` already uses.
- **Copy — §9's two requirements, both mandatory:**
  - Title: `Delete provider "{provider_name}"?`
  - **Who loses a key, by name.** *"{member_count} people lose the key this provider gave them:"*
    followed by a `<ul>` of every `AIProviderDeleteMember` — `full_name` when present, otherwise
    `email` — inside `max-h-[40vh] overflow-y-auto`, styled as the blocked-member list at
    `LlmProviderActionsMenu.tsx:468-480`. Named people, not a count, is the whole reason Phase 4
    shaped the body this way.
  - **Whether keys are revoked at the provider**, keyed off `minted_key_count`:
    `> 0` → *"{minted_key_count} of those keys were created at {Type} and will be **revoked there**.
    They stop working immediately."*
    `=== 0` → *"Their copies of the key are deleted here. The key itself stays valid at {Type} —
    remove it in your {Type} console if you no longer want it live."*
    This inverts the current `ProviderAdminCredentialsTable.tsx:251-303` copy, which promises that
    live provider keys survive. Under §5.4 a forced delete revokes them first. Carrying that sentence
    over unchanged would be a false statement about a destructive action.
  - Action: `Delete provider and revoke {n} keys` when `minted_key_count > 0`, otherwise
    `Delete provider`; `bg-destructive`, sends `force: true`.
  - **Second 409** (`code === "ai_provider_members_not_removed"`): the dialog stays open, appends the
    server's own message in `text-destructive`, and relabels the action `Try again` — §12 records
    that the retry completes the removal, so the copy must offer the retry rather than read as a
    dead end.
- **States:** the impact request is pending → *"Working out what this would cost…"*; it failed →
  the server's message and a `Retry`; pending on the destructive button while forcing.
- **Components:** `AlertDialog`. New: `Admin/AiProviders/DeleteProviderDialog.tsx`.
- **Screenshots:** none. The member list is data-length dependent, but §9's trigger needs the dev
  database to actually hold more than five members on one provider; it does not, and the
  `max-h-[40vh]` is judged from the JSX. Do not create members to reach it.

---

### S7 — Managed credential dialog (conditionally read-only)

- **Intent:** unchanged for manual records; for a provider-owned record the admin wants to see what
  the provider decided and manage **who holds it**.
- **Placement:** the existing `Dialog sm:max-w-lg max-h-[90vh] overflow-y-auto`.
- **The manual branch loses three sections outright**, because Phase 4 made them unsendable
  (`ManagedAICredentialCreate` / `Update` set `extra="forbid"`, so a client still sending these gets
  a 422 naming the field, and the generated types no longer declare them):
  - the **Key source** card and its `provisioning_mode` `RadioGroup`
    (`ManagedCredentialDialog.tsx:1117-1177`) — a manual record is always `shared`;
  - the **Provider organisation** select (`:1181-1215`) and the minted explainer panel (`:1219-1243`)
    — a manual record can never point at a provider;
  - the **Auto-provision** card (`:1555-1596`) — the rule lives on the provider now. In its place,
    one `text-xs text-muted-foreground` line: *"Who automatically receives a key is set on a
    provider, under **Providers**."* linking to `/admin/ai-credentials#providers`. Deleting the
    control without saying where it went is how an admin concludes the feature was removed.
  The manual branch is then **8 blocks**: Name · Type · API key · Base URL/Model · Default and
  available models · Target Users · Set as default · Set user SDK defaults (+ modes). At the §2 cap,
  and one block lighter than today.
- **The provider-owned branch is a different composition, not the same form with `disabled` props.**
  §6.4 asks for read-only controls with an explanation; this specification goes further and
  **replaces the controls with text**, which removes the anti-pattern instead of annotating it and
  satisfies §9's accessibility requirement structurally — there is no disabled styling to be the
  only carrier of meaning. **4 blocks:**
  1. An `Alert` (default variant): title **"Managed by the provider {provider_name}"**, body *"The
     key, the models and the SDK wiring are set on the provider. Change them there. Deleting this
     credential means deleting its provider."* — the last sentence because Phase 4 made a
     provider-owned credential undeletable on its own (400 naming the provider), and an admin who
     cannot find Delete needs to be told where it is. Plus a link **"Open {provider_name}"** →
     `/admin/ai-credentials#providers`. §5 puts an `Alert` exactly here: *"inline warning that changes
     what the user can do on this surface (locked control, missing prerequisite)"*.
  2. **What the provider decided** — a plain definition list, `text-xs text-muted-foreground` labels
     over `text-sm` values, no inputs: `Type`, `Key source` ("Fixed key" / "Per-user keys", from
     `provisioning_mode`), `Default model`, `SDK defaults` (the wired modes, or "Not wired"),
     `Model overrides` (per mode, or "None"). Values come from `ManagedAICredentialPublic`, which
     computes them through the policy resolver, so this block cannot disagree with the provider.
  3. **Who holds it** — an `h3.text-sm.font-medium` heading over the existing `UserAllowlistPicker`.
     The one editable thing, and the reason the dialog is still a form.
  4. Footer: `Cancel` / `Save`, submitting **only** `target_user_ids`.
  Title for this branch: the record's name; description: *"Managed by {provider_name}."*
- **Row menu changes (`LlmProviderActionsMenu`), by branch:**
  - manual: `Edit` · `Set default for all` · separator · `Delete` — 3 items, unchanged.
  - provider-owned: `Members` (the same dialog, named for what it does) · `Set default for all` —
    2 items. **`Delete` is not rendered** (it is a 400 naming the provider), and
    **`Apply to existing users` is not rendered** — that action moved to the provider in §5.2, and
    the endpoint it calls is not on the router (see "Contract gaps").
- **Interaction model:** form, unchanged. One model per surface (§2) — the read-only block is text,
  so it introduces no second model.
- **States:** unchanged; the provider-owned branch has no test-connection (there is no key here to
  test) and no conflict `Alert` (there is no rule here to conflict).
- **Components:** `Dialog`, `Alert`, `Separator`, `UserAllowlistPicker`, `LoadingButton`.
- **Anti-patterns:** §4's "dialog showing disabled controls with no explanation" — avoided by
  construction rather than by annotation. The file also loses ~200 lines of dead form, which is the
  A5 row's "AI Credentials: in progress" moving forward.
- **Screenshots:** none. The read-only branch's whole risk is *"is the explanation there, in text,
  naming the owner and the place to change it"* — a JSX question, and §9 lists form dialogs and copy
  changes as the first case that does not need a capture.

---

### S8 — Managed credentials table (Source column and the vocabulary fix)

- **Intent:** unchanged — find a credential and act on it.
- **Change 1 — the Source column (§6.5).** Column 5 (`Auto`, `w-[12%]`, `AutoProvisionCell`) becomes
  **`Source`** in the same position and width, so no other column's width is disturbed and the count
  stays 9. It renders `provider_name` as a `Link to="/admin/ai-credentials" hash="providers"` when
  `is_provider_owned`, otherwise `<Badge variant="outline" className="text-muted-foreground">Manual</Badge>`
  — the "never a blank cell" rule `AutoProvisionCell` already states (`LlmProvidersTable.tsx:31-33`).
  `AutoProvisionCell` and its role chips are deleted with the column.
- **Change 2 — the §2.1 vocabulary regression, in two headers.** These are visible copy in which
  "Provider" means the vendor, on the very page that now also lists Providers:
  - column 2 header `Provider` → **`Type`** (it renders `getProviderTypeLabel(record.type)`);
  - column 3 header `Default provider` → **`Default key`** (it renders `set_as_default`, which is
    about a default *key*, not about a vendor and not about a provider).
  Nothing else in the table changes.
- **Pattern:** P9, unchanged — this table earns its columns through the pagination and the user
  filter the route already carries, which is the §2 test the S2 list fails and this one passes.
- **States:** unchanged.
- **Screenshots:** none — a column rename and a cell swap.

---

### S9 — Invite wizard, Provisioning step

- **Intent:** the admin wants to see, and adjust, what this person will get before sending.
- **Placement and pattern:** unchanged — step 2 of the existing P6 wizard, the flat `Checkbox`
  checklist inside `div className="max-h-[40vh] space-y-2 overflow-y-auto"`
  (`InviteUserDialog.tsx:521`). §2 excludes a selection checklist in a wizard step from the
  "list in a card" cap and prescribes exactly that max-height, so the container is already right.
- **Change: it lists providers.** `AdminAiProvidersService.listAiProviders()` on
  `["admin","ai-providers"]`, `enabled: isOpen`, replacing
  `AdminLlmProvidersService.listManagedAiCredentials({})` at `:159-168`. A manual managed credential
  is no longer grantable at invite time — the wizard lists key *sources*, which is Phase 3's decision.
- **Section label** `AI credentials` → **`AI providers`**; helper unchanged in shape:
  *"Pre-selected from what a {Role} is auto-provisioned. Adjust for this person only."*
- **Per item, two muted facts** replacing the current `· {default_model}` at `:540-546`:
  `{provider.name}` then `<span className="text-muted-foreground"> · {Kind label} · {Type label}</span>`
  — "Per-user keys · OpenAI". Kind and type are what an admin ticking a box needs to know; the
  default model is not, and it was the only fact shown.
- **Preselection** — the same predicate, read off the provider:
  `providers.filter((p) => (p.auto_provision_roles ?? []).includes(role)).map((p) => p.id)`,
  replacing `defaultCredentialIds` at `:112-119`. Seeded on the Next transition as today.
- **Tri-state, preserved verbatim** (`:275-277`): `provider_ids: providers ? (providerIds ?? suggestedIds) : null`
  — `null` while the query is pending or errored, `[]` when the admin unticked everything, the list
  otherwise. The field name changes from `managed_credential_ids`; the rule does not.
- **States:** the three existing messages keep their shapes, with providers substituted:
  loading *"Loading providers…"*; error *"Could not load AI providers. The invitation will still be
  sent, and the account gets whatever a {Role} is auto-provisioned."*; empty *"No AI providers are
  connected yet. Add one on the AI Credentials page."* — and that sentence must link, per §2's empty
  state row, to `/admin/ai-credentials#providers`, which the current copy ("Add one on the LLM
  Providers page") both fails to do and names a page that no longer exists under that name.
- **Screenshots:** none. The wizard has two steps, so §9's stepper trigger does not fire, and the
  change is a data source plus copy inside an unchanged container.

---

### S10 — Invite success panel: skips, and the declined default slot

This surface exists in this specification because it is where **both** of Phase 6's mandatory copy
items render, and neither has a reader today.

- **Intent:** the admin wants to know what the invited account actually got.
- **Placement and pattern:** unchanged — the P6 success panel, `text-xs text-muted-foreground` lines
  under the provisioning branch at `InviteSuccessPanel.tsx:190-198`.
- **New prop:** `providers: AIProviderPublic[]` (the array the wizard already holds), so both line
  families can name a provider instead of printing an id. Resolve with a
  `providerName(id): string | null` helper; a null result is the honest fallback and is exactly what
  `provider_not_found` will produce.

**1. The skip-reason map** — `SKIP_REASON_COPY` (`:21-27`) is keyed by reason and the row is now
`{ provider_id, reason }`, so `skip.managed_credential_id` at `:192` is a field that no longer
exists. New map, matching the wire vocabulary the model enumerates:

```
user_not_found:      "the new account could not be read back"
user_inactive:       "the account is not active"
provider_not_found:  "that provider no longer exists, or has no credential to grant through"
provision_failed:    "provisioning failed for that provider"
add_members_failed:  "the grant itself failed"
```

`managed_credential_not_found` is **removed** — nothing produces it any more, and a dead key in a
copy map is how the next reader concludes the old vocabulary is still live. Line template, keyed by
`skip.provider_id`: **`{name} was skipped — {copy}.`** when the id resolves to a provider the wizard
listed, otherwise **`One provider was skipped — {copy}.`** The second form is the normal rendering
for `provider_not_found` and is correct rather than degraded: an id that names no provider is
precisely what that reason reports.

*Correction to the plan's premise, stated so Phase 6 does not chase the wrong bug:* §5.6, §12 and §11
each say an unmapped reason "renders a blank line". It does not — the fallback expression is
`SKIP_REASON_COPY[skip.reason] ?? skip.reason`, so `provider_not_found` would render as the literal
token `provider_not_found` inside an admin-facing sentence. Worse in a different way, and the
required change is the same one; but the claim as written is not what the code does.

*Citation corrected in Phase 7.* This paragraph cited `:194` and §5.6 / §12 cited `:196`; neither
line held the expression. In the pre-Phase-6 file it was at `:196`, and Phase 6 rewrote the panel —
it is at **`frontend/src/components/Admin/InviteSuccessPanel.tsx:240`** in the built tree. A spec
citing a line that does not hold the expression is how the next reader concludes the expression
moved.

**2. The declined default slot** — `InviteProvisioningSummary.default_slot_skips`, typed
`{ provider_id, mode }`, is not read anywhere in the frontend today.

- **It renders as a note beside a successful grant.** Same `text-xs text-muted-foreground` class as
  the skip lines, in the same block, immediately after them — **not** destructive, **not** amber,
  **not** inside the `provisioningFailed` or `accountInactive` branches, and it must render when
  `added_count > 0` (which is the case it exists for). The three-way branch at `:143-189` is
  untouched; these lines join the always-rendered tail beside it.
- **Copy:** **`{name} was granted, but it did not become the {Mode} default — the account already
  had one, and it was kept.`** with `{Mode}` from `sdkModeLabel` ("Conversation" / "Building") and
  the same name fallback as above (*"A provider was granted, but…"*). The sentence says four things
  on purpose: the grant **succeeded**; which provider's policy did not take effect; which mode it did
  not take; and that nothing was lost. It never uses the words "failed", "skipped" or "error".
- **Why the shape is fixed rather than a designer's choice:** this is `adopted_existing_account`'s
  category — the codebase's existing "disclosure, not bookkeeping" — and its whole risk is being read
  as a grant that fell over. It never sets `provisioning_failed` and never joins `skipped`, and no
  count anywhere on this panel may include it.
- The credential already occupying the slot is deliberately not on the wire and must not be invented
  client-side.
- **Screenshots:** none. Copy inside an existing panel; §9's first "not required" case.

---

### S11 — Company AI providers card (Access tab)

- **Intent:** an admin setting up the front door wants to answer *"what does a new Agent Developer
  get?"* without opening every key source in turn.
- **Why it is in this specification.** §6 does not list it, but Phase 4 broke it: it PATCHes
  `auto_provision_roles` onto a managed credential (`CompanyAiCredentialsCard.tsx:148`), a field the
  generated `ManagedAICredentialUpdate` no longer declares, so the file does not compile today
  (`error TS2353` on that line) and every click would 422 if it did. It reads and writes the rule
  this feature moved, so it must move with it. This is a repair of a surface the phase already
  breaks, not new scope.
- **Placement and pattern: unchanged.** It stays a half-width **P5** card in the
  `grid grid-cols-1 lg:grid-cols-2 gap-6 items-start` at
  `routes/_layout/admin/server-configuration.tsx:88`, fourth of five, on `Common/PreviewList.tsx`,
  capped at 5 with the "Show all (N) on AI Credentials" link. Row shape: the **segmented
  multi-toggle row** — `CompanyAiCredentialRow` is A8's reference implementation of it and is
  re-pointed, not rewritten.
- **What changes — the entity, and only the entity:**
  - the query becomes `AdminAiProvidersService.listAiProviders()` on `["admin","ai-providers"]`;
  - the mutation becomes `AdminAiProvidersService.updateAiProvider({ providerId, requestBody: { auto_provision_roles: next } })`
    — the field exists on `AIProviderUpdate`, which is the point;
  - the row's `meta` fact becomes the **type label** plus the **kind**: `"OpenAI · Per-user keys"`.
    Two facts, `≤ 2` per P3, and the kind is what tells a "shared key" source from a "one key each"
    source at a glance. It stays on the metadata line and does **not** become a `Badge`: this row is
    ~276 px at 1024 and the three-segment toggle takes ~130 of it, which is the yardstick
    `CompanyAiCredentialRow.tsx:47-51` already writes down;
  - `findAutoProvisionConflict` and the refusal `Alert` keep their shapes; the `Alert` title becomes
    **"Another provider owns that default"** and the body uses the rewritten
    `describeAutoProvisionConflict` from S3;
  - the card title becomes **"Company AI providers"**; description: *"Key sources a new account
    receives, by role. Changing a role later never grants or revokes a key — use "Apply to existing
    users" on the AI Credentials page for accounts that already exist."* The empty state's link text
    becomes *"connect one"* and points at `/admin/ai-credentials#providers` — the hash S1 makes
    linkable, which is one of the two reasons §6.1 asks for it.
  - `sortForPreview` is unchanged in shape: granting first, then alphabetical.
- **Header icon: `KeyRound`, unchanged.** §7 item 9a — its four siblings in this grid are
  `RegistrationCard` (`ShieldCheck`), `SignInMethodsCard` (`LogIn`), `NewUserDefaultsCard` (`UserPlus`)
  and `LandingPageCard` (`Globe`); all five are half-width, all five carry one `h-5 w-5` lucide icon
  and a one-sentence description. This card already matches and must keep matching — changing the
  icon during a rename would break the row the Access tab rework closed on 2026-09-07.
- **Interaction model:** auto-save, unchanged — one click is one PATCH, with the per-record pending
  `Set` and the `onSuccess` cache write-through kept exactly as they are. That mechanism is load-
  bearing and its reasons are written out at `CompanyAiCredentialsCard.tsx:104-119` and `:153-172`;
  re-deriving it against a new service is how it gets lost.
- **States:** unchanged, including `isError && records === undefined` so a failed background refetch
  does not blank a live list.
- **Budget:** 3 blocks (the list, the conflict `Alert`, the footer link), 5 rows, 1 control per row.
- **Screenshots:** none. Same host, same grid position, same card and row shapes; the only visual
  change is one metadata string. R15's sibling comparison is answered from the neighbours' JSX, as
  R15 requires.

---

### Frontend phase order

Phase 6 is buildable in four passes. Passes 1 and 2 together take the tree back to a clean
`npx tsc --noEmit`; nothing else in the frontend references the deleted routes.

| Pass | Surfaces | Backend contract it needs |
|---|---|---|
| **6a — unbreak and repoint** | `useProviderAdapters.ts` (repoint to `AdminAiProvidersService.listProviderAdapters`, query key `["admin","ai-providers","adapters"]`); delete `Admin/ProviderAdminCredentials/`; S1 | `GET /admin/ai-providers/adapters` → `AdminAiProvidersService.listProviderAdapters` |
| **6b — the provider surfaces** | S2, S3, S4, S5, S6 | `GET/POST /admin/ai-providers/`, `GET/PATCH/DELETE /{id}`, `POST /{id}/verify`, `POST /{id}/rotate-key`, `POST /{id}/apply-to-existing?dry_run=` → `AdminAiProvidersService.{listAiProviders,createAiProvider,getAiProvider,updateAiProvider,deleteAiProvider,verifyAiProvider,rotateAiProviderKey,applyAiProviderToExisting}` |
| **6c — the credential surfaces** | S7, S8, S11 | `AdminLlmProvidersService.{listManagedAiCredentials,updateManagedAiCredential,setManagedAiCredentialDefault,deleteManagedAiCredential}`; `AdminAiProvidersService.{listAiProviders,updateAiProvider}` for S11 |
| **6d — the invite path** | S9, S10 | `UsersService.inviteUser` with `provider_ids`; `AdminAiProvidersService.listAiProviders` |

Shared edits, each touched by exactly one pass: `providerTypes.ts` (`describeAutoProvisionConflict`'s
sentence, and `PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY` deleted) in 6b;
`MemberKeyStatus.tsx` (`hasProviderKeyInFlight` extracted beside `hasKeyInFlight`) in 6b;
`Common/HashTabs.tsx` (`onTabChange`) in 6a.

---

### Where §6's inventory was incomplete

Three things §6 does not list, all of which Phase 6 must handle because Phase 4 already changed the
ground under them. Established by running `npx tsc --noEmit` in `frontend/` against the current tree.

1. **`Admin/AccessPolicy/CompanyAiCredentialsCard.tsx` does not compile** — it sends
   `auto_provision_roles` in a `ManagedAICredentialUpdate`. Specified as **S11**.
2. **`Admin/LlmProviders/useProviderAdapters.ts` does not compile** — it imports
   `AdminProviderAdaptersService`, which Phase 4 deleted with `/admin/provider-adapters`. It is the
   adapters reader for every surface here, so it is the first thing 6a repoints.
3. **`Admin/ProviderAdminCredentials/*` and the route's import of `AdminProviderCredentialsService`
   do not compile** — the service is gone. Both component files are deleted rather than migrated;
   S2 replaces them.

---

### Contract gaps for the planner

These are backend-side questions this specification cannot decide. Neither blocks 6a.

1. **`can_mint_now` no longer answers the question the create dialog asks.** §8 names it as the
   authority for whether "minted" may be offered, and Phase 4 defines it as
   `adapter.supports_minting and adapter.type in connected_types`
   (`admin_ai_providers.py:158-160`), where `connected_types` is the types that **already have a
   provider**. Under the old model that conjunction was right — a minted managed credential needed an
   organisation connected first, on a separate tab. Under the new model the provider *is* the
   organisation, so `can_mint_now` is `false` for the very first OpenAI provider an admin ever
   connects, and gating S3's tile on it would make the feature's headline scenario unreachable
   through the UI. **S3 therefore gates the "Per-user keys" tile on `supports_minting`**, the
   permanent capability, and never on `can_mint_now`. If the intended reading is different, the
   server should say so by redefining the field; the frontend should not carry a second definition.
2. **`AdminLlmProvidersService.applyManagedAiCredentialToExisting` is in the generated client but not
   on the router.** `frontend/openapi.json` still lists
   `/api/v1/admin/llm-providers/{managed_credential_id}/apply-to-existing`, and `sdk.gen.ts:1028`
   exports the method, while the only `apply-to-existing` decorator in `backend/app/api/` is
   `admin_ai_providers.py:401` — the string "apply" does not occur in `admin_llm_providers.py` at
   all. `frontend/openapi.json` is also unmodified in the working tree while `frontend/src/client/*`
   is modified, so the committed spec is behind the generated client, and the generated client is
   behind the router.
   Either way S7 does not call it — §5.2 moved the action to the provider and a manual credential has
   no roles left to apply — but a client regen should be run and the diff read before 6c lands.
3. ~~**`ManagedAICredentialPublic` still declares `auto_provision_roles`** while §3.2 drops the
   column.~~ **Answered in Phase 7, against the code: it does not.** The field is absent from the
   model (its docstring says so explicitly), from `frontend/openapi.json`, from
   `frontend/src/client/types.gen.ts`, and from the projection's construction site in
   `managed_ai_credentials_service`. There is no field with no reader here and nothing to remove.

---

### What Phase 6 owes §12 (testing checklist)

Recorded here rather than edited into §12, which other agents are working in.

- `cinna-core.ui.review` returns **PASS** on S1–S11 — the Phase 6 gate in §11.
- `npx tsc --noEmit` is clean for `frontend/src/components/Admin/**` and
  `frontend/src/routes/_layout/admin/ai-credentials.tsx`. It is **not** clean today; the three
  failures above are the baseline.
- The three screenshot bookings in S2, S3 and S4 are taken, or reported as "required but not
  capturable" with the reason. **No data is created to reach a state** (§9), including the provider
  the S4 capture needs.
- **The vocabulary check has a reader.** §12 already adds an architecture test that "provider" never
  means the vendor in code; the frontend half of it is a grep over
  `frontend/src/components/Admin/**` and `frontend/src/routes/_layout/admin/**` for a user-visible
  string in which *Provider* denotes a vendor. Its three current hits are named in S8 and S3 (two
  table headers and `describeAutoProvisionConflict`'s sentence) and are the only ones this
  specification found.
- Both mandatory copy items are asserted, not assumed: `provider_not_found` resolves to a sentence
  rather than to a raw token, and a `default_slot_skips` entry renders a note beside a grant while
  `provisioning_failed` is false and `skipped` is empty.

### What Phase 6 owes §13 (documentation checklist)

- `admin_ai_credential_provisioning.md` gains the admin's route through the new surfaces: connect a
  provider (three steps), what "extra key" means, and where the read-only managed credential's values
  come from.
- The vocabulary paragraph §13 already asks for should name the **UI** consequence explicitly: the
  AI Credentials page has a Providers tab, and the word "Provider" on that page never means the
  vendor — the vendor is the **Type** column.
- The `docs/README.md` glossary entry for **Provider** should say where an admin sees one:
  `/admin/ai-credentials#providers`.

### Open questions

Only decisions this specification cannot make alone.

1. **`can_mint_now`** — contract gap 1 above. S3 has a defensible answer and can be built on it, but
   the field's meaning should be settled rather than routed around twice.
2. **Tab titles.** "Managed credentials" and "Providers" are this specification's choice. If the
   product prefers "Key sources" over "Providers" as the tab label, say so before 6a — the hash value
   `providers` is linked from three places in this specification (S7's Alert, S9's empty state, S11's
   footer link) and is cheaper to fix once.

### Lessons for the guideline

- **A full-width tab body that is a list, not a table, has no rule.** §2's rows decide cards
  (half-width, capped at 5) and §1 decides Manage-list routes (`DataTable`), and S2 is neither: a
  Manage-list surface whose entity count never justifies search or pagination. The §2 "Card width"
  sentence — *"a table that genuinely needs its columns also needs search or pagination"* — is what
  decided it, read in the direction it was not written for. If a second instance appears, this is a
  named shape: **the tab-width row list**, `ListRowGroup` in the sibling table's `rounded-md border`
  frame, uncapped because R4's cap is a card rule.
- **Disclosure depth pushed a real control out of an Edit sheet.** S4 cannot host "Replace key"
  because Sheet → Dialog is level 3. The resolution — the action lives on the row menu and the Sheet
  says so in one muted line — worked, and the codebase had already reached it independently for
  Verify. §2's disclosure row could name it: *an Edit sheet's out-of-band actions stay on the row
  that opened it, and the sheet points at them.*
- **"Read-only" is better answered by removing controls than by disabling them.** §4's anti-pattern
  is "a dialog that shows disabled controls with no explanation" and the obvious fix is to add the
  explanation. S7 shows the stronger fix: render the values as text. There is then no disabled
  styling for the explanation to compensate for, which is also what §9's accessibility line is
  asking for. Worth a sentence in §2 or in §4's fix column.
- **A copy map keyed by a machine string needs its fallback stated.** Three places in this plan say
  an unmapped skip reason "renders a blank line"; the code prints the raw token. Both are bugs, but
  a spec that repeats the wrong one sends the builder looking for the wrong thing. §6 "States and
  copy" could say: a reason→copy map's fallback is part of its contract, and the spec names it.
