# Account CLI Workspace — Phase 3 Implementation Plan (Convenience Verbs + Generic API Escape Hatch)

> Builds on the shipped Phases 1–2 (see `account_cli_workspace_plan.md`,
> `docs/application/cinna_cli_integration/account_cli_workspace.md` +
> `_tech.md`). Phase 3 adds three CLI capabilities so the local coding agent can
> drive the agent network end-to-end without the UI:
>
> 1. `cinna agent create` — thin client; backend applies all defaults via the
>    normal agent-create path (Resolved Decision 3).
> 2. `cinna connect agent-api|mcp` — wrap the existing one-click connect helpers.
> 3. `cinna api <METHOD> <path> [...]` — an authenticated escape hatch into (most
>    of) the generated API reference, with capability exclusions (Resolved
>    Decision 6) enforced **server-side** in a single chokepoint.
>
> All three are reached with the **account CLI token** (`token_type =
> "cli-account"`). The structural guarantee from Phase 1 — account tokens are
> physically rejected outside `/api/v1/cli/account/*` via `AccountCLIContextDep`
> and the `_resolve_cli_context` type guard — must be preserved. The escape hatch
> is the only verb that needs to reach the rest of the API, and the central
> design decision below explains how it does so *without* dismantling that guard.

---

## Overview

Phases 1–2 gave the account workspace an auth spine (mint/discover, cascade
revoke) and a static context package. Phase 3 makes the workspace **productive**:
the orchestrator agent can stand up a new agent, wire two agents together, and —
for anything not yet wrapped in a dedicated verb — call the platform API directly.

The convenience verbs (1) and (2) get **explicit `/account/*` endpoints** that
delegate to the already-shipped agent-create and connect-helper services. They do
**not** route through the generic escape hatch (they predate it, are simpler to
gate precisely, and shouldn't wait on it). The escape hatch (3) is the genuinely
new mechanism and the bulk of this plan.

### What's in scope

| Item | Mechanism |
|------|-----------|
| `cinna agent create` | New `POST /api/v1/cli/account/agents` → `AgentService.create_agent` (thin client) |
| `cinna connect agent-api` | New `POST /api/v1/cli/account/connect/agent-api` → `AgentApiTokenService.connect_agent_api` |
| `cinna connect mcp` | New `POST /api/v1/cli/account/connect/mcp` → `MCPProviderService.connect_to_agent` |
| `cinna api METHOD path` | New `POST /api/v1/cli/account/api-proxy` → in-process ASGI re-dispatch as the owning user, behind a single exclusion chokepoint |

### What's NOT in scope (deferred / verified-none)

- No new persisted data model (verified below — Phase 3 is pure service/route).
- No agentic-team registration (that is Phase 4).
- No frontend beyond the mandatory client regen (verified below).

---

## The Central Design Problem — How the Escape Hatch Reaches the Rest of the API

Phase 1 made the account token **structurally** unable to authenticate anything
but `/account/*`: it only resolves through `AccountCLIContextDep`, and
`_resolve_cli_context` explicitly rejects `token_type == "cli-account"`. The
generic `cinna api` verb needs the account token's *holder* to call (most of) the
rest of the API. We must let it through **without** weakening that structural
guarantee for every other route.

Three options were evaluated.

### Option (a) — Server-side proxy endpoint (CHOSEN)

A single `/account/*` endpoint, `POST /api/v1/cli/account/api-proxy`, takes
`{method, path, query, body, headers?}`, runs the **exclusion chokepoint** on the
target path+method, and then **re-dispatches the request internally against the
same ASGI app** as the owning user, returning the inner response's status / body /
selected headers verbatim.

The account token never leaves `AccountCLIContextDep`. The inner call is made with
a **freshly-minted, short-lived, request-scoped normal user JWT** for
`account_ctx.user` (8-second TTL, single use within the proxy call), so downstream
routes see an ordinary authenticated user via the unchanged `get_current_user`.
The escape hatch is therefore *exactly* as capable as the user is in the UI —
minus the exclusion denylist, which is checked **before** the inner dispatch.

**Why a request-scoped user JWT and not the account token for the inner call?**
Because every downstream route depends on `get_current_user` (a real `User`), not
on a CLI context. Re-dispatching with a normal user JWT means **zero changes to
any downstream route or dependency** — they authenticate the user the way they
always have. The account token's structural isolation is untouched: it still only
authenticates the outer `/account/api-proxy` route.

**Internal re-dispatch realities (decided):**

- **Transport:** `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`
  against the in-process FastAPI app (`app.main.app`), base URL
  `http://internal`. This exercises the *real* middleware + dependency stack
  (auth, validation, response models) — no logic is bypassed or re-implemented.
  The app object is imported lazily inside the service to avoid an import cycle.
- **Path normalization:** the caller supplies a path **relative to the API root**
  (e.g. `agents`, `/agents`, `agents/{id}/credentials`). The service normalizes to
  a single leading slash and prefixes `settings.API_V1_STR` (`/api/v1`). The
  exclusion check runs on the **normalized, post-prefix** path so a caller can't
  smuggle in `/api/v1/credentials/{id}/reveal` by sending `..` segments — the
  service rejects any path containing `..` or not matching `^/api/v1/[A-Za-z0-9...]`
  outright (400).
- **Status / headers / body stream back:** status code mirrored 1:1. Response
  body returned as-is (bytes). A **small allowlist of response headers** is
  forwarded (`content-type`, `content-disposition`); hop-by-hop and auth-bearing
  headers are dropped. For Phase 3 the proxy returns a **buffered**
  `Response(content=..., status_code=..., media_type=...)`, not a stream — the
  escape hatch targets JSON control-plane calls, not SSE/streaming/binary
  downloads (those are out of scope and **denied** by method/route, see below).
- **Size limits:** request body capped at `ACCOUNT_API_PROXY_MAX_BODY_BYTES`
  (config, default 1 MiB) — rejected `413` before dispatch. Response body capped
  at `ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES` (config, default 8 MiB); a larger
  response is truncated→`502` with a clear detail (the escape hatch is not a file
  transfer channel).
- **Multipart / binary routes:** **not supported** in Phase 3. The proxy accepts
  only JSON request bodies (`Content-Type: application/json`) and forwards only
  `content-type`/`content-disposition` on the way back. SSE/streaming endpoints
  (`text/event-stream`) and websocket routes are unreachable (websockets aren't
  HTTP; streaming GET/POST like `exec`/`create-flow-stream`/`api-proxy`-of-stream
  are denied by the exclusion list). This keeps the chokepoint simple and the
  blast radius small; multipart upload via the escape hatch is explicitly a future
  item.

**Cost:** one extra in-process HTTP round-trip per escape-hatch call. Negligible
for a low-frequency developer tool, and it buys us a **single, total** reuse of
the real route stack with **no per-route changes**.

### Option (b) — Route-allowlist widening (REJECTED)

Add a dual-auth dependency that accepts account tokens on an explicit allowlist of
existing routes. Rejected because: (i) it dismantles the Phase 1 structural
guarantee route-by-route — every widened route now has to reason about two token
types and two identity shapes; (ii) "most of the API" is a large, growing
allowlist that must be hand-maintained as routes are added (the opposite of a
single chokepoint); (iii) downstream routes expect `CurrentUser`, not a CLI
context, so each would need adaptation. High surface area, high regression risk,
exactly the "dismantle the guarantee" outcome the prompt warns against.

### Option (c) — Short-lived restricted user JWT with a global guard (REJECTED as the primary, but its JWT-minting idea is reused inside (a))

Mint a short-lived user JWT carrying `restricted: "cli-account"` and add a
**global middleware/guard** that, for any request bearing that claim, checks the
target path against an exclusion denylist before the route executes. Rejected as
the *primary* mechanism because: (i) the claim escapes the account workspace — a
restricted JWT is a bearer token the CLI now stores/handles, expanding the
credential surface (we'd be issuing user-identity tokens to the machine, which is
precisely what Phase 1 avoided by keeping the account token mint-only); (ii) a
global guard that inspects every request for a special claim is a cross-cutting
behavior change touching the hot path of *all* traffic; (iii) enforcement is split
between the guard and whatever issues/refreshes the restricted JWT, violating the
"single chokepoint" requirement.

**What we keep from (c):** the *idea* of a short-lived user JWT for the inner
identity — but it is minted **inside** the `api-proxy` service, used **once** for
the single internal re-dispatch, and **never leaves the backend**. The CLI only
ever holds the account token. This gives (a) its "downstream sees a normal user"
property without (c)'s leaked-credential and global-guard downsides.

### Decision

**Option (a)** — server-side `api-proxy` endpoint with in-process ASGI
re-dispatch under a request-scoped, backend-only user JWT, gated by a single
exclusion chokepoint. It preserves the Phase 1 structural guarantee verbatim,
requires zero downstream route changes, and concentrates all policy in one tested
function.

---

## The Single Exclusion Chokepoint

```python
# backend/app/services/cli/account_api_proxy_policy.py

class ApiProxyDenied(Exception):
    """Raised when a target (method, path) is excluded from the escape hatch."""
    def __init__(self, reason: str, message: str):
        self.reason = reason          # "excluded_path" | "excluded_method" | "malformed_path"
        self.message = message

def assert_api_proxy_allowed(method: str, normalized_path: str) -> None:
    """
    The ONE place that decides whether the account-CLI escape hatch may reach a
    target route. Raises ``ApiProxyDenied`` otherwise. Has its own unit tests.

    ``normalized_path`` is already prefixed with ``settings.API_V1_STR`` and has a
    single leading slash, no ``..`` segments (the caller guarantees this; we
    re-assert defensively).
    """
```

### Policy: **denylist** (default-allow), justified

The escape hatch's whole purpose is "call anything in the generated API reference
the local agent might need." A denylist matches that intent: allow the broad
control plane, **subtract** the sensitive surfaces. An allowlist would have to be
hand-curated against the entire (growing) reference and would defeat the verb's
reason to exist. The denylist is small, explicit, and lives in one file with one
test per excluded prefix.

> **Defense in depth, not a substitute.** The denylist is the *escape hatch's*
> policy. It does **not** replace the per-route authorization those routes already
> enforce (ownership checks, `require_developer`, superuser guards). Because the
> inner call runs as the *real user* with a normal JWT, every downstream
> authorization check still applies. The denylist exists to remove
> **categories** the account token must never touch even if the user *could* touch
> them in the UI (Resolved Decision 6: no credential value reads, no user
> management, no admin surfaces) — i.e. it is strictly *more* restrictive than the
> user's own rights, never less.

### Excluded path prefixes / operations (denylist, all under `/api/v1`)

Method-agnostic unless noted. Matching is by **path-segment prefix** on the
normalized path.

| Excluded prefix / pattern | Why |
|---|---|
| `…/credentials/*` (all), `…/ai-credentials/*`, `…/oauth-credentials/*`, `…/credential-shares/*` | **Credential values.** Decision 6 forbids credential reads. The connect helpers create credentials but never *read values*; deny the whole credentials surface from the generic hatch. (Connect verbs use their dedicated `/account/connect/*` endpoints, not this proxy.) |
| `…/users/*` **except** `GET …/users/me` and `GET …/users/search` | **User management.** No create/update/delete/list of users; allow only the caller's own profile read and the existing minimal user-search projection (needed to wire shares). |
| `…/admin*`, `…/admin-environments/*`, `private/*` | **Admin surfaces.** Decision 6. |
| `…/cli/*` (the entire CLI router, incl. `/cli/account/*`) | No recursion / no token self-management via the hatch (an account token must not mint per-agent setup tokens, list/revoke account tokens, or **call api-proxy again**). Closes the recursion + privilege-self-escalation hole. |
| `…/desktop-auth/*`, `…/app-auth/*`, `…/app-sync/*` | **Other clients' auth + zero-knowledge sync store.** Not the account workspace's business; sensitive. |
| `…/mfa/*` | **2FA management.** Never reachable from a machine credential. |
| `…/security-events/*` | Audit log read — already excluded from the API reference (`SKIP_TAGS`); deny for consistency. |
| `…/login/*`, `…/oauth/*`, `…/auth/*`, `…/token` | **Auth/session issuance.** Already `SKIP_TAGS`; deny defensively. |
| **Streaming/SSE & exec operations:** `POST …/cli/agents/{id}/exec`, `POST …/agents/create-flow-stream`, `…/agents/create-flow`, any `…/sync-stream`, `…/console` ws | Streaming/binary/long-poll responses the buffered proxy can't represent; and `exec` is a per-agent CLI capability that belongs to child tokens, not the account hatch. (Most are already unreachable as non-`/account` CLI routes; listed for completeness.) |
| Non-`application/json` request bodies; any path with `..`; any path not matching `^/api/v1/…` | **Malformed / unsupported** → `malformed_path`. |

**Method note:** the denylist is path-first. We additionally **deny websocket
upgrade** (the proxy is HTTP-only) and deny any response whose `Content-Type` is
`text/event-stream` (caught post-dispatch → `502`, "streaming responses are not
supported via the escape hatch").

> The exact prefix list lives as module constants
> (`EXCLUDED_PREFIXES`, `USER_PATH_ALLOW_EXACT`, `STREAMING_DENY`) so the test
> file asserts each one. Adding a sensitive surface later = one line + one test.

### Where it's enforced — single chokepoint

`assert_api_proxy_allowed(method, normalized_path)` is called **once**, in
`AccountApiProxyService.proxy(...)`, **before** the inner ASGI dispatch. No other
call site. This mirrors the established egress-guard pattern (`assert_url_allowed`
is the single chokepoint for outbound MCP/OAuth calls). The route layer
(`/account/api-proxy`) does no policy itself — it only marshals the request and
maps `ApiProxyDenied` → HTTP.

### How it fails — **403 with explicit detail** (not 404)

The escape hatch caller is an *authenticated* account-token holder who is being
told a *category* is off-limits. There is no existence-leak concern here (the
excluded surfaces are well-known platform features, not user-scoped resources), so
**403** with a precise detail is correct and more useful than a misleading 404:

- `excluded_path` → `403 {"detail": "The escape hatch may not call '<path>'
  (credential/user-management/admin surfaces are excluded). Use a dedicated
  command if available."}`
- `excluded_method` / streaming → `403` with the streaming/method explanation.
- `malformed_path` → `400`.

(Contrast: the *mint* endpoint uses 404 for inaccessible agents to avoid leaking
agent existence. That existence-leak rule is about user-scoped resource IDs; the
denylist is about platform-wide capability categories, so 403 is the right signal.)

### Audit (SecurityEvent)

One new constant in `backend/app/models/events/security_event.py`:

- `CLI_ACCOUNT_API_PROXY_CALL = "CLI_ACCOUNT_API_PROXY_CALL"`

Emission policy (kept cheap — this is a high-ish-frequency developer tool):

- **On every exclusion hit** (`ApiProxyDenied` with reason `excluded_*`):
  `severity="medium"`, `agent_id=None`, `details={method, path, reason,
  account_token_id, ip}`. These are the security-interesting events (someone/
  something is probing an off-limits surface).
- **On allowed calls:** **do not** write a SecurityEvent per call (would flood the
  log). Instead the proxy logs an `info`-level structured log line
  (`method path → status`) for debuggability. *Rationale:* the inner call already
  runs as the real user and any sensitive *write* it performs is audited by that
  downstream route's own mechanisms; duplicating per-call audit here adds noise
  without new security signal.

The convenience verbs (create/connect) **do** each write a SecurityEvent (see
their sections) because they are discrete, infrequent, state-changing grants.

### Rate limiting

Per-account-token in-process token-bucket on the proxy:
`ACCOUNT_API_PROXY_RATE_LIMIT` (config, default `120/min` per account token).
Exceeded → `429 + Retry-After`. Implemented as a small in-memory throttle keyed by
`account_token.id` (mirrors the in-memory dedup/throttle pattern already used by
event handlers and MCP rate limiting). This is a backstop against a runaway local
agent loop, not a billing control. The convenience verbs are not rate-limited
(they're low-frequency and already gated by `can_build`/ownership).

---

## Convenience Verbs (explicit `/account/*` endpoints — do NOT use the proxy)

These three endpoints delegate to **already-shipped** services. They each
authenticate via `AccountCLIContextDep` (account token), resolve the owning user
from the context, and pass `is_superuser=account_ctx.user.is_superuser` through to
the underlying service so superuser semantics are preserved.

### 1. `POST /api/v1/cli/account/agents` — create agent (thin client)

**Request** (`AccountAgentCreateBody`, new minimal model):
```json
{ "name": "CRM Agent", "description": "optional", "env_name": "optional template" }
```
- `name` required (min 1). `description` optional. `env_name` optional (template
  name; when omitted the service falls back to `settings.DEFAULT_AGENT_ENV_NAME`).
- **Thin client (Decision 3):** the CLI sends only these user-specified fields.
  The backend applies ALL defaults via the **normal** agent-create path exactly as
  the UI does — default AI-credential resolution, default env template,
  environment creation. We call the same `AgentService.create_agent(session,
  user_id, AgentCreate(...), user)` that `POST /api/v1/agents/` uses. (We map the
  minimal body → `AgentCreate(name=..., description=..., user_workspace_id=None)`.
  `env_name`, if we want it honored at create time, is threaded through the same
  way the UI's create-flow threads it — confirm `create_agent` accepts an env
  template arg; if it only accepts `AgentCreate`, env template selection stays at
  its server default and `env_name` is accepted-but-noop in v1 with a documented
  follow-up. **Open question O1.**)

**Gating:** `RoleService.require_developer(account_ctx.user)` → 403 for
`agent-user`. This mirrors the UI route `POST /api/v1/agents/` which carries
`dependencies=[Depends(require_developer)]`. Create requires developer role
(Resolved Decision context; consistent with the UI).

**Response:** the full `AgentPublic` record (same `response_model=AgentPublic` as
the UI route), so the CLI gets the complete created agent (id, env id, etc.).

**Audit:** `SecurityEventService` — reuse the existing agent-creation audit if one
exists; otherwise no *new* constant is required (agent creation is already a
first-class audited action via the normal path). **No** account-specific event is
added for create (it's a normal create that happens to originate from the CLI; the
normal path's audit covers it). *Confirm whether `create_agent` emits an audit
event today;* if not, that's a pre-existing gap, not Phase 3's to fix.

### 2. `POST /api/v1/cli/account/connect/agent-api` — wrap agent_api connect

**Request** (`AccountConnectAgentApiBody`):
```json
{ "producer_agent_id": "<uuid>", "consumer_agent_id": "<uuid>",
  "credential_label": "optional", "read_only_override": false }
```
Maps directly to `ConnectAgentApiRequest`. `producer_agent_id` is a path-free body
field (CLI passes `--producer X --consumer Y`).

**Delegates to:** `AgentApiTokenService.connect_agent_api(session,
producer_agent_id, account_ctx.user.id, ConnectAgentApiRequest(...),
is_superuser=...)`.

**Gating — investigated, and matched to the UI helper:**
- The existing UI helper requires the caller to **own the producer agent** (mint
  requires producer-side authority) — `_verify_agent_ownership` raises 403
  otherwise — and, if a consumer is given, to **own the consumer agent** (403
  otherwise). It is **not** `require_developer`-gated at the route today; it is
  ownership-gated in the service.
- **Decision for the CLI wrapper (justified):** keep parity with the UI — gate on
  the **same ownership checks the service already enforces** (producer ownership +
  consumer ownership), and **additionally** require `require_developer` on the
  account-CLI route. Rationale: (i) the account workspace is a developer tool and
  the account setup token already requires developer (Decision 6), so a
  non-developer can't even reach here; (ii) producer-side `can_build` is *stronger*
  than bare ownership and aligns with Phase 1's "building rights, not bare
  ownership" principle — **but** the producer in a connect is not being *built*,
  it's being *consumed-from*, and the UI deliberately uses ownership (you can mint
  a consumption token for an agent you own even if it's a foreign install you
  can't *build*). So we do **not** impose `can_build` on the producer; we mirror
  the UI's ownership gate exactly, plus the developer-role gate the account context
  already implies. **What's required on the producer side: ownership (consistent
  with the UI), not `can_build`.** The service's existing 403/404 mapping is
  reused verbatim.

**Response:** `ConnectAgentApiResponse` (credential_id, token_id, token_prefix,
base_url, spec_url, linked_consumer_agent_id) — returned as-is.

**Audit:** new constant `CLI_ACCOUNT_CONNECT_AGENT_API`
(`severity="medium"`, `agent_id=consumer_agent_id`, `details={producer_agent_id,
credential_id, token_prefix, ip}`). Written by the account-CLI route/service
wrapper (the underlying connect helper's own audit, if any, is unchanged).

### 3. `POST /api/v1/cli/account/connect/mcp` — wrap mcp_provider connect

**Request** (`AccountConnectMcpBody`):
```json
{ "connector_id": "<uuid>", "consumer_agent_id": "<uuid>",
  "mcp_mode_conversation": true, "mcp_mode_building": true, "label": "optional" }
```
Maps to `ConnectMcpProviderAgentRequest`.

> **Producer identity note (CLI ergonomics).** The MCP connect helper takes a
> `connector_id`, not a producer agent id. `cinna connect mcp --producer X` gives
> an *agent*; the CLI must resolve the agent → its agent2agent connector id. The
> backend supports this discovery via `GET
> /api/v1/mcp-providers/discoverable-agents` (returns `{agent_id, connector_id,
> …}`), but that route is **not** account-token-reachable. **Add a thin
> account-CLI passthrough:** `GET /api/v1/cli/account/connect/mcp/discoverable`
> (account token) → `MCPProviderService.list_discoverable_agents(session,
> account_ctx.user, consumer_agent_id=...)`, so the CLI can map
> `--producer <agent>` → `connector_id` before calling connect. (Alternatively the
> connect body accepts `producer_agent_id` and the service resolves the connector;
> **Open question O2** — prefer the discoverable passthrough to avoid changing the
> shared connect service signature.)

**Delegates to:** `MCPProviderService.connect_to_agent(session, account_ctx.user,
ConnectMcpProviderAgentRequest(...), is_superuser=...)`.

**Gating — investigated, matched to the UI helper:**
- The UI helper ACL-checks the caller on the **producer connector** (owner / in
  the connector's `allowed_user_ids` ACL / superuser → else 403; missing/non-a2a
  connector → 404 no-leak) and, if a consumer is given, requires **consumer
  ownership** (403 otherwise). Consuming an MCP connection is "use-only, available
  to every role" per the mcp_providers route docstring — i.e. **not**
  developer-gated in the UI.
- **Decision for the CLI wrapper:** because the account workspace itself is
  developer-gated (you need developer to even get an account token), the practical
  caller is always a developer. We **mirror the UI's gates exactly** (producer
  connector ACL + consumer ownership) and add the route-level `require_developer`
  that the account context already implies. **What's required on the producer
  side: connector ACL membership (owner/allowed-user/superuser), consistent with
  the UI — not `can_build`.** Reuse the service's 403/404 mapping verbatim.

**Response:** `MCPProviderConnectionResponse` returned as-is.

**Audit:** new constant `CLI_ACCOUNT_CONNECT_MCP` (`severity="medium"`,
`agent_id=consumer_agent_id`, `details={connector_id, credential_id, ip}`).

---

## Data Models

**No new persisted tables or columns.** Verified: all Phase-3 capabilities reuse
existing tables (agents, credentials, agent_api_token, mcp_connector/mcp_token).
The escape hatch persists nothing; convenience verbs persist via existing services.

New **Pydantic request/response schemas** only (no `table=True`):

- `backend/app/models/cli/account_convenience.py` (new):
  - `AccountAgentCreateBody` `{name, description?, env_name?}`
  - `AccountConnectAgentApiBody` `{producer_agent_id, consumer_agent_id?,
    credential_label?, read_only_override=false}`
  - `AccountConnectMcpBody` `{connector_id, consumer_agent_id?,
    mcp_mode_conversation=true, mcp_mode_building=true, label?}`
  - `AccountApiProxyRequest` `{method, path, query?: dict[str,str|list[str]],
    json_body?: Any, headers?: dict[str,str]}` — `method ∈ {GET,POST,PUT,PATCH,
    DELETE}` (validated); `headers` accepted but **ignored** in v1 except a future
    allowlist (Open question O3 — default ignore for safety).
  - `AccountApiProxyResponse` — **not** a model; the route returns a raw
    `fastapi.Response` so status/body pass through. (Documented in the route.)
- Reuse existing `AgentPublic`, `ConnectAgentApiResponse`,
  `MCPProviderConnectionResponse`, `DiscoverableAgents`.

New **SecurityEvent constants** (`security_event.py`):
- `CLI_ACCOUNT_CONNECT_AGENT_API`
- `CLI_ACCOUNT_CONNECT_MCP`
- `CLI_ACCOUNT_API_PROXY_CALL` (exclusion hits only)

---

## Backend Implementation

### Service layer

**`backend/app/services/cli/account_api_proxy_policy.py` (new)** — the chokepoint
(`assert_api_proxy_allowed`, `ApiProxyDenied`, the denylist constants). Pure,
dependency-free, unit-tested in isolation.

**`backend/app/services/cli/account_api_proxy_service.py` (new)** —
`AccountApiProxyService`:
- `async proxy(db, account_token, user, req: AccountApiProxyRequest, request:
  Request) -> Response`:
  1. Rate-limit check (per `account_token.id`) → `429`.
  2. Normalize + validate path; reject `..`/non-`/api/v1` (→ `ApiProxyDenied
     malformed_path`).
  3. `assert_api_proxy_allowed(req.method, normalized_path)` (chokepoint).
  4. Enforce request `json_body` size → `413`.
  5. Mint a request-scoped user JWT for `user` (8s TTL, via
     `create_access_token(user.id, expires_delta=timedelta(seconds=8))` — no
     special claim needed; it's an ordinary user JWT).
  6. `httpx.ASGITransport` dispatch against `app.main.app`:
     `await client.request(method, normalized_path, params=query,
     json=json_body, headers={"Authorization": f"Bearer {jwt}"})`.
  7. Enforce response size → `502` if over cap; reject `text/event-stream` → `502`.
  8. Return `Response(content=resp.content, status_code=resp.status_code,
     media_type=resp.headers.get("content-type"))` with the forwarded header
     allowlist.
- On `ApiProxyDenied`: write `CLI_ACCOUNT_API_PROXY_CALL` SecurityEvent for
  `excluded_*` reasons, then re-raise for the route to map to 403/400.

**Reused as-is:** `AgentService.create_agent`, `AgentApiTokenService.connect_agent_api`,
`MCPProviderService.connect_to_agent` / `list_discoverable_agents`,
`RoleService.require_developer`, `SecurityEventService.create_event`,
`create_access_token` (already supports the needed TTL).

> **Optional thin orchestration on `AccountCLIService`:** add
> `create_agent(db, user, body)`, `connect_agent_api(db, user, body)`,
> `connect_mcp(db, user, body)` static methods that wrap the underlying services +
> emit the convenience-verb SecurityEvents, keeping the route thin and the audit in
> one place. The proxy stays in its own service (it's substantial).

### Dependencies

**No new dep.** All Phase-3 routes use the existing `AccountCLIContextDep`. The
escape hatch's inner identity is a JWT minted inside the service, not a dependency.
`_resolve_cli_context` and `_resolve_account_cli_context` are **unchanged** — the
Phase 1 guard stands.

### API routes (all appended to `backend/app/api/routes/cli.py`, `router` group)

| Method | Path | Auth | Body | Returns | Codes |
|---|---|---|---|---|---|
| POST | `/api/v1/cli/account/agents` | `AccountCLIContextDep` + `require_developer` | `AccountAgentCreateBody` | `AgentPublic` | 200 / 403 |
| POST | `/api/v1/cli/account/connect/agent-api` | `AccountCLIContextDep` + `require_developer` | `AccountConnectAgentApiBody` | `ConnectAgentApiResponse` | 200 / 400 / 403 / 404 |
| GET | `/api/v1/cli/account/connect/mcp/discoverable` | `AccountCLIContextDep` | `?consumer_agent_id=` | `DiscoverableAgents` | 200 |
| POST | `/api/v1/cli/account/connect/mcp` | `AccountCLIContextDep` + `require_developer` | `AccountConnectMcpBody` | `MCPProviderConnectionResponse` | 200 / 400 / 403 / 404 |
| POST | `/api/v1/cli/account/api-proxy` | `AccountCLIContextDep` | `AccountApiProxyRequest` | raw passthrough | 200 / 4xx-5xx (inner) / 403 (denied) / 413 / 429 / 502 |

Route layer is thin: marshal body → service; map `CanBuildError`/
`AgentApiTokenError`/`MCPProviderError` via the existing handlers (`_raise_can_build_http`,
`_handle_token_error`, `_handle_error`); map `ApiProxyDenied` → 403/400;
`require_developer` via the existing `RoleService.require_developer` try/except
pattern already used by `create_account_setup_token`.

> **Router ordering caveat:** `/account/connect/mcp/discoverable` (GET) and
> `/account/connect/mcp` (POST) don't collide (different methods), and neither
> collides with `/account/agents/{agent_id}/mint`. The new `/account/agents` POST
> must be registered so it doesn't shadow `/account/agents` GET (different methods
> — fine). No path-param ambiguity introduced.

### Config additions (`core/config.py`)

- `ACCOUNT_API_PROXY_MAX_BODY_BYTES: int = 1_048_576`
- `ACCOUNT_API_PROXY_MAX_RESPONSE_BYTES: int = 8_388_608`
- `ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN: int = 120`

---

## Frontend Impact

**None beyond client regeneration.** Verified: Phase 3 adds no UI. The new routes
land in the generated client after `bash scripts/generate-client.sh` (or
`source ./backend/.venv/bin/activate && make gen-client`). The escape-hatch route
returns a raw passthrough (no `response_model`) so it appears in the client as an
untyped call — acceptable, since no frontend consumes it (only the CLI does, and
the CLI calls the raw HTTP API, not the TS client). Run client regen so the
`CliService` stays in sync and typecheck doesn't drift, but expect no `.tsx`
changes.

---

## Database Migrations

**None.** No schema changes (verified). Confirm `alembic heads` is single-headed
at implementation time per the repo's standing multi-head caution, but Phase 3
adds no revision.

---

## Error Handling & Edge Cases

| Scenario | Outcome |
|---|---|
| Escape hatch targets a credential/admin/users-mgmt/cli/mfa/desktop/app-sync path | `403 excluded_path` + SecurityEvent |
| Escape hatch targets `GET /users/me` or `GET /users/search` | Allowed (user-path exact allowlist) |
| Escape hatch path contains `..` or isn't under `/api/v1` | `400 malformed_path` |
| Escape hatch targets a streaming/SSE/exec/create-flow-stream route | `403` (denylist) or `502` if the inner response is `text/event-stream` |
| Request body > 1 MiB / response > 8 MiB | `413` / `502` |
| Inner route returns 4xx/5xx (e.g. 404 agent not found) | Mirrored verbatim to the CLI — the escape hatch is transparent for *allowed* paths |
| Rate limit exceeded for the account token | `429 + Retry-After` |
| Per-agent token used on any Phase-3 account route | `401` (unchanged `_resolve_account_cli_context` guard) |
| `agent-user` (somehow holding an account token) calls create/connect | `403` (`require_developer`) |
| `create agent` with default everything | Full `AgentPublic` with env created by the normal path |
| `connect agent-api` producer not owned | `403` (service ownership check) |
| `connect agent-api` producer `agent_api_enabled=false` | `400` (service) |
| `connect mcp` connector not in caller's ACL | `403`; missing/non-a2a connector | `404` (no-leak, service) |
| `connect` consumer agent not owned | `403` (service) |
| Account token revoked mid-session | `401` on next Phase-3 call (cascade-revoke + expiry guard, Phase 1) |

---

## Testing Approach (API-only; see `backend/tests/README.md`)

Read `backend/tests/README.md` first (no direct DB access, scenario-based,
API-only, reuse `backend/tests/utils/cli.py`'s account helpers from Phase 1). New
test file `backend/tests/api/cli/test_account_cli_phase3.py` (keep Phase 1/2
scenarios in `test_account_cli.py`).

**Unit test for the chokepoint** (`backend/tests/unit/` per the repo's unit
policy, or a focused module test) — `test_account_api_proxy_policy.py`:
- One assertion **per excluded prefix** (credentials, ai-credentials,
  oauth-credentials, credential-shares, users[create/update/delete/list], admin,
  admin-environments, private, cli, desktop-auth, app-auth, app-sync, mfa,
  security-events, login, oauth) → `ApiProxyDenied(excluded_path)`.
- `GET /users/me`, `GET /users/search` → **allowed**.
- `..` traversal, non-`/api/v1` path → `malformed_path`.
- Streaming/exec/create-flow-stream paths → denied.
- A representative **allowed** control-plane path (`GET /agents`,
  `GET /agents/{id}`, `GET /agentic-teams`) → allowed.

**API scenarios** (`test_account_cli_phase3.py`):
- **agent create** — developer account token creates an agent (200, full record,
  env present); `agent-user`-held token → 403; thin-client defaults applied.
- **connect agent-api** — owner connects producer→consumer (200); non-owned
  producer → 403; `agent_api_enabled=false` producer → 400; non-owned consumer →
  403; resulting credential exists and is linked; `CLI_ACCOUNT_CONNECT_AGENT_API`
  audited.
- **connect mcp** — discoverable passthrough returns the connector; connect via a
  connector in the caller's ACL (200); outside-ACL → 403; missing/non-a2a → 404;
  `CLI_ACCOUNT_CONNECT_MCP` audited.
- **escape hatch — allowed** — `POST /account/api-proxy {GET, agents}` returns the
  user's agent list (same bytes the direct `GET /api/v1/agents` returns for that
  user); `POST /account/api-proxy {GET, agents/{id}}` mirrors inner 404 for an
  inaccessible id (transparent passthrough).
- **escape hatch — excluded** — proxy to `credentials/{id}` → 403 `excluded_path`
  + `CLI_ACCOUNT_API_PROXY_CALL` written; proxy to `users` (list) → 403; proxy to
  `cli/account/api-proxy` (recursion) → 403; proxy to `admin*` → 403.
- **escape hatch — identity** — the inner call runs as the *account token's user*:
  a proxied `GET /users/me` returns that user; a second user's account token can't
  see the first user's resources through the hatch.
- **escape hatch — structural guarantee intact** — the account token is still
  rejected (`401`) by a direct call to any per-agent CLI route and any non-account
  route (regression: confirm Phase 1 guard unchanged).
- **escape hatch — limits** — oversized body → 413; rate-limit exceeded → 429;
  streaming target → 403/502.

**Regression** — run `backend/tests/api/cli/` to confirm no Phase 1/2 break, and a
smoke pass on `test_agents` / `test_agent_api` / mcp_provider tests to confirm the
wrappers don't perturb the underlying services.

CLI-side tests live in the cinna-cli repo against the documented contract below.

---

## cinna-cli companion work (separate repo: `/Users/evgenyl/dev/ml-llm/cinna-cli`)

> Self-contained contract. All Phase-3 verbs authenticate with the **account
> token** from `.cinna/account.json` (`Authorization: Bearer <account_token>`),
> resolved by walking up to the account workspace root (the `AccountConfig`
> machinery added in Phase 1).

### Command surface

| Command | Backend call | Behavior / output |
|---|---|---|
| `cinna agent create <name> [--description D] [--template T]` | `POST /api/v1/cli/account/agents` body `{name, description, env_name=T}` | Print the created agent's id, name, env id, and "synced? run `cinna agent sync <name>`". On 403 print "requires agent-developer role". |
| `cinna connect agent-api --producer P --consumer C [--label L] [--read-only]` | `POST /api/v1/cli/account/connect/agent-api` | Resolve `P`/`C` (name→id via cached `account agents`); body `{producer_agent_id, consumer_agent_id, credential_label, read_only_override}`. Print credential_id, token_prefix, base_url, spec_url. |
| `cinna connect mcp --producer P --consumer C [--label L] [--no-conversation] [--no-building]` | `GET …/account/connect/mcp/discoverable?consumer_agent_id=C` then `POST …/account/connect/mcp` | Resolve `P`→`connector_id` from the discoverable list; body `{connector_id, consumer_agent_id, mcp_mode_*}`. Print credential_id, endpoint_url, status. |
| `cinna api <METHOD> <path> [--json '<obj>' \| --data @file.json] [--query k=v ...]` | `POST /api/v1/cli/account/api-proxy` body `{method, path, json_body, query}` | The escape hatch. See below. |

### `cinna api` — request handling & output

- **Mapping:** `cinna api GET agents` → `POST /account/api-proxy
  {"method":"GET","path":"agents"}`. `path` is sent **relative to the API root**
  (no `/api/v1` prefix — the backend prepends it). The CLI passes it through
  verbatim; the backend normalizes.
- **Body:** `--json '<inline JSON>'` or `--data @file.json` populates `json_body`
  (object). Mutually exclusive. Only JSON is supported (matches backend v1).
- **Query:** repeatable `--query k=v` → `query` map (repeat key → list value).
- **Response handling:** the backend returns the **inner response verbatim**
  (status + body). The CLI:
  - prints the response body to stdout (pretty-prints if `Content-Type` is JSON),
  - sets exit code `0` for inner `2xx`, non-zero for inner `4xx/5xx` (so it
    composes in shell pipelines / the local agent can branch on it),
  - on the escape hatch's *own* errors (403 excluded / 400 malformed / 429 / 413 /
    502) prints the `detail` to stderr and exits non-zero with a distinct code so
    the agent can tell "the platform said no" from "the target route errored".
- **Discovery affordance:** `cinna api --help` points the agent at
  `context/api_reference/` (shipped by Phase 2) as the catalogue of callable
  endpoints, and notes the excluded categories (credentials/users-mgmt/admin/cli/
  mfa/auth) so the agent doesn't waste calls on them.

### CLI files to add/modify (orientation only)

- `src/cinna/main.py` — `agent create`; `connect` group (`agent-api`, `mcp`);
  top-level `api` verb.
- `src/cinna/client.py` — `create_agent`, `connect_agent_api`,
  `list_discoverable_mcp`, `connect_mcp`, `api_proxy` methods.
- name→id resolution reuses the cached `account agents` listing (Phase 1).

---

## Implementation Order (phased-within-phase)

1. **Chokepoint first (pure + tested).** `account_api_proxy_policy.py` +
   its unit test. Lock the denylist and its semantics before any wiring.
2. **Escape-hatch service + route.** `account_api_proxy_service.py`, the
   `/account/api-proxy` route, config knobs, the `CLI_ACCOUNT_API_PROXY_CALL`
   audit, rate limit. API scenario tests for allowed/excluded/identity/limits.
3. **Convenience verb: agent create.** Minimal model + route delegating to
   `AgentService.create_agent`; `require_developer`; tests. (Resolve O1 here.)
4. **Convenience verbs: connect.** agent-api + mcp routes + discoverable
   passthrough; reuse underlying services; convenience SecurityEvents; tests.
   (Resolve O2 here.)
5. **Client regen + docs.** `make gen-client`; update
   `account_cli_workspace.md` + `_tech.md` (lift the roadmap note to "Phase 3
   shipped"); confirm `alembic heads` single (no revision expected).
6. **cinna-cli companion** (separate repo) against the contract above.

---

## Open Questions (genuinely open)

- **O1 — `env_name` on CLI agent create.** Does `AgentService.create_agent`
  accept an environment-template argument, or is template selection only available
  through the streaming `create_agent_flow` path? If `create_agent` can't honor
  `env_name`, decide: (a) accept-but-noop `env_name` in v1 with the server default
  and a documented follow-up, or (b) route CLI create through the create-flow
  service (which *does* take `env_name`) and collapse its SSE into a single
  buffered response. Recommendation: (a) for a clean thin-client v1; (b) only if
  product wants template choice at create time immediately.
- **O2 — MCP connect producer addressing.** Confirm the chosen shape: dedicated
  `…/connect/mcp/discoverable` passthrough so the CLI maps `--producer <agent>` →
  `connector_id` client-side (preferred — no change to the shared connect service),
  vs. extending the connect body/service to accept `producer_agent_id` and resolve
  the connector server-side. Recommendation: the passthrough.
- **O3 — Escape-hatch request headers.** v1 ignores caller-supplied `headers`
  (only the minted user JWT is sent). Is there a near-term need to forward any
  request header (e.g. `Idempotency-Key`)? If so, define a tiny forward-allowlist;
  otherwise keep "ignore" as the safe default.
