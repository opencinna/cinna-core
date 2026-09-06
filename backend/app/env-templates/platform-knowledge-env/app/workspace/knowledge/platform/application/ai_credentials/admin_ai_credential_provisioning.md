# Admin-Provisioned AI Credentials + Native Account-Config

## Purpose

Two related backend capabilities that together deliver a "ready on login" experience for a company DevOps admin managing a cinna-core instance.

- **Part A — Admin-provisioned AI credentials.** A superuser creates a single **Managed AI Credential** parent record and assigns it to one or more target users — by hand, and/or **automatically to every account created with a chosen role** (see [Auto-Provisioning at Account Creation](#auto-provisioning-at-account-creation)). The parent record is the canonical source of truth (name, type, encrypted key, default flags). The service reconciles the desired target-user set into per-user `AICredential` child rows, which participate automatically in all existing per-user plumbing. Users can use their child credential and set it as their default, but cannot edit, delete, or re-key it — those operations return `403`.
- **Part B — Native account-config endpoint.** A native-token-gated endpoint (`GET /api/v1/external/account-config`) returns the caller's own usable AI credentials with the *decrypted* API key, so Cinna Desktop and Cinna Mobile can auto-create local "LLM providers" and a suggested chat mode per credential on login, without the user having to copy-paste keys into the native app.

> **Frontend status:** The admin **AI Credentials** section (`/admin/ai-credentials`; called "LLM Providers" at `/admin/llm-providers` until zero-touch-onboarding phase 4, which renamed the **UI only** and left a redirect stub) is **implemented** — superusers can provision, list, filter, edit, set-default, delete, and set an admin-curated model list (default model + available models) from the web UI. The user-facing AI Credentials card renders admin-managed child credentials with a **"Managed" badge** and a read-only **Default model** line when a curated default is set. The native-app side (Part B) backend is complete; Cinna Desktop/Mobile provider auto-creation is not yet built.

---

## Core Concepts

| Term | Definition |
|------|-----------|
| **Managed AI Credential (parent)** | A `ManagedAICredential` row owned by a superuser. Holds the canonical config (name, type, Fernet-encrypted key, base_url/model, default flags) and is reconciled into child rows. Membership is derived from the children — there is no `target_user_ids` column on the parent. |
| **Child credential** | An ordinary `AICredential` row with `is_admin_managed=True`, `managed_credential_id` pointing at the parent, and `owner_id` set to the target user. Participates in all existing per-user plumbing; read-only through the user-facing CRUD. |
| **Reconcile** | The diff-and-converge operation: Add (desired − current) creates children; Remove (current − desired) deletes them (Tier-2 blast-radius gated); Update (intersection) writes changed parent fields and/or rotated key through to each child and applies/clears the default flag. Idempotent — a no-op desired set with unchanged fields produces empty added/removed/updated lists. |
| **Account config** | The native-client bundle: a list of provider descriptors (with decrypted API keys) that Cinna Desktop / Mobile uses to create local LLM providers on login. |
| **Native token gate** | The `account-config` endpoint is only accessible to JWTs whose `client_kind` claim is `"desktop"` or `"mobile"`. Plain web-session JWTs are rejected `403`. |
| **Auto-provision roles** | `ManagedAICredential.auto_provision_roles` — the roles whose **newly created** accounts receive this credential automatically. Empty (the default) means the admin picks members by hand. Read at the account-creation chokepoint, never on a later role change. |
| **Apply to existing users** | The explicit admin action that grants an auto-provisioning record to the accounts that already exist. Add-only; previewed with a dry run before it is committed. |
| **Model override (per mode)** | `model_override_conversation` / `model_override_building` — the model id pinned on a *member's profile* (`User.default_model_override_<mode>`) for the modes this record wires. Distinct from `default_model`, which is written onto the *child credential row*. |
| **Default slot** | One of the two `(role, mode)` positions on a member's profile — `default_ai_credential_conversation_id` / `default_ai_credential_building_id` plus their model override. A slot holds exactly one credential, which is why only one auto-provisioning record may claim it per role. |

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
| `auto_provision_roles` | Roles whose newly created accounts receive this credential. `[]` (default) = never granted automatically. Validated against the role enum — an unknown role is a `400`, not a silent drop. |
| `model_override_conversation` / `model_override_building` | Model id pinned on each member's profile for that mode, written whenever this record wires that mode's SDK default. Blank = this record has no opinion and members fall back to the credential's own `default_model`. |

### Reconcile semantics

| Pass | Logic | Per-child failures |
|------|-------|--------------------|
| **Add** (desired − current) | Validate user exists + is active; decrypt parent key; create child via `ai_credentials_service.create_credential`; stamp markers + parent link; apply optional default/SDK wiring. | Unknown/inactive user → `skipped`. Committed child with failed post-create wiring is retained as a member (not demoted to skipped). |
| **Remove** (current − desired) | Delete child via `ai_credentials_service.delete_credential(admin_override=True)`. Tier-2 bundle blast-radius triggers `AICredentialInUseError`. | In-use child → `blocked` (member stays) unless `force=True`. Any other removal failure → `blocked` with reason `remove_failed`; the member stays and the PATCH still returns its reconcile result rather than a `500`. |
| **Update** (current ∩ desired) | Diff parent scalars against child; write changed fields/key through; apply/clear `set_as_default`. Only mutated children appear in `updated`; unchanged children produce no audit event. | Failed update → `skipped` with reason `update_failed`; the rest of the reconcile continues and the PATCH still returns its reconcile result rather than a `500`. |

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

### Why the invite wizard has no "add a key for this user" step

**It does not have one by design.** A future reader who notices the gap should recognise it as decided, not file it as an omission.

The reason is an ordering constraint, not an oversight: the wizard's provisioning step runs *before* the account exists. The account row is created when the wizard is **submitted**, so at the step where such a control would live there is no user id to attach a key to and no `target_user_ids` to send. A version bolted onto the success screen — after the account exists — was considered and cut rather than deferred: it is a second, differently-shaped credential-creation surface for a case the AI Credentials page already covers.

Meanwhile, an administrator who needs to hand one person their own key adds it from **Admin → AI Credentials** after the invitation is sent, exactly as they would for any existing account.

Where that fallback ultimately lands is owned by phase 5 (provider adapters and per-user key minting): if per-user minting proves unusable for a provider, phase 5 decides where the "paste a key for this person" affordance goes. This step is not it.

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

### Admin UI — "AI Credentials" section

The admin UI is accessed via **Admin menu → AI Credentials** (`/admin/ai-credentials`) in the sidebar. The route is superuser-gated: non-superusers are redirected to `/` by `beforeLoad`.

The page was called **LLM Providers** and lived at `/admin/llm-providers` until zero-touch-onboarding phase 4. That was a **UI rename only** — the backend prefix `/api/v1/admin/llm-providers`, its OpenAPI tag, the generated `AdminLlmProvidersService`, the `MANAGED_CREDENTIALS_QUERY_PREFIX` cache key and the `components/Admin/LlmProviders/` directory are all unchanged, and the old route survives as a `beforeLoad` redirect stub so bookmarks and older documentation keep working. Every `/admin/llm-providers/...` **HTTP path** in this document is a backend call and is current.

**User flow:**

1. The page loads a fleet-wide table of all parent records, sorted by name. Each row shows: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | **Auto** (role chips, or an "Off" badge when no role is set — a blank cell would read as missing data) | Shared with (inline member chips — name + email, no per-member default badge) | Created.
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

### Admin UI — Server Configuration → Access & New Users

The same flag seen from the other end. An admin setting up the front door asks "what does a new Agent Developer get?", and answering that from a list of credentials means opening each one in turn. So the *New users* block of the [Access Policy](../server_configuration/access_policy.md) card ends with a **Company AI credentials** matrix (`AutoProvisionedCredentialsMatrix`): managed credentials down the rows, the three roles across the columns, one checkbox per cell.

- A toggle is one `PATCH /admin/llm-providers/{id}` carrying `auto_provision_roles` and nothing else; the matrix never invents state of its own and shares the AI Credentials page's query key, so a change made on either surface shows on both.
- A cell whose tick would be refused carries an advisory **Conflict** badge, computed client-side from the loaded list. It is a hint, not a gate — the list can be stale, the click still goes to the server, and a real `409` renders as an alert under the table.
- Empty state: "No managed AI credentials yet — create one", linking to `/admin/ai-credentials`. A "Manage AI credentials" link sits in the block header.
- The block's own helper text states the creation-time rule: "Changing a role later never grants or revokes a key."

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
- **Auto-provisioning is creation-time only.** A role change on an existing account never grants or revokes a key; "Apply to existing users" is the explicit action.
- **Provisioning failure never fails account creation.** Recorded as a skip + a medium-severity security event in the new owner's feed + a warning log; the account stands. A failure in the *post-create* default wiring is a different case: the child row is already committed, so the member is retained, the failure is logged only, and no skip or failure event is written.
- **One auto-provisioning owner per `(role, mode)` default slot**, validated on create and PATCH (`409`), scoped to the slots the request newly claims.
- **`auto_provision_roles` is validated; `sdk_default_modes` is not.** An unknown role is a `400`. An unknown mode string saves with a `200` and then wires nothing, forever — see [Known Gaps](#known-gaps).
- **Emptying `auto_provision_roles` is not a revoke.** Existing members keep their credential; the record simply stops being granted to new accounts.
- **An invited account provisions through the same service.** The wizard's explicit list goes to `AccountProvisioningService.provision_explicit`, not to a route-level `add_members` loop, so it inherits the never-fail net, the session-repair discipline, the skip reporting into the invited account's own feed, and the deactivated-account short-circuit. Its pre-ticked set is derived from the *same* `auto_provision_roles` predicate the automatic path uses, so an invited account and a self-registered one of the same role start with the same keys.
- **The invite wizard deliberately has no "add a key for this user" step**, because the account does not exist at the step where such a control would live — see [Why the invite wizard has no "add a key for this user" step](#why-the-invite-wizard-has-no-add-a-key-for-this-user-step).

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

---

## Integration Points

- **[Auth](../auth/auth.md)** — `UserService.create_account` is the chokepoint that triggers auto-provisioning, and `AccountOrigin` is what records which arrival path a grant came from.
- **[User Roles](../user_roles/user_roles.md)** — `User.role` is the selector: a record is granted when the new account's role appears in its `auto_provision_roles`. The role itself comes from `ServerConfig.default_user_role` unless the caller passes one explicitly.
- **[Access Policy](../server_configuration/access_policy.md)** — hosts the *Company AI credentials* matrix in its **New users** block, and gates which origins may create an account at all.
- **[AI Credentials](ai_credentials.md)** — the reused core service: `create_credential`, `set_default`, `update_credential`, `delete_credential`, `decrypt_credential`, `resolve_default_credential_for_sdk`. `ManagedAICredentialsService` delegates every per-child operation with `user_id = owner_id` so all per-user invariants (one-default-per-type, profile auto-sync, SDK-default wiring) run for the target user.
- **[AI Credentials Tech](ai_credentials_tech.md)** — `AICredential` model with three managed-credential columns (`is_admin_managed`, `managed_by_id`, `managed_credential_id`); `AICredentialsService.update_credential` / `delete_credential` `admin_override` kwarg; `AICredentialPublic.is_admin_managed` projection.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the `/external/` route namespace that the account-config endpoint extends. The same `ExternalAccountConfigService` sits under `services/external/`.
- **[Desktop Auth](../desktop_auth/desktop_auth.md)** — issues the desktop/mobile JWTs with `client_kind` and `external_client_id` claims. The live revocation check in `get_current_user` ensures revoked device tokens are rejected `401` before the native gate runs.
- **[User Roles](../user_roles/user_roles.md)** — `get_current_active_superuser` gates the admin surface. Only superusers may create or manage parent records.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the blast-radius gate on child credential removal (`AICredentialInUseError`, Tier-2, published bundle references) is the same mechanism used by the regular credential deletion guard.

---

*Last updated: 2026-09-06 — zero-touch onboarding phase 4 renamed the admin page to **AI Credentials** (`/admin/ai-credentials`), UI only. Phase 2: auto-provision roles, per-mode model overrides, apply-to-existing, `(role, mode)` slot conflicts; migration `b71863b32aa1`*
