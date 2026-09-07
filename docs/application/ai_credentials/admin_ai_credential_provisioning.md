# Admin-Provisioned AI Credentials + Native Account-Config

## Purpose

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

- **Part A — Admin-provisioned AI credentials.** A superuser creates a single **Managed AI Credential** parent record and assigns it to one or more target users — by hand, and/or **automatically to every account created with a chosen role** (see [Auto-Provisioning at Account Creation](#auto-provisioning-at-account-creation)). The parent record is the canonical source of truth (name, type, default flags, and — in shared mode — the encrypted key). The service reconciles the desired member set into per-user `AICredential` child rows, which participate automatically in all existing per-user plumbing. Users can use their child credential and set it as their default, but cannot edit, delete, or re-key it — those operations return `403`.
- **Part A′ — Per-user key minting.** A record can instead be configured so that **each member gets their own key**, created at the provider through a connected **provider admin credential** and destroyed again when they lose the record. See [Key source: one shared key, or one key each](#key-source-one-shared-key-or-one-key-each). Only OpenAI can do this today; for every other provider a key is **pasted**, which is a normal path and not a fallback.
- **Part B — Native account-config endpoint.** A native-token-gated endpoint (`GET /api/v1/external/account-config`) returns the caller's own usable AI credentials with the *decrypted* API key, so Cinna Desktop and Cinna Mobile can auto-create local "LLM providers" and a suggested chat mode per credential on login, without the user having to copy-paste keys into the native app.

> **Frontend status:** The admin **AI Credentials** section (`/admin/ai-credentials`; called "LLM Providers" at `/admin/llm-providers` until zero-touch-onboarding phase 4, which renamed the **UI only** and left a redirect stub) is **implemented** — superusers can provision, list, filter, edit, set-default, delete, and set an admin-curated model list (default model + available models) from the web UI. The user-facing AI Credentials card renders admin-managed child credentials with a **"Managed" badge** and a read-only **Default model** line when a curated default is set. Phase 5 added a second tab, **Provider keys**, for the provider organisations that per-user minting runs through, and a member-key status column on the managed table. The native-app side (Part B) backend is complete; Cinna Desktop/Mobile provider auto-creation is not yet built.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **Managed AI Credential (parent)** | A `ManagedAICredential` row owned by a superuser. Holds the canonical config (name, type, base_url/model, default flags, provisioning mode) and — in shared mode only — a Fernet-encrypted key. Reconciled into child rows. |
| **Membership row** | A `ManagedAICredentialMembership` row, one per `(parent, user)`. **The only definition of "who is a member."** It carries an explicit `status` and, for a minted key, the provider handles needed to revoke it. Membership used to be *derived* from the children; that derivation is **gone**, not kept alongside. |
| **Child credential** | An ordinary `AICredential` row with `is_admin_managed=True`, `managed_credential_id` pointing at the parent, and `owner_id` set to the target user. Participates in all existing per-user plumbing; read-only through the user-facing CRUD. Now a *consequence* of membership: present when a key exists, absent while one is being minted or after a mint has failed. |
| **Provisioning mode** | `shared` (the default and the historical behaviour — the admin pastes one key and every member holds a copy) or `minted` (each member gets their own key, created at the provider). Fixed at creation; the dialog disables the choice in edit mode. |
| **Provider admin credential** | A `ProviderAdminCredential` row — an instance-wide **administration** secret for one provider organisation, which can create and destroy keys for that whole organisation. Server-scoped (no owner), superuser-only, write-only, and in its own table so no per-credential plumbing can ever reach it. |
| **Reconcile** | The diff-and-converge operation: Add (desired − current) creates a membership row and, in shared mode, a child; Remove (current − desired) deletes the child (Tier-2 blast-radius gated) and then the membership row, scheduling a provider revoke for a minted key; Update (intersection) writes changed parent fields and/or rotated key through to each child that exists. Idempotent — a no-op desired set with unchanged fields produces empty added/removed/updated lists. |
| **Account config** | The native-client bundle: a list of provider descriptors (with decrypted API keys) that Cinna Desktop / Mobile uses to create local LLM providers on login. |
| **Native token gate** | The `account-config` endpoint is only accessible to JWTs whose `client_kind` claim is `"desktop"` or `"mobile"`. Plain web-session JWTs are rejected `403`. |
| **Auto-provision roles** | `ManagedAICredential.auto_provision_roles` — the roles whose **newly created** accounts receive this credential automatically. Empty (the default) means the admin picks members by hand. Read at the account-creation chokepoint, never on a later role change. |
| **Apply to existing users** | The explicit admin action that grants an auto-provisioning record to the accounts that already exist. Add-only; previewed with a dry run before it is committed. |
| **Model override (per mode)** | `model_override_conversation` / `model_override_building` — the model id pinned on a *member's profile* (`User.default_model_override_<mode>`) for the modes this record wires. Distinct from `default_model`, which is written onto the *child credential row*. |
| **Default slot** | One of the two `(role, mode)` positions on a member's profile — `default_ai_credential_conversation_id` / `default_ai_credential_building_id` plus their model override. A slot holds exactly one credential, which is why only one auto-provisioning record may claim it per role. |
| **External key ref** | The provider handles for a minted key (`project_id`, `service_account_id`, `api_key_id`, `scopes_requested`, `scopes_granted`), stored on the membership row. **A key nobody can name is a key nobody can destroy** — this is what makes revocation possible, and it is written *before* the key is stored. |
| **Converge pass** | One tick of the key-provisioning scheduler: pick up membership rows that are due a mint, call the provider, settle each row. Runs every minute, single-leader across workers. |

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

Membership is a **row**: one `ManagedAICredentialMembership` per `(parent, user)`, in `managed_ai_credential_membership`. It is the only definition of "who is a member", and it replaced the previous *derivation* from the children — the derivation is gone rather than kept in parallel, because two definitions of membership is exactly the duplication that produces a member list which is quietly wrong.

There is still no `target_user_ids` column on the parent: the *desired* set is supplied at create/update time and reconciled. The membership row is not a desired set — it is the reconciled state, written by the same pass that creates and deletes the child, and carrying an explicit status that says which of "intended" and "actual" it currently is.

**Why it had to become a row.** Per-user minting separates two facts that used to be the same one. The moment an admin adds somebody to a minted record they *are* a member and they do *not* hold a key — the provider has not been called yet, and the call may fail. A derived model has nowhere to put that: a row that does not exist cannot carry a status, and the alternative (a child credential holding an empty key) breaks the invariant every consumer of the credentials table relies on — **every `AICredential` row that exists is usable**. There is no placeholder credential, no "preparing" state on a credential, and no empty-key sentinel anywhere in this design.

### What the admin can set

| Option | Effect |
|--------|--------|
| `set_as_default = True` | Calls `set_default` for each member, including profile auto-sync (`ai_credentials_encrypted`). A PATCH that changes this flag applies it (or clears it) for every existing member in the Update pass of the reconcile. |
| `set_user_sdk_defaults = True` | Wires each owner's `default_sdk_conversation` / `default_sdk_building` and `default_ai_credential_*_id` to their child, using the same SDK composition as the Add Environment dialog (`claude-code` for Anthropic/MiniMax, `opencode/<provider>` for OpenAI/Google/OpenAI-Compatible). |
| `sdk_default_modes` | Modes to wire: `"conversation"`, `"building"`, or both (default). Modes incompatible with the credential type are silently skipped. |
| `default_model` | Admin-curated preferred model ID (bare concrete id, e.g. `claude-sonnet-4-6`). Reconciled onto each child row. When set and the environment has no per-mode override, this model is used instead of the catalog tier default. `NULL` = use the catalog tier default. |
| `available_models` | Admin-curated list of selectable concrete model IDs reconciled onto each child row. When non-empty, model-picker datalists and the native `suggested_models` field show this list instead of the auto-discovered list. `NULL`/empty on the credential = fall back to `discovered_models`. |
| `auto_provision_roles` | Roles whose newly created accounts receive this credential. `[]` (default) = never granted automatically. Validated against the role enum — an unknown role is a `400`, not a silent drop. |
| `model_override_conversation` / `model_override_building` | Model id pinned on each member's profile for that mode, written whenever this record wires that mode's SDK default. Blank = this record has no opinion and members fall back to the credential's own `default_model`. |

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

### Key source: one shared key, or one key each

Every managed record has a **provisioning mode**, chosen when it is created and fixed afterwards.

| Mode | What the admin supplies | What each member holds | Revoked when they leave? |
|------|------------------------|------------------------|--------------------------|
| `shared` (default) | One API key, pasted | A child credential holding a **copy** of that key | No — one key held by many people must not be destroyed because one holder left. Their child row simply stops being reachable |
| `minted` | No key at all — a **provider admin credential** to mint through | A child credential holding a key created for **them alone** | Yes, at the provider |

A minted parent holds no key of its own (`encrypted_data` is NULL), which is why a rotation request against it is refused with a `400` rather than accepted and ignored: there is nothing there to rotate, and silently accepting the paste is how an admin comes to believe they have rolled a key they have not. Rotating a minted member's key is a per-member mint, not a parent edit.

#### Pasting a key is a normal path

**Anthropic's administration API lists and updates keys; it does not create them** — verified against the API and the SDK. So for an Anthropic record (and every provider but OpenAI today) the administrator pastes a key, and that is the ordinary way those records work. It is not a fallback, not a degraded mode, and not something the product apologises for. The minted option is simply not offered for such a provider, and the dialog says why in the provider's own name: *"Anthropic's administration API does not create keys, so an Anthropic key is pasted here and shared."*

The one thing that is provider-agnostic is **membership**. A shared member and a minted member are both membership rows, both appear in the same member list, and both are reconciled by the same pass.

### Provider admin credentials

Before any key can be minted, a superuser connects a **provider organisation** on **Admin → AI Credentials → Provider keys**. That record holds an administration secret — categorically different from every other secret in this codebase, because it does not grant access to a model, it grants the power to create and destroy keys for a whole provider organisation.

It is kept in its own table for a structural reason, not a stylistic one. Two existing services are correct for the table they read and would both reach an admin secret the day it lived in `ai_credential`: the model-discovery cron selects **every** row and calls the provider with each key, and `GET /external/account-config` hands **every** row a user owns, decrypted, to the desktop client. Sharing, environment linking, bundle publisher wiring and the blast-radius counts are all foreign keys pinned to `ai_credential.id`; the environment credential bag is a fixed slot dict with no slot to pour into. A table that is not `ai_credential` is not reachable from any of them.

What the admin does with the record:

| Action | Effect |
|--------|--------|
| **Connect provider** | Stores the name, provider, encrypted secret and config (project id, optional organisation id, monthly spend limit in **cents**). **No provider call is made** — so a provider outage cannot stop an admin from recording the configuration |
| **Verify** | One press, two answers: *is the secret good* and *is the project capped by an enforcing spend limit*. They fail independently, and an admin who fixes one wants to see the other without a second round trip. The result is stamped on the record (`last_verified_at` / `last_verify_error`) and shown in the table |
| **Apply spend limit** | Sets the configured monthly limit on the project. **A setup action** — see below |
| **Edit / rotate key** | Omitting the secret keeps the stored one, so renaming a record never round-trips a secret |
| **Disconnect** | Refused `409` while managed records still mint through it or keys minted with it are still live. `force` overrides, and the override is audited **with the counts it overrode** |

The secret is write-only: it is accepted on create and update, is never a field of any response, and appears in no projection. The admin-facing projection carries `has_secret: bool` instead — copying `MailServerConfigPublic`, because a projection that carries the value's *slot* is one refactor away from carrying the value.

**Deleting it is gated on what it can still revoke.** Losing the secret does not only stop new keys being minted; it strands every key already minted with it, because an administration call authenticated by this secret is the only way to destroy one. The refusal names two counts, because they mean different things to the person pressing Delete: *managed records that mint through it* is configuration they would have to redo, and *live keys only it can revoke* is keys that would stay live at the provider forever. The server also publishes `delete_blocked` — the **answer** — so the browser does not recompute `(a or b) > 0` and keep answering the old way the day the rule changes.

`created_by_id` is **SET NULL**, never CASCADE. User deletion is a bare cascade, and deleting the superuser who happened to paste the key must not destroy the instance's ability to mint and — worse — to *revoke* every key it has ever minted.

### The spend-limit precondition

**The project's monthly spend limit is set or verified at setup, before any key exists.** It is never applied after minting has started: capping a project that already has live keys in it leaves a window in which an uncapped key is in the world, and under a single project that window does not need to exist at all.

The predicate is `enforcement.status == "enforcing"`, and it is defined in exactly one place (`SpendLimitStatus.is_capped`). A limit that exists but reports `inactive` **is not a cap** — reading the threshold alone, the obvious field, would call it one — and minting into such a project is refused with `project_not_capped`. Verify reports the same thing to the admin: *"The key works, but the project's monthly spend limit is not being enforced. Keys are not created in an uncapped project."*

Enforcement is documented as not instantaneous at the provider: recorded spend can slightly exceed the cap. Nothing here promises a hard stop; it promises a limit.

Amounts are **integer cents** at every layer, with the unit in the field name (`spend_limit_cents`, `threshold_cents`), because an off-by-100 here is a hundred-fold cap.

### What a minted key carries

A minted key carries **write access to the project's API resources**. Its blast radius is bounded by the project's spend limit, which Cinna verifies is enforcing before minting into that project, and by revocation. That is the whole of the claim.

It is deliberately not softened. The provider documents default service-account permissions as read and write of all of the project's API resources; nothing documents a narrower guarantee we could inherit. Three things follow, and they are recorded as decisions rather than left to be rediscovered:

- **We do not request key `scopes` when minting.** The scope vocabulary is an open string array with no enum, and no artifact we hold contains it — the pinned `openai==2.15.0` has no `organization` resource at all — so a guessed list would hard-fail the create call the day the provider changed it. `scopes_requested` and `scopes_granted` are nevertheless recorded on **every** external ref, so a later hardening pass changes two values rather than rewriting the record, and no child ever claims a narrowness it does not have.
- **`model_permissions` was deliberately not used.** A known lever, deliberately not pulled in this pass.
- **The single enforcement lever is therefore the project spend limit.** This is not defence in depth, and it is checked *before* a key exists rather than applied after one does.

### One project, not one per user

Single-project only. A project per user was considered and dropped: projects cannot be deleted at the provider (archive only, and archive is irreversible), the ceiling is around 2,000 per organisation, and archived projects are retained — so project-per-user is a one-way ratchet that buys roughly 2,000 *lifetime* provisions and two permanent failure classes no retry can resolve. Service accounts have no documented ceiling, are genuinely deletable, and per-user cost attribution is available under one project anyway.

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

`failed` is terminal on purpose, but terminal must not mean unreachable. **Admin → AI Credentials → Managed credentials** shows a failed member with the reason and a **Retry** button (`POST /admin/llm-providers/{id}/members/{user_id}/retry`), which puts the row back to `pending` at zero attempts. The likeliest first-run failure is a project whose spend limit is not enforcing; the admin fixes it in a minute and needs a way to say "try again" that does something.

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

A managed credential can be handed out **by role**, so a company key is already wired on the new employee's dashboard the first time they sign in — and reaches Cinna Desktop through `GET /external/account-config` with no further step.

**How the grant happens.** Every `User` row on the platform is built in one place, `UserService.create_account` (see [Auth](../auth/auth.md#the-account-creation-chokepoint)). After the account row is committed, that function calls `AccountProvisioningService.on_account_created`, which selects every managed credential whose `auto_provision_roles` contains the new account's role and grants each one through the same `add_members` path the admin UI uses. Every arrival path inherits it: password signup, Google first login, admin-created users, externally-arriving channel senders, the local-dev `/private/users/` helper, and the first-superuser seed.

**Account creation never fails because provisioning failed.** A parent whose key cannot be decrypted, a database error, a blast-radius exception — none of them can cost the person their account. Each parent is attempted in isolation; a failure is recorded as a skipped entry, written to the new owner's security feed as a medium-severity `admin.ai_credential.auto_provision_failed` event, and logged at warning. The account still lands, and the admin sees the affected user again on the next "Apply to existing users" preview.

**That guarantee is structural, not incidental.** It only holds if every handler that returns on a failed attempt repairs the session *before* it does anything else — a statement-level failure (a query that errors, a lock timeout, a dropped connection) leaves the database transaction aborted while the ORM still believes the session is usable, so the failure surfaces later, inside unrelated code, as a commit that cannot run. One shared repair helper is therefore called from every such handler on this path: the per-parent guard at account creation, the account-creation belt-and-braces guard around it, the per-user guard in the member-add pass, and the reconcile Remove and Update passes.

**A failure event means what it says.** Post-create default wiring runs *after* the child credential row is committed, so a failure there costs the member nothing: they keep the credential, the failure is logged, and no `auto_provision_failed` event is written. An account holder never sees a security-feed entry claiming they were not given a credential they in fact hold.

**No third-party provider is contacted on this path.** Every key handed out here is already in the database. Nothing in the provisioning path talks to OpenAI, Anthropic or a Google admin API — this code runs inline on the signup and OAuth-callback request, where a provider timeout would become a failed login.

**Rules worth stating outright:**

- **Creation-time only.** A role change on an existing account does **not** re-run provisioning. Promoting someone to `agent-developer` must not silently hand them a company API key as a side effect; "Apply to existing users" is the explicit path for that.
- **Deactivated accounts are skipped without an event.** An admin creating an inactive account is a normal act, not a failure: no child is created and **no failure event is written** — a medium-severity row per credential, in the feed of an account that has done nothing, is noise about an outcome that was never in doubt. The *report* is not silent: on the explicit (invitation) path it carries one `user_inactive` skip per credential the admin ticked, so the wizard can tell that apart from an admin who ticked none. "Apply to existing users" covers the account once it is activated.
- **`admin` is a valid auto-provision role.** An instance where every employee is an administrator is a normal small-team shape.
- **Auto-provisioning without SDK defaults is allowed.** A record with `auto_provision_roles` but `set_user_sdk_defaults=false` simply adds the credential and touches no default.
- **The grant is system-initiated.** The child is stamped with the *parent's* managing admin; the audit event is scoped to the **new owner** (it is their feed the grant belongs in) with `details.actor = "system"` alongside the origin and the parent id. No key material is ever recorded.

### Provisioning an Invited Account

An invitation grants the credentials **the administrator ticked in the wizard**, and it does so through the *same* service, the same body, and the same failure semantics as every automatic arrival — not through a second path.

`AccountProvisioningService` has two entry points and one body. `on_account_created` is the automatic one (every managed credential whose `auto_provision_roles` contains the new account's role); `provision_explicit(session, user, origin, *, managed_credential_ids, actor)` is the invitation wizard's. They share `_provision` and the outer never-fail net, deliberately, because the moment they are two implementations an invited `agent-user` and a Google-arriving `agent-user` can end up with different key sets, different audit events and different failure semantics — and nothing reports the difference.

What routing through the service (rather than looping `add_members` in the invite route) inherits, all four of which a hand-rolled loop would reimplement badly and silently:

- the never-fail net — an invitation must not fail because an admin pasted an expired key last month;
- `restore_session` before any post-failure logging, which is what stops a `PendingRollbackError` escaping past every net;
- the `ProvisioningReport` skip list and the per-credential `SecurityEvent` written into the **invited** account's feed;
- the `not user.is_active` short-circuit, so inviting a deactivated account grants nothing and writes no skip events.

Two details specific to the explicit list:

- **`None` is not `[]`.** `managed_credential_ids=None` means *grant exactly what this role would have been granted automatically*, evaluated by the same predicate `_provision` uses; `[]` means the admin deliberately unticked everything. The wizard sends `None` when its credential list failed to load or is still in flight, because "not stated" is the honest answer there and sending `[]` would silently grant nothing.
- **A ticked id that no longer names a record is reported, not dropped.** The wizard's list can go stale against a concurrent deletion, and "you asked for four keys and got three" is only visible if the fourth says why. That produces the one skip reason the automatic path can never emit: `managed_credential_not_found`.

`actor` is the acting superuser and is **required** here — unlike the automatic path, there is an admin in this story, and the grant is attributed to them in both `add_members` and the audit event's `details.actor`. That also keeps the architecture test's "one attributable grant" rule green: `actor=None` remains sanctioned in exactly one place, inside this service.

The wizard's pre-ticked set is derived from `auto_provision_roles` — the same field `_provision` filters on — so what an invited account starts with matches what a self-registering account of the same role would have got.

### Handing off to add a key — the success screen's onward link

The wizard's *provisioning step* still has no key-entry control, and that remains an ordering fact rather than a preference: the account row is created when the wizard is **submitted**, so at that step there is no user id to attach a key to and no `target_user_ids` to send.

What phase 5 added is the affordance **after** the account exists; a later redesign (the invite success screen's own guideline pass) moved it from a nested dialog into a plain onward link, per the "a success screen links onward" composition rule. The invite success panel (`InviteSuccessPanel`) shows one `Button variant="link"` reading **"Add an AI key for `<email>`"**, navigating to `/admin/ai-credentials?newCredentialFor=<user.id>&label=<display name>`. The AI Credentials route reads those two search params, opens the ordinary `ManagedCredentialDialog` in create mode **controlled**, pre-seeded with the new account as its only target and a suggested name from the label, then strips both params from the URL — the same `?new=1` latch idiom `credential/$credentialId.tsx` uses, so a refresh or a Back does not reopen it.

On success the AI Credentials page derives a one-line result from the created record's own member row (`api_key_onboarding_state` — the same field the person's own paste-a-key wall reads), not from "a row was created": `has_key` → "Key added."; `preparing` → "The key is being created now."; `needs_key` → an amber alert, "Key added — not their default," since `set_as_default` defaults off and a perfectly good credential can still leave its owner walled.

Two things this deliberately is not:

- **Not a second credential-creation surface.** It is the same dialog, the same route, the same reconcile. The AI Credentials page passes it `initialTargets` / `nameSubject` / `onCreated`; it re-implements nothing.
- **Not shown for a deactivated account.** The link is hidden entirely when the newly-touched account is inactive — there would be nobody to give a key to.

This is where the "paste a key for one person" path lands, and it is a first-class one: for every provider whose administration API does not create keys — which is every provider but OpenAI today — it is *the* way an administrator hands somebody their own key.

### One Owner per Default Slot (the 409 conflict)

`User.default_ai_credential_<mode>_id` holds exactly one credential. If two managed records both auto-provision to the same role **and** both wire that role's accounts' SDK default for the same mode, the account gets whichever was provisioned second — silently, and differently per user if the row order ever changes. So the second configuration is refused at write time instead of resolved at grant time.

A configuration claims a `(role, mode)` slot only when it does all three things: auto-provisions to that role, has `set_user_sdk_defaults=true`, and lists that mode in `sdk_default_modes`. Any number of auto-provisioning records that do not wire SDK defaults may coexist.

Create and PATCH both return `409` with a structured body naming the other side, so the dialog can render the conflict inline instead of a bare "conflict":

```json
{"code": "auto_provision_conflict",
 "message": "'Company Claude' already sets the building default for auto-provisioned agent-developer accounts.",
 "conflicting_credential_id": "…", "conflicting_credential_name": "Company Claude",
 "role": "agent-developer", "mode": "building"}
```

**Validate the transition, not the end state.** Only slots a request *newly* claims can raise. A record already sitting in a conflicting configuration is not made this request's problem by being touched — renaming it, rotating its key or editing an unrelated field must never `409` on a collision the admin did not introduce. This is the feature's rule, not a local patch here: the same answer was reached for the access policy's lockout validation in phase 1, and invitation validation inherits it. The dialog helps by sending only the fields the admin actually changed; the transition-scoped validator is the backstop under that.

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

`auto_provision_roles` fires at account *creation*, so switching it on does nothing for the people already on the instance. `POST /admin/llm-providers/{id}/apply-to-existing?dry_run=` is the explicit action that closes that gap, and being explicit is the point — the admin sees the count before it happens.

- **Desired set** = every **active** account whose role is in `auto_provision_roles`, minus the current members. Add-only: nobody loses a credential, and existing members are untouched.
- **`dry_run=true`** writes nothing and emits no audit events (there is nothing to audit about a question). It returns `candidate_count`, the `candidates[]` list (user id, email, full name, role) and `defaults_overwrite_count`.
- **A real run** returns the same reconcile shape as create/PATCH (`added`, `skipped`, …) plus `dry_run: false`, `candidate_count` and `defaults_overwrite_count`; `candidates` is empty.
- **`defaults_overwrite_count`** is how many candidates **lose something** to the grant — counted once per person, however many slots they lose, because the question the admin is asking is "how many people does this disturb", not "how many columns move". It covers **both** of the independent default axes the grant writes:
  - `set_user_sdk_defaults` — a mode this record claims already holds a credential pointer **or** a model the member pinned (claiming a slot resets both, so either one alone is a loss).
  - `set_as_default` — the candidate already holds a default credential of this provider type, which the grant demotes.

  It is zero only when the record wires **no** defaults at all. "12 users will receive this credential" is only half the story when 9 of them lose the default they chose, so the confirm dialog quotes both numbers.
- **What the count deliberately leaves out.** `default_sdk_<mode>` — the engine string — is never NULL, so counting it would make the number equal `candidate_count` for every record that claims a mode and the number would stop meaning anything. That is why the zero case is worded narrowly ("nobody has picked a credential or a model for these slots yet") rather than as the broader "nothing is replaced": an OpenAI record claiming the conversation slot *does* move everyone's engine, and only the narrower sentence is true.
- **Counting `set_as_default` is not the same as refusing it.** The uniqueness rule above (One Owner per Default Slot) still ignores `set_as_default` entirely — two records may both claim to be their members' default-for-type, and neither is refused (accepted debt, see [Known Gaps](#known-gaps)). The preview does not change that; it only tells the admin what confirming the second one costs. Treating "we count it" and "we forbid it" as the same question is exactly the mistake the original bug was made of: a `set_as_default`-only record previewed as `defaults_overwrite_count = 0` while silently stripping every candidate's own default key.
- **Idempotent.** A second run adds nobody; the candidate set is already empty.
- The preview is a snapshot — nothing locks the candidate set between the dry run and the commit — so the UI says so when the number moved between the two.

### Admin CRUD

Through the `/admin/llm-providers/` surface, superusers can:
- **Create** a parent record and provision its initial member set (reconcile result returned)
- **List** all parent records fleet-wide, optionally filtered by `?managed_by_id=` (the managing admin) and/or `?target_user_id=` (records that include this user as a member)
- **Get** a single parent record by ID
- **Update** (PATCH) the parent's name/key/base_url/model/default-flags and/or membership; reconcile runs automatically; `?force=` overrides the Tier-2 block on removed members
- **Delete** the parent and all its children — blocked `409` (with `blocked` list) when any child is referenced by a published bundle, unless `?force=true`
- **Set default for all** — calls `set_default` on every current member's child and stamps `set_as_default=True` on the parent
- **Apply to existing users** — grant the record to every active account its `auto_provision_roles` cover (`?dry_run=true` previews without writing)
- **Retry a member's key** — `POST /{id}/members/{user_id}/retry`, for a member whose minting has terminally failed (`400` if it has not failed, `404` if they are not a member)

Through the `/admin/provider-admin-credentials/` surface (superuser-only, secret write-only): **create**, **list**, **get**, **update** (omitting `secret` keeps the stored one), **delete** (`409` unless `?force=true`), **verify**, and **apply-spend-limit**. `GET /admin/provider-adapters/` describes every provider the server supports, derived from the adapter registry — see [provider_adapters_tech](provider_adapters_tech.md).

### Admin UI — "AI Credentials" section

The admin UI is accessed via **Admin menu → AI Credentials** (`/admin/ai-credentials`) in the sidebar. The route is superuser-gated: non-superusers are redirected to `/` by `beforeLoad`.

The page was called **LLM Providers** and lived at `/admin/llm-providers` until zero-touch-onboarding phase 4. That was a **UI rename only** — the backend prefix `/api/v1/admin/llm-providers`, its OpenAPI tag, the generated `AdminLlmProvidersService`, the `MANAGED_CREDENTIALS_QUERY_PREFIX` cache key and the `components/Admin/LlmProviders/` directory are all unchanged, and the old route survives as a `beforeLoad` redirect stub so bookmarks and older documentation keep working. Every `/admin/llm-providers/...` **HTTP path** in this document is a backend call and is current.

The page has **two tabs** on the one URL (local state, not a search param, so there is no per-tab URL):

- **Managed credentials** — the parent records, described below.
- **Provider keys** — the connected provider organisations (`ProviderAdminCredentialsTable`), with **Connect provider** in the page header. Columns: Name (with a sub-line naming how many managed credentials mint through it and how many live keys it is the only means of revoking) | Provider | Project | Spend limit (`cents / 100` per month) | Verified | Last error, plus a row menu of **Verify**, **Apply spend limit**, **Edit / rotate key**, **Disconnect**. Its empty state points back at the other tab: *"For a provider whose administration API does not create keys, a key is pasted for each person on the Managed credentials tab."* Disconnect sends `force=false` first; a `409` renders the refusal inline with both counts and relabels the button **"Disconnect anyway"**.

**User flow (Managed credentials tab):**

1. The page loads a fleet-wide table of all parent records, sorted by name. Each row shows: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | **Auto** (role chips, or an "Off" badge when no role is set — a blank cell would read as missing data) | **Keys** | Members (inline `MemberChip`s — name + email + the member's key status; a failed member also shows the reason and a **Retry** button) | Created. The **Keys** column shows `Shared key` for a shared record, and for a minted one a per-status roll-up ("2 key created", "1 creating key") with a spinner while anything is in flight. The list polls every 10 seconds while any record has a key in flight.
2. The admin can **filter** the table to records that have a specific user as a member via a `UserAllowlistPicker` toggle-panel in the page header. A filled dot on the Filter button indicates an active filter; a "Clear filter" button appears inside the panel.
3. Clicking **"Provision Credential"** opens a unified dialog (`ManagedCredentialDialog` in `create` mode) with:
   - Name (free-form; auto-suggested as `"<Provider> Key"` until the admin types their own)
   - Provider type (dropdown: Anthropic, OpenAI, OpenAI Compatible, Google — MiniMax not offered in the UI)
   - **Key source** (radio, create-only — disabled in edit mode with "Set when the record was created and fixed afterwards"): *"One key, shared by every member"* — "You paste a key and each member gets their own copy of it" — versus *"A separate key for each member"*. The second option is disabled unless the server says the provider can mint **and** an organisation for it is connected, and the help text says which is missing: the provider's administration API does not create keys, or *"Connect a provider organisation on the Provider keys tab first."* When minted is chosen the **API key field disappears entirely** and a required **Provider organisation** select appears, filtered to organisations of the same provider, together with a plain-language explainer of what the record does with each member's key
   - API key (password field; required on create **in shared mode only**)
   - Base URL — shown only for `openai_compatible` (required) and `google` (optional)
   - Model — shown only for `openai_compatible` (required)
   - **Default model** — optional text input; the concrete model ID used by default for all environments and native apps using this credential (leave blank to use the platform catalog default); a **"View available models ↗"** link to the provider's official models reference appears next to the label (Anthropic → platform.claude.com models overview; Google → ai.google.dev Gemini API models; OpenAI → developers.openai.com models; omitted for OpenAI Compatible)
   - **Available models** — optional multi-line/comma editor; the curated list of model IDs offered for selection; leave empty to offer all auto-detected models
   - **"Fill top 10 models"** button — auto-runs Test Connection first if no fresh successful result exists, then fills "Available models" with the top 10 discovered models (deduped, provider-prefix stripped) and auto-sets "Default model" using provider-specific logic: Google → `gemini-flash-latest` (fixed alias); Anthropic → highest-version Sonnet found in the model list, falling back to the first model; OpenAI / OpenAI Compatible → first model in the list
   - Target Users — multi-select `UserAllowlistPicker`; **no longer required**: an auto-provision-only record legitimately starts empty. On create the dialog refuses only when there are neither target users nor auto-provision roles, because such a record would do nothing at all
   - "Set as default" toggle
   - "Set user SDK defaults" toggle — when on, reveals a bordered block with **Modes to wire** (Conversation / Building checkboxes) and, per ticked mode, a **Conversation/Building model override** input with the shared **List models** button beside it (the picker probes this record's key through `POST /test-connection` and inserts a bare model id)
   - **Auto-provision for new users** — Agent User / Agent Developer / Admin checkboxes, with the helper text "Applied when an account is created; use Apply to existing users for current accounts". A `409` slot conflict renders as an inline alert inside this block, naming the other credential
   - "Test Connection" button (probes the entered key without persisting; surfaces model count or skip reason)
   - On submit, the dialog surfaces a reconcile summary toast (`+N added, −N removed, ~N updated`) plus per-user skip/blocked toasts
4. Each row has a three-dot actions menu (`LlmProviderActionsMenu`) with:
   - **Edit** — opens `ManagedCredentialDialog` in `edit` mode; same fields as create; API key field blank means "keep stored key for all members"; member add/remove via `UserAllowlistPicker` pre-seeded from current `record.members`; provider type is immutable after creation. The PATCH carries **only the fields this admin actually changed**, diffed against the snapshot the dialog opened with — membership included (`target_user_ids` is an absolute desired set, and resubmitting a stale snapshot would delete every member acquired since the dialog opened, which is exactly what auto-provisioning does all day). Emptying the member picker now shows a warning naming how many members would lose their copy of the credential
   - **Set default for all** — calls the `/set-default` endpoint for every current member
   - **Apply to existing users** — opens a confirm dialog that always fires a fresh dry run (the table row is up to 30s stale). The first paragraph quotes `candidate_count` ("N accounts will receive …"); the second says what the grant *costs*, and is **not** gated on `set_user_sdk_defaults` — it names every default slot the record claims, the per-mode defaults *and* "the default <Provider> credential" when `set_as_default` is on:
     - nonzero — "**M** of them already have a default this grant takes away. "X" becomes the conversation default and the default Anthropic credential for every account here, and the model each of those modes was pinned to is reset."
     - zero — "None of them has picked a credential or a model for the conversation default yet, so "X" fills those in rather than replacing a choice." (deliberately narrower than "nothing is replaced" — see the note on `default_sdk_<mode>` above)
     - There is a fallback arm that states the number without naming the slots, so the dialog can never be silent while `defaults_overwrite_count > 0`.

     It also tells the admin when the record auto-provisions for no role at all. Skips are surfaced one toast per user, by name, resolved from the preview's candidate list
   - **Delete** — opens an `AlertDialog`; on `409` (blocked members) escalates to a force-delete confirmation listing blocked users by name

### Admin UI — Server Configuration → Access

The same flag seen from the other end. An admin setting up the front door asks "what does a new Agent Developer get?", and answering that from a list of credentials means opening each one in turn. So the Access tab's own **Company AI credentials** card (`CompanyAiCredentialsCard`, a half-width card alongside the [Access Policy](../server_configuration/access_policy.md) cards, not nested inside any of them) lists the managed credentials one per row, each with a **User · Dev · Admin** role toggle group; a segment on means new accounts of that role receive the key.

- A toggle is one `PATCH /admin/llm-providers/{id}` carrying `auto_provision_roles` and nothing else; the card never invents state of its own and shares the AI Credentials page's query key, so a change made on either surface shows on both.
- Capped at **5 rows** (credentials that already grant something first, then alphabetical) with a footer link reading "Manage AI credentials" when everything fits, or "Show all (N) on AI Credentials" once it does not — one link to `/admin/ai-credentials`, not two.
- A cell whose tick would be refused carries an advisory **Conflict** badge, computed client-side from the loaded list. It is a hint, not a gate — the list can be stale, the click still goes to the server, and a real `409` renders as an alert under the table.
- Empty state: "No managed AI credentials yet — create one", linking to `/admin/ai-credentials`.
- The card's own description states the creation-time rule: "Changing a role later never grants or revokes a key — use \"Apply to existing users\" on the AI Credentials page for accounts that already exist."

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

On apply-to-existing (`POST /{id}/apply-to-existing`, real run only — a dry run emits nothing):
- `admin.ai_credential.provision` per added child (scoped to the child owner)
- `admin.managed_ai_credential.apply_to_existing` scoped to the admin

On automatic grant at account creation (no acting admin, so **not** emitted by the admin route):
- `admin.ai_credential.auto_provision` — severity `low`, scoped to the **new owner**, `details = {managed_credential_id, child_credential_id, target_user_id, origin, role, managed_by_id, actor: "system"}`
- `admin.ai_credential.auto_provision_failed` — severity `medium`, scoped to the new owner, `details = {managed_credential_id, target_user_id, origin, role, reason, actor: "system"}`. `reason` is one of `add_members_failed`, `user_not_found`, `user_inactive`, `provision_failed`

On parent delete (`DELETE /{id}`):
- `admin.ai_credential.delete` per removed child
- `admin.managed_ai_credential.delete` scoped to the admin

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone; it is replaced by the parent-level `admin.managed_ai_credential.*` events.

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
| `admin.provider_admin_credential.{create,update,delete,verify,apply_spend_limit}` | medium | the acting admin | Provider-organisation administration. `update` records `secret_rotated: bool`, never what it was rotated to; `delete` records `forced` **plus the two counts it overrode**, because that is the one action that permanently strands live provider keys and its audit row is the last place anyone can learn how many |

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
- **Parent record is the source of truth.** Membership is a row, one per `(parent, user)` — not derived from the children. In shared mode the parent holds its own encrypted key so adding a member or rotating the key never requires re-entering the secret; in minted mode it holds no key at all.
- **Every `AICredential` row that exists is usable.** No placeholder child, no empty key, no "preparing" credential. The membership row carries the pre-key state, so every consumer of the credentials table — the discovery cron, the account-config bundle, the environment credential bag — needs no new filter and cannot grow one by accident.
- **Membership existence and the provisioning lifecycle have different owners.** `ManagedAICredentialsService` creates and deletes membership rows and sets their *initial* status; `KeyProvisioningService` owns every `pending → minting → provisioned | failed` transition plus `suspended`, and never creates or deletes a row. Two questions, two owners, so they cannot disagree.
- **Statuses are explicit and never inferred from NULL** — including the terminal ones. The one absence that means something is the row itself.
- **Every write an attempt makes after taking the claim is conditional on it — the success path and every failure path alike.** On a lost claim the minted key is revoked rather than stored; if that revoke fails, the durable high-severity event carries the external ref. A minted key must never end up live-but-unrecorded. The failure path is conditional for a reason of its own: `_record_failure` writes `pending` with a retry time, so an unconditional version would put a row a concurrent deactivation had settled `suspended` back into the converge queue.
- **The publisher is told at publish time what installers will get.** `PublishService.publisher_ai_credential_notices` returns finished sentences on the publish response (`publish_notices`), rendered as a dismissible persistent callout on the Bundle tab. Every sentence names an action: installers supply their own key, or an administrator provisions the credential to them directly. A live existing share is called out as still working but no longer sanctioned.
- **A blocked removal states its own reason sentence.** `ManagedReconcileBlock.message` is server-authored, required, drawn from one table, and rendered verbatim everywhere — so a mint-in-flight block can never be reported as a bundle conflict, whose remedy (`force=true`) would strand a live key.
- **A failure is durable.** A `failed` membership is never deleted to tidy up, and the only way out of it is an explicit Retry.
- **No provider call on a request path.** Adding a member records an intent; a converge pass mints against it.
- **Minting is refused into a project whose spend limit is not `enforcing`**, and the limit is set or verified at setup — never applied after a key exists.
- **No child of a managed record is ever shared, in either mode.** `share_credential` refuses when `ai_credentials_service.is_shareable(credential)` is false — the predicate is the plain `is_admin_managed` column, so an orphaned child (parent force-deleted, FK `SET NULL`) is refused too. The refusal is the typed `AICredentialNotShareableError`, a permanent policy answer its one caller can tell apart from the transient failures it swallows. A bundle wired to such a credential degrades to "user provides"; it does not fail the install, and it is not reported to the installer at all, because the environment resolves the installer's own AI credential in its place and nothing is missing. **Where a share already exists** both the install path and the gate look for an `AICredentialShare` to the installer before asking the policy, so an install running on a share made before the widening keeps working. Those rows are deliberately left alone — the accepted price is that such a share can stay live indefinitely if its publisher never publishes again, and the publisher is told so, with the action to take, at publish time.
- **Only a provider whose administration API creates keys may be minted for**, and only while an organisation for it is connected. Both halves are enforced server-side in `_validate_provisioning_shape`; the browser reads the server's `can_mint_now` answer rather than recomputing the conjunction.
- **Provisioning mode is fixed at creation.** `api_key` on a minted record is a `400`, not a silent no-op.
- **A provider admin credential cannot be deleted while it is the only means of revoking a live key** — `409` unless forced, and forcing is audited with the counts it overrode.
- **Deactivation revokes minted keys and keeps the membership; reactivation mints a fresh one.** Shared credentials are untouched by either. Which memberships have a key to revoke is `holds_provider_key` — one predicate shared with deletion and with the admin-secret usage count — never a status list, because a terminal `failed` row can still hold a live `external_key_ref`.
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
- **One auto-provisioning owner per `(role, mode)` default slot**, validated on create and PATCH (`409`), scoped to the slots the request newly claims.
- **`auto_provision_roles` is validated; `sdk_default_modes` is not.** An unknown role is a `400`. An unknown mode string saves with a `200` and then wires nothing, forever — see [Known Gaps](#known-gaps).
- **Emptying `auto_provision_roles` is not a revoke.** Existing members keep their credential; the record simply stops being granted to new accounts.
- **An invited account provisions through the same service.** The wizard's explicit list goes to `AccountProvisioningService.provision_explicit`, not to a route-level `add_members` loop, so it inherits the never-fail net, the session-repair discipline, the skip reporting into the invited account's own feed, and the deactivated-account short-circuit. Its pre-ticked set is derived from the *same* `auto_provision_roles` predicate the automatic path uses, so an invited account and a self-registered one of the same role start with the same keys.
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


Any account-creation path (signup / google / invite / admin / external / seed)
   │
   ▼
UserService.create_account(session, *, email, origin, ...)   ← the ONLY place a User row is built
   │  commit + refresh
   ▼
AccountProvisioningService.on_account_created(session, user, origin)   [never raises]
   ├── parents = ManagedAICredential where user.role ∈ auto_provision_roles
   ├── per parent, inside its own try:
   │     managed_ai_credentials_service.add_members(parent=…, user_ids=[user.id], actor=None)
   │       → _add_child() → _stamp_child() → [optional] set_default / _apply_sdk_defaults
   │                                              (writes default_model_override_<mode>)
   │     SecurityEvent("admin.ai_credential.auto_provision", low)     [per added child]
   └── on failure: rollback → warn → skipped entry
         SecurityEvent("admin.ai_credential.auto_provision_failed", medium)


Superuser (web)
   │
   │  POST /api/v1/admin/llm-providers/{id}/apply-to-existing?dry_run=
   ▼
ManagedAICredentialsService.apply_to_existing(session, admin, id, dry_run=…)
   ├── candidates = active users with role ∈ auto_provision_roles, minus current members
   ├── defaults_overwrite_count = candidates who lose a default (either axis), counted once each
   ├── dry_run → return candidates, write nothing, emit nothing
   └── else add_members(actor=admin) → SecurityEvent per child
         + admin.managed_ai_credential.apply_to_existing  [scoped to admin]


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
5. **The uniqueness rule does not cover `set_as_default`.** Two auto-provisioning records that both set themselves as their members' default-for-type are the same last-writer-wins class as the SDK-default slot, and are not refused. This is a gap in *validation only*, and it must not be read as a gap in disclosure: `_count_default_overwrites` does count this axis, so the apply-to-existing preview tells the admin exactly how many people the second record demotes. The admin is warned, and then allowed.
6. **A blocked removal can strand a retracted override.** If one PATCH both clears a model override and fails to remove a member whose child is in use, that member keeps the retracted pin and no later request can express the retraction again — the transition is gone. Narrow enough to accept; the alternative is unpinning someone who did not actually leave.
7. **One branch is deliberately untested rather than covered by a test that asserts nothing.** `detect_anthropic_credential_type`'s `adapter is None` fallback carries `# pragma: no cover - the registry always serves it`. Reaching it needs the registry to fail to serve a shipped provider, and a test that arranges that is testing the arrangement. It is documented as unreachable — a claim a reader can check — rather than green-washed. *(The sharing guard that used to sit at this number **is** covered now: bundle-shaped, through the install path that calls it, parametrized over `minted` and `shared`.)*
8. **One service account can leak on a crash inside the mint window.** Between the provider creating the service account and the process committing anything, no row names it. The create is a single call that returns the secret, so it cannot be split without asking for a response that carries no secret. The orphan is visible in the provider's own console. See [The provisioning lifecycle](#the-provisioning-lifecycle).
9. **Key scopes are not requested.** Deliberate — the scope vocabulary is not authoritative in any artifact this repo holds, and a guessed list would hard-fail the create call the day the provider changed it. `scopes_requested` / `scopes_granted` are recorded on every external ref so hardening is a change to two values rather than a rewrite. `model_permissions` is a known lever, deliberately not pulled. Nothing in this feature may be described as narrowing a minted key's permissions until one of these is actually enforced.
10. **A rejected Google key surfaces as a 500 rather than `invalid_key`.** `google-genai` raises `google.genai.errors.ClientError`, which is not an `httpx` exception, so it bypasses the auth-error mapping. **Symptoms: a 500 from Test Connection, and `"ClientError"` as the reason in the discovery cron.** Pre-existing — the old single dispatch had the identical hole — and preserved deliberately; see [provider_adapters_tech](provider_adapters_tech.md#known-gap--a-vendor-sdks-own-error-bypasses-the-invalid_key-mapping).
11. **Two per-provider tables in `environment_lifecycle.py` are outside the architecture test's reach**, because they are keyed on the enum's *string values*. Covering them would mean guessing, and the guesses would need an allowlist — and the empty allowlist is the whole basis of that test's design. One of the two is genuinely provider-shaped and belongs on an adapter one day; the other is dispatch over local variables and does not. See [provider_adapters_tech](provider_adapters_tech.md#the-known-hole-tables-keyed-on-the-enums-string-values).
12. **The list-response envelope is inconsistent across the three new endpoints.** `GET /admin/provider-adapters/` returns `{data, count}`; `GET /admin/provider-admin-credentials/` and `GET /ai-credentials/provisioning` return bare arrays. Each is consumed correctly, but "how does an admin list respond" now has two answers.
14. **RESOLVED — an unshareable publisher credential no longer gates an install.** It used to. When a bundle's publisher AI credential was not shareable *and* no existing share reached the installer, two components disagreed: the environment fell back to the installer's own default (`InstallService._linkable_publisher_ai_credential` returns `None`, so the install ran), while `InstallReadinessGate._scan_ai_credentials` emitted `publisher_credential_unshareable` — a `publisher_broken` verdict short-circuiting **every inbound message** on chat, MCP, A2A and session webhooks before any LLM call. It was **unclearable by the installer**, because the scan reads `bundle.publisher_ai_credential_*_id` and no installer action changes a bundle FK, and the copy named the wrong screen: the agent's Credentials tab has no AI-credential control at all.

    Escalated as a product question — *should an unshareable publisher credential block the install at all, given the environment has already fallen back to a working credential?* — and answered **no**. The gate now skips the credential, the `publisher_credential_unshareable` reason is removed from `GateMissingReason`, and the bespoke copy is gone from both renderers. Scenarios P/Q/R in `agents_bundles_install_readiness_test.py` pin the outcome, with R the load-bearing one: it proves the gate still reports a *shareable* credential that simply is not shared, so P and Q are "we skip the admin-managed ones deliberately" rather than "we stopped scanning".

13. **The dialog still states per-provider required-field rules three times**, in `ManagedCredentialDialog`'s zod enum, its `superRefine` branch, and its `showBaseUrl` / `showModel` conditions — while `requires_base_url` / `requires_model` now arrive from the adapters endpoint and are consumed nowhere in the frontend. Separately, `PROVIDER_TYPE_OPTIONS` still holds a hardcoded provider list, and that one is **deliberate**: driving the picker from the adapters endpoint would re-expose MiniMax, which the UI hides on purpose.

---

## Integration Points

- **[Auth](../auth/auth.md)** — `UserService.create_account` is the chokepoint that triggers auto-provisioning, and `AccountOrigin` is what records which arrival path a grant came from.
- **[User Roles](../user_roles/user_roles.md)** — `User.role` is the selector: a record is granted when the new account's role appears in its `auto_provision_roles`. The role itself comes from `ServerConfig.default_user_role` unless the caller passes one explicitly.
- **[Access Policy](../server_configuration/access_policy.md)** — hosts the *Company AI credentials* card beside its **New user defaults** card, and gates which origins may create an account at all.
- **[AI Credentials](ai_credentials.md)** — the reused core service: `create_credential`, `set_default`, `update_credential`, `delete_credential`, `decrypt_credential`, `resolve_default_credential_for_sdk`. `ManagedAICredentialsService` delegates every per-child operation with `user_id = owner_id` so all per-user invariants (one-default-per-type, profile auto-sync, SDK-default wiring) run for the target user.
- **[AI Credentials Tech](ai_credentials_tech.md)** — `AICredential` model with three managed-credential columns (`is_admin_managed`, `managed_by_id`, `managed_credential_id`); `AICredentialsService.update_credential` / `delete_credential` `admin_override` kwarg; `AICredentialPublic.is_admin_managed` projection.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the `/external/` route namespace that the account-config endpoint extends. The same `ExternalAccountConfigService` sits under `services/external/`.
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the desktop/mobile JWTs with `client_kind` and `external_client_id` claims. The live revocation check in `get_current_user` ensures revoked device tokens are rejected `401` before the native gate runs.
- **[User Roles](../user_roles/user_roles.md)** — `get_current_active_superuser` gates the admin surface. Only superusers may create or manage parent records.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the blast-radius gate on child credential removal (`AICredentialInUseError`, Tier-2, published bundle references) is the same mechanism used by the regular credential deletion guard. It is also what can block a deactivated user's minted key from being deleted, in which case the key is deliberately left live and the block is recorded.
- **[Provider Adapters](provider_adapters_tech.md)** — the registry that declares which providers can mint, what an administration credential for them needs, and the `KeyProvisioner` contract every provider call goes through.
- **[Agent Bundles](../../agents/agent_bundles/agent_bundles.md)** — bundle publisher wiring is the only caller of `share_credential`, and is where the refusal is actually observed. `InstallService._linkable_publisher_ai_credential` keeps the unreachable credential off the new environment so the bundle really does fall back to "user provides" (it previously failed the whole install with a bare `400`), and `InstallReadinessGate` reports `publisher_credential_unshareable` to the installer — but only when no `AICredentialShare` already reaches them; an install running on a pre-existing share is reported as missing nothing. `PublishService.publisher_ai_credential_notices` tells the **publisher** the same fact at publish time, with the action to take. `GateMissingReason` lives in `backend/app/models/bundles/catalog.py`, declared once and imported by the gate.
- **[Status Repair](../../system/status_repair/status_repair.md)** — a sibling consumer of `app.core.db.leader_session`, the single implementation of the single-leader advisory-lock pattern the key-provisioning scheduler also uses.

---

*Last updated: 2026-09-06 — known gap 14 resolved: an unshareable publisher credential no longer gates an install. The `publisher_credential_unshareable` reason is removed from `GateMissingReason`, the gate skips the credential whether or not a share exists, and the bespoke copy is gone from both renderers — the environment already resolves the installer's own credential, so nothing is missing.*

*Previously — zero-touch onboarding phase 5, **fifth pass** (review of the fourth pass's fixes): the share lookup now runs **before** `is_shareable` in both `InstallReadinessGate._scan_ai_credentials` and `InstallService._linkable_publisher_ai_credential`, so an installer who already holds a share is reported as missing nothing and keeps the credential on reinstall — three facts (the share exists / it still works / it will not be created again) kept apart instead of collapsed into one false sentence; leaving those share rows in place is an **accepted** trade, with its named weakness (a publisher who never publishes again leaves one live indefinitely) recorded as accepted; `PublishService.publisher_ai_credential_notices` → `publish_notices` on the publish response, rendered as a dismissible persistent callout on the Bundle tab, every sentence carrying an action; `UserPublicWithAICredentials.api_key_onboarding_state` made **required with no default** (a default makes it optional in the generated client, which forces the browser to supply the policy fallback this enum forbids); `_linkable_publisher_ai_credential` returns `None` for a vanished row — the FK made the old "graceful fallback" claim an `IntegrityError`; `ai_credentials_service.has_a_default` deleted (no callers). New [Known Gap 14](#known-gaps): an unshareable publisher credential with no existing share blocks every message on an install that runs, unclearably by the installer, with copy naming the wrong screen — escalated as a product question, deliberately unfixed.*

*Previously — **fourth pass** (whole-feature seam review): the sharing refusal widened from minted-only to every admin-managed child (`is_shareable`, typed `AICredentialNotShareableError`) — so a bundle wired to a `shared`-mode child now degrades to "user provides" rather than redistributing the admin's key; the install fallback made real (`InstallService._linkable_publisher_ai_credential`, which previously failed the whole install with a bare 400); the `publisher_credential_unshareable` gate reason with `GateMissingReason` consolidated into `app/models/bundles/catalog.py`; `ManagedReconcileBlock.message`; `AIKeyOnboardingState` made provider-agnostic (`owner_ids_with_a_default`) with the latched, non-destructive dashboard wall and the invite success message on the same predicate; the mint claim token and revoke-on-lost-claim; `holds_provider_key` as the one key-holding predicate. Phase 5 itself: provider adapter registry, provider admin credentials, per-user key minting (membership as a row, converge scheduler, revocation cascade), the invite success screen's "Add a key for this user" step, and `api_key_onboarding_state`; migration `ed8d6a23f13c`. Phase 4 renamed the admin page to **AI Credentials** (`/admin/ai-credentials`), UI only. Phase 2: auto-provision roles, per-mode model overrides, apply-to-existing, `(role, mode)` slot conflicts; migration `b71863b32aa1`*
