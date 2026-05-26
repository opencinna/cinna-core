# Agent REST API Building Guide

## Overview
You can expose a **capability-narrowed REST API** from this agent's container by
writing plain typed Python functions decorated with the `cinna_api` SDK at
`/app/workspace/agent_api/`. The platform discovers your functions, runs a real
FastAPI app inside the container, harvests its OpenAPI spec, and serves it to
other agents (and to you, for preview) through a backend proxy.

The point of this feature: front a **powerful upstream credential** (an ERP key,
a broad OAuth scope, a legacy API token) with a **narrow, validated API**. The
powerful credential never leaves this container — the proxy is the only egress.
Other agents consume only the surface you choose to expose.

This is **code-to-code**: there is no LLM in the loop when a caller hits your
endpoints. It is for deterministic, typed, high-frequency function calls. (If
you want intelligence-mediated delegation, use A2A or handover instead.)

## Directory Structure
Place all API files in `/app/workspace/agent_api/`:
- `orders.py`, `customers.py`, … — modules with decorated endpoint functions
- `app.py` — *optional* explicit entrypoint (advanced; see Escape Hatch below)
- `policy.yaml` — platform-enforced guardrails (see Policy below)

Every `*.py` under `agent_api/` is imported on build, which registers its
endpoints. There is no manual wiring.

**Only endpoint modules belong in this directory.** Discovery imports *every*
`*.py` whose name does not start with a **double** underscore (`__`) — it does
**not** skip single-underscore files. So a `_smoke_test.py`, a `_helpers.py`, or
a Mutagen `*.conflict.*.py` copy left in `agent_api/` gets imported too. If such
a file imports one of your endpoint modules (directly or transitively), that
module's `@api.*` decorators run a **second** time against the shared singleton
`api` router, registering every route twice. The symptom is a
`UserWarning: Duplicate Operation ID …` per endpoint, after which the spec
harvest **produces no JSON** (`app.openapi()` fails on the collision) — the API
shows a boot error even though your endpoint module is itself correct.

Keep helpers, tests, and scratch files **out of** `agent_api/` — put them under
`scripts/` or another non-discovered location. If a helper genuinely must sit
beside the API, prefix it with a double underscore (e.g. `__helpers.py`) so the
glob skips it.

## The `cinna_api` SDK
Import everything from one place:

```python
from cinna_api import api, credentials, Query, Body, File, UploadFile, error
from cinna_api import StreamingResponse, BaseModel, Field
```

- `api` — a pre-created router. `@api.get/post/put/patch/delete(...)` mirror
  FastAPI's own decorators (pass-through), so all of FastAPI's parameter
  parsing, validation, and schema generation apply unchanged.
- `credentials` — typed accessor over `/app/workspace/credentials/credentials.json`:
  - `credentials.by_type("odoo")` → first credential of that type, or `None`
  - `credentials.get("<id-or-name>")` → a specific credential, or `None`
  - `credentials.all()` / `credentials.all_by_type(t)`
  - **Reads the file fresh on every call** — never cache the result; a token may
    have just been refreshed.
- `error(status, detail)` — raise it to return a consistent JSON error body.
- Re-exports: `UploadFile`, `File`, `Query`, `Body`, `StreamingResponse`,
  `BaseModel`, `Field`.

## Naming Your API (OpenAPI metadata)
Label the spec by defining these **module-level constants** in any of your
`agent_api` modules. The first non-empty value found wins per field; if you set
none, a generic default is used. (The platform does **not** name your API after
the environment — that's just the image name.)

```python
API_TITLE = "Orders API"
API_DESCRIPTION = "A narrow, read-only API in front of the upstream orders system."
API_VERSION = "1.0.0"
```

These flow straight into the harvested OpenAPI `info` block, so consumers (and
the spec preview) see a meaningful name. If you use the explicit `app.py` escape
hatch, set `title=`/`description=`/`version=` on your own `FastAPI(...)` instead.

## Minimal Example
```python
from cinna_api import api, Query, error, credentials
import httpx

API_TITLE = "Orders API"

@api.get("/orders")
def list_orders(limit: int = Query(20, ge=1, le=100)):
    """List orders. `limit` is validated (1..100) and shown in the spec."""
    cred = credentials.by_type("odoo")
    if cred is None:
        raise error(503, "Upstream credential not configured")
    data = cred["credential_data"]
    # ... call your upstream with `data`, return plain JSON-serializable values ...
    return {"orders": [...]}
```

## Fine-Grained Parameter Control (in code, reflected in the spec)
The way to "limit what callers can ask for" is to **type and constrain your
parameters**. FastAPI validates them and they appear in the harvested OpenAPI
schema, so callers (and generated clients) see exactly what is allowed:

```python
@api.get("/report")
def report(
    period: str = Query("month", pattern="^(day|week|month)$"),
    page_size: int = Query(50, ge=1, le=200),
):
    ...
```

Use Pydantic models with `Field(...)` constraints for request bodies.

## File Upload (multipart)
```python
from cinna_api import api, UploadFile, File

@api.post("/import")
async def import_file(f: UploadFile = File(...)):
    contents = await f.read()
    return {"filename": f.filename, "bytes": len(contents)}
```

## Streaming Responses
```python
from cinna_api import api, StreamingResponse

@api.get("/export")
def export():
    def rows():
        for r in fetch_big_dataset():
            yield (r + "\n").encode()
    return StreamingResponse(rows(), media_type="text/plain")
```

## Policy (`policy.yaml`) — platform-enforced guardrails
`policy.yaml` declares coarse guarantees the platform enforces **at the proxy
edge, before a request reaches your code**:

| Key | Default | Effect |
|---|---|---|
| `read_only` | `true` | Rejects non-GET/HEAD requests (405). |
| `allowed_methods` | (from `read_only`) | Explicit verb allowlist. |
| `auth` | `required` | A valid token is mandatory (no anonymous access). |
| `max_body_bytes` | `10485760` (10 MB) | Larger bodies rejected (413). |
| `rate_limit` | `60/min` | Per-token rate limit (429). |
| `expose_spec` | `true` | When `false`, blocks `/openapi.json` passthrough. |
| `allowed_paths` | `["*"]` | Optional path-prefix allowlist. |

A missing `policy.yaml` uses these defaults. A **malformed** `policy.yaml` fails
**closed** (deny-all) — the platform locks the API down rather than open it up.

**`read_only` precision:** it enforces *no state-changing HTTP verb*, NOT *no
state change*. A `GET` handler can still mutate its upstream. The proxy
guarantees the **method / body / rate** envelope; semantic safety is your
responsibility. Keep `GET` handlers genuinely read-only.

## Security Rules
- **Never return the upstream credential** (or any secret) in a response body.
  The whole point is that the powerful credential stays in this container.
- Read credentials **inside** your handlers via `credentials.*`, not at import.
- Never log credential values.
- Validate / clamp every input via typed params + `Field` constraints.

## Escape Hatch: explicit `app.py`
If you need full control, create `agent_api/app.py` defining your own
`app = FastAPI()`. When present, it takes precedence over auto-discovery. Most
agents should NOT need this — the decorator model is simpler and the spec is
harvested the same way.

## Dependencies
FastAPI, uvicorn, httpx, and pydantic are already installed in the base image —
**zero-install is the supported path**. (A per-API isolated `requirements.txt`
venv is a future enhancement; do not rely on it yet.)

## Lifecycle (what the platform does for you)
- The serving process is spawned **lazily on the first call** and **idle-reaped**
  after a few minutes — a chat-only session pays no API overhead.
- The OpenAPI spec is harvested **without** running the serving process, so
  "View Spec" works even when nothing is serving.
- On any change under `agent_api/`, the spec is re-harvested automatically. If
  your code has an import/boot error, it surfaces as an error status with the
  traceback — fix it and save again.

## Scaffolding a Starter
Run the scaffolder to drop a working `orders.py` + `policy.yaml` (read-only,
proxying one upstream GET) so you start from a running example:

```bash
python /app/core/scripts/scaffold_agent_api.py
```

It skips files that already exist (use `--force` to overwrite). To make this a
one-touch command in chat, add it to `docs/CLI_COMMANDS.yaml`:

```yaml
commands:
  - name: scaffold-agent-api
    command: python /app/core/scripts/scaffold_agent_api.py
    description: Scaffold a starter agent REST API (orders.py + policy.yaml)
```

…then invoke it as `/run:scaffold-agent-api`.

## What NOT to Do
- Do not return secrets or raw upstream credentials in responses.
- Do not write files outside `/app/workspace/agent_api/`.
- Do not put helper, test, or scratch files in `agent_api/`. Discovery imports
  every non-`__`-prefixed `*.py`, so a stray `_smoke_test.py` (or a Mutagen
  `*.conflict.*.py`) that imports an endpoint module double-registers its routes
  on the shared singleton router and breaks the spec harvest with
  `Duplicate Operation ID`. Keep such files in `scripts/`.
- Do not assume `read_only: true` makes a handler safe — keep GETs read-only.
- Do not block the event loop with long synchronous work in `async` handlers;
  use sync `def` handlers (FastAPI runs them in a threadpool) for blocking I/O.
