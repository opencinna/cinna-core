# AI Credential Keys — Implementation Plan

**Status**: **Complete.** All five phases landed: backend (1–2), client regeneration and the UI
Specification (3), frontend (4, reviewed — three findings raised and fixed), documentation (5).
**Feature name**: `ai-credential-keys`
**Builds on**: `docs/plans/ai_credential_providers_plan.md` (complete). Nothing in that split is
undone here; this plan changes what the admin *sees and can act on*, not what the server stores.
**Docs to update on completion**: `docs/application/ai_credentials/admin_ai_credential_provisioning.md`
+ `_tech.md`, `backend/app/env-templates/platform-knowledge-env/.../api_reference/admin_llm_providers.md`
(+ its knowledge mirror), `docs/README.md` if the route inventory changes.

---

## 1. Overview

### 1.1 The problem

`/admin/ai-credentials#managed-credentials` lists **parent records**. For a `minted` provider one
record hides one key per member, so the surface says nothing about the unit an administrator
actually acts on:

- `LlmProvidersTable.tsx:127` renders `record.members` as an unbounded chip cell — one chip per
  person inside a single `<TableCell>`, each carrying its own inline Retry button. One row for
  three people today; one row for five hundred with the whole company on it.
- The route's pagination (`PAGE_SIZE = 10`) paginates **records**. It never touches the count
  that grows.
- `GET /admin/llm-providers/` returns every parent with every member embedded and no pagination
  at all (`ManagedAICredentialsService.list`). The payload grows with headcount on every load.
- The only per-member verb in the whole feature is `POST /{id}/members/{user_id}/retry`.
  Removing one person's key is a `PATCH` of the parent with the **entire** `target_user_ids` set —
  which is why the blast-radius gate has to answer with a *list* of blocked members. There is no
  per-key revoke, no per-key re-mint, and no per-person "make this their default"; the only
  default verb is *Set default for all*.

So: the record is the unit of administration, the key is the unit of reality, and the two have
drifted apart for exactly the configuration the feature was built for.

### 1.2 The rule this plan implements

**One row = one real API key.**

That maps onto the existing model with no migration, because the model already draws the line in
the right place:

| Record | Real keys | Row |
|---|---|---|
| `minted` provider | one per member, each with its own `external_key_ref` and its own child credential | **one row per membership** |
| `fixed_key` provider, or a manual record | **one** — `_resolve_key` decrypts the parent's key and `_add_child` copies *that same key* into every member's child row | **one row per record** |

The N child credentials under a shared record are copies, not keys. That is why a shared record
collapses to one row and a minted record expands to N: the row count follows the number of
secrets that exist at the vendor.

### 1.3 The chosen shape

One list, mixed rows, one search box:

```
Keys                                            [search] [filter ▾]
───────────────────────────────────────────────────────────────────
● alice@acme.com              OpenAI · svc_0egm…            ⋯
● bob@acme.com                OpenAI · svc_snpl…            ⋯
○ carol@acme.com              OpenAI · minting…             ⋯
● Anthropic Key — BELANA      Shared · 12 people            ⋯
```

Rejected: two lists on one tab (two empty states and two search boxes for one question), and
keys-under-the-provider (finding one person's key would start with knowing which provider issued
it — the thing an admin least reliably knows).

### 1.4 What does *not* move

Granting is not a verb on this list. A new member is still added on the record (the members
editor in `ManagedCredentialDialog`) or by the provider's rule / *Apply to existing users*. The
invite wizard's existing hand-off (`?newCredentialFor=`) keeps landing on the record dialog.
This list is where keys are **seen, repaired, rotated and revoked** — the whole reason it exists
is that those four are per-key and have nowhere to live today.

The **Providers** tab keeps the aggregate: `ProviderRow` already prints
"N members · 3 provisioned · 1 failed" off `key_state_summary`. The keys list is therefore free
to stop being a roll-up, and the two surfaces answer two different questions rather than the
same one twice.

---

## 2. Architecture

```
GET /admin/llm-providers/            (unchanged — record CRUD, members editor, filters)
GET /admin/ai-providers/             (unchanged — the rule, the secret, apply-to-existing)

NEW  GET    /admin/ai-credentials/keys
NEW  DELETE /admin/ai-credentials/keys/{membership_id}
NEW  POST   /admin/ai-credentials/keys/{membership_id}/rotate
NEW  POST   /admin/ai-credentials/keys/{membership_id}/default
MOVE POST   /admin/ai-credentials/keys/{membership_id}/retry
              (from /admin/llm-providers/{id}/members/{user_id}/retry)
```

The membership row is the resource. It already is the thing that carries the key's handles
(`external_key_ref`), names the child credential (`ai_credential_id`) and holds the provisioning
lifecycle — every per-key verb below is addressed by `membership_id` and nothing else.

**No migration.** No new column, no new table. Everything the row needs is stored.

### 2.1 Where the new code goes

- `app/models/credentials/managed_ai_credential.py` — the row DTO and its page wrapper.
- `app/services/credentials/managed_ai_credentials_service.py` — `list_keys()`, the projection.
- `app/services/credentials/key_provisioning_service.py` — `rotate_member_key()`, alongside the
  suspend/resume pair it is built from.
- `app/api/routes/admin_ai_keys.py` — the five routes. **Deviation from the draft**, which put
  them in `admin_llm_providers.py` to keep one generated service: a router's prefix and its file
  are the same choice in FastAPI, so `/admin/ai-credentials/keys` could not live in a router
  prefixed `/admin/llm-providers` anyway. Its own tag gives the browser
  `AdminAiKeysService`, which is the honest name for a separate resource.

Reading the provider table stays inside the two modules the architecture test allows
(`ai_providers_service`, `key_provisioning_service`); the projection resolves providers through
`resolve_policies`, exactly as `list()` does.

---

## 3. The row DTO

```python
class AdminAIKeyRow(SQLModel):
    """One real API key, or one key that is on its way."""

    kind: Literal["per_user", "shared"]          # required, no default
    managed_credential_id: uuid.UUID             # always
    membership_id: uuid.UUID | None              # per_user only — the verb target
    credential_name: str
    type: AICredentialType
    provider_id: uuid.UUID | None
    provider_name: str | None
    # per_user
    holder_user_id: uuid.UUID | None
    holder_email: str | None
    holder_full_name: str | None
    provisioning_status: MembershipProvisioningStatus | None
    provision_error: str | None
    provision_attempts: int
    child_credential_id: uuid.UUID | None
    is_default: bool                             # is this key its holder's default
    api_key_onboarding_state: AIKeyOnboardingState | None
    key_reference: str | None                    # "proj_… / user-…" — handles, never the secret
    # shared
    member_count: int | None
    created_at: datetime


class AdminAIKeysPublic(SQLModel):
    data: list[AdminAIKeyRow]
    count: int          # total matching rows, not the page size — house shape, per UsersPublic
```

**Two id fields rather than one polymorphic `id`.** A single `id` meaning "membership here,
credential there" is a field whose type depends on a sibling field, and every consumer has to
re-derive which. The client keys rows on `membership_id ?? managed_credential_id`.

**Nullable-by-kind is deliberate and documented per field.** `provisioning_status` is `None`
for exactly `kind="shared"`, because a shared record has no provisioning lifecycle — its key was
pasted, not minted. This is the one place the feature relaxes its "required, no default" rule for
a status, and the reason is that the alternative is worse: a synthetic `not_applicable` on a
shared row would be a status the server invented for a row that has no membership behind it.

**`key_reference` is new on the wire.** The handles (`project_id`, `service_account_id`) have
until now lived only in `external_key_ref` and in security events. Publishing them to a
superuser-only surface is what lets an admin match a row against the vendor's own console — the
same job the `email (membership id)` service-account name does from the other side. Never the
key, never the `api_key_id`'s secret half.

---

## 4. Backend implementation

### 4.1 `GET /admin/ai-credentials/keys`

```
?q=            substring, case-insensitive, over holder email + full name + credential name
?status=       MembershipProvisioningStatus (repeatable)
?kind=         per_user | shared
?provider_id=  uuid
?skip= &limit= house pagination (UsersPublic shape); limit clamped 1–100
```

Ordering: `kind` is not a sort axis (mixing is the point). Sort by a computed key —
holder email for `per_user`, credential name for `shared` — ascending, with
`managed_credential_id` as the tiebreak so the order is total and a page boundary cannot repeat
or drop a row.

**The query-count promise carries over, and is the hard part.** `_to_public`'s docstring earns
its keep by asking nothing per member; the new projection must ask nothing per **row**:

1. one paginated query over `managed_ai_credential_membership` joined to `user` (the filter and
   the sort both need the user), restricted to memberships of `minted` parents;
2. one query for the `shared` parents (they are the parent rows themselves, so they page
   alongside — see §8.1 for the one honest complication);
3. one query for the child credentials named by the page's memberships;
4. one `resolve_policies` batch for the page's parents;
5. one `_owner_key_states` batch for the page's holders.

Five queries per page, independent of page size and of headcount. A test mirrors
`test_the_member_list_costs_the_same_however_many_members_there_are` and holds it.

### 4.2 `DELETE /admin/ai-credentials/keys/{membership_id}`

Removes one grant: delete the child credential, then revoke the key at the provider — the
established order, because a child that outlives its key is a credential that fails on use, and
`suspend_user_memberships` already encodes it.

Three rules it inherits rather than restates:

- **The Tier-2 blast-radius gate.** `409` unless `?force=true`, with the impact payload the
  full-set PATCH already returns — but scoped to *one* person, which is the shape the gate was
  always trying to express (`ManagedReconcileBlock` currently has to carry a list because the
  caller could only ever remove a set).
- **Refuse while a mint is in flight.** The existing rule (`status = minting` means a converge
  pass is inside a provider call and is about to write the ref) applies unchanged; the removal is
  blocked for at most one converge tick.
- **A revoke that fails still emits `revoke_blocked` / `revoke_failed`.** Nothing about the audit
  trail changes because the caller is now a route instead of a reconcile.

### 4.3 `POST /admin/ai-credentials/keys/{membership_id}/rotate`

New verb, but **not new machinery**: rotate is `suspend` then `resume` on one membership.

```
suspend_user_memberships(one row) → child deleted, key revoked, status = suspended
resume_user_memberships(one row)  → status = pending, attempts = 0
                                  → the converge pass mints a fresh key
```

That reuse is the whole design. The deactivate/reactivate cascade already destroys a key and
re-mints one, has the crash-safety and claim-token behaviour, and is tested; a bespoke rotate
would be a second implementation of the one sequence that must never leave a key live and
unrecorded.

Two consequences to state in the UI copy rather than hide:

- The holder is **without a key** until the next converge tick (≤ 1 minute). That window is
  honest and identical to the one a deactivation/reactivation already produces.
- Rotation is the only way an existing service account picks up the
  `email (membership id)` name, because OpenAI can create, list and delete a service account but
  never rename one.

Refused for `kind="shared"` (rotating a pasted key is *Replace key* on the provider, which
exists) and while a mint is in flight.

### 4.4 `POST /admin/ai-credentials/keys/{membership_id}/default`

Sets that one person's default to their child credential, via
`ai_credentials_service.set_default(session, child_id, owner_id)` — the same call the existing
per-user pipeline makes. Distinct from *Set default for all*, which stays on the record.

`400` when the membership holds no child yet (`pending` / `minting` / `failed` / `suspended`):
there is no credential to point a default at, and inventing one would break the invariant that
every `AICredential` row that exists is usable.

### 4.5 Retry, re-homed

`POST /admin/ai-credentials/keys/{membership_id}/retry` replaces
`POST /admin/llm-providers/{id}/members/{user_id}/retry`. Same
`key_provisioning_service.requeue_failed_member`, same refusal for a member who has not failed,
same security event. The old path is **removed**, not aliased: the client is regenerated from the
spec, and two paths to one verb is how a surface ends up calling the one nobody maintains.

---

## 5. Frontend surfaces (inventory — composition decided by `cinna-core.ui.design`)

| Surface | File | Change |
|---|---|---|
| Keys tab body | `Admin/AiKeys/KeysTab.tsx` | **New.** Server-paginated list, search box, status/kind/provider filters |
| One key row | `Admin/AiKeys/KeyRow.tsx` | **New.** Both kinds render through it |
| Revoke | `Admin/AiKeys/RevokeKeyDialog.tsx` | **New.** Names the person, shows the impact payload, one destructive button |
| Rotate | `Admin/AiKeys/RotateKeyDialog.tsx` | **New.** States the without-a-key window in one sentence |
| Shared members | `Admin/AiKeys/SharedKeyMembersSheet.tsx` | **New.** The N people holding a copy, behind the row's menu |
| Route | `routes/_layout/admin/ai-credentials.tsx` | Tab body swapped; the poll predicate reads the page's rows; header buttons unchanged |
| Old table | `Admin/LlmProviders/LlmProvidersTable.tsx` | **Deleted** |
| Member chips | `Admin/LlmProviders/MemberKeyStatus.tsx` | `KeyStatusSummary` + `MemberChip` deleted; `hasKeyInFlight` reshaped to a row predicate; `hasProviderKeyInFlight` untouched (the Providers tab owns it) |
| Record dialog | `Admin/LlmProviders/ManagedCredentialDialog.tsx` | Unchanged — still create/edit for manual records and the members editor |

**Constraints the UI Specification has to settle**, named here so the designer does not have to
re-derive them:

- This is the **Manage-list** story (§1 of the guidelines): search, pagination, act on many. P9's
  reference is `Common/DataTable.tsx`; the chosen preview reads as `Common/ListRow` rows. Both are
  house patterns and they disagree here — the spec picks one and says why.
- **Row action budget**: ≤ 2 inline including any On/Off. Retry is the row's purpose when it has
  failed and is the candidate for the one inline action; Rotate, Revoke, Set as their default and
  Members go in the `⋯` menu, destructive last, confirmed with `AlertDialog` naming the person.
- **A row with no key yet is still a row** (`pending` / `minting` / `failed` / `suspended`). It is
  the only place an admin can see that one person's key is stuck, which is half the reason the
  list exists. The leading dot carries it; the status word does not become a `Badge` on every row.
- **The 10 s poll survives** but is scoped to the rows on the current page.

---

## 6. Error handling & edge cases

| Case | Behaviour |
|---|---|
| Membership in `minting` | Every mutating verb refuses with the existing in-flight message; the row shows the spinner tone |
| Membership `suspended` (deactivated account) | Row renders muted; Revoke allowed, Rotate and Set-default refused (there is no active account to mint for) |
| Shared record with zero members | Still one row — the key exists whether or not anyone holds a copy |
| Provider-owned record whose provider row is gone | Row renders with `provider_name = null` and the "Provider (missing)" treatment the table already has; no verb infers a provider from it |
| `q` matching nothing | The list's empty state distinguishes "no keys yet" from "nothing matches this search" — two sentences, not one |
| A key revoked at the vendor by hand | Not detectable here and not claimed to be; the row reports our state, and the reconciler is out of scope (§7) |

---

## 7. Out of scope

- Reconciling our rows against the vendor's actual service-account list (a real gap — a key
  deleted in the OpenAI console still reads `provisioned` here — but it is a scheduler, not a
  surface, and it belongs with the discovery cron).
- Per-key spend readout. The provider exposes it; showing cost per row is a second feature with
  its own polling budget.
- Bulk verbs (revoke selected, rotate all). Worth having; not before one-at-a-time is right.
- Any change to how membership is granted (§1.4).

---

## 8. Known complications, stated up front

### 8.1 Paging two row sources with one cursor — **resolved differently**

`per_user` rows come from `managed_ai_credential_membership`; `shared` rows come from
`managed_ai_credential`. One page has to be a slice of the union. The draft weighed a SQL
`UNION ALL` against a client-side merge (wrong at a page boundary) and picked the union.

**What was built is a third option: make `kind` the primary sort key.** Shared rows first, each
group by name. The shared rows are few and already in memory — every parent is loaded to resolve
policies — so a page either starts inside that block and is topped up from one paged membership
query, or lies entirely past it at a known offset. Exact at every boundary, one query, no union
over two dissimilar tables. What it gives up is interleaving the two kinds by name, which buys an
administrator nothing: a specific key is found by searching, not by scrolling to where it sorts.
`test_shared_rows_come_first_and_paging_is_exact_across_the_boundary` walks every page and asserts
each row is served exactly once.

### 8.2 `member_count` on a shared row is a second query per page, not per row

It is one grouped count over the page's shared parents. Named here because the obvious
implementation (`len(parent.members)`) is a per-row load and would quietly reintroduce exactly
the cost this plan exists to remove.

---

## 9. Phases

1. **Row DTO + `GET /keys`** — projection, union paging, filters, search. Tests: mixed-kind
   ordering, page-boundary stability, the five-query ceiling, filter/search correctness.
2. **Per-key verbs** — revoke (gate + in-flight refusal), rotate (suspend/resume reuse),
   set-default, retry re-homed. Tests: each refusal, the audit events, and a rotate that produces
   a *different* service account.
3. **Client regeneration + UI Specification** (`cinna-core.ui.design` on this plan).
4. **Frontend** — the keys list, the row, the three overlays; delete `LlmProvidersTable`;
   `cinna-core.ui.review` to PASS.
5. **Docs** — the two `admin_ai_credential_provisioning` files, the API reference + knowledge
   mirror, and the anti-pattern rows this closes in `ui_ux_guidelines.md` §4.

Phases 1 and 2 are independently shippable; the surface cannot land before both.

---

## 10. Testing & validation

- `tests/api/ai_credentials/admin_ai_keys_test.py` — new: the list (shape, ordering, paging,
  filters, search), the four verbs, and every refusal.
- `tests/api/ai_credentials/minted_ai_credentials_test.py` — extend for rotate; the existing
  in-flight race tests already cover the window the new verbs enter.
- Query-count test for the projection, modelled on the existing member-list one.
- Regression scope for the change: `tests/api/ai_credentials/` (184 today), plus
  `tests/api/agents/bundles_install/` because it stubs the same provisioner.

---

## UI Specification

_Produced by `cinna-core.ui.design` on 2026-09-08. Composition decisions here override §5 of this plan; data and API decisions stay with the plan._

### Surfaces

| # | Surface | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---------|------|-------|--------------------|---------|--------|--------------|
| S1 | Keys tab | `/admin/ai-credentials` › `HashTabs` tab 1 | Manage-list | "Complex configuration" → its own tab; "Card width" → search + pagination makes it a Manage-list, not a card | P9 | 7 columns, 25 rows/page, **0** inline row actions, menu ≤ 4 items + 1 destructive | checklist + screenshots |
| S2 | Revoke key confirm | S1 row menu | Edit (destructive) | "Confirmation" → `Dialog` allowed when it must show fetched impact | P3 confirm + impact | 1 sentence + impact list + 1 destructive button | checklist |
| S3 | Rotate key confirm | S1 row menu | Edit (destructive) | "Confirmation" → `AlertDialog` naming the entity | AlertDialog | 2 sentences, 1 destructive button | checklist |
| ~~S4~~ | ~~Holders sheet~~ | — | — | **Dropped in build** — see below | — | — | — |
| S5 | Keys toolbar | S1, above the table | Manage-list | P9 "Filters as a toolbar above the table, never as a side card" | P9 | 4 controls | with S1 |

### S1 — Keys tab

- **Intent:** "Show me every API key this company has, whose it is, and let me act on one of them." The tab this replaces answered "what records exist", which for a minted provider hid one key per member behind a single row.
- **Layout:** the P9 skeleton (`routes/_layout/admin/users.tsx` + `Common/DataTable.tsx` + `Admin/columns.tsx`), rendered as a tab body rather than a route body — the host page already owns the header and the tab strip. Toolbar (S5) above the table, `Pagination` below it, both outside the `DataTable`.
- **Tab identity:** value `keys`, title **Keys**, still `tabs[0]`. The old `#managed-credentials` hash has no link anywhere in `frontend/src` (grep: none outside the route itself) and `HashTabs.getInitialTab` falls back to `tabs[0]`, so an old bookmark lands here anyway.
- **Columns (6 as built, 7 as specified).** The **Provider reference** column was cut during the
  build: at 1024 seven columns pushed the table into a horizontal scroll that moved the sidebar
  with it. The handles moved into the row's `RowInfo` glyph, which is the house's own home for a
  fact that is looked *up* rather than scanned — and since a minted service account is now named
  `<email> (<membership id>)` at the vendor, the email in the first column is already the match.
  `DataTable` also gained an `overflow-x-auto` wrapper so no consumer can push the page sideways
  again.
- **Columns (as specified):**
  1. **Key** — `per_user`: holder email, second line `credential_name`. `shared`: `credential_name`, second line "Shared with N people". One `Badge h-5` "Default" on the title line when `is_default`; nothing else on that line.
  2. **Status** — the tone-dot + label form §5 permits in a `DataTable` cell. `per_user`: the `provisioning_status` label from `utils/keyProvisioning.ts` with its tone, plus `describeProvisionError(provision_error)` as a muted second line on `failed`. `shared`: "Shared key", muted, no dot — it has no lifecycle.
  3. **Type** — `Badge variant="secondary"` from `getProviderTypeLabel(row.type)`.
  4. **Source** — the provider name as a `Link to="/admin/ai-credentials" hash="providers"`, "Provider (missing)" when `provider_id && !provider_name`, `Badge` "Manual" otherwise. Lifted verbatim from `LlmProvidersTable.SourceCell`, which is deleted with the file.
  5. **Reference** — `key_reference` in `font-mono text-xs text-muted-foreground truncate`, `—` when absent. This is the column that lets an admin match a row against the vendor's console.
  6. **Created** — `formatDate(created_at)`.
  7. **`⋯`** — `w-[48px]`, right-aligned.
- **Actions — 0 inline, everything in the `⋯`.** A Retry button rendered only on failed rows would be an empty column on every healthy one; the failure reason is already in the Status cell and Retry is the menu's first item, so nothing is hidden that the row does not already announce.
  - `per_user`: **Retry** (only when `provisioning_status === "failed"`) · **Set as their default** (disabled with a tooltip when `child_credential_id` is null) · **Rotate key** · separator · **Revoke key** (`variant="destructive"`).
  - `shared`: renders the existing `LlmProviders/LlmProviderActionsMenu` — Members · Set default for all · Delete (manual only) — behind a lazy `useQuery` for the record (`getManagedAiCredential`, `enabled` on menu open). Reuse rather than reimplementation: a shared row **is** a record, and its verbs have not changed.
- **Interaction model:** form/verb — every mutation is a menu item that opens a confirm or fires one request with a toast. No auto-saving control on the table.
- **States:**
  - loading — `PendingItems` (the tab's current fallback) on first load; a background refetch never blanks the table.
  - error — `QueryErrorAlert` with Retry, and **only** when `data === undefined`; a failed background refetch must not replace a live table (the trap `project_ui_build_onboarding_admin` records).
  - empty (no filters) — "No keys yet." + "Connect a provider to start issuing keys" linking to `#providers`.
  - empty (filtered) — "No keys match this search." + a `Clear filters` button. Two different sentences: an admin who reads "no keys yet" after typing a name concludes the company has none.
  - pending — the acting row's menu trigger is disabled, scoped by `variables?.membershipId === row.membership_id`, never by `isPending` alone (one observer describes only its latest call — `project_shared_mutation_row_pending`).
- **Polling:** keep the 10 s `refetchInterval`, now predicated on the **page's** rows (`data.data.some(r => membershipStatusMeta(r.provisioning_status).inFlight)`) and on the Keys tab being the active one.
- **Components:** `DataTable`, `Pagination`, `Badge`, `Tooltip`, `DropdownMenu` via the existing menus, `QueryErrorAlert`, `Skeleton` inside `PendingItems`, `Input` + three `Select`s in the toolbar. No new primitive.
- **Anti-patterns:** **A4-shaped (unbounded list) fixed here** — `LlmProvidersTable`'s `members` cell rendered one chip per member with an inline Retry on each, unbounded and unpaginated; the file is deleted. `KeyProvisioningRows.tsx` (open A10 row) is **deferred**: it is the user's own settings card, not touched by this phase.
- **Siblings:** n/a — the tab body is a table, not a card in a grid. The other tab (`ProvidersTab`) is a `PreviewList` of `ListRow`s inside a card; the two differ because they serve different stories, which is the §2 "Card width" row's exact provision (search + pagination ⇒ Manage-list, not card).
- **Screenshots:** required — P9 in a new host (a tab body) and a dense list (25 rows/page). Capture `/admin/ai-credentials#keys` at **1440** and **1024**, default full-page mode, light theme. Two captures, no more.

### S2 — Revoke key confirm

- **Intent:** "Take this person's key away, and tell me first if that breaks something."
- **Layout:** `AlertDialog` naming the holder — *"Revoke the key for alice@acme.com?"* — one sentence of consequence: "Their credential is deleted and the key is destroyed at OpenAI. This cannot be undone."
- **The 409 branch:** on `409` the dialog **stays open** and swaps its body for the blocked reasons, read from `detail.blocked[]` exactly as `LlmProviderActionsMenu.blockedFromError` already parses them. `in_use_bundle` then offers **Revoke anyway** (`force=true`); `mint_in_flight` offers only Close, because forcing during a mint is the one action that strands a key. This is why it is an `AlertDialog` that mutates rather than a plain confirm: the impact is only knowable after the attempt.
- **States:** pending on the confirm button (the menu item that opened it has unmounted); error via toast for anything that is not a 409.
- **Components:** `AlertDialog`, `Button variant="destructive"`.
- **Anti-patterns:** none. Depth stays at 2 (table → dialog).
- **Screenshots:** none — a confirm dialog is explicitly excluded by §9.

### S3 — Rotate key confirm

- **Intent:** "Give this person a new key" — after a suspected leak, or to move an old service account onto the current naming.
- **Layout:** `AlertDialog`, title *"Rotate the key for alice@acme.com?"*, body two sentences: "Their current key is destroyed at the provider straight away. A new one is created within a minute, and until it arrives they have no key." No third sentence — the window is the only thing the admin does not already know.
- **States:** pending on the confirm button; success toast "Key rotation queued"; 409/400 via toast.
- **Components:** `AlertDialog`.
- **Screenshots:** none.

### S4 — Holders sheet (shared rows) — **dropped in build**

The shared row's menu delegates to `LlmProviderActionsMenu`, whose **Members** item already opens
the record's member editor: it lists every holder *and* can change the set. A read-only sheet
beside it would be a second, weaker answer to the same question, and the guidelines' own rule
against two surfaces for one story applies to a designer's own spec. The `membership_id` contract
gap under Open questions stands unchanged — it is what would let per-person removal live on a row
rather than in a picker.

<details><summary>The original specification, for the record</summary>

#### S4 — Holders sheet (shared rows)

- **Intent:** "Who is holding a copy of this one key?"
- **Layout:** `Sheet` (side), title = the credential name, description = "N people hold a copy of this key." Body is a `ListRowGroup` of `ListRow`s: identity tile, name/email as `title`, no `meta`, **no actions** — read-only.
- **Why read-only:** removing one holder is a per-membership verb, and `ManagedAICredentialMember` does not publish `membership_id`, so this surface cannot address it. Membership editing stays where it already is, in `ManagedCredentialDialog`'s member picker, reachable from the same row's menu. See Open questions.
- **States:** loading `Skeleton` rows; empty "Nobody holds this key yet."; error `QueryErrorAlert`.
- **Components:** `Sheet`, `ListRowGroup`, `ListRow`.
- **Screenshots:** none — a straight P5 Sheet.

</details>

### S5 — Keys toolbar

- **Intent:** "Find the key I mean."
- **Layout:** one row above the table, `flex flex-wrap gap-2`: a search `Input` (`placeholder="Search by person or credential"`, 300 ms debounce, `min-w-[16rem] max-w-sm`), then `Select`s for **Status** (All / Provisioned / Creating / Failed / Suspended / Pending), **Kind** (All / Per-user keys / Shared keys) and **Provider** (All + each connected provider, from the providers query the sibling tab already runs). A `Clear` ghost button appears only when something is set.
- **Replaces:** the header **Filter** button and its `UserAllowlistPicker` panel. That control filtered *records* by member and needed a user lookup to do it; the search box answers the same question server-side and three more besides. The header keeps only the tab's primary button.
- **Every control writes the query, never the client:** each maps to a query param on `listAiKeys` and resets `skip` to 0.
- **States:** n/a (controls, not content).
- **Components:** `Input`, `Select`, `Button variant="ghost"`.

### Frontend phase order

| Phase | Surfaces | Backend contract |
|---|---|---|
| F1 | S1 columns + S5 toolbar + pagination; delete `LlmProvidersTable.tsx`; `MemberKeyStatus` reduced to the two predicates the Providers tab still uses | `AdminAiKeysService.listAiKeys` |
| F2 | S2, S3 and the `per_user` menu | `revokeAiKey`, `rotateAiKey`, `setAiKeyAsDefault`, `retryAiKey` |
| F3 | S4 and the `shared` menu delegation | `AdminLlmProvidersService.getManagedAiCredential` |

### Open questions

1. **`membership_id` on the record projection.** `ManagedAICredentialMember` publishes `user_id` but not the membership id, so S4 cannot offer per-person removal and the Providers tab cannot link a member to their key row. Adding it is a one-field change to `_member_dto`; it is a contract gap for the planner, not a composition decision, and S4 is read-only until it lands.
2. **Tab title.** "Keys" is used throughout above. If the admin vocabulary should stay "Credentials", the title changes and nothing else does.
