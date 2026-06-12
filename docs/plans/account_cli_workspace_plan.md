# Account CLI Workspace — Implementation Plan

> Account-level cinna CLI session. A user bootstraps **one** local "account
> workspace" from a Settings card (no per-agent click-through), and from there
> a local coding agent (Claude Code) can list the user's agents, attach
> standard per-agent dev workspaces on demand, and exec into any agent it has
> building rights on — with zero further UI interaction.

This is conceptually the **General Assistant prototype re-delivered as a local
workspace**: the account token is the credential, the local coding agent is the
orchestrator, and (Phase 2) the General Assistant's platform-docs snapshot +
generated API reference become the context package shipped at account setup.

---

## Overview

Today, building a multi-agent network from a local coding assistant means:
open each agent's Integrations tab → click **Setup** → run each bootstrap
one-liner → wire inter-agent connections in the UI. This feature collapses that
to a single account-level bootstrap plus CLI verbs.

Core capabilities (full feature, phased below):

- **One account bootstrap** — `Settings → Local Development` card emits a
  `curl | python3` one-liner (same pattern as the existing per-agent flow) that
  produces a local **account workspace** (`.cinna/account.json` + orchestrator
  `CLAUDE.md`).
- **Server-side agent discovery** — `cinna account agents` lists the agents the
  user can access, flagging which ones grant *building rights*.
- **On-demand per-agent sync** — `cinna agent sync <agent>` mints a normal
  per-agent CLI token programmatically (no UI) and materialises a **standard**
  cinna workspace under `agents/<agent>/`; `cinna agent unsync <agent>` tears it
  down. The result is byte-for-byte what `cinna setup` produces today — only the
  token's *provenance* differs.
- **Account-root exec** — `cinna exec --agent <agent> <cmd>` runs in any
  building-rights agent's remote env from the account root.
- **(Later)** convenience verbs (`agent create`, `connect agent-api|mcp`),
  generic `cinna api` escape hatch, agentic-team registration.

### High-Level Flow (Phase 1)

```
Settings → Local Development card
        │  POST /api/v1/cli/account/setup-tokens   (require_developer)
        ▼
account setup-token (15-min, single-use)
        │  curl -sL …/api/cli-setup/account/{token} | python3 -
        ▼  POST /api/cli-setup/account/{token}      (token = credential)
ACCOUNT CLI TOKEN  (token_type="cli-account", agent_id=NULL)
        │  stored in ./my-cinna/.cinna/account.json
        │
        ├── GET  /api/v1/cli/account/agents              → discovery (can_build flags)
        │
        ├── POST /api/v1/cli/account/agents/{id}/mint     (can_build gate)
        │        └─► mints NORMAL per-agent CLIToken (token_type="cli",
        │            minted_by_account_token_id=<account token id>)
        │            → cinna writes agents/<agent>/.cinna/config.json
        │              + ~/.cinna/agents.json entry  (standard workspace)
        │
        └── exec --agent → reuses the minted per-agent token against the
                           existing POST /api/v1/cli/agents/{id}/exec
```

Revoking the account token (Settings card) cascade-revokes every child token it
minted; next CLI call on any child gets 401 and Mutagen pauses — identical to
today's per-agent disconnect.

---

## Architecture Overview

### Reuse, don't rebuild

The existing **Cinna CLI Integration** feature
(`docs/application/cinna_cli_integration/`) already provides everything a
per-agent workspace needs: setup-token → CLI-token exchange, the
`cli_token` model, `_resolve_cli_context`, rolling 7-day expiry, Mutagen sync
tunnel, `cinna exec` SSE streaming, `~/.cinna/agents.json` registry, and
`cinna disconnect`. **The account feature adds an authentication spine on top —
it does not duplicate the per-agent runtime.**

The single new idea is **child-token minting**: the account token cannot itself
sync or exec; it mints *normal* per-agent CLI tokens. Once minted, those tokens
flow through the *unchanged* per-agent endpoints and the *unchanged* CLI dev
loop. A synced child workspace is indistinguishable from one set up by hand —
it shows up in the agent's Integrations-tab session list as a normal CLI session
and `cinna disconnect` inside it behaves normally.

### The three pillars of Phase 1

1. **Account token** — a new `token_type` on the existing `cli_token` table
   (nullable `agent_id`), plus a `minted_by_account_token_id` self-FK on child
   tokens for the cascade. New account-scoped setup-token flow and bootstrap
   route. New `AccountCLIContext` dep that authenticates the account token and
   explicitly *cannot* resolve a single agent.

2. **`can_build(user, agent)` helper unification** — one predicate
   = `(developer-or-admin role) AND (not a foreign install)`. It becomes the
   authorization gate for: the new mint/discovery endpoints, **and** the
   existing per-agent CLI setup-token creation (today ownership-only — a
   pre-bundle bug we fix here). Every UI-relevant "can this user build this
   agent?" call-site routes through it.

3. **Accessible-agents listing** — a projection endpoint the account token can
   read that returns the agents the user can *access* (own + workspace-visible),
   each flagged `can_build` / `is_foreign_install`, without leaking data the
   user can't see and without exposing credential values.

### Data flow (mint)

```
account token ──auth──► AccountCLIContext (user resolved, NO agent)
        │
   POST /account/agents/{agent_id}/mint
        │  1. load agent; 2. can_build(user, agent) else 403
        │  3. CLIService.exchange_setup_token-equivalent: build a normal
        │     per-agent CLIToken, stamp minted_by_account_token_id
        │  4. SecurityEvent(CLI_ACCOUNT_CHILD_TOKEN_MINTED)
        ▼
returns CLITokenCreated (full JWT shown once) + full AgentPublic-ish record
        │
   cinna writes agents/<agent>/.cinna/config.json + ~/.cinna/agents.json
        ▼
   normal per-agent dev loop (sync / exec / disconnect) — unchanged endpoints
```

### Integration with existing systems

- **cinna_cli_integration** — the host of all reused machinery; this plan adds a
  sibling `account/*` route group and an `AccountCLIContext` dep next to
  `CLIContext`.
- **user_roles** — `can_build` builds on `RoleService.is_developer`.
- **agent_bundles** — `can_build`'s foreign-install half reuses
  `_is_foreign_install()` (`bundle_uuid is not None and not is_publisher_install`).
- **general_assistant** — Phase 2 factors its docs-snapshot + API-reference into
  a shared backend service consumed at account setup.
- **events / security_event** — every account-token creation and child mint
  writes a `SecurityEvent`.

---

## Data Models

### Modified: `cli_token` (account token + child provenance)

The account token lives in the **same table** as per-agent CLI tokens (decision
1). Two changes:

| Field | Change | Rationale |
|-------|--------|-----------|
| `agent_id` | `uuid \| None` — make **nullable**, keep `ondelete="CASCADE"` | Account tokens have no single agent. Per-agent tokens keep a non-null `agent_id`. |
| `token_type` | **new** `str` column, `default="cli"`, indexed | `"cli"` = per-agent (existing), `"cli-account"` = account token. Mirrors the JWT `token_type` claim already used by `decode_cli_jwt`. |
| `minted_by_account_token_id` | **new** `uuid \| None`, `FK -> cli_token.id`, `ondelete="CASCADE"`, nullable, indexed | Child-token provenance. Self-referential FK on the same table. CASCADE ⇒ deleting the account row deletes child rows; but deletion is not how we revoke (see lifecycle), so the cascade is a safety net, not the primary mechanism. |

Notes:
- The DB `agent_id` becoming nullable does **not** weaken per-agent security:
  per-agent tokens are still minted with a concrete `agent_id`, and
  `_resolve_cli_context` still requires a non-null agent (account tokens are
  rejected by it — see deps below).
- `name` for an account token holds the machine name (e.g. "My MacBook"); same
  as per-agent.
- No new `expires_at` semantics: account tokens use the **same 7-day rolling
  expiry** (decision 1). `CLIAuthService.refresh_token_usage` is reused; for the
  account token there is no environment to bump, so the env-keepalive call is
  skipped when `environment is None`.

### New `token_type` constant + JWT claim

- `CLITokenPayload.token_type` already exists (default `"cli"`). Add
  `"cli-account"` as a recognised value.
- `create_cli_jwt` gains an optional `token_type="cli"` parameter and an
  optional `agent_id: uuid | None` (account tokens pass `agent_id=None`,
  `token_type="cli-account"`). The JWT for an account token therefore omits
  `agent_id` (or sets it null).

### New: account setup token — reuse `cli_setup_token` with nullable `agent_id`

The account bootstrap needs a short-lived single-use setup token, exactly like
the per-agent flow. Reuse the **same `cli_setup_token` table**:

| Field | Change |
|-------|--------|
| `agent_id` | make **nullable** (`uuid \| None`, FK CASCADE retained). Account setup tokens have `agent_id=NULL`. |
| `kind` (or reuse `token_type`) | **new** `str` column, `default="agent"`; `"account"` for account setup tokens. |

`environment_id` stays nullable (already is); account setup tokens leave it
`NULL`.

> Alternative considered and rejected: a separate `cli_account_setup_token`
> table. Reusing `cli_setup_token` keeps the cleanup scheduler, the exchange
> path, and the 15-min/single-use semantics in one place. The `kind` column
> disambiguates.

### Indexes

- `ix_cli_token_token_type` (btree on `token_type`) — list account tokens for a
  user, and filter child tokens.
- `ix_cli_token_minted_by` (btree on `minted_by_account_token_id`) — fast
  cascade-revoke lookup ("all children of this account token").
- Existing `ix_cli_token_token_hash` (unique) and `ix_cli_token_owner_agent`
  unchanged. Note: `ix_cli_token_owner_agent` is composite on
  `(owner_id, agent_id)`; with nullable `agent_id` this remains valid (NULLs are
  allowed in non-unique btree).

### Lifecycle states

```
account setup-token: pending ──(exchange)──► used   (15-min TTL, single-use)
account CLI token:   active  ──(revoke from Settings)──► revoked
                              └─(7-day inactivity)──────► expired
child CLI token:     active  ──(account token revoked)──► revoked (cascade)
                              ├─(cinna disconnect / UI Disconnect)──► revoked
                              └─(7-day inactivity)──────────────────► expired
```

---

## Security Architecture

### Account token capability exclusions (decision 6)

The account token is a **mint-and-discover** credential only. It must NOT be
able to:

- read credential values (no credential endpoints accept it),
- manage other users or touch admin surfaces,
- act as a per-agent token (it is rejected by `_resolve_cli_context`).

Enforced structurally: the account token only authenticates through the new
`AccountCLIContext` dep, which is wired to **exactly** the `account/*` routes
listed below. It is never accepted by `CLIContextDep`/`CLIContextWSDep` (those
decode `token_type` and reject `"cli-account"`), so it physically cannot reach
sync/exec/credential/knowledge endpoints. Conversely, child per-agent tokens are
ordinary `"cli"` tokens with full per-agent scope — exactly as today.

### Authorization predicate — `can_build` (decision 2)

```
can_build(user, agent) :=
    RoleService.is_developer(user)            # agent-developer OR admin OR superuser
    AND not _is_foreign_install(agent)        # bundle_uuid is None OR is_publisher_install
    AND user_can_access(user, agent)          # owner, or workspace-visible (see listing)
```

- **One shared helper.** Placed in `AgentService` (or a small
  `agents/agent_access.py`) as `AgentService.can_build(user, agent) -> bool`
  plus `AgentService.assert_can_build(user, agent)` raising a typed error.
  `_is_foreign_install` moves from a route-private function in `agents.py` into
  the service so both the route and the helper share it (keep a thin re-export in
  `agents.py` to avoid churn).
- **Call-sites routed through it (Phase 1):**
  1. New `POST /account/agents/{id}/mint` — gate.
  2. New `GET /account/agents` — sets the `can_build` flag per row.
  3. **Existing** `POST /api/v1/cli/setup-tokens` — today ownership-only; switch
     to `assert_can_build`. This is a deliberate correctness fix: a developer
     should not be able to set up local dev on a foreign install (its workspace
     is publisher-owned and read-only-ish), and an `agent-user` should not get a
     building-context CLI session at all.
  4. (Audit pass) any other place that currently checks "owner + developer to
     edit/build an agent" can adopt `can_build` opportunistically, but Phase 1
     only *requires* the three above plus the new endpoints.

> Foreign installs still **appear** in `cinna account agents` (flagged
> `can_build=false, is_foreign_install=true`) and can be wired as *producers* via
> credentials in later phases — they just can't be sync/exec/mint targets.

### Accessible-agents projection (no data leak)

`GET /account/agents` must return only agents the user can access and must not
leak fields the user shouldn't see. The projection is a **minimal**
`AccountAgentListItem` (see below) — id, name, description, color preset,
`owner_id`, workspace id, `bundle_uuid`, `is_publisher_install`, derived
`is_foreign_install`, derived `can_build`, and a `has_active_environment` flag.
**No credentials, no prompts, no env internals.** Access set = the same set the
existing `read_agents` returns for the user (own agents + workspace-visible +
GA), reusing `AgentService.list_agents`.

### Input validation / rate limiting

- Account setup-token creation requires `require_developer` (decision 6) — an
  `agent-user` cannot even generate the link.
- Mint endpoint is idempotent-friendly but not idempotent: each call mints a new
  child token (a new machine / re-sync). The CLI is expected to reuse an existing
  child token if one is already in `~/.cinna/agents.json`; the backend does not
  dedupe (mirrors per-agent setup, where re-running `cinna setup` mints a new
  token). Optionally cap children-per-account-token (config, default unlimited —
  decision 5 says no caps).
- The bootstrap exchange route is unauthenticated (the setup token *is* the
  credential), single-use, 15-min TTL — identical hardening to the per-agent
  bootstrap.

### Audit (SecurityEvent)

Two new event-type constants in
`backend/app/models/events/security_event.py`:

- `CLI_ACCOUNT_TOKEN_CREATED` — emitted on account setup-token **exchange**
  (account token minted). `severity="medium"`, `details={machine_name, ip}`,
  `agent_id=None`.
- `CLI_ACCOUNT_CHILD_TOKEN_MINTED` — emitted on every child mint.
  `severity="medium"`, `agent_id=<target>`,
  `details={account_token_id, child_token_id, prefix, ip}`.

Written via `SecurityEventService.create_event(session, user_id, SecurityEventCreate(...))`.
(Optionally a `CLI_ACCOUNT_TOKEN_REVOKED` event on revoke; nice-to-have.)

---

## Backend Implementation

### Service Layer

All new logic lands in a dedicated service to keep `CLIService` focused:

**`backend/app/services/cli/account_cli_service.py` — `AccountCLIService`**

- `create_account_setup_token(db, user, request) -> CLISetupTokenCreated`
  Mirrors `CLIService.create_setup_token` but `agent_id=None, kind="account"`.
  Builds `setup_command = curl -sL {platform_url}/api/cli-setup/account/{token} | python3 -`.
  Caller (route) is already `require_developer`-gated.
- `exchange_account_setup_token(db, token_str, machine_name, machine_info, request) -> dict`
  Validates the setup token (kind=="account", not used, not expired), creates a
  `CLIToken` with `agent_id=None, token_type="cli-account"`, mints the JWT via
  `CLIAuthService.create_cli_jwt(..., agent_id=None, token_type="cli-account")`,
  marks the setup token used, writes `CLI_ACCOUNT_TOKEN_CREATED` SecurityEvent,
  and returns `{token, platform_url, frontend_url, account_workspace_files…}`
  (the bootstrap payload — see CLI contract).
- `list_accessible_agents(db, user) -> list[AccountAgentListItem]`
  Reuses `AgentService.list_agents(user, ...)` for the access set, then projects
  each into the minimal item with `can_build` / `is_foreign_install` flags and
  `has_active_environment`.
- `mint_child_token(db, account_ctx, agent_id, machine_name, machine_info, request) -> CLITokenCreated`
  1. `agent = db.get(Agent, agent_id)` → 404 if missing or not in the user's
     access set (use the listing's access predicate — never 404-leak existence of
     inaccessible agents: return 404 for both "missing" and "not accessible").
  2. `AgentService.assert_can_build(user, agent)` → 403 otherwise.
  3. Build a **normal** per-agent `CLIToken` (token_type default `"cli"`,
     `agent_id=agent.id`, `owner_id=user.id`) exactly like
     `CLIService.exchange_setup_token` does, additionally stamping
     `minted_by_account_token_id=account_ctx.cli_token.id`.
  4. Write `CLI_ACCOUNT_CHILD_TOKEN_MINTED` SecurityEvent.
  5. Return `CLITokenCreated` (JWT shown once) **plus** the agent record fields
     the CLI needs to write the standard workspace (name, env id, template,
     knowledge sources, frontend_url) — same shape the per-agent exchange
     returns, so the CLI's existing workspace-bootstrap code path is reused.
- `revoke_account_token(db, token_id, user) -> int`
  Soft-revokes the account token (`is_revoked=True`) **and** every child:
  `UPDATE cli_token SET is_revoked=true WHERE minted_by_account_token_id=:id OR id=:id`.
  Returns the count revoked. Ownership-checked. This is the cascade (decision 1).

**Reused as-is:** `CLIAuthService` (extend `create_cli_jwt` signature),
`CLIService` per-agent endpoints, `SyncActivityTracker`, the setup-token cleanup
scheduler (it already deletes used/expired `cli_setup_token` rows regardless of
`kind`).

### Dependencies — `AccountCLIContext`

`backend/app/api/deps.py`:

```
class AccountCLIContext(SQLModel):     # mirrors CLIContext but agent-free
    user: User
    cli_token: CLIToken                # token_type == "cli-account"

def get_account_cli_context(token, db) -> AccountCLIContext:
    # decode CLI JWT; REQUIRE token_type == "cli-account" (reject "cli")
    # lookup CLIToken, check revoked/expired, load active user
    # roll 7-day expiry via refresh_token_usage(db, token, environment=None)
    # raise 401/404 appropriately
AccountCLIContextDep = Annotated[AccountCLIContext, Depends(get_account_cli_context)]
```

Also harden the existing `_resolve_cli_context`: it must **reject**
`token_type == "cli-account"` (raise `CLIAuthError("invalid_token", …)`) so an
account token can never satisfy a per-agent route. (Today it doesn't check the
type beyond `decode_cli_jwt`; add the explicit guard.) `decode_cli_jwt` should
accept both types and let the caller decide.

### API Routes

All under the existing `router` (`/api/v1/cli`) and `setup_router`
(`/api/cli-setup`) in `backend/app/api/routes/cli.py` (thin controllers,
delegate to `AccountCLIService`):

**Account setup / management (user JWT, `require_developer`)**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/cli/account/setup-tokens` | `CurrentUser` + `require_developer` | Create account setup token; returns `CLISetupTokenCreated` with the account `setup_command`. |
| GET | `/api/v1/cli/account/tokens` | `CurrentUser` | List the user's account tokens (`token_type="cli-account"`) with child counts. |
| DELETE | `/api/v1/cli/account/tokens/{token_id}` | `CurrentUser` | Revoke account token **and cascade-revoke its children**. Returns count. |

**Account bootstrap (no auth — setup token is the credential)**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/cli-setup/account/{token}` | Serve the account bootstrap Python script (checks for `cinna`, delegates to `cinna account setup <url>` or prints install hints). Reuses `CLIService.render_bootstrap_script` with an `account` flavor. |
| POST | `/api/cli-setup/account/{token}` | Exchange account setup token → account CLI token + account bootstrap payload. |

**Account-scoped (account CLI token via `AccountCLIContextDep`)**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/cli/account/agents` | `AccountCLIContextDep` | List accessible agents with `can_build` / `is_foreign_install` / `has_active_environment` flags. |
| POST | `/api/v1/cli/account/agents/{agent_id}/mint` | `AccountCLIContextDep` | `can_build`-gated. Mint a normal per-agent CLI token (provenance-stamped); return `CLITokenCreated` + workspace-bootstrap fields. 403 if not buildable, 404 if inaccessible. |

> `cinna exec --agent <agent>` does **not** need a new endpoint: the CLI uses the
> already-minted child token against the existing
> `POST /api/v1/cli/agents/{id}/exec`. If the agent isn't yet synced, the CLI
> first calls `mint`, persists the child token, then execs. (Optionally a future
> `POST /account/agents/{id}/exec` convenience proxy — out of scope for Phase 1.)

### Models (Pydantic schemas)

`backend/app/models/cli/cli_token.py`:
- `CLIAccountTokenPublic` — account-token row + `child_count: int`.
- `CLIAccountTokensPublic` — `{data, count}`.

`backend/app/models/cli/account_agent.py` (new):
- `AccountAgentListItem` — `{id, name, description, ui_color_preset, owner_id,
  user_workspace_id, bundle_uuid, is_publisher_install, is_foreign_install,
  can_build, has_active_environment}`.
- `AccountAgentsPublic` — `{data, count}`.

The mint response reuses `CLITokenCreated` extended with the workspace-bootstrap
fields (or a new `AccountMintResult(CLITokenCreated)` wrapper) so the CLI's
existing per-agent bootstrap writer is reused verbatim.

---

## Frontend Implementation

### UI Component — `LocalDevelopmentCard` (account-level)

`frontend/src/components/UserSettings/LocalDevelopmentCard.tsx` — a Settings
card modeled on the per-agent `LocalDevCard.tsx`, but account-scoped:

- **Generate / Copy** — "Set up Local Development" button calls
  `AccountCliService.createAccountSetupToken`; renders the
  `curl … | python3 -` one-liner in a readonly input with Regenerate / Copy-token
  / Copy-command icon buttons and the 15-min expiry countdown (reuse the
  per-agent card's countdown logic).
- **Active account sessions** — `useQuery(["account-cli-tokens"])` →
  `AccountCliService.listAccountTokens`; one row per account token with
  machine name, prefix, created/last-used, and a **child count** ("3 agents
  synced"). Icon-only **Disconnect** (destructive AlertDialog, Enter-to-confirm,
  same pattern as `LocalDevCard`) → `revokeAccountToken` → invalidates the query.
  Copy says "Revoking disconnects all agents synced from this machine."
- **Empty state** — short explainer + the General-Assistant-style pitch: "Drive
  your whole agent network from your local coding assistant."

**Gating:** the card only renders for `agent-developer`/`admin` (decision 6).
Reuse the existing role check used to gate developer-only Settings UI.

### Placement

Add `<LocalDevelopmentCard />` to the **Channels** tab grid in
`frontend/src/routes/_layout/settings.tsx` (next to `AppAgentRoutesCard` /
`IdentityServerCard`), OR a new **"Local Development"** tab if the product wants
top-level prominence. Recommended: Channels tab to start (lower surface area).

### State Management (React Query)

- `["account-cli-setup-token"]` — ephemeral, the generated setup token.
- `["account-cli-tokens"]` — the active account sessions list; invalidated on
  create/revoke.
- No localStorage usage (the account token lives on the user's machine, never in
  the browser).

### User flows / states

- **Generate → copy → run locally** — same muscle memory as per-agent setup.
- **Revoke** — confirm dialog warns it disconnects all synced agents; on success
  next CLI call on any child gets 401 (Mutagen pauses) — same UX as per-agent
  disconnect, just fan-out.
- **Loading / error** — standard query skeleton + toast on mutation error.

---

## Database Migrations

Single Alembic revision, e.g. `add_account_cli_tokens.py`.

> **Single-head discipline.** The repo has had multi-head Alembic situations.
> Set this revision's `down_revision` to the *current single head* at
> implementation time and confirm `alembic heads` shows exactly one head before
> and after. If multiple heads exist when you start, create a merge revision
> first — do **not** branch.

Operations:

1. `cli_token.token_type` — add `VARCHAR`, `server_default='cli'`,
   `NOT NULL`; backfill is automatic via server_default; then optionally drop the
   server_default in a follow-up (or leave it — harmless). Add
   `ix_cli_token_token_type`.
2. `cli_token.minted_by_account_token_id` — add nullable `UUID`, self-FK
   `cli_token.id` `ON DELETE CASCADE`, add `ix_cli_token_minted_by`.
3. `cli_token.agent_id` — `ALTER COLUMN … DROP NOT NULL` (make nullable; keep the
   existing FK + CASCADE). All existing rows already have a value, so no backfill.
4. `cli_setup_token.agent_id` — `DROP NOT NULL`.
5. `cli_setup_token.kind` — add `VARCHAR`, `server_default='agent'`, `NOT NULL`.

**Downgrade:** drop the two new indexes, drop `minted_by_account_token_id`, drop
`token_type` and `kind`, and re-impose `NOT NULL` on both `agent_id` columns
(safe only after deleting any account rows — note this in the downgrade docstring;
downgrade should `DELETE FROM cli_token WHERE token_type='cli-account'` and
`DELETE FROM cli_setup_token WHERE kind='account'` before re-adding NOT NULL).

No new tables.

---

## Error Handling & Edge Cases

- **Account token used on a per-agent route** → `_resolve_cli_context` rejects
  `token_type="cli-account"` → 401. (Structural guarantee for decision 6.)
- **Per-agent token used on an account route** → `get_account_cli_context`
  rejects `token_type="cli"` → 401.
- **Mint on a foreign install** → 403 with a clear message ("This is an installed
  bundle; its workspace is publisher-managed and can't be synced for local
  development.").
- **Mint on an inaccessible agent** → 404 (do not distinguish from "missing" — no
  existence leak; mirrors the credential-deletion 403/404 existence-leak rule
  already established in this codebase).
- **Mint when user role demoted to `agent-user` after token creation** →
  `can_build` re-checked on every mint → 403. (The account token still exists but
  can't mint anything buildable.)
- **Account token revoked while children active** → cascade sets all children
  `is_revoked=true`; next sync/exec 401s and Mutagen pauses. Local files intact.
- **Child token independently revoked** (UI Disconnect on the agent's
  Integrations tab, or `cinna disconnect`) → only that child dies; account token
  and siblings unaffected. The agent's session list shows the child as a normal
  CLI session, so the existing Disconnect button already works on it.
- **Agent deleted** → existing `agent_id` CASCADE removes the child token row;
  account token unaffected.
- **Setup token expired/used** → 400 (reuse existing exchange validation).
- **Concurrent mints for the same agent** → both succeed (two child tokens); the
  CLI keeps the latest in `~/.cinna/agents.json`. No locking needed (decision 5,
  no caps).

---

## UI/UX Considerations

- Reuse the per-agent card's copy-to-clipboard + green-check confirmation +
  expiry countdown patterns verbatim.
- Child-count badge per account session ("3 agents synced") gives the cascade
  blast radius at a glance before revoke.
- Destructive-revoke dialog must spell out the fan-out ("disconnects all agents
  synced from this machine").
- Developer-only visibility: don't render the card for `agent-user`.

---

## Integration Points

- **cinna_cli_integration** — sibling route group + dep; all per-agent runtime
  reused. Update its docs after implementation.
- **agent_bundles** — `_is_foreign_install` consumed by `can_build`.
- **user_roles** — `RoleService.is_developer` consumed by `can_build`.
- **events** — two new SecurityEvent types.
- **API client regeneration** — after backend routes land, run
  `bash scripts/generate-client.sh` (or `source ./backend/.venv/bin/activate &&
  make gen-client`) to regenerate `frontend/src/client/` so the new
  `AccountCliService` methods and `AccountAgent*` / `CLIAccountToken*` types
  exist before wiring the Settings card.

---

## cinna-cli companion work

> The CLI lives in a **separate repo** at `/Users/evgenyl/dev/ml-llm/cinna-cli`.
> This section is the complete contract a separate session there implements
> against — endpoints, payload shapes, auth headers, and the on-disk workspace
> layout. Nothing below should need re-derivation.

### Account workspace layout

```
my-cinna/                       # the account workspace root
  .cinna/
    account.json                # NEW account config (see schema)
  CLAUDE.md                     # orchestrator prompt (Phase 1: minimal; Phase 2: rich)
  context/                      # (Phase 2) glossary, API reference, guides
  agents/
    crm-agent/                  # a 100% STANDARD cinna per-agent workspace
      .cinna/config.json        #   (identical to `cinna setup` output)
      workspace/                #   synced via Mutagen as today
      CLAUDE.md  BUILDING_AGENT.md  mutagen.yml  .mcp.json  opencode.json
```

The global `~/.cinna/agents.json` registry is shared with the per-agent flow —
each synced child agent gets a normal entry (the SSH shim and `cinna list` keep
working unchanged). Child workspaces are first-class (decision 4): `cd
agents/crm-agent && cinna dev` is byte-identical to a hand-set-up agent.

### `.cinna/account.json` schema

```json
{
  "platform_url": "https://platform.example.com",
  "frontend_url": "https://platform.example.com",
  "account_token": "<account CLI JWT>",
  "machine_name": "My MacBook"
}
```

Written `0o600`. The account token is **only** used for the `account/*`
endpoints; per-agent tokens (in each child's `.cinna/config.json` and in
`~/.cinna/agents.json`) drive sync/exec.

### New CLI commands

| Command | Calls | Behavior |
|---------|-------|----------|
| `cinna account setup <token_or_url>` | `POST /api/cli-setup/account/{token}` | Exchange account setup token → account CLI token; create `my-cinna/` with `.cinna/account.json` + orchestrator `CLAUDE.md`; print next steps. The bootstrap `curl` one-liner delegates here. |
| `cinna account agents` | `GET /api/v1/cli/account/agents` | Print a table of accessible agents: name, id, `can_build` (✓ / "view-only"), `is_foreign_install` flag, env-active dot. |
| `cinna agent sync <agent>` | `POST /api/v1/cli/account/agents/{id}/mint` then the **existing** per-agent bootstrap | Resolve `<agent>` (name or id) from `account agents`; mint a child token; write `agents/<slug>/` as a standard workspace (reuse the `cinna setup` bootstrap writer — workspace tarball clone via `GET /api/v1/cli/agents/{id}/workspace`, building-context, generated files, `~/.cinna/agents.json` upsert). Then `cinna dev` works inside it. |
| `cinna agent unsync <agent>` | (local) + child-token revoke | Stop sync, remove `agents/<slug>/`, drop the registry entry. Equivalent to `cinna disconnect` run inside the child plus deleting the dir. Optionally call the per-agent `DELETE /tokens/{id}` to revoke the child server-side (or just let it expire). |
| `cinna exec --agent <agent> <cmd>` | existing `POST /api/v1/cli/agents/{id}/exec` | If `<agent>` not yet synced, run `sync` first (mint + minimal bootstrap), then exec with the child token. Streams SSE exactly as `cinna exec` does today. |

`<agent>` accepts the display name (slugified) or the agent UUID; resolution uses
the cached `account agents` listing.

### Auth headers

- `account/*` authenticated routes: `Authorization: Bearer <account_token>`.
- Per-agent routes (sync/exec/workspace): `Authorization: Bearer <child_token>`
  (unchanged from today; sync WS also accepts `?token=`).

### Mint response shape (`POST /account/agents/{id}/mint`)

```json
{
  "token": "<child CLI JWT — shown once>",
  "id": "<child token id>",
  "agent_id": "<agent id>",
  "owner_id": "<user id>",
  "prefix": "...",
  "expires_at": "...",
  "agent_name": "CRM Agent",
  "environment_id": "<env id or null>",
  "template": "...",
  "frontend_url": "https://...",
  "knowledge_sources": [ ... ]
}
```

(The non-token fields mirror the per-agent `exchange_setup_token` payload so the
existing workspace writer is reused.)

### `GET /account/agents` response shape

```json
{
  "count": 4,
  "data": [
    {
      "id": "…", "name": "CRM Agent", "description": "…",
      "ui_color_preset": "violet",
      "owner_id": "…", "user_workspace_id": "…",
      "bundle_uuid": null, "is_publisher_install": false,
      "is_foreign_install": false, "can_build": true,
      "has_active_environment": true
    }
  ]
}
```

### CLI files to add/modify (orientation only — confirm against repo)

- `src/cinna/main.py` — new `account` command group (`setup`, `agents`) and
  `agent` group (`sync`, `unsync`); `--agent` option on `exec`.
- `src/cinna/account.py` (new) — `AccountConfig` dataclass + `.cinna/account.json`
  read/write; account-context resolution (walk up to find `account.json`).
- `src/cinna/bootstrap.py` — extract the per-agent workspace-writer so
  `agent sync` reuses it given a minted token (rather than a setup-token
  exchange).
- `src/cinna/client.py` — add `account_setup`, `list_account_agents`,
  `mint_agent_token` client methods.

---

## Phase Breakdown

Each phase is independently reviewable; Phase 1 is the detailed scope above.

- **Phase 1 — Backend auth spine + CLI contract (this plan, detailed).**
  Account token type + account setup-token flow + Settings card + mint endpoint +
  cascade revocation + `can_build` unification + accessible-agents listing; CLI
  contract for `account setup`, `account agents`, `agent sync|unsync`,
  `exec --agent`.
- **Phase 2 — Account context package.** Factor the General Assistant's
  platform-docs snapshot (`knowledge/platform/`) + generated API reference
  (`api_reference/` from `frontend/openapi.json`) into a shared backend
  service/endpoint (e.g. `GET /api/v1/cli/account/context-package`) consumed at
  `cinna account setup`; ship a rich orchestrator `CLAUDE.md` and the `context/`
  tree. Reuses `.cinna-core-kit/scripts/sync_ga_knowledge.py` output.
- **Phase 3 — Convenience verbs.** `cinna agent create` (thin client; backend
  applies all defaults via the normal agent-create path — decision 3),
  `cinna connect agent-api`, `cinna connect mcp`, and a generic
  `cinna api <METHOD> <path>` escape hatch (account-token-scoped, capability
  exclusions enforced server-side).
- **Phase 4 — Agentic networks.** Agentic-team registration from the CLI + a
  worked "build an agentic network" playbook guide in `context/`.

---

## Testing Approach (API-only; see `backend/tests/README.md`)

Read `backend/tests/README.md` and `backend/tests/api/cli/README.md` (if present)
first. Scenario-based, API-only, no direct DB access; reuse
`backend/tests/utils/cli.py` and extend with account helpers. New file
`backend/tests/api/cli/test_account_cli.py`:

- **Setup-token flow** — developer creates account setup token (200); `agent-user`
  is 403; exchange yields an account CLI token; re-exchange is 400 (single-use);
  expired token is 400.
- **`can_build` gate** — mint succeeds for a developer-owned standalone agent;
  403 on a foreign install; 403 after demoting the user to `agent-user`; 404 on
  an inaccessible agent (no existence leak).
- **Mint provenance** — minted child token authenticates the existing per-agent
  endpoints (exec/workspace) and is scoped to exactly the target agent; the
  account token is rejected by those same endpoints (401).
- **Cascade revoke** — revoking the account token sets all children
  `is_revoked`; subsequent per-agent calls 401; sibling/unrelated tokens
  untouched; independently revoking one child leaves the account token and
  siblings alive.
- **Listing projection** — `account agents` returns the user's access set with
  correct `can_build` / `is_foreign_install` flags and leaks no
  credentials/prompts; excludes other users' agents.
- **SecurityEvent audit** — `CLI_ACCOUNT_TOKEN_CREATED` on exchange and
  `CLI_ACCOUNT_CHILD_TOKEN_MINTED` on each mint are written with the right
  `agent_id`/details.
- **Regression** — run the existing `backend/tests/api/cli/` suite to confirm the
  nullable-`agent_id` and `_resolve_cli_context` type-guard changes don't break
  the per-agent path; confirm the per-agent setup-token route's switch to
  `assert_can_build` still passes for developer-owned agents and now 403s on
  foreign installs (update/extend the relevant per-agent test).

CLI-side tests live in the cinna-cli repo against the documented contract.

---

## Resolved Decisions (from product owner — final, not open questions)

1. Child-token minting model; account token never syncs directly; cascade revoke
   via `minted_by_account_token_id`; SecurityEvent on create + every mint; 7-day
   rolling expiry.
2. Authorization predicate = building rights (`can_build`), not bare ownership;
   ONE shared helper routes the mint endpoint, the listing flag, **and** the
   existing per-agent CLI setup-token route (fixing its ownership-only check).
   Foreign installs are listed (flagged) but not sync/exec/mint targets.
3. Thin client — CLI sends only user-specified fields; backend applies all
   defaults and returns the full record.
4. Child workspaces are first-class standard workspaces (own token, registry
   entry, generated files); only provenance differs.
5. No sync resource caps; account-initiated syncs follow the existing `cinna dev`
   lifecycle.
6. Account token capability exclusions (no credential reads, no user mgmt, no
   admin); link generation requires `agent-developer`; revocation takes effect on
   next API call.
7. Phased delivery as above.

### Genuinely open (flagged, low-risk)

- **Settings placement** — Channels tab vs. a dedicated top-level "Local
  Development" tab. Recommendation: Channels tab for Phase 1.
- **`unsync` server-side revoke** — whether `cinna agent unsync` actively revokes
  the child token server-side or lets it expire. Recommendation: actively revoke
  (call `DELETE /api/v1/cli/tokens/{id}`) for a clean session list.

---

## Summary Checklist

**Backend**
- [ ] Migration `add_account_cli_tokens` (single-head): `cli_token.token_type`,
      `cli_token.minted_by_account_token_id` (+ indexes), `cli_token.agent_id`
      nullable; `cli_setup_token.agent_id` nullable + `kind`.
- [ ] Extend `CLIAuthService.create_cli_jwt` with `token_type` + nullable
      `agent_id`; teach `decode_cli_jwt` to accept `"cli-account"`.
- [ ] Add `AccountCLIContext` + `get_account_cli_context` dep; harden
      `_resolve_cli_context` to reject `token_type="cli-account"`.
- [ ] `AgentService.can_build` / `assert_can_build` + move `_is_foreign_install`
      into the service (re-export in `agents.py`).
- [ ] Switch existing `POST /api/v1/cli/setup-tokens` to `assert_can_build`.
- [ ] `AccountCLIService` (setup-token create/exchange, accessible-agents
      listing, mint, cascade revoke).
- [ ] Account routes (setup-tokens, tokens list/revoke, bootstrap GET/POST,
      `account/agents`, `account/agents/{id}/mint`).
- [ ] Two new `SecurityEvent` constants + emission on create and mint.
- [ ] Pydantic schemas: `CLIAccountTokenPublic/​sPublic`, `AccountAgentListItem`,
      `AccountAgentsPublic`, mint result.

**Frontend**
- [ ] `LocalDevelopmentCard.tsx` (developer-gated) in Settings → Channels.
- [ ] React Query keys `["account-cli-setup-token"]`, `["account-cli-tokens"]`.
- [ ] Regenerate client (`bash scripts/generate-client.sh`).

**cinna-cli (separate repo)**
- [ ] `account` group (`setup`, `agents`), `agent` group (`sync`, `unsync`),
      `exec --agent`; `.cinna/account.json` + `AccountConfig`; reuse per-agent
      workspace writer for child syncs.

**Testing & validation**
- [ ] `test_account_cli.py` scenarios (setup-token, can_build gate, provenance,
      cascade revoke, listing projection, audit).
- [ ] Regression on existing `backend/tests/api/cli/` (nullable agent_id +
      type-guard + per-agent setup-token `can_build` switch).
- [ ] Manual: bootstrap account workspace, `account agents`, `agent sync`,
      `cd agents/x && cinna dev`, verify child appears in the agent's Integrations
      session list, revoke account token → all children 401.
```
