# Bundle Permissions Management — Implementation Plan

Unify the two silo'd publisher access surfaces — bundle catalog access
(`BundleAccessGrant`, gated by `AgentBundle.visibility == "users"`) and producer
Agent-REST-API per-user capability scopes (`agent_api_access_grant`, gated by
`Agent.agent_api_identity_enabled`) — into **one publisher-facing "Permissions
management" card** on the Bundle tab. The card is a single unified table (one row
per user) plus one "Add user" modal that fans out to the existing,
already-authorized mutation endpoints.

> **No new database tables. No Alembic migration.** This feature is a
> read-aggregation layer + UI over two already-modeled systems. Every write
> reuses an existing, already owner-gated endpoint untouched.

---

## 1. Overview

### Problem

A bundle publisher shipping a "complex" agent (e.g. a Billing Assistant that
depends on a producer agent's Agent REST API with per-user scopes) must manage
access in **two** places today:

1. **Bundle catalog access** — `BundleAccessGrant` rows, surfaced inline in
   `AgentBundleTab.tsx` (the `UserAllowlistPicker` shown only when
   `bundle.visibility === "users"`).
2. **Producer capability scopes** — `agent_api_access_grant` rows, surfaced on
   the **producer agent's own** Integrations → Agent REST API → Access & Scopes
   card (`AgentApiAccessScopesCard.tsx`), gated by `agent_api_identity_enabled`.

Sharing with a new user means remembering to visit the producer agent and grant
scopes there too. Silo'd, error-prone, doesn't scale.

### Proposed design

A new **"Permissions management"** card on the Bundle tab, rendered only on the
publisher's working install (`is_publisher_install === true`, `require_developer`
gated — same place the existing Grants picker and `CredentialProvisioningSection`
live), shown when **either**:

- `bundle.visibility === "users"` (bundle-level allowlist active), **or**
- the install has ≥1 linked `agent_api` credential whose producer
  `Agent.agent_api_identity_enabled === true`.

When the trigger is true, this card **replaces** the old inline Grants
`UserAllowlistPicker` in `AgentBundleTab.tsx` (the new card subsumes
bundle-access management as one column). The visibility `Select` stays in the
Bundle settings card.

### Core capabilities

- One unified table: one row per relevant user, with a **Bundle access** column
  (when `visibility === "users"`) and one **scope column per manageable
  identity-enabled producer**.
- Read-only "Managed by `<owner email>`" entries for connected producers the
  publisher does **not** own (their grants are genuinely unreadable —
  `AgentApiGrantService.list_grants` is owner-gated).
- One "Add user" modal: pick a user, optionally grant bundle access, and assign
  scopes per manageable producer — fanning out to existing endpoints with
  per-section partial-success reporting.
- Independent per-section removal (no surprise cascade).

### High-level flow

```
Publisher install (Agent, is_publisher_install=true)
   │
   │  GET /agents/{id}/bundle-permissions-overview   (NEW, read-only aggregator)
   ▼
BundlePermissionsService.build_overview()
   ├── bundle grants     ← BundleService.list_grants(bundle)        (visibility=="users")
   ├── connected producers ← AgentApiTokenService.list_connected_producers()  (NEW method)
   │        for each identity-enabled producer the caller can manage:
   │            ├── AgentApiGrantService.list_grants(producer)       (owner-gated, reused)
   │            └── AgentApiGrantService.get_scope_catalog(producer) (owner-gated, reused)
   └── union of users (resolved display info)
   ▼
BundlePermissionsCard.tsx  →  unified table + "Add user" modal
   │
   │  writes fan out to EXISTING endpoints (no new write surface):
   ├── POST/DELETE /bundles/{uuid}/grants               (bundle access)
   └── POST/PUT/DELETE /agents/{producerId}/agent-api/grants  (producer scopes)
```

---

## 2. Architecture Overview

### Components

| Layer | New / Changed | File |
|-------|---------------|------|
| Model (no table) | NEW response schemas | `backend/app/models/bundles/bundle_permissions.py` |
| Service | NEW orchestrator | `backend/app/services/bundles/bundle_permissions_service.py` |
| Service | NEW read method | `AgentApiTokenService.list_connected_producers()` in `agent_api_token_service.py` |
| Route | NEW aggregator | `GET /agents/{agent_id}/bundle-permissions-overview` in `backend/app/api/routes/installs.py` |
| Frontend | NEW card | `frontend/src/components/Agents/BundlePermissionsCard.tsx` |
| Frontend | NEW modal | `frontend/src/components/Agents/BundlePermissionsAddUserModal.tsx` |
| Frontend | CHANGED — remove inline grants, mount card | `frontend/src/components/Agents/AgentBundleTab.tsx` |

### Integration points with existing systems

- **Agent Bundles** (`docs/agents/agent_bundles/agent_bundles.md`) — reuses
  `BundleService.list_grants / grant_access / revoke_grant` and the
  `BundleAccessGrant` / `BundleAccessGrantPublic` models verbatim. The aggregator
  route mirrors the existing `GET /agents/{id}/bundle-credential-drift`
  publisher-install-only, 404-leak-safe pattern in `installs.py`.
- **Agent REST API** (`docs/agents/agent_api/agent_api.md`) — reuses
  `AgentApiGrantService` (owner-gated CRUD, scope catalog, `to_public`) and the
  `agent_api_access_grant` table **completely unchanged**. The producer's own
  Access & Scopes card (`AgentApiAccessScopesCard.tsx`) stays untouched and
  working — the new card is additive owner-gated convenience.
- **User Selector Pattern** (`docs/development/frontend/user_selector_pattern.md`)
  — the Add-user modal reuses `UserAllowlistPicker` + `UsersService.searchUsers`
  exactly like `AgentApiAccessScopesCard` and the existing bundle grant picker.

---

## 3. Data Models

**No database tables are created or modified. No migration.**

All new models are **non-table** SQLModel response schemas in a new file
`backend/app/models/bundles/bundle_permissions.py`, re-exported from
`backend/app/models/__init__.py`.

### `BundlePermissionScopeCatalogEntry`

Projection of one `policy.yaml` catalog scope for the modal's quick-add chips.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Scope name |
| `description` | `str \| None` | Optional human description |

### `BundlePermissionGrant`

Per-user scope state on one producer (minimal; display info comes from the
top-level `users` union to avoid N duplicate user resolutions).

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | The granted user |
| `grant_id` | `uuid.UUID` | The `agent_api_access_grant.id` (delete/edit key) |
| `scopes` | `list[str]` | Current scope names |

### `BundlePermissionProducer`

One connected, identity-enabled producer the install consumes via an `agent_api`
credential. Manageable producers carry catalog + grants; non-manageable ones
carry neither (read-only "Managed by" entry).

| Field | Type | Notes |
|-------|------|-------|
| `producer_agent_id` | `uuid.UUID` | Producer (Agent A) id |
| `producer_agent_name` | `str \| None` | For the column header |
| `producer_ui_color_preset` | `str \| None` | For the `AgentBadge` |
| `credential_id` | `uuid.UUID` | The `agent_api` connection credential on this install |
| `credential_name` | `str \| None` | Connection label |
| `identity_enabled` | `bool` | `producer.agent_api_identity_enabled` (always `true` for listed producers) |
| `can_manage` | `bool` | Caller owns the producer **or** is superuser |
| `owner_email` | `str \| None` | Producer owner's email (for "Managed by `<email>`") |
| `scope_catalog` | `list[BundlePermissionScopeCatalogEntry]` | Non-empty only when `can_manage` |
| `grants` | `list[BundlePermissionGrant]` | Populated only when `can_manage`; **always `[]` for non-manageable producers** (owner-gated read never runs) |

### `BundlePermissionUser`

The resolved display info for every user appearing anywhere in the union
(bundle grant or any producer grant). Drives the table rows and supplies
`fallbackLabel` for pills.

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | |
| `email` | `str \| None` | |
| `full_name` | `str \| None` | |
| `bundle_grant_id` | `uuid.UUID \| None` | Set when the user has a `BundleAccessGrant` (the revoke key) |

### `BundlePermissionsOverview` (top-level response)

| Field | Type | Notes |
|-------|------|-------|
| `bundle_uuid` | `uuid.UUID \| None` | `None` before first publish |
| `visibility` | `str \| None` | `bundle.visibility` (`private` / `users` / `public`) |
| `bundle_access_applicable` | `bool` | `visibility == "users"` — drives whether the Bundle access column renders |
| `bundle_grants` | `list[BundleAccessGrantPublic]` | Reuses the existing model (carries `user_id` + `user_email`) |
| `producers` | `list[BundlePermissionProducer]` | Identity-enabled connected producers only |
| `users` | `list[BundlePermissionUser]` | Server-computed union of all users in the table |
| `show_card` | `bool` | `bundle_access_applicable or len(producers) > 0` |

### Security considerations (model layer)

- The overview carries **no secrets** — only scope names, emails, agent
  names/colors, and grant ids. `agent_api` `credential_data` (which holds the
  proxy `token`) is decrypted **only** to read `producer_agent_id` and is never
  serialized into the response.
- `grants` and `scope_catalog` are populated **only** for producers where
  `can_manage` is true. For non-owned producers the owner-gated read paths are
  never invoked, so the publisher cannot learn another owner's scope state.

---

## 4. Security Architecture

### Hard requirements (from the feature brief)

1. **No change to `agent_api_access_grant` authorization semantics.** A bundle
   publisher who does not own a connected producer must never read or write that
   producer's grants through this surface. Enforced structurally: the aggregator
   only calls `AgentApiGrantService.list_grants` / `.get_scope_catalog` for
   producers where `can_manage` is true (caller owns the producer or is
   superuser). For non-manageable producers it returns `grants=[]`,
   `scope_catalog=[]`, and surfaces the producer as a read-only "Managed by"
   entry. Writes go to the existing `POST/PUT/DELETE /agents/{producerId}/agent-api/grants`
   routes, which are **already** owner-gated via
   `AgentApiService.resolve_agent_only` (404, no existence leak) — unchanged.
2. **`AgentApiAccessScopesCard.tsx` stays untouched** on the producer's own
   Integrations tab.

### Access control on the new aggregator route

- `Depends(require_developer)` — publisher UI only.
- Publisher-install-owner-only, mirroring `get_bundle_credential_drift`:
  load `Agent`, then `404` (not 403) when `install is None`, the caller is not
  the owner and not a superuser, or `not install.is_publisher_install`. This
  avoids leaking the existence of a bundle to non-publishers.

### Caller-can-manage resolution

`can_manage = is_superuser or producer.owner_id == current_user.id`. Computed in
`AgentApiTokenService.list_connected_producers`. This is the **only** authority
that gates editable scope columns; the read of the publisher's *own* install's
linked credentials is always allowed (they are the publisher's credentials).

### Sensitive data handling

- The proxy `token` inside each `agent_api` credential is never read into the
  response (only `producer_agent_id` is).
- Producer owner email is surfaced for disambiguation — consistent with the
  existing producer Connections list, which already shows consumer-owner emails.

---

## 5. Backend Implementation

### 5.1 New service method — discover connected producers

`backend/app/services/agent_api/agent_api_token_service.py`, alongside
`list_producer_connections` (the inverse direction: producer → consumers).

```
@staticmethod
def list_connected_producers(
    session: Session,
    consumer_agent_id: uuid.UUID,
    user_id: uuid.UUID,
    is_superuser: bool = False,
) -> list[ConnectedProducerRow]
```

Where `ConnectedProducerRow` is a lightweight internal dataclass / dict carrying
`{producer_agent_id, producer_agent_name, producer_ui_color_preset,
credential_id, credential_name, identity_enabled, can_manage, owner_email}`.

**Logic:**

1. Enumerate the consumer agent's linked credentials via
   `CredentialsService.get_agent_credentials(session, consumer_agent_id)`,
   filtered to `type == CredentialType.AGENT_API`.
2. For each, `CredentialsService.decrypt_credential_data(...)` and read
   `producer_agent_id` (string → `uuid.UUID`). Skip malformed/missing.
3. `session.get(Agent, producer_agent_id)` → producer. Skip if `None`
   (dangling connection).
4. **Dedupe by `producer_agent_id`** (keep the first credential; multiple
   credentials pointing at the same producer is unusual but possible).
5. Annotate `identity_enabled = producer.agent_api_identity_enabled`,
   `can_manage = is_superuser or producer.owner_id == user_id`,
   `owner_email = session.get(User, producer.owner_id).email`.
6. **Return only producers where `identity_enabled` is true** (scope management
   is meaningless otherwise; a connected producer with identity OFF has no scope
   concept and is not surfaced). Note in code that an owner who wants scopes on a
   currently-OFF producer enables identity on that producer's own Integrations
   tab first.

Read-only; never mutates; never raises on a single bad credential (logs + skips).

### 5.2 New orchestrator service

`backend/app/services/bundles/bundle_permissions_service.py` —
`BundlePermissionsService.build_overview(session, install, current_user) ->
BundlePermissionsOverview`. Keeps the cross-domain assembly in one place so the
route stays thin.

**Logic:**

1. Resolve the bundle: `bundle = BundleService.get_bundle_by_uuid(session,
   install.bundle_uuid)` when `install.bundle_uuid` is set, else `None`.
   `visibility = bundle.visibility if bundle else None`,
   `bundle_access_applicable = visibility == "users"`.
2. **Bundle grants** — when `bundle_access_applicable`,
   `BundleService.list_grants(session, bundle)` → project each via the existing
   `_grant_to_public`-equivalent into `BundleAccessGrantPublic` (move/share that
   projection helper to the service, or reuse `BundleService`). Else `[]`.
3. **Connected producers** —
   `AgentApiTokenService.list_connected_producers(session, install.id,
   current_user.id, current_user.is_superuser)`.
4. For each producer:
   - If `can_manage`: `AgentApiGrantService.list_grants(...)` (→ map to
     `BundlePermissionGrant{user_id, grant_id, scopes}`) and
     `AgentApiGrantService.get_scope_catalog(...)` (→ `scope_catalog`). Both are
     owner-gated and will succeed because `can_manage` already implies ownership
     / superuser.
   - Else: `grants=[]`, `scope_catalog=[]` (read never runs).
5. **Users union** — collect user ids from bundle grants + every manageable
   producer's grants; resolve `User` rows once (`email`, `full_name`); set
   `bundle_grant_id` from the matching bundle grant when present.
6. `show_card = bundle_access_applicable or len(producers) > 0`.

### 5.3 New aggregator route

`backend/app/api/routes/installs.py` (router prefix `/agents`, tag `installs` —
so the generated client lands on `InstallsService`, matching
`getBundleCredentialDrift`). Place directly after `get_bundle_credential_drift`.

```
@router.get(
    "/{agent_id}/bundle-permissions-overview",
    response_model=BundlePermissionsOverview,
    dependencies=[Depends(require_developer)],
)
def get_bundle_permissions_overview(agent_id, session, current_user) -> BundlePermissionsOverview
```

- Load `Agent`; `404` when missing, not owned (and not superuser), or
  `not is_publisher_install` — identical guard to `get_bundle_credential_drift`.
- Delegate to `BundlePermissionsService.build_overview(session, install,
  current_user)`.

**No new write routes.** Create / update / delete reuse:

| Action | Existing endpoint (unchanged) |
|--------|-------------------------------|
| Grant bundle access | `POST /bundles/{bundle_uuid}/grants` (`{email}`) |
| Revoke bundle access | `DELETE /bundles/{bundle_uuid}/grants/{grant_id}` |
| Create producer scopes | `POST /agents/{producerId}/agent-api/grants` (`{user_id, scopes}`) |
| Update producer scopes | `PUT /agents/{producerId}/agent-api/grants/{grant_id}` (`{scopes}`) |
| Remove producer scopes | `DELETE /agents/{producerId}/agent-api/grants/{grant_id}` |

### 5.4 Model re-export

Add the new schemas to `backend/app/models/__init__.py` so
`from app.models import BundlePermissionsOverview` works and the route's
`response_model` resolves.

---

## 6. Frontend Implementation

### 6.1 New card — `BundlePermissionsCard.tsx`

`frontend/src/components/Agents/BundlePermissionsCard.tsx`. Props: `{ agent:
AgentPublic; bundleUuid: string; visibility: string }`.

**Query:** `["bundlePermissionsOverview", agent.id]` →
`InstallsService.getBundlePermissionsOverview({ agentId: agent.id })`, enabled
when `agent.is_publisher_install && !!agent.bundle_uuid`.

**Render:** nothing when `overview.show_card === false`. Otherwise a `Card`
("Permissions management") containing a unified table:

- **Columns:**
  - **User** — `full_name` / `email` from the `users` union.
  - **Bundle access** — only when `overview.bundle_access_applicable`; a
    granted/not chip with an `X` to revoke (calls `revokeGrant`), or a muted
    "—" when not granted. (When `bundle_access_applicable` is false, this column
    is omitted entirely — covers a PUBLIC bundle where the publisher only wants
    producer-side scope control.)
  - **One column per `producers[]`:**
    - `can_manage === true` → editable scope chips for that user on that
      producer (join `producers[].grants` by `user_id`); chips reuse the
      catalog-suggestion + free-text pattern from `AgentApiAccessScopesCard`. An
      `X` on the chip-set removes the producer grant
      (`AgentApiService.deleteAgentApiGrant({ agentId: producer_agent_id,
      grantId })`); clicking the cell opens the modal in edit mode for that
      (user, producer).
    - `can_manage === false` → a **disabled / read-only** column header
      "Managed by `<owner_email>`" with no per-user data merged in, and a short
      caption that this producer's scopes are managed on its own page.
- **Degradation** (no special-casing needed — falls out of the column logic):
  - `visibility === "users"` but no identity-managed producers → only the Bundle
    access column (relabeled "just bundle access").
  - `visibility !== "users"` but identity-managed producers connected → scope
    columns only, no Bundle access column.
- **"Add user" button** → opens `BundlePermissionsAddUserModal`.

**Degraded altitude note:** when only the Bundle access column is present, the
card is visually equivalent to today's inline grants picker — this is the
intended "subsume, don't regress" behavior.

### 6.2 New modal — `BundlePermissionsAddUserModal.tsx`

1. **User picker** — `UserAllowlistPicker` (single selection, `enabled={open}`),
   `excludeUserIds` = users already in the table, `onAdd`/`onRemove` set a single
   `selectedUser`. (Do **not** pass `includeSelf` — bundle access to the
   publisher themselves is rejected by the backend; for producer-owner-self
   scopes the publisher uses the producer's own card.)
2. **Bundle access checkbox** — rendered only when
   `overview.bundle_access_applicable`; default **checked**.
3. **One scope-multiselect block per manageable producer** (iterate
   `producers.filter(p => p.can_manage)`): catalog quick-add chips
   (`producer.scope_catalog`) + free-text add + removable assigned chips —
   lifted from the `GrantDialog` body in `AgentApiAccessScopesCard.tsx`.
4. **Submit** fires, per section, in sequence:
   - If bundle-access checked → `BundlesService.addGrant({ bundleUuid,
     requestBody: { email: selectedUser.email } })`.
   - For each manageable producer with ≥1 selected scope →
     `AgentApiService.createAgentApiGrant({ agentId: producer.producer_agent_id,
     requestBody: { user_id: selectedUser.userId, scopes } })`.
   - Collect a per-section result (`{ section, ok, error }`); report
     partial success/failure via per-section inline error state + toasts (see
     Decision 2 in §9). On all-success, invalidate
     `["bundlePermissionsOverview", agent.id]` and close.
5. **Submit disabled** until at least one of `{bundle access checked, any scope
   selected in any producer block}` (Decision 3).

### 6.3 Changes to `AgentBundleTab.tsx`

- **Remove** the inline Grants `UserAllowlistPicker` block (the
  `bundle.visibility === "users"` block, ~lines 376–392) and its
  `addGrantMutation` / `revokeGrantMutation` / `grants` query — they move into
  the new card. The visibility `Select` stays.
- **Mount** `BundlePermissionsCard` full-width in the publisher-install section,
  next to / below `CredentialProvisioningSection` (it is already guarded by
  `agent.is_publisher_install`). Pass `bundleUuid={agent.bundle_uuid}` and
  `visibility={bundle.visibility}`.

### 6.4 State management / React Query

- New query key: `["bundlePermissionsOverview", agentId]`.
- After **any** mutation (bundle or producer), invalidate
  `["bundlePermissionsOverview", agentId]`. Also invalidate the existing
  `["agentApiGrants", producerId]` (so the producer's own Access & Scopes card
  stays consistent if open) and `["bundles", bundleUuid, "grants"]`.
- All API calls via the auto-generated client (`InstallsService`,
  `BundlesService`, `AgentApiService`); never hand-edit `src/client`.

### 6.5 User flows

- **Empty state** — card with the table header and an "Add user to manage their
  bundle access and producer scopes" hint.
- **Add user** — modal → pick user → toggle bundle access + scopes → Submit →
  per-section result → table refreshes.
- **Edit a user's producer scopes** — click that producer cell → modal in edit
  mode (user fixed, scope block prefilled) → `updateAgentApiGrant`.
- **Remove** — independent per-section `X` (bundle chip → `revokeGrant`;
  producer chip-set → `deleteAgentApiGrant`). No cascade (Decision 1).
- **Loading / error** — overview query `isLoading` → skeleton row; per-section
  mutation error → inline red text on that section + toast.

---

## 7. Database Migrations

**None.** No tables created or altered. Explicitly: this feature is a
read-aggregation + UI layer over `bundle_access_grant` and
`agent_api_access_grant`, both already modeled and migrated. No Alembic revision
is authored.

---

## 8. Error Handling & Edge Cases

| Scenario | Handling |
|----------|----------|
| Bundle access grant succeeds, a producer scope `POST` 403s mid-flow (ownership changed) | Per-section result map; the failed producer section shows inline error + toast; the successful bundle grant is **not** rolled back (no cross-domain transaction). Overview re-query reflects reality. (Decision 2) |
| `agent_api` credential decrypts but `producer_agent_id` is missing/garbage | `list_connected_producers` logs + skips that credential; never raises. |
| Connected producer agent was deleted (dangling credential) | `session.get(Agent, ...)` is `None` → producer skipped. |
| Producer connected but `agent_api_identity_enabled === false` | Not surfaced (no scope concept). Publisher enables identity on the producer's own page first. |
| Producer connected but not owned by the publisher | Surfaced as read-only "Managed by `<owner_email>`"; `grants`/`scope_catalog` empty; owner-gated reads never run. |
| `POST /bundles/{uuid}/grants` to the publisher themselves | Existing endpoint returns 400 ("Cannot grant access to the bundle's own publisher"); modal surfaces it. Picker omits `includeSelf`. |
| Duplicate producer grant (user already granted) | Existing `create_grant` returns 409; modal surfaces "edit it instead". Add-modal already excludes existing-table users via `excludeUserIds`, so this is an edge race. |
| Non-publisher / non-owner hits the aggregator route | `404` (leak-safe), mirroring `bundle-credential-drift`. |
| Bundle not yet published (`bundle_uuid` is null) | Card query disabled; `show_card` effectively false. |
| Submit with zero selections | Submit button disabled (Decision 3). |

---

## 9. Open Decisions — Resolved

### Decision 1 — Removing a user from the unified table

**Resolved: independent per-section removal.** Two distinct actions — `X` on the
bundle-access chip (`revokeGrant`) and `X` on each producer's scope chip-set
(`deleteAgentApiGrant`). No single cascading "remove entirely" button.
Rationale: safer; avoids surprise over-revocation across two independent
authority domains (a publisher revoking catalog access rarely intends to also
strip producer capability scopes, and vice versa).

### Decision 2 — Partial-failure UX

**Resolved: per-section result state, not a single toast.** The modal submits
sections sequentially and accumulates `{ section, ok, error }` per section. On
completion: successful sections persist; failed sections render inline error
text (e.g. "Producer X: 403 — you no longer own this producer") and a single
summary toast ("Added with 1 issue — see details"). No cross-domain
transactional endpoint is invented. The card's overview re-query is the source of
truth after close, so the table always reflects what actually persisted.

### Decision 3 — Zero-selection submit

**Resolved: disable submit until ≥1 selection.** The Add-user modal's primary
button is disabled until at least one of `{bundle-access checkbox checked, any
scope selected in any producer block}` is true. Prevents a no-op submit (which
would otherwise create nothing and confuse the user).

---

## 10. UI/UX Considerations

- **Read-only producer columns** use a muted/disabled visual treatment with a
  lock or info affordance and the literal "Managed by `<owner_email>`" so the
  publisher understands that producer's scopes live on its own page — never
  silently omitted.
- **Scope chips** reuse the exact catalog-suggestion (dashed `+` chips) + free-text
  pattern and copy from `AgentApiAccessScopesCard.tsx` for consistency.
- **Bundle access** rendered as a simple granted/not chip, matching the
  lightweight feel of today's inline picker so the degraded ("just bundle
  access") mode is visually unchanged.
- **`AgentBadge`** for producer column headers (reuse `producer_ui_color_preset`).
- Empty / loading / per-section error states all explicitly handled (§6.5, §8).

---

## 11. Integration Points

- **Client regen:** after adding the aggregator route, run
  `source ./backend/.venv/bin/activate && make gen-client` (or
  `bash scripts/generate-client.sh`) so `InstallsService.getBundlePermissionsOverview`
  + the `BundlePermissionsOverview` types appear in `frontend/src/client`.
- **No agent-env changes** — purely backend read + frontend UI over existing
  systems. No credential pipeline, env template, or migration changes.
- **Docs:** update `docs/agents/agent_bundles/agent_bundles.md` (+ `_tech.md`)
  and `docs/agents/agent_api/agent_api.md` with a cross-reference to the unified
  Permissions management card; add an entry to the bundles row in
  `docs/README.md`.

---

## 12. Future Enhancements (Out of Scope)

- A cross-domain transactional "apply all" endpoint (deliberately avoided —
  per-section reporting is simpler and safer for v1).
- Surfacing / managing scopes for connected producers with identity **OFF**
  (today they're enabled on the producer's own page first).
- Per-install token isolation for publisher-provided `agent_api` credentials
  (tracked under Agent Bundles "Known Gaps", unrelated to this card).
- Bulk add (multi-user) in the Add-user modal — v1 is single-user.

---

## 13. Summary Checklist

### Backend
- [ ] NEW non-table models in `backend/app/models/bundles/bundle_permissions.py`:
      `BundlePermissionScopeCatalogEntry`, `BundlePermissionGrant`,
      `BundlePermissionProducer`, `BundlePermissionUser`,
      `BundlePermissionsOverview`; re-export from `models/__init__.py`.
- [ ] NEW `AgentApiTokenService.list_connected_producers(session,
      consumer_agent_id, user_id, is_superuser)` in `agent_api_token_service.py`
      (decrypt `agent_api` creds → `producer_agent_id`; dedupe; annotate
      `identity_enabled` / `can_manage` / `owner_email`; return identity-enabled
      only).
- [ ] NEW `BundlePermissionsService.build_overview()` in
      `backend/app/services/bundles/bundle_permissions_service.py` (assemble
      bundle grants + producers + per-manageable-producer grants & catalog +
      users union + `show_card`).
- [ ] NEW route `GET /agents/{agent_id}/bundle-permissions-overview` in
      `installs.py`, `require_developer` + publisher-install-owner-only (404
      leak-safe, mirroring `get_bundle_credential_drift`).
- [ ] Verify owner-gated reads (`list_grants` / `get_scope_catalog`) run **only**
      for `can_manage` producers.

### Frontend
- [ ] NEW `BundlePermissionsCard.tsx` (unified table; query
      `["bundlePermissionsOverview", agentId]`; per-section removal).
- [ ] NEW `BundlePermissionsAddUserModal.tsx` (`UserAllowlistPicker` + bundle
      checkbox + per-manageable-producer scope blocks; sequential fan-out with
      per-section result; submit disabled until ≥1 selection).
- [ ] CHANGE `AgentBundleTab.tsx`: remove inline Grants picker + its
      mutations/query; mount `BundlePermissionsCard` in the publisher-install
      section.
- [ ] Invalidate `["bundlePermissionsOverview", agentId]` (+ `["agentApiGrants",
      producerId]`, `["bundles", uuid, "grants"]`) after mutations.
- [ ] Run `make gen-client`.

### Testing & validation
- [ ] Overview returns bundle grants only when `visibility === "users"`.
- [ ] Overview lists identity-enabled connected producers; manageable ones carry
      grants + catalog, non-manageable ones carry neither and expose
      `owner_email`.
- [ ] Non-owner / non-publisher-install caller → 404 (no leak).
- [ ] A publisher who does **not** own a connected producer cannot read or write
      its scopes through this surface (grants empty; writes still 404 at the
      agent-api grant route).
- [ ] Add-user modal fan-out hits the existing bundle + agent-api grant
      endpoints; partial failure reports per section without rolling back
      succeeded sections.
- [ ] Independent per-section removal works (bundle chip vs producer chip-set).
- [ ] `AgentApiAccessScopesCard.tsx` on the producer page still works unchanged.
- [ ] Degradation modes render correctly (users-only; producers-only).

---

*Plan authored: 2026-06-30. No migration expected.*
