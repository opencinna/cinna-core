# Agent REST API

## Purpose

Enable a producer agent to expose a **capability-narrowed REST API** built from plain decorated Python functions inside its container. The platform supervises the API process, harvests its OpenAPI spec, enforces declarative guardrails at the proxy edge, and lets other agents consume it as a standard `agent_api` credential — so a powerful upstream credential (an ERP key, a broad OAuth scope, a legacy API token) never leaves the producer container. Consumers call `GET /orders` the same way they would call any other endpoint; there is no LLM in the request path.

This is a **code-to-code** channel: deterministic, typed, and high-frequency. Contrast with A2A (intelligence-to-intelligence task delegation) and MCP (LLM-to-tool exposure). Those channels involve model inference on every call; this one never does.

---

## Core Concepts

### Producer / Consumer Model

An agent that holds a powerful credential authors an `agent_api/` directory in its workspace. The platform supervises a real FastAPI app built from those files and exposes it through a backend proxy. A second agent holds only an `agent_api` credential (`{base_url, token, spec_url}`) and calls the proxy — it never touches the upstream credential.

### Connection = Credential (no manual tokens)

There is **no manual token management**. A *connection* between two agents **is** the `agent_api` credential, and the proxy token is its internal secret. A connection is created in one action ("Connect Agent API") which mints the token and creates the credential together; the token is bound to that credential and is never shown or edited by the user. **Deleting the credential is the only way to revoke access** — it cascade-deletes the token (disconnect). This is why the producer's card and the credential's detail page both talk about *connections*, not tokens.

```
PRODUCER SIDE                                 CONSUMER SIDE
┌────────────────────────────┐               ┌─────────────────────────────┐
│ Agent A (has Odoo cred)     │               │ Agent B (no Odoo cred)       │
│ agent_api/orders.py          │               │ reads agent_api cred from    │
│   @api.get("/orders") ...    │               │   credentials.json           │
│ policy.yaml (read_only)      │               │ fetches {base_url}/openapi   │
└─────────────┬────────────────┘               │ calls {base_url}/orders      │
              │ env-core supervises             └─────────────┬────────────────┘
              │ uvicorn child (:9100)                         │ Bearer token
              ▼                                               ▼
   ┌──────────────────────────── Backend proxy ─────────────────────────────┐
   │ /api/v1/agents/{agent_id}/agent-api/*      (owner preview)              │
   │ /api/v1/agent-api/{agent_id}/*             (consumer routes)            │
   │   validate token → enforce policy.yaml → keep-alive → proxy to env-core │
   └─────────────────────────────────────────────────────────────────────────┘
```

### Feature Toggle

`agent_api_enabled` on the `Agent` row (default `false`) mirrors `webapp_enabled`. When disabled:
- Consumer routes return 404
- Connecting (the connect helper) is blocked (400)
- The `_status` endpoint still reports `disabled` (so the enable CTA can render)

### OpenAPI Spec is Always Accurate

The spec is **harvested from the live app** via an import-only subprocess — the platform imports the agent's modules and calls `app.openapi()` without starting the serving child. The result is cached on `AgentEnvironment`. Consumers, the credential "Test" button, and client generation all read from cache without cold-starting a suspended producer.

**Spec naming is author-controlled.** The OpenAPI `title` / `description` / `version` come from module-level constants the producer defines in any `agent_api` module (`API_TITLE`, `API_DESCRIPTION`, `API_VERSION`); the first non-empty value wins. When none are set, a generic default (`"Agent API"`) is used — the platform never names the API after the environment image (which is just an internal template name, not meaningful to consumers).

**Harvest resilience.** Discovery imports every `*.py` in `agent_api/` that does not start with `__`. A stray non-endpoint file (a helper, a smoke test, a Mutagen `*.sync-conflict-*.py` copy) that imports an endpoint module would otherwise re-register its routes twice and break the spec (operationId collision). The platform de-duplicates routes and isolates the harvest's output channel, so a stray re-import degrades to a logged warning instead of a boot error — a correct API still harvests cleanly.

---

## Three Security Layers

The proxy is a **confused deputy by design**: it calls a powerful upstream credential on behalf of whoever presents a valid token. Security comes from three independent, composition-of-guarantees layers:

### Layer 1: Containment

The upstream credential is synced only into the producer's container via the existing credential whitelist pipeline. It is never returned in any proxy response. The proxy is the only egress from the producer.

### Layer 2: Platform-Enforced `policy.yaml`

A `policy.yaml` file in the producer workspace defines **coarse** guardrails that the backend enforces **before the request reaches the agent's app**:

| Key | Default | Effect |
|-----|---------|--------|
| `read_only` | `true` | Non-`GET`/`HEAD` rejected with `405`. |
| `allowed_methods` | (derived from `read_only`) | Explicit method allowlist; overrides `read_only`. |
| `auth` | `required` | A valid `agent_api` token is mandatory. |
| `max_body_bytes` | `10 485 760` (10 MB) | Bodies larger than this are rejected `413` before buffering. |
| `rate_limit` | `60/min` per token | Exceeded requests return `429 + Retry-After`. |
| `expose_spec` | `true` | When `false`, `/openapi.json` passthrough returns `403`. |
| `allowed_paths` | `["*"]` | Optional path-prefix allowlist for extra narrowing. |

**`read_only` precision:** this flag enforces *no state-changing HTTP verb* — it rejects `POST`, `PUT`, `PATCH`, `DELETE`. It does **not** guarantee semantic read-only behaviour. A handler is free to mutate upstream state from inside a `GET`. Semantic safety is the producer's responsibility; `policy.yaml` guarantees only the method/body/rate envelope.

Policy parse errors fail **closed**: a `FAIL_CLOSED_POLICY` (deny-all) is applied and the error is surfaced to the owner. A missing `policy.yaml` applies the defaults (read-only by default).

### Layer 3: In-Code Shape Constraints

Typed function parameters, `Query(le=, ge=, regex=)` constraints, and Pydantic `Field` validators are enforced by FastAPI and reflected in the harvested OpenAPI spec. Producers use these to express which data shapes are permitted.

---

## Producer → Consumer Flow

```
1. Owner enables agent_api on producer Agent A. Owner can click "View Spec"
   to open the rendered OpenAPI docs in a new tab (see [Spec Viewer](spec_viewer.md)).
   If the producer env is suspended or stopped at that moment, the Spec Viewer
   wakes it first (showing "Waking up agent…") before loading the spec.
2. In a building session, agent A writes agent_api/*.py + policy.yaml.
   env-core reloads the supervised child; spec cache refreshes on the backend.
3. From consumer Agent B's Credentials tab, the owner clicks "Connect Agent API"
   and picks Agent A from the agent selector (agents with the API enabled,
   excluding B itself).
4. The connect action mints the proxy token, creates an agent_api credential
   {base_url, token, spec_url, label, producer_agent_id} bound to that token,
   and links it to Agent B (syncs into B's containers).
5. Agent B reads base_url + token from credentials.json,
   fetches {base_url}/openapi.json to discover the API,
   and calls endpoints with Bearer token.
   The proxy validates, enforces policy, and resolves A's env. If A's env is
   suspended or stopped (idle), the proxy auto-activates it and waits up to 10s
   for it to come up, then forwards the call — so B's first call after an idle
   period just takes a little longer, and the rest are fast. If A's env fails to
   start, or is still booting after 10s, B gets a 503 and retries (by which
   point A's env is typically already running).
6. To disconnect, delete the agent_api credential (from the producer card's
   connection list or the credential detail page). This cascade-deletes the
   token, so B immediately loses access.
```

> **Public vs. container URL.** The `base_url`/`spec_url` shown in the UI is the **public** proxy address (built from `FRONTEND_HOST`, e.g. `https://app.example.com/...` or `http://localhost:5173/...` in dev). The copy written into the consumer's `credentials.json` is **rewritten to the container-reachable backend origin** (`AGENT_ENV_BACKEND_URL`, e.g. `http://backend:8000/...`) — because inside the container the public host is not the backend (`localhost` is the container itself). The agent's code always uses the synced value; only the host differs, the path is identical. See [agent_api_tech.md](agent_api_tech.md#url-rewrite-on-env-sync-_rewrite_agent_api_urls_for_env).

---

## Token Model

`agent_api` tokens are **opaque tokens**, not JWTs (unlike A2A access tokens). They are never created or managed by hand — each token is minted by the connect helper and is the internal secret behind one `agent_api` connection credential. Security model mirrors `AgentAccessToken`:

- Token value generated at connect time and written **once** into the connection credential's encrypted data; only a SHA256 hash is stored, plus an 8-char `token_prefix` for display.
- Scoped to a single producer agent.
- **Bound to its credential** via `credential_id` (`ON DELETE CASCADE`): deleting the credential — disconnecting — deletes the token. This is the only revocation path (there is no manual revoke/restore).
- Tokens never expire — internal machine credentials.
- `last_used_at` updated on each validated use.
- `read_only_override`: a token may only narrow the producer's policy (force read-only), never widen it. (Not exposed in the simplified connect UI; the producer's `policy.yaml` is the primary read-only control, and it defaults to read-only.)
- 2FA does not apply — machine credential, consistent with A2A tokens.

---

## Caller Identity & Producer Scopes

The connection model above is **anonymous by design**: every consumer calls the producer with the *same* shared token (Level 1), so neither the platform nor the producer can tell *which user* is behind a call. This feature adds a second, automatic layer that lets the producer **know who is calling** and **control what each user may do** — with zero token management for the end user and no change to how a building agent writes API calls.

### The two-level model

- **Level 1 — Connection token (UNCHANGED).** The shared `agent_api` token = "you may reach Agent A's API at all." Granted by the producer's owner to anyone, shareable, transport-agnostic. The connect flow, the token, and bundle credential-sharing are untouched.
- **Level 2 — Caller identity (NEW).** A platform-minted, **auto-injected**, self-describing `owner_identity_token` placed in the consumer's `credentials.json`. The agent sends it as a second header alongside the L1 bearer. The platform proxy verifies it, resolves the *owner of the calling install*, looks up that owner's producer-defined scopes, and injects trusted `X-Cinna-Caller-*` headers into the forwarded request. The raw identity token is **stripped** before the request reaches the producer.

Attribution is "the cinna-core user who owns the calling install," not "whoever runs the code" — and it is owner-agnostic, so it works regardless of whether the producer's owner is the same person as the consumer's owner (e.g. across a bundle install).

### Anonymous-by-default contract

Identity is **never required**. If the identity token is absent, expired, or invalid (a raw caller, a legacy shared-token connection, a long-idle env whose token lapsed before its next sync), the proxy injects **no** caller headers and the producer sees an **anonymous** caller — never an error. Existing connections keep working unchanged; the producer decides what an anonymous caller may do.

### Zero token management for the user

The `owner_identity_token` is delivered exactly like the synthetic [`current_user`](../agent_credentials/agent_credentials.md#current-user-context) entry: computed **host-side** from the install owner each time credentials are prepared, **never stored**, **never redacted**, **never user-editable**. It is injected **only** when the env has at least one linked `agent_api` credential (no point shipping it to envs that never call a producer). The building agent never "knows" anything — the entry is **self-describing** (it carries the header name and a usage note), and the credentials `README.md` documents it under an *Owner Identity* section, so the agent just reads the entry and sends the header it names. The platform re-mints the token on every credential sync, so running envs always carry a fresh one.

### Owner assigns scopes; the platform resolves them live; the producer enforces

The producer's owner assigns **scopes** to individual platform users from the **Integrations → Agent REST API → Access & Scopes** card (an opt-in switch plus a user picker with per-user scope chips — see [UI States](#ui-states)). Available scope names come from a catalog the producer declares in `policy.yaml`.

- **Live resolution.** Scopes live in a grant table, *not* in the token. The proxy resolves `grant(producer, owner) → scopes` on **every call** and injects them as `X-Cinna-Caller-Scopes`. Editing or deleting a user's scopes takes effect on the **next call** — no token re-mint, no re-sync. This is the "control access from my side, from the UI" requirement.
- **No grant ⇒ no scopes.** An attributed-but-unscoped (or anonymous) caller arrives with an empty scope list; the producer decides what such a caller may do.
- **The producer is the primary enforcer.** It reads `caller.scopes` (capability level) and does the **data-level** authorization the platform cannot know ("user-E may touch archive-E, not archive-F"). The platform can *optionally* hard-deny scope-gated endpoints at the edge (see below), but that is defense-in-depth, not a replacement.
- **Scopes opt-in.** Scope injection (and the optional edge enforcement) only happen when the producer turns on the per-user-scopes switch (`agent_api_identity_enabled`). Identity *attribution* (`X-Cinna-Caller-User-Id/-Email/-Username`) is honored regardless of the flag — only scopes are gated by it, keeping the change backward-compatible.

### How the producer reads it (SDK `caller`)

Inside the producer's `agent_api/` code, the `cinna_api` SDK exposes a request-scoped `caller` accessor:

```python
from cinna_api import api, caller, Caller, error

@api.get("/orders")
def list_orders(me: Caller = caller):
    if me.is_anonymous:
        raise error(401, "Sign-in required")
    if not me.has_scope("orders.read"):
        raise error(403, "Missing scope")
    return {"orders": orders_for(me.user_id)}   # me.user_id / me.email / me.username
```

`caller` is a FastAPI dependency: `me.user_id`, `me.email`, `me.username`, `me.scopes`, plus `me.is_anonymous` and `me.has_scope(...)`. The header params are hidden from the harvested OpenAPI spec. **The `caller` accessor requires an environment rebuild** (it is new SDK code shipped in the env template).

### Four non-negotiable proxy security rules

All four hold at the `consumer_proxy` chokepoint and *are* the security model:

1. **Verify, then strip the identity token.** The proxy verifies the `X-Cinna-Caller-Identity` token (signature + audience + expiry), resolves the owner, then **removes the header before forwarding**. The producer *never* receives the raw identity token — otherwise a malicious producer could harvest callers' tokens and replay them to impersonate those users elsewhere. This is the linchpin.
2. **Strip inbound `X-Cinna-Caller-*` and set them authoritatively.** The identity token is the *only* accepted identity input. Any client-supplied `X-Cinna-Caller-*` headers are discarded and re-set by the proxy from the resolved owner + live grant.
3. **Producer reachable only via the proxy.** If a container could reach the producer's API directly it could forge `X-Cinna-Caller-*`. This invariant already holds (consumers only ever get the internal proxy origin in `credentials.json`).
4. **Missing identity ⇒ anonymous, never an error.** (The anonymous-by-default contract above.)

The identity token is a narrow, audience-restricted JWT (`aud="agent_api_caller"`, HS256/`SECRET_KEY`, verified backend-only) — useless as a general backend credential, so a leak is bounded to "I am owner-E" assertions on agent-api calls, and capability is still gated by the live grant.

---

## Cross-User Sharing via `CredentialShare`

`agent_api` credentials support all three sharing modes (user / publisher / template) by riding the existing `CredentialShare` pipeline. Because the thing shared is the **narrowed proxy** (`{base_url, token}`) and not the upstream secret, cross-user sharing is safe by construction:

- Owner sets `allow_sharing=True` on an `agent_api` connection credential (sharing cards remain on the credential detail page).
- Share by recipient email; recipient sees it under "Shared with Me".
- Recipient links it to their agent; it syncs into their container.
- Revoking access: revoke the `CredentialShare` (cuts that recipient) **or** delete the connection credential (deletes the token → cuts everyone using it).

---

## "Connect Agent API" — the only way to connect

Connecting is a single action, surfaced from the **consumer** agent's Credentials tab (and the global "Add Credential" picker). It uses the shared agent selector to pick a producer agent that has the API enabled (excluding the current agent), then calls `POST /agents/{producer_id}/agent-api/connect`, which:

1. Mints an `agent_api` token on the producer agent.
2. Creates an `agent_api` credential pre-filled with `{base_url, token, spec_url, label, producer_agent_id}` and binds the token to it (`credential_id`).
3. Links the credential to the consumer agent (immediate credential sync into the consumer's running containers).

The caller must own the producer agent (or be a superuser). The resulting credential is created with `allow_sharing=False` by default; the owner enables sharing afterwards. When invoked from the global picker with no consumer agent, the credential is created unlinked (it shows as "Not linked to an agent" on the producer card until linked).

**Workspace home.** The connection credential is stamped with a `user_workspace_id` at connect time so it groups under the same workspace as the agent it belongs to (instead of always landing in the default workspace). Consumer-first: when a consumer agent is given, the credential inherits *that* agent's workspace (it is configured on and synced into the consumer's containers); from the global picker with no consumer, it inherits the producer's workspace. If neither agent has a workspace it stays in the default workspace. The workspace does not follow a later re-link to an agent in a different workspace (grouping convenience, not an auth boundary). Legacy connections created before this change have no workspace stamp; an optional one-off backfill stamps those that are linked to exactly one workspaced agent.

**Global Credentials view.** `agent_api` connections appear under a dedicated **"Automatic Credentials"** section in `/credentials` (derived from `type == agent_api`, no stored flag), separate from "My Credentials". Their detail page lets you edit **name and notes** (the proxy token is still never shown), keeps the **Sharing** card, and **omits the Template-sharing** card — a connection has no user-fillable private fields, so it can never be template-provided.

---

## UI States

### Producer Integrations UI (Agent REST API card)

The producer card is **enable + View Spec + Refresh + a Connections list** — there is no token management UI.

| State | What the user sees |
|-------|-------------------|
| **Empty** (toggle off) | Enable toggle + one-paragraph explainer (e.g. "a narrow API in front of credentials with excessive permissions"). |
| **Error** | A **compact one-line summary** of the boot/harvest failure (e.g. "API failed to start. Error 404. Probably, not implemented yet."), a **Details** toggle that reveals the full raw error, and a **Retry** button. |
| **Enabled** | Status badge, a **View Spec** button (opens the harvested OpenAPI as rendered docs in a new tab — see [Spec Viewer](spec_viewer.md)), a **Refresh** button, and a **Connections** section listing the agents consuming this API. |

**Refresh button.** Next to **View Spec**, the **Refresh** button forces an on-demand re-harvest: it re-imports the producer's `agent_api/` modules to refresh the cached OpenAPI spec **and** re-parses `policy.yaml` to refresh the cached guardrails (`POST /_refresh` → `get_spec(force_refresh=True)` + `load_policy(force_refresh=True)`). By default both caches only refresh on the next *automatic* re-harvest (triggered when the producer edits a workspace file), so a `policy.yaml` edit applied out-of-band — or a transient harvest error — would otherwise stick until the next edit. Refresh clears it immediately. It is the same mechanism the error banner's **Retry** button uses.

If the producer environment is **suspended or stopped** when Refresh is clicked, the button first wakes the environment — showing a **"Waking up agent…"** state and polling `_refresh` in a bounded loop until the env reports `running`. The re-harvest runs only once the env is up. On success it toasts confirmed; on `state === "error"` it toasts an error. There is no false "refreshed" toast while the env is still down.

**Error banner — compact summary + Retry.** The raw boot/harvest error can be a long traceback or HTTP error string; the banner shows a short human summary (it extracts the HTTP status code when present and adds "Probably, not implemented yet." for a 404) with the full text behind **Details**. The error caches are *sticky* — env-core's in-memory boot error and the env-row spec error only clear on a successful re-harvest, which by default happens only when the producer edits a file. **Retry** forces an immediate re-harvest so a transient or already-fixed error clears without waiting for the next edit.

**Connections section:** one row per connection (token + its credential), showing the linked consumer agent(s) as their normal Bot badge (icon + agent colour preset), the agent **owner's email** next to each badge, a read-only badge when applicable, and a **Disconnect** (trash) button. The owner email disambiguates identical agent names — e.g. several users who each installed the same bundle and connected their copy produce same-named consumer agents that would otherwise be indistinguishable. Disconnect deletes the connection credential (or an orphaned token directly) — cascade-deleting the token. A connection created without a consumer link shows "Not linked to an agent" and can still be disconnected.

**Access & Scopes card.** Inside the same producer "Agent REST API" card sits an **Access & Scopes** sub-card for caller identity and per-user scopes. It has an opt-in switch ("identify calling users and grant each one capability scopes") that toggles `agent_api_identity_enabled`; while off, calls are still attributed to a user but carry no scopes. With it on, a `UserAllowlistPicker` (the same server-search picker credential sharing and the MCP connector ACL use) adds platform users, and each granted user gets a row of **scope chips**: catalog scopes the producer declared in `policy.yaml` are offered as quick-add suggestions, and free-text scope names can be added too. Changes are written through the grant routes and take effect on the next call.

### Consumer View

The `agent_api` connection credential appears in the Credentials page and Agent Credentials tab like any other credential. Its **detail page** is a connection panel: the producer agent, the connected consumer agent(s) as Bot badges, a **View Spec** button (opens the producer's spec as rendered docs in a new tab — see [Spec Viewer](spec_viewer.md)), the proxy `base_url`, and the standard Sharing / Share-as-Template cards. The token itself is never shown.

---

## Integration Points

- **[Agent Webapp](../agent_webapp/agent_webapp.md)** — `agent_api` reuses the container-HTTP-via-backend-proxy pattern, env auto-activation, keep-alive (`last_activity_at`), feature-toggle shape, and `EnvironmentPanel` tab convention. The two features coexist: one agent can have both a webapp (for human viewers) and an API (for agent consumers).

- **[A2A Access Tokens](../../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md)** — `agent_api_token` mirrors the A2A token security model (opaque value, SHA256 hash at rest, prefix, `last_used`). Unlike A2A tokens, `agent_api` tokens are not JWTs, never expire, and are not created or revoked directly — they live and die with their connection credential.

- **[Agent Credentials / Whitelist / Sharing](../agent_credentials/agent_credentials.md)** — new `AGENT_API` credential type rides the entire pipeline: credential sync to containers, whitelist (`base_url`, `spec_url`, `token`, `label`, `producer_agent_id` allowed), redaction (`token` appears as `***REDACTED***` in `README.md`), and `CredentialShare` for cross-user access. The caller-identity feature adds a second synthetic credentials.json entry (`owner_identity_token`) alongside `current_user` — see [Current User Context](../agent_credentials/agent_credentials.md#current-user-context).

- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — new `cinna_api` SDK package, supervised uvicorn child, and env-core HTTP routes live in the container. The lazy child supervision pattern mirrors the OpenCode child supervision (`opencode_sdk_adapter.py`).

- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** — `agent_api/` is added to `dirs_to_copy` in `copy_workspace_between_environments()`, so it ships with environment cloning and syncing.

- **[Agent Bundles](../agent_bundles/agent_bundles.md)** — `agent_api_enabled` and `agent_api/` workspace content can be snapshotted into a revision, so a published bundle can ship a working producer API. Publisher-provided `agent_api` credentials use the **one-shared-token model**: the publisher enables `allow_sharing=True` on the connection credential, marks it `provided_by="publisher"`, and the existing PBP pipeline delivers `{base_url, token}` to every installer's container at install time. Per-install token isolation remains future work (see Known Gaps). For per-user-scoped access, pair the PBP connection credential with a PBU per-user `api_token` second credential using `service_uri` to steer the install-time auto-match — see [Credential Sharing — `service_uri` Slot ID](../agent_credentials/credential_sharing.md#service_uri-slot-id-and-the-per-user-token-pattern) and [Agent Bundles — Two-credential pattern](../agent_bundles/agent_bundles.md).

- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** — `AGENT_API_STATUS_CHANGED` event is emitted after a spec reload or boot error so the owner's Integrations tab updates live without polling.

- **[Account CLI Workspace](../../application/cinna_cli_integration/account_cli_workspace.md)** — two layers. **Connect:** `cinna connect agent-api --producer P --consumer C` wraps `AgentApiTokenService.connect_agent_api` via `POST /api/v1/cli/account/connect/agent-api`; producer ownership gate and 403/404 mapping are reused unchanged. **Build + verify:** `cinna agent-api enable|refresh|spec <agent>` (the producer-side half that precedes connect) wrap the same `agent_api_enabled` toggle (`AgentService.update_agent`), `_refresh` re-harvest, and `openapi.json` spec read the Integrations card uses, via `POST /account/agent-api/enable`, `POST /account/agent-api/refresh`, and `GET /account/agent-api/spec`. So a local coding agent can stand up a producer API and verify the harvested spec, then wire a consumer, entirely from the CLI.

---

## Known Gaps and Future Work

### Bundle Publisher-Provided `agent_api` Credentials — Supported (One-Shared-Token Model)

Publisher-provided `agent_api` credentials in bundles are supported using the **one-shared-token model**. The publisher enables `allow_sharing=True` on an `agent_api` connection credential, marks it `provided_by="publisher"` in the Credential provisioning panel, publishes the bundle, and the existing PBP pipeline delivers a single `{base_url, token}` to every installer's container.

Accepted trade-offs with this model:
- **Shared rate-limit budget**: all installers' calls draw from the single token's per-token rate limit configured in the producer's `policy.yaml`.
- **Single producer environment**: all installer traffic routes through the publisher's one producer environment. The publisher must keep it running and scaled for the aggregate load.
- **No per-install revocation via token**: revoking the `CredentialShare` for a specific installer removes their access to the connection credential, but the token value itself is not per-install. Revoking the `CredentialShare` cuts that installer's PBP link and the runtime gate switches them to `publisher_broken`.

**Per-install token isolation** (minting a distinct token per installer, enabling per-install rate limits and independent revocation) remains future work.

For bundles that need per-user authority on top of the shared connection, pair the PBP connection credential with a PBU per-user `api_token` second credential stamped with a `service_uri` slot id. The install-time matcher auto-suggests the correct pre-shared per-user token even when its name differs from the spec. See [Credential Sharing — `service_uri` Slot ID](../agent_credentials/credential_sharing.md#service_uri-slot-id-and-the-per-user-token-pattern) for the full two-credential pattern and ordering constraint.

### Caller-identity `/introspect` endpoint — Deferred

Plan item 10 proposed an optional producer-facing `/introspect` endpoint (an OAuth-resource-server style "resolve identity per call" round-trip) for producers wanting a richer or lazy lookup instead of inline headers. It was **skipped** as redundant with the inline `X-Cinna-Caller-*` injection (which avoids a per-call round-trip and avoids handing the producer a backend credential). Inline injection is the only identity path; `/introspect` remains future work if a need emerges.

### Out of Scope (§12 of the original plan)

- **Declarative manifest mode** — pure-YAML "map upstream call → exposed endpoint" for non-coding owners.
- **Response field projection policy** — declarative allow/deny of response fields at the proxy edge.
- **Versioning** — `/v1`/`v2` prefixes or multiple spec versions per agent.
- **Per-endpoint token scopes** — limiting a token to specific `operationId`s.
- **Usage analytics / quotas dashboard** — per-token call counts, latency, error rates.
- **Caching layer** — proxy-level response caching for idempotent GETs.

---

*Last updated: 2026-06-21*
