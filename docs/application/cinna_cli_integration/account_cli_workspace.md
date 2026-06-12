# Account CLI Workspace

## Purpose

Extends the per-agent CLI with an **account-level** bootstrap flow. A single
setup command produces an account workspace from which a local coding agent can:

- discover all agents the user has building rights on,
- mint normal per-agent CLI tokens on demand (no further UI interaction),
- exec into any buildable agent using the existing per-agent exec path.

The account workspace is a multi-agent development environment. Each synced
agent under `agents/` is a 100% standard `cinna dev` workspace — only the
token's provenance differs from one set up manually.

## Core Concepts

| Concept | Description |
|---------|-------------|
| **Account Setup Token** | Short-lived (15 min), single-use token with `kind="account"`. Created from Settings → Channels → Local Development. The `curl | python3` one-liner exchanges it for an account CLI token |
| **Account CLI Token** | Long-lived JWT (`token_type="cli-account"`, `agent_id=NULL`). Stored in `.cinna/account.json` on the user's machine. Scoped **only** to the `/account/*` route group — rejected by all per-agent routes |
| **Child Token (minted)** | A standard per-agent `token_type="cli"` token minted by the account token via `POST /account/agents/{id}/mint`. Carries `minted_by_account_token_id` as provenance. Authenticates the existing per-agent sync / exec / workspace endpoints unchanged |
| **Building-rights predicate (`can_build`)** | `developer-or-admin role AND not a foreign install AND user owns the agent`. The single gate for setup-token creation (both per-agent and account), the mint endpoint, and the `can_build` flag in the agents listing |
| **Accessible-agents listing** | `GET /account/agents` — returns the user's own agents with `can_build`, `is_foreign_install`, and `has_active_environment` flags. No credentials, prompts, or env internals |
| **Cascade revocation** | Revoking an account token soft-revokes every child token it minted. Independent per-agent tokens from other sources are unaffected |

## User Stories / Flows

### 1. Bootstrapping the Account Workspace

1. User navigates to **Settings → Channels** tab.
2. The **Local Development** card is visible only to `agent-developer` / `admin`
   users (agent-users do not see it at all).
3. User clicks **Set up Local Development** — the platform generates a
   15-minute single-use account setup token and displays a `curl | python3`
   one-liner.
4. User copies the command and runs it in their terminal.
5. The bootstrap script (`GET /api/cli-setup/account/{token}`) checks if
   `cinna` is installed:
   - If installed: delegates to `cinna account setup <url>`, which exchanges
     the token for an account CLI token, downloads the context package
     (see Flow 4 below), writes `my-cinna/.cinna/account.json`, and creates
     a `CLAUDE.md` whose first instruction is to read `context/README.md`.
   - If not installed: prints install instructions and exits.
6. The exchange (`POST /api/cli-setup/account/{token}`) returns:
   `account_token`, `platform_url`, `frontend_url`, `machine_name`.
7. The platform writes a `CLI_ACCOUNT_TOKEN_CREATED` security event.

### 2. Discovering and Syncing Agents

Once the account workspace is bootstrapped, the local coding agent (or the
developer directly) uses the account token to:

1. **List accessible agents** (`cinna account agents` → `GET /account/agents`):
   prints name, ID, `can_build` flag, `is_foreign_install` flag, and an
   env-active indicator.
2. **Mint a per-agent token** (`cinna agent sync <agent>` →
   `POST /account/agents/{id}/mint`):
   - `can_build` is checked — if the target is a foreign install, returns 403;
     if inaccessible or missing, returns 404.
   - A normal `token_type="cli"` `CLIToken` is created with
     `minted_by_account_token_id` stamped.
   - The response mirrors the per-agent setup-token exchange payload
     (`token`, `agent_id`, `agent_name`, `environment_id`, `template`,
     `frontend_url`, `knowledge_sources`), so the CLI's existing
     workspace-bootstrap writer is reused verbatim.
   - A `CLI_ACCOUNT_CHILD_TOKEN_MINTED` security event is written.
3. **Use standard per-agent commands** inside `agents/<name>/`:
   - `cinna dev` — live Mutagen sync (uses the child token).
   - `cinna exec <cmd>` — streams command output from the remote env.
   - `cinna disconnect` — revokes the child token and tears down the local
     workspace.

The account workspace layout:

```
my-cinna/
  .cinna/
    account.json         # account token + platform/frontend URLs + machine name
  CLAUDE.md              # orchestrator prompt — first instruction: read context/README.md
  context/               # platform knowledge package (downloaded by cinna account setup)
    README.md            # package index the CLAUDE.md points at
    platform/
      README.md          # feature map (entrypoint for exploring capabilities)
      application/       # business-logic docs for user-facing features
      agents/            # business-logic docs for agent-side features
    api_reference/
      README.md          # index of all API domain files
      *.md               # generated REST API reference, one file per domain
    examples/            # working API-script patterns (platform_helper + samples)
    guides/              # end-to-end worked walkthroughs (Phase 4)
      build-an-agentic-network.md   # how to build a delegating multi-agent network
  agents/
    crm-agent/           # 100% standard cinna per-agent workspace
      .cinna/config.json
      workspace/
      CLAUDE.md  BUILDING_AGENT.md  mutagen.yml  .mcp.json  opencode.json
```

### 4. Downloading the Platform Context Package

The context package is static, pre-built platform knowledge: no user data,
no secrets. It gives the orchestrator agent in the account workspace the
same self-knowledge that the General Assistant has inside its container.

1. `cinna account setup` calls
   `GET /api/v1/cli/account/context-package` (authenticated by the account
   CLI token) and receives a `application/tar+gzip` tarball.
2. The CLI extracts it safely (rejects `..` and absolute-path members) into
   the account workspace's `context/` tree.
3. The orchestrator `CLAUDE.md` points at `context/README.md` as its first
   read. From there the agent navigates to `context/platform/` for feature
   docs, `context/api_reference/` for endpoint signatures, and
   `context/guides/` for end-to-end worked walkthroughs (Phase 4).
4. `cinna account refresh-context` re-downloads and replaces the `context/`
   tree in place. If the download fails, the command warns and exits without
   corrupting the existing `context/` content.

The package is assembled from the committed `general-assistant-env`
template snapshot inside the backend container (the only copy of this
knowledge available at runtime — `docs/` is not in the image). It is
built once per deployment and then memoized in-process (keyed by snapshot
mtime + file count across all three source dirs), so repeated downloads are
cheap. If the snapshot is missing or empty, the endpoint returns **503**
rather than serving a near-empty package — the caller can detect and report
the deploy defect. Missing `examples/` or `guides/` is tolerated: a
warning is logged and the corresponding `context/` subtree is simply
omitted from the package.

### 3. Choosing an Active Workspace

The account workspace can be bound to one of the user's **[user workspaces](../user_workspaces/user_workspaces.md)**
so that everything workspace-scoped it creates (agents, and the connection
credentials those agents acquire) lands in that workspace by default — the CLI
equivalent of picking a workspace in the sidebar switcher before creating things.

1. `cinna account user-workspace list` → `GET /account/user-workspaces` prints the
   user's workspaces (id + name), marking the active one. "Default" (no workspace)
   is always available implicitly.
2. `cinna account user-workspace --activate=<id>` (or `--activate=default` to
   clear) validates the id against that list and stores it in
   `.cinna/account.json`. **This is a client-side CLI setting** — the backend
   keeps no "active workspace" state; the chosen id is simply attached to each
   create call.
3. From then on, `cinna agent create` sends the stored `user_workspace_id`, so the
   new agent (and the `agent_api` / `mcp_provider` credentials a later `cinna
   connect` mints on it, which inherit the agent's workspace) belong to that
   workspace.

The listing endpoint is account-token-reachable and read-only; it never exposes
another user's workspaces (owner-scoped projection).

### 4. Creating an Agent from the Account Workspace

Once the account workspace is active, an orchestrator agent (or developer) can
create a new platform agent without opening the UI:

1. `cinna agent create <name> [--description D]` → `POST /account/agents`
2. The backend applies all platform defaults via the same path that `POST
   /api/v1/agents/` uses: default AI-credential resolution, default environment
   template, and environment creation.
3. The agent is assigned to the account workspace's **active user workspace** (the
   `user_workspace_id` from `.cinna/account.json`, see flow 3 above); omitted or
   `default` → the Default (unassigned) workspace. A `user_workspace_id` that the
   caller does not own returns **404** (no existence leak) and creates nothing.
4. The full `AgentPublic` record is returned (id, env id, workspace id, etc.).
5. `env_name` (template selection) is accepted but has no effect in v1 — the
   server always picks `DEFAULT_AGENT_ENV_NAME`. Template selection at create time
   is a documented follow-up item.
6. Requires `agent-developer` role. An `agent-user` receives **403**.

After creation, the developer runs `cinna agent sync <name>` to mint a child
token and set up the agent's local workspace.

### 4. Connecting Agents (Agent API and MCP) from the CLI

The account workspace exposes the same "one-click connect" helpers available in
the UI, so the orchestrator agent can wire two agents together without navigating
the Settings pages.

**Connect Agent REST API (`cinna connect agent-api`):**

1. `cinna connect agent-api --producer P --consumer C [--label L] [--read-only]`
   → resolves agent names to IDs from the cached `account agents` list, then
   `POST /account/connect/agent-api`
2. The backend delegates to the same `AgentApiTokenService.connect_agent_api` that
   the UI uses. Enforces:
   - Caller must **own the producer agent** (403 otherwise).
   - If a consumer is supplied, caller must **own the consumer agent** (403 otherwise).
   - Producer's Agent REST API must be enabled (400 if disabled).
3. On success: a `CredentialType.AGENT_API` credential is created on the consumer
   and the `ConnectAgentApiResponse` is returned (credential_id, token_prefix,
   base_url, spec_url, linked_consumer_agent_id).
4. `CLI_ACCOUNT_CONNECT_AGENT_API` security event is written.
5. Requires `agent-developer` role.

**Connect MCP Provider (`cinna connect mcp`):**

1. `cinna connect mcp --producer P --consumer C [--label L] [--conversation-only|--building-only]`
   → `GET /account/connect/mcp/discoverable?consumer_agent_id=C` to resolve
   producer agent name → connector_id, then `POST /account/connect/mcp`
2. The backend delegates to `MCPProviderService.connect_to_agent`. Enforces:
   - Caller must be in the producer connector's ACL (owner / `allowed_user_ids` /
     superuser) — 403 otherwise; missing or non-agent2agent connector → 404
     (no-leak).
   - If a consumer is supplied, caller must **own the consumer agent** (403).
3. On success: a `CredentialType.MCP_PROVIDER` credential is created on the
   consumer. The credential is injected into the SDK runtime config (not
   `credentials.json`) per the agent-to-agent MCP connector feature.
4. `CLI_ACCOUNT_CONNECT_MCP` security event is written.
5. Requires `agent-developer` role.

**Producer ownership vs. `can_build`:** both connect verbs gate on *ownership* of
the producer (consistent with the UI), not on the stricter `can_build` predicate
(which additionally bars foreign installs). A foreign install the user owns can
still be the target of a connect operation from the CLI.

### 5. Drafting and Attaching Credentials (`cinna account credentials`)

The motivating problem: an agent often needs a credential (a Stripe key, an Odoo
login, an IMAP mailbox) to function, but the **user** must decide what to create,
fill in the secret, and attach it to the right agent. The account CLI flips this
around — the orchestrator agent **scaffolds the credential as a draft and wires it
to the agent**, then tells the user exactly what to fill in. The user never has to
reason about what to create or share.

**Security invariant (the reason these are dedicated verbs, not the escape hatch):**
the account token can **never read or write a credential's secret value**. These
verbs touch only *metadata and structure*:

- `cinna account credentials types` → `GET /account/credentials/types` lists every
  credential type and the fields the user will need to fill (so the agent can pick
  a type and explain the setup).
- `cinna account credentials create --name N --type T [--workspace ...]` →
  `POST /account/credentials` creates an **empty draft** (no secret value). The
  response includes the created credential (with `status="incomplete"`), the
  `required_fields` the user must fill, and a `setup_url` deep-link to the
  Credentials page. The draft lands in the account's active workspace (flow 3).
- `cinna account credentials list` → `GET /account/credentials` lists the user's
  credentials with their `status` (`complete` / `incomplete`) — **metadata only,
  no values** — so the agent can see which drafts are still waiting on the user.
- `cinna account credentials update <id> [--name ...] [--notes ...]` →
  `PUT /account/credentials/{id}` edits **metadata only**; there is no field for
  the secret value, so the CLI structurally cannot set one.
- `cinna account credentials share-with-agent <id> --agent A` →
  `POST /account/credentials/{id}/share-with-agent` attaches the credential to an
  agent the user owns (`AgentCredentialLink`). Once the user fills the secret, its
  whitelisted fields sync to that agent's environment automatically.
- `cinna account credentials delete <id> [--force]` →
  `DELETE /account/credentials/{id}` removes it, reusing the platform's
  blast-radius gate (Tier 2 publisher-provided-in-published-bundle deletions
  return **409** with the impact unless `--force`).

The canonical scenario:

```
# 1. Agent realizes the new "billing-agent" needs a Stripe API token
cinna account credentials create --name "Stripe API Key" --type api_token
#   → status=incomplete, required_fields=["api_token"], setup_url=.../credentials

# 2. Agent attaches the draft to the agent that will use it
cinna account credentials share-with-agent <cred_id> --agent billing-agent

# 3. Agent tells the user: "I created a 'Stripe API Key' credential and attached it
#    to billing-agent. Open <setup_url>, fill in the api_token, and you're done."
```

All five write verbs require `agent-developer` role; listing/types are open to any
account-token holder. Each write emits a `CLI_ACCOUNT_CREDENTIAL_*` security event.

**Why not the escape hatch?** The `cinna api` escape hatch **denies** the entire
`credentials` prefix (a path-prefix denylist cannot tell "create an empty draft"
apart from "read the decrypted value"). These dedicated verbs are the only way to
expose the *safe* slice of credential management — draft + attach — while keeping
secret reads/writes structurally impossible for the account token.

### 6. Generic API Escape Hatch (`cinna api`)

For anything not yet wrapped in a dedicated verb, the account workspace provides a
generic authenticated escape hatch into (most of) the platform API:

1. `cinna api <METHOD> <path> [--json '<obj>'|--data @file.json] [--query k=v ...]`
   → `POST /account/api-proxy`
2. The CLI sends `{method, path, query, json_body}`. `path` is relative to the API
   root — no `/api/v1` prefix (the backend prepends it).
3. The backend's **single exclusion chokepoint** (`assert_api_proxy_allowed`) runs
   before any dispatch. Excluded surfaces return **403** with an explicit detail (not
   404 — these are well-known platform capability categories, not user resources).
4. Allowed calls are re-dispatched in-process under a request-scoped, short-lived
   (8 s) normal user JWT that never leaves the backend. Downstream routes see an
   ordinary authenticated user — zero per-route changes, all per-route
   authorization still applies.
5. The inner response status code and body are returned verbatim. JSON responses are
   pretty-printed by the CLI. Exit code 0 for inner `2xx`, non-zero for `4xx/5xx`.
6. `context/api_reference/` (from the Phase 2 context package) serves as the
   catalog of callable endpoints; `cinna api --help` points the agent there.

**Excluded surfaces (403 `excluded_path`):**

| Category | Prefixes excluded |
|----------|------------------|
| Credential values | `credentials`, `ai-credentials`, `oauth-credentials`, `credential-shares` |
| User management | `users` (except `GET /users/me` and `GET /users/search`) |
| Admin | `admin`, `admin-environments`, `private` |
| CLI recursion / self-management | `cli` (the entire CLI router, incl. `/account/*`) |
| Other clients' auth | `desktop-auth`, `app-auth`, `app-sync` |
| 2FA | `mfa` |
| Audit log | `security-events` |
| Auth / session issuance | `login`, `oauth`, `auth`, `token` |
| Streaming / SSE | `agents/create-flow-stream`, `agents/create-flow` |

Carve-outs explicitly allowed from the user-management exclusion:
`GET /users/me` (own profile) and `GET /users/search` (minimal user-picker
projection, needed to wire shares).

**Other limits:**
- Request body: capped at `ACCOUNT_API_PROXY_MAX_BODY_BYTES` (default 1 MiB) → **413**
- Response body: capped at `ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES` (default 8 MiB) → **502**
- Streaming responses (`text/event-stream`): blocked → **502**
- Rate limit: `ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN` (default 120/min per account
  token, sliding window) → **429 + Retry-After**
- Malformed or path-traversal paths: **400 `malformed_path`**

**Audit policy:** `CLI_ACCOUNT_API_PROXY_CALL` security event is written **only on
exclusion hits** (`excluded_path` or `excluded_method` reason). Allowed calls write
an `info`-level log line (`method path → status`) — not a security event — to avoid
flooding the audit log with high-frequency developer tool calls. `malformed_path`
(caller mistake, not a probe) is not audited.

**Structural guarantee preserved:** the account token still only authenticates the
outer `/account/api-proxy` route via `AccountCLIContextDep`. The inner JWT is
minted inside the service, used once, and never returned to the CLI.

### 7. Registering an Agentic Network (Phase 4)

Once the agents are created and wired (via `cinna agent create`, `cinna connect
agent-api`, `cinna connect mcp`), the orchestrator can register a **team graph**
that encodes the delegation topology — which agent may hand a subtask to which
other agent. This graph is static (Blueprint phase): it does not run anything, but
it is load-bearing because the task system enforces that
`mcp__agent_task__create_subtask` is only permitted along a drawn connection.

**No new CLI verbs were added in Phase 4.** The agentic-teams API is not on the
escape-hatch denylist (see §5 for the denylist), so every team endpoint is fully
reachable through `cinna api`. This is a deliberate thin-client decision: team
registration is a low-frequency, one-shot act; the escape hatch is the designated
home for exactly this case.

The canonical sequence (all via `cinna api`):

```
# 1. Create the team (task_prefix gives subtasks readable short-codes)
cinna api POST agentic-teams \
  --json '{"name":"Meeting Booking","icon":"users","task_prefix":"BOOK"}'
# → capture team id from response

# 2. Add a node per agent (agent_id from cinna account agents or cinna agent create)
cinna api POST agentic-teams/<team_id>/nodes/ \
  --json '{"agent_id":"<front-desk id>","is_lead":true}'
cinna api POST agentic-teams/<team_id>/nodes/ \
  --json '{"agent_id":"<crm-agent id>"}'
# ... repeat for each agent
# To read back all node ids at once:
cinna api GET agentic-teams/<team_id>/chart

# 3. Draw delegation edges
cinna api POST agentic-teams/<team_id>/connections/ \
  --json '{"source_node_id":"<front-desk node id>","target_node_id":"<crm-agent node id>","connection_prompt":"Hand off contact lookups..."}'

# 4. (Optional) AI-generate a handover prompt, review it, then save
cinna api POST agentic-teams/<team_id>/connections/<conn_id>/generate-prompt
cinna api PUT  agentic-teams/<team_id>/connections/<conn_id> \
  --json '{"connection_prompt":"<edited text>"}'

# 5. Verify
cinna api GET agentic-teams/<team_id>/chart
```

The CLI-built team is a first-class platform team — identical to one drawn in the
UI. Open `/agentic-teams/<id>` for a visual check.

**Two wiring layers to distinguish:**
- **Capability wiring** (`cinna connect agent-api` / `cinna connect mcp`) — creates
  a credential on the consumer so it can *call* the producer's tools.
- **Delegation wiring** (team connections) — permits agent A to *hand a subtask to*
  agent B via the task system.

A complete network typically needs both for a given pair: e.g. `front-desk → crm-agent`
needs an agent-api credential (capability) AND a team connection (delegation).
`cinna connect mcp` is agent-to-agent only — connecting calendar-agent to Google
Calendar requires per-agent credential setup via browser OAuth (credentials never
leave the platform; this is an intentional MANUAL step).

**The `context/guides/build-an-agentic-network.md` playbook** (included in the
context package since Phase 4) walks through the full meeting-booking scenario
end-to-end, including the capability-vs-delegation table, the create→capture→reference
id-capture loop, and the business-rule cheat-sheet. The orchestrator `CLAUDE.md`
points to it for "build a multi-agent network" requests.

### 8. Managing Account Sessions (UI)

1. Settings → Channels → Local Development card lists active account sessions.
2. Each row shows machine name, token prefix, and the synced-child count
   (e.g. "3 agents synced"), giving the cascade blast radius at a glance.
3. **Disconnect** (icon-only button, destructive alert dialog) — revokes the
   account token. Dialog warns: "Revoking disconnects all agents synced from
   this machine."
4. After revocation: the account token entry disappears from the list; on the
   next CLI call all child tokens return 401 and Mutagen pauses.
5. Local files remain intact on the developer's machine; only the session
   credentials are revoked.

## Business Rules

### Token-Type Capability Isolation

The account token and per-agent tokens operate on **separate route groups**:

| Token type | Works on | Rejected by |
|------------|----------|-------------|
| `cli-account` | `/api/v1/cli/account/*`, `/api/cli-setup/account/*` | Every per-agent CLI route (sync, exec, workspace, building-context, knowledge) |
| `cli` (per-agent) | All per-agent CLI routes | Every `/api/v1/cli/account/*` route (list, mint) |

This is a structural guarantee — the account token is wired only through
`AccountCLIContextDep`, which requires `token_type == "cli-account"`.
`CLIContextDep` / `CLIContextWSDep` explicitly reject `"cli-account"` tokens.

### `can_build` Predicate

```
can_build(user, agent) :=
    RoleService.is_developer(user)
    AND NOT AgentService.is_foreign_install(agent)
    AND AgentService.user_can_access(session, user, agent)
```

where `is_foreign_install` is `bundle_uuid is not None AND NOT is_publisher_install`,
and `user_can_access` is `agent.owner_id == user.id`.

`can_build` gates:
1. `POST /api/v1/cli/account/agents/{id}/mint` — mint endpoint.
2. `POST /api/v1/cli/setup-tokens` — per-agent setup-token creation (changed
   from bare-ownership to `can_build` as part of this feature — a correctness
   fix that 403s foreign installs and non-developer users).
3. The `can_build` flag on each row of `GET /account/agents`.

Foreign-bundle installs **do appear** in the listing (flagged
`can_build=false, is_foreign_install=true`) but cannot be mint / sync / exec
targets.

### Cascade Revocation

Revoking an account token:
- Soft-revokes (`is_revoked=True`) the account token itself.
- Soft-revokes all `CLIToken` rows where `minted_by_account_token_id` equals
  the revoked token's ID.
- Does NOT touch per-agent tokens from other sources (hand-created via the
  per-agent Integrations tab or via separate `cinna setup` runs).

Independently revoking a child (via the per-agent UI Disconnect button or
`cinna disconnect`):
- Only that child is revoked; the account token and all siblings remain alive.

### Token Listing Filter

`GET /account/tokens` returns only non-revoked, non-expired account tokens
(`token_type="cli-account"`, `is_revoked=False`, `expires_at > now`), ordered
newest-first. Revoked tokens are hidden immediately.

### Rolling Expiry

Account tokens use the same 7-day rolling expiry as per-agent tokens.
`CLIAuthService.refresh_token_usage(db, token, environment=None)` is called on
every authenticated request; the `environment=None` path skips the env
`last_activity_at` keepalive (there is no single environment to keep warm).

### Phase 3 Gating

The three Phase 3 routes are all `require_developer`-gated (mirrors the account
setup-token creation gate):

| Route | Additional gate beyond `require_developer` |
|-------|---------------------------------------------|
| `POST /account/agents` | None (all defaults applied server-side) |
| `POST /account/connect/agent-api` | Producer ownership + consumer ownership (service-enforced) |
| `GET /account/connect/mcp/discoverable` | None — listing is unrestricted for any account token holder |
| `POST /account/connect/mcp` | Connector ACL (owner/allowed-user/superuser) + consumer ownership (service-enforced) |
| `POST /account/api-proxy` | Single exclusion chokepoint (`assert_api_proxy_allowed`); no `require_developer` at route level — the account token itself implies developer |

The escape hatch (`api-proxy`) is not separately `require_developer`-gated because
account tokens can only be obtained by developers (the account setup-token creation
is `require_developer`-gated), and the inner call runs as the real user under
normal JWT auth.

### Security Events

| Event | When | `agent_id` | Details |
|-------|------|-----------|---------|
| `CLI_ACCOUNT_TOKEN_CREATED` | Account setup-token exchange | `None` | `{machine_name, ip}` |
| `CLI_ACCOUNT_CHILD_TOKEN_MINTED` | Every successful mint | target agent | `{account_token_id, child_token_id, prefix, ip}` |
| `CLI_ACCOUNT_CHILD_TOKEN_REVOKED` | Successful child-token revoke via `unsync`; written only on a real revoke (already-revoked is a no-op, no duplicate event) | target agent | `{account_token_id, child_token_id, prefix, ip}` |
| `CLI_ACCOUNT_CONNECT_AGENT_API` | Successful `cinna connect agent-api` | consumer agent | `{producer_agent_id, credential_id, token_prefix, ip}` |
| `CLI_ACCOUNT_CONNECT_MCP` | Successful `cinna connect mcp` | consumer agent | `{connector_id, credential_id, ip}` |
| `CLI_ACCOUNT_API_PROXY_CALL` | Exclusion hit (`excluded_path` or `excluded_method`) on `api-proxy` — NOT on allowed calls or `malformed_path` | `None` | `{method, path, reason, account_token_id, ip}` |

### Setup-Token Kind Guard

A per-agent setup token (`kind="agent"`) cannot be exchanged on the account
exchange path and vice versa. Each exchange endpoint validates the `kind` field
and returns 400 on mismatch.

### No Existence Leak

`assert_can_build` checks access first. An agent that belongs to a different
user raises `"not_accessible"` (mapped to 404), not 403 — so the mint endpoint
never reveals whether an inaccessible agent UUID exists.

## CLI Companion Commands

The CLI-side implementation lives in the separate `cinna-cli` repo.
The backend contract these commands consume:

**Phase 1–2 commands:**

| Command | Backend endpoint | Behavior |
|---------|-----------------|----------|
| `cinna account setup <token_or_url>` | `POST /api/cli-setup/account/{token}` then `GET /api/v1/cli/account/context-package` | Exchange account setup token; download and extract context package into `context/`; write `account.json` + `CLAUDE.md` |
| `cinna account refresh-context` | `GET /api/v1/cli/account/context-package` | Re-download the context package and replace `context/` in place; warns and exits cleanly on failure without corrupting existing content |
| `cinna account agents` | `GET /api/v1/cli/account/agents` | Print accessible-agents table with `can_build` / `is_foreign_install` flags |
| `cinna agent sync <agent>` | `POST /api/v1/cli/account/agents/{id}/mint` then existing per-agent bootstrap | Mint child token; write `agents/<slug>/` as a standard workspace |
| `cinna agent unsync <agent>` | `DELETE /api/v1/cli/account/tokens/children/{child_token_id}` then local | Revokes the child token server-side (authenticated by the account token), then stops sync and removes `agents/<slug>/` from the local registry |
| `cinna exec --agent <agent> <cmd>` | Existing `POST /api/v1/cli/agents/{id}/exec` | Mint (if needed) then exec with child token |

**Phase 3 commands:**

| Command | Backend endpoint | Behavior |
|---------|-----------------|----------|
| `cinna agent create <name> [--description D]` | `POST /api/v1/cli/account/agents` body `{name, description, env_name}` | Create agent with backend defaults; print created agent's id, name, env id. 403 if not developer |
| `cinna connect agent-api --producer P --consumer C [--label L] [--read-only]` | `POST /api/v1/cli/account/connect/agent-api` | Resolve P/C names → IDs from cached agents list; body `{producer_agent_id, consumer_agent_id, credential_label, read_only_override}`; print credential_id, token_prefix, base_url, spec_url |
| `cinna connect mcp --producer P --consumer C [--label L] [--conversation-only\|--building-only]` | `GET …/account/connect/mcp/discoverable?consumer_agent_id=C` then `POST …/account/connect/mcp` | Resolve P → connector_id from discoverable list; body `{connector_id, consumer_agent_id, mcp_mode_conversation, mcp_mode_building, label}`; print credential_id, endpoint_url |
| `cinna api <METHOD> <path> [--json '<obj>'\|--data @file.json] [--query k=v ...]` | `POST /api/v1/cli/account/api-proxy` body `{method, path, query, json_body}` | Generic escape hatch; path is relative to API root; response body to stdout (pretty-printed if JSON); exit code 0 for 2xx, non-zero for 4xx/5xx; proxy errors (403/400/429/413/502) to stderr |

### `.cinna/account.json` Schema

```json
{
  "platform_url": "https://platform.example.com",
  "frontend_url": "https://platform.example.com",
  "account_token": "<account CLI JWT>",
  "machine_name": "My MacBook"
}
```

Written with `0o600` permissions. The account token is used **only** for the
`/account/*` endpoints. Per-agent child tokens are stored separately in each
child workspace's `.cinna/config.json` and in `~/.cinna/agents.json`.

## Edge Cases and Error Handling

| Scenario | Outcome |
|----------|---------|
| Account token used on a per-agent route | 401 (structural rejection by `_resolve_cli_context`) |
| Per-agent token used on an account route | 401 (structural rejection by `_resolve_account_cli_context`) |
| Regular user JWT used on `/account/agents` | 401 |
| Mint on a foreign install | 403 with message about publisher-managed workspace |
| Mint on an inaccessible / non-existent agent | 404 (no existence leak) |
| Mint after user demoted to `agent-user` | 403 on next mint (`can_build` re-checked on every call) |
| Account token revoked; children try to sync/exec | 401 on next API call; Mutagen pauses; local files intact |
| Agent deleted | `agent_id` CASCADE removes the child token; account token unaffected |
| Re-exchange a used setup token | 400 "already been used" |
| Per-agent setup token exchanged on account path | 400 (kind mismatch) |
| Concurrent mints for the same agent | Both succeed (two child tokens); each is distinct |
| `cinna agent unsync` on an already-revoked child token | 200 (idempotent); no duplicate `CLI_ACCOUNT_CHILD_TOKEN_REVOKED` event written |
| Child-token revoke attempted with a user JWT or per-agent token | 401 (route requires account CLI token) |
| Child-token revoke for a token minted by a different account token | 404 (existence-leak discipline) |
| `cinna agent create` by `agent-user` | 403 (`require_developer` at route) |
| `connect agent-api` with non-owned producer | 403 (service ownership check) |
| `connect agent-api` when producer's REST API is disabled | 400 (service) |
| `connect agent-api` with non-owned consumer | 403 (service ownership check) |
| `connect mcp` connector not in caller's ACL | 403 (service ACL check) |
| `connect mcp` missing or non-agent2agent connector | 404 (no-leak, service) |
| `cinna api` targets `credentials/*`, `admin/*`, `cli/*`, etc. | 403 `excluded_path` + `CLI_ACCOUNT_API_PROXY_CALL` SecurityEvent |
| `cinna api GET users/me` | Allowed (carve-out from user exclusion) |
| `cinna api GET users/search` | Allowed (carve-out from user exclusion) |
| `cinna api` path contains `..` or is not under `/api/v1` | 400 `malformed_path` (not audited) |
| `cinna api` to a streaming/SSE route | 403 (denylist) or 502 if inner response is `text/event-stream` |
| `cinna api` request body > 1 MiB | 413 |
| `cinna api` inner response > 8 MiB | 502 |
| `cinna api` rate limit exceeded | 429 + `Retry-After` header |
| `cinna api` inner route returns 4xx/5xx (e.g. 404 agent not found) | Mirrored verbatim — the escape hatch is transparent for allowed paths |
| `cinna api` called via `cinna api POST cli/account/api-proxy` (recursion) | 403 (CLI prefix is excluded) |
| `connect agent-api` with default everything (no consumer) | Credential created without a linked consumer; consumer agent is optional |

## Roadmap Note

This document covers **Phases 1 through 4** — all four phases are now shipped:

- **Phase 1** — Account token type, setup-token flow, accessible-agents listing,
  child-token minting, cascade revocation, and the Settings card.
- **Phase 2** — Account context package: `GET /account/context-package` endpoint,
  in-process memoized tarball assembly, `cinna account setup` context extraction,
  and `cinna account refresh-context` command.
- **Phase 3** — Convenience verbs (`cinna agent create`, `cinna connect agent-api|mcp`,
  `cinna api <METHOD> <path>`) and the generic API escape hatch.
- **Phase 4** — Agentic-network registration via the escape hatch (no new CLI
  verbs or backend endpoints); `context/guides/` subtree added to the context
  package; `context/guides/build-an-agentic-network.md` playbook ships a
  full end-to-end meeting-booking walkthrough covering agent creation, capability
  wiring, and team-graph registration via `cinna api`.

## Integration Points

- **cinna_cli_integration** — the host of all per-agent CLI runtime machinery
  (setup token, `CLIToken` model, sync, exec, building context). The account
  feature adds a sibling route group and `AccountCLIContext` dep without
  duplicating any per-agent runtime. See
  [cinna_cli_integration.md](cinna_cli_integration.md)
- **agent_bundles** — `is_foreign_install` reuses
  `agent.bundle_uuid is not None AND NOT agent.is_publisher_install` from the
  bundles model. See [agent_bundles.md](../../agents/agent_bundles/agent_bundles.md)
- **user_roles** — `can_build` calls `RoleService.is_developer` to require
  `agent-developer` or `admin`. See
  [user_roles.md](../user_roles/user_roles.md)
- **events / security_events** — six `SecurityEvent` constants covering account
  token lifecycle, child-token minting/revocation, Phase 3 connect verbs, and
  escape-hatch exclusion hits
- **general_assistant** — the `general-assistant-env` template snapshot is the
  source for the context package's platform docs and API reference. The
  generation logic is factored into `ga_knowledge_assets.py`, shared between
  this endpoint and the GA sync script. See
  [general_assistant.md](../general_assistant/general_assistant.md)
- **agent_api** (Phase 3) — `POST /account/connect/agent-api` wraps
  `AgentApiTokenService.connect_agent_api`; the resulting `AGENT_API` credential
  rides the existing credential sync / whitelist / redaction pipeline unchanged.
  See [agent_api.md](../../agents/agent_api/agent_api.md)
- **agent_to_agent_mcp_connector** (Phase 3) — `POST /account/connect/mcp` wraps
  `MCPProviderService.connect_to_agent`; the resulting `MCP_PROVIDER` credential
  is injected into the consumer SDK runtime config (not `credentials.json`).
  `GET /account/connect/mcp/discoverable` is the account-token-accessible
  passthrough to the discoverable-agents picker. See
  [agent_to_agent_mcp_connector.md](../mcp_integration/agent_to_agent_mcp_connector.md)
- **agentic_teams** (Phase 4) — the agentic-teams API is not on the escape-hatch
  denylist, so the orchestrator reaches it entirely via `cinna api`. The team
  graph (nodes + directed connections) is the delegation-policy artifact that
  permits `mcp__agent_task__create_subtask` along drawn edges. See
  [agentic_teams.md](../../agents/agentic_teams/agentic_teams.md)
