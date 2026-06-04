# Agent REST API — Automatic Credentials & Bundle Embed Detection

**Status:** Draft plan (planning only — do not implement)
**Feature name:** `agent-api-automatic-credentials`
**Scope:** Two related fixes for `agent_api` (`CredentialType.AGENT_API`) connection credentials.

---

## Overview

`agent_api` credentials are auto-created by the "Connect Agent API" helper
(`AgentApiTokenService.connect_agent_api`). They are the connection between a
consumer agent and a producer agent's REST API and store
`{base_url, spec_url, token, label, producer_agent_id}`. Two problems:

- **Problem 1 — poor home in the global Credentials view.** They land in the
  default workspace (NULL) regardless of which workspace the owning agent lives
  in, and are mixed into "My Credentials" with no distinct treatment.
- **Problem 2 — bundle install mis-detects a shared `agent_api` credential as
  user-provided.** The live Bundle config screen shows "Embedded (shared)"
  (publisher), but the install screen shows the same spec as "user-provided".

This plan delivers:

1. A new **"Automatic Credentials"** section in `/credentials` (derived from
   `type == agent_api`, no new column), with workspace association at connect
   time, editable name/notes, the Sharing card kept, and the Template-sharing
   card hidden.
2. The Problem-2 fix: the root cause is a **publish-time-vs-live divergence**
   (the snapshot's `provided_by` was frozen as `"user"` when `allow_sharing` was
   still `False`; the config screen recomputes it live). The fix makes the
   publisher's intent durable and gives the publisher a clear "republish to
   apply" signal — no special-casing of `agent_api`.

```
Connect helper ──► agent_api credential (now workspace-stamped, allow_sharing=False)
                         │
   global Credentials ───┤──► "My Credentials"         (everything else)
                         └──► "Automatic Credentials"   (type == agent_api)
                         │
   publisher links it, enables allow_sharing, sets provided_by=publisher
                         │
                    PUBLISH ──► revision.required_credential_specs[i].provided_by
                         │           = resolve_provided_by(...) (snapshot, frozen)
                         ▼
   INSTALL screen reads snapshot.provided_by  ◄── must equal the config screen
```

---

## Architecture Overview

### Components touched

| Layer | Component | Change |
|-------|-----------|--------|
| Backend service | `AgentApiTokenService.connect_agent_api` | Stamp `user_workspace_id` on the auto-created credential |
| Backend service | `PublishService` (Problem 2) | Make snapshot/live agreement detectable + correct |
| Backend route | `GET /credentials` (`read_credentials`) | Optional: expose grouping signal (derive client-side; no new param required) |
| Backend route | `GET /agents/{id}/bundle-publish-status` (or existing publish endpoint) | Surface "specs are stale vs live" warning (Problem 2 UX) |
| Frontend route | `frontend/src/routes/_layout/credentials.tsx` | New "Automatic Credentials" section |
| Frontend component | `frontend/src/routes/_layout/credential/$credentialId.tsx` | Hide Template card for `agent_api` |
| Frontend component | `CredentialProvisioningSection.tsx` | "Republish to apply" hint when live ≠ snapshot |

### Key invariant (Problem 2)

The install screen renders `parsed.provided_by` **verbatim** from
`revision.required_credential_specs` (`catalog_service.py` → `parse_credential_spec`
→ `InstallContextSpec.provided_by` → `InstallServiceCredentialItem`). The config
screen (`CredentialProvisioningSection`) computes `provided_by` **live** from the
current credential row (`valueFor` → `inferredFor`, reading `cred.allow_sharing`).
These two sources must agree. They diverge whenever the credential's
`allow_sharing` / override changes **after** the last publish.

---

## Problem 2 — Root Cause (verified, not assumed)

### What the code does today

1. **Connect helper** creates the `agent_api` credential with
   `allow_sharing=False` (hardcoded — `agent_api_token_service.py:214`).
2. **Config screen** `CredentialProvisioningSection.tsx` computes the badge live:
   `valueFor(cred) = serverOverrides[cred.name] ?? inferredFor(cred)` where
   `inferredFor` returns `"publisher"` when `cred.allow_sharing` is true
   (`:154-168`). So the instant the publisher toggles `allow_sharing=True`, the
   screen shows **"Embedded (shared)"** — with no publish required.
3. **Publish** snapshots the spec via `PublishService._collect_credential_specs`
   (`:503`), which calls `resolve_provided_by(cred, install)` (`:465`). That
   function is correct: override → `allow_sharing` → `allow_template_sharing` →
   `"user"`. The result is **frozen** into `revision.required_credential_specs`.
4. **Install** reads the frozen snapshot. `catalog_service.py:274-353` faithfully
   reflects `parsed.provided_by`; `parse_credential_spec` defaults a missing /
   `"user"` value to `"user"`.

### The divergence

The screen reads **live** state; the install reads the **publish-time snapshot**.
The only way `agent_api` shows "Embedded (shared)" live yet installs as
"user-provided" is:

> **The bundle revision was published while `allow_sharing` was still `False`
> (the connect-helper default), and the publisher enabled sharing / set
> `provided_by="publisher"` afterwards without re-publishing.** The snapshot's
> `provided_by` stayed `"user"`; the live screen recomputed `"publisher"`.

This matches the background exactly: the connect helper defaults
`allow_sharing=False`, and the publisher enables sharing "afterwards" (per the
agent_api doc, §"Connect Agent API"). If the publisher had already published a
revision before flipping the flag, that revision is stale.

### Hypotheses explicitly ruled out

- **(b) override keyed by `credential.name` + rename** — `resolve_provided_by`
  reads the override by `credential.name`; a rename could miss the override.
  **But** the inference fallback still yields `"publisher"` from
  `allow_sharing=True`, so a rename alone cannot produce a `"user"` snapshot when
  sharing is on. Not the cause (though see Edge Cases — a rename can still strand
  an override key; harmless here because inference covers it).
- **(c) `agent_api` special-casing in publish/validate** — searched
  `backend/app/services/bundles/` for `agent_api` / `AGENT_API`: **no matches**.
  No special path exists. Ruled out.
- **(d) `allow_sharing` not persisting for `agent_api`** — `allow_sharing` is a
  plain `CredentialBase` column; `update_credential` persists it like any other
  type; `CredentialPublic` returns it. No type gate. Ruled out.
- **(e) config screen vs publish use different resolution** — they do (live vs
  snapshot) — **this is exactly the divergence**, but both derive from the same
  `allow_sharing`/override inputs. The bug is staleness, not a logic mismatch.

### Conclusion

Problem 2 is **not** an `agent_api`-specific bug. It is the generic
"snapshot drifts from live after the publisher changes a credential's sharing
mode" gap, made visible by the agent_api connect flow because that flow always
creates the credential with `allow_sharing=False` and relies on the publisher
flipping it *after* the fact. The correct, minimal fix has two parts:

1. **Make the publisher's intent durable at connect time where we can.** (Limited
   — we cannot know at connect time that the credential will be published as
   `publisher`.) Instead, the durable signal is the **republish requirement**.
2. **Surface the staleness** so the publisher knows the live badge won't take
   effect for installers until they republish; and (defensive) ensure the install
   path cannot silently present a `"user"` spec for a credential that is, at
   read time, demonstrably publisher-shared.

The primary fix is detection + UX (republish hint). An optional hardening
(reconcile snapshot against live at install-context build) is described as a
secondary, opt-in measure with its trade-offs.

---

## Problem 2 — The Fix

### Primary fix (minimal, correct): publish-staleness detection + republish hint

The snapshot is *supposed* to be immutable per revision — that is by design and
must not change. The real defect is that the publisher has no signal that the
live config no longer matches what installers receive. We add that signal.

**Backend — a reusable "spec drift" computation.**

Add a method on `PublishService` (single source of truth, mirrors
`resolve_provided_by`):

- `compute_credential_spec_drift(session, install) -> list[CredentialSpecDrift]`
  - For the publisher install, recompute the *would-be* spec set the same way
    `_collect_credential_specs` does (live), and diff each entry's `provided_by`
    against the **latest published revision's** `required_credential_specs`
    (snapshot).
  - Emit one `CredentialSpecDrift{ name, type, live_provided_by,
    snapshot_provided_by, drifted: bool }` per linked credential. `drifted` is
    `True` when the two differ (or when the credential is newly linked / removed
    relative to the snapshot).
  - Reuse `resolve_provided_by` for the live side and `parse_credential_spec`
    for the snapshot side so the comparison cannot disagree with publish.

**Backend — surface it.** Extend the existing bundle/publish status payload the
Bundle tab already loads (the one feeding `CredentialProvisioningSection`).
Preferred host: the install/agent detail response that already carries
`publish_settings` and `is_publisher_install`. Add a field:

- `credential_specs_stale: bool` (true if any drift) and optionally
  `credential_spec_drift: list[CredentialSpecDrift]` for per-row hinting.

If no suitable existing endpoint cleanly carries this, add a tiny read-only
endpoint:

- `GET /api/v1/agents/{agent_id}/bundle-credential-drift`
  → `{ stale: bool, drift: list[CredentialSpecDrift] }`. Publisher-install
  owner-only (404 on non-owner / non-publisher-install, no existence leak).

**Frontend.** In `CredentialProvisioningSection.tsx`, when a row's
`live_provided_by !== snapshot_provided_by` (or `credential_specs_stale`), show
an amber inline hint next to the dropdown: *"Installers still receive the
previously published setting (user-provided). Republish the bundle to apply
'Embedded (shared)'."* with the existing republish CTA. This directly resolves
the reported confusion: the publisher sees *why* the install screen disagrees.

This is the **minimal correct change**: it does not mutate immutable revisions,
does not special-case `agent_api`, and makes the contract ("publish freezes the
spec; republish to change it") explicit in the UI.

### Secondary hardening (optional, documented trade-off)

Optionally, at install-context build time
(`catalog_service.py:_build_install_context`), for a spec whose snapshot
`provided_by == "user"` but whose `publisher_credential_id`/live credential is
resolvable and currently `allow_sharing=True`, the platform *could* prefer the
live `"publisher"` resolution. **Recommendation: do NOT do this in the MVP.**
Trade-offs:

- Pro: a publisher who forgot to republish still ships a working publisher cred.
- Con: breaks the "revision is immutable" guarantee installers rely on;
  introduces a live DB read of the publisher's credential during every
  consumer's install-context build (cross-user read, perf + auth-surface
  concerns); the snapshot no longer reflects what was actually reviewed at
  publish. The publisher's `publisher_credential_id` is only snapshotted when
  `provided_by=="publisher"` at publish time, so a stale `"user"` spec has **no**
  `publisher_credential_id` to resolve from anyway — the reconcile would need a
  name+type live lookup, which is exactly the fragile path we want to avoid.

Given the above, the primary fix (republish hint) is the chosen approach; the
secondary hardening is explicitly **out of scope** and recorded as future work.

### Producer_agent_id pitfall — confirm PBP one-shared-token still correct

When a consumer installs a bundle whose `agent_api` connection credential is PBP,
the existing flow shares the **publisher's** credential row (which references the
publisher's `producer_agent_id` and carries the single shared `token` + public
`base_url`). This is intentional and already documented (one-shared-token model):
every installer's container receives `{base_url, token}` pointing at the
**publisher's** producer environment. The `producer_agent_id` inside the blob is
informational for the producer-side connection list; it is **not** used to
resolve the proxy at consume time (the proxy is keyed by `base_url` +
`agent-api/{producer_agent_id}` already baked into `base_url`). The Problem-2 fix
changes only `provided_by` detection, not the delivered payload, so it aligns
with the one-shared-token model. **Verification task** (tests below): publish an
`agent_api` PBP credential, install as a foreign user, assert the install screen
shows publisher-provided and the container receives the publisher's
`{base_url, token}`.

---

## Problem 1 — The Fix

### Chosen approach for grouping: derive from `type == AGENT_API` (no new column)

Per the brief's preference, grouping is **derived**, not stored. The global
Credentials list already returns `type` on every `CredentialPublic`. The frontend
splits the existing query result into two sections:

- **My Credentials** — `type != "agent_api"`
- **Automatic Credentials** — `type == "agent_api"`

No new DB column, no new query param, no migration for grouping. Rationale: the
distinction is intrinsic to the type; a flag would duplicate state already
implied by `type` and risk drift. (A new column would only be justified if we
needed to mark *non-`agent_api`* credentials as "automatic" — we don't.)

### Workspace association at connect time

**Problem:** the auto-created credential gets `user_workspace_id = NULL` (default
workspace) regardless of the owning agent's workspace, so it disappears under any
non-default workspace filter.

**Fix:** in `AgentApiTokenService.connect_agent_api`, pass `user_workspace_id`
into the `CredentialCreate`. `CredentialCreate` and
`CredentialsService.create_credential` already accept and persist
`user_workspace_id` (`credential.py:122`, `credentials_service.py:1299`) — only
the connect helper omits it.

**Derivation rule (documented trade-off):**

1. If `data.consumer_agent_id` is provided → use that **consumer** agent's
   `user_workspace_id`. The credential "belongs to" the agent it is linked to
   and will be used in; that agent's workspace is the natural home.
2. Else (global picker, no consumer) → use the **producer** agent's
   `user_workspace_id`. The producer is the only agent in scope; its workspace is
   the best available signal.
3. If the chosen agent's `user_workspace_id` is `NULL` → credential stays NULL
   (default workspace), unchanged from today.

Trade-offs:

- **Consumer-first** matches "the credential is configured on the consumer's
  Credentials tab and synced into the consumer's containers" (per agent_api doc
  step 4). This is the strongest ownership signal.
- A credential later **re-linked** to an agent in a different workspace will not
  auto-move. Acceptable for MVP: `user_workspace_id` is owner-editable, and the
  workspace is a *grouping* convenience, not an authorization boundary
  (credentials are owner-scoped regardless of workspace). Document this; do not
  add auto-migration of workspace on relink (out of scope).
- The owner can always change the workspace later via the credential edit
  surface (already supported through `CredentialUpdate`? — see Edge Cases:
  `CredentialUpdate` does **not** currently expose `user_workspace_id`; moving an
  existing automatic credential between workspaces is therefore out-of-scope
  unless we add it. The connect-time stamp covers the new-credential path, which
  is the reported problem.)

**Backward compatibility:** existing `agent_api` credentials created before this
change keep `user_workspace_id = NULL`. They will appear in the Automatic
Credentials section only under the default-workspace filter (or the unfiltered
"all" view). This is acceptable; a one-off backfill is optional (see Migration).

### Editable name/notes + Sharing kept + Template hidden

- **Name/notes editable.** Today the `agent_api` branch in
  `$credentialId.tsx:281-292` renders `AgentApiConnectionView` (connection panel)
  with **no** name/notes form. Add an editable Basic Information card (name +
  notes only — never the secret) above/beside the connection view, wired to the
  same `updateCredential` mutation already present in `OwnedCredentialView`.
  `CredentialUpdate` already accepts `name` and `notes`.
- **Sharing card kept.** `CredentialSharing` stays (already rendered).
- **Template card hidden.** Remove `CredentialTemplateSharing` from the
  `agent_api` branch — template sharing makes no sense for a connection
  credential (there are no user-fillable "private fields"; the only data are the
  proxy URL + opaque token). Replace the 2-column `grid` with a single Sharing
  card for `agent_api`.

---

## Data Models

No new tables. No required new columns.

### Reused existing fields

- `Credential.user_workspace_id` (UUID | None, FK → `user_workspace.id`,
  `ON DELETE SET NULL`) — already present; now populated by the connect helper.
- `Credential.type == CredentialType.AGENT_API` — the grouping discriminator.

### New response schema (Problem 2 detection)

`CredentialSpecDrift` (Pydantic, non-table) in
`backend/app/models/bundles/...` (alongside publish-status models) or inline in
`backend/app/models/credentials/credential.py`:

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Credential name (spec key) |
| `type` | `str` | Credential type value |
| `live_provided_by` | `"user"\|"publisher"\|"template"` | Recomputed via `resolve_provided_by` |
| `snapshot_provided_by` | `"user"\|"publisher"\|"template"` | From latest revision spec; `"user"` when absent |
| `drifted` | `bool` | `live != snapshot` or added/removed |

Optional wrapper `BundleCredentialDrift{ stale: bool, drift: list[CredentialSpecDrift] }`.

Re-export any new model from `backend/app/models/__init__.py`.

---

## Security Architecture

- **No new secret exposure.** The Automatic Credentials section and the editable
  name/notes form never surface `token`; redaction and the existing
  `CredentialPublic` projection (no `credential_data`) are unchanged.
- **Owner-scoping preserved.** `read_credentials` remains
  `owner_id == current_user.id`. Grouping is purely presentational; it does not
  widen visibility.
- **Drift endpoint authz.** Publisher-install owner-only; 404 (not 403) on
  non-owner / non-publisher-install to avoid existence leaks, consistent with
  `AgentApiTokenService._verify_agent_ownership` and the deletion-impact
  endpoints.
- **Workspace is not an auth boundary.** Stamping `user_workspace_id` changes
  grouping/filtering only; credentials stay owner-private regardless of
  workspace. No access-control change.

---

## Backend Implementation

### 1. Connect-time workspace stamping

File: `backend/app/services/agent_api/agent_api_token_service.py`
(`connect_agent_api`, around `:208-224`).

- Resolve `workspace_id`:
  - if `data.consumer_agent_id`: load that agent, use its `user_workspace_id`;
  - else: use the producer `agent.user_workspace_id`.
- Pass `user_workspace_id=workspace_id` into `CredentialCreate(...)`.
- Keep `allow_sharing=False` (unchanged — the publisher enables it later).

No signature change to `create_credential` (already accepts the field).

### 2. Problem-2 drift detection

File: `backend/app/services/bundles/publish_service.py`.

- Add `compute_credential_spec_drift(session, install) -> list[CredentialSpecDrift]`:
  - Recompute live specs via the same logic as `_collect_credential_specs`
    (factor the per-credential `provided_by` resolution out so both reuse
    `resolve_provided_by`).
  - Load the latest revision (`install` → bundle → `latest_revision_id` →
    `required_credential_specs`), parse via `parse_credential_spec`, index by
    `(name)`.
  - Diff and emit `CredentialSpecDrift` rows.

File: route — extend the publisher-install detail payload (preferred) **or** add
`GET /api/v1/agents/{agent_id}/bundle-credential-drift` in
`backend/app/api/routes/installs.py`, owner-only, returning
`BundleCredentialDrift`.

### 3. (No change) publish resolution

`resolve_provided_by`, `_collect_credential_specs`, `_validate_publisher_provides`
remain correct and unchanged. Confirm via tests that an `agent_api` credential
with `allow_sharing=True` snapshots as `provided_by="publisher"`.

---

## Frontend Implementation

### 1. Automatic Credentials section — `frontend/src/routes/_layout/credentials.tsx`

- Keep the single `["credentials", workspaceFilter]` query (one fetch).
- After fetch, partition:
  - `automatic = data.filter(c => c.type === "agent_api")`
  - `mine = data.filter(c => c.type !== "agent_api")`
- Render three stacked sections:
  1. **My Credentials** (`mine`) — existing grid; empty-state unchanged but
     computed from `mine`.
  2. **Automatic Credentials** (`automatic`) — new section, same
     `CredentialCard` grid, with a one-line explainer ("Connections created by
     'Connect Agent API'. Manage name, notes, and sharing here."). Hide the
     whole section when `automatic.length === 0`.
  3. **Shared With Me** — unchanged.
- Respects the workspace filter automatically (same query). New automatic
  credentials now carry the owning agent's workspace, so they appear under that
  workspace's filter.

Refactor note: `CredentialsGrid` currently owns the query and the empty state.
Lift the query to `Credentials()` (or pass the partitioned list down) so both
sections share one fetch and the empty-state logic is per-section. Keep the
existing `key={activeWorkspaceId}` remount behavior.

### 2. Credential detail — `frontend/src/routes/_layout/credential/$credentialId.tsx`

In the `credential.type === "agent_api"` branch (`:281-292`):

- Add an editable **Basic Information** card (name + notes only), reusing the
  existing `form` + `updateCredential` mutation already defined in
  `OwnedCredentialView`. (The form/mutation already exist for other types; the
  `agent_api` branch currently returns early before rendering them — render a
  name/notes-only card here.)
- Keep `AgentApiConnectionView` (connection panel + View Spec).
- Keep `CredentialSharing`.
- **Remove** `CredentialTemplateSharing`; render Sharing full-width (drop the
  2-column grid for this type).

### 3. Republish hint — `CredentialProvisioningSection.tsx`

- Consume `credential_specs_stale` / per-row drift (from the extended detail
  payload or the new drift endpoint).
- When a row drifts, render an amber inline note + reuse the existing republish
  CTA. Pure presentational; no behavior change to the override save.

### Client regeneration

Backend response changes (the `CredentialSpecDrift` field / endpoint) require
regenerating the OpenAPI client:

```
source ./backend/.venv/bin/activate && make gen-client
```

The Problem-1 frontend changes need **no** client regen (they use existing
`type` and `updateCredential`). Only the Problem-2 drift surface needs it.

---

## Database Migrations

- **Grouping:** none (derived from `type`).
- **Workspace stamping:** none (column exists).
- **Optional backfill (recommended, low-risk):** a data-only migration to set
  `user_workspace_id` on existing `agent_api` credentials to the workspace of the
  single agent they are linked to (when exactly one link exists and that agent
  has a workspace). Leave NULL when ambiguous (zero or multiple links, or linked
  agent in default workspace). Downgrade is a no-op (cannot reliably reverse).
  If skipped, legacy automatic credentials simply show under the default-
  workspace / unfiltered view — acceptable.

No schema migration is strictly required for this feature.

---

## Error Handling & Edge Cases

- **Agent renamed → override key strands.** `resolve_provided_by` reads the
  override by `credential.name`; renaming the credential orphans the override
  entry. For `agent_api` this is harmless (inference from `allow_sharing=True`
  still yields `"publisher"`), but note it generally. Out of scope to fix here;
  the drift hint will still flag any resulting mismatch.
- **Connect with consumer agent in a different workspace than producer.** We
  prefer the consumer's workspace (rule 1). If the consumer later moves
  workspaces, the credential does not follow (documented limitation).
- **Legacy `agent_api` credentials (NULL workspace).** Appear under default /
  unfiltered view only. Optional backfill addresses this.
- **Template card removal must not break existing data.** Hiding the card is
  presentational; any `agent_api` credential that somehow had
  `allow_template_sharing=True` is left untouched in DB (won't be publishable as
  template because `_validate_publisher_provides` only enforces, and the connect
  helper never sets it). Consider a guard so `agent_api` cannot be set
  `provided_by="template"` in `CredentialProvisioningSection` (optional
  hardening; the dropdown can omit the Template option when `type==="agent_api"`).
- **Drift endpoint on a never-published install.** No latest revision → treat
  every linked credential as drift=`False` (nothing to be stale against) or omit;
  the hint only matters once a revision exists.
- **Install of a stale revision (the actual Problem-2 symptom).** Until the
  publisher republishes, installers still receive the snapshot (`user`). This is
  correct behavior given immutable revisions; the fix is the publisher-side hint,
  not silently changing what installers get.

---

## UI/UX Considerations

- Automatic Credentials section: distinct heading + short explainer; reuse
  `CredentialCard` so cards look consistent. Consider a small "Automatic" / link
  icon badge on the card for `agent_api` type (optional).
- Republish hint: amber, non-blocking, with the existing republish action;
  copy must name both states ("now: user-provided → after republish: embedded
  (shared)").
- agent_api detail: name/notes editable; the proxy token is never shown
  (unchanged); Sharing card explains cross-user sharing is safe (narrowed proxy).

---

## Integration Points

- **Agent Credentials / Sharing** — `agent_api` continues to ride the credential
  sync / whitelist / redaction / `CredentialShare` pipeline unchanged.
- **Agent Bundles** — `resolve_provided_by` stays the single source of truth;
  the drift computation reuses it and `parse_credential_spec` so publish and the
  hint cannot disagree.
- **User Workspaces** — connect helper now stamps `user_workspace_id`; grouping
  filter already supported by `read_credentials`.
- **One-shared-token PBP model** — unchanged; Problem-2 fix touches detection,
  not the delivered `{base_url, token}` payload.
- **Client regen** — required only for the Problem-2 drift surface.

---

## Tests

Follow `backend/tests/README.md` (API-only, scenario-based, no direct DB access;
check `tests/api/agents/README.md`). Suggested file:
`backend/tests/api/agents/agents_agent_api_automatic_credentials_test.py`
(and extend `agents_bundles_*` for publish/install).

### Problem 2 — publish→install provided_by for agent_api

1. **Shared agent_api snapshots as publisher.** Enable `agent_api` on producer,
   connect (creates `agent_api` cred), set `allow_sharing=True`, link to the
   publisher install, publish → assert the latest revision's
   `required_credential_specs` entry has `provided_by="publisher"` and a
   `publisher_credential_id`.
2. **Foreign install sees publisher-provided.** Foreign user fetches the install
   context → assert the `agent_api` spec `provided_by="publisher"`, not `"user"`.
3. **Stale-before-share reproduction (the bug).** Publish while
   `allow_sharing=False` (snapshot `"user"`), then enable sharing without
   republishing → assert the drift endpoint/field reports `stale=True` with
   `live_provided_by="publisher"`, `snapshot_provided_by="user"`; and the foreign
   install context still shows `"user"` (immutable revision) until republish.
4. **Republish clears drift.** After republish → drift `stale=False` and install
   context shows `"publisher"`.
5. **One-shared-token delivery.** Foreign install of the PBP agent_api bundle →
   assert the installer's linked credential resolves to the publisher's
   `{base_url, token}` (publisher's row shared), consistent with the model.

### Problem 1 — connect-time workspace + list grouping

6. **Connect with consumer agent → consumer workspace.** Producer + consumer in
   workspace W; connect with `consumer_agent_id` → the created `agent_api`
   credential has `user_workspace_id == W`.
7. **Connect from global picker → producer workspace.** No `consumer_agent_id`,
   producer in workspace W → credential `user_workspace_id == W`.
8. **Workspace filter surfaces it.** `GET /credentials?user_workspace_id=W`
   returns the automatic credential; `?user_workspace_id=` (default) does not
   (when W is non-default).
9. **Grouping signal.** `GET /credentials` returns the `agent_api` credential
   with `type=="agent_api"` so the frontend can partition (assert via response,
   since grouping is client-side).

### Frontend (manual / component-level)

- Automatic Credentials section renders only `agent_api` creds; hidden when none.
- agent_api detail: name/notes editable & persist; Sharing card present;
  Template card absent.
- Republish hint appears when live ≠ snapshot.

---

## Docs to Update

- `docs/agents/agent_api/agent_api.md` — connect helper now stamps the owning
  agent's `user_workspace_id`; global Credentials shows agent_api under
  "Automatic Credentials"; detail page has editable name/notes, keeps Sharing,
  drops Template-sharing.
- `docs/agents/agent_api/agent_api_tech.md` — `connect_agent_api` workspace
  derivation rule; the drift computation; new response field/endpoint.
- `docs/agents/agent_credentials/credential_sharing.md` /
  `credential_sharing_tech.md` — document the publish-vs-live drift hint and that
  `provided_by` is frozen at publish (republish to change); note `agent_api`
  cannot be template-provided.
- `docs/agents/agent_bundles/agent_bundles.md` /
  `agent_bundles_tech.md` — republish requirement to apply a changed
  `provided_by`; the drift surface.
- `docs/README.md` — update the `agent_api` and `agent_credentials` registry
  blurbs if the "Automatic Credentials" concept warrants a glossary line.

---

## Out of Scope / Future Work

- **Live reconcile of stale snapshots at install** (secondary hardening) — keep
  revisions immutable; only the republish hint is shipped.
- **Auto-moving an existing credential's workspace on relink** — connect-time
  stamp only; no relink migration.
- **Exposing `user_workspace_id` in `CredentialUpdate`** so owners can move an
  automatic credential between workspaces from the UI — possible follow-up; not
  required to fix the reported problem.
- **Per-install agent_api token isolation** — unchanged known gap (agent_api
  doc §"Known Gaps").
- **A dedicated `is_automatic` column** — rejected; grouping derives from `type`.

---

## Summary Checklist

**Backend**
- [ ] `connect_agent_api`: derive `user_workspace_id` (consumer-first, else
      producer) and pass into `CredentialCreate`.
- [ ] `PublishService.compute_credential_spec_drift(...)` reusing
      `resolve_provided_by` + `parse_credential_spec`.
- [ ] Surface drift: extend publisher-install detail payload **or** add
      `GET /agents/{agent_id}/bundle-credential-drift` (owner-only, 404 leak-safe).
- [ ] `CredentialSpecDrift` (+ optional `BundleCredentialDrift`) model;
      re-export from `models/__init__.py`.
- [ ] (Optional) data-only backfill migration for legacy agent_api workspaces.
- [ ] (Optional) reject `provided_by="template"` for `agent_api` in
      `update_publish_settings` validation.

**Frontend**
- [ ] `credentials.tsx`: single query, partition into My / Automatic / Shared;
      hide Automatic when empty.
- [ ] `$credentialId.tsx` agent_api branch: add editable name/notes card; keep
      Sharing; remove Template-sharing; single-column layout.
- [ ] `CredentialProvisioningSection.tsx`: amber "republish to apply" hint on
      drift; optionally omit Template option for agent_api.
- [ ] `make gen-client` after the drift backend change.

**Tests & validation**
- [ ] Publish→install provided_by for shared agent_api = publisher.
- [ ] Drift reported when sharing enabled post-publish; cleared after republish.
- [ ] One-shared-token delivery intact for PBP agent_api.
- [ ] Connect-time workspace = consumer (with consumer) / producer (global).
- [ ] Workspace filter surfaces automatic credentials; grouping by type.
- [ ] Manual: Automatic section, editable name/notes, hidden Template card,
      republish hint.
```
