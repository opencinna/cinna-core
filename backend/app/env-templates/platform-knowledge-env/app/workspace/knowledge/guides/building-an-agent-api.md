# Building an Agent REST API (producer side)

End-to-end walkthrough for turning an agent into an **Agent REST API producer**
from the account workspace: write the handler code + `policy.yaml`, enable the
API, declare per-user **scopes**, and verify the harvested spec — without opening
the browser.

This is the "per-agent development guide" the agentic-network playbook defers to
for the producer's API. Pair it with
[build-an-agentic-network.md](build-an-agentic-network.md), which covers the
**consumer** side (`cinna connect agent-api`).

## When to use this

Read this when an agent must expose a **capability-narrowed REST API** that other
agents call **code-to-code** (no LLM in the request path) — e.g. wrapping a
powerful upstream credential (an ERP key, a broad OAuth token, a legacy API)
behind a narrow, validated surface that stays inside the producer container.

If you only need to *consume* an existing producer's API, you don't need this —
jump straight to `cinna connect agent-api`.

## The shape of a producer

A producer keeps two things in its workspace under `agent_api/`:

```
agents/<name>/workspace/agent_api/
├── orders.py        # your decorated handler functions (any *.py name)
└── policy.yaml      # platform-enforced guardrails (see Policy below)
```

- Files land in the container the same way the rest of the workspace does — edit
  them locally under `agents/<name>/workspace/agent_api/` and they sync to the
  running environment. On any change under `agent_api/`, the platform
  **re-harvests** the OpenAPI spec automatically.
- FastAPI / uvicorn / httpx / pydantic are **pre-installed** in the base image —
  zero-install is the supported path.
- The serving process is spawned **lazily on the first call** and idle-reaped
  after a few minutes; a chat-only session pays no API overhead. The spec is
  harvested **without** running the serving process, so `spec` works even when
  nothing is serving.

### Handler basics (`@api` + `caller`)

The full handler reference lives **inside the container** at
`/app/core/prompts/REST_API_BUILDING.md` (read it from a building session, or
`cinna exec --agent <name> -- cat /app/core/prompts/REST_API_BUILDING.md`).
The essentials:

- Decorate plain functions with `@api.get(...)` / `@api.post(...)` from
  `cinna_api`. Typed params + pydantic `Field` constraints ARE your input
  validation and show up in the spec.
- Read the upstream secret **inside** the handler via `credentials.*` — never at
  import, never returned in a response body.
- Add a `me: Caller = caller` parameter to learn **who is calling**:
  `me.user_id`, `me.email`, `me.scopes` (the resolved capability list), and
  `me.has_scope("orders.write")`. **You are the primary authorizer** — branch on
  `me.scopes` / `me.user_id` and enforce your own rules. Anonymous callers
  (`me.user_id is None`) must be handled explicitly.

Scaffold a running starter (`orders.py` + `policy.yaml`) from a building session:

```bash
python /app/core/scripts/scaffold_agent_api.py     # add --force to overwrite
```

## Step 1 — Enable the API

The API only serves when `agent_api_enabled` is true on the agent:

```bash
cinna agent-api enable --agent <name>
```

This is developer-gated and returns the resulting status, so it doubles as a
first verify. (Equivalent low-level call:
`cinna api PUT agents/<id> --json '{"agent_api_enabled": true}'`.)

## Step 2 — Author `policy.yaml`

`policy.yaml` declares **coarse** guarantees the platform enforces at the proxy
edge, *before* a request reaches your code. All keys are optional; defaults apply
to anything omitted.

| Key | Default | Effect |
|---|---|---|
| `read_only` | `true` | Rejects non-GET/HEAD requests (405). |
| `allowed_methods` | (from `read_only`) | Explicit verb allowlist; overrides `read_only`. |
| `auth` | `required` | A valid token is mandatory (no anonymous access). |
| `max_body_bytes` | `10485760` (10 MB) | Larger bodies rejected (413). |
| `rate_limit` | `"60/min"` | Per-token sliding-window limit (429). |
| `expose_spec` | `true` | When `false`, blocks `/openapi.json` passthrough. |
| `allowed_paths` | `["*"]` | Optional path-prefix allowlist. |
| `scopes` | (none) | Per-user capability catalog — see Step 3. |

```yaml
# agent_api/policy.yaml
read_only: true
allowed_methods: [GET, POST]
max_body_bytes: 10485760
rate_limit: "60/min"
allowed_paths:
  - "/orders"
  - "/products"
```

- A **missing** `policy.yaml` applies these defaults (read-only by default).
- A **malformed** `policy.yaml` fails **closed** (deny-all) — the platform locks
  the API down rather than open it up. Unknown keys are silently ignored.
- `read_only: true` enforces *no state-changing verb*, NOT *no state change* — a
  `GET` handler can still mutate upstream. The proxy guarantees the
  method/body/rate envelope only; keep GETs genuinely read-only yourself.

## Step 3 — Declare per-user scopes (the `scopes:` catalog)

Scopes let the producer's owner grant **named capabilities** to individual
platform users. The platform resolves a caller's grant **live on every call** and
hands the names to your code as `caller.scopes` (and, optionally, hard-denies at
the edge). Editing a user's scopes takes effect on the **next call** — no
redeploy, no token re-mint.

> **This is the `scopes:` key that the "Access & Scopes" UI catalog reads.** The
> picker offers these names as quick-add suggestions; the exact authoring forms
> are below.

Two authoring forms are accepted (mix freely in one catalog):

```yaml
# Form 1 — documentation only (name: description). The names appear in the UI
# picker and arrive as caller.scopes; YOUR code is the only enforcer.
scopes:
  orders.read: "Read orders"
  orders.write: "Create or modify orders"
```

```yaml
# Form 2 — also lets the PLATFORM hard-deny at the edge (defense-in-depth).
# A request matching a `requires` pattern is rejected 403 BEFORE reaching your
# code unless the caller's grant carries that scope.
scopes:
  orders.read: "Read orders"
  orders.write:
    description: "Create or modify orders"
    requires:
      - { method: POST, path: /orders }   # method optional; omit for "any verb"
      - { method: PUT,  path: /orders }
  admin:
    description: "Administrative endpoints"
    requires:
      - { path: /admin }                  # any method under /admin
```

(A bare list — `scopes: ["orders.read", "orders.write"]` — is also accepted and
is equivalent to Form 1 with empty descriptions.)

Rules to keep in mind:

- `path` is a **prefix** (`/orders` gates `/orders` and `/orders/123`, but not
  `/orders-archive`). `method` is optional and defaults to **any** verb.
- **Edge enforcement is opt-in and conservative.** It fires only when BOTH (a)
  per-user access is enabled on the agent (`agent_api_identity_enabled`, Step 4),
  AND (b) the scope declares `requires:` patterns. A documentation-only catalog
  (Form 1) is never edge-enforced; a producer that hasn't enabled per-user access
  is never edge-denied — existing connections keep working unchanged.
- **You remain the primary enforcer.** Edge enforcement is a coarse method/path
  backstop; only your code knows data-level rules. Check `caller.scopes` /
  `caller.has_scope(...)` in handlers even for edge-gated endpoints.
- A malformed `scopes:` section degrades to **no catalog** (no edge enforcement)
  without failing the rest of the policy; a wholly unparseable `policy.yaml`
  still fails closed.
- Scope **names are opaque tokens** transported space-separated on the wire —
  use dot/underscore names (`orders.read`), never names with whitespace.

## Step 4 — Turn on per-user access and assign grants

Scope *injection* (and the optional edge enforcement) only happen once the
producer opts in. Identity *attribution* (`X-Cinna-Caller-User-Id/-Email/
-Username`) is honored regardless — only scopes are gated by this flag.

```bash
# Opt in (per-user scopes ON). This is the "Access & Scopes" switch.
cinna api PUT agents/<id> --json '{"agent_api_identity_enabled": true}'
```

Grant routes live under `agents/<id>/agent-api/grants` (reachable via `cinna api`;
ownership-gated, 404 no-leak):

```bash
# What scope names does my policy.yaml expose? (the catalog the UI shows)
cinna api GET agents/<id>/agent-api/grants/scope-catalog

# Grant a user a set of scopes (one grant row per user; empty list = identified
# but no capabilities). Find the user_id via `cinna api GET users/search?q=...`.
cinna api POST agents/<id>/agent-api/grants \
  --json '{"user_id": "<user-uuid>", "scopes": ["orders.read", "orders.write"]}'

# List / update / remove
cinna api GET    agents/<id>/agent-api/grants
cinna api PUT    agents/<id>/agent-api/grants/<grant_id> --json '{"scopes": ["orders.read"]}'
cinna api DELETE agents/<id>/agent-api/grants/<grant_id>
```

> Grants are **per platform user**, keyed to the *owner of the calling install*.
> Granting `orders.write` to user U means: when an install owned by U calls this
> API, `caller.scopes` contains `orders.write`.

## Step 5 — Verify

```bash
# Re-harvest the spec + re-parse policy.yaml on demand (also clears a sticky
# boot/harvest error). Wakes the env if it's not running.
cinna agent-api refresh --agent <name>

# Fetch the harvested OpenAPI spec (works even when nothing is serving).
cinna agent-api spec --agent <name>

# Owner-side smoke test of one endpoint (no consumer token, no edge policy —
# query params ARE forwarded). Confirms a handler actually runs.
cinna agent-api call --agent <name> --method GET --path /orders
```

A successful `refresh` with no `last_error` and a `spec` listing your endpoints
means the producer is ready to connect.

## Step 6 — Connect a consumer

Once the spec harvests cleanly, wire a consumer agent to it:

```bash
cinna connect agent-api --producer <name> --consumer <other-agent> --label "Orders API"
```

The consumer receives an `agent_api` credential in its `credentials.json`
(`base_url` + `token`); the upstream secret never leaves the producer. See
[build-an-agentic-network.md](build-an-agentic-network.md) for the full consumer
flow and how the connection sits alongside MCP and team-delegation wiring.

## Common pitfalls

- **Scopes not reaching `caller.scopes`.** Confirm `agent_api_identity_enabled`
  is `true` (Step 4) AND the calling install's owner has a grant. No flag or no
  grant ⇒ empty `caller.scopes` (the caller is still identified).
- **Catalog endpoint returns empty.** The `scopes:` block is missing, malformed,
  or the spec hasn't re-harvested — run `cinna agent-api refresh`.
- **Edge 403 you didn't expect.** A `requires:` pattern matched and the caller's
  grant lacks the scope. Remember edge matching is path-**prefix** based.
- **`Duplicate Operation ID` on harvest.** A stray helper/test/`*.conflict.*.py`
  file under `agent_api/` got imported and double-registered routes. Keep
  non-endpoint files out of `agent_api/` (put them in `scripts/`).
