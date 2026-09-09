---
feature: admin_ai_credential_provisioning
domain: credentials
one_liner: "Superuser connects an AI provider (shared or per-user-minted key) that auto-provisions and reconciles per-user AI credentials for chosen roles."
docs:
  tech: admin_ai_credential_provisioning_tech.md
---
# AI Providers, Admin-Provisioned AI Credentials + Native Account-Config

## Purpose

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

- **Part A — Providers and the credentials they hand out.** A superuser connects a **Provider**: a source of keys plus the rule for who automatically gets one. Each Provider owns exactly one **managed AI credential**, which owns the member list, and each member holds an ordinary per-user `AICredential` child row that participates in all existing per-user plumbing. Users can use their child credential and set it as their default, but cannot edit, delete, or re-key it — those operations return `403`.
- **Part A′ — Per-user key minting.** A Provider is one of two **kinds**. A `fixed_key` Provider holds one pasted key that every member gets a copy of. A `minted` Provider holds an organisation administration secret and creates **each member their own key** at the vendor, destroying it again when they lose the credential. Only OpenAI can do this today; for every other vendor a key is **pasted**, which is a normal path and not a fallback.
- **Part B — Native account-config endpoint.** A native-token-gated endpoint (`GET /api/v1/external/account-config`) returns the caller's own usable AI credentials with the *decrypted* API key, so Cinna Desktop and Cinna Mobile can auto-create local "LLM providers" and a suggested chat mode per credential on login, without the user having to copy-paste keys into the native app.

> **Frontend status:** The admin **AI Credentials** section (`/admin/ai-credentials`) is implemented, with two hash-addressable tabs — **Keys** (`#keys`) and **Providers** (`#providers`). Superusers connect, edit, verify, re-key, apply and delete Providers from the Providers tab; the Keys tab lists **one row per real API key** and carries the per-key verbs (retry, set as their default, rotate, revoke). The user-facing AI Credentials card renders admin-managed child credentials with a **"Managed" badge** and a read-only **Default model** line when a curated default is set. The native-app side (Part B) backend is complete; Cinna Desktop/Mobile provider auto-creation is not yet built.

---

## The word "Provider"

**A Provider is an entity an administrator creates. It is never the vendor.** The vendor is the **type**.

| You mean | Say | Identifier |
|----------|-----|------------|
| A key source plus the rule for who gets one | **Provider** | `AIProvider`, table `ai_provider`, surface `/admin/ai-providers` |
| Anthropic, OpenAI, Google, MiniMax, OpenAI-compatible | **Type** | `AICredentialType`; the `Type` column and the wizard's first step |
| The server-side module that knows how to talk to a type | **Adapter** | `app/services/ai_providers/*` — see [provider_adapters_tech](provider_adapters_tech.md) |
| The credential record and its member list | **Managed AI credential** | `ManagedAICredential` |
| One person holding a key from a managed credential | **Member** | `ManagedAICredentialMembership` |

This matters on the AI Credentials page in particular, because that page now has a **Providers** tab: on it, "Provider" is the row, and the vendor is the `Type` column. The managed-credentials table names the vendor `Type` and the default-key flag `Default key` for the same reason — neither is a Provider.

The wire keys `conflicting_credential_id` / `conflicting_credential_name` on the slot-conflict `409` keep their old names deliberately; only what a person reads changed.

---

## The two configurations this is built for

Both are ordinary setups an administrator chooses between, and they compose: a company can run both at once, because the second one claims nothing the first one needs.

### 1. Per-user spend tracking

The admin creates a project in the OpenAI console and sets a monthly spend limit on it. In Cinna they connect it as a Provider of type `openai`, kind **per-user keys** (`minted`), naming that project, and tick **every role** under auto-provision.

From then on, every new account gets **its own key**, created at OpenAI under that project. Usage is attributable per person in the vendor's own console, and revoking one person is destroying one key. The project's spend limit bounds the whole thing, and Cinna refuses to mint into a project that has no limit at all.

Nothing here waits on OpenAI. A signup writes a `pending` membership row and returns; a converge pass picks it up on a later tick.

### 2. An extra key for one role

The admin has an Anthropic key they want their developers to have **in addition** to whatever they already use. They connect a Provider of type `anthropic`, kind **fixed key**, auto-provisioning to `agent-developer`, with **`set_as_default` off**.

Every new `agent-developer` receives that key. It shows up in their settings and in Cinna Desktop, and it is selectable per agent — and it **displaces nobody's default**. It claims no `(role, mode)` default slot, so it does not collide with configuration 1, and two providers like it may coexist without either being refused.

**`set_as_default` is the "extra key" switch, and this is a headline configuration rather than an edge case.** With it on, the Provider is saying *this becomes their default key*; with it off, it is saying *this is one more key they can pick*. The admin surface states both in full, next to the toggle.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **Provider** | An `AIProvider` row: one source of keys, plus the rule for who automatically gets one. Holds the secret, the type, `auto_provision_roles`, and the whole wiring policy (`set_as_default`, `set_user_sdk_defaults`, `sdk_default_modes`, `default_model`, `available_models`, the per-mode model overrides, `expiry_notification_date`). Server-scoped, superuser-only, and its secret is never returned by any endpoint. Owns exactly one managed AI credential. |
| **Kind** | `fixed_key` (one pasted key, copied to every member) or `minted` (each member gets their own key, created at the vendor). Chosen when the Provider is connected and fixed afterwards — the two secrets are not interchangeable. |
| **Type** | The vendor: `anthropic`, `openai`, `google`, `minimax`, `openai_compatible`. Stored as `provider_type`, exposed on the wire as `type`. |
| **Managed AI credential (parent)** | A `ManagedAICredential` row. Holds the name, the type, `base_url` / `model`, and the member list. When it belongs to a Provider it is read-only for the key and every wiring flag; when it has no Provider it is a **manual** record and its own columns are the real values. |
| **Manual managed credential** | A managed AI credential with no `provider_id`. It holds its own pasted key, its own wiring policy, and a member list the admin curates by hand. It auto-provisions to nobody — the rule is a property of a Provider. Manual records keep working exactly as they did. |
| **Membership row** | A `ManagedAICredentialMembership` row, one per `(parent, user)`. **The only definition of "who is a member."** Carries an explicit `status` and, for a minted key, the provider handles needed to revoke it. Membership used to be *derived* from the children; that derivation is **gone**, not kept alongside. |
| **Child credential** | An ordinary `AICredential` row with `is_admin_managed=True`, `managed_credential_id` pointing at the parent, and `owner_id` set to the target user. Participates in all existing per-user plumbing; read-only through the user-facing CRUD. A *consequence* of membership: present when a key exists, absent while one is being minted or after a mint has failed. |
| **Provisioning mode** | `shared` or `minted`, **derived** rather than stored: no Provider → `shared`; a `fixed_key` Provider → `shared`; a `minted` Provider → `minted`. Still on the wire as `ManagedAICredentialPublic.provisioning_mode`, so a record and its Provider cannot disagree about it. |
| **Wiring policy** | The set of facts that decide how a recipient's profile is wired: `set_as_default`, `set_user_sdk_defaults`, `sdk_default_modes`, `default_model`, `available_models`, `model_override_*`, `expiry_notification_date`. Owned by the Provider for a provider-owned record, and by the record itself for a manual one. Read through one resolver — see the [tech doc](admin_ai_credential_provisioning_tech.md#the-policy-resolver-provisioning_policypy). |
| **Reconcile** | The diff-and-converge operation: Add (desired − current) creates a membership row and, in shared mode, a child; Remove (current − desired) deletes the child (Tier-2 blast-radius gated) and then the membership row, scheduling a provider revoke for a minted key; Update (intersection) writes changed parent fields and/or rotated key through to each child that exists. Idempotent — a no-op desired set with unchanged fields produces empty added/removed/updated lists. |
| **Account config** | The native-client bundle: a list of provider descriptors (with decrypted API keys) that Cinna Desktop / Mobile uses to create local LLM providers on login. |
| **Native token gate** | The `account-config` endpoint is only accessible to JWTs whose `client_kind` claim is `"desktop"` or `"mobile"`. Plain web-session JWTs are rejected `403`. |
| **Auto-provision roles** | `AIProvider.auto_provision_roles` — the roles whose **newly created** accounts receive a key from this Provider. Empty (the default) means nobody. Read at the account-creation chokepoint, never on a later role change. |
| **Apply to existing users** | The explicit admin action on a Provider that grants it to the accounts that already exist. Add-only; previewed with a dry run before it is committed. |
| **Model override (per mode)** | `model_override_conversation` / `model_override_building` — the model id pinned on a *member's profile* (`User.default_model_override_<mode>`) for the modes this policy wires. Distinct from `default_model`, which is written onto the *child credential row*. |
| **Default slot** | One of the two `(role, mode)` positions on a member's profile — `default_ai_credential_conversation_id` / `default_ai_credential_building_id` plus their model override. A slot holds exactly one credential, which is why only one Provider may claim it per role. |
| **External key ref** | The provider handles for a minted key (`project_id`, `service_account_id`, `api_key_id`, `scopes_requested`, `scopes_granted`), stored on the membership row. **A key nobody can name is a key nobody can destroy** — this is what makes revocation possible, and it is written *before* the key is stored. |
| **Converge pass** | One tick of the key-provisioning scheduler: pick up membership rows that are due a mint, call the provider, settle each row. Runs every minute, single-leader across workers. |

---

## Part A — Providers and Provisioning

### The split: what holds the key, and what holds the rule

One record used to be both a credential and its own factory. `ManagedAICredential` held a key *and* the rule that handed that key out, which is why "give this one key to every new agent-developer" was expressed as a property of a key.

Those are now two records:

```
Provider (ai_provider)
    kind: fixed_key | minted
    type: anthropic | openai | google | minimax | openai_compatible
    secret: the fixed key, OR the organisation administration key
    rule:   auto_provision_roles
    policy: set_as_default, set_user_sdk_defaults, sdk_default_modes,
            default_model, available_models, model_override_*,
            expiry_notification_date
        │
        │  owns exactly one
        ▼
ManagedAICredential
    provider_id (NULL → a manual record)
    members: ManagedAICredentialMembership, one per person
        │
        │  one child per member
        ▼
AICredential (owner_id = the member)
    fixed_key → a copy of the Provider's key
    minted    → that person's own minted key
```

Three consequences worth stating plainly:

- **A Provider owns exactly one managed credential**, written together in one transaction by `AIProvidersService.create`. The foreign key allows many; the service is what holds the shape.
- **Nothing downstream of `AICredential` changed.** The per-user child row is what makes a key usable — `User.default_ai_credential_<mode>_id` points at it, `agent_environment` links it, `/external/account-config` ships it to Desktop, and deleting it is how one person is revoked.
- **A manual managed credential works with no Provider at all.** It keeps every control it has today: its own key, its own wiring policy, its own hand-curated member list. What it cannot do is auto-provision, because that rule lives on a Provider.

On a provider-owned record the same-named policy columns are still present in the table and are **shadowed** — no longer read. They are deliberately not mirrored down from the Provider: a second copy of a number the Provider owns goes stale the moment either side is edited, and a stale copy displayed as the active policy is worse than no copy. One resolver answers the question instead.

### What the admin sets on a Provider

| Option | Effect |
|--------|--------|
| **Name** | Free-form label. The name a refusal, an audit row and the managed-credentials **Source** column all use to point at this Provider. |
| **Kind** | `fixed_key` — everyone shares one key you paste. `minted` — each person gets their own, created in your provider account. Fixed once connected. |
| **Type** | The vendor. Fixed once connected; changing it would re-point an existing member's key at a different vendor. |
| **Secret** | The model API key (`fixed_key`) or the organisation administration key (`minted`). Write-only: accepted on connect, never in any response, replaced through **Replace key** rather than through an edit. |
| `base_url` / `model` | For a `fixed_key` Provider whose adapter requires them. They are part of the key's shape, so they live in the Provider's encrypted envelope and an edit here re-encrypts it — which is what makes the change reach members added afterwards as well as those already there. |
| `config.organization_id` / `config.project_id` | For a `minted` Provider: which organisation and project keys are minted into. Rendered from the adapter's own `admin_config_schema`, never from a client-side list. A misspelled key here is a `422` naming the field, not a silent drop. |
| `auto_provision_roles` | Roles whose newly created accounts receive a key from this Provider. `[]` (default) = never granted automatically. An unknown role is a `400`, not a silent drop. |
| `set_as_default = True` | Each member's child becomes its owner's default credential of that type, including profile auto-sync. Off means an **extra key** (configuration 2 above). |
| `set_user_sdk_defaults = True` | Wires each owner's `default_sdk_conversation` / `default_sdk_building` and `default_ai_credential_*_id` to their child, using the same SDK composition as the Add Environment dialog (`claude-code` for Anthropic/MiniMax, `opencode/<provider>` for OpenAI/Google/OpenAI-Compatible). |
| `sdk_default_modes` | Modes to wire: `"conversation"`, `"building"`, or both (default). Modes incompatible with the credential type are silently skipped. |
| `default_model` | Preferred model ID (bare concrete id, e.g. `claude-sonnet-4-6`). Written onto each child row. When set and the environment has no per-mode override, this model is used instead of the catalog tier default. `NULL` = use the catalog tier default. |
| `available_models` | Curated list of selectable concrete model IDs written onto each child row. When non-empty, model-picker datalists and the native `suggested_models` field show this list instead of the auto-discovered list. `NULL`/empty = fall back to `discovered_models`. |
| `model_override_conversation` / `model_override_building` | Model id pinned on each member's profile for that mode, written whenever this Provider wires that mode's SDK default. Blank = no opinion, and members fall back to the credential's own `default_model`. |
| `expiry_notification_date` | Informational reminder about the key behind this Provider. It describes the key, and the Provider owns the key. |

**Editing any of the policy fields re-applies to everyone who already holds the key.** Model and override changes are written through to every member; clearing an override unpins the members it pinned. The edit sheet says so at the head of the Provisioning policy section: the behaviour is invisible until something states it, and that is where an admin is about to trigger it.

### How a member gets a key

A member of a `fixed_key` Provider's credential gets a child `AICredential` built from the Provider's encrypted envelope, immediately, in the same request. A member of a `minted` Provider's credential gets a `pending` membership row and nothing else, and a converge pass creates their key on a later tick — **no provider call ever happens on a request path**.

Membership is a **row**: one `ManagedAICredentialMembership` per `(parent, user)`. It is the only definition of "who is a member", and it replaced a previous *derivation* from the children — the derivation is gone rather than kept in parallel, because two definitions of membership is exactly the duplication that produces a member list which is quietly wrong.

Each child row carries:
- `owner_id = target_user.id` — the row participates in all existing per-user logic
- `is_admin_managed = True` — the single behavioral flag that makes the row read-only for the owner
- `managed_by_id = parent.managed_by_id` — audit-only, SET NULL when the admin account is deleted
- `managed_credential_id = parent.id` — structural link; SET NULL if the parent is ever deleted out-of-band (children degrade to plain `is_admin_managed` orphans rather than vanishing)

**Why membership had to become a row.** Per-user minting separates two facts that used to be the same one. The moment an admin adds somebody to a minted record they *are* a member and they do *not* hold a key — the vendor has not been called yet, and the call may fail. A derived model has nowhere to put that: a row that does not exist cannot carry a status, and the alternative (a child credential holding an empty key) breaks the invariant every consumer of the credentials table relies on — **every `AICredential` row that exists is usable**. There is no placeholder credential, no "preparing" state on a credential, and no empty-key sentinel anywhere in this design.

### Reconcile semantics

| Pass | Logic | Per-child failures |
|------|-------|--------------------|
| **Add** (desired − current) | Validate user exists + is active. **Shared mode:** decrypt parent key; create child via `ai_credentials_service.create_credential`; stamp markers + parent link; write a membership row with status `not_applicable`; apply optional default/SDK wiring. **Minted mode:** write a membership row with status `pending` and stop — *no provider call happens on a request path*. | Unknown/inactive user → `skipped`. Committed child with failed post-create wiring is retained as a member (not demoted to skipped). |
| **Remove** (current − desired) | Delete the child via `ai_credentials_service.delete_credential(admin_override=True)`, then delete the membership row, then schedule the provider revoke for a minted key. Tier-2 bundle blast-radius triggers `AICredentialInUseError`. | In-use child → `blocked` (member stays) unless `force=True`. Any other removal failure → `blocked` with reason `remove_failed`. **A member whose status is `minting` → `blocked` with reason `mint_in_flight`** (see below). The PATCH still returns its reconcile result rather than a `500`. |
| **Update** (current ∩ desired) | Diff parent scalars against child; write changed fields/key through; apply/clear `set_as_default`. **A member with no child row is skipped silently** — there is nothing to write through to, and it is not a loss: when their key is minted the child is created from the parent as it stands *then*, so it cannot be stale. Only mutated children appear in `updated`. | Failed update → `skipped` with reason `update_failed`; the rest of the reconcile continues and the PATCH still returns its reconcile result rather than a `500`. |

The response is a `ManagedAICredentialReconcileResult` carrying the parent public record plus `added`, `removed`, `updated`, `updated_count`, `skipped`, and `blocked` lists.

**Removal is refused while a mint is in flight.** A converge pass that has claimed a membership row is inside a provider call in another session, and is about to write the `external_key_ref` for a service account the provider has already created. Deleting the row underneath it means that ref lands nowhere: the key exists, no row names it, and no revocation can ever be scheduled for it. So the removal is blocked for at most one converge tick and the admin retries — a far better outcome than a key nobody can find.

**A blocked member is told *why* by the server, in one sentence, from one place.** `ManagedReconcileBlock` carries a `message` alongside its `reason`, **required with no default**, filled from a reason→sentence table declared beside the model, and rendered verbatim by all three consumers (the `409` body, the force-delete confirmation dialog, and the member-dialog toast).

It used to carry only the reason, and each of the three renderers substituted its own copy of the constant "in use by a published bundle". So an admin blocked by an **in-flight mint** was told they had a bundle conflict — and the remedy for a bundle conflict is `force=true`, which on that path strands a live provider key. Three surfaces, one wrong sentence each, and the wrong sentence pointed at a destructive action. The rule is now structural: `message` being required makes the table-consulting constructor the only practical one, and membership of that table is also the validity test for a reason string, so a fourth reason cannot be added without a sentence to go with it.

| `reason` | What the admin is told |
|---|---|
| `in_use_bundle` | "Their credential is in use by a published bundle. Removing them anyway degrades that bundle back to \"user provides\"." |
| `mint_in_flight` | "A key is being created for them right now. Try again in a moment — forcing it through does not help and is not needed." |
| `remove_failed` | "Removing their credential failed unexpectedly. It has been logged; try again." |

**Delete first, revoke second**, in every removal path. The child delete can be refused by the Tier-2 blast-radius gate, and a revoke that ran first would leave a dead key on a surviving row that reads as healthy everywhere and fails at first use. A blocked member keeps a *working* key, which is recoverable. Revocations are handed to a background task after every row change has committed, never awaited — reconcile runs on request paths, and an admin's PATCH must not be as slow as the provider is.

**Member removal order is sorted.** Removals (and therefore revocations) run in a stable order on every run, because `set` iteration order is not stable and a failure report that reads differently each time it is produced is a failure report nobody trusts.

### Kind: one shared key, or one key each

A Provider's **kind** is chosen when it is connected and fixed afterwards, because the two secrets are not interchangeable.

| Kind | What the admin supplies | What each member holds | Revoked when they leave? |
|------|------------------------|------------------------|--------------------------|
| `fixed_key` | One model API key, pasted | A child credential holding a **copy** of that key | No — one key held by many people must not be destroyed because one holder left. Their child row simply stops being reachable |
| `minted` | An organisation **administration** key and the project to mint into — no model key at all | A child credential holding a key created for **them alone** | Yes, at the vendor |

The managed credential a `minted` Provider owns holds no key of its own (`encrypted_data` is NULL), and a `fixed_key` Provider's key lives on the Provider rather than on the credential. **Replace key** is therefore a Provider action, and it is refused with a `400` on a `minted` Provider: there is nothing there to rotate, and silently accepting the paste is how an admin comes to believe they have rolled a key they have not. Re-keying one member of a `minted` Provider is a per-member mint, not a Provider edit — which is why the row menu omits the item for a minted Provider rather than showing it disabled, and the edit sheet says so in the Key section.

#### Pasting a key is a normal path

**Anthropic's administration API lists and updates keys; it does not create them** — verified against the API and the SDK. So for an Anthropic Provider (and every type but OpenAI today) the administrator pastes a key, and that is the ordinary way those Providers work. It is not a fallback, not a degraded mode, and not something the product apologises for. The **Per-user keys** tile is unavailable for such a type and the reason is written next to it in the type's own name, composed from what the adapter declares — *"{Type} cannot create keys through its API, so a {Type} provider shares one pasted key. That is a normal setup, not a limitation of this server."*

Whether the tile may be offered is one term, `supports_minting`, published by `GET /admin/ai-providers/adapters` and enforced by `AIProvidersService._validate_shape`. It is a fact about the vendor's API, not about what the instance has connected — see [provider_adapters_tech](provider_adapters_tech.md#can_mint_now-is-gone-and-why-it-had-to-go) for the field that used to conjoin the two and why it was removed.

The one thing that is kind-agnostic is **membership**. A fixed-key member and a minted member are both membership rows, both appear in the same member list, and both are reconciled by the same pass.

### Connecting a Provider

A superuser connects a Provider on **Admin → AI Credentials → Providers**. Connecting writes the Provider and its one managed credential together, in one transaction, and grants any initial members through the same reconcile every later grant runs.

The secret a Provider holds means one of two categorically different things, keyed to its kind: a `fixed_key` Provider holds an ordinary **model API key**, and a `minted` Provider holds an **administration** secret that does not call a model at all — it creates and destroys keys for a whole organisation, and it is the only means of revoking what it has minted.

The row lives in its own table for a structural reason, not a stylistic one, and that argument is unaffected by the fixed-key addition because it is about the *table*. Two existing services are correct for the table they read and would both reach either secret the day it lived in `ai_credential`: the model-discovery cron selects **every** row and calls the vendor with each key, and `GET /external/account-config` hands **every** row a user owns, decrypted, to the desktop client. Sharing, environment linking, bundle publisher wiring and the blast-radius counts are all foreign keys pinned to `ai_credential.id`; the environment credential bag is a fixed slot dict with no slot to pour into. A table that is not `ai_credential` is not reachable from any of them.

What the admin does with a Provider:

| Action | Effect |
|--------|--------|
| **Connect provider** | Stores the name, kind, type, encrypted secret, config and the whole provisioning policy, and creates the managed credential it owns. **No vendor call is made** — so a vendor outage cannot stop an admin from recording the configuration |
| **Verify** | One press, and *which* question was asked depends on the kind. A `minted` Provider is asked whether its administration secret authenticates **and** whether the project carries a hard spend limit (read from the vendor; Cinna cannot set one) — the two fail independently, and an admin who fixes one wants to see the other without a second round trip. A `fixed_key` Provider is asked whether the key works. The result is stamped (`last_verified_at` / `last_verify_error`) and shown on the row |
| **Edit provider** | Name, key shape, config, and the whole provisioning policy. Every policy change **re-applies to existing members**. The secret is not editable here |
| **Replace key** | `fixed_key` only. Re-encrypts on the Provider and writes the new key through to every member's child credential. A `400` on a `minted` Provider |
| **Apply to existing users** | Grant this Provider to every active account whose role it already covers. Add-only, previewed with a dry run |
| **Delete provider** | Refused `409` with the impact — **named people**, not a count — while anybody holds a key from it. `force` revokes every minted key at the vendor first, then removes the child credentials, the memberships, the credential and the Provider, in that order |

The secret is write-only: accepted on connect, replaced through **Replace key**, and never a field of any response. The admin-facing projection carries `has_secret: bool` instead — copying `MailServerConfigPublic`, because a projection that carries the value's *slot* is one refactor away from carrying the value. There is no reveal endpoint.

**Deleting a `minted` Provider destroys keys.** Losing the administration secret does not only stop new keys being minted; it strands every key already minted with it, because an administration call authenticated by that secret is the only way to destroy one. So the forced delete revokes first, while the secret still exists, and the confirmation says so: *"{n} of those keys were created at {Type} and will be revoked there. They stop working immediately."* For a `fixed_key` Provider the sentence is the other one — the copies here are deleted, and the key itself stays valid at the vendor until the admin removes it in their own console.

`created_by_id` is **SET NULL**, never CASCADE. User deletion is a bare cascade, and deleting the superuser who happened to paste the key must not destroy the instance's ability to mint and — worse — to *revoke* every key it has ever minted.

### Deleting the credential a Provider owns

**A provider-owned managed credential cannot be deleted on its own.** `DELETE /admin/llm-providers/{id}` answers `400` naming the Provider, and the sentence says where Delete lives: *"This credential belongs to the AI provider '{name}' and cannot be deleted on its own — the provider would go on auto-provisioning with nothing to grant through. Delete the provider instead; that revokes every key it issued first."*

This is a behaviour an admin can hit, not a defensive check. The foreign key is `ON DELETE RESTRICT` in one direction only, so deleting the credential used to leave the `ai_provider` row standing with its `auto_provision_roles` intact — and the auto-provision scan **drops a Provider that owns no credential silently**: no skip, no log, no event. Every later signup for those roles would get nothing, for ever, while the admin surface went on showing the rule as active. The read-only branch of the managed-credential dialog therefore says *"Deleting this credential means deleting its provider"* rather than leaving an admin hunting for a Delete that is not rendered.

The **forced Provider delete** could produce the same orphan from inside its own gate, and no longer does. `ManagedAICredentialsService.delete(force=True)` reports blocked members *and deletes the parent anyway*, so refusing on that report deleted the credential, answered `409`, and left the Provider — the silent no-op the gate exists to prevent, produced by the gate. The refusal is now keyed on whether the credential row genuinely survived, which is where `RESTRICT` would otherwise trip, and it carries its own code (`ai_provider_members_not_removed`) so a client reading `detail.code` on the first `409` shape does not get `undefined` on the second. The admin retries, and the retry completes the removal.

### The spend-limit precondition

**The project's monthly spend limit is set on the provider's own console, and Cinna only ever reads it.** There is no editable threshold on this side, no "Apply spend limit" action, and no fallback value — the capability was removed rather than hidden. The limit belongs where it is authoritative: the same provider organisation is reachable from the console and from any number of other tools, so a threshold stored here is a second copy of somebody else's number that goes stale the moment the real one is edited, with nothing to notice. A stale figure presented as the project's cap is worse than showing none.

What remains is the check, and it runs before any key exists: an uncapped project is refused. Nothing caps a project *after* minting has started either, which was never something to give up — capping a project that already holds live keys leaves a window in which an uncapped key is in the world.

The predicate is **the existence of a project spend limit with a threshold**, and it is defined in exactly one place (`SpendLimitStatus.is_capped`). A project with no limit object at all is refused with `project_not_capped`. Verify reports that to the admin, and the sentence points at the only place that can fix it: *"The key works, but the project has no monthly spend limit. Set one in the OpenAI console — keys are not created in an uncapped project."*

**`enforcement.status` is deliberately not the predicate.** It is the limit's *current* runtime state, not its configuration — the provider's generated types read *"Whether the hard spend limit is **currently** enforcing"*. A correctly capped project sitting at $0 of $100 reports `inactive`, which is the state a healthy capped project is in essentially all of the time; `enforcing` means the threshold has been reached and traffic is already failing. Gating on `enforcing` inverts the check: it refuses every healthy project and admits only an exhausted one, where a minted key is dead on arrival. That is how this first shipped, and it is why the rule now reads the object's existence instead. Monitoring without enforcement is a separate object — a project *spend alert* — that this code never reads.

Enforcement is documented as not instantaneous at the provider: recorded spend can slightly exceed the cap. Nothing here promises a hard stop; it promises a limit.

The one amount that crosses a boundary is the threshold read back from the provider, and it is **integer cents** with the unit in the field name (`threshold_cents`), because an off-by-100 here is a hundred-fold cap.

### What a minted key carries

A minted key carries **write access to the project's API resources**. Its blast radius is bounded by the project's spend limit, which Cinna verifies is in place before minting into that project, and by revocation. That is the whole of the claim.

It is deliberately not softened. The provider documents default service-account permissions as read and write of all of the project's API resources; nothing documents a narrower guarantee we could inherit. Three things follow, and they are recorded as decisions rather than left to be rediscovered:

- **We do not request key `scopes` when minting.** The scope vocabulary is an open string array with no enum, and no artifact we hold contains it — the pinned `openai==2.15.0` has no `organization` resource at all — so a guessed list would hard-fail the create call the day the provider changed it. `scopes_requested` and `scopes_granted` are nevertheless recorded on **every** external ref, so a later hardening pass changes two values rather than rewriting the record, and no child ever claims a narrowness it does not have.
- **`model_permissions` was deliberately not used.** A known lever, deliberately not pulled in this pass.
- **The single enforcement lever is therefore the project spend limit.** This is not defence in depth, and it is checked *before* a key exists rather than applied after one does.

### The name a minted key carries at the provider

A minted service account is named **`<member email> (<membership id>)`**, so a person in the provider's own console — OpenAI's project → service accounts list — can read off whose key it is without a lookup table on our side. The email leads because that is the question being asked; the id postfix is what keeps two keys for the same person apart (a re-mint after a revoke, or the same person under two Providers) and names the membership row whose credential the key ended up on.

The credential's id cannot be used there: the credential row is created *from* the minted secret, so it does not exist when the name is chosen, and the provider has no rename — a service account can be created, listed and deleted, never modified. The membership id is the identifier that exists on both sides of the mint.

This is also what makes the one unavoidable leak nameable: a crash between the provider creating the service account and Cinna recording it leaves an orphan that still says whose it was.

### One project, not one per user

Single-project only. A project per user was considered and dropped: projects cannot be deleted at the provider (archive only, and archive is irreversible), the ceiling is around 2,000 per organisation, and archived projects are retained — so project-per-user is a one-way ratchet that buys roughly 2,000 *lifetime* provisions and two permanent failure classes no retry can resolve. Service accounts have no documented ceiling, are genuinely deletable, and per-user cost attribution is available under one project anyway.

### The Keys surface

**One row = one real API key.** The record list answers "what records exist"; that is a different question from "what keys exist", and for a minted Provider the two had drifted a long way apart — one record row stood for one key per member, and an admin could see them only as a row of chips inside a table cell.

The arithmetic is the model's, not a presentation choice:

| Record | Real keys at the vendor | Rows |
|---|---|---|
| A `minted` Provider's credential | one per member, each with its own `external_key_ref` and its own child credential | **one per member** |
| A `fixed_key` Provider's credential, or a manual record | **one** — the parent's key, copied onto every member's child row | **one for the record** |

So the N child credentials under a shared record are *copies*, not keys, and the row count follows the number of secrets that exist at the provider. That is what makes the surface mean something at company size, and why it is paginated, searched and filtered on the server rather than in the browser.

A row with **no key yet** — `pending`, `minting`, `failed`, `suspended` — is still a row. It is the only place an administrator sees that one person's key is stuck, which is half the reason the list exists.

#### The four per-key verbs

Each addresses one membership row by id.

- **Retry** — a terminally `failed` key back into the queue at zero attempts. `400` if it has not failed.
- **Set as their default** — makes this key its holder's default for its type. The per-person counterpart of *Set default for all*, which is an administrator overwriting everybody's choice; this one fixes the single person whose grant declined an occupied slot under the incumbent-wins rule. `400` when no key exists yet: there is nothing to point a default at, and creating something to point at would break the invariant that every credential row that exists is usable.
- **Rotate** — destroy this key at the vendor and queue a fresh mint. It is the **suspend/resume pair applied to one row**, not a second implementation: deactivating an account already destroys a minted key and reactivating it already mints a new one, with the delete-then-revoke ordering and the never-leave-a-key-live-and-unrecorded guarantee that sequence carries. The holder is **without a key until the next converge tick** — at most a minute, and the confirmation says so. Refused for a shared key (replace that on the Provider) and while a mint is in flight.
- **Revoke** — the same removal a PATCH of the member set performs, aimed at one person: the child credential is deleted, then the key is destroyed at the vendor. It inherits the Tier-2 blast-radius gate (`409` with the impact unless forced) and the refusal while a mint is in flight. On a **shared** record it removes that person's copy and leaves the key working for everyone else.

Granting is deliberately **not** a verb here. A new member is added on the record or by the Provider's rule; this surface is where keys are seen, repaired, rotated and revoked, which are the four things that are per-key and had nowhere to live.

### Membership statuses

Every membership row carries an explicit status. **None of them is ever inferred from a NULL** — including the terminal ones. The one absence that means something is the *row*: no row = never a member.

| Status | Meaning | Holds a key? |
|--------|---------|--------------|
| `not_applicable` | **Terminal.** A shared parent's member. There is no provider call in this member's story and there never will be. Distinct from `provisioned` because the two revoke differently | Yes (a copy of the shared key) |
| `pending` | A mint is owed; the next converge pass will attempt it | No |
| `minting` | An attempt is in flight, claimed by a converge pass | Not yet |
| `provisioned` | **Terminal.** A key was minted and the child row holds it; `external_key_ref` carries the handles needed to revoke it | Yes (their own) |
| `failed` | **Terminal.** Bounded retries were exhausted. Durable, visible, and never cleaned up automatically | No |
| `suspended` | **Terminal until reactivation.** The owner's account was deactivated: their key was revoked and their child row deleted, but they are still a member | No |

**A failure is durable.** A `failed` membership is never deleted to tidy up. "Account creation never fails because provisioning failed" is only an honest promise if the failure is still visible to an administrator afterwards — otherwise the statement is true and worthless. `suspended` is deliberately not `pending` for the mirror-image reason: a pending row on a disabled account would read as "still working" for as long as the account stays disabled.

### The provisioning lifecycle

**No provider call ever happens on a request path.** Adding a member records an intent and returns. Account creation (signup, the OAuth callback, an invitation accept) runs the same `add_members` inline, and a provider timeout on that path would become a failed login.

A **converge pass** runs every minute, picks up membership rows in `pending` or `minting` whose next attempt is due, and attempts them in batches of 25. Brand-new rows (`next_attempt_at IS NULL`) are ordered **first**, explicitly, so a batch full of backed-off failures cannot starve the people who just joined.

Each attempt:

1. **Claims the row first** — status `minting`, attempt count incremented, committed — *before* any configuration check. Every path from that point is an attempt and must cost an attempt. An earlier version checked the configuration before claiming, which meant a record wired to a deleted admin credential retried every minute forever, never reached the ceiling, and so never became `failed` — precisely the "infinite backoff that reads as still working" this design refuses.
2. **Revokes any stale key named by `external_key_ref`** before minting a new one. A previous attempt that stored handles and then died left a key we own and cannot use; destroying it first is why a crash costs a wasted key rather than a leaked one.
3. **Mints**, then commits the provider handles **in their own commit, before the key is stored**.
4. **Materialises the child credential** and settles the row to `provisioned`.

**Every write after the provider call is conditional on the row still being claimed.** Claiming is not only a way to stop two workers minting at once; it is a token the process carries through the `await`. While the provider is being called, an admin can deactivate the account, delete it, or force-delete the parent record — and any of those settles the row underneath the mint. So each write is an `UPDATE … WHERE id = ? AND status = 'minting' AND updated_at = <the stamp we claimed with>`, and a zero-row result means the claim is gone.

**On a lost claim the key that was just minted is revoked rather than stored**, and if the revoke itself fails the durable `admin.ai_credential.revoke_failed` event carries the external ref. The invariant is written at the mint site:

> *A minted key must never end up live-but-unrecorded. If you cannot store it, revoke it; if the revoke fails, emit the durable event with the ref in it.*

A key held in a local variable and named by nothing else is the one state from which no later pass can recover — no retry, no deactivation cascade, no deletion sweep — because nothing knows it exists. Three races are covered by tests: **deactivation**, **deletion** and **force-delete of the parent record**, each run against an in-flight mint.

Retries are bounded: **5 attempts**, backing off 60s → 300s → 900s → 3600s, then terminal `failed`. The ceiling is what makes `failed` mean something — a permanently broken configuration (a revoked admin secret, an uncapped project) would otherwise sit at `pending` with an ever-later retry, which reads as "still working" to every surface that shows it.

**One window remains and is stated rather than papered over.** Between the provider creating the service account and this process committing anything, there is no record. The create is a single call that returns the secret — splitting it in two would mean asking for a response that carries no secret at all — so a crash inside that window leaks one service account. It is visible in the provider's own console and is not silent there; nothing we could write would close it.

#### Retrying a failed member

`failed` is terminal on purpose, but terminal must not mean unreachable. **Admin → AI Credentials → Keys** shows a failed key with the reason on the row and **Retry** in its menu (`POST /admin/ai-credentials/keys/{membership_id}/retry`), which puts the row back to `pending` at zero attempts. The likeliest first-run failure is a project with no spend limit on it; the admin fixes it in a minute and needs a way to say "try again" that does something.

It is deliberately **not** folded into "re-add the member": re-adding an existing member is a no-op, and making it a requeue instead would mean any PATCH that merely renames the record quietly resets every durable failure it touches (reconcile passes the current membership list as the desired one). `last_error` is deliberately kept across the requeue — until the retry succeeds, why it failed last time is still the most useful thing anyone can read there.

### Account lifecycle

A minted key has exactly one holder, so an account change that takes away that person's access must take away the key. A shared key has many holders and is **untouched** by all of this — that is not an oversight, it is the difference the two modes exist to express.

| Event | Minted memberships | Shared memberships |
|-------|-------------------|--------------------|
| **Deactivated** (`is_active` → false) | Child credential deleted, membership moved to `suspended`, provider revoke scheduled | Untouched. The child row simply stops being reachable with the account |
| **Reactivated** | `suspended` → `pending`; a **fresh** key is minted | Untouched |
| **Deleted** | Provider handles snapshotted *before* the delete, revoke scheduled *after* it commits | Untouched (the rows go with the cascade) |
| **Removed from the record** | Child deleted, membership row deleted, revoke scheduled | Child deleted, membership row deleted |

Three details worth keeping:

- **Deactivation is wired at two `is_active` call sites**, and the second is the one a reader would not think to look for: the admin `PATCH /users/{id}` `is_active` transition, and **the re-invite path** — re-inviting an existing account with `is_active: false` flips the flag on an existing user. Without that wiring the account would be disabled and its provider key still live. **Deletion is deliberately not a third site**: `delete_user` / `delete_user_me` take the deletion path (`on_account_deleted`) instead, because a deleted account's membership rows — and the provider handles on them — are gone the moment the delete commits, so they are snapshotted before it and revoked after.
- **Snapshot before the delete, schedule after it.** Both deletion routes are a bare `session.delete(user)` relying on database-level `ON DELETE`, so the membership rows — and with them the provider handles — are gone the moment the delete commits. Returning the requests rather than scheduling them means the provider is only contacted if the deletion actually commits.
- **Whether a member holds a key is one predicate, not a status list.** `holds_provider_key(membership)` — literally "is `external_key_ref` set" — is asked by the admin-secret usage count, the deletion path **and** the deactivation path, in its Python form and its SQL twin `holds_provider_key_clause()`.

  Deactivation used to skip `failed` memberships instead, on the premise that a terminal failure holds no key. **Two paths reach the attempt ceiling with a live `external_key_ref`**, so that premise was wrong and the key survived the deactivation. A terminal failure now keeps its status, its error and its attempt count — the durable record an administrator still has to act on is not blanked by an account action that has nothing to do with it — **while its key is revoked**.

**Deactivation never fails because a provider record could not be tidied.** If the suspend step raises, the session is repaired, a warning is logged, and the account is still deactivated. The same net covers reactivation and the pre-deletion snapshot.

**A key that could not be deleted is recorded, not forgotten.** If the child delete is refused by the Tier-2 blast-radius gate (the credential is a published bundle's publisher credential), the member keeps a working key and an `admin.ai_credential.revoke_blocked` event is written. If the *provider* call fails, `admin.ai_credential.revoke_failed` carries the external ref — that event **is** the durable record, because the membership row that held the handles is gone by then.

### A managed child credential cannot be shared — in either mode

`share_credential` refuses **any** child of a managed record: *"This key was provisioned for one person and is withdrawn with their account, so it cannot be shared."* The predicate is `ai_credentials_service.is_shareable(credential)`, and it is one line — `not credential.is_admin_managed`. Two things about that line are deliberate.

**It says *never*, not "not if minted".** The guard used to refuse a minted child and allow a `shared`-mode one, on the reasoning that a shared key has many holders anyway. That distinction described a difference in *how the row dies*, not in whether it does:

- A **minted** child is revoked at the provider when its holder is deactivated or removed from the record.
- A **`shared`**-mode child is *deleted by reconcile* when its holder is removed from the record.

Either way the `AICredentialShare` rows cascade with it (`ON DELETE CASCADE`) and the sharees lose access with no event in their feed and no way to see why. Same failure, different cause of death.

**It reads the column, not the parent link.** `managed_credential_id` is `ON DELETE SET NULL`, and an orphaned child — one whose parent record was force-deleted — is a documented, tolerated state. A guard that began by looking up the parent (`if managed_credential_id is None: return False`) therefore answered "shareable" for precisely the rows most likely to be in a strange state. `is_admin_managed` survives the orphaning, so it is what the predicate asks. One component's tolerated orphan must not become another component's silent assumption.

**The refusal has its own exception type.** `AICredentialNotShareableError` subclasses `HTTPException` (so any future route path still answers `400` without a mapping layer) and carries the credential id. The type matters because its one caller — bundle publisher wiring — catches `HTTPException` broadly so a transient hiccup cannot abort an install. A bare `400` was therefore indistinguishable from "the row vanished": a permanent policy refusal was logged as a transient warning and then reported to the publisher as `publisher_credential_unshared` — telling them to fix a thing policy will never let them fix.

There is still no user-facing route into `share_credential`; AI-credential shares are materialised only from bundle publisher wiring. The guard **is** covered by tests now — bundle-shaped, through the install path that actually calls it, and parametrized over both `minted` and `shared`, because treating one mode as the interesting one is what produced the earlier gap.

#### What this changes for bundles — a deliberate behaviour change

A bundle wired to a **`shared`-mode managed child** now degrades to **"user provides"** instead of handing every installer a copy of the administrator's key. This is an intentional change, not a bug fix, and it is worth a release note.

Correctness is the first reason: the child row is deleted the moment the admin removes its holder from the record, so every installer would lose access at once. The second reason is the one worth saying outright — **it stops a publisher redistributing an administrator's key beyond the member list the administrator curated.** Wiring a bundle to an admin's key was a way to hand that key to everyone who installs the bundle, past the membership the admin actually chose.

The install does not fail — and that is itself new.

#### The install fallback now actually works

`InstallService._link_publisher_ai_credential`'s comment had always claimed a refused share "lets the bundle fall back to user provides". **The claim was false.** The publisher's credential id was linked to the new environment regardless of whether the share had been created, so `create_environment`'s access check rejected a credential the installer cannot access and **the entire install failed with a `400`** — *"Cannot access the specified conversation AI credential"* — naming neither the bundle nor the reason.

`InstallService._linkable_publisher_ai_credential(session, credential_id, user)` now sits between the bundle FK and the environment. It returns the id, or `None` when the credential cannot reach this installer. On `None`, that mode falls through to the installer's own selection, exactly as the comment always said.

It asks four questions, in this order:

1. **Does the row still exist?** No → `None`.
2. **Is it the installer's own row?** Yes → linkable. The publisher installing their own bundle needs no share, and this is asked before anything that costs a query.
3. **Does the installer already hold an `AICredentialShare`?** Yes → linkable. It is working right now.
4. **Could a share be made?** Answered with the **same `is_shareable` predicate the share path enforces**, rather than by inferring from the absence of a row — "no share exists" is a symptom shared by a transient failure and by a permanent policy refusal, and only one of those should stop the credential being linked on a later reinstall.

**The position of (3) is what the previous pass got wrong.** Widening the refusal to every admin-managed child deleted no existing share rows, so asking the policy before looking for a share silently dropped a credential that a live `AICredentialShare` was still serving — on the next reinstall of a bundle that had been installing fine.

**A vanished row returns `None`, and the reason the old comment gave was false.** It said linking a dangling id "lets the env-side resolver produce the existing 'no credential' path". There is no such path: `agent_environment.conversation_ai_credential_id` is a **foreign key**, so a dangling id is an `IntegrityError` on the environment insert — a failed install, not a graceful fallback.

This is the **fourth** "documented tolerance that was never true" found in this phase — a comment describing a graceful degradation that the code around it did not implement. It is recorded here as a class of defect, not just an instance: a tolerance stated in prose and not exercised by a test is a tolerance nobody has checked.

#### The installer is not told, because there is nothing to tell them

`InstallReadinessGate` briefly gained a fourth `GateMissingReason`, `publisher_credential_unshareable`. It has since been **removed**, and an admin-managed publisher credential is now skipped by the gate whether or not a share exists.

**Three facts live here, and collapsing them is what made the old message false.** For an admin-managed publisher credential:

1. **the share exists** — a row reaches this installer;
2. **it still works** — the installer can use the credential today;
3. **it will not be created again**, for this or any other installer, because the credential is admin-managed.

Only (3) is about policy. An earlier order asked (3) first and reported it as though it were (1) and (2), telling an installer whose agent was running that their publisher's credential "cannot be shared". The gate's list is *what the install is missing*, and a credential the installer can already use is not missing — so the gate reports **nothing at all**. Fact (3) is told to the publisher, at publish time, where somebody can act on it: see [Told at publish time](#told-at-publish-time-publish_notices).

**With no share, the same conclusion holds, and this is the part that took a second pass to get right.** `InstallService._linkable_publisher_ai_credential` returns `None` for exactly this credential, so the environment never links it and resolves the installer's own AI credential instead: the agent has a working key and the install is not missing anything. Reporting it produced a `publisher_broken` verdict that refused **every inbound message** on an install that ran fine — and no installer action could clear it, because the scan reads a field on the *bundle*. The reason was removed rather than reworded.

The trade this accepts: an installer is silently running on their own model rather than the one the bundle was designed around. That is the same position as every `provided_by="user"` spec, and it is the publisher's to disclose.

**Leaving existing shares in place is an accepted trade, recorded as accepted.** The decision was: *leave them, surface them, revoke nothing, migrate nothing* — revoking or migrating would break installs that work today. Its weakness was named when the decision was taken: **a publisher who never opens the publish flow again leaves such a share live indefinitely.** That is the cost of not breaking working installs, not an oversight to be tidied up later, and the code records it the same way (`install_readiness_gate.py:257`).

The bespoke copy it carried in both renderers is gone with it. What remains under `publisher_broken` is a credential that is genuinely missing or genuinely unshared — both of which the publisher can actually act on, so the generic "restore access" wording is now true wherever it appears:

- Chat / MCP / A2A reply: *"This bundle is wired to an AI credential that was provisioned for one person and cannot be shared. The publisher needs to point the bundle at a shareable credential; in the meantime you can supply your own from the agent's Credentials tab."*
- `SetupNeededBanner` title: **"Publisher credentials cannot be shared"**, with body copy that names the credential and points at the same two routes out.

**`GateMissingReason` moved to `backend/app/models/bundles/catalog.py`.** It had been spelled out in the gate service *and* again in the response model, and adding a fourth reason made the gate produce a value the response model rejected — a validation error at the boundary between two copies of one list, surfacing as a **500**. One list, declared once, imported by the other. See [Agent Bundles — tech](../../agents/agent_bundles/agent_bundles_tech.md#installreadinessgate-phase-4).

#### Told at publish time (`publish_notices`)

**The gate told the wrong person.** It reported an unshareable publisher credential to the *installer*, on their setup screen, after they had installed something that did not work the way its publisher believed. The person who wired the credential and the person who was told about it are different people at different moments, and the one who could act on it never saw it. That is why the installer-side report was removed outright rather than reworded — there was no wording that made it actionable for the reader it reached.

`PublishService.publisher_ai_credential_notices(session, bundle)` says the same fact to the other half of that pair, at the only moment they are looking. `POST /agents/{id}/publish` returns it as `publish_notices: list[str]` on the revision, and the Bundle tab renders it as a **dismissible persistent callout** headed *"What people who install this will get"* — deliberately not a toast, because a publisher needs this in their release notes and a message that vanishes in four seconds is not where you tell them.

It says what *will happen* rather than naming a policy, and reads the same `is_shareable` predicate the share path enforces — never a second opinion about which credentials are redistributable. Three shapes:

| Situation | What the publisher is told |
|-----------|---------------------------|
| The FK names a row that no longer exists | That mode falls back to the installer's own key; pick a credential you own, or tell installers to add their own |
| An admin-managed credential, no share anywhere | Names the credential, says installers will supply their own key for that mode, and ends with the two actions |
| An admin-managed credential that **already has** shares | The same, plus: the people it was already shared with keep using it, but that share is no longer sanctioned and will not be recreated — act before it matters |
| A shareable credential | Nothing. Empty is the normal answer |

**Every sentence carries an action**, and that is the point rather than a nicety. A surface that only states a fact is a slower version of doing nothing, and doing nothing is the named weakness of leaving existing shares alone. So each notice ends with the two things a publisher can actually do: tell installers to add their own key, **or ask an administrator to provision the credential to them directly** — which keeps the administrator's member list the authority on who holds the key, instead of routing an admin key around it.

`AgentBundleRevisionPublic.publish_notices` is **required with no default**, so a second construction site cannot quietly ship a revision with none; the marshaller (`_revision_to_public`) takes it as an optional keyword and defaults to empty, because a listing has no "just published" moment.

### User experience after provisioning

The user sees the credential in **Settings → AI Credentials** with `is_admin_managed: true` in the API projection. The row renders a **"Managed" badge** (shield icon, tooltip "Managed by your administrator — you can use it and set it as default, but it can't be edited or deleted here"), and the **Edit and Delete buttons are not rendered** for it (the "Set as default" star stays). The backend enforces the read-only guard regardless (`403` on edit/delete/re-key), so the UI gating is defense-in-depth, not the sole protection.

- User **can**: use the credential in environments, set it as their default, see it in model-override selectors.
- User **cannot**: update the name/key/base_url, change the model field, or delete it. Attempts return `403 "This credential is managed by your administrator and cannot be modified."`.

Because each child row is owned by the user, **web environment creation resolves it automatically** via the existing default-resolution and credential-linking pipelines — no extra steps.

#### While a key is being made for them

A minted member holds no credential until the mint lands, so there is nothing in the credential list to see. **Settings → AI Credentials** therefore renders a separate block above the list, fed by `GET /api/v1/ai-credentials/provisioning`:

- *"Your administrator is setting this key up for you. It appears here as a credential once it is ready."* — while the mint is `pending` or `minting`. The page polls every 10 seconds while anything is in flight, and refreshes the credential list when the last one settles.
- *"This key could not be set up. Ask your administrator to look at it — they can start it again from the AI Credentials admin page."* — when it is `failed`.

This is deliberately a **separate list** rather than a placeholder row in the credential list, because every credential in that list is usable and an entry with no key would break the one invariant every consumer of that table relies on. It is a **server** projection rather than a second list the browser folds into the first: the status is stated once, by the side that owns it. Only the keyless statuses are ever projected (`pending`, `minting`, `failed`) — `provisioned` and `not_applicable` are already in the credential list, and appearing in both is how one thing starts looking like two. `suspended` is absent because it only exists on a deactivated account, and a deactivated account has nobody looking at this screen.

#### The dashboard "paste an API key" wall

The dashboard used to ask a two-valued question — does this account have an Anthropic key? — and put up a blocking key-entry wall when the answer was no. Minting adds a case that question cannot express: **a person for whom a key is being created right now holds no credential**, so the boolean says "no key" and the wall goes up in front of somebody who is about to be handed one. Replacing the boolean was only half the fix; the other half was noticing that the *question itself* was scoped to one provider (below).

`GET /users/me/ai-credentials-status` now carries `api_key_onboarding_state`, with three values:

| State | Meaning | Dashboard |
|-------|---------|-----------|
| `has_key` | The account holds a **default** AI credential — of any provider | No wall |
| `preparing` | No default, but a key is in flight for this person | **No wall**, an explaining banner, and the page polls every 10s |
| `needs_key` | No default and nothing coming | Wall on first load; an inline banner afterwards. Polls |

**Both halves are provider-agnostic.** `has_key` is `ai_credentials_service.owner_ids_with_a_default(...)` — is there an `AICredential` row owned by this person with `is_default` set — with no type filter at all.

It was Anthropic-specific for one pass, on the reasoning that the wall asks for an Anthropic key and nothing else will dismiss it. That reasoning made `preparing → has_key` **unreachable**: a mint lands, the person now holds a perfectly good default OpenAI credential, `has_key` still says no, and they are walled anyway — the exact failure the three-valued state was introduced to prevent. Every `preparing` resolved to `needs_key` on success.

**The code fact that settled it: an agent environment can run on a non-Anthropic credential alone.** The compatibility gate is **per-SDK, not per-provider** — `SDK_TO_CREDENTIAL_TYPE` maps `opencode/openai` → `openai`, `opencode/google` → `google`, and so on, and `SDK_CREDENTIAL_COMPATIBILITY` lists four credential types under the `opencode` engine. Nothing requires an Anthropic key to exist. More pointedly, `_apply_sdk_defaults` will set a member's **default SDK** to `opencode/openai` to match a minted OpenAI child. Treating Anthropic as the only key that counts contradicted what the provisioning path itself does.

**"Has a credential" is not "has a default".** The state asks about a *default*, because that is what environment creation resolves through. `set_as_default` on a managed record defaults to `False`, so an administrator can create a perfectly good credential for somebody and leave them on the wall with that exact credential listed in their settings. Two consequences follow:

- The user-facing banner says "Add one in Settings, **or make an existing credential your default**".
- **The admin's invite success message is derived from the same predicate.** `ManagedAICredentialMember` carries `api_key_onboarding_state`, computed by the same `api_key_onboarding_states` call, and the invite success card renders the server's answer for that person: "Key added." for `has_key`, a note that the key is still being created for `preparing`, and **"Key added — not their default."** for `needs_key`, with copy explaining that nothing will pick it up yet. Previously the admin was told "Key added" flatly while the user faced the wall — with that credential in their settings.

The obvious alternative — let the browser fetch the memberships too and suppress the wall itself — is the one thing this must not be. That makes the client a second implementation of a policy the server already owns, and the two answers diverge the first time either side changes. `has_anthropic_api_key` stays on the projection for its other readers; it is **not** the same fact as `has_key` any more, and nothing should treat it as one.

**The field is required, with no default — and that is the phase's closing lesson.** `UserPublicWithAICredentials.api_key_onboarding_state` used to default to `needs_key`. A default there looks like a convenience and is not one: it makes the field **optional in the generated client**, which forces every browser reader to write `?? "needs_key"` — and that fallback *is* the client-side policy this state exists to forbid. It is also silently wrong in the direction that costs the most. The browser cannot tell *"the server has not answered yet"* from *"the server said `needs_key`"*, so a query that is pending, paused (offline) or errored resolves to a confident `needs_key` — and this was the one field a **full-page wall** depended on, in front of somebody who has a key, behind a "Skip for now" that writes a permanent flag. Making it required leaves `undefined` meaning exactly one thing: the server has not answered. The dashboard reads `credentialsStatus?.api_key_onboarding_state` with no `??`, latches the wall on `credentialsStatus !== undefined`, and renders no banner at all while the answer is missing.

#### The wall does not take the page away after it has loaded

The full-page key-entry form is **latched to first render**: if a wall was not warranted on the first render that had a real answer, it is never taken again for that mount. Without the latch, the polling above could replace the entire dashboard ten seconds into a session — discarding a draft message, attached files and the selected agent. That is data loss regardless of whether the state that flipped was computed correctly, and correct states flip: an admin deleting a credential and a minted key being revoked both do it.

Everything the wall would have said is said afterwards without taking the page away, as an inline banner at the top of the dashboard:

- `preparing` — *"Your AI access is being set up. An administrator is creating an API key for your account. This page updates on its own when it is ready — there is nothing for you to do."* `preparing` never gets the full-page wall at all, and it previously rendered **nothing whatsoever**: that person saw a dashboard where nothing worked and no explanation of why.
- `needs_key`, post-load — *"No AI credential yet."* with the Settings route and the make-it-default hint.

And **`needs_key` polls too** now, not just `preparing`. An administrator adding a key by hand from the invite screen changes this person's state without them doing anything, exactly as a landing mint does; before this, the invite happy path needed a manual page reload to leave the wall.

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

### Auto-Provisioning at Account Creation

A **Provider** hands its key out **by role**, so a company key is already wired on the new employee's dashboard the first time they sign in — and reaches Cinna Desktop through `GET /external/account-config` with no further step.

**How the grant happens.** Every `User` row on the platform is built in one place, `UserService.create_account` (see [Auth](../auth/auth.md#the-account-creation-chokepoint)). After the account row is committed, that function calls `AccountProvisioningService.on_account_created`, which **scans Providers** — every `ai_provider` row whose `auto_provision_roles` contains the new account's role — and grants each one through the credential it owns, using the same `add_members` path the admin UI uses. Every arrival path inherits it: password signup, Google first login, admin-created users, externally-arriving channel senders, the local-dev `/private/users/` helper, and the first-superuser seed.

The scan reads Providers rather than credentials because that is where the rule now lives. A **manual** managed credential is therefore never granted automatically: it carries no roles, and nothing looks at it. The service does not query `ai_provider` itself — it asks `AIProvidersService.auto_provision_targets`, which hands back Provider-and-credential pairs, so the isolation of the provider table survives the account-creation path continuing to auto-provision.

**Account creation never fails because provisioning failed.** A Provider whose key cannot be decrypted, a database error, a blast-radius exception — none of them can cost the person their account. Each Provider is attempted in isolation; a failure is recorded as a skipped entry, written to the new owner's security feed as a medium-severity `admin.ai_credential.auto_provision_failed` event, and logged at warning. The account still lands, and the admin sees the affected user again on the next "Apply to existing users" preview.

**That guarantee is structural, not incidental.** It only holds if every handler that returns on a failed attempt repairs the session *before* it does anything else — a statement-level failure (a query that errors, a lock timeout, a dropped connection) leaves the database transaction aborted while the ORM still believes the session is usable, so the failure surfaces later, inside unrelated code, as a commit that cannot run. One shared repair helper is therefore called from every such handler on this path: the per-Provider guard at account creation, the account-creation belt-and-braces guard around it, the per-user guard in the member-add pass, and the reconcile Remove and Update passes.

**A failure event means what it says.** Post-create default wiring runs *after* the child credential row is committed, so a failure there costs the member nothing: they keep the credential, the failure is logged, and no `auto_provision_failed` event is written. An account holder never sees a security-feed entry claiming they were not given a credential they in fact hold.

**No vendor is contacted on this path.** A `fixed_key` grant is a database write. A `minted` grant writes a `pending` membership row and stops — the key is created by a later converge tick, not by the signup request. Nothing in the provisioning path talks to OpenAI, Anthropic or a Google admin API, because this code runs inline on the signup and OAuth-callback request, where a vendor timeout would become a failed login.

**Rules worth stating outright:**

- **Creation-time only.** A role change on an existing account does **not** re-run provisioning. Promoting someone to `agent-developer` must not silently hand them a company API key as a side effect; "Apply to existing users" is the explicit path for that.
- **Deactivated accounts are skipped without an event.** An admin creating an inactive account is a normal act, not a failure: no child is created and **no failure event is written** — a medium-severity row per Provider, in the feed of an account that has done nothing, is noise about an outcome that was never in doubt. The *report* is not silent: on the explicit (invitation) path it carries one `user_inactive` skip per Provider the admin ticked, so the wizard can tell that apart from an admin who ticked none. "Apply to existing users" covers the account once it is activated.
- **`admin` is a valid auto-provision role.** An instance where every employee is an administrator is a normal small-team shape.
- **Auto-provisioning without SDK defaults is allowed** — it is configuration 2 above. A Provider with `auto_provision_roles` and `set_user_sdk_defaults=false` adds the credential and touches no default, and any number of them may coexist.
- **A Provider that owns no credential yields no target.** It is dropped from the scan, and — read through the explicit path — reads back as a `provider_not_found` skip. That silence on the automatic path is exactly why deleting a provider-owned credential is refused.
- **The grant is system-initiated.** The child is stamped with the credential's managing admin; the audit event is scoped to the **new owner** (it is their feed the grant belongs in) with `details.actor = "system"` alongside the origin, the Provider id and the credential id. No key material is ever recorded.
- **Automatic provisioning never takes a default somebody already holds** — see [One owner per default slot](#one-owner-per-default-slot-the-two-conflict-rules).

### Provisioning an Invited Account

An invitation grants the **Providers the administrator ticked in the wizard**, and it does so through the *same* service, the same body, and the same failure semantics as every automatic arrival — not through a second path.

The wizard lists **key sources**, which is why its section is labelled *AI providers* and each item shows the Provider's name with its kind and type beside it. A **manual** managed credential is not grantable at invite time: it carries no rule, and the wizard is asking which rules to apply to this person now.

`AccountProvisioningService` has two entry points and one body. `on_account_created` is the automatic one (every Provider whose `auto_provision_roles` contains the new account's role); `provision_explicit(session, user, origin, *, provider_ids, actor)` is the invitation wizard's. They share `_provision` and the outer never-fail net, deliberately, because the moment they are two implementations an invited `agent-user` and a Google-arriving `agent-user` can end up with different key sets, different audit events and different failure semantics — and nothing reports the difference.

What routing through the service (rather than looping `add_members` in the invite route) inherits, all of which a hand-rolled loop would reimplement badly and silently:

- the never-fail net — an invitation must not fail because an admin pasted an expired key last month;
- `restore_session` before any post-failure logging, which is what stops a `PendingRollbackError` escaping past every net;
- the `ProvisioningReport` skip list and the per-Provider `SecurityEvent` written into the **invited** account's feed;
- the `not user.is_active` short-circuit, so inviting a deactivated account grants nothing and writes no skip events.

Three details specific to the explicit list:

- **`None` is not `[]`.** `provider_ids=None` means *grant exactly what this role would have been granted automatically*, evaluated by the same predicate `_provision` uses; `[]` means the admin deliberately unticked everything; a list means exactly those and **not** the automatic set as well. The wizard sends `None` when its provider list failed to load or is still in flight, because "not stated" is the honest answer there and sending `[]` would silently grant nothing.
- **A ticked id that no longer names a grantable Provider is reported, not dropped.** The wizard's list can go stale against a concurrent deletion, and "you asked for four keys and got three" is only visible if the fourth says why. That produces the one skip reason the automatic path can never emit: **`provider_not_found`**. It has a second producer worth knowing about — a Provider that exists but owns no credential to grant through reads back the same way, which is the explicit path surfacing the silence the automatic scan keeps.
- **A grant that lands on an occupied default slot is disclosed, not failed.** See [One owner per default slot](#one-owner-per-default-slot-the-two-conflict-rules).

The skip vocabulary is `user_not_found`, `user_inactive`, `provider_not_found`, `provision_failed` and `add_members_failed`, keyed by `provider_id`. `managed_credential_not_found` is **gone** — nothing produces it, and the wizard's reason→copy map dropped the key with it, because a dead entry in a copy map is how the next reader concludes the old vocabulary is still live.

`actor` is the acting superuser and is **required** here — unlike the automatic path, there is an admin in this story, and the grant is attributed to them in both `add_members` and the audit event's `details.actor`. That also keeps the architecture test's "one attributable grant" rule green: `actor=None` remains sanctioned in exactly one place, inside this service.

The wizard's pre-ticked set is derived from the Provider's `auto_provision_roles` — the same field `_provision` filters on — so what an invited account starts with matches what a self-registering account of the same role would have got.

### Handing off to add a key — the success screen's onward link

The wizard's *provisioning step* still has no key-entry control, and that remains an ordering fact rather than a preference: the account row is created when the wizard is **submitted**, so at that step there is no user id to attach a key to and no `target_user_ids` to send.

What phase 5 added is the affordance **after** the account exists; a later redesign (the invite success screen's own guideline pass) moved it from a nested dialog into a plain onward link, per the "a success screen links onward" composition rule. The invite success panel (`InviteSuccessPanel`) shows one `Button variant="link"` reading **"Add an AI key for `<email>`"**, navigating to `/admin/ai-credentials?newCredentialFor=<user.id>&label=<display name>`. The AI Credentials route reads those two search params, opens the ordinary `ManagedCredentialDialog` in create mode **controlled**, pre-seeded with the new account as its only target and a suggested name from the label, then strips both params from the URL — the same `?new=1` latch idiom `credential/$credentialId.tsx` uses, so a refresh or a Back does not reopen it.

On success the AI Credentials page derives a one-line result from the created record's own member row (`api_key_onboarding_state` — the same field the person's own paste-a-key wall reads), not from "a row was created": `has_key` → "Key added."; `preparing` → "The key is being created now."; `needs_key` → an amber alert, "Key added — not their default," since `set_as_default` defaults off and a perfectly good credential can still leave its owner walled.

Two things this deliberately is not:

- **Not a second credential-creation surface.** It is the same dialog, the same route, the same reconcile. The AI Credentials page passes it `initialTargets` / `nameSubject` / `onCreated`; it re-implements nothing.
- **Not shown for a deactivated account.** The link is hidden entirely when the newly-touched account is inactive — there would be nobody to give a key to.

This is where the "paste a key for one person" path lands, and it is a first-class one: for a type whose administration API does not create keys it is *the* way an administrator hands somebody their own key. What it creates is a **manual** managed credential — one person, one pasted key, no rule — which is exactly the shape that surface is for.

### One owner per default slot (the two conflict rules)

`User.default_ai_credential_<mode>_id` holds exactly one credential. There are **two different questions** about that, they are asked at two different times, and they have two different answers.

#### Write time — two Providers claiming the same slot: refused

If two Providers both auto-provision to the same role **and** both wire that role's accounts' SDK default for the same mode, an account gets whichever was provisioned second — silently, and differently per user if the row order ever changes. So the second **configuration** is refused when it is saved.

A configuration claims a `(role, mode)` slot only when it does all three things: auto-provisions to that role, has `set_user_sdk_defaults=true`, and lists that mode in `sdk_default_modes`. **An "extra key" Provider claims nothing**, so any number of them coexist — which is what lets configuration 2 above sit beside configuration 1.

The rule is **Provider vs Provider**. A managed credential has no auto-provision roles of its own left to fight with.

Connect and edit both return `409` with a structured body naming the other side, so the dialog can render the conflict inline instead of a bare "conflict":

```json
{"code": "auto_provision_conflict",
 "message": "'Company Claude' already sets the building default for auto-provisioned agent-developer accounts.",
 "conflicting_credential_id": "…", "conflicting_credential_name": "Company Claude",
 "role": "agent-developer", "mode": "building"}
```

The two `conflicting_credential_*` keys keep their names on the wire even though what they now name is a Provider; the sentence a person reads says *provider*: *"Only one provider can own a role's default for a mode — drop the role, or turn off that mode, on one of the two."*

**Validate the transition, not the end state.** Only slots a request *newly* claims can raise. A Provider already sitting in a conflicting configuration is not made this request's problem by being touched — renaming it, replacing its key or editing an unrelated field must never `409` on a collision the admin did not introduce. This is the feature's rule, not a local patch here: the same answer was reached for the access policy's lockout validation, and invitation validation inherits it. The edit sheet helps by sending only the fields the admin actually changed; the transition-scoped validator is the backstop under that.

#### Run time — a real person's slot is already occupied: the incumbent wins

The write-time rule guards *configurations*. It says nothing about one Provider meeting one person who already holds a default — a key their own owner pasted, or an earlier Provider's grant.

**Automatic provisioning never steals a default.** If the slot is empty, the grant claims it. If it is held by anything at all, the grant does **not** claim it — and the grant still happens: the person becomes a member and holds a usable key. The incumbent is by construction the earlier one, so a manual credential beats a provider-granted one and, between two manual ones, the first created wins. This is a normal, expected situation and it is *resolved*, never prevented.

**The two deliberate admin acts do overwrite.** "Apply to existing users" and "Set default for all" are somebody pressing a button on purpose, and both take the slot. For a `minted` Provider the intent has to survive the request that expressed it — the grant and the wiring happen on different requests, because the key is created by a later converge pass — so it is persisted on the membership row (`claim_held_default_slots`, migration `45938a69aee7`). Without that, the escape hatch silently stopped overwriting for exactly the Provider kind configuration 1 is built on.

**A declined slot is disclosed rather than hidden.** A grant that lands on an occupied slot produces a `default_slot_skips` entry carrying the Provider and the mode, and the invite success panel renders it beside the successful grant: *"{name} was granted, but it did not become the {Mode} default — the account already had one, and it was kept."* It never sets `provisioning_failed`, never joins `skipped`, and no count on that panel includes it, because the whole risk in this shape is being read as a grant that fell over.

That is an ordinary path, not an exotic one: a re-invite of an account that already exists — an interrupted earlier attempt, or the passwordless account a server channel created for an inbound sender — runs explicit provisioning against somebody who may already hold a default, and **one ticked Provider is enough**. The credential already sitting in the slot is deliberately not carried up: it belongs to the invited person's own configuration and the wizard has no name for it. The Provider and the mode are what an administrator can act on.

### Per-Mode Model Overrides

`model_override_conversation` / `model_override_building` pin a model on the **member's profile** for the modes this record wires. They are separate from `default_model`, which is the credential's own preferred model written onto the child credential row.

**The update contract is three-valued, and the distinction is deliberate:**

| Request value | Meaning |
|---|---|
| omitted / `null` | Leave the stored override alone. The common case — a PATCH that renames the record says nothing about models. |
| `""` (empty string) | **Clear** it back to NULL, and retract it from members who still carry it. |
| a model id | Set it, and write it through to every member who still has this credential in that slot. |

**Claiming a slot resets it; holding a slot only pushes an actual opinion.**

- When a member is **added** and the credential pointer moves onto their new child, the override is written unconditionally for every mode this record claims — including back to NULL when the record has none. That is a reset, not a wipe: whatever override sat in the slot described a *different* credential and may name a model this provider does not serve (an OpenAI model id left pinned in front of a freshly wired Anthropic key). Clearing it falls back to the credential's own `default_model`.
- On an **update** against a slot the child already occupies, the record only writes when it has an opinion. A stored `None` means "no opinion", not "clear theirs" — otherwise an admin renaming a record would silently erase the model every member had chosen.
- A **clear** (`""`) retracts the pin from existing members, but only where the member's pin still equals the value being dropped. A member who picked their own model against this credential keeps it. Without this a clear would be cosmetic: the record shows blank, every member stays pinned, and no admin action could ever remove it.
- The asymmetry is intentional: *setting* an override does overwrite a model the member picked, *clearing* one only retracts this record's own value. The admin's opinion outranks the member's while it exists and defers to it once withdrawn — so a member's own pick, once overwritten by a set, is not restored by the later clear.

**Teardown.** When a member is removed, or the child's default flag is cleared, the override is torn down with the credential pointer it belongs to — otherwise a model chosen for a credential the user no longer holds would be applied to whatever they select next. A mode merely *dropped* from `sdk_default_modes` is not torn down: the record stops managing that mode, it does not un-manage what it already set (the pointer has always behaved this way, and the pair is left consistent on purpose).

### Apply to Existing Users

`auto_provision_roles` fires at account *creation*, so switching it on does nothing for the people already on the instance. `POST /admin/ai-providers/{id}/apply-to-existing?dry_run=` is the explicit action that closes that gap, and being explicit is the point — the admin sees the count before it happens.

It lives on the **Provider**, because the roles do. The managed-credential surface has no apply action at all: for a provider-owned record it would have run the identical reconcile through a second door, and for a manual record — which can never carry roles — it always answered `candidate_count: 0`, so it was cut rather than kept as an honest no-op. That is the same call this feature makes about rotating a `minted` Provider: a `400`, not an accepted action that does nothing.

- **Desired set** = every **active** account whose role is in the Provider's `auto_provision_roles`, minus the current members. Add-only: nobody loses a credential, and existing members are untouched.
- **`dry_run=true`** writes nothing and emits no audit events (there is nothing to audit about a question). It returns `candidate_count`, the `candidates[]` list (user id, email, full name, role) and `defaults_overwrite_count`.
- **A real run** returns the same reconcile shape as create/PATCH (`added`, `skipped`, …) plus `dry_run: false`, `candidate_count` and `defaults_overwrite_count`; `candidates` is empty.
- **`defaults_overwrite_count`** is how many candidates **lose something** to the grant — counted once per person, however many slots they lose, because the question the admin is asking is "how many people does this disturb", not "how many columns move". It covers **both** of the independent default axes the grant writes:
  - `set_user_sdk_defaults` — a mode this policy claims already holds a credential pointer **or** a model the member pinned (claiming a slot resets both, so either one alone is a loss).
  - `set_as_default` — the candidate already holds a default credential of this type, which the grant demotes.

  It is zero only when the policy wires **no** defaults at all. "12 users will receive this credential" is only half the story when 9 of them lose the default they chose, so the confirm dialog quotes both numbers.
- **This action is one of the two that deliberately overwrite.** Automatic provisioning declines an occupied slot; this does not, which is exactly why the preview has to say what it costs before it runs.
- **What the count deliberately leaves out.** `default_sdk_<mode>` — the engine string — is never NULL, so counting it would make the number equal `candidate_count` for every policy that claims a mode and the number would stop meaning anything. That is why the zero case is worded narrowly ("nobody has picked a credential or a model for these slots yet") rather than as the broader "nothing is replaced": an OpenAI policy claiming the conversation slot *does* move everyone's engine, and only the narrower sentence is true.
- **Counting `set_as_default` is not the same as refusing it.** The write-time uniqueness rule ignores `set_as_default` entirely — two Providers may both claim to be their members' default-for-type, and neither is refused (accepted debt, see [Known Gaps](#known-gaps)). The preview does not change that; it only tells the admin what confirming the second one costs. Treating "we count it" and "we forbid it" as the same question is exactly the mistake the original bug was made of: a `set_as_default`-only record previewed as `defaults_overwrite_count = 0` while silently stripping every candidate's own default key.
- **Idempotent.** A second run adds nobody; the candidate set is already empty.
- The preview is a snapshot — nothing locks the candidate set between the dry run and the commit — so the UI says so when the number moved between the two.

### Admin CRUD

Two superuser-only surfaces, and which one an action lives on follows from which record owns the fact.

Through `/admin/ai-providers/` — **the key source and the rule**:
- **Connect** a Provider, which writes it and its one managed credential together and grants any initial members
- **List** / **get** Providers, with `owned_credential_id`, `member_count` and a per-status `key_state_summary`
- **Update** the name, the key's shape, the config and the whole provisioning policy; every policy change re-applies to existing members, and a newly claimed `(role, mode)` slot another Provider owns is a `409`
- **Delete** — `409` carrying the impact (named people) unless `?force=true`
- **Verify** — the connection, and for a `minted` Provider the project's spend cap
- **Replace key** — `fixed_key` only; a `400` on `minted`
- **Apply to existing users** — `?dry_run=true` previews without writing
- **`GET /admin/ai-providers/adapters`** — every **type** the server supports, derived from the adapter registry. See [provider_adapters_tech](provider_adapters_tech.md)

Through `/admin/llm-providers/` — **the credential and its members**:
- **Create** a **manual** managed credential and provision its initial member set. It takes no `auto_provision_roles`, no `provisioning_mode` and no provider pointer; a request still carrying one of those gets a `422` naming the field rather than a `200` that changes nothing
- **List** all records fleet-wide, optionally filtered by `?managed_by_id=` and/or `?target_user_id=`
- **Get** a single record by ID
- **Update** the name/key/base_url/model/default-flags and/or membership; reconcile runs automatically; `?force=` overrides the Tier-2 block on removed members. On a **provider-owned** record every wiring field, plus the key and its shape, is refused with a `400` naming the Provider
- **Delete** the record and its children — `409` (with a `blocked` list) when any child is referenced by a published bundle, unless `?force=true`; and a flat `400` naming the Provider for a provider-owned record
- **Set default for all** — one of the two deliberate acts that overwrite a held slot
- **Per-key verbs** live on their own resource, `/admin/ai-credentials/keys/{membership_id}` — see [The Keys surface](#the-keys-surface). Retry moved there from `/{id}/members/{user_id}/retry`, which is gone

There is no **apply to existing users** here; it is a Provider action.

`ManagedAICredentialPublic` carries `provider_id`, `provider_name` and `is_provider_owned` so a surface can render the source and the read-only state without re-deriving either, and computes `provisioning_mode` and `has_api_key` through the policy resolver so a record and its Provider cannot disagree. It does **not** publish `auto_provision_roles`: the rule has one owner, and a second answer on the wire is how a future surface re-grows it on the wrong entity.

### Admin UI — the AI Credentials page

Reached from **Admin menu → AI Credentials** (`/admin/ai-credentials`). Superuser-gated: non-superusers are redirected to `/` by `beforeLoad`.

The page was called **LLM Providers** and lived at `/admin/llm-providers` until the zero-touch-onboarding rename. That was a **UI rename only** — the backend prefix `/api/v1/admin/llm-providers`, its OpenAPI tag, the generated `AdminLlmProvidersService`, the `MANAGED_CREDENTIALS_QUERY_PREFIX` cache key and the `components/Admin/LlmProviders/` directory are unchanged, and the old route survives as a `beforeLoad` redirect stub so bookmarks and older documentation keep working. Every `/admin/llm-providers/...` **HTTP path** in this document is a backend call and is current.

Two tabs, addressed by the URL hash so an admin can link to one and come back to it: **`#managed-credentials`** (the default) and **`#providers`**. The page also latches `?newCredentialFor=` out of the URL and strips it, and the strip carries the current hash forward — arriving at `#providers` and dismissing a hand-off leaves the admin on `#providers`.

#### Providers tab (`#providers`)

*"Where AI keys come from, and who automatically gets one. A provider holds the key and the rule for handing it out; the vendor it talks to is its **type**."*

One row per Provider, each carrying:

- a **status dot** answering the health question — verified, last check failed, or never verified;
- an icon for the **kind** and a badge spelling it out: **Fixed key** / **Per-user keys**;
- the **name**;
- two facts on the metadata line: the **type label** (read from the adapters endpoint, never from a hardcoded client list) and the rule — *"Automatic for Agent User, Agent Developer"*, or *"Not automatic"*;
- **the wiring flag**, whenever the Provider auto-provisions at all, in one of its two faces. `set_as_default` on → *"Becomes each recipient's default key"* (and, with SDK defaults wired, which modes). `set_as_default` off → *"Extra key — granted in addition to what they already have, and displaces nobody's default."* This is where configuration 2 becomes visible at a glance;
- a warning flag when any member's key failed to be created;
- an info affordance carrying the member count, the per-status key summary, the organisation and project ids when set (so two Providers into one organisation are visible rather than silent), and the last verification result or error verbatim.

**Verify** is the one inline action. The row menu carries **Edit provider**, **Apply to existing users**, **Replace key** — rendered only for a `fixed_key` Provider — and **Delete provider**. The list polls while any Provider has a key in flight and stops when it settles.

**Verify's result is a toast**, and the spend-limit sentence is only shown for a `minted` Provider: for a `fixed_key` Provider the question does not apply, and rendering it as "no limit" would be the false negative the check exists to prevent. **The limit itself is never displayed.** A number on screen in Cinna reads as a Cinna setting whether or not it was stored, and nothing here writes one.

#### Connect provider (three steps)

**1 Source · 2 Key · 3 Who gets one.**

1. **Source** — pick the **type** first, from pills fed by `GET /admin/ai-providers/adapters`, because the shape of everything after it depends on the answer. Then the **kind**, as two tiles: *"Fixed key — everyone shares one key you paste"* and *"Per-user keys — each person gets their own, created in your provider account. This is what makes per-user spend tracking possible."* For a type whose adapter cannot mint, the second tile is unavailable **and the reason is written next to it in words**, composed from the adapter's own label.
2. **Key** — the secret, labelled and explained by kind: *"The model API key every member will hold a copy of"* for a fixed key, *"The organisation key Cinna uses to create each person's key. It is never handed to a member and never shown again after saving"* for per-user keys. `Base URL` and `Model` appear when the adapter requires them; the organisation and project inputs are rendered from the adapter's own `admin_config_schema`. For a `minted` Provider, one line says where the spend limit lives: set it in the vendor's console, Cinna checks it is in force and refuses to create keys in an uncapped project, and never sets or stores one. **There is no spend-limit input anywhere.**
3. **Who gets one** — the auto-provision roles, with the creation-time rule stated (*"Changing someone's role later never grants or revokes a key — use Apply to existing users for accounts that already exist"*); **Make it their default key**, whose helper carries the "extra key" configuration in full; **Wire it into their SDK defaults** and the modes, each ticked mode carrying its own model override; and an Advanced disclosure for the default and available models. A slot-conflict `409` renders here, under the roles, naming the other Provider.

#### Edit provider

A right-hand sheet with three sections — **Details**, **Key**, **Provisioning policy** — and explicit Save/Cancel. The type and kind are stated as prose rather than shown as disabled controls: *"{Type} · {Kind label}. A provider's type and kind are fixed when it is connected."* Replace key is not here; it is on the row menu, and the Key section says so, along with the reason a `minted` Provider has no key to replace.

At the head of the policy section: **"Changes here re-apply to everyone who already holds this key."** Model and override changes are written through to every member; clearing an override unpins the members it pinned. The submit is a diff against the snapshot the sheet opened with, so an untouched field is omitted and the three-state `model_override_*` contract survives.

#### Delete provider

Selecting Delete fires the unforced delete as a probe. Nobody holding a key means there was nothing to confirm and it just goes. Otherwise the `409`'s impact fills the confirmation, which names **the people who lose a key** rather than counting them, and says whether the keys are revoked at the vendor: for a `minted` Provider *"{n} of those keys were created at {Type} and will be revoked there. They stop working immediately."*; for a `fixed_key` one, that the copies here are deleted and the key stays valid at the vendor until the admin removes it there. The destructive button names what it does — *Delete provider and revoke {n} keys* — and a second `409` (`ai_provider_members_not_removed`) keeps the dialog open and offers **Try again**, because the retry completes the removal.

#### Keys tab (`#keys`)

The fleet-wide table of **keys**, one row each — see [The Keys surface](#the-keys-surface) for what a row is. Six columns: **Key** (the holder's email, with the credential's name beneath it; for a shared key the credential's name, with "Shared with N people" beneath), **Status**, **Type**, **Source** (the Provider's name, linking to the Providers tab, or **Manual**), **Created**, and the row menu. A `Default` badge marks a key that is its holder's default; the vendor handles ride in the row's detail glyph rather than a column, since a minted service account is now named `<email> (<membership id>)` at the provider and the email in the first column is already the match.

Above the table: a search box that matches the holder's email or name **or** the credential's name, plus Status, Kind and Provider filters. Every one of them is a server-side query parameter, and the page resets to the first when any changes. A status filter excludes shared rows by construction — a shared key has no provisioning lifecycle.

A per-user row's menu is the four verbs. A **shared** row's menu is the *record's* menu, unchanged — Members, Set default for all, Edit and Delete — because a shared row is a record, and the record is fetched when the menu is reached for rather than shipped with every page.

The dialog behind the record menu has **two branches**:

- **A manual record** keeps every control it has today: name, type, API key, base URL/model, default and available models, target users, "set as default", "set user SDK defaults" and its modes. What it no longer has is a Key-source radio, a Provider organisation select, or an Auto-provision block — a manual record is always shared, can never point at a Provider, and carries no rule. In place of the Auto-provision block, one line says where the rule went: *"Who automatically receives a key is set on a provider, under **Providers**."*
- **A provider-owned record** is a different composition rather than the same form with disabled controls. An alert names the owner — *"Managed by the provider {name}"* — says the key, the models and the SDK wiring are set there, and adds *"Deleting this credential means deleting its provider."* What the Provider decided is rendered as **text**, not as greyed inputs, so nothing depends on disabled styling to carry meaning. The one editable thing is **who holds it**, and that is the reason the dialog is still a form. Its row menu shows **Members** and **Set default for all**; Delete is not rendered (it is a `400` naming the Provider) and Apply to existing users is not rendered (it lives on the Provider).

### Admin UI — Server Configuration → Access

The same rule seen from the other end. An admin setting up the front door asks "what does a new Agent Developer get?", and answering that by opening every key source in turn is the wrong shape. So the Access tab carries a **Company AI providers** card (`CompanyAiCredentialsCard`, a half-width card alongside the [Access Policy](../server_configuration/access_policy.md) cards, not nested inside any of them) listing Providers one per row, each with a **User · Dev · Admin** role toggle group; a segment on means new accounts of that role receive a key from that Provider.

- A toggle is one `PATCH /admin/ai-providers/{id}` carrying `auto_provision_roles` and nothing else — the field exists on `AIProviderUpdate`, which is the point of the card moving with the rule. The card never invents state of its own and shares the Providers query key, so a change made on either surface shows on both.
- Each row's metadata line is the **type** and the **kind** — "OpenAI · Per-user keys" — which is what tells a shared-key source from a one-key-each source at a glance.
- Capped at **5 rows** (Providers that already grant something first, then alphabetical) with a footer link reading "Manage AI credentials" when everything fits, or "Show all (N) on AI Credentials" once it does not — one link to `/admin/ai-credentials#providers`, not two.
- A cell whose tick would be refused carries an advisory **Conflict** badge, computed client-side from the loaded list. It is a hint, not a gate — the list can be stale, the click still goes to the server, and a real `409` renders as an alert under the list, titled *"Another provider owns that default"*.
- Empty state links to `/admin/ai-credentials#providers`.
- The card's own description states the creation-time rule: "Changing a role later never grants or revokes a key — use \"Apply to existing users\" on the AI Credentials page for accounts that already exist."

### Security audit

All events contain counts/IDs but **never** key bytes.

**Provider acts** are recorded against the acting superuser, one event per act, from `/admin/ai-providers`:

- `admin.ai_provider.created`
- `admin.ai_provider.updated`
- `admin.ai_provider.deleted` — carries `forced` **and what the force overrode**: the member count and the minted-key count. This is the last place anyone can learn how many people lost a key and how many keys were destroyed at the vendor
- `admin.ai_provider.verified`
- `admin.ai_provider.rotated` — the fact of a rotation, never the before or after value
- `admin.ai_provider.applied_to_existing` — real run only; a dry run emits nothing, because there is nothing to audit about a question

None of them carries key material, a vendor response, or anything but ids and coarse reason codes.

**Credential and member acts** are recorded from `/admin/llm-providers`, and the per-child events are scoped to the child's owner so each person has their own audit trail.

On create (`POST /`):
- One `admin.ai_credential.provision` event **per added child** (scoped to the child owner)
- One `admin.managed_ai_credential.create` event scoped to the admin (batch summary: `added_count`, `removed_count`, `updated_count`, `skipped_count`, `blocked_count`)

On update (`PATCH /{id}`):
- `admin.ai_credential.provision` per newly-added child
- `admin.ai_credential.delete` per removed child
- `admin.ai_credential.update` per actually-mutated child (no event on unchanged members)
- `admin.managed_ai_credential.update` scoped to the admin

On set-default (`POST /{id}/set-default`):
- `admin.ai_credential.set_default` per member child (scoped to each owner)

On automatic grant at account creation (no acting admin, so **not** emitted by an admin route):
- `admin.ai_credential.auto_provision` — severity `low`, scoped to the **new owner**, `details = {provider_id, managed_credential_id, target_user_id, origin, role, managed_by_id, actor: "system", provisioning_status}`, plus `child_credential_id` when one exists. A minted grant has a membership and no credential yet, so the id is omitted rather than stringified from a `None` into a field every other row of the feed reads as an id
- `admin.ai_credential.auto_provision_failed` — severity `medium`, scoped to the new owner, `details = {provider_id, managed_credential_id, target_user_id, origin, role, reason, actor: "system"}`. `reason` is one of `add_members_failed`, `user_not_found`, `user_inactive`, `provision_failed`, `provider_not_found`

On delete (`DELETE /{id}`):
- `admin.ai_credential.delete` per removed child
- `admin.managed_ai_credential.delete` scoped to the admin

The old `admin.ai_credential.provision_batch` event type from the pre-parent model is gone; it was replaced by the parent-level `admin.managed_ai_credential.*` events. The `admin.provider_admin_credential.*` family went with the route that emitted it; `admin.ai_provider.*` is where a provider act is recorded now.

#### Minting events

External key refs **are** recorded in event details, deliberately — they are what makes a key nameable after the fact. Key material never is.

| Event | Severity | Feed | When |
|-------|----------|------|------|
| `admin.ai_credential.mint_requested` | medium | the member | A member is added to a minted record (instead of `provision`, which names a credential), and on an explicit Retry (carrying `retry_of: <last_error>`) |
| `admin.ai_credential.minted` | medium | the member | A key was minted and their child row created. Carries `child_credential_id` and `external_key_ref` |
| `admin.ai_credential.mint_failed` | medium | the member | One failed attempt. Carries `reason`, `attempts`, and `terminal` — so a transient backoff and a give-up are distinguishable in the feed |
| `admin.ai_credential.revoked` | medium | see below | A key was destroyed at the provider |
| `admin.ai_credential.revoke_failed` | **high** | see below | A key we could not destroy. **This event is the durable record** — the membership row that carried the handles is gone by then, so without it the key would be live at the provider with nothing anywhere naming it |
| `admin.ai_credential.revoke_blocked` | **high** | the member | A key deliberately left live because its child credential could not be deleted (`in_use_bundle`, `delete_failed`) |
| `admin.ai_provider.{created,updated,deleted,verified,rotated,applied_to_existing}` | medium | the acting admin | Provider administration. `rotated` records that a key was replaced, never what with; `deleted` records `forced` **plus the member and minted-key counts it overrode**, because a forced delete of a `minted` Provider destroys keys at the vendor and its audit row is the last place anyone can learn how many |

**Whose feed a revoke lands in is not always the key holder's.** `security_event.user_id` is NOT NULL with a foreign key to `user`. On the account-deletion path the holder no longer exists by the time the provider is called, so the event goes to the administrator who deleted the account — which is also where it is useful. When a user deletes their **own** account, subject and actor are the same and both are gone: there is genuinely no feed, and the revoke is logged rather than audited. Inventing an owner for it would be worse than saying so.

`admin.ai_credential.provision` on a minted record's added member is replaced by `mint_requested` rather than emitted with a null credential id: a feed entry saying a credential was provisioned, naming no credential, is worse than one saying a mint was asked for. `set_default` events likewise skip members with no key — the parent flag is set and applies when their key arrives.

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
- **The Provider owns the key and the rule; the credential owns the members.** A Provider owns exactly one managed credential, written with it in one transaction. Membership is a row, one per `(parent, user)` — not derived from the children. A `fixed_key` Provider holds the key so adding a member or replacing the key never requires re-entering the secret; a `minted` Provider holds an administration secret and no model key at all.
- **A manual managed credential has no Provider and keeps every control it has today** — its own key, its own wiring policy, its own hand-curated members. What it cannot do is auto-provision.
- **The wiring policy has exactly one read path.** A provider-owned record's same-named columns are shadowed, not mirrored, and `provisioning_policy.resolve_policy` is the only legal way to read any of those facts. Two copies of a policy is how a stale one gets displayed as the active one.
- **Every `AICredential` row that exists is usable.** No placeholder child, no empty key, no "preparing" credential. The membership row carries the pre-key state, so every consumer of the credentials table — the discovery cron, the account-config bundle, the environment credential bag — needs no new filter and cannot grow one by accident.
- **Membership existence and the provisioning lifecycle have different owners.** `ManagedAICredentialsService` creates and deletes membership rows and sets their *initial* status; `KeyProvisioningService` owns every `pending → minting → provisioned | failed` transition plus `suspended`, and never creates or deletes a row. Two questions, two owners, so they cannot disagree.
- **Statuses are explicit and never inferred from NULL** — including the terminal ones. The one absence that means something is the row itself.
- **Every write an attempt makes after taking the claim is conditional on it — the success path and every failure path alike.** On a lost claim the minted key is revoked rather than stored; if that revoke fails, the durable high-severity event carries the external ref. A minted key must never end up live-but-unrecorded. The failure path is conditional for a reason of its own: `_record_failure` writes `pending` with a retry time, so an unconditional version would put a row a concurrent deactivation had settled `suspended` back into the converge queue.
- **The publisher is told at publish time what installers will get.** `PublishService.publisher_ai_credential_notices` returns finished sentences on the publish response (`publish_notices`), rendered as a dismissible persistent callout on the Bundle tab. Every sentence names an action: installers supply their own key, or an administrator provisions the credential to them directly. A live existing share is called out as still working but no longer sanctioned.
- **A blocked removal states its own reason sentence.** `ManagedReconcileBlock.message` is server-authored, required, drawn from one table, and rendered verbatim everywhere — so a mint-in-flight block can never be reported as a bundle conflict, whose remedy (`force=true`) would strand a live key.
- **A failure is durable.** A `failed` membership is never deleted to tidy up, and the only way out of it is an explicit Retry.
- **No vendor call on a request path.** Adding a member to a `minted` Provider's credential records an intent; a converge pass mints against it.
- **Minting is refused into a project with no spend limit at all**, and the limit is set or verified at setup — never applied after a key exists. An `inactive` enforcement status is a capped project that has not yet hit its threshold, not an uncapped one.
- **No child of a managed record is ever shared, in either mode.** `share_credential` refuses when `ai_credentials_service.is_shareable(credential)` is false — the predicate is the plain `is_admin_managed` column, so an orphaned child (parent force-deleted, FK `SET NULL`) is refused too. The refusal is the typed `AICredentialNotShareableError`, a permanent policy answer its one caller can tell apart from the transient failures it swallows. A bundle wired to such a credential degrades to "user provides"; it does not fail the install, and it is not reported to the installer at all, because the environment resolves the installer's own AI credential in its place and nothing is missing. **Where a share already exists** both the install path and the gate look for an `AICredentialShare` to the installer before asking the policy, so an install running on a share made before the widening keeps working. Those rows are deliberately left alone — the accepted price is that such a share can stay live indefinitely if its publisher never publishes again, and the publisher is told so, with the action to take, at publish time.
- **Only a type whose administration API creates keys may be a `minted` Provider.** That is one term, `supports_minting`, published by `GET /admin/ai-providers/adapters` and enforced server-side by `AIProvidersService._validate_shape`; it is a fact about the vendor, not about what the instance has connected. A `minted` Provider also needs the project it mints into, because the project's spend limit is what bounds every key it creates.
- **Kind is fixed once a Provider is connected.** Replacing the key on a `minted` Provider is a `400`, not a silent no-op.
- **A Provider cannot be deleted while anybody holds a key from it** — `409` carrying the impact as named people, unless forced. A forced delete revokes every minted key at the vendor **first**, while the administration secret still exists, then removes the child credentials, the memberships, the credential and the Provider. Getting that order wrong surfaces as a database error rather than as an orphan, because the credential's foreign key is `ON DELETE RESTRICT`.
- **A provider-owned managed credential cannot be deleted on its own** — `400` naming the Provider. Deleting it would leave the Provider auto-provisioning with nothing to grant through, and the scan drops such a Provider silently.
- **A provider-owned record refuses every write to a shadowed field, plus the key and its shape** — `400` naming the Provider, listing every offending field rather than one per round trip. `base_url` and `model` are refused for a reason of their own: on a `fixed_key` Provider they live in the encrypted envelope each new member's credential is built from, so an edit here reached today's members and was silently reverted for everyone added afterwards.
- **Deactivation revokes minted keys and keeps the membership; reactivation mints a fresh one.** Shared credentials are untouched by either. Which memberships have a key to revoke is `holds_provider_key` — one predicate shared with deletion and with the Provider delete impact — never a status list, because a terminal `failed` row can still hold a live `external_key_ref`.
- **Children are ordinary per-user rows.** Each child is an `AICredential` with `is_admin_managed=True` and a `managed_credential_id` FK to the parent. All existing per-user plumbing (default resolution, environment creation, agent-log visibility) applies unchanged.
- **Invalid targets skipped, not errored.** Unknown or inactive users appear in `skipped`; the successfully reconciled members proceed.
- **Reconcile is idempotent.** Supplying the same desired set with unchanged parent scalars produces empty added/removed/updated lists and emits no child-level audit events.
- **One-default-per-type invariant preserved.** If `set_as_default=True` and the target user already has a default of that type, the existing default is unset (existing `set_default` behavior). The user ends up with exactly one default.
- **Blast-radius gate on member removal.** Removing a member whose child is referenced by a published bundle is blocked (appended to `blocked`) unless `force=True`. The parent and blocked members stay intact on a non-forced delete.
- **Admin deletion preserves children.** When the managing admin's user account is deleted, both `ManagedAICredential.managed_by_id` and `AICredential.managed_by_id` are set to `NULL` (SET NULL FK) — the parent record stays fleet-manageable by any superuser, and each user keeps their child credential.
- **Out-of-band parent deletion degrades gracefully.** If a parent row is ever deleted outside the service path, its children's `managed_credential_id` becomes NULL (SET NULL FK) — they degrade to plain `is_admin_managed` orphans rather than disappearing.
- **`set_default` is open to the owner.** Setting an admin-managed credential as one's default is a read-only use, not a modification. The owner may do this freely through the user-facing CRUD.
- **Auto-provisioning is creation-time only.** A role change on an existing account never grants or revokes a key; "Apply to existing users" is the explicit action.
- **Provisioning failure never fails account creation.** Recorded as a skip + a medium-severity security event in the new owner's feed + a warning log; the account stands. A failure in the *post-create* default wiring is a different case: the child row is already committed, so the member is retained, the failure is logged only, and no skip or failure event is written.
- **One Provider per `(role, mode)` default slot**, validated on connect and edit (`409`), scoped to the slots the request newly claims. The rule is Provider vs Provider; a managed credential has no roles of its own to fight with.
- **At run time the incumbent wins.** Automatic provisioning never takes a default slot somebody already holds — it grants the credential and declines the slot, and the declined slot is disclosed on the invite response beside the successful grant. "Apply to existing users" and "Set default for all" are deliberate admin acts and **do** overwrite; for a `minted` Provider that intent is persisted on the membership row, because the grant and the wiring happen on different requests.
- **`auto_provision_roles` is validated; `sdk_default_modes` is not.** An unknown role is a `400`. An unknown mode string saves with a `200` and then wires nothing, forever — see [Known Gaps](#known-gaps).
- **Emptying `auto_provision_roles` is not a revoke.** Existing members keep their credential; the Provider simply stops being granted to new accounts.
- **An invited account provisions through the same service.** The wizard submits **`provider_ids`** to `AccountProvisioningService.provision_explicit`, not to a route-level `add_members` loop, so it inherits the never-fail net, the session-repair discipline, the skip reporting into the invited account's own feed, and the deactivated-account short-circuit. Its pre-ticked set is derived from the *same* `auto_provision_roles` predicate the automatic path uses, so an invited account and a self-registered one of the same role start with the same keys. A manual managed credential is not grantable there — the wizard lists key sources.
- **The invite wizard's provisioning step still has no key-entry control**, because the account does not exist at that step; the affordance lives on the **success screen** instead, as a link onward that reuses the ordinary create dialog — see [Handing off to add a key](#handing-off-to-add-a-key--the-success-screens-onward-link).

### Native account-config

- Owner-only scope (shares excluded — see security boundary above).
- Empty credentials → `200` with `providers: []`.
- An undecryptable credential row is skipped (warning logged, call does not fail) so a single corrupt row cannot block login bootstrap.

---

## Architecture

```
Superuser (web)
   │
   │  POST /api/v1/admin/ai-providers/            [connect a Provider]
   ▼
AIProvidersService.create(session, AIProviderCreate, actor)
   ├── _validate_shape(kind, type, secret, config, base_url, model)
   │     minted  → adapter.supports_minting and config.project_id
   │     fixed   → ai_credentials_service._validate_credential_data (one rule, shared)
   ├── _validate_auto_provision_uniqueness(...)   → AIProviderConflictError → 409
   ├── encrypt the secret  (fixed_key → the credential envelope; minted → bare)
   ├── INSERT AIProvider  +  INSERT ManagedAICredential(provider_id=…)
   │     one flush to order the inserts, one commit — the 1:1 invariant is held here
   └── reconcile(desired=target_user_ids)  → the same path every later grant takes

SecurityEvent("admin.ai_provider.created")  [scoped to the acting admin]


Superuser (web)
   │
   │  PATCH /api/v1/admin/ai-providers/{id}       [edit the rule or the policy]
   ▼
AIProvidersService.update(session, id, AIProviderUpdate, actor)
   ├── snapshot previously_claimed + previous_overrides BEFORE writing a field
   ├── _validate_auto_provision_uniqueness(newly claimed only) → 409
   ├── write the columns; re-encrypt the fixed_key envelope on a base_url/model edit
   └── _reapply_to_members(...) through ManagedAICredentialsService
         reconcile / _update_child_fields / _sync_model_overrides(cleared_overrides)
         → default_model + available_models written through to every child
         → model_override_* written through, and a cleared one unpins the members it pinned

SecurityEvent("admin.ai_provider.updated") + the per-child credential events


Superuser (web)
   │
   │  POST /api/v1/admin/llm-providers/           [a manual managed credential]
   ▼
ManagedAICredentialsService.create(session, admin, ManagedAICredentialCreate)
   ├── _validate_provisioning_shape → a manual record is always shared, so it must hold a key
   ├── Validate + Fernet-encrypt the key (reuses _validate_credential_data)
   ├── INSERT ManagedAICredential (no provider_id) — managed_by_id=admin.id
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
   ├── resolve_policy(parent) → provider-owned? then _refuse_shadowed_writes → 400
   ├── Apply changed scalars to parent; rotate encrypted_data if api_key supplied
   └── reconcile(desired=new_target_user_ids_or_current, apply_fields=True, key_rotated=...)
         ├── Add pass   → add new members
         ├── Remove pass → delete children (Tier-2 gate → blocked list if force=False)
         └── Update pass → _update_child_fields diff:
               name / base_url / model / key (if rotated) → update_credential(admin_override)
               set_as_default toggle → set_default / _clear_child_default
               [no change → not in updated list, no event emitted]

SecurityEvent per mutated child + admin.managed_ai_credential.update


Any account-creation path (signup / google / invite / admin / external / seed)
   │
   ▼
UserService.create_account(session, *, email, origin, ...)   ← the ONLY place a User row is built
   │  commit + refresh
   ▼
AccountProvisioningService.on_account_created(session, user, origin)   [never raises]
   ├── targets = AIProvidersService.auto_provision_targets(session, role=user.role)
   │       → (provider, its managed credential) pairs; a provider owning no
   │         credential yields no target
   ├── per target, inside its own try:
   │     managed_ai_credentials_service.add_members(parent=…, user_ids=[user.id],
   │                                                actor=None, claim_held_slots=False)
   │       fixed_key → _add_child() → _stamp_child() → [optional] set_default /
   │                                                    _apply_sdk_defaults
   │                                                    (declines an occupied slot)
   │       minted    → membership row in `pending`; no vendor call on this path
   │     SecurityEvent("admin.ai_credential.auto_provision", low)     [per grant]
   └── on failure: rollback → warn → skipped entry
         SecurityEvent("admin.ai_credential.auto_provision_failed", medium)


Superuser (web)
   │
   │  POST /api/v1/admin/ai-providers/{id}/apply-to-existing?dry_run=
   ▼
AIProvidersService.apply_to_existing(session, provider_id, actor, dry_run=…)
   └── ManagedAICredentialsService.apply_to_existing(..., claim_held_slots=True)
         ├── candidates = active users with role ∈ provider.auto_provision_roles,
         │                minus current members
         ├── defaults_overwrite_count = candidates who lose a default (either axis),
         │                              counted once each
         ├── dry_run → return candidates, write nothing, emit nothing
         └── else add_members(actor=admin) → SecurityEvent per child
               + admin.ai_provider.applied_to_existing  [scoped to admin]


Superuser (web)
   │
   │  DELETE /api/v1/admin/ai-providers/{id}?force=
   ▼
AIProvidersService.delete(session, id, actor, force=…)          [async]
   ├── delete_impact(...) → 409 AIProviderDeleteImpact unless force  (named people)
   ├── await _revoke_member_keys(...)   ← FIRST, while the admin secret still exists
   ├── ManagedAICredentialsService.delete(force=True, allow_provider_owned=True)
   │      → child credentials, memberships, then the credential itself
   ├── if the credential survived → 409 {code: "ai_provider_members_not_removed"}
   └── session.delete(provider)

SecurityEvent("admin.ai_provider.deleted", details={forced, member_count, minted_key_count})


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

## Known Gaps

Deliberate, each carrying a docstring at the code that owns it. Recorded here so the next phase does not rediscover them.

1. **`default_sdk_<mode>` dangles when a member is removed, and it is not harmless.** Deleting the child clears `default_ai_credential_<mode>_id` (an `ondelete="SET NULL"` database fact) and the service now clears the model override beside it — but the *engine* string is left as-is. Nothing re-derives it: `EnvironmentService` and `ExternalAccountConfigService` both read `user.default_sdk_<mode>` verbatim, so a removed member's next environment is composed on the engine of a credential they no longer hold. The dangle predates this phase and exists on paths this service does not own; `_clear_child_default` and the reconcile Remove pass therefore knowingly disagree about this one field. A fix wants its own change and its own tests.
2. **`sdk_default_modes` is never validated.** `["banana"]` saves with a `200` and wires nothing, forever, with no error anywhere — while the sibling `auto_provision_roles` returns `400` for exactly the same mistake. The two fields disagree. The conflict validator filters unknown modes out, so two records sharing an invalid mode are correctly not treated as a collision, but that is damage control, not validation.
3. **Model-id normalisation is inconsistent across three fields on the same row.** `_normalize_default_model` strips `vendor/` prefixes, and both model overrides go through it — so an OpenRouter-shaped `anthropic/claude-3.5-sonnet` override is stored as `claude-3.5-sonnet`. The same row stores `parent.model` unstripped. Three model fields, two normalisation rules.
4. **Clearing `base_url` is still not expressible.** The dialog's new dirty gate correctly detects that the admin emptied the field and sends `base_url: null` — and `update()` reads `None` as "leave unchanged", so the edit is dropped silently. (Clearing it through to children was never possible either; for the only type that uses it, `openai_compatible`, it is required.)
5. **The uniqueness rule does not cover `set_as_default`.** Two auto-provisioning Providers that both set themselves as their members' default-for-type are the same last-writer-wins class as the SDK-default slot, and are not refused. This is a gap in *validation only*, and it must not be read as a gap in disclosure: `_count_default_overwrites` does count this axis, so the apply-to-existing preview tells the admin exactly how many people the second record demotes. The admin is warned, and then allowed.
6. **A blocked removal can strand a retracted override.** If one PATCH both clears a model override and fails to remove a member whose child is in use, that member keeps the retracted pin and no later request can express the retraction again — the transition is gone. Narrow enough to accept; the alternative is unpinning someone who did not actually leave.
7. **One branch is deliberately untested rather than covered by a test that asserts nothing.** `detect_anthropic_credential_type`'s `adapter is None` fallback carries `# pragma: no cover - the registry always serves it`. Reaching it needs the registry to fail to serve a shipped provider, and a test that arranges that is testing the arrangement. It is documented as unreachable — a claim a reader can check — rather than green-washed. *(The sharing guard that used to sit at this number **is** covered now: bundle-shaped, through the install path that calls it, parametrized over `minted` and `shared`.)*
8. **One service account can leak on a crash inside the mint window.** Between the provider creating the service account and the process committing anything, no row names it. The create is a single call that returns the secret, so it cannot be split without asking for a response that carries no secret. The orphan is visible in the provider's own console. See [The provisioning lifecycle](#the-provisioning-lifecycle).
9. **Key scopes are not requested.** Deliberate — the scope vocabulary is not authoritative in any artifact this repo holds, and a guessed list would hard-fail the create call the day the provider changed it. `scopes_requested` / `scopes_granted` are recorded on every external ref so hardening is a change to two values rather than a rewrite. `model_permissions` is a known lever, deliberately not pulled. Nothing in this feature may be described as narrowing a minted key's permissions until one of these is actually enforced.
10. **A rejected Google key surfaces as a 500 rather than `invalid_key`.** `google-genai` raises `google.genai.errors.ClientError`, which is not an `httpx` exception, so it bypasses the auth-error mapping. **Symptoms: a 500 from Test Connection, and `"ClientError"` as the reason in the discovery cron.** Pre-existing — the old single dispatch had the identical hole — and preserved deliberately; see [provider_adapters_tech](provider_adapters_tech.md#known-gap--a-vendor-sdks-own-error-bypasses-the-invalid_key-mapping).
11. **Two per-provider tables in `environment_lifecycle.py` are outside the architecture test's reach**, because they are keyed on the enum's *string values*. Covering them would mean guessing, and the guesses would need an allowlist — and the empty allowlist is the whole basis of that test's design. One of the two is genuinely provider-shaped and belongs on an adapter one day; the other is dispatch over local variables and does not. See [provider_adapters_tech](provider_adapters_tech.md#the-known-hole-tables-keyed-on-the-enums-string-values).
12. **The list-response envelope is inconsistent.** `GET /admin/ai-providers/adapters` returns `{data, count}`; `GET /admin/ai-providers/`, `GET /admin/llm-providers/` and `GET /ai-credentials/provisioning` return bare arrays. Each is consumed correctly, but "how does an admin list respond" has two answers.
14. **RESOLVED — an unshareable publisher credential no longer gates an install.** It used to. When a bundle's publisher AI credential was not shareable *and* no existing share reached the installer, two components disagreed: the environment fell back to the installer's own default (`InstallService._linkable_publisher_ai_credential` returns `None`, so the install ran), while `InstallReadinessGate._scan_ai_credentials` emitted `publisher_credential_unshareable` — a `publisher_broken` verdict short-circuiting **every inbound message** on chat, MCP, A2A and session webhooks before any LLM call. It was **unclearable by the installer**, because the scan reads `bundle.publisher_ai_credential_*_id` and no installer action changes a bundle FK, and the copy named the wrong screen: the agent's Credentials tab has no AI-credential control at all.

    Escalated as a product question — *should an unshareable publisher credential block the install at all, given the environment has already fallen back to a working credential?* — and answered **no**. The gate now skips the credential, the `publisher_credential_unshareable` reason is removed from `GateMissingReason`, and the bespoke copy is gone from both renderers. Scenarios P/Q/R in `agents_bundles_install_readiness_test.py` pin the outcome, with R the load-bearing one: it proves the gate still reports a *shareable* credential that simply is not shared, so P and Q are "we skip the admin-managed ones deliberately" rather than "we stopped scanning".

13. **`ManagedCredentialDialog` still states per-type required-field rules in its own zod schema**, in its `superRefine` branch, and in its `showBaseUrl` / `showModel` conditions, while `requires_base_url` / `requires_model` arrive from the adapters endpoint. The Provider wizard does read them from the adapter; the managed-credential dialog does not. Separately, `PROVIDER_TYPE_OPTIONS` still holds a hardcoded type list, and that one is **deliberate**: driving that picker from the adapters endpoint would re-expose MiniMax, which the UI hides on purpose. The Providers tab reads its type labels from the adapters endpoint precisely so it does not inherit that omission.

15. **An agent in an already-running environment will call the deleted routes and get a 404.** The platform-knowledge agent reads its API reference from `/app/core`, which is a **per-environment template copy, not baked into the image**. An environment that was created before this change keeps its stale copy until it is rebuilt, so it still describes `/admin/provider-admin-credentials` and `/admin/provider-adapters` — routes that no longer exist — and it will do so *after* the feature looks finished, because nothing about shipping the backend touches a running container's file. Rebuilding the environment is what updates it.

16. **`expiry_notification_date` can be set and moved but not removed.** See the [tech doc's known limitation](admin_ai_credential_provisioning_tech.md#expiry_notification_date-can-be-set-but-not-cleared) — it is inherited from the managed-credential update shape, not introduced by the Provider surface, and a fix belongs on both surfaces or neither.

---

## Integration Points

- **[Auth](../auth/auth.md)** — `UserService.create_account` is the chokepoint that triggers auto-provisioning, and `AccountOrigin` is what records which arrival path a grant came from.
- **[User Roles](../user_roles/user_roles.md)** — `User.role` is the selector: a Provider is granted when the new account's role appears in its `auto_provision_roles`. The role itself comes from `ServerConfig.default_user_role` unless the caller passes one explicitly.
- **[Access Policy](../server_configuration/access_policy.md)** — hosts the *Company AI providers* card beside its **New user defaults** card, and gates which origins may create an account at all.
- **[AI Credentials](ai_credentials.md)** — the reused core service: `create_credential`, `set_default`, `update_credential`, `delete_credential`, `decrypt_credential`, `resolve_default_credential_for_sdk`. `ManagedAICredentialsService` delegates every per-child operation with `user_id = owner_id` so all per-user invariants (one-default-per-type, profile auto-sync, SDK-default wiring) run for the target user.
- **[AI Credentials Tech](ai_credentials_tech.md)** — `AICredential` model with three managed-credential columns (`is_admin_managed`, `managed_by_id`, `managed_credential_id`); `AICredentialsService.update_credential` / `delete_credential` `admin_override` kwarg; `AICredentialPublic.is_admin_managed` projection.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the `/external/` route namespace that the account-config endpoint extends. The same `ExternalAccountConfigService` sits under `services/external/`.
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the desktop/mobile JWTs with `client_kind` and `external_client_id` claims. The live revocation check in `get_current_user` ensures revoked device tokens are rejected `401` before the native gate runs.
- **[User Roles](../user_roles/user_roles.md)** — `get_current_active_superuser` gates both admin surfaces. Only superusers may connect a Provider or manage a managed credential.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the blast-radius gate on child credential removal (`AICredentialInUseError`, Tier-2, published bundle references) is the same mechanism used by the regular credential deletion guard. It is also what can block a deactivated user's minted key from being deleted, in which case the key is deliberately left live and the block is recorded.
- **[Provider Adapters](provider_adapters_tech.md)** — the registry that declares which **types** can mint, what a per-user-keys Provider of that type needs, and the `KeyProvisioner` contract every vendor call goes through. An adapter describes a type and never a Provider; that file states the rule.
- **[Agent Bundles](../../agents/agent_bundles/agent_bundles.md)** — bundle publisher wiring is the only caller of `share_credential`, and is where the refusal is actually observed. `InstallService._linkable_publisher_ai_credential` keeps the unreachable credential off the new environment so the bundle really does fall back to "user provides" (it previously failed the whole install with a bare `400`), and `InstallReadinessGate` reports `publisher_credential_unshareable` to the installer — but only when no `AICredentialShare` already reaches them; an install running on a pre-existing share is reported as missing nothing. `PublishService.publisher_ai_credential_notices` tells the **publisher** the same fact at publish time, with the action to take. `GateMissingReason` lives in `backend/app/models/bundles/catalog.py`, declared once and imported by the gate.
- **[Status Repair](../../system/status_repair/status_repair.md)** — a sibling consumer of `app.core.db.leader_session`, the single implementation of the single-leader advisory-lock pattern the key-provisioning scheduler also uses.

---

*Last updated: 2026-09-08 — **ai-credential-keys**. The admin surface listed *records*, which for a minted Provider meant one row standing for one key per member, rendered as an unbounded cell of member chips and paginated by the count that never grows. The first tab is now **Keys** (`#keys`): **one row = one real API key** — one per member for a minted record, one for a shared one, because a shared record's N children are copies of a single secret. `/admin/ai-credentials/keys` is the new resource, addressed by **membership id**, with four per-key verbs: retry (moved here from `/admin/llm-providers/{id}/members/{user_id}/retry`, which is gone), set as their holder's default, rotate (the suspend/resume pair applied to one row, so the key is destroyed and re-minted through the sequence that already carries the never-leave-a-key-live guarantee) and revoke (the same removal a member-set PATCH performs, inheriting the Tier-2 gate and the in-flight-mint refusal). Search, filters and paging are server-side, so the payload no longer grows with headcount. No migration: the membership row already carried everything a key row needs.*

*Previously — 2026-09-07 — **ai-credential-providers**. One record used to be both a credential and its own factory; it is now a **Provider** (the key source plus the rule for who gets one) that owns exactly one **managed AI credential** (the key that exists and who holds it), with manual managed credentials unchanged and provider-less. `/admin/ai-providers` is the provider surface — connect, edit, verify, replace key, apply to existing users, delete with a named-people impact gate — and `GET /admin/ai-providers/adapters` describes the **types** the server supports; `/admin/provider-admin-credentials` and `/admin/provider-adapters` are gone, and so is `POST /admin/llm-providers/{id}/apply-to-existing`. `/admin/llm-providers` keeps its path and now creates manual records only. The wiring policy has one read path (`provisioning_policy.resolve_policy`) because the same-named columns are shadowed rather than mirrored on a provider-owned record. Two conflict rules at two times: provider-vs-provider at write time (409, scoped to newly claimed slots), and **the incumbent wins** at run time, where automatic provisioning declines an occupied default and discloses it while "apply to existing users" and "set default for all" deliberately overwrite. Deleting a provider-owned credential on its own is a 400 naming the Provider, and the forced provider delete no longer leaves that same orphan behind its own gate. The invite wizard submits `provider_ids`, and `managed_credential_not_found` gave way to `provider_not_found`. Migrations `c23d6b59a8f5` then `45938a69aee7`.*

*Previously — 2026-09-06 — known gap 14 resolved: an unshareable publisher credential no longer gates an install. The `publisher_credential_unshareable` reason is removed from `GateMissingReason`, the gate skips the credential whether or not a share exists, and the bespoke copy is gone from both renderers — the environment already resolves the installer's own credential, so nothing is missing.*

*Previously — zero-touch onboarding phase 5, **fifth pass** (review of the fourth pass's fixes): the share lookup now runs **before** `is_shareable` in both `InstallReadinessGate._scan_ai_credentials` and `InstallService._linkable_publisher_ai_credential`, so an installer who already holds a share is reported as missing nothing and keeps the credential on reinstall — three facts (the share exists / it still works / it will not be created again) kept apart instead of collapsed into one false sentence; leaving those share rows in place is an **accepted** trade, with its named weakness (a publisher who never publishes again leaves one live indefinitely) recorded as accepted; `PublishService.publisher_ai_credential_notices` → `publish_notices` on the publish response, rendered as a dismissible persistent callout on the Bundle tab, every sentence carrying an action; `UserPublicWithAICredentials.api_key_onboarding_state` made **required with no default** (a default makes it optional in the generated client, which forces the browser to supply the policy fallback this enum forbids); `_linkable_publisher_ai_credential` returns `None` for a vanished row — the FK made the old "graceful fallback" claim an `IntegrityError`; `ai_credentials_service.has_a_default` deleted (no callers). New [Known Gap 14](#known-gaps): an unshareable publisher credential with no existing share blocks every message on an install that runs, unclearably by the installer, with copy naming the wrong screen — escalated as a product question, deliberately unfixed.*

*Previously — **fourth pass** (whole-feature seam review): the sharing refusal widened from minted-only to every admin-managed child (`is_shareable`, typed `AICredentialNotShareableError`) — so a bundle wired to a `shared`-mode child now degrades to "user provides" rather than redistributing the admin's key; the install fallback made real (`InstallService._linkable_publisher_ai_credential`, which previously failed the whole install with a bare 400); the `publisher_credential_unshareable` gate reason with `GateMissingReason` consolidated into `app/models/bundles/catalog.py`; `ManagedReconcileBlock.message`; `AIKeyOnboardingState` made provider-agnostic (`owner_ids_with_a_default`) with the latched, non-destructive dashboard wall and the invite success message on the same predicate; the mint claim token and revoke-on-lost-claim; `holds_provider_key` as the one key-holding predicate. Phase 5 itself: provider adapter registry, provider admin credentials, per-user key minting (membership as a row, converge scheduler, revocation cascade), the invite success screen's "Add a key for this user" step, and `api_key_onboarding_state`; migration `ed8d6a23f13c`. Phase 4 renamed the admin page to **AI Credentials** (`/admin/ai-credentials`), UI only. Phase 2: auto-provision roles, per-mode model overrides, apply-to-existing, `(role, mode)` slot conflicts; migration `b71863b32aa1`*
