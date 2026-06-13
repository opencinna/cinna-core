# Managed AI Credential Record — Implementation Plan

## Overview

Introduce a **parent "Managed AI Credential" record** that a superuser manages once and that **auto-syncs to per-user `AICredential` child rows**. Today each admin provision creates N independent `AICredential` rows (one per target user) flagged `is_admin_managed=True` with an audit-only `managed_by_id`; there is no parent, so the admin UI fakes a "shared with N users" grouping by string-matching `name + type`, and edit/delete are per-user-row. This plan replaces that with a real parent record whose **target user set and config are the source of truth**, reconciled into child credentials on every change.

Core capabilities:
- One admin record holds canonical config (name, type, key, base_url, model, default flags) **plus the target user set**.
- A reconcile service diffs desired-vs-actual and creates/updates/deletes child `AICredential` rows.
- Children remain ordinary per-user credentials, so **all existing per-user plumbing keeps working unchanged**.
- Admin edit = the create form pre-filled, with the user picker showing current members (add/remove). Adding a member never requires re-typing the key.

```
        ┌──────────────────────────────────────────────┐
Admin → │  ManagedAICredential (parent, source of truth)│
        │  name/type/key/base_url/model/defaults/targets │
        └───────────────┬──────────────────────────────┘
                        │ reconcile(desired vs actual)
       ┌────────────────┼─────────────────┐
       ▼                ▼                 ▼
  AICredential     AICredential      AICredential   ← child rows (one per target user)
  owner=U1         owner=U2          owner=U3         is_admin_managed=True
  managed_credential_id = parent.id                   (look exactly like today's managed rows)
       │                │                 │
       └── existing per-user plumbing (default resolution, env linking, model discovery) ──┘
```

## Architecture Overview

**Components**
- New table `managed_ai_credential` (parent). New nullable FK `managed_credential_id` on `ai_credential` (child link).
- New `ManagedAICredentialService` — owns CRUD on the parent and the **reconcile** routine. Delegates per-child create/update/delete/set-default to the existing `ai_credentials_service` (so one-default-per-type, profile auto-sync, blast-radius gating, encryption all run unchanged).
- Reworked `/admin/llm-providers` route surface — parent-oriented CRUD (replaces the per-row provisioning endpoints).
- Reworked admin frontend (`Admin/LlmProviders/*`) — list of parent records, unified create/edit dialog operating on the whole record.

**Data flow (edit example)**
1. Admin opens a parent record → form pre-filled with config + member badges.
2. Admin removes U2, adds U4, rotates the key, toggles "set as default".
3. `PATCH /admin/llm-providers/{id}` updates the parent row, then `reconcile()`:
   - U2 removed → `delete_credential(child_U2, admin_override)` (blast-radius aware).
   - U4 added → `create_credential` for U4 + stamp `is_admin_managed` + `managed_credential_id`, decrypt parent key for the new child.
   - Key/field change → `update_credential(admin_override)` write-through to U1, U3 children.
   - default on → `set_default` per surviving child.
4. Each child mutation emits a `SecurityEvent`; the response returns the parent with refreshed member projections.

**Integration points**: `ai_credentials_service` (per-user pipeline), `sdk_constants` (SDK composition for `set_user_sdk_defaults`), credential deletion-impact/blast-radius (Tier 2 bundle gating), SecurityEvent audit, model discovery cron (operates on children unchanged).

## Data Models

### New table: `managed_ai_credential` (parent)

| Field | Type | Constraints / Default | Notes |
|-------|------|----------------------|-------|
| `id` | UUID | PK, `default_factory=uuid4` | |
| `name` | str(255) | NOT NULL, min_length 1 | Canonical name pushed to all children |
| `type` | str(50) | NOT NULL | `AICredentialType` (anthropic/openai/openai_compatible/google; minimax retained at code level) |
| `encrypted_data` | Text | NOT NULL | Fernet-encrypted JSON `{api_key, base_url?, model?}` — **canonical key**, same shape/codec as `ai_credential.encrypted_data` |
| `base_url` | str(500) | NULL | Non-secret mirror for projection/UI (openai_compatible/google) |
| `model` | str(255) | NULL | Non-secret mirror (openai_compatible) |
| `set_as_default` | bool | NOT NULL, server_default false | Desired: each child set as its owner's default for the type |
| `set_user_sdk_defaults` | bool | NOT NULL, server_default false | Desired: wire each owner's `default_sdk_*` to their child |
| `sdk_default_modes` | JSON | default `["conversation","building"]` | Modes wired when `set_user_sdk_defaults=True` |
| `expiry_notification_date` | datetime | NULL | Mirrored to children |
| `managed_by_id` | UUID | FK `user.id` ON DELETE SET NULL, indexed | Admin who owns/manages the record (audit + "who provisioned") |
| `created_at` / `updated_at` | datetime | NOT NULL | |

- No `target_user_ids` column on the parent — membership is derived from the child rows (the children with `managed_credential_id = parent.id`). This keeps a single source of truth for membership and avoids drift. (Alternative considered: a `managed_ai_credential_member` join table to record *intended* members even when a child failed to create; deferred — failures surface in the API result instead, see Reconcile.)
- Index: `ix_managed_ai_credential_managed_by` on `managed_by_id` (fleet listing/filtering).

### Modified table: `ai_credential` (child)

Add one column:

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| `managed_credential_id` | UUID | NULL, FK `managed_ai_credential.id` **ON DELETE SET NULL**, indexed | Structural link to the parent. NULL = a normal self-created credential. |

- **Why SET NULL, not CASCADE**: parent deletion must route through the reconcile/delete path so each child gets proper profile un-wiring + Tier-2 blast-radius gating. A raw DB cascade would bypass that. SET NULL is a safety net only — if a parent row is ever deleted out-of-band, children degrade to plain `is_admin_managed` orphans (today's behavior) rather than silently vanishing.
- `is_admin_managed` stays the **read-only discriminator** for the user-facing CRUD (unchanged). `managed_credential_id` is the new **structural** link. A child has both set.
- `managed_by_id` on the child is retained (audit) and set to `parent.managed_by_id`.

### Projection DTOs (new)

- `ManagedAICredentialMember` — `{ user_id, email, full_name | None, child_credential_id, is_default }`. One per child.
- `ManagedAICredentialPublic` — parent fields (no secret) + `members: list[ManagedAICredentialMember]` + `member_count`. `has_api_key: bool` (always true), `is_oauth_token` (derive from type/key prefix as today). Never includes `encrypted_data`/key.
- `ManagedAICredentialCreate` — `{ name, type, api_key, base_url?, model?, expiry_notification_date?, target_user_ids: [≥1], set_as_default, set_user_sdk_defaults, sdk_default_modes }`.
- `ManagedAICredentialUpdate` — all fields optional: `{ name?, api_key?, base_url?, model?, expiry_notification_date?, target_user_ids?, set_as_default?, set_user_sdk_defaults?, sdk_default_modes? }`. Omitting `api_key` keeps the stored key. Omitting `target_user_ids` leaves membership unchanged.
- `ManagedAICredentialReconcileResult` — `{ record: ManagedAICredentialPublic, added: [...], removed: [...], updated_count, skipped: [ {user_id, reason} ], blocked: [ {user_id, reason, impact} ] }` (reason e.g. `user_not_found`, `user_inactive`, `in_use_bundle`).

## Security Architecture

- **Encryption**: identical to `ai_credential` — Fernet via `backend/app/core/security.py`, JSON `{api_key, base_url?, model?}` stored in `encrypted_data`. Parent and each child each hold their own encrypted copy (write-through). Reuse `ai_credentials_service.decrypt_credential` shape to read the parent key for new-child creation / rotation.
- **Access control**: all parent routes require `get_current_active_superuser` (403 otherwise). Children continue to be read-only through the user-facing CRUD via the existing `is_admin_managed` guard in `AICredentialsService.update_credential/delete_credential` (admin path passes `admin_override=True`).
- **Key never exposed**: `ManagedAICredentialPublic` omits `encrypted_data` and never returns the key. Edit form shows a blank "API Key" field ("leave blank to keep existing"). Test Connection reuses the entered key, or — when blank on an existing record — probes via the parent's stored key (parent id passed to a parent-aware test path, mirroring the existing `credential_id` resolution in `AICredentialTestRequest`).
- **Audit**: every child mutation triggered by reconcile emits a `SecurityEvent` keyed to the child owner (`admin.ai_credential.provision|update|delete|set_default`), plus a parent-level batch event (`admin.managed_ai_credential.create|update|delete`) keyed to the admin. No key material in any event.
- **Input validation**: per-type field rules delegated to `create_credential` (e.g. `openai_compatible` requires base_url + model) → a bad payload fails the whole call with 400. `target_user_ids` min length 1 on create.

## Backend Implementation

### API Routes — `backend/app/api/routes/admin_llm_providers.py` (reworked, prefix kept `/admin/llm-providers`)

| Method / Path | Body | Response | Notes |
|---|---|---|---|
| `POST /admin/llm-providers/` | `ManagedAICredentialCreate` | `ManagedAICredentialReconcileResult` | Create parent + reconcile to create children. Invalid targets → `skipped`. |
| `GET /admin/llm-providers/` | `?managed_by_id=&target_user_id=` | `list[ManagedAICredentialPublic]` | Fleet-wide list of parents. `target_user_id` filters to records that have that user as a member. |
| `GET /admin/llm-providers/{id}` | — | `ManagedAICredentialPublic` | 404 if not found. |
| `PATCH /admin/llm-providers/{id}` | `ManagedAICredentialUpdate` `?force=` | `ManagedAICredentialReconcileResult` | Update fields + membership; reconcile. `force` overrides Tier-2 on removed members. |
| `DELETE /admin/llm-providers/{id}` | `?force=` | `Message` | Cascade-delete all children (service-level), then parent. `force` overrides Tier-2. 409 with impact list when blocked. |
| `POST /admin/llm-providers/{id}/set-default` | — | `ManagedAICredentialPublic` | Set every member's child as that user's default for the type. |
| `POST /admin/llm-providers/test-connection` | `AICredentialTestRequest` (+ optional `managed_credential_id`) | `AICredentialTestResult` | Validate key before save; resolve stored key from parent when key blank. May reuse the existing `ai-credentials/test-connection` if a parent-key path is added. |

- Dependencies: `SessionDep`, `SuperUser = Depends(get_current_active_superuser)`.
- The **old per-row endpoints** (`PATCH/DELETE/{credential_id}`, `POST/{credential_id}/set-default`, and the per-row `POST /` provisioning shape) are removed from the admin surface; their per-child logic moves behind the reconcile service. (No other caller uses them — only the admin UI.)

### Service Layer — `backend/app/services/credentials/managed_ai_credentials_service.py` (new)

Singleton `managed_ai_credentials_service` (mirrors `admin_ai_credentials_service`). Key methods:

- `create(session, admin, data: ManagedAICredentialCreate) -> ManagedAICredentialReconcileResult`
  - Validate + encrypt canonical key into the parent row (reuse `_validate_credential_data` / encryption helper from `ai_credentials_service`).
  - Persist parent with `managed_by_id = admin.id`.
  - Call `reconcile(...)` with desired target set = `data.target_user_ids`.

- `reconcile(session, admin, parent, desired_user_ids, *, apply_fields=True, force=False) -> ManagedAICredentialReconcileResult`
  - **Current members** = children where `managed_credential_id == parent.id` (map owner_id → child).
  - **Add** (`desired − current`): validate user exists/active (else `skipped`); decrypt parent key; `ai_credentials_service.create_credential(owner_id, AICredentialCreate(... parent key ...))`; stamp `is_admin_managed=True`, `managed_by_id=parent.managed_by_id`, `managed_credential_id=parent.id`; optional `set_default` + `_apply_sdk_defaults`.
  - **Remove** (`current − desired`): `ai_credentials_service.delete_credential(child.id, child.owner_id, force=force, admin_override=True)`. On `AICredentialInUseError` → append to `blocked` (don't fail the whole op) unless `force`.
  - **Update** (`current ∩ desired`, when `apply_fields`): build `AICredentialUpdate` from changed parent fields (name/base_url/model/expiry, and api_key only when rotated this call); `update_credential(admin_override=True)`. Apply/clear default per `set_as_default`.
  - Idempotent: identical desired state + unchanged fields → no-op (empty added/removed/updated).

- `update(session, admin, id, data: ManagedAICredentialUpdate, force) -> ...`
  - Load parent or 404. If `api_key` present → re-encrypt parent `encrypted_data` (rotation). Update parent scalar fields. Desired set = `data.target_user_ids` if provided else current members. `reconcile(apply_fields=True, force=force)`. Bump `updated_at`.

- `delete(session, admin, id, force) -> None`
  - Load parent or 404. Reconcile to empty membership (deletes all children with blast-radius gating; 409 if any blocked and not `force`). Then delete the parent row.

- `set_default_all(session, admin, id) -> ManagedAICredentialPublic` — set every child as its owner's default; set parent `set_as_default=True`.

- `list(session, admin, managed_by_id=None, target_user_id=None) -> list[ManagedAICredentialPublic]` and `get(...)` with member projection (joins children + their owners for name/email).

- Projection helper `_to_public(session, parent) -> ManagedAICredentialPublic` — loads children + owners, builds `members`.

**Reuse, don't duplicate**: all encryption, per-type validation, one-default-per-type, profile auto-sync, and blast-radius gating come from `ai_credentials_service`. This service only owns the parent row and the diff.

### Background Tasks

None. Reconcile is synchronous within the request (membership sets are small — admin fan-out). Model discovery cron already runs over children unchanged.

## Frontend Implementation

### Components — `frontend/src/components/Admin/LlmProviders/`

- **`ProvisionLlmProviderDialog.tsx` → unified create/edit dialog** (rename optional, e.g. `ManagedCredentialDialog.tsx`):
  - Props: `mode: "create" | "edit"`, `record?: ManagedAICredentialPublic`, controlled `open`/`onOpenChange` for edit (create keeps the header trigger button).
  - Fields: Name (auto-suggested "<Provider> Key"), Provider Type (disabled in edit — immutable), API Key (blank in edit = keep existing), Base URL / Model (per type), **Target Users** (`UserAllowlistPicker`, multi, pre-filled with current members in edit), Set as default (switch), Set user SDK defaults (switch), **Test Connection** (already implemented; in edit-with-blank-key, probe via stored parent key).
  - Submit: create → `POST`; edit → `PATCH` with `target_user_ids` = picker selection. Surface `skipped`/`blocked` from the reconcile result as per-user toasts (e.g. "U2 not removed — in use by a published bundle").
- **`LlmProvidersTable.tsx`** — render **one row per parent record**: Name, Provider badge, "Shared with" = member badges `Full Name <email>`, Created. A single parent-level actions menu (Edit / Set default for all / Delete). Remove the client-side `name+type` string-grouping (now real records). Keep the member-badge styling.
- **`LlmProviderActionsMenu.tsx`** — operate on the parent `record` (edit opens the unified dialog in edit mode; delete calls parent `DELETE` with a 409→force confirm; set-default calls parent set-default).
- **`providerTypes.ts`** — unchanged provider option/label helpers.

### Route — `frontend/src/routes/_layout/admin/llm-providers.tsx`

- Query `AdminLlmProvidersService.list...()` now returns `ManagedAICredentialPublic[]` (parents). Drop the grouping memo; paginate over records (existing group-pagination logic adapts directly). Keep the header **Filter** toggle (filter by member user) + **Provision Credential** button. `ownerInfo` map still resolves member name/email for badges.

### State Management

- React Query keys: keep `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin","llm-providers"]`; list keyed by filter. Mutations (create/update/delete/set-default) invalidate the prefix.
- Delete-blocked (409) flow: catch impact payload → confirm dialog → retry with `force=true` (mirrors the existing AI-credential Tier-2 force pattern).

### User Flows

- **Create**: header → dialog → fill fields + pick users → Test Connection (optional) → Provision → toast with created/skipped counts.
- **Edit**: row ⋮ → Edit → dialog pre-filled (members shown as removable chips, add via picker) → rotate key optionally → Save → toast summarizing added/removed/updated, plus any blocked members.
- **Delete**: row ⋮ → Delete → confirm; if blocked by bundle usage → "force" confirm.
- Empty/loading/error states reuse the current page scaffolding.

## Database Migrations

Migration `add_managed_ai_credential.py` (single head; verify `alembic heads` first — repo has had multi-head situations):

- **Create** `managed_ai_credential` (columns per Data Models), PK on `id`, index `ix_managed_ai_credential_managed_by`, FK `managed_by_id → user.id` ON DELETE SET NULL.
- **Alter** `ai_credential`: add `managed_credential_id UUID NULL`, FK → `managed_ai_credential.id` ON DELETE SET NULL, index `ix_ai_credential_managed_credential`.
- **No data backfill** — the database has no existing admin-managed (`is_admin_managed`) records, so the migration is pure schema. (If any ever existed, they would simply remain standalone `is_admin_managed` rows with `managed_credential_id = NULL`, harmless and invisible to the new parent UI.)
- **Downgrade**: drop the FK + `managed_credential_id` column, drop `managed_ai_credential`.

## Error Handling & Edge Cases

- **Removed member in use by a published bundle** (Tier 2): `delete_credential` raises `AICredentialInUseError` → reconcile records it in `blocked` (not a hard failure). The member stays; UI shows "couldn't remove — in use". `force=true` overrides.
- **Target user unknown/inactive**: recorded in `skipped` (mirrors today's provision skip), op succeeds for the rest.
- **Key rotation failure / bad key payload**: per-type validation fails the whole call (400) before any child mutation (validate before reconcile).
- **Partial reconcile failure** (a single child update throws unexpectedly): wrap per-child operations; collect failures into `skipped`/`blocked`; commit successful ones; never leave the parent and children in a torn state where the parent claims a member that has no child (membership is *derived* from children, so a failed add simply isn't a member — self-healing on next edit).
- **Concurrent edits** by two admins: last write wins on parent scalars; membership reconcile is a diff so converges. No row locks needed for MVP (document).
- **Duplicate target ids** in one request: de-duplicate preserving order (as today).
- **Deleting the managing admin account**: `managed_by_id` SET NULL on both parent and children; record remains manageable by any superuser (listing is fleet-wide).

## UI/UX Considerations

- "Shared with" member chips: `Full Name <email>`, with a "Default" mini-badge where the child is that user's default; wrap on narrow widths.
- Edit dialog hint under API Key: "Leave blank to keep the current key for all members." Under Provider Type (disabled): "Provider type can't be changed after creation."
- Reconcile result toasts: success summarizes `+added / −removed / ~updated`; warnings list skipped/blocked members by name. Color: success default, blocked amber/destructive.
- Test Connection inline alert reused as-is (green check / skip-reason note / destructive failure).

## Integration Points

- **Per-user AICredential plumbing** — children are created/updated/deleted exclusively through `ai_credentials_service`, so default resolution, profile sync, env linking, and model discovery need **zero changes**.
- **Credential deletion blast-radius** — reuse `get_deletion_impact` / `AICredentialInUseError` for removed members and parent delete.
- **SecurityEvent** — per-child + parent batch events.
- **API client regen** — after backend changes run `bash scripts/generate-client.sh` (new `ManagedAICredential*` types + reworked `AdminLlmProvidersService`).
- **Docs** — update `docs/application/ai_credentials/admin_ai_credential_provisioning.md` (+ `_tech.md`) to describe the parent/child model and reconcile; the per-user-row provisioning narrative is superseded.

## Future Enhancements (Out of Scope)

- Explicit `managed_ai_credential_member` join table to persist *intended* members even when a child create fails (retry queue).
- Per-member overrides (different default/SDK wiring per user under one record).
- Bulk key rotation across multiple records; scheduled key expiry rotation.
- Workspace-scoped managed records (currently fleet-wide superuser only).
- Live WebSocket push so an affected user's UI reflects a newly provisioned credential without refresh.

## Summary Checklist

**Backend**
- [ ] Add `ManagedAICredential` table model + `managed_credential_id` FK on `AICredential` (`backend/app/models/credentials/`), re-export in `models/__init__.py`.
- [ ] Add DTOs: `ManagedAICredentialCreate/Update/Public`, `ManagedAICredentialMember`, `ManagedAICredentialReconcileResult`.
- [ ] Create `managed_ai_credentials_service.py` with `create / update / delete / reconcile / set_default_all / list / get / _to_public`.
- [ ] Reconcile delegates child create/update/delete/set-default to `ai_credentials_service` (encryption, validation, profile sync, blast-radius all reused).
- [ ] Rework `admin_llm_providers.py` routes to parent-oriented CRUD + set-default + test-connection; remove per-row endpoints; emit SecurityEvents.
- [ ] Add parent-aware Test Connection path (resolve stored key when `api_key` blank).
- [ ] Alembic migration: create table, add FK column + indexes (no data backfill — DB has no existing admin-managed rows); downgrade drops both.

**Frontend**
- [ ] Convert list to parent records (`LlmProvidersTable.tsx`); drop string-grouping; parent-level actions menu.
- [ ] Unify create/edit into one dialog with `mode` + pre-fill + `UserAllowlistPicker` member add/remove + Test Connection + set-default/SDK-defaults switches.
- [ ] Update `llm-providers.tsx` to query parent records, paginate, keep Filter + Provision header.
- [ ] Handle reconcile result (skipped/blocked toasts) and 409 delete→force confirm.
- [ ] Regenerate API client (`bash scripts/generate-client.sh`).

**Docs & Testing**
- [ ] Update `admin_ai_credential_provisioning.md` + `_tech.md` for the parent/child + reconcile model.
- [ ] Verify: create record provisions children for all targets; child rows behave identically to today (default resolution, env creation).
- [ ] Verify: edit removes/adds members correctly; removing a bundle-referenced member is blocked (409) and `force` overrides.
- [ ] Verify: key rotation writes through to all children; adding a member with blank key uses the stored key.
- [ ] Verify: delete cascades children with blast-radius gating; parent removed.
- [ ] Verify: migration applies cleanly (schema-only, no backfill) and downgrade is clean.
```
