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
| **Account Setup Token** | Short-lived (15 min), single-use token with `kind="account"`. Created from Settings → Security → Local Development. The `curl | python3` one-liner exchanges it for an account CLI token |
| **Account CLI Token** | Long-lived JWT (`token_type="cli-account"`, `agent_id=NULL`). Stored in `.cinna/account.json` on the user's machine. Scoped **only** to the `/account/*` route group — rejected by all per-agent routes |
| **Child Token (minted)** | A standard per-agent `token_type="cli"` token minted by the account token via `POST /account/agents/{id}/mint`. Carries `minted_by_account_token_id` as provenance. Authenticates the existing per-agent sync / exec / workspace endpoints unchanged |
| **Building-rights predicate (`can_build`)** | `developer-or-admin role AND not a foreign install AND user owns the agent`. The single gate for setup-token creation (both per-agent and account), the mint endpoint, and the `can_build` flag in the agents listing |
| **Accessible-agents listing** | `GET /account/agents` — returns the user's own agents with `can_build`, `is_foreign_install`, `has_active_environment`, and `user_workspace_id`. No credentials, prompts, or env internals. The endpoint always returns the full owner-scoped set; `cinna account agents` filters to the active workspace client-side (`--all` to show every workspace) |
| **Cascade revocation** | Revoking an account token soft-revokes every child token it minted. Independent per-agent tokens from other sources are unaffected |

## User Stories / Flows

### 1. Bootstrapping the Account Workspace

1. User navigates to **Settings → Security** tab.
2. The **Local Development** card is visible only to `agent-developer` / `admin`
   users (agent-users do not see it at all).
3. User clicks **Setup** — the platform generates a
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
   env-active indicator. The listing is **scoped to the active user workspace by
   default** (flow 3) — the CLI filters the full `GET /account/agents` result
   client-side by each row's `user_workspace_id` and the header states which
   workspace is shown; `cinna account agents --all` lists every workspace. The
   endpoint itself is unchanged and always returns the full owner-scoped set
   (the name/id resolvers used by `sync` / `connect` / `agent-api` rely on
   seeing every agent regardless of the active workspace).
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
      authoring-agent-prompts.md    # how to author prompts and finalize the description
    local-kit/            # Local Agent Kit, rendered for this instance (see below)
  agents/
    crm-agent/           # 100% standard cinna per-agent workspace
      .cinna/config.json
      workspace/
      CLAUDE.md  BUILDING_AGENT.md  mutagen.yml  .mcp.json  opencode.json
```

### 4. Downloading the Platform Context Package

The context package is static, pre-built platform knowledge: no user data,
no secrets. It gives the orchestrator agent in the account workspace the
same self-knowledge snapshot that the platform ships in its knowledge
template environment.

1. `cinna account setup` calls
   `GET /api/v1/cli/account/context-package` (authenticated by the account
   CLI token) and receives a `application/tar+gzip` tarball.
2. The CLI extracts it safely (rejects `..` and absolute-path members) into
   the account workspace's `context/` tree.
3. The orchestrator `CLAUDE.md` points at `context/README.md` as its first
   read. From there the agent navigates to `context/platform/` for feature
   docs, `context/api_reference/` for endpoint signatures, `context/guides/`
   for end-to-end worked walkthroughs (Phase 4), and `context/local-kit/` for
   the [Local Agent Kit](../local_agent_kit/local_agent_kit.md)'s conventions —
   read `context/local-kit/guides/11-go-cloud.md` when the orchestrator is
   asked to import an agent someone built locally with `cinna agent import`
   (cinna-cli, separate repo).
4. `cinna account refresh-context` re-downloads and replaces the `context/`
   tree in place. If the download fails, the command warns and exits without
   corrupting the existing `context/` content.
5. **Staleness is detectable.** The package stamps its own content version into
   `context/VERSION`, echoes it on the download as the
   `X-Context-Package-Version` header, and serves it from
   `GET /api/v1/cli/account/context-package/version`. A workspace set up before
   a guide — or a whole set of verbs — existed otherwise has no way to know it
   is behind, because the package is extracted once at setup and never checked
   again; comparing the two values is what lets the CLI say so. The version is a
   hash of the packaged **content**, not of file mtimes, so a redeploy shipping
   identical knowledge does not tell every workspace it is stale. A workspace
   with no `VERSION` file at all predates the stamp, which is itself the answer.

The package is assembled from the committed `platform-knowledge-env`
template snapshot inside the backend container (the only copy of this
knowledge available at runtime — `docs/` is not in the image). It is
built once per deployment and then memoized in-process (keyed by snapshot
mtime + file count across all four source dirs, `local-kit/` included), so
repeated downloads are cheap. If the platform-docs snapshot is missing or
empty, the endpoint returns **503** rather than serving a near-empty package —
the caller can detect and report the deploy defect. Missing `examples/`,
`guides/`, or `local-kit/` is tolerated: a warning is logged and the
corresponding `context/` subtree is simply omitted from the package.
`local-kit/` is packaged **rendered** (through
[`LocalAgentKitService`](../local_agent_kit/local_agent_kit_tech.md), reusing
its own memoized build rather than reading the raw snapshot a second time), so
this instance's URLs are already resolved and the copy inside the context
package is byte-identical to the one an assistant downloads from `/agent-start` —
never gated on `ServerConfig.local_agent_kit_enabled`, since that flag governs
only the public anonymous surface, not what an already-authenticated account
workspace may read.

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

### 4c. Building a Producer's Agent REST API (`cinna agent-api`)

`cinna connect agent-api` (flow 4 above) only *wires* a consumer to a producer
that **already exposes** a REST API. The `cinna agent-api` verbs are the other
half: they let a local coding agent **build and verify** that producer API
itself — the CLI equivalent of the Integrations → **Agent REST API** card's
enable toggle, **Refresh**, and **View Spec**. This closes the loop so a coding
assistant can stand up the whole thing end-to-end without the browser:

```
# 1. Create the producer + consumer agents
cinna agent create acme-orders
cinna agent create crm-agent

# 2. Sync the producer and author its API
cinna agent sync acme-orders
#    write agents/acme-orders/workspace/agent_api/orders.py + policy.yaml,
#    then land it in the container (cinna dev live-sync, or cinna exec)

# 3. Turn the feature on
cinna agent-api enable acme-orders        # → prints status (enabled, state, spec?)

# 4. Re-harvest + verify the spec after each code/policy edit
cinna agent-api refresh acme-orders       # re-import modules + re-parse policy.yaml
cinna agent-api spec    acme-orders        # the harvested OpenAPI JSON (or -o file)

# 4b. Smoke-test an endpoint directly (no consumer needed; query params work)
cinna agent-api call acme-orders btc-rate --query vs_currency=eur

# 5. Once the spec looks right, wire the consumer (flow 4)
cinna connect agent-api --producer acme-orders --consumer crm-agent

# Recover a wedged env / stuck producer API, or inspect what the runtime sees
cinna agent restart-env acme-orders
cinna agent show acme-orders --prompts
```

Each verb resolves `AGENT_REF` (name / slug / id) against the cached `cinna
account agents` listing, then calls a thin `/account/*` endpoint that delegates
to the same services the UI uses:

| Command | Backend endpoint | Delegates to | Gate |
|---------|------------------|--------------|------|
| `cinna agent-api enable <agent> [--disable]` | `POST /account/agent-api/enable` | `AgentService.update_agent` (`agent_api_enabled`) | `require_developer` + producer ownership (404 no-leak) |
| `cinna agent-api refresh <agent>` | `POST /account/agent-api/refresh` | `AgentApiService.get_spec(force_refresh) + load_policy` | producer ownership (404 no-leak) |
| `cinna agent-api spec <agent> [-o file]` | `GET /account/agent-api/spec?agent_id=` | `AgentApiService.get_spec` | producer ownership (404 no-leak) |
| `cinna agent-api call <agent> <path> [-X method] [--query k=v] [--json …]` | `POST /account/agent-api/call` | owner-preview proxy (`adapter.proxy_agent_api`, buffered) | producer ownership (404), `agent_api_enabled` (400), running env (503) |
| `cinna agent restart-env <agent>` | `POST /account/agents/{id}/restart-env` | `EnvironmentService.restart_environment` | `can_build` (404 no-leak / 403); 400 if no active env. **Audited** (`CLI_ACCOUNT_ENV_RESTARTED`) |
| `cinna agent show <agent> [--prompts]` | `GET /account/agents/{id}/inspect` | `Agent` fields + `CredentialsService.get_agent_credentials` (metadata only) | producer ownership (404 no-leak) |

Behavioural notes:

- **`enable` doubles as a verify.** It returns the resulting agent-api status
  (`agent_api_enabled`, `state`, `spec_available`, `last_error`, `env_status`),
  so a coding agent confirms the toggle and learns whether a spec is already
  available in one round-trip. `--disable` flips it back off.
- **`refresh` is the iterative dev loop.** It mirrors the card's **Refresh**
  button: a successful re-harvest refreshes the spec and re-parses `policy.yaml`;
  a failed one records the error. It **never raises on a harvest failure** — the
  returned status's `last_error` carries it (the CLI prints it as a warning), so
  the agent reads the import error, fixes `agent_api/*.py`, syncs, and refreshes
  again.
- **`spec` is machine-facing.** It prints the harvested OpenAPI JSON to stdout
  (plain, pipe-friendly) or writes it to a file with `-o`. `400` if the API is
  disabled, `503` if the env is not running and the spec cache is cold. The
  status surfaced by `enable`/`refresh` now also carries `spec_fetched_at`, which
  the CLI renders as a "Spec harvested: 3m ago" line so spec freshness is visible
  separately from the live serving-child `state`.
- **`call` is the owner-side smoke test.** It invokes one of the producer's own
  endpoints through the owner-preview proxy (no consumer token, no policy edge)
  and prints status + body. **Query params are forwarded**, so it catches the
  silent query-drop class of bug directly — replacing hand-rolled consumer
  probes. Exit code 0 for an inner 2xx, 1 for a 4xx/5xx (body still printed).
- **`restart-env` is the recovery path.** It bounces the agent's container the
  same way the UI restart button does — the supported way to clear a wedged env
  or a stuck producer serving child, instead of the raw
  `environments/{id}/restart` escape hatch. It is `can_build`-gated and audited
  (`CLI_ACCOUNT_ENV_RESTARTED`); 400 if the agent has no active environment.
- **`show` answers "is what I edited actually live?"** It aggregates the agent's
  effective prompts (the DB fields synced verbatim into the runtime's prompt
  docs), enabled features, and connected credential metadata (name + type ONLY —
  never a secret), plus live agent-api status when enabled. `--prompts` narrows
  the output to just the prompts.
- **Authoring the code is still a sync step.** The producer's `agent_api/*.py`
  and `policy.yaml` live in the *agent's* workspace, not the account workspace —
  the coding agent writes them under `agents/<producer>/workspace/agent_api/` and
  lands them with `cinna dev` (live Mutagen sync) or `cinna exec`. `cinna
  agent-api` only manages the feature toggle, spec cache, and spec read.

**Gating** mirrors the connect verbs: `enable` (a state change) is
`require_developer`-gated; `refresh` / `spec` are diagnostic and open to any
account-token holder, but all three enforce producer **ownership** via
`AgentApiService.resolve_agent_only` (404 no-leak, never 403, so an inaccessible
agent id is never confirmed to exist).

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

### 6b. Querying Platform Knowledge from the Account Workspace

While building in the account workspace, the local orchestrator agent can query the platform's knowledge DB — the same indexed articles that an in-container building agent can access via its `knowledge_query` MCP tool.

The query is handled via the account workspace's MCP proxy (wired in the CLI-side `.mcp.json`; implementation in the `cinna-cli` repo). The proxy forwards each `knowledge_query` tool call to the backend:

1. Orchestrator agent invokes `knowledge_query` (or `mcp__cinna_account__knowledge_query`) inside the account workspace.
2. The MCP proxy POSTs to `POST /api/v1/cli/account/knowledge/search` authenticated with the account CLI token.
3. The backend resolves all knowledge sources accessible to the account user: **all public sources** plus the user's own private connected sources. No agent scope, no workspace filter (`workspace_id=None`).
4. A vector search runs and returns ranked chunks. The response is always `{"results": [{content, source, similarity}, ...]}` — an empty list if no accessible sources exist.
5. The result is returned to the orchestrator agent as the MCP tool output.

This is the account-level analogue of the per-agent knowledge proxy (see [cinna_cli_integration.md](cinna_cli_integration.md) — "MCP Proxy" concept and flow 2 step 6). The scoping differs: the per-agent proxy filters results to the agent's user workspace; the account proxy applies no workspace filter (the orchestrator agent has no associated workspace).

No additional setup is required beyond the standard account workspace bootstrap (`cinna account setup`). The `knowledge_query` tool is available in the account workspace's `.mcp.json` from that point on.

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

### 7b. Authoring Agent Prompts (Finalize Step)

Once an agent's functionality is built and verified — scripts run, connections
resolve, the REST API spec harvests — the orchestrator should author the agent's
prompts and write a description that matches the **finished** agent. This is the
final acceptance step of every build.

**Author prompts LAST.** Create the agent with a one-line provisional
description, build and verify all functionality first, then come back to write
the real prompt set from what actually exists.

**The six prompt-ish fields** (each consumed by a different system):

| Field | Consumer | Notes |
|-------|---------|-------|
| `description` | Discovery cards, A2A card; feeds router-trigger / A2A skill generation | One clear sentence. Rewrite it to match the *finished* agent. |
| `workflow_prompt` | Conversation-mode system prompt — every conversation session | Operational: which scripts to run, how to parse output, how to present results. The agent is a *bridge* that runs scripts, parses, and rephrases. |
| `entrypoint_prompt` | First user message for scheduled / automated runs | Conversational, **not** technical. ✅ *"What is my time-off balance?"* Fired automatically, so it must be self-contained — never a placeholder. |
| `refiner_prompt` | AI task refinement, before execution | Default-fill rules and mandatory fields. |
| `router_trigger_prompt` | App MCP router classifier only — never in any system prompt | Describes *when to route here*, not how to behave. |
| `example_prompts` | Surfaced as MCP slash commands and in the external agent catalog | Short imperative **user-ready templates**: `["reconcile last week", "show failed payouts", "Reconcile payouts for account <account id>"]`. See the templates rule below. |

**`example_prompts` are templates, not a replay of the build.** They are shown to
a different person, later, with different data — so the orchestrator must never
freeze build-session values (the URL it tested against, a fixture invoice id, a
sample file path, today's date) into them. Two legal shapes: *universal*, sendable
as-is (`"What is my status today?"`); or *input-required*, written as a visibly
unfinished template with an obvious placeholder (`"Investigate this URL — <paste
the URL here>"`). A realistic-looking fake (`https://example.com/report`,
`ACC-12345`) is worse than a blank — users send it unchanged. One structural
gotcha: `example_prompts` lines parse as `slug: prompt text` and the **first colon
splits the line** (`backend/app/mcp/prompts.py`), so a slugless template must not
end with a colon — use an em dash (`Investigate this URL — <url>`) or the explicit
`investigate_url: Investigate this URL — <url>` form. The same rule applies to
route-level `prompt_examples`. Full rationale, ✅/❌ table and self-check list:
`context/guides/authoring-agent-prompts.md`.

**The bulk workflow** — keep a local `agents/<name>/prompts.json` holding only the
prompt subset, and push it in one atomic write:

```jsonc
// agents/billing-agent/prompts.json
{
  "description": "Reconciles Stripe payouts against the ledger and flags mismatches.",
  "workflow_prompt": "You reconcile Stripe payouts. Run reconcile.py for the requested period, parse the JSON output, ...",
  "entrypoint_prompt": "Reconcile this week's payouts.",
  "refiner_prompt": "If no period is given, default to the current week. Always capture account id and currency.",
  "router_trigger_prompt": "Reconciles Stripe payouts and flags ledger mismatches.",
  "example_prompts": [
    "reconcile last week",
    "show failed payouts",
    "Reconcile payouts for account <account id>"
  ]
}
```

```bash
# 1. Bulk write — the agent's config (DB) is the authoritative source of truth
cinna api PUT agents/<agent_id> --data @agents/billing-agent/prompts.json

# 2. Verify what actually landed
cinna agent show billing-agent --prompts
```

All fields are optional — omitted keys are left unchanged. `agents/*` is not on
the escape-hatch denylist (only `agents/create-flow-stream` and `agents/create-flow`
are excluded), so `PUT /agents/{id}`, `POST /agents/{id}/sync-prompts`, and
`POST /agents/{id}/generate-router-trigger-prompt` are all reachable via
`cinna api` — no new backend endpoints or CLI verbs were needed.

**How it reaches the running environment:** the three document-backed prompts
(`workflow_prompt`, `entrypoint_prompt`, `refiner_prompt`) are seeded into the
container's `docs/*.md` automatically on the next environment start (`SEED_PUSH`
when the env files are empty — the fresh-agent case). If the environment is
**already running** and you want the doc prompts pushed immediately:

```bash
cinna api POST agents/<agent_id>/sync-prompts
```

`router_trigger_prompt`, `example_prompts`, and `description` are config-only and
take effect immediately after the write.

**Optional — let the platform generate the router trigger** from the agent's name
and description:

```bash
cinna api POST agents/<agent_id>/generate-router-trigger-prompt
```

**The finalize step (end of every build):**

1. Confirm all functionality works (scripts, connections, API spec, team wiring).
2. Author the full prompt set from what you *actually built*. Rewrite
   `description` explicitly in the same payload — do not rely on auto-derivation.
3. `cinna api PUT agents/<id> --data @agents/<name>/prompts.json`
4. `cinna agent show <name> --prompts` to confirm.
5. If the env is already running: `cinna api POST agents/<id>/sync-prompts`.

**The guide** (`context/guides/authoring-agent-prompts.md`) ships in
`knowledge/guides/` inside the `platform-knowledge-env` template (survives
knowledge re-sync, the same mechanism as the agentic-network playbook). The
context package assembler includes it verbatim under `context/guides/`; the
generated `context/README.md` index points the orchestrator at it for the
finalize step. Like the network playbook, it requires no new backend endpoints or
migration.

### 7c. Managing Agent Schedules (Phase 5)

Scheduling is a first-class part of building an agent, so the account workspace
ships dedicated verbs for the full schedule lifecycle — the CLI equivalent of the
agent Config → **Schedules** card. They wrap the same `AgentSchedulerService` the
UI route uses, so behaviour (CRON conversion, per-type frequency floor, the
foreign-install read-only contract) is identical.

```
# Turn natural language into a cron string + next-execution preview
cinna agent schedule generate crm-agent "every weekday at 7am" --tz Europe/Berlin

# Create a static-prompt schedule (always spins a session)
cinna agent schedule create crm-agent \
  --name "Daily report" --cron "0 6 * * 1-5" --tz Europe/Berlin \
  --prompt "Produce the daily report"

# Create a script-trigger schedule (only sessions when output != "OK")
cinna agent schedule create crm-agent \
  --name "DB check" --cron "*/30 * * * *" --tz UTC \
  --type script_trigger --command "python scripts/check_db.py"

cinna agent schedule list crm-agent                 # all schedules
cinna agent schedule update crm-agent <sid> --disable   # toggle off
cinna agent schedule run crm-agent <sid>            # Run now
cinna agent schedule logs crm-agent <sid>           # last 50 execution logs
cinna agent schedule delete crm-agent <sid>
```

Each verb resolves `AGENT_REF` against the cached `cinna account agents` listing,
then calls a thin `/account/agents/{id}/schedules*` endpoint:

| Command | Backend endpoint | Gate |
|---------|------------------|------|
| `cinna agent schedule list <agent>` | `GET …/schedules` | ownership (404 no-leak) |
| `cinna agent schedule generate <agent> <text>` | `POST …/schedules/generate` | ownership; stateless AI preview |
| `cinna agent schedule create <agent> …` | `POST …/schedules` | `require_developer` + ownership; **foreign install → 403** |
| `cinna agent schedule update <agent> <sid> …` | `PUT …/schedules/{sid}` | `require_developer` + ownership; on a foreign install only `enabled` may change (else 403) |
| `cinna agent schedule run <agent> <sid>` | `POST …/schedules/{sid}/run` | `require_developer` + ownership; allowed on foreign installs |
| `cinna agent schedule logs <agent> <sid>` | `GET …/schedules/{sid}/logs` | ownership |
| `cinna agent schedule delete <agent> <sid>` | `DELETE …/schedules/{sid}` | `require_developer` + ownership; **foreign install → 403** |

Behavioural notes:

- **Foreign installs are publisher-managed.** A consumer (bundle-owned,
  non-publisher) install may toggle / run / view logs, but cannot create / edit /
  delete the definitions — the same read-only contract the UI enforces. Create
  and delete return 403; update accepts only `enabled`.
- **Frequency floor is server-side.** `static_prompt` schedules must run no more
  than once every 10 minutes (computed from the real cron cadence);
  `script_trigger` has no floor. The backend rejects too-frequent
  `static_prompt` cadences on generate-preview and create.
- **Run now reflects the env state.** The success message is either "Schedule
  triggered successfully" (env was running) or "Environment is starting; the
  schedule will run automatically once it's ready." (env is waking up).
- Create / update / delete / run each emit a `CLI_ACCOUNT_SCHEDULE_*` security
  event; list / generate / logs are diagnostic reads and are not audited.

### 7d. Managing Agent Status (Phase 5)

The agent's self-reported status (`STATUS.md`) and its **status refresh command**
are also reachable from the account workspace — the CLI equivalent of the
Integrations → **Agent status** card. This lets a builder confirm an agent is
healthy and configure how its status is regenerated, all without the browser.

```
cinna agent status show crm-agent          # cached snapshot + configured command
cinna agent status refresh crm-agent       # force a live STATUS.md re-read
cinna agent status set-command crm-agent "/run:status"   # set the pre-command
```

| Command | Backend endpoint | Gate |
|---------|------------------|------|
| `cinna agent status show <agent>` | `GET …/status` | ownership (404 no-leak) |
| `cinna agent status refresh <agent>` | `GET …/status?force_refresh=true` | ownership |
| `cinna agent status set-command <agent> <cmd>` | `POST …/status/refresh-command` | `require_developer` + ownership |

Behavioural notes:

- **One read for both.** The `GET …/status` response is an
  `AccountAgentStatusResult` — the `AgentStatusPublic` snapshot **plus** the
  agent's configured `status_refresh_command` — so the builder sees the live
  state and the pre-command that produced it in one call.
- **Refresh runs the full force flow.** `?force_refresh=true` wakes a suspended
  env, runs the configured pre-command, re-reads `STATUS.md`, and falls back to
  the cached snapshot on any failure — it never raises. A pre-command that did
  not run cleanly is reported in `status.refresh_command_warning`.
- **Set-command mirrors the card input.** The command is a raw shell/Python
  string or a `/run:<name>` reference (resolved against the agent's
  `CLI_COMMANDS.yaml`); empty string is a deliberate opt-out; the platform
  default is `/run:status`. It flips `status_refresh_command` through the same
  `AgentService.update_agent` path the UI `PATCH /agents/{id}` uses and emits
  `CLI_ACCOUNT_STATUS_COMMAND_SET`. Reads / refreshes are diagnostic and not
  audited.

### 7e. Testing an Agent via Console Chat (`cinna chat`)

Once an agent is built and running, the orchestrator can smoke-test it end-to-end
through the real platform session pipeline — including sending local files — without
leaving the terminal. `cinna chat` is the account-workspace equivalent of opening a
session in the browser, and is the canonical final-verification step before
publishing or handing over an agent.

**Purpose:** drive the target agent through the same `MessageService` / session
pipeline the UI uses, so integration problems (wrong prompt wiring, broken
credential injection, file-handling bugs) are caught at the chat level, not just
by unit-testing scripts.

**Session control plane (api-proxy):** all session routes are reachable via the
standard `POST /account/api-proxy` escape hatch. The `sessions` prefix is **not**
on the exclusion denylist, so the following routes are already proxyable:

| Route | Purpose |
|-------|---------|
| `POST sessions/` | Create a new session for the target agent |
| `GET sessions/{id}` | Fetch session metadata |
| `POST sessions/{id}/messages/stream` | Send a message (returns a JSON ack, not an SSE body — see below) |
| `GET sessions/{id}/messages` | Poll for the reply messages |
| `GET sessions/{id}/messages/streaming-status` | Poll for streaming progress |
| `POST sessions/{id}/messages/interrupt` | Cancel a running generation |

**Polling, not streaming:** `POST sessions/{id}/messages/stream` returns a JSON
ack dict (`MessageService.build_stream_response`), not an SSE body. Real-time
streaming is over socket.io, which is not available to the CLI. The CLI observes
progress by polling `GET sessions/{id}/messages` and
`GET sessions/{id}/messages/streaming-status` until the reply is complete. The
api-proxy would block a genuine `text/event-stream` response anyway (that is a
documented proxy limit), but the stream endpoint is JSON, so the proxy delivers it
normally.

**File upload (the one new backend route):** the api-proxy carries only JSON
bodies. To attach a local file, `cinna chat --file <path>` calls the dedicated
multipart route `POST /api/v1/cli/account/files/upload` (authenticated by the
account CLI token) first. This route:
- is implemented in `backend/app/api/routes/cli.py` (function
  `account_upload_file`)
- delegates to `FileService.create_file_upload(session=db, user_id=..., file=file)`
  — the same service the normal `POST /files/upload` route uses, so it inherits
  the same size cap, MIME-type whitelist, and per-user storage quota validation
- returns `FileUploadPublic` (`id`, `filename`, `file_size`, `mime_type`, `status`,
  `uploaded_at`); new uploads start with `status="temporary"` and become durable
  when referenced in a session message's `file_ids`
- attributed to the account token's owning user; no new model, no migration, no
  config knobs

Once the file is uploaded, `cinna chat` creates (or reuses) a session via the
api-proxy and sends a message with the returned file id in `file_ids` — identical
to the UI flow.

**File download through the proxy:** `GET files/{id}/download` also works through
the api-proxy. The proxy mirrors binary response bodies 1:1, bounded by the 8 MiB
response cap. This lets `cinna chat` fetch any files the agent attaches in its
reply.

**The canonical chat-with-file flow:**

```
# 1. Upload the local file
#    → returns { id, filename, ... status="temporary" }
cinna chat --agent billing-agent --file ./q3_payouts.csv

# 2. Behind the scenes:
#    POST /account/files/upload (multipart)  → FileUploadPublic { id: <file_id> }
#    POST sessions/  (via api-proxy)         → { id: <session_id> }
#    POST sessions/<id>/messages/stream      → JSON ack
#         body: { content: "Analyse this file", file_ids: ["<file_id>"] }
#    poll GET sessions/<id>/messages         → until reply complete
#    poll GET sessions/<id>/messages/streaming-status

# 3. Agent reply (and any attached files) are printed to the terminal.
```

**Security:** the upload route and the chat flow add no new security events. File
upload is a normal authenticated account-user upload (same audit surface as the
regular `POST /files/upload`); session activity is recorded by the session
infrastructure unchanged.

**No new models, no migration, no config knobs.** The only backend addition is
the `POST /api/v1/cli/account/files/upload` route. Everything else — session
creation, message sending, polling, and file download — reaches existing platform
infrastructure through the api-proxy.

### 7f. Improvement Requests (`cinna improve`)

Turns a user's feedback on a bad agent response into something a local coding
agent can act on. When a session owner shares a session via a session's
**Improve Agent** menu item or `/session-improve` — see
[Agent Improvement Requests](../agent_improvement_requests/agent_improvement_requests.md)
— it lands as a request on the agent's owner (the bundle publisher for a
consumer install, or the requester themselves otherwise). These verbs are the
CLI-side surface for that owner to triage and close the loop.

```
cinna improve list --status new              # everything you own, across all agents
cinna improve show <id>                      # full detail incl. the frozen runtime context
cinna improve download <id>                  # → improvements/<short-id>/README.md, session/*, context.json, prompts/*, memory/*
cinna improve status <id> in_progress
cinna improve status <id> completed --note "Fixed in v1.4 — no longer re-asks for the file."
```

| Command | Backend endpoint | Behavior |
|---|---|---|
| `cinna improve list [--status S] [--agent A]` | `GET /api/v1/cli/account/improvement-requests` | Table across every agent you own: id, agent, requester, version, date, status |
| `cinna improve show <id>` | `GET /api/v1/cli/account/improvement-requests/{id}` | Full detail including the frozen `context` block |
| `cinna improve download <id> [--out DIR]` | `GET /api/v1/cli/account/improvement-requests/{id}/archive` | Saves + extracts the ZIP into `improvements/<short-id>/`, prints the path |
| `cinna improve status <id> <status> [--note N]` | `PATCH /api/v1/cli/account/improvement-requests/{id}` | Sets status (`new` / `in_progress` / `completed` / `declined`) and the resolution note, which is shown to the requester |

**These verbs live in the separate `cinna-cli` repository** — what exists in
this backend is the four `/account/improvement-requests*` routes above (all
delegating to the same `ImprovementRequestService` the web UI uses, so
ownership rules cannot drift between transports) and the shipped guide,
`context/guides/handling-improvement-requests.md`, which walks the loop:
discover → read → establish ownership (standalone agent vs. publisher install
vs. a foreign install a fallback landed on) → decide how much to change without
asking → fix → close the loop. The guide's read step now leads with
`prompts/README.md`: it tells the agent to diff the archive's prompt documents
against its own install before debugging, since a diverged consumer install is
not running the publisher's text, and to treat `memory/` as the reporter's
personal content — read to understand the run, never adopted into a workspace. The `improvement-requests` prefix is not on the
`/account/api-proxy` denylist, so `cinna api GET improvement-requests/mine` also
reaches the requester-side listing.

Behavioural notes:

- **Archive download is audited when cross-user.** The archive route writes the
  same `IMPROVEMENT_ARCHIVE_DOWNLOADED` security event the web route writes,
  whenever the recipient is not the requester.
- **`status` and `download` are the only mutations.** There is no `create` or
  `delete` verb — consent to share is final, raised only from the web UI or
  `/session-improve`, and only the recipient can act on a request once raised.

### 8. Managing Account Sessions (UI)

1. Settings → Security → Local Development card lists active account sessions.
   The list uses the same row style as the App Sessions card (`divide-y` list,
   leading device icon, name + muted sub-line).
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
| `CLI_ACCOUNT_AGENT_API_ENABLED` | Successful `cinna agent-api enable` (and `--disable`) — the state-changing toggle. `refresh` / `spec` / `call` are diagnostic and **not** audited | producer agent | `{enabled, ip}` |
| `CLI_ACCOUNT_ENV_RESTARTED` | Successful `cinna agent restart-env` — a build-rights state change (bounces the container). `agent show` is diagnostic and **not** audited | target agent | `{environment_id, ip}` |
| `CLI_ACCOUNT_API_PROXY_CALL` | Exclusion hit (`excluded_path` or `excluded_method`) on `api-proxy` — NOT on allowed calls or `malformed_path` | `None` | `{method, path, reason, account_token_id, ip}` |
| `CLI_ACCOUNT_SCHEDULE_CREATED` | Successful `cinna agent schedule create` | target agent | `{schedule_id, schedule_type, ip}` |
| `CLI_ACCOUNT_SCHEDULE_UPDATED` | Successful `cinna agent schedule update` (incl. toggle) | target agent | `{schedule_id, fields, ip}` |
| `CLI_ACCOUNT_SCHEDULE_DELETED` | Successful `cinna agent schedule delete` | target agent | `{schedule_id, ip}` |
| `CLI_ACCOUNT_SCHEDULE_RUN` | Successful `cinna agent schedule run` (spends tokens / spins a session) | target agent | `{schedule_id, action, ip}` |
| `CLI_ACCOUNT_STATUS_COMMAND_SET` | Successful `cinna agent status set-command` — `show` / `refresh` are diagnostic and **not** audited | target agent | `{command, ip}` |

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
| *(staleness probe)* | `GET /api/v1/cli/account/context-package/version` | `{"version": "<hash>"}` — compare against the workspace's `context/VERSION` to decide whether `refresh-context` is due. Cheap: the package is built once per process and cached |
| `cinna account agents [--all]` | `GET /api/v1/cli/account/agents` | Print accessible-agents table with `can_build` / `is_foreign_install` flags; **scoped to the active workspace by default** (client-side filter on `user_workspace_id`, header names the workspace), `--all` for every workspace |
| `cinna agent sync <agent>` | `POST /api/v1/cli/account/agents/{id}/mint` then existing per-agent bootstrap | Mint child token; write `agents/<slug>/` as a standard workspace |
| `cinna agent unsync <agent>` | `DELETE /api/v1/cli/account/tokens/children/{child_token_id}` then local | Revokes the child token server-side (authenticated by the account token), then stops sync and removes `agents/<slug>/` from the local registry |
| `cinna exec --agent <agent> <cmd>` | Existing `POST /api/v1/cli/agents/{id}/exec` | Mint (if needed) then exec with child token |
| `knowledge_query` MCP tool (account workspace `.mcp.json`) | `POST /api/v1/cli/account/knowledge/search` body `{query, topic?}` | Account-workspace MCP proxy forwards orchestrator `knowledge_query` calls to the backend; scoped to the account user's accessible sources (public + own private); returns `{results: [{content, source, similarity}]}`. Not a typed `cinna` subcommand — served via the MCP proxy. CLI proxy wiring lives in the `cinna-cli` repo |

**Phase 3 commands:**

| Command | Backend endpoint | Behavior |
|---------|-----------------|----------|
| `cinna agent create <name> [--description D]` | `POST /api/v1/cli/account/agents` body `{name, description, env_name}` | Create agent with backend defaults; print created agent's id, name, env id. 403 if not developer |
| `cinna connect agent-api --producer P --consumer C [--label L] [--read-only]` | `POST /api/v1/cli/account/connect/agent-api` | Resolve P/C names → IDs from cached agents list; body `{producer_agent_id, consumer_agent_id, credential_label, read_only_override}`; print credential_id, token_prefix, base_url, spec_url |
| `cinna connect mcp --producer P --consumer C [--label L] [--conversation-only\|--building-only]` | `GET …/account/connect/mcp/discoverable?consumer_agent_id=C` then `POST …/account/connect/mcp` | Resolve P → connector_id from discoverable list; body `{connector_id, consumer_agent_id, mcp_mode_conversation, mcp_mode_building, label}`; print credential_id, endpoint_url |
| `cinna agent-api enable <agent> [--disable]` | `POST /api/v1/cli/account/agent-api/enable` body `{agent_id, enabled}` | Resolve agent → id; toggle `agent_api_enabled`; print resulting status (state, spec_available). 403 if not developer |
| `cinna agent-api refresh <agent>` | `POST /api/v1/cli/account/agent-api/refresh` body `{agent_id}` | Force a spec + policy re-harvest; print status (warns on `last_error`). Never raises on a harvest failure |
| `cinna agent-api spec <agent> [-o file]` | `GET /api/v1/cli/account/agent-api/spec?agent_id=` | Print the harvested OpenAPI JSON to stdout (or write to `-o file`). 400 if disabled, 503 if env not running + cache cold |
| `cinna api <METHOD> <path> [--json '<obj>'\|--data @file.json] [--query k=v ...]` | `POST /api/v1/cli/account/api-proxy` body `{method, path, query, json_body}` | Generic escape hatch; path is relative to API root; response body to stdout (pretty-printed if JSON); exit code 0 for 2xx, non-zero for 4xx/5xx; proxy errors (403/400/429/413/502) to stderr |

**Phase 5 commands (schedules + status):**

| Command | Backend endpoint | Behavior |
|---------|-----------------|----------|
| `cinna agent schedule list <agent>` | `GET /api/v1/cli/account/agents/{id}/schedules` | Print the agent's schedules |
| `cinna agent schedule generate <agent> <text> [--tz]` | `POST …/schedules/generate` | NL → cron + next-execution preview (stateless) |
| `cinna agent schedule create <agent> --name --cron --tz [--type] [--prompt\|--command] [--disabled]` | `POST …/schedules` | Create a schedule; 403 on a foreign install; 400 if `script_trigger` without command or cadence too frequent |
| `cinna agent schedule update <agent> <sid> [--enable\|--disable] [--name --cron --tz --prompt --command]` | `PUT …/schedules/{sid}` | Partial update / toggle; on a foreign install only `enabled` may change (else 403) |
| `cinna agent schedule run <agent> <sid>` | `POST …/schedules/{sid}/run` | Run now; prints the env-state-aware message |
| `cinna agent schedule logs <agent> <sid>` | `GET …/schedules/{sid}/logs` | Print the last 50 execution logs |
| `cinna agent schedule delete <agent> <sid>` | `DELETE …/schedules/{sid}` | Delete; 403 on a foreign install |
| `cinna agent status show <agent>` | `GET …/status` | Print cached snapshot + configured refresh command |
| `cinna agent status refresh <agent>` | `GET …/status?force_refresh=true` | Force a live STATUS.md re-read (wakes a suspended env; never raises) |
| `cinna agent status set-command <agent> <cmd>` | `POST …/status/refresh-command` | Set `status_refresh_command`; 403 if not developer |

**Console chat (file upload + session control via proxy):**

| Command | Backend endpoints | Behavior |
|---------|-----------------|----------|
| `cinna chat --agent <agent> [--file <path>] [<message>]` | `POST /api/v1/cli/account/files/upload` (multipart, if `--file` given) then `POST sessions/`, `POST sessions/{id}/messages/stream`, `GET sessions/{id}/messages`, `GET sessions/{id}/messages/streaming-status` all via `POST /api/v1/cli/account/api-proxy` | Upload file → create session → send message with `file_ids` → poll for reply; stream endpoint returns JSON ack (polling not SSE); file download (`GET files/{id}/download`) also via proxy (binary 1:1, 8 MiB cap) |

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
| `agent-api enable` on a non-owned / non-existent agent | 404 (no existence leak) |
| `agent-api enable` by a demoted (agent-user) account token | 403 (`require_developer`, re-checked per call) |
| `agent-api spec` while the producer's API is disabled | 400 (disabled) |
| `agent-api spec` when the env is not running and the spec cache is cold | 503 |
| `agent-api refresh` when the harvest fails (bad `agent_api/` code) | 200 with `last_error` in the status (never raises); CLI prints a warning |
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
| `schedule create` / `delete` on a foreign (bundle) install | 403 (publisher-managed definitions) |
| `schedule update` setting any field other than `enabled` on a foreign install | 403 (only enable/disable allowed) |
| `schedule create` with `script_trigger` but no command | 400 |
| `schedule create` static_prompt cadence under the 10-minute floor | 400 (frequency too high) |
| `schedule` / `status` verb on a ghost or another user's agent | 404 (no existence leak) |
| `schedule create|update|delete|run` / `status set-command` by a demoted (agent-user) account token | 403 (`require_developer`, re-checked per call) |
| `status refresh` when the env is suspended / down / has no STATUS.md | 200 — cached snapshot returned (never raises); `refresh_command_warning` carries any pre-command failure |
| `status set-command` with an empty string | 200 — deliberate opt-out (no pre-command runs) |

## Roadmap Note

This document covers **Phases 1 through 5** — all phases are now shipped:

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
  wiring, and team-graph registration via `cinna api`;
  `context/guides/authoring-agent-prompts.md` guide ships the bulk-prompt
  authoring workflow and finalize-the-description rule (flow 7b — no new backend
  endpoints or migration).
- **Phase 5** — Dedicated schedule + status verbs (flows 7c / 7d): full schedule
  CRUD (`cinna agent schedule list|generate|create|update|run|logs|delete`) and
  status management (`cinna agent status show|refresh|set-command`) reached
  through new thin `/account/agents/{id}/schedules*` and
  `/account/agents/{id}/status*` endpoints that delegate to the existing
  `AgentSchedulerService` / `AgentStatusService` / `AgentService` (so behaviour,
  the per-type frequency floor, and the foreign-install read-only contract match
  the UI). Five new `CLI_ACCOUNT_SCHEDULE_*` / `CLI_ACCOUNT_STATUS_COMMAND_SET`
  security events; no new models persisted, no migration. Also ships `cinna chat`
  (flow 7e): a dedicated `POST /api/v1/cli/account/files/upload` multipart route
  lets `cinna chat --file` upload a local file and get back a file id; all session
  control (create / send / poll) rides the existing api-proxy; no new models,
  migration, or security events.

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
- **platform knowledge snapshot** — the `platform-knowledge-env` template snapshot is the
  source for the context package's platform docs and API reference. The
  generation logic is factored into `platform_knowledge_assets.py`, shared between
  this endpoint and the platform knowledge sync script
  (`.cinna-core-kit/scripts/sync_platform_knowledge.py`).
- **agent_api** (Phase 3) — `POST /account/connect/agent-api` wraps
  `AgentApiTokenService.connect_agent_api`; the resulting `AGENT_API` credential
  rides the existing credential sync / whitelist / redaction pipeline unchanged.
  The `cinna agent-api enable|refresh|spec` verbs (flow 4c) wrap
  `AgentService.update_agent` (`agent_api_enabled`) and `AgentApiService`
  (`get_spec` / `load_policy`) — the producer-side build+verify half that
  precedes connect, mirroring the Integrations → Agent REST API card (enable /
  Refresh / View Spec). See [agent_api.md](../../agents/agent_api/agent_api.md)
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
- **agent_schedulers** (Phase 5 / flow 7c) — the schedule verbs wrap
  `AgentSchedulerService` (generate / create / get / update / delete / execute_now
  / logs) behind `/account/agents/{id}/schedules*`. The per-type frequency floor,
  local→UTC cron conversion, and foreign-install read-only contract (consumers
  may toggle / run / view logs only) are reused verbatim from the UI route. See
  [agent_schedulers.md](../../agents/agent_schedulers/agent_schedulers.md)
- **agent_status_tracking** (Phase 5 / flow 7d) — `GET …/status` wraps
  `AgentStatusService.force_refresh_status` / `get_cached_status` and also returns
  the agent's `status_refresh_command`; `POST …/status/refresh-command` flips that
  field through `AgentService.update_agent` (the same `PATCH /agents/{id}` path the
  Integrations → Agent status card uses). See
  [agent_status_tracking.md](../../agents/agent_status_tracking/agent_status_tracking.md)
- **agent_prompts** (Phase 4 / flow 7b) — the bulk-prompt authoring workflow
  (`cinna api PUT agents/{id}`) targets the standard `PUT /agents/{id}` route,
  which is not on the escape-hatch denylist. `POST /agents/{id}/sync-prompts`
  (force-push DB→env) and `POST /agents/{id}/generate-router-trigger-prompt`
  (AI-generate routing sentence) are also reachable via `cinna api`. The
  three-way reconcile and SEED_PUSH mechanics are described in
  [agent_prompts.md](../../agents/agent_prompts/agent_prompts.md)
- **file_uploads** (Phase 5 / flow 7e) — `POST /account/files/upload` delegates
  to `FileService.create_file_upload`, the same service behind the normal
  `POST /files/upload` route; inherits the size cap, MIME-type whitelist, and
  quota enforcement. File ids returned by the upload are threaded into session
  messages via the api-proxy. `GET files/{id}/download` is also proxied (binary
  1:1 mirroring, 8 MiB cap). No dedicated integration doc; the file-upload feature
  is documented in the files feature area.
- **agent_improvement_requests** (flow 7f) — the four `/account/improvement-requests*`
  routes delegate straight into `ImprovementRequestService`, the same service the
  web `/improvement-requests*` routes use, so ownership rules (recipient-or-requester
  read, recipient-only mutate, 404-not-403 for inaccessible ids) cannot drift
  between the two transports. The archive route is dedicated (not `api-proxy`)
  because the escape hatch is JSON-only and cannot carry a binary body — the same
  reason `/account/files/upload` is a dedicated route. See
  [agent_improvement_requests.md](../agent_improvement_requests/agent_improvement_requests.md)
- **local_agent_kit** — the context package's 4th source, `context/local-kit/`,
  is the rendered [Local Agent Kit](../local_agent_kit/local_agent_kit.md),
  shared via `LocalAgentKitService.get_rendered_tree()` and the same
  `snapshot_cache_key` helper this endpoint uses for its other three sources.
  `cinna agent import` — the go-cloud counterpart to this workspace, invoked
  from `Cloud/` against an agent scaffolded by `kit.py new` — lives in the
  separate `cinna-cli` repository and reuses `run_agent_create`,
  `run_agent_sync`, and the credential-drafting / schedule / status verbs
  documented above (flows 5, 7c, 7d); no dedicated backend endpoint exists for
  it. See [local_agent_kit.md](../local_agent_kit/local_agent_kit.md)
