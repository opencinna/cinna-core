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

---

## UI States

### Producer Integrations UI (Agent REST API card)

The producer card is **enable + View Spec + a Connections list** — there is no token management UI.

| State | What the user sees |
|-------|-------------------|
| **Empty** (toggle off) | Enable toggle + one-paragraph explainer (e.g. "a narrow API in front of credentials with excessive permissions"). |
| **Error** | A **compact one-line summary** of the boot/harvest failure (e.g. "API failed to start. Error 404. Probably, not implemented yet."), a **Details** toggle that reveals the full raw error, and a **Retry** button. |
| **Enabled** | Status badge, a **View Spec** button (opens the harvested OpenAPI as rendered docs in a new tab — see [Spec Viewer](spec_viewer.md)), and a **Connections** section listing the agents consuming this API. |

**Error banner — compact summary + Retry.** The raw boot/harvest error can be a long traceback or HTTP error string; the banner shows a short human summary (it extracts the HTTP status code when present and adds "Probably, not implemented yet." for a 404) with the full text behind **Details**. The error caches are *sticky* — env-core's in-memory boot error and the env-row spec error only clear on a successful re-harvest, which by default happens only when the producer edits a file. **Retry** forces an immediate re-harvest so a transient or already-fixed error clears without waiting for the next edit.

**Connections section:** one row per connection (token + its credential), showing the linked consumer agent(s) as their normal Bot badge (icon + agent colour preset), a read-only badge when applicable, and a **Disconnect** (trash) button. Disconnect deletes the connection credential (or an orphaned token directly) — cascade-deleting the token. A connection created without a consumer link shows "Not linked to an agent" and can still be disconnected.

### Consumer View

The `agent_api` connection credential appears in the Credentials page and Agent Credentials tab like any other credential. Its **detail page** is a connection panel: the producer agent, the connected consumer agent(s) as Bot badges, a **View Spec** button (opens the producer's spec as rendered docs in a new tab — see [Spec Viewer](spec_viewer.md)), the proxy `base_url`, and the standard Sharing / Share-as-Template cards. The token itself is never shown.

---

## Integration Points

- **[Agent Webapp](../agent_webapp/agent_webapp.md)** — `agent_api` reuses the container-HTTP-via-backend-proxy pattern, env auto-activation, keep-alive (`last_activity_at`), feature-toggle shape, and `EnvironmentPanel` tab convention. The two features coexist: one agent can have both a webapp (for human viewers) and an API (for agent consumers).

- **[A2A Access Tokens](../../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md)** — `agent_api_token` mirrors the A2A token security model (opaque value, SHA256 hash at rest, prefix, `last_used`). Unlike A2A tokens, `agent_api` tokens are not JWTs, never expire, and are not created or revoked directly — they live and die with their connection credential.

- **[Agent Credentials / Whitelist / Sharing](../agent_credentials/agent_credentials.md)** — new `AGENT_API` credential type rides the entire pipeline: credential sync to containers, whitelist (`base_url`, `spec_url`, `token`, `label`, `producer_agent_id` allowed), redaction (`token` appears as `***REDACTED***` in `README.md`), and `CredentialShare` for cross-user access.

- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — new `cinna_api` SDK package, supervised uvicorn child, and env-core HTTP routes live in the container. The lazy child supervision pattern mirrors the OpenCode child supervision (`opencode_sdk_adapter.py`).

- **[Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)** — `agent_api/` is added to `dirs_to_copy` in `copy_workspace_between_environments()`, so it ships with environment cloning and syncing.

- **[Agent Bundles](../agent_bundles/agent_bundles.md)** — `agent_api_enabled` and `agent_api/` workspace content can be snapshotted into a revision, so a published bundle can ship a working producer API. Bundle publisher-provided `agent_api` credentials are **not yet implemented** (see Known Gaps below).

- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** — `AGENT_API_STATUS_CHANGED` event is emitted after a spec reload or boot error so the owner's Integrations tab updates live without polling.

---

## Known Gaps and Future Work

### Bundle Publisher-Provided `agent_api` Credentials (Not Implemented)

Deliberately deferred. The open question (plan §6.2): should a bundle ship **one shared token** (simple, but no per-install revocation and the publisher's single env serves all installs' traffic) or **mint a token per install** (isolatable, but scales all consumer traffic through the publisher's env)? This is the same foreign-install-guard tension in the bundle propagation model. Do not ship publisher-mode `agent_api` credentials until that decision and the "who owns the env serving N foreign installs" question are resolved.

### Out of Scope (§12 of the original plan)

- **Declarative manifest mode** — pure-YAML "map upstream call → exposed endpoint" for non-coding owners.
- **Response field projection policy** — declarative allow/deny of response fields at the proxy edge.
- **Versioning** — `/v1`/`v2` prefixes or multiple spec versions per agent.
- **Per-endpoint token scopes** — limiting a token to specific `operationId`s.
- **Usage analytics / quotas dashboard** — per-token call counts, latency, error rates.
- **Caching layer** — proxy-level response caching for idempotent GETs.

---

*Last updated: 2026-05-27*
