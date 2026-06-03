# Plan: Per-User-Scoped Credential Provisioning for Shared Agent Bundles

## Goal

Let a bundle publisher predefine, **per installing user**, which slice of an
upstream capability that user may access — without hardcoding user identities in
the producer agent and without leaking the over-privileged upstream credential.

The mechanism: a bundle ships **two** credentials.

1. The **`agent_api` connection credential** — general access to producer Agent
   A's capability-narrowed REST API. Shared to all installers via the existing
   **publisher-provided (PBP) / one-shared-token** model. A "dumb pipe" with no
   per-user authority.
2. A **per-user "second token"** (`api_token` type) carrying that user's company
   scope. Provisioned as user-provided (PBU) or template (PBT), **pre-shared by
   the publisher to each specific user**, and **auto-detected/attached at
   install** so the experience is "install and it just works."

Per-user authority lives **entirely in the second token**, validated
server-side inside Agent A (requested companies ⊆ token's allowed set). The
`policy.yaml read_only` rule blocks write verbs at the proxy edge. Revoking a
user = revoking that user's second-token `CredentialShare`.

### The blocker this plan removes

The install-time auto-prefill matcher (`CredentialsService.find_match_for_spec`)
keys on `(lower(name), type)`. At publish the spec's `name` comes from the
publisher's single linked credential; each installer's pre-shared per-user token
is a **different** credential with a **different** name (e.g. "Company A Token
for Agent B"). Name matching never matches → auto-detect fails → every user
lands in `needs_setup`. We need a stable slot/audience identifier decoupled from
the human name.

---

## Security Invariants (preserved across all phases)

- **I1 — Upstream secret never travels.** Agent A's powerful upstream credential
  stays in A's container. Only the narrowed proxy surface is exposed. **Never
  bundle Agent A** (the producer) — only Agent B (the consumer). Bundling A would
  ship the upstream secret.
- **I2 — Authority lives in the second token, validated server-side in A.**
  Requested companies ⊆ the token's allowed set; out-of-scope rejected inside A.
  The platform proxy enforces `policy.yaml` (method allowlist / read-only) at the
  edge. The connection credential carries **no** per-user authority.
- **I3 — Revocation = revoking that user's second-token `CredentialShare`.** The
  `InstallReadinessGate` then leaves the user in `needs_setup` (no token = no
  access). The connection credential's share is independent and shared to all.
- **I4 — `service_uri` is a non-secret audience/slot id.** It is plaintext, never
  encrypted, never carries authority by itself. It only steers *which* credential
  the matcher suggests; the token value still gates access.
- **I5 — Backward compatibility.** Specs/credentials with `service_uri = NULL`
  behave exactly as today. The new tier is additive and tried before the existing
  name tiers; legacy installs are untouched.

---

## Decisions (confirmed) & Open Questions for the builder

### Confirmed
- **One-shared-token model** for the `agent_api` connection credential (PBP).
  Accepted trade-offs: a single shared per-token rate-limit budget and a single
  producer env serving all installs' traffic. Per-install isolation /
  token-per-install remains future work.
- **`service_uri` is the top-precedence match tier**, tried before owned-name and
  shared-name tiers. Format is an opaque publisher-chosen string, e.g.
  `agent-b://company-scope-token`.
- The PBP backend pipeline is already fully generic over credential type and
  handles `agent_api` end-to-end. Part 2 is **enablement + UI + verification +
  docs**, not new backend plumbing.

### Resolved decisions (builder-confirmed 2026-06-02)
- **OQ1 — `service_uri` match vs PBT value-anchor precedence → RESOLVED: YES.**
  The `service_uri` tier runs first and **short-circuits, even on the PBT path** —
  a `service_uri` match wins over value-anchoring. Rationale: the slot id is the
  stronger, intentional signal (the publisher explicitly stamped both the spec and
  each user token with the same `service_uri`); value-anchoring is a heuristic that
  can't disambiguate an all-private token anyway. Implement P2 accordingly.
- **OQ2 — Whitelist `service_uri` into the container → RESOLVED: NO (out of scope).**
  Do not sync `service_uri` into the container. Agent A continues to validate the
  token's allowed-company set server-side (I2); `service_uri` stays a platform-side
  matching/organization field only. Leave it as a future hardening hook, unbuilt.
- **OQ3 — Post-install "re-run matcher" affordance → RESOLVED: OUT OF SCOPE.**
  Pre-sharing before install is the expected, documented flow. Do NOT build an
  auto-rematch flow or a setup-page "newly-shared credential" hint. P4 only
  documents the share-before-install ordering constraint; the manual-link-from-
  Credentials-tab path is the accepted fallback when a token is shared late.

---

## Three Parts → Phasing

| Part | Theme | Phases |
|------|-------|--------|
| Part 1 | `service_uri` credential discriminator (the main new code) | P1, P2, P5 |
| Part 2 | Bless PBP one-shared-token bundling of `agent_api` credentials | P3, P5 |
| Part 3 | End-to-end share-via-UI + ordering constraints | P4, P5 |
| Verification | Dedicated testing phase across all parts | P6 |

Phases are ordered so each is independently shippable and testable.

---

## Data Model Changes

### `Credential.service_uri` (new column)

- **Table:** `credential`
- **Column:** `service_uri TEXT NULL` (nullable plaintext, no encryption — I4).
- **Semantics:** an audience/slot id. The publisher's linked connection-or-token
  credential AND every per-user token for the same slot carry the **same**
  `service_uri`; their `name` and token values differ.
- **Index:** partial btree index
  `ix_credential_service_uri (service_uri) WHERE service_uri IS NOT NULL` — the
  matcher filters on `service_uri = :spec_value AND type = :type`, and the vast
  majority of rows will have `NULL`, so a partial index keeps it small.
- **Model file:** `backend/app/models/credentials/credential.py`
  - Add `service_uri: str | None` to `CredentialBase` (so it flows through
    `CredentialCreate`, `CredentialPublic`, and the DB model automatically).
  - Add `service_uri: str | None = None` to `CredentialUpdate` (editable).
  - Add the partial `Index(...)` to `Credential.__table_args__`.
  - `service_uri` is **not** sensitive → it appears in `CredentialPublic`
    (no redaction needed; do NOT add to any private/redaction lists).
- **Migration:** new Alembic revision (see Database Migrations below).

### `ParsedCredentialSpec.service_uri` (new field)

- **File:** `backend/app/services/bundles/credential_spec.py`
- Add `service_uri: str | None` to the frozen `ParsedCredentialSpec` dataclass.
- In `parse_credential_spec`, read `spec.get("service_uri")`, coalesce to
  `None` when missing/non-string. Fully backward compatible — old revision JSON
  has no `service_uri` key → `None`.

### `InstallContextSpec.service_uri` (new field, optional surfacing)

- **File:** `backend/app/models/bundles/catalog.py`
- Add `service_uri: str | None = None` to `InstallContextSpec` so the install
  screen can render the slot id (informational; optional for MVP UI but cheap to
  carry). No change to `InstallContextPublisherSummary`.

---

## Phase 1 — `service_uri` model, spec plumbing, publish snapshot

**Scope:** introduce the column and carry it end-to-end through publish so it
lands in `required_credential_specs`. No matcher change yet (so legacy behavior
is unchanged and this phase is a safe, additive substrate).

**Files to touch:**
1. `backend/app/models/credentials/credential.py` — add `service_uri` to
   `CredentialBase`, `CredentialUpdate`, and the partial index in
   `__table_args__` (per Data Model above).
2. `backend/app/alembic/versions/<new>_add_credential_service_uri.py` — new
   migration (see Database Migrations).
3. `backend/app/services/bundles/credential_spec.py` — add `service_uri` field +
   parsing to `ParsedCredentialSpec` / `parse_credential_spec`.
4. `backend/app/services/bundles/publish_service.py` — in
   `_collect_credential_specs` (~line 503) add `"service_uri": cred.service_uri`
   to each emitted spec dict. Because the manifest feeds `content_hash`
   (`_hash_tree_with_manifest`), stamping/changing a `service_uri` yields a new
   hash → installs see a pending update (consistent with the snapshot pattern).
5. `backend/app/models/bundles/catalog.py` — add `service_uri` to
   `InstallContextSpec`.
6. `backend/app/api/routes/credentials.py` — verify create/update accept and
   persist `service_uri` (should be automatic via `CredentialBase` /
   `CredentialUpdate`; confirm no field-allowlist drops it).

**Security invariants preserved:** I4 (plaintext, non-secret), I5 (NULL =
legacy). No behavior change to matching yet.

**Verification / acceptance:**
- Publishing a bundle whose linked credential has a `service_uri` writes that
  value into `revision.required_credential_specs` and `manifest`.
- Publishing a credential with `service_uri = NULL` emits specs identical to
  today (no `service_uri` key influence; value is `None`).
- `parse_credential_spec` round-trips `service_uri`; old JSON → `None`.
- Migration applies cleanly on a DB seeded with existing credentials (all
  backfill to `NULL`).

---

## Phase 2 — `service_uri` top-precedence matcher tier

**Scope:** add the new top tier to `find_match_for_spec` and thread the spec's
`service_uri` from the install-context builder into the matcher.

**Files to touch:**
1. `backend/app/services/credentials/credentials_service.py` —
   `find_match_for_spec` (~line 1594):
   - Add a keyword-only `service_uri: str | None = None` parameter.
   - **Before** the existing owned-name and shared-name tiers, add the
     `service_uri` tier (only when `service_uri` is a non-empty string):
     - **Tier 0a (owned):** `Credential.owner_id == user_id AND type == enum AND
       Credential.service_uri == service_uri`, order by `id desc`, return first.
     - **Tier 0b (shared):** same predicate joined through `CredentialShare`
       (`shared_with_user_id == user_id`), return first.
   - **OQ1 resolution (assumed yes):** the `service_uri` tier runs and
     short-circuits **even on the PBT path** (i.e. before the value-anchor
     check). Rationale in OQ1. When `service_uri` is set, the slot id is
     authoritative for steering; the token value still gates access at runtime
     (I2). When `service_uri` is `NULL`/empty, fall through to the existing
     PBT value-anchor / name / type-only behavior unchanged.
   - Update the method docstring's "Match precedence" block to document the new
     tier and the PBT interaction.
2. `backend/app/services/bundles/catalog_service.py` — `build_install_context`
   (~line 308): pass `service_uri=parsed.service_uri` into **both**
   `find_match_for_spec` calls (the `template` call and the PBU/default call).

**Design notes:**
- The `service_uri` tier is **owned-before-shared**, mirroring the existing
  tier ordering. For the per-user-token scenario the match is almost always in
  Tier 0b (shared), since the publisher pre-shares the token to the installer.
- Keep the tier strictly additive: when `service_uri` is `None` the function is
  byte-for-byte equivalent to today (I5).

**Security invariants preserved:** I2 (matcher only suggests; runtime token still
gates), I4, I5.

**Verification / acceptance:** covered in Phase 6 (the bulk of matcher tests).
Smoke acceptance for this phase:
- A spec with `service_uri = S` and a shared credential with `service_uri = S`
  (different name) now yields a `suggested_credential_id` on the install screen.
- A spec with `service_uri = NULL` produces the same suggestion as before the
  change for the same fixtures.

---

## Phase 3 — Bless PBP one-shared-token bundling of `agent_api` credentials

**Scope:** the backend PBP pipeline already handles `agent_api` generically
(verified: `_collect_credential_specs` has no type filter; `resolve_provided_by`
returns `"publisher"` when `allow_sharing=True`; `_validate_publisher_provides`
is generic; `_try_link_publisher_credential` creates `CredentialShare` +
`AgentCredentialLink` generically; existing sync writes `{base_url, token, …}`
into the installer container with host-rewrite via
`_rewrite_agent_api_urls_for_env`). So this phase is **enablement + UI + docs**.

**Files to touch (mostly frontend + verification):**
1. **Provisioning panel surfaces `agent_api`-typed linked credentials.**
   `frontend/src/components/Agents/CredentialProvisioningSection.tsx` — verify
   that an `agent_api`-typed credential linked to the publisher install appears
   as a *linked* credential the publisher can mark `provided_by="publisher"`.
   The `agent_api` type is "display-only, not in the manual add picker"; confirm
   it is **not** filtered out of the provisioning list. If filtered, remove the
   filter for the linked-credential path (inference already covers `provided_by`
   even without UI, but the publisher needs to *see* and confirm it).
2. **Install page PBP summary shows the `agent_api` spec.**
   `frontend/src/components/Install/InstallServiceCredentialItem.tsx` — confirm a
   `provided_by="publisher"` spec of type `agent_api` renders its publisher
   summary (name + type) and does not get hidden by a type filter.
3. **Sharing card lets the publisher flip `allow_sharing=True` on an
   `agent_api` credential.** `frontend/src/components/Credentials/CredentialSharing.tsx`
   — the connect helper creates the credential with `allow_sharing=False` by
   default (`agent_api_token_service.py` ~line 214). Confirm the Sharing card is
   rendered for `agent_api`-typed credentials (subject to the existing role
   gating — `agent-developer`/`admin` only) so the publisher can toggle it.
4. **Docs:** `docs/agents/agent_api/agent_api.md` Known Gaps (lines ~196–200):
   flip "Not Implemented" → "Supported (one-shared-token model)". Document the
   accepted trade-offs (shared per-token rate-limit budget; single producer env
   serving all installs) and that per-install isolation / token-per-install
   remains future work. Update the integration note at ~line 190 to match.

**Security invariants preserved:** I1 (only the narrowed proxy connection is
shared, never Agent A's upstream secret — cross-user safe by construction; the
consumer proxy authenticates on the `agent_api_token` alone), I3.

**Verification / acceptance:** covered in Phase 6 test #2. Acceptance for this
phase:
- Publisher can mark an `agent_api` credential `provided_by="publisher"` in the
  provisioning panel and publish without validation error.
- Install of that bundle creates a `CredentialShare` + `AgentCredentialLink` for
  the connection credential and syncs `{base_url, token}` into the installer's
  container.

---

## Phase 4 — End-to-end share-via-UI flow & ordering constraints

**Scope:** make the full two-credential provisioning flow coherent and
documented, and surface the install-time ordering constraint.

**Flow (publisher side):**
1. Publisher pre-creates each per-user second-token credential (`api_token`),
   sets its `service_uri` to the bundle's slot id, and either enables
   `allow_template_sharing` or shares directly.
2. Publisher shares each token to the correct installing user via the existing
   Credential Sharing UI (`CredentialSharing.tsx`).
3. Publisher marks the bundle's connection credential `provided_by="publisher"`
   (Phase 3) and the per-user token spec `provided_by="user"` (PBU) — the
   token's *value* is each user's own, only the slot id (`service_uri`) is
   shared across the spec and the tokens.

**Files to touch:**
1. `frontend/src/components/Credentials/CredentialFields/ApiTokenFields.tsx` (and
   the generic/edit form `CredentialForms/GenericCredentialForm.tsx` /
   `EditCredential.tsx`) — add an **optional `service_uri` field**, surfaced for
   `api_token` and optionally `agent_api`. Helper text: "Audience/slot id shared
   across all per-user tokens for the same bundle; not secret."
2. `frontend/src/components/Install/InstallServiceCredentialItem.tsx` — optionally
   render the spec's `service_uri` (informational) so the installer understands
   why a differently-named credential was auto-suggested.
3. **Ordering-constraint UX/docs** (OQ3, document-don't-build):
   - In `docs/agents/agent_credentials/credential_sharing.md` and
     `docs/agents/agent_bundles/agent_bundles.md`, document: auto-prefill runs at
     install time, so per-user credentials must be shared **before** the user
     installs. If shared later, the install already created a placeholder and the
     user links manually from the Credentials tab.
   - Decision recorded in OQ3: no auto-rematch flow in MVP.
4. **Client regen:** run `make gen-client` (see Integration Points) after the
   Phase 1–3 backend field additions so `service_uri` appears on the generated
   types/services the frontend consumes.

**Runtime gate confirmation:** the `InstallReadinessGate._scan_service_credentials`
already leaves an un-provisioned user in `needs_setup`: an unmatched PBU spec
creates an installer-owned placeholder (`is_placeholder=True`) →
`placeholder_empty` → `needs_setup` (no token = no access; I3). When the matcher
*does* suggest a shared token and the install form submits
`mode="use_existing"` with that id, the shared row is linked and the gate passes.
No gate code change required; this phase **verifies** that behavior.

**Security invariants preserved:** I3 (no token ⇒ needs_setup), I4.

**Verification / acceptance:** covered in Phase 6 tests #3 and #4.

---

## Phase 5 — Documentation

**Files to touch:**
1. `docs/agents/agent_credentials/credential_sharing.md` — add a
   "`service_uri` slot id" subsection to the match-precedence / sharing-modes
   discussion; document the new top matcher tier and the ordering constraint.
2. `docs/agents/agent_bundles/agent_bundles.md` — document `service_uri` in the
   `required_credential_specs` match-precedence section and the per-user-token
   provisioning pattern.
3. `docs/agents/agent_api/agent_api.md` — Known Gaps flip (also referenced in
   Phase 3).
4. `docs/README.md` — touch the `agent_credentials` / `agent_api` registry blurbs
   if the new capability warrants a one-line mention (per the "no feature
   counters" convention — prose only).
5. Optionally the `_tech` companions (`credential_sharing_tech.md`,
   `agent_bundles_tech.md`) for the matcher tier and column.

**Acceptance:** docs describe the two-credential model, the `service_uri` tier,
the one-shared-token PBP `agent_api` posture, and the share-before-install
ordering rule.

---

## Phase 6 — Verification / Testing (dedicated)

Follow `backend/tests/README.md` (API-only, scenario-based, no direct DB access)
and the agents domain `backend/tests/api/agents/README.md`. Mirror existing
patterns. Relevant existing files to extend/mirror:
- `backend/tests/api/agents/agents_bundles_install_credential_match_test.py`
- `backend/tests/api/agents/agents_bundles_install_context_test.py`
- `backend/tests/api/agents/agents_bundles_credential_specs_test.py`
- `backend/tests/api/agents/agents_bundles_install_readiness_test.py`
- `backend/tests/api/agents/agents_bundles_e2e_install_flow_test.py`
- `backend/tests/api/agents/agents_agent_api_test.py` (connect helper +
  `EnvironmentTestAdapter` stubs)

### Test group 1 — `service_uri` matcher precedence
- **`service_uri` beats name:** spec `service_uri=S`, type `api_token`; a shared
  credential with `service_uri=S` but a **different name** is suggested, while a
  same-name credential **without** `service_uri` is NOT preferred over it.
- **Owned before shared:** when both an owned and a shared credential carry
  `service_uri=S`, the owned one is suggested.
- **NULL `service_uri` = legacy:** spec with no `service_uri` produces the exact
  same suggestion as the pre-change behavior for the same fixtures (regression
  guard — extend existing context tests).
- **PBT value-anchor interaction (OQ1):** a PBT spec with `service_uri=S` matches
  the `service_uri`-tagged shared/owned credential **even when** its decrypted
  data would fail value-anchoring; and when `service_uri` is absent, the existing
  value-anchor behavior is unchanged.
- **Divergent-name per-user tokens auto-detect:** two specs/credentials sharing a
  `service_uri` but with distinct human names now produce a suggestion (the
  original blocker is gone).

### Test group 2 — PBP `agent_api` one-shared-token via bundle (integration)
- Publish a bundle whose linked `agent_api` credential has `allow_sharing=True`
  and `provided_by="publisher"`.
- Install as a second user → assert a `CredentialShare` exists (publisher →
  installer) and an `AgentCredentialLink` for the install.
- Assert the connection syncs `{base_url, token}` into the installer's container
  (use the `EnvironmentTestAdapter` stubs as in `agents_agent_api_test.py`).
- Assert a consumer proxy call authenticates on the token (mirror the existing
  agent_api consumer-call test path).

### Test group 3 — Per-user scoped end-to-end
- Two installers; two **different** pre-shared `api_token` second tokens with the
  **same** `service_uri`.
- Each install's auto-detect (`build_install_context`) suggests the correct
  per-user token (installer A → token A, installer B → token B).
- After install with `mode="use_existing"` on the suggested id, each install's
  readiness gate is `ready`; an installer with **no** pre-shared token lands in
  `needs_setup` (placeholder), confirming I3.

### Test group 4 — Credential sharing via UI ordering
- **Share-before-install:** token shared first → install auto-links it (gate
  `ready`).
- **Manual-link-after fallback:** token shared *after* install → install created
  a placeholder (`needs_setup`); after the user manually links the now-shared
  token via the Credentials tab path, the gate flips to `ready`.

### Running
```bash
make test-backend
docker compose exec backend python -m pytest tests/api/agents/agents_bundles_install_credential_match_test.py -v
docker compose exec backend python -m pytest tests/api/agents/agents_agent_api_test.py -v
```
Prerequisite: Docker services running (`make up`). Per project convention, the
user runs the full suite (`make test-backend`); the implementer runs targeted
files plus the agents + credentials domain regression.

---

## Database Migrations

- **New revision:** `backend/app/alembic/versions/<rev>_add_credential_service_uri.py`.
- **Current single head is `d54391bd8cf2`** (verified via `alembic heads` at plan
  time — the prior multi-head condition noted in older memory no longer applies).
  Set `down_revision = "d54391bd8cf2"` so the chain stays single-headed. **Re-run
  `alembic heads` immediately before creating the migration** in case head moved.
- **Upgrade:**
  - `op.add_column("credential", sa.Column("service_uri", sa.Text(), nullable=True))`
  - `op.create_index("ix_credential_service_uri", "credential", ["service_uri"],
    postgresql_where=sa.text("service_uri IS NOT NULL"))`
- **Downgrade:** drop the index then the column. No data backfill (all existing
  rows are `NULL` = legacy behavior, I5).
- Generate via `make migration` (Docker), then **review and edit** the
  autogenerated file (autogen may emit a non-partial index; hand-edit to the
  partial `postgresql_where`).

---

## Integration Points

- **OpenAPI client regeneration (required).** After the Phase 1–3 backend field
  additions (`service_uri` on `CredentialPublic`/`CredentialCreate`/
  `CredentialUpdate`/`InstallContextSpec`), run:
  ```bash
  source ./backend/.venv/bin/activate && make gen-client
  ```
  This regenerates `frontend/src/client/` so the credential form and install
  components can read/write `service_uri`. Do this before Phase 4 frontend work.
- **Credential sync pipeline** — `service_uri` is metadata only; it does **not**
  enter the synced container payload (no change to whitelist/redaction). The
  `agent_api` connection payload (`{base_url, token}`) is unchanged.
- **Bundle content hash** — `service_uri` in the manifest means stamping it
  produces a pending update on existing installs (expected; consistent with the
  snapshot pattern).
- **Role gating** — Sharing/provisioning UI remains `agent-developer`/`admin`
  only (existing convention); `agent-user` installers consume transparently.

---

## Error Handling & Edge Cases

- **`service_uri` set on spec but no matching credential shared to the user** →
  no suggestion → PBU placeholder → `needs_setup` (correct; I3).
- **Two owned credentials share the same `service_uri`** → Tier 0a returns the
  newest by `id desc` (deterministic, mirrors existing tier behavior). Document
  that a `service_uri` should be unique per (user, slot); collisions resolve to
  most-recent, not an error.
- **Publisher revokes the connection credential's `allow_sharing`** →
  `_try_link_publisher_credential` falls through to placeholder + degraded; gate
  reports `publisher_broken` (existing behavior).
- **Token shared after install** → manual-link fallback (OQ3).
- **Unknown/legacy revision JSON** → `service_uri` parses to `None`; legacy path
  (I5).

---

## Future Enhancements (Out of Scope)

- **Token-per-install** isolation for the `agent_api` connection (the deferred
  half of the original agent_api §6.2 decision).
- **`service_uri` whitelisted into the container** for Agent-A-side audience
  validation as defense-in-depth (OQ2).
- **Post-install auto-rematch** affordance when a credential is shared after
  install (OQ3).
- **Per-endpoint token scopes** / usage analytics (inherited from agent_api §12).

---

## Summary Checklist

### Backend
- [ ] Add `service_uri` to `CredentialBase` + `CredentialUpdate` + partial index
      (`credential.py`).
- [ ] New Alembic migration (`down_revision = current single head`; partial
      index hand-edited).
- [ ] Add `service_uri` to `ParsedCredentialSpec` + `parse_credential_spec`.
- [ ] Emit `service_uri` in `_collect_credential_specs` (publish).
- [ ] Add `service_uri` to `InstallContextSpec`.
- [ ] Add `service_uri` top tier (owned, then shared) to `find_match_for_spec`,
      short-circuiting before name/PBT tiers (OQ1).
- [ ] Pass `service_uri` from `build_install_context` into both matcher calls.
- [ ] Verify create/update routes persist `service_uri`.

### Frontend
- [ ] `make gen-client` after backend field additions.
- [ ] Optional `service_uri` field on `api_token` (and optionally `agent_api`)
      create/edit forms.
- [ ] Provisioning panel surfaces `agent_api` linked credentials (not filtered).
- [ ] Install page PBP summary + optional `service_uri` display.
- [ ] Sharing card flips `allow_sharing=True` on `agent_api` credentials.

### Docs
- [ ] `credential_sharing.md` — `service_uri` tier + ordering constraint.
- [ ] `agent_bundles.md` — `service_uri` in match precedence + per-user pattern.
- [ ] `agent_api.md` — flip Known Gap to "Supported (one-shared-token model)".
- [ ] `docs/README.md` — prose registry touch-up if warranted.

### Testing & validation
- [ ] Matcher precedence (service_uri beats name; owned-before-shared; NULL =
      legacy; PBT interaction; divergent-name auto-detect).
- [ ] PBP `agent_api` one-shared-token install → share + link + container sync +
      proxy auth.
- [ ] Per-user scoped end-to-end (two installers, two tokens, same `service_uri`;
      no-token ⇒ `needs_setup`).
- [ ] Share-before-install vs manual-link-after fallback.
- [ ] `make test-backend` + agents/credentials domain regression green.
