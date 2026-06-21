# Agent-Environment Token Scoping — Security Hardening Plan

> **Classification:** Security hardening for a **verified privilege-escalation / account-takeover vulnerability.**
> Correctness and exhaustiveness are the top priority. Every behavior change must be preceded by the
> Phase-0 consumer enumeration and reconciled against the back-compat strategy in Phase 4.

---

## 1. Overview

The agent-environment internal token (`AGENT_AUTH_TOKEN`, stored in `AgentEnvironment.config["auth_token"]`,
injected into the container `.env` / `docker-compose.yml`) is currently minted as a **plain owner-user JWT** with
a **10-year** lifetime and **no distinguishing claim** (`{sub: owner_id, exp}`). Because `sub = owner_id`, the
token decodes cleanly in the generic `get_current_user` dependency and resolves to the **full owner `User` with no
agent/environment scoping.** Any code running inside an agent container (the realistic compromise surface — it runs
untrusted/agent-authored workloads) can therefore call **any** `CurrentUser`-protected endpoint as the owner,
including `GET /credentials/{id}/with-data` (decrypted secrets for **all** the owner's credentials), other agents,
sessions, files, and AI credentials. **One container compromise = full account takeover.**

This plan hardens the token so that:

- The env token is **identifiable** as an env token (new `token_type` + `env_id` + `agent_id` claims).
- The generic `get_current_user` dependency **rejects** env tokens — the **load-bearing fix**.
- A single **scoped dependency** (`AgentEnvContextDep`, mirroring `CLIContextDep`) resolves
  `(environment, agent, owner)` from the env token and is the **only** way an env token authenticates.
- Every backend route the container legitimately calls back into is migrated onto that scoped dep, each enforcing
  that the call only touches resources belonging to the **originating agent/environment**.
- The token is **short(er)-lived, rotated on rebuild, and server-side revocable** (hash/version stored on the env).

### Core capabilities after this change

- Env token cannot satisfy `CurrentUser` — credential/agent/session/file endpoints are inaccessible to it.
- Env token is bound to exactly one `(env_id, agent_id, owner_id)` triple; cross-agent / cross-env access is denied.
- Real user, CLI, and desktop tokens are **unaffected**.
- A stale/leaked env token can be invalidated server-side (revocation field) and is rotated on rebuild.

### High-level flow

```
                        ┌──────────────────────────── BEFORE (vulnerable) ───────────────────────────┐
container code ──Bearer owner-JWT (sub=owner_id, 10y)──► get_current_user ──► FULL owner User (no scope)
                                                                              │
                                                                              └─► GET /credentials/{id}/with-data ✅ (!!)

                        ┌──────────────────────────── AFTER (scoped) ───────────────────────────────┐
container code ──Bearer env-JWT (token_type=agent_env, env_id, agent_id) + X-Agent-Env-Id──┐
                                                                                            ▼
   get_current_user ──► REJECTS env token (token_type=agent_env) ──► 401      AgentEnvContextDep
                                                                                  │ verify aud/type, match env.config hash,
                                                                                  │ check not revoked, env_id==path/header
                                                                                  ▼
                                                          (environment, agent, owner) scoped to ONE install
                                                                                  │
                              GET /credentials/{id}/with-data  ── NOT reachable (route uses CurrentUser) ──► 401
                              POST /agent/tasks/create         ── reachable, scoped to env's agent ──────► ✅
```

---

## 2. Architecture Overview

### 2.1 Components touched

| Layer | File(s) | Role |
|-------|---------|------|
| Token mint | `backend/app/services/environments/environment_lifecycle.py` (`_generate_auth_token` @ 2805, caller `_update_environment_config` @ 1738/1782) | Mint env token with new claims + store revocation material |
| Env model | `backend/app/models/environments/agent_environment.py` | New revocation/version column |
| Generic dep | `backend/app/api/deps.py` (`get_current_user` @ 35) | **Reject** env tokens |
| Scoped dep | `backend/app/api/deps.py` (new `AgentEnvContext` + `get_agent_env_context`) | Resolve `(env, agent, owner)` |
| Existing scoped deps (reconcile) | `backend/app/api/routes/environments.py` (`_verify_env_agent_auth` @ 44), `backend/app/api/routes/knowledge.py` (`verify_agent_auth_token` @ 16) | Collapse both into the single new dep |
| Vulnerable env-called routes (migrate) | `backend/app/api/routes/task_agent_api.py` (11 routes, all `CurrentUser`), `backend/app/api/routes/security_events.py` (`POST /report` @ 57) | Migrate to scoped dep |
| Already-scoped env-called routes | `knowledge.py::POST /knowledge/query`, `environments.py::{workspace-files-changed, prompt-file-changed, agent-api-reloaded}` | Re-point to unified dep |
| Env-core (container) | `backend/app/env-templates/app_core_base/core/...` | Send `X-Agent-Env-Id` on task/security-event calls; read new token unchanged |

### 2.2 Three roles of `AGENT_AUTH_TOKEN` today (must be preserved or consciously split)

The single value plays **three** roles. The plan must not break any of them:

1. **Inbound bearer** — backend → container. The container verifies the bearer **by exact string match** against its
   own `AGENT_AUTH_TOKEN` env var (`app_core_base/core/server/routes.py::verify_auth_token` @ ~233, compare @ ~262).
   *This role does not care about JWT claims* — adding claims is transparent to it.
2. **Outbound bearer** — container → backend. Today validated as a user JWT via `CurrentUser` (the vulnerability).
   **This is the role we are scoping.**
3. **HMAC signing key** — backend signs `session_context` with the token value (`session_context_signer.py`,
   `message_service.py` @ ~2456); container verifies with the same value
   (`active_session_manager.py::_verify_hmac` @ 135-148). *This role uses the token as an opaque secret string* —
   adding claims is transparent to it, **but token rotation changes the HMAC key** (see Phase 4 rotation note).

> **Key insight:** roles (1) and (3) treat the token as an opaque shared secret and are unaffected by adding claims.
> Only role (2) — the outbound user-JWT impersonation — is the vulnerability and the target of this plan.

### 2.3 Data flow (after)

```
environment_lifecycle._update_environment_config
   ├─ _generate_auth_token(owner_id, env_id, agent_id) ── mints JWT { sub:owner_id, token_type:"agent_env",
   │                                                                   aud:"agent_env", env_id, agent_id, exp }
   ├─ env.config["auth_token"]      = <jwt>          (role 1 inbound + role 3 HMAC, unchanged)
   ├─ env.auth_token_hash           = sha256(<jwt>)  (NEW — revocation/verification material on the row)
   └─ writes .env (AGENT_AUTH_TOKEN=<jwt>) + docker-compose
            │
container outbound call (task/knowledge/security-event/callback)
   └─ Authorization: Bearer <jwt> + X-Agent-Env-Id: <env_id>
            │
backend AgentEnvContextDep:
   1. jwt.decode(token, aud="agent_env")  → require token_type=="agent_env"     (else 401)
   2. env = session.get(AgentEnvironment, claims.env_id)                         (else 404)
   3. sha256(token) == env.auth_token_hash AND token == env.config["auth_token"] (else 401 — revocation/rotation gate)
   4. (header X-Agent-Env-Id present) env_id matches claim & header & path {id}  (else 403)
   5. load agent (claims.agent_id == env.agent_id), load active owner User       (else 403/404)
   → AgentEnvContext(environment, agent, owner)
```

---

## 3. Data Models

### 3.1 `AgentEnvironment` — new column

Add a **revocation / verification** column to the env row, mirroring the spirit of `CLIToken.token_hash`
(hash-at-rest + server-side revocability). The token itself **remains** in `config["auth_token"]` because roles 1
and 3 read it back verbatim; the new column is the **authoritative verification + revocation** anchor.

| Field | Type | Constraints / Default | Purpose |
|-------|------|-----------------------|---------|
| `auth_token_hash` | `str \| None` | nullable, `index=True`, `max_length=64` | SHA-256 hex of the current env token. Set on every mint. The scoped dep verifies `sha256(presented) == auth_token_hash`. Setting it to `NULL` (or to a new value) **instantly revokes** all previously-issued tokens for this env. |

Rationale for storing both the raw token (in `config`) **and** the hash (new column):
- `config["auth_token"]` must keep the raw value (roles 1 + 3 read it back).
- The hash column is what the dep checks, and is the single field to mutate for revocation/rotation. (Verifying
  against `config["auth_token"]` directly also works, but a dedicated indexed column makes revocation explicit,
  auditable, and lets a future change drop the raw value from `config` without touching the dep.)

> **Decision (single source of verification):** the dep checks **both** `sha256(token) == env.auth_token_hash`
> **and** `token == env.config["auth_token"]`. During the grace window (Phase 4), `auth_token_hash` may be `NULL`
> for not-yet-rotated envs; in that case the dep falls back to the verbatim `config["auth_token"]` compare (the
> existing behavior of the two legacy deps). Once rotated, the hash is authoritative.

No new tables. No `CLIToken`-style standalone token table is introduced — the env row already **is** the per-env
record, so the revocation material lives on it (one token per env, 1:1, unlike CLI where one user has many tokens).

### 3.2 New JWT claim set (env token payload)

Minted via `core/security.create_access_token(subject, expires_delta, extra_claims=...)`:

| Claim | Value | Purpose |
|-------|-------|---------|
| `sub` | `str(owner_id)` | Kept so downstream identity (HMAC/role-3 unaffected, owner resolution in dep) still works |
| `token_type` | `"agent_env"` | Distinguishing claim — `get_current_user` rejects on this; scoped dep requires it |
| `aud` | `"agent_env"` | Audience restriction (mirrors `owner_identity_token` `aud="agent_api_caller"`); scoped dep enforces via `jwt.decode(..., audience="agent_env")` |
| `env_id` | `str(env_id)` | Binds the token to one environment |
| `agent_id` | `str(agent_id)` | Binds the token to one agent |
| `exp` | now + TTL (see Phase 1) | Bounded lifetime (was 10 years) |

> Both `token_type` and `aud` are set. `token_type` is the cheap structural reject in `get_current_user`
> (no audience decode needed); `aud` is defense-in-depth so the token is cryptographically useless on any decode
> path that enforces audience. This matches the `owner_identity_token` precedent (`aud` + `type`).

---

## 4. Security Architecture

### 4.1 The load-bearing fix — reject env tokens in `get_current_user`

`get_current_user` (`deps.py:35`) currently does `jwt.decode` → `TokenPayload(sub)` → `session.get(User, sub)`.
Because the env token's `sub` is the owner's real user UUID, it resolves to a full `User`. We add an **explicit
reject** immediately after decode:

```
payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])     # existing
if payload.get("token_type") == "agent_env":                        # NEW
    raise HTTPException(401, "Agent-environment tokens cannot be used here")
```

- This MUST run before the `session.get(User, ...)` call.
- 401 (not 403/404) so the failure is unambiguous and not confused with "user not found".
- This is the single change that makes every other mitigation non-bypassable: even if a route migration is missed,
  an env token can no longer impersonate the owner on any `CurrentUser` route.

> **Why a `token_type` check and not audience?** `get_current_user` does a plain `jwt.decode` without `audience=`,
> so an `aud`-only token would still decode (PyJWT does not require `aud` unless you pass `audience=`/`options`).
> The explicit `token_type` reject is the reliable structural gate. (We also set `aud` for the scoped path.)

### 4.2 Scoped dependency — `AgentEnvContext` / `get_agent_env_context`

Mirrors `CLIContext` / `_resolve_cli_context` / `CLIContextDep` exactly (`deps.py:262-375`).

Context object (new, in `deps.py`):

```
class AgentEnvContext(SQLModel):
    environment: Any   # AgentEnvironment
    agent: Any         # Agent
    owner: User
```

Resolver `_resolve_agent_env_context(db, raw_token, env_id_header, path_env_id=None) -> AgentEnvContext`:

1. `jwt.decode(raw_token, SECRET_KEY, algorithms=[ALGORITHM], audience="agent_env")` — on failure → 401.
2. Require `payload["token_type"] == "agent_env"` → else 401.
3. `env_id = UUID(payload["env_id"])`; if `X-Agent-Env-Id` header present, require it equals `env_id` → else 403;
   if route has a path `{id}`, require it equals `env_id` → else 403.
4. `env = db.get(AgentEnvironment, env_id)` → not found → 404.
5. **Revocation/rotation gate:** if `env.auth_token_hash` is set → require `sha256(raw_token).hexdigest() == env.auth_token_hash`;
   else (grace window, hash NULL) → require `raw_token == env.config.get("auth_token")`. Mismatch → 401.
6. `agent = db.get(Agent, env.agent_id)`; require `payload["agent_id"] == str(env.agent_id)` → else 403.
7. `owner = db.get(User, agent.owner_id)`; require active → else 404/400.
8. Return `AgentEnvContext(environment=env, agent=agent, owner=owner)`.

Wrappers:

```
def get_agent_env_context(token: TokenDep, db: SessionDep,
                          x_agent_env_id: Annotated[str | None, Header()] = None) -> AgentEnvContext: ...
AgentEnvContextDep = Annotated[AgentEnvContext, Depends(get_agent_env_context)]
```

> The `{id}` path check: today it lives **inside** the environments.py handlers (`if env.id != id: 403`).
> The new dep cannot see the path param generically, so for the three environments.py callbacks the handler keeps a
> one-line `if ctx.environment.id != id: raise 403` (or we add a path-aware variant). Folding the comparison into the
> dep for header-only routes (task/knowledge/security-events) is sufficient there.

### 4.3 Access control rules enforced

- An env token authenticates **only** via `AgentEnvContextDep`; it is rejected by `CurrentUser`.
- Every migrated route operates strictly on `ctx.environment` / `ctx.agent` / `ctx.owner` — never on an arbitrary
  user-supplied agent/credential/session id outside that scope. Each handler must re-derive the agent from `ctx`,
  not from request body/query, for ownership-sensitive operations.
- `task_agent_api.py` routes that today resolve "agent identity from the task record" must additionally assert the
  resolved task/agent belongs to `ctx.agent` / `ctx.owner` (see Phase 3 per-route notes).

### 4.4 Sensitive-data handling / logging

- Never log the raw token. Log `prefix` (first 8-12 chars) or the hash on auth failures, mirroring CLI/agent_api.
- The hash column is non-secret (one-way); safe to index and to surface in admin tooling.
- Fail-closed: unlike the container-side `verify_auth_token`/`_verify_hmac` which **fail open** when the token is
  unset, the new backend dep must **fail closed** (missing/empty token → 401). (Container-side fail-open is a
  separate, pre-existing concern noted in §9; out of scope to change here but flagged.)

### 4.5 Rate limiting

Not required for this fix (the token is machine-to-machine on the internal Docker network). No change.

---

## 5. Backend Implementation

### Phase 0 — Exhaustive token-consumer enumeration (DELIVERABLE, do this FIRST)

> **Mandatory gate.** No behavior may change until this enumeration is produced and reviewed. The lists below are
> the result of the planning investigation and MUST be re-verified by the developer before edits (line numbers
> drift). Produce the final enumeration as a checked-in artifact (PR description or a short `*_consumers.md` note).

**A. Generation / storage / injection**
- `environment_lifecycle.py:2805` `_generate_auth_token` (def) — the only mint helper.
- `environment_lifecycle.py:1782` — only caller (`_update_environment_config`, def @ 1738); runs on create/start/restart/rebuild.
- `environment_lifecycle.py:1783-1784` — writes `config["auth_token"]` + `flag_modified`.
- `environment_lifecycle.py:118-125` — `get_adapter` reads `config.get("auth_token")` → `DockerEnvironmentAdapter(auth_token=...)`.
- `environment_lifecycle.py:2294` — writes `AGENT_AUTH_TOKEN=` into per-env `.env`; `:2291` writes `BACKEND_URL=settings.AGENT_ENV_BACKEND_URL`.
- `environment_lifecycle.py:2128` — `_generate_compose_file` substitutes `${AGENT_AUTH_TOKEN}`.
- Templates carrying the placeholder: `general-env/docker-compose.template.yml:14`, `python-env-advanced/docker-compose.template.yml:14`, `platform-knowledge-env/docker-compose.template.yml:13`; `.env.template`s: `general-env/.env.template:14`, `python-env-advanced/.env.template:14`, `platform-knowledge-env/app/.env.template:3`.
- `core/config.py:300` `AGENT_AUTH_TOKEN: str = secrets.token_urlsafe(32)` — global Settings fallback; **verify whether actually used** vs. the per-env token (likely a dev/global default — confirm and document).

**B. Backend → container (token sent) + HMAC**
- Canonical header builder: `message_service.py:1400` `get_auth_headers(environment)` → `{"Authorization": f"Bearer {config['auth_token']}"}`.
- `agent_env_connector.py` Bearer-sending methods: `stream_chat` @ 31, `exec_command` @ 103 (builds header @ 133), `stream_command` @ 170, `open_sync_websocket` @ 260, `open_shell_websocket` @ 295, `interrupt_command` @ 361.
- Connector call sites: `message_service.py` (1487, 1579-1606, 1682, 1997, 2068, 2088, 2132, 2455-2464, 2538); `cli_service.py` (419, 425, 468, 474, 741, 755, 833, 836); `environment_console_service.py:243-246`; `agent_webhook_service.py:684-699`; `agent_status_service.py:226-231`; `agent_schedule_scheduler.py:427-434`; `task_attachment_service.py:203-204`; `input_task_service.py:3041-3042`.
- Docker adapter header builder + ~23 call sites: `docker_adapter.py:46/62` (ctor), `:273-277` (`_get_headers`), calls at 283…1470.
- HMAC: `session_context_signer.py:19/36`; `message_service.py:16` (import), `:1683` (capture token), `:2455-2457` (sign with `ctx.env_auth_token`); container verify `active_session_manager.py:135-148`.

**C. Env-core side (container) outbound calls — base URL = `BACKEND_URL` (from `settings.AGENT_ENV_BACKEND_URL`); all send `Authorization: Bearer {AGENT_AUTH_TOKEN}`**

| env-core file | backend path it calls | Sends `X-Agent-Env-Id` today? |
|---|---|---|
| `core/main.py:68-70` | `POST /api/v1/environments/{env_id}/workspace-files-changed` | env_id in path |
| `core/cinna_api/supervisor.py:545-547` | `POST /api/v1/environments/{env_id}/agent-api-reloaded` | env_id in path |
| `core/server/routes.py:319-322` | `POST /api/v1/security-events/report` | **NO** ⟶ must add |
| `core/server/tools/agent_task_*.py` (create/by-code/current+id comment/status/subtask/details/list) | `/api/v1/agent/tasks/*` | **NO** ⟶ must add |
| `core/server/tools/knowledge_query.py:201-204` | `POST /api/v1/knowledge/query` | **YES** (already) |
| `core/server/tools/mcp_bridge/task_server.py` (multiple) | `/api/v1/agent/tasks/*` | **NO** ⟶ must add |
| `core/server/tools/mcp_bridge/knowledge_server.py:164-172` | `POST /api/v1/knowledge/query` | confirm; align with knowledge_query |
| `platform-knowledge-env/.../scripts/examples/platform_helper.py` | generic `{BACKEND_URL}{path}` | example helper — see §11 note |

- Token read in container: `routes.py:148`, `active_session_manager.py:137`, `main.py:61`, `cinna_api/supervisor.py:67`, per-tool module constants (`agent_task_*.py`, `knowledge_query.py:28`), mcp bridges (`task_server.py:38`, `knowledge_server.py:31`).
- Container inbound verify (role 1): `routes.py:233` `verify_auth_token` (compare @ ~262, **fail-open** when unset @ ~243); WS `/sync/exec` manual check @ 2061-2069.

**D. Other references**
- `core/config.py:282-285` `AGENT_ENV_BACKEND_URL` (= `BACKEND_URL`).
- `credentials_service.py:605-619` uses `AGENT_ENV_BACKEND_URL` to rewrite agent_api URLs (origin parity).
- `message_service.py:384-385` `EnvContext.auth_headers / env_auth_token`.
- Tests: `agents_session_context_test.py:79-84,171-178,227-228` (HMAC both directions); `agents_agent_api_test.py:1369,1416`; `agents_bundles_pbp_agent_api_test.py:282`; `test_opencode_mcp_bridge.py:51,198`.
- Generated SDK doc-comments: `frontend/src/client/sdk.gen.ts` (no runtime use).
- Runtime-materialized per-env copies under `backend/agent-environments/<uuid>/app/core/...` — deployed instances of the template, not separate source; rebuilt from `app_core_base`.

**Phase 0 verification checks**
- [ ] Confirm the env container calls **only** these backend paths: `/agent/tasks/*`, `/knowledge/query`,
      `/environments/{id}/{workspace-files-changed,prompt-file-changed,agent-api-reloaded}`, `/security-events/report`.
      (grep env-templates for `BACKEND_URL` and `Authorization` — nothing else may call back.)
- [ ] Confirm `agent_status.py`, `agent_api_public.py` (consumer proxy uses its own `agent_api` token), `agent_api.py`,
      `mcp_providers.py`, `mcp_consent.py` are **not** env-called (no migration there).
- [ ] Confirm no **non-env** caller relies on `get_current_user` accepting an env-shaped token (search for any code
      that mints `create_access_token` with `sub=<user_id>` and feeds it to `CurrentUser` routes deliberately — there
      should be none besides this env token).

---

### Phase 1 — Token minting with new claims + revocation field + migration

**Files & changes**

1. `backend/app/models/environments/agent_environment.py`
   - Add `auth_token_hash: str | None = Field(default=None, index=True, max_length=64)` to the table model.
   - Do **not** add it to public/read schemas (it is internal; never serialized to clients).

2. `backend/app/services/environments/environment_lifecycle.py` — `_generate_auth_token` (@ 2805)
   - New signature: `_generate_auth_token(self, user_id: UUID, env_id: UUID, agent_id: UUID) -> str`.
   - Mint via:
     ```
     security.create_access_token(
         subject=str(user_id),
         expires_delta=timedelta(days=settings.AGENT_ENV_TOKEN_EXPIRE_DAYS),
         extra_claims={"token_type": "agent_env", "aud": "agent_env",
                       "env_id": str(env_id), "agent_id": str(agent_id)},
     )
     ```
   - TTL: introduce `settings.AGENT_ENV_TOKEN_EXPIRE_DAYS` (config). **Recommendation: 30 days** (long enough to
     avoid churn since rotation happens on every rebuild/start anyway; short enough to bound a leak). Was 3650 days.
   - At the caller `_update_environment_config` (@ 1782): pass `env_id=environment.id, agent_id=environment.agent_id`;
     after setting `config["auth_token"]`, also set `environment.auth_token_hash = sha256(token).hexdigest()` and
     `flag_modified(environment, "config")`. This **rotates** the token on every configure (create/start/restart/rebuild),
     satisfying "rotate on rebuild."

3. `backend/app/core/config.py`
   - Add `AGENT_ENV_TOKEN_EXPIRE_DAYS: int = 30` near `AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS` (@ ~296).

4. **Alembic migration** (required — new column)
   - `alembic revision --autogenerate -m "add auth_token_hash to agent_environment"` (via Docker, per Makefile).
   - Adds nullable `auth_token_hash VARCHAR(64)` + index on `agent_environment`.
   - **Backfill (optional, recommended):** in the migration's `upgrade()`, leave it `NULL` for existing rows — the
     grace-window fallback (Phase 4) handles old envs by comparing against `config["auth_token"]` verbatim. (A data
     backfill that hashes existing `config["auth_token"]` is possible but unnecessary; rotation on next start sets it.)
   - `downgrade()` drops the column + index.

**Phase 1 verification**
- [ ] Minted token decodes with `audience="agent_env"` and carries `token_type/env_id/agent_id`.
- [ ] `auth_token_hash` is set on create/start/restart/rebuild and equals `sha256(config["auth_token"])`.
- [ ] HMAC role still works (session_context sign/verify) — the token value is still in `config["auth_token"]`.
- [ ] Container inbound auth still works (role 1) — `.env` still gets `AGENT_AUTH_TOKEN=<jwt>` verbatim.

---

### Phase 2 — `get_current_user` rejection + new scoped dependency

**Files & changes**

1. `backend/app/api/deps.py` — `get_current_user` (@ 35)
   - After `payload = jwt.decode(...)`, before `TokenPayload(**payload)` / `session.get(User, ...)`, add the
     `token_type == "agent_env"` → `HTTPException(401)` reject (see §4.1).

2. `backend/app/api/deps.py` — new scoped dep
   - Add `AgentEnvContext(SQLModel)` (fields: `environment`, `agent`, `owner`).
   - Add `_resolve_agent_env_context(...)` per §4.2 and `AgentEnvError` (mirroring `CLIAuthError`) → status mapping
     (invalid/revoked/expired/decode → 401; env_id/agent/path mismatch → 403; env/owner missing → 404).
   - Add `get_agent_env_context(token, db, x_agent_env_id)` + `AgentEnvContextDep`.
   - Add a WS variant `AgentEnvContextWSDep` **only if** any env→backend WS call needs it. (Per Phase 0, the env's
     only WS endpoint is `/sync/exec`, which is **backend→container**, not container→backend; the container does not
     open a backend WS. So a WS variant is likely **not needed** — confirm and omit if so.)

**Phase 2 verification**
- [ ] Unit: an env-shaped JWT (`token_type=agent_env`) on a `CurrentUser` route → 401.
- [ ] Unit: a real user JWT (no `token_type` / `token_type` absent) on `CurrentUser` → still works.
- [ ] Unit: `_resolve_agent_env_context` returns correct `(env, agent, owner)` for a valid env token + matching header.
- [ ] Unit: cross-env token (env_id claim ≠ header/path) → 403; revoked (hash mismatch) → 401.

---

### Phase 3 — Migrate env-called routes + reconcile the two existing env-auth mechanisms

**3a. Reconcile the two duplicate scoped deps into the new one**

- `backend/app/api/routes/knowledge.py` — delete `verify_agent_auth_token` (@ 16); change `POST /knowledge/query`
  (@ 123) to depend on `AgentEnvContextDep`. The handler currently receives an `AgentEnvironment`; update it to read
  `ctx.environment`. Knowledge queries must be scoped to `ctx.agent`/`ctx.owner`'s accessible knowledge sources
  (public + owner-private) — verify the query service already filters by the env's owner; if it filtered by the
  raw env before, pass `ctx.environment`.
- `backend/app/api/routes/environments.py` — delete `_verify_env_agent_auth` (@ 44) and the `_EnvFromAgentAuth`
  alias (@ 436); change the three callbacks (`workspace-files-changed` @ 474, `prompt-file-changed` @ 498,
  `agent-api-reloaded` @ 519) to `AgentEnvContextDep`. Keep the per-handler `if ctx.environment.id != id: 403`
  path check (the dep enforces header/claim match; path match stays in-handler).
- **Result:** one env-auth mechanism (`AgentEnvContextDep`) replaces both `verify_agent_auth_token` and
  `_verify_env_agent_auth`.

**3b. Migrate the vulnerable `CurrentUser` env-called routes**

`backend/app/api/routes/task_agent_api.py` — **all 11 routes** currently use `CurrentUser` (module docstring
admits it relies on the owner JWT). Migrate each to `AgentEnvContextDep` and re-scope:

| Route (method + path) | Line | Re-scope requirement |
|---|---|---|
| `POST /agent/tasks/create` | 100 | Create under `ctx.agent`/`ctx.owner`; ignore any caller-supplied owner. |
| `GET /agent/tasks/by-code/{short_code}` | 131 | Resolve task, then assert it belongs to `ctx.owner` (or `ctx.agent`'s team scope) → else 403/404. |
| `POST /agent/tasks/current/comment` | 155 | "current" task derives from the env's active session/agent — assert task↔`ctx`. |
| `POST /agent/tasks/current/status` | 182 | same |
| `GET /agent/tasks/current/details` | 215 | same |
| `POST /agent/tasks/current/subtask` | 241 | same; team topology check unchanged but anchored to `ctx.agent`. |
| `POST /agent/tasks/{task_id}/comment` | 277 | Load task by id, assert ownership/team-scope vs `ctx` → else 403/404. |
| `POST /agent/tasks/{task_id}/status` | 307 | same |
| `POST /agent/tasks/{task_id}/subtask` | 348 | same |
| `GET /agent/tasks/my-tasks` | 389 | Scope listing to `ctx.agent`/`ctx.owner` (was "task owner"). |
| `GET /agent/tasks/{task_id}/details` | 418 | Load + assert scope vs `ctx`. |

> **Critical re-scoping rule:** previously these routes used the owner identity from the JWT and trusted
> "agent identity derived from the task record." Now the authoritative scope is `ctx.agent`/`ctx.owner`. Every
> route that accepts a `task_id`/`short_code` MUST verify the resolved task belongs to the env's agent/owner before
> acting — otherwise a compromised container could still touch the owner's *other* tasks via the task tools. This is
> the per-route enforcement the plan mandates. Update the module docstring to describe the new scoped model.

`backend/app/api/routes/security_events.py`
- `POST /security-events/report` (@ 57) — migrate from `CurrentUser` to `AgentEnvContextDep`. The reporter is the
  env; attribute the security event to `ctx.environment`/`ctx.agent`/`ctx.owner`. Update docstring.
- `POST /security-events/` (@ 93) and `GET /security-events/` (@ 115) — **confirm caller.** The GET is legitimate
  owner/admin audit access (keep `CurrentUser`). The bare `POST /` should be checked: if env-called it migrates too;
  if owner/admin-called it stays. (Phase 0 flagged `POST /` as env-adjacent — resolve before editing.)

**3c. Env-core changes — send `X-Agent-Env-Id` on the newly-scoped calls**

The new dep binds via the `env_id` claim AND (for safety) the `X-Agent-Env-Id` header where present. The task tools
and security-events report currently send only the bearer. Update them to also send `"X-Agent-Env-Id": ENV_ID`
(the pattern `knowledge_query.py:202-204` already uses):

- `app_core_base/core/server/tools/agent_task_create_task.py` (header @ ~82), `agent_task_get_details.py`,
  `agent_task_update_status.py`, `agent_task_add_comment.py`, `agent_task_list_tasks.py`,
  `agent_task_create_subtask.py` — add `X-Agent-Env-Id: ENV_ID` to each `headers` dict. `ENV_ID` is already a
  module constant in these files (used in logging).
- `app_core_base/core/server/tools/mcp_bridge/task_server.py` — add the header to its request builder(s).
- `app_core_base/core/server/routes.py` security-event proxy (@ ~319) — add `X-Agent-Env-Id` to the forwarded headers.
- `mcp_bridge/knowledge_server.py` — align with `knowledge_query.py` (ensure it sends the header).

> **env-template = rebuild.** Changes under `app_core_base` only reach a container on **rebuild**. The new dep MUST
> therefore tolerate a **missing** `X-Agent-Env-Id` header by falling back to the `env_id` JWT claim (so not-yet-
> rebuilt containers still authenticate during the grace window). Once the header is present it must match the claim.
> The runtime-materialized copies under `backend/agent-environments/<uuid>/...` are regenerated from the template on
> rebuild — do not hand-edit them.

**Phase 3 verification**
- [ ] Only `AgentEnvContextDep` remains as the env-auth mechanism; `verify_agent_auth_token` and
      `_verify_env_agent_auth` are deleted and have no remaining references (grep).
- [ ] Every env→backend route (`/agent/tasks/*`, `/knowledge/query`, the three env callbacks, `/security-events/report`)
      uses `AgentEnvContextDep`.
- [ ] No env→backend route still uses `CurrentUser`.
- [ ] Each `task_id`/`short_code` route rejects tasks not belonging to `ctx`.
- [ ] Client regen not required (no public OpenAPI shape change to user-facing routes — env routes are not in the SDK;
      confirm none of the migrated routes were in the generated client).

---

### Phase 4 — Back-compat / rotation story (MANDATORY DECISION)

**Problem:** existing **running** environments already hold OLD-format tokens (plain owner JWT, no `token_type`,
`aud`, `env_id`, `agent_id`, 10-year exp) in their `.env`, and `auth_token_hash` is `NULL` for them. If we hard-reject
anything lacking the new claims, **every deployed env breaks instantly** (task tools, knowledge, callbacks all 401).

**Recommended strategy: grace window with dual-accept, then forced rotation.**

1. **Mint new-format tokens immediately** (Phase 1). Any env that is created/started/restarted/rebuilt after deploy
   gets the new token + `auth_token_hash` automatically (rotation on configure already happens on every lifecycle op).
2. **`get_current_user` rejects only NEW-format env tokens** (`token_type == "agent_env"`). An **old-format** env
   token still has `sub=owner_id` and **still resolves as the owner via `CurrentUser`** — i.e. the vulnerability
   persists for not-yet-rotated envs during the window. To close this, the `AgentEnvContextDep` must **accept
   old-format tokens during the grace window** (so legitimate old containers keep working) AND we must **force
   rotation** to flip them to new-format quickly.
3. **`AgentEnvContextDep` dual-accept during grace window:**
   - New-format token → full validation (claims + `audience` + hash).
   - Old-format token (no `token_type`/`aud`/`env_id`) → fall back to the **legacy verbatim compare**:
     require `X-Agent-Env-Id` header (knowledge already sends it; old task tools do **not**, so old task-tool calls
     would fail — acceptable only if we rotate fast, see below) OR compare `raw_token == env.config["auth_token"]`
     by locating the env. **Because old task tools send no `X-Agent-Env-Id`, the cleanest path is to FORCE-ROTATE.**
4. **Forced rotation (recommended over an indefinite grace window):**
   - On deploy, run a **one-time rotation sweep**: for every running `AgentEnvironment`, re-run the configure step
     (or a lightweight "re-issue token + re-sync `.env` + restart container") so it picks up a new-format token AND
     the rebuilt env-template (which sends `X-Agent-Env-Id`). This is the same machinery as
     `admin_agent_environments` bulk rebuild — reuse it.
   - Until an env is rotated+rebuilt, its old task-tool calls (no env-id header) will not authenticate under the new
     dep. **Therefore the grace window for old-format tokens should be paired with the rebuild, not relied upon
     long-term.** For the period between deploy and rebuild, the safest posture is: keep the old-format acceptance in
     `AgentEnvContextDep` limited to routes the **knowledge tool + callbacks** use (they send the header), and treat
     the task-tool gap as "rebuild required." Document this clearly in release notes.

**Decision & recommendation (explicit):**
> **Adopt forced rotation via a deploy-time bulk rebuild of all running environments** (reusing the admin bulk-rebuild
> path), combined with a **short grace window (e.g. one release / 7 days)** during which `AgentEnvContextDep`
> dual-accepts old-format tokens **that present `X-Agent-Env-Id`** (knowledge + callbacks). After the window, remove
> old-format acceptance from the dep so only new-format env tokens authenticate. Hard-rejecting old tokens in
> `get_current_user` is **not** done for old-format (they look like owner JWTs); the vulnerability for un-rotated
> envs is closed by the bulk rebuild, which should be run as part of the deploy, not left optional.

**Operational steps**
- [ ] Deploy backend (mint new-format, dep dual-accepts, `get_current_user` rejects new-format env tokens).
- [ ] Run the bulk env rebuild sweep (admin tooling) → all running envs get new-format token + new env-template.
- [ ] After the grace window, remove old-format acceptance from `AgentEnvContextDep` (a follow-up small PR).

**Phase 4 verification**
- [ ] A running env created before deploy continues to function for knowledge/callbacks during the grace window.
- [ ] After bulk rebuild, all running envs present new-format tokens with `X-Agent-Env-Id`; nothing relies on old format.
- [ ] HMAC session_context still verifies after rotation (the container's `AGENT_AUTH_TOKEN` and the backend's
      `config["auth_token"]` are rotated **together** in the same configure step — they never diverge mid-flight;
      confirm a session started just before rotation is not left with a stale HMAC key — sessions are short-lived and
      the sign/verify happen within one stream, so a rebuild between sessions is safe; document this).

---

### Phase 5 — Tests

See the dedicated **Test Matrix** (§9-prefixed section below) for the full required coverage. Tests are API-level
per `backend/tests/README.md` (no direct DB access; scenario-based). Place under `backend/tests/api/agents/` (e.g.
`agents_env_token_scoping_test.py`) and reuse existing env/session fixtures
(`agents_session_context_test.py` is the closest existing example for env-token handling).

---

## 6. Frontend Implementation

**None.** This is an internal machine-token hardening change. No user-facing routes, components, or client SDK
shapes change. Do **not** run `generate-client.sh` unless Phase 3 inadvertently alters a user-facing route signature
(it should not — the migrated routes are env-only and not part of the generated client; verify).

---

## 7. Database Migrations

- **Required: yes.** One Alembic migration adds `agent_environment.auth_token_hash VARCHAR(64) NULL` + a btree index.
- Naming: autogenerate, message `"add auth_token_hash to agent_environment"`; review the generated file (autogen often
  picks up unrelated drift — trim to just the column + index).
- `upgrade()`: `op.add_column` + `op.create_index`. No backfill needed (NULL handled by grace-window fallback).
- `downgrade()`: `op.drop_index` + `op.drop_column`.
- Apply via `make migrate` (Docker). Note the repo has a history of multiple alembic heads — set `down_revision` to
  the current single head and run `alembic heads` to confirm you are not adding a new head.

---

## 8. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Env token used on a `CurrentUser` route (e.g. `GET /credentials/{id}/with-data`) | 401 (reject in `get_current_user`) |
| Env token, valid, on its own scoped route | 200, scoped to its agent/env |
| Env token whose `env_id` claim ≠ `X-Agent-Env-Id` header or path `{id}` | 403 |
| Env token whose `agent_id` claim ≠ `env.agent_id` | 403 |
| Env token after rotation (old hash) presented to a rotated env | 401 (hash mismatch = revoked) |
| Env token for a deleted env | 404 |
| Expired env token | 401 |
| Missing/empty bearer on scoped route | 401 (fail-closed) |
| Old-format env token during grace window, with `X-Agent-Env-Id` | accepted (dual-accept) until window closes |
| Old-format env token after grace window | 401 |
| Real user / CLI / desktop token on `CurrentUser` routes | unaffected (no `token_type=agent_env`) |
| CLI token on a scoped env route | 401 (no `aud=agent_env` / `token_type=agent_env`) |

Edge cases to guard:
- The global `settings.AGENT_AUTH_TOKEN` fallback (`config.py:300`) — confirm it is not silently used as a real env
  token anywhere; if a dev path uses it, it will lack the new claims and be rejected by the scoped dep (intended).
- Container-side fail-open paths (`verify_auth_token` when token unset; `_verify_hmac` when token unset) are
  **pre-existing** and out of scope, but **flag them in the PR** as a follow-up (they weaken roles 1 and 3 in
  misconfigured/dev environments).

---

## 9. Test Matrix (REQUIRED)

API-level tests (`backend/tests/api/...`), scenario-based per `backend/tests/README.md`.

**(a) Env token REJECTED by generic `CurrentUser` routes**
- [ ] Env token → `GET /credentials/{id}/with-data` → **401/403** (must NOT return decrypted data). *Primary regression guard.*
- [ ] Env token → `GET /credentials/` (list all owner credentials) → 401.
- [ ] Env token → `GET /agents/` and access to **another** agent's detail → 401.
- [ ] Env token → sessions / files / AI-credentials `CurrentUser` routes → 401.
- [ ] Env token of **agent A's env** → any attempt to act as owner on agent B's resources → 401 (blocked at the dep,
      since `CurrentUser` rejects it entirely).

**(b) Env token ACCEPTED on scoped env routes for its OWN agent/env**
- [ ] Env token + matching `X-Agent-Env-Id` → `POST /agent/tasks/create` → 200, task created under the env's agent.
- [ ] Env token → `POST /knowledge/query` → 200.
- [ ] Env token → the three env callbacks (`workspace-files-changed`, `prompt-file-changed`, `agent-api-reloaded`) → 200.
- [ ] Env token → `POST /security-events/report` → 200, attributed to the env's agent/owner.

**(c) Env token DENIED cross-agent / cross-env on scoped routes**
- [ ] Env A token + `X-Agent-Env-Id` of env B → 403.
- [ ] Env A token attempting `GET /agent/tasks/{task_id}/details` for a task owned by a **different** owner/agent → 403/404.
- [ ] Env A token attempting `POST /agent/tasks/{task_id}/comment` on env B's owner's task → 403/404.
- [ ] Rotated env: a token minted before rebuild (old hash) → 401 (revocation/rotation gate).

**(d) Real user / CLI / desktop tokens still work**
- [ ] Real user JWT → `GET /credentials/{id}/with-data` (own credential) → 200.
- [ ] Real user JWT → all previously-working `CurrentUser` routes → unchanged.
- [ ] CLI token (`token_type=cli`) → its CLI routes → 200; → a scoped env route → 401; → `CurrentUser` → 404 (existing behavior).
- [ ] Desktop token (`client_kind=desktop`) → its routes → 200 (revocation check unaffected).

**(e) Back-compat / grace window**
- [ ] Old-format env token (no claims) + `X-Agent-Env-Id` → accepted during grace window on knowledge/callbacks.
- [ ] Old-format env token after grace-window removal → 401.
- [ ] New-format token + missing header (not-yet-rebuilt container) → accepted via `env_id` claim fallback.

---

## 10. Integration Points

- **Agent Environment Core** (`docs/agents/agent_environment_core/agent_environment_core.md`, "Authentication" rule)
  — update the doc: token is no longer a "plain owner JWT, 10-year"; it now carries `token_type/aud/env_id/agent_id`
  and a sane TTL, is rotated on rebuild, and is server-side revocable via `auth_token_hash`.
- **Agent Credentials** (`owner_identity_token` precedent) — this plan mirrors that audience-restricted, backend-only
  verified design.
- **CLI integration** — `CLIContextDep` is the structural template for `AgentEnvContextDep`.
- **Admin Agent Environments** — the bulk-rebuild path is reused for the Phase-4 forced-rotation sweep.
- **Knowledge / Tasks / Security Events** — routes migrate to the unified scoped dep.
- **Client regen:** not required (no user-facing SDK change) — verify and skip.

---

## 11. Notes, Risks & Open Questions

- **`platform_helper.py` example script** (`platform-knowledge-env/.../scripts/examples/platform_helper.py`) is a
  generic `{BACKEND_URL}{path}` helper that sends `Bearer {AGENT_AUTH_TOKEN}`. After this change it can only reach
  routes covered by `AgentEnvContextDep`. If it documents/encourages calling arbitrary `CurrentUser` routes, update
  the example + its docstring to the scoped reality (or it will start returning 401). **This is intended hardening,
  but the example must be corrected so it does not mislead agent authors.**
- **Container fail-open paths** (`routes.py::verify_auth_token`, `active_session_manager.py::_verify_hmac`) are
  pre-existing and out of scope; flag as follow-up.
- **Global `AGENT_AUTH_TOKEN` Settings default** (`config.py:300`) — confirm its actual usage; document or remove.
- **HMAC key rotation timing** — token is rotated together with the container `.env` in one configure step, so
  sign/verify never use mismatched keys within a session; confirm no long-lived session spans a rotation.
- **Open question:** should `AgentEnvContextDep` *require* `X-Agent-Env-Id` (defense-in-depth) or rely solely on the
  `env_id` JWT claim? Recommendation: **require it once all env templates are rebuilt** (post-grace-window), accept
  its absence (claim-only) during the window. Decide and record before closing the grace window.
- **Open question:** keep storing the raw token in `config["auth_token"]` long-term, or move to hash-only once roles
  1+3 are refactored to read from a dedicated field? Out of scope here; the hash column makes that future change easy.

---

## 12. Summary Checklist

**Phase 0 — Enumeration (gate)**
- [ ] Produce and review the full token-consumer enumeration (§5 Phase 0 A–D) as a checked-in artifact.
- [ ] Confirm the complete env→backend route list and that nothing else calls back.
- [ ] Confirm no non-env caller depends on `get_current_user` accepting an env-shaped token.

**Phase 1 — Mint + model + migration**
- [ ] Add `auth_token_hash` column to `AgentEnvironment` + Alembic migration (+ index).
- [ ] Rewrite `_generate_auth_token` to add `token_type/aud/env_id/agent_id` + sane TTL (`AGENT_ENV_TOKEN_EXPIRE_DAYS=30`).
- [ ] Set `auth_token_hash` on every configure; rotate on rebuild/start/restart.
- [ ] Add `AGENT_ENV_TOKEN_EXPIRE_DAYS` to config.

**Phase 2 — Reject + scoped dep**
- [ ] Reject `token_type=="agent_env"` in `get_current_user` (401) — the load-bearing fix.
- [ ] Add `AgentEnvContext` + `_resolve_agent_env_context` + `get_agent_env_context` + `AgentEnvContextDep`.

**Phase 3 — Migrate + reconcile**
- [ ] Delete `verify_agent_auth_token` (knowledge.py) and `_verify_env_agent_auth` (environments.py); repoint to `AgentEnvContextDep`.
- [ ] Migrate all 11 `task_agent_api.py` routes + `security-events/report` to `AgentEnvContextDep` with per-route scope checks.
- [ ] env-core: add `X-Agent-Env-Id` to task tools, mcp task bridge, and security-event proxy headers.
- [ ] Verify one and only one env-auth mechanism remains.

**Phase 4 — Back-compat**
- [ ] Implement grace-window dual-accept in `AgentEnvContextDep` (old-format with `X-Agent-Env-Id`).
- [ ] Run deploy-time bulk env rebuild (reuse admin bulk-rebuild) to force rotation + new template.
- [ ] Follow-up PR to remove old-format acceptance after the window.

**Phase 5 — Tests**
- [ ] Implement the full Test Matrix (§9 a–e).

**Docs**
- [ ] Update `agent_environment_core.md` "Authentication" rule and `agent_credentials.md` cross-reference.

---

*Plan author note:* The single non-negotiable change is the `get_current_user` reject (Phase 2). Everything else
(scoping, rotation, revocation, reconciliation) is necessary to keep legitimate env traffic working and to reduce
the blast radius — but without the reject, an env token remains a full owner credential.
