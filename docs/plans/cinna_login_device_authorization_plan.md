# Implementation Plan: `cinna login` — Account Device-Authorization Flow (RFC 8628)

**Feature name:** `cinna-login-device-authorization`
**Scope:** cinna-core (platform backend) + one browser authorization screen. The CLI half (`cinna login` in the separate `cinna-cli` repo) **already exists** and is NOT touched here.
**Authoritative requirements:** `docs/plans/cinna_login_device_authorization_requirements.md` (read in full — exact contract shapes, status spellings, security checklist).

---

## 1. Overview

`cinna login` lets the cinna-cli refresh an expired **account CLI token** (`token_type="cli-account"`) through a browser approval instead of pasting a setup token. It is an OAuth 2.0 Device Authorization Grant (RFC 8628), but the poll endpoint standardizes on **HTTP 200 + `status`** (not RFC 400 + `error`) to match the rest of `/cli/account/*` and the existing CLI implementation.

Core capabilities:
- **`POST /api/v1/cli/account/login/start`** (unauthenticated) — CLI begins a device-login request; returns `device_code` + `user_code` + verification URLs + `interval`/`expires_in`.
- **`POST /api/v1/cli/account/login/poll`** (unauthenticated) — CLI polls with `device_code`; always 200 with one of `authorization_pending` / `slow_down` / `authorized` / `access_denied` / `expired_token`.
- **Browser authorization screen** (`/device`, platform-user-authenticated) — shows `user_code` / `machine_name` / `machine_info`, with Approve / Reject. Approving mints a fresh account CLI token (identical scope/identity to `POST /cli-setup/account/{token}`) and flips poll to `authorized`; rejecting flips to `access_denied`.

### High-level flow

```
 CLI                         Backend                          Browser (signed-in user)
  │  POST login/start          │                                    │
  │ ─────────────────────────▶ │  create CLIDeviceLoginRequest      │
  │                            │  (status=pending, store hashes)    │
  │ ◀───────────────────────── │  {device_code, user_code, uris}    │
  │  print user_code, open ───────────────────────────────────────▶ │ GET /device?code=USER-CODE
  │  verification_uri_complete │                                    │ (auth-gated; login redirect)
  │                            │  GET login/request?user_code= ◀──── │ fetch display metadata
  │                            │ ─────────────────────────────────▶ │ show machine_name/info + code
  │  POST login/poll (loop)    │                                    │ user clicks Approve
  │ ─────────────────────────▶ │  pending / slow_down               │
  │ ◀───────────────────────── │                                    │ POST login/approve {user_code}
  │                            │  mint cli-account token, flip ◀──── │  (require_developer)
  │  POST login/poll           │  status=authorized                 │
  │ ─────────────────────────▶ │  return account_token (single-use) │
  │ ◀───────────────────────── │  status=authorized + account_token │
  │  write .cinna/account.json │  flip status=consumed              │
```

---

## 2. Architecture Overview

This feature is a structural clone of the **desktop OAuth consent flow** (`desktop_auth.py` / `desktop_auth_service.py` / `desktop_oauth_client.py`) with three differences:

1. The browser side does **not** redirect back to a native app — the CLI is polling, so the approval just flips a server-side status. The browser shows a terminal "you can return to your terminal" screen.
2. The minted credential is an **account CLI token** (reusing `AccountCLIService.exchange_account_setup_token`'s minting block, factored into a shared helper), not a desktop access/refresh pair.
3. The poll endpoint returns **200 + `status`** for every flow state.

### Components

| Layer | New / reused | Location |
|---|---|---|
| Model | **new** `CLIDeviceLoginRequest` | `backend/app/models/cli/cli_device_login.py` |
| Migration | **new** | `backend/app/alembic/versions/<hash>_add_cli_device_login_request.py` |
| Service | **new** `DeviceLoginService` | `backend/app/services/cli/device_login_service.py` |
| Shared minting helper | **refactor** out of `AccountCLIService.exchange_account_setup_token` | `backend/app/services/cli/account_cli_service.py` |
| Rate limiter | **reuse** `_RateLimiter` (copy/import) | `backend/app/services/cli/account_api_proxy_service.py:90` |
| Routes (2 CLI, public) | **new** on the **public** `setup_router` (prefix `/api/cli-setup`) — see §5 note | `backend/app/api/routes/cli.py` |
| Routes (3 browser) | **new** | `backend/app/api/routes/cli.py` |
| Security events | **new** constants | `backend/app/models/events/security_event.py` |
| Frontend screen | **new** route + reuse a device-variant consent component | `frontend/src/routes/device.tsx` |

### Integration points
- Minted token is a `CLIToken(token_type="cli-account")` — works unchanged on `AccountCLIContextDep` and `POST /api/v1/cli/account/agents/{id}/mint` (the requirements' acceptance test step 5).
- Reuses `CLIAuthService.create_cli_jwt` / `hash_token`, `CLI_TOKEN_EXPIRY_DAYS=7`, `SecurityEventService.create_event`, `_get_platform_url`, `_client_ip`, `settings.FRONTEND_HOST`.

---

## 3. Data Models

### New table: `cli_device_login_request`

File: `backend/app/models/cli/cli_device_login.py` (re-export from `backend/app/models/__init__.py`).

Purpose: server-side state machine for one device-login attempt. Holds **hashes** of the secrets (never plaintext `device_code`), the human `user_code` for browser lookup, the approver binding, and the minted-token linkage.

| Field | Type | Constraints / default | Notes |
|---|---|---|---|
| `id` | `uuid.UUID` | PK, `default_factory=uuid4` | |
| `device_code_hash` | `str` | `unique=True, index=True` | SHA-256 of the raw `device_code`. CLI echoes the raw value; we look up by hash (mirrors `CLIToken.token_hash`). **Never store plaintext.** |
| `user_code` | `str` | `max_length=16, index=True` | Human code, **normalized uppercase, no dashes** for storage/lookup (e.g. stored `WX7K9Q2P`, displayed `WX7K-9Q2P`). Indexed for the browser screen lookup. Not unique by column — collisions handled by retry at generation (see §4). |
| `status` | `str` | `max_length=20, default="pending"`, `index=True` | One of: `pending`, `approved`, `denied`, `expired`, `consumed`. See §4 transitions. |
| `machine_name` | `str` | `max_length=100` | Display + becomes the minted `CLIToken.name`. |
| `machine_info` | `str \| None` | `max_length=200, default=None` | Display + minted `CLIToken.machine_info`. |
| `approved_by_user_id` | `uuid.UUID \| None` | `foreign_key="user.id", ondelete="CASCADE", default=None` | Set at approval. Binds the request to whoever approved (requirements §"Authorization screen behavior"). |
| `minted_token_id` | `uuid.UUID \| None` | `foreign_key="cli_token.id", ondelete="SET NULL", default=None` | The `CLIToken` minted at approval. SET NULL so revoking/deleting the token row doesn't orphan-delete this audit row. |
| `account_token_jwt` | `str \| None` | `default=None` (Text) | **Transient** raw JWT, stored only between approval and the first successful `authorized` poll, then nulled (Decision 2(a)). See §4 / §13. |
| `client_ip` | `str \| None` | `max_length=64, default=None` | Source IP at `start` (audit). |
| `last_polled_at` | `datetime \| None` | `default=None`, tz-aware | Drives `slow_down` (poll faster than `interval`). |
| `created_at` | `datetime` | `default_factory=now(UTC)`, tz-aware | |
| `expires_at` | `datetime` | tz-aware, non-null | `created_at + DEVICE_LOGIN_EXPIRY` (default 15 min / 900 s). |

**Pydantic schemas** (same file, no `table=True`):
- `DeviceLoginStartRequest` — `{machine_name: str, machine_info: str | None = None}`.
- `DeviceLoginStartResponse` — `{device_code, user_code, verification_uri, verification_uri_complete, interval, expires_in}` (RFC 8628 field names exactly).
- `DeviceLoginPollRequest` — `{device_code: str}`.
- `DeviceLoginPollResponse` — `{status: str, account_token: str | None = None, platform_url: str | None = None, frontend_url: str | None = None, machine_name: str | None = None}`. (FastAPI `exclude_none` — only `authorized` carries the extras; the requirements' table shows the other statuses carry no extra fields.)
- `DeviceLoginRequestPublic` — browser display metadata: `{user_code: str, machine_name: str, machine_info: str | None, status: str}`. **No `device_code`, no token, no IP, no approver.**

### Lifecycle states

```
pending ──approve──▶ approved ──first authorized poll──▶ consumed   (terminal)
   │  └─reject──▶ denied                                             (terminal)
   └─expires_at<now (lazy, on any read)──▶ expired                   (terminal)
```
Once `approved`/`denied`/`expired`/`consumed`, the `device_code` yields no further token (Decision 3 — single-use / terminal-state invalidation).

---

## 4. Behavior & State Machine (service logic)

All in `DeviceLoginService` (`backend/app/services/cli/device_login_service.py`). Static methods, mirrors `DesktopAuthService`. Module constants:

```
DEVICE_LOGIN_EXPIRY_SECONDS = 900   # expires_in
DEVICE_LOGIN_POLL_INTERVAL  = 5     # interval
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L — unambiguous
```

### `start(db, machine_name, machine_info, request) -> DeviceLoginStartResponse`
1. **Per-IP rate limit** (`_RateLimiter.check(ip, START_LIMIT_PER_MIN=10)`) → if throttled raise `HTTPException(429)`.
2. `device_code = secrets.token_urlsafe(32)` (≥256-bit entropy, well over the 128-bit floor).
3. `user_code` = generate 8 chars from `USER_CODE_ALPHABET`; **collision-retry**: re-generate (up to ~5 tries) while a non-terminal (`pending`/`approved`) request with the same normalized `user_code` exists. Store normalized (uppercase, no dash); display dashed `XXXX-XXXX`.
4. Insert `CLIDeviceLoginRequest(device_code_hash=hash_token(device_code), user_code=<normalized>, status="pending", machine_name, machine_info, client_ip=_client_ip(request), expires_at=now+DEVICE_LOGIN_EXPIRY_SECONDS)`. Commit.
5. Emit `CLI_DEVICE_LOGIN_STARTED` SecurityEvent? — **No** (start is unauthenticated, no user yet; audit happens at approval where the user is known). Optionally log at INFO.
6. Build URLs from `_get_platform_url(request)` and `settings.FRONTEND_HOST`:
   - `verification_uri = <frontend>/device`
   - `verification_uri_complete = <frontend>/device?code=<dashed user_code>`
   - (frontend resolution: use the same `_get_platform_url` dev/prod logic so a localhost dev box returns a reachable browser URL.)
7. Return `{device_code (raw), user_code (dashed), verification_uri, verification_uri_complete, interval=DEVICE_LOGIN_POLL_INTERVAL, expires_in=DEVICE_LOGIN_EXPIRY_SECONDS}`.

### `poll(db, device_code, request) -> DeviceLoginPollResponse`
Always returns HTTP 200 (the route never raises for flow states).
1. **Per-IP rate limit** (`POLL_IP_LIMIT_PER_MIN=60`) — defense against a non-`device_code`-keyed flood. If throttled, return `status="slow_down"` (still 200).
2. Look up by `hash_token(device_code)`. **Not found → `expired_token`** (unknown == expired; do not distinguish, anti-enumeration).
3. **Lazy expiry:** if `expires_at < now` and status is not already terminal, flip `status="expired"`, commit. Then if status is `expired` → return `expired_token`.
4. **Per-`device_code` slow_down:** if `last_polled_at` is set and `now - last_polled_at < interval`, return `status="slow_down"` (do not update `last_polled_at`, do not advance). Otherwise stamp `last_polled_at = now`, commit.
5. Dispatch on `status`:
   - `pending` → `authorization_pending`.
   - `denied` → `access_denied`.
   - `consumed` → `expired_token` (already used once; single-use — Decision 3).
   - `expired` → `expired_token`.
   - `approved` → **mint already done at approval (Decision 2(a))**: return `status="authorized"`, `account_token = row.account_token_jwt`, plus `platform_url=_get_platform_url(request)`, `frontend_url=settings.FRONTEND_HOST.rstrip("/")`, `machine_name=row.machine_name`. Then flip `status="consumed"`, **null `account_token_jwt`**, commit. (Single-use: a second poll now sees `consumed` → `expired_token`.)

### `get_request_for_display(db, user_code) -> DeviceLoginRequestPublic | None`
Browser metadata endpoint. Normalize input `user_code` (uppercase, strip dashes). Look up the newest non-`consumed` request with that `user_code`. Apply lazy-expiry. Return `DeviceLoginRequestPublic{user_code (dashed), machine_name, machine_info, status}` or `None` (404 at route) if unknown.

### `approve(db, user, user_code, request) -> None`
1. Normalize `user_code`. Load the request; `None`/terminal/expired → raise `DeviceLoginError("not_found"|"expired"|"already_resolved")` (route → 404/409 as appropriate, existence-leak-safe).
2. Lazy-expire check; if expired → raise.
3. Status must be `pending`; else `already_resolved` (409).
4. **Mint the account token now (Decision 2(a))** via the shared helper `AccountCLIService.mint_account_cli_token(db, owner_id=user.id, machine_name=row.machine_name, machine_info=row.machine_info, request=request)` (see §5 refactor). This returns `(jwt_value, cli_token)` and emits `CLI_ACCOUNT_TOKEN_CREATED` (identical audit to the setup-token path — requirements §"The token that comes back").
5. Set `row.status="approved"`, `row.approved_by_user_id=user.id`, `row.minted_token_id=cli_token.id`, `row.account_token_jwt=jwt_value`. Commit.
6. Emit `CLI_DEVICE_LOGIN_APPROVED` SecurityEvent (`user_id=user.id`, details `{user_code, machine_name, machine_info, minted_token_id, ip}`).

> **Why mint at approval, not at poll (Decision 2):** approval is the single moment the platform user is authenticated and known — minting there keeps the approver binding atomic with the status flip, and reuses the existing minting block verbatim. The raw JWT cannot be re-derived from `token_hash`, so we stash it transiently in `account_token_jwt` (option 2(a)) and null it on the first `authorized` poll. The row is short-lived (≤15 min) and single-use; the window where the raw JWT sits at rest is bounded by `expires_at` and ends at first poll. (Alternative 2(b) — mint at poll time from an `approved` flag — was rejected: it would require carrying the approver id and re-running the minting path on an unauthenticated request, duplicating the can-mint logic outside an authenticated context.)

### `reject(db, user, user_code, request) -> None`
Load + validate as in `approve` (must be `pending`). Set `status="denied"`. Commit. Emit `CLI_DEVICE_LOGIN_REJECTED` (`user_id=user.id`). No token minted.

### `DeviceLoginError(reason, message)`
Mirror `CLIAuthError`/`DesktopAuthError` — carries a `reason` enum (`"not_found"`, `"expired"`, `"already_resolved"`) the route maps to HTTP codes.

---

## 5. Backend Implementation

### Shared minting helper (refactor — DRY)

Refactor the minting block (currently `account_cli_service.py:199-238`) out of `exchange_account_setup_token` into a reusable static method so both the setup-token exchange and the device-approval path call it:

```
AccountCLIService.mint_account_cli_token(
    db, *, owner_id: uuid.UUID, machine_name: str,
    machine_info: str | None, request: Request,
) -> tuple[str, CLIToken]
```
- Does exactly: new `cli_token_id`, `cli_expires_at = now + timedelta(days=CLI_TOKEN_EXPIRY_DAYS)`, `create_cli_jwt(..., owner_id=owner_id, token_type="cli-account")`, `hash_token`, `prefix=jwt_value[:12]`, insert `CLIToken(agent_id=None, owner_id, name=machine_name, token_hash, prefix, token_type="cli-account", machine_info, expires_at)`, **commit**, emit `CLI_ACCOUNT_TOKEN_CREATED`. Returns `(jwt_value, cli_token)`.
- `exchange_account_setup_token` is rewritten to: validate setup token → `mint_account_cli_token(...)` → mark setup token used → return the bootstrap dict. (Behavior unchanged; one commit boundary preserved — keep the existing single commit by having the helper accept the setup-token marking, OR mark-used before calling and commit inside the helper. Simplest: mark `setup_token.is_used=True; db.add(setup_token)` **before** calling the helper so the helper's single `commit()` persists both. Document this ordering in the helper docstring.)

This is the only change to `account_cli_service.py` beyond adding the new method.

### Routes — placement note (important)

The two CLI endpoints must be **unauthenticated**. The authenticated account routes live on `router = APIRouter(prefix="/cli")` and are reachable via `AccountCLIContextDep`. To get the paths the requirements mandate (`/api/v1/cli/account/login/*`) while staying unauthenticated, add them to the **`router` (prefix `/cli`)** in `cli.py` but with **no auth dependency** (they take only `SessionDep` + `Request`, like `desktop_auth.py`'s public `/authorize` and `/token`). The path becomes `/api/v1/cli/account/login/start` — matching the contract. (Do **not** put them on `setup_router` whose prefix is `/api/cli-setup`; that would give the wrong path.)

> If the project prefers all-unauthenticated CLI endpoints to live on a clearly public sub-router, create `login_router = APIRouter(prefix="/cli/account/login", tags=["cli"])`, register it in `api/main.py` next to `cli.router`, and mount the 5 endpoints there. Either way the final paths must be `/api/v1/cli/account/login/{start,poll,request,approve,reject}`.

### API Routes (all in `cli.py`)

| Method & path | Auth | Body / query | Response | Codes |
|---|---|---|---|---|
| `POST /cli/account/login/start` | **none** | `DeviceLoginStartRequest` | `DeviceLoginStartResponse` | 200; 429 (per-IP throttle); 422 (validation) |
| `POST /cli/account/login/poll` | **none** | `DeviceLoginPollRequest` | `DeviceLoginPollResponse` | **always 200** (flow states in `status`); 422 only on malformed body |
| `GET /cli/account/login/request?user_code=` | **none** (display metadata only) | query `user_code` | `DeviceLoginRequestPublic` | 200; 404 (unknown/consumed) |
| `POST /cli/account/login/approve` | **`CurrentUser`** + `require_developer` | `{user_code: str}` | `Message` / 204 | 204; 401; 403 (not developer); 404 (unknown/expired); 409 (already resolved) |
| `POST /cli/account/login/reject` | **`CurrentUser`** + `require_developer` | `{user_code: str}` | `Message` / 204 | 204; 401; 403; 404; 409 |

Route handlers are thin: catch `DeviceLoginError` and map `reason` → status (`not_found`→404, `expired`→404, `already_resolved`→409). The poll handler must **never** let a `DeviceLoginError` escape as non-200 — poll returns flow states only.

**Developer gate (Decision 6):** approve/reject require `require_developer` (reuse the existing `RoleService.require_developer`, mirroring `_require_developer_account` and `create_account_setup_token`). A demoted user gets 403. This matches that the account token is only useful to builders, and satisfies "the request is bound to whoever approves it" (the approver must hold the developer role). Document the 403 on the screen as a clear message.

### Service layer summary
- `DeviceLoginService` — §4 (start/poll/get_request_for_display/approve/reject + `DeviceLoginError`). Owns its own `_RateLimiter` instance (process-local, like `AccountApiProxyService._rate_limiter`). Import `_RateLimiter` from `account_api_proxy_service` or lift it to a shared `backend/app/services/cli/_rate_limiter.py` if you prefer not to couple modules (recommend lifting — it's now used by two services).
- `AccountCLIService.mint_account_cli_token` — shared minting helper (above).

### Background tasks
None required. Expiry is **lazy-on-read** (Decision 8) — any `poll` / `get_request_for_display` / `approve` / `reject` flips `expired` when `expires_at < now`. Optionally piggyback row deletion onto the existing setup-token cleanup scheduler later (out of scope; see §12).

---

## 6. Frontend Implementation

### Route: `frontend/src/routes/device.tsx` (public-but-auth-gated)
- File-based route `/device`. `validateSearch`: `{ code: z.string().optional() }`.
- `beforeLoad`: `await ensureSessionValid("/device?code=<code>")` — redirects to login preserving the target (same pattern as `desktop-auth/consent.tsx`). After login the user lands back here signed in.
- Reads `?code=` (the dashed `user_code`). If absent, render an input box prompting the user to type the code the CLI printed (anti-phishing confirmation).

### Component: a device-variant consent screen
`NativeAuthConsentPage` is close but **redirect-oriented** (it does `window.location.href = redirect_to` on success). The device flow has no redirect — the CLI is polling. Two options:
- **(Recommended)** New `DeviceLoginConsentPage` component (`frontend/src/components/Auth/DeviceLoginConsentPage.tsx`) modeled on `NativeAuthConsentPage` but:
  - fetches via `GET /cli/account/login/request?user_code=` (display: `machine_name`, `machine_info`, `user_code`, `status`);
  - shows the `user_code` prominently so the user confirms it matches the CLI;
  - Approve → `POST /cli/account/login/approve {user_code}`; Reject → `POST .../reject`;
  - on success shows a terminal "Approved — return to your terminal, `cinna login` will finish automatically" card (no redirect, no `window.close` reliance);
  - on 403 shows "Your account needs the developer role to authorize CLI access."
- Reuse the shadcn `Card` layout, `ShieldCheck`/`ShieldX`/`CheckCircle2` icons, and the "Signed in as {email}" block from `NativeAuthConsentPage` verbatim.

### State management
- React Query: `useQuery(["device-login-request", userCode], () => CliService.getDeviceLoginRequest({ userCode }))`; `useMutation` for approve/reject. On 401/403 from the mutation, `redirectToLoginPreservingTarget()` (same as existing component).
- No localStorage beyond the existing `access_token`.

### User flows
- **Happy path:** CLI prints `WX7K-9Q2P` + opens `/device?code=WX7K-9Q2P` → (login if needed) → screen shows machine name/info + code → Approve → "return to terminal" → CLI's next poll returns `authorized`.
- **Code typed manually:** `/device` with no `?code` → input box → submit → same screen.
- **Reject:** Reject → "Authorization denied" card → CLI poll returns `access_denied`.
- **Expired:** if `status="expired"` on load, show "This login request has expired — run `cinna login` again."
- **Not a developer:** Approve → 403 → inline message.

### Client regeneration
After backend routes land, run `source ./backend/.venv/bin/activate && make gen-client` (or `bash scripts/generate-client.sh`) to regenerate `frontend/src/client/`. The new methods land under `CliService` (tag `cli`). Reference them as `CliService.deviceLoginStart` etc. — never hand-edit the client.

---

## 7. Database Migration

File: `backend/app/alembic/versions/<hash>_add_cli_device_login_request.py`.

- **`down_revision`:** current single head is **`3974f541ab0b`**. Verify with `docker compose exec backend alembic heads` before generating — if the repo has gained heads since this plan, set `down_revision` to the actual current head (the repo has historically had multi-head situations; do not assume).
- **Upgrade:** `op.create_table("cli_device_login_request", ...)` with the columns from §3. Use `sa.DateTime(timezone=True)` for `created_at` / `expires_at` / `last_polled_at`.
- **Indexes / constraints:**
  - unique index on `device_code_hash` (`ix_cli_device_login_request_device_code_hash`, unique=True);
  - btree index on `user_code` (`ix_..._user_code`);
  - btree index on `status`;
  - FK `approved_by_user_id → user.id` ON DELETE CASCADE;
  - FK `minted_token_id → cli_token.id` ON DELETE SET NULL.
- **Downgrade:** `op.drop_table("cli_device_login_request")` (drops its indexes/FKs).
- Generate via `make migration` (autogenerate), then **review** the generated file — autogenerate often drifts on `server_default`, FK `ondelete`, and tz-aware columns; hand-correct to match §3.

---

## 8. Security Architecture (checklist mapping)

Direct mapping of the requirements' Security checklist → concrete plan elements:

| Requirement | Plan element |
|---|---|
| `device_code` ≥128-bit entropy | `secrets.token_urlsafe(32)` (~256-bit) |
| `device_code` single-use | First `authorized` poll flips `status="consumed"` + nulls `account_token_jwt`; subsequent polls → `expired_token` (§4) |
| `device_code` server-side expiry | `expires_at` column; lazy-expire on every read (§4 step 3 / §8) |
| `device_code` invalidated after terminal poll | `consumed`/`denied`/`expired` are terminal; never re-yield a token (§3 lifecycle, §4 dispatch) |
| `device_code` never stored plaintext | Only `device_code_hash` (SHA-256) stored; CLI echoes raw, looked up by hash (mirrors `CLIToken.token_hash`) |
| `user_code` short/human/typeable | 8 chars, unambiguous alphabet (no `0/O/1/I/L`), dashed display |
| `user_code` rate-limited + expiring | Shares the row's `expires_at`; browser metadata + approve/reject all lazy-expire; per-IP throttle on the surrounding endpoints |
| Rate-limit `poll` per `device_code` + enforce `slow_down` | `last_polled_at` per row; faster-than-`interval` → `slow_down` (§4 step 4) |
| `start`/`poll` unauthenticated → per-IP rate limit + short expiries | `_RateLimiter` keyed by `_client_ip` on both (START 10/min, POLL 60/min); 15-min `expires_at` |
| Scope minted token exactly as setup token | Reuses `mint_account_cli_token` (the exact setup-token minting block) → `token_type="cli-account"`, no broader scope |
| Log `machine_name`/`machine_info` against the token | `CLI_ACCOUNT_TOKEN_CREATED` (in the shared helper) records `machine_name`; `CLI_DEVICE_LOGIN_APPROVED` records `machine_name`/`machine_info`/`minted_token_id`/`approver`/`ip` |

Additional hardening:
- **Anti-enumeration:** unknown `device_code` and unknown `user_code` both map to the generic terminal result (`expired_token` / 404) — no distinction between "never existed" and "expired/consumed".
- **No secrets in display metadata:** `DeviceLoginRequestPublic` exposes only `user_code`/`machine_name`/`machine_info`/`status` — never `device_code`, token, IP, or approver.
- **Approver binding + developer gate:** approve/reject are `CurrentUser` + `require_developer`; `approved_by_user_id` records the binding (Decision 6).

### New SecurityEvent constants
Add to `backend/app/models/events/security_event.py`:
```
CLI_DEVICE_LOGIN_APPROVED = "CLI_DEVICE_LOGIN_APPROVED"
CLI_DEVICE_LOGIN_REJECTED = "CLI_DEVICE_LOGIN_REJECTED"
```
(`event_type` is a free-form `str` column — no enum migration needed.) `CLI_ACCOUNT_TOKEN_CREATED` is still emitted by the shared minting helper, so the issued token has the same audit trail as the setup-token path.

---

## 9. Error Handling & Edge Cases

- **Unknown / expired / consumed `device_code` on poll** → `expired_token` (200). CLI surfaces "login request expired."
- **Poll faster than `interval`** → `slow_down` (200). CLI adds 5 s.
- **Approve a non-pending request** (already approved/denied/expired) → 409 `already_resolved`. Screen shows "This request was already handled."
- **Approve by a non-developer** → 403. Screen shows the role message; token is NOT minted.
- **Two browsers approve the same code** → first wins (status flips to `approved`); second sees `pending`→ already-resolved race: guard by re-reading status inside `approve` and 409 if not `pending` (commit is the serialization point; acceptable benign race like the desktop flow).
- **Clock skew / naive datetimes from DB** → wrap all `expires_at`/`last_polled_at` comparisons in `_ensure_utc` (reuse the existing helper).
- **Migration applied but client not regenerated** → frontend type errors; remind to run `make gen-client`.
- **`start` flood from one IP** → 429.
- **User code collision** at generation → retry loop (§4 step 3); after N tries, fall back to a longer code or 500 (extremely unlikely with 32^8 space).

---

## 10. UI/UX Considerations

- Prominent, monospaced `user_code` display with the dash, matching what the CLI printed (anti-phishing confirmation).
- "Signed in as {email}" block (reused) so the user sees which account they're authorizing.
- Terminal success card: "Approved — return to your terminal; `cinna login` will finish automatically." (No reliance on `window.close`.)
- Clear copy for denied / expired / not-a-developer states.
- Loading + error states reused from `NativeAuthConsentPage`.

---

## 11. Integration Points

- **Account token reuse:** the minted `cli-account` token authenticates via `AccountCLIContextDep` and is immediately usable on `POST /api/v1/cli/account/agents/{id}/mint` (acceptance test step 5) with no extra steps.
- **Non-breaking rollout:** if `start` 404s (backend not deployed), the CLI falls back to the paste path — so the only hard requirement is that the two CLI paths resolve to `/api/v1/cli/account/login/{start,poll}`.
- **Client regen** after backend changes (`make gen-client`).
- **No agent-env or workspace changes** — this is purely an auth-token refresh flow; `user_workspace_id` and other `account.json` fields are preserved client-side by the CLI.

---

## 12. Future Enhancements (Out of Scope)

- **Background sweep** to hard-delete terminal/expired `cli_device_login_request` rows (piggyback on the existing setup-token cleanup scheduler). Lazy-on-read keeps the flow correct without it; this is housekeeping only.
- **Rotate/supersede the prior account token** on login (the requirements say re-using the same identity is ideal and superseding is "fine" — v1 mints a fresh token alongside; explicit supersede is a future option).
- **Token-family linkage** between successive `cinna login` tokens for the same machine (analytics / single-active-per-machine).
- **RFC-strict mode** (400 + `error`) behind a flag, if a future non-cinna CLI needs it.

---

## 13. Summary Checklist

### Backend
- [ ] Add `CLIDeviceLoginRequest` table + schemas in `backend/app/models/cli/cli_device_login.py`; re-export in `models/__init__.py`.
- [ ] Refactor the account-token minting block out of `AccountCLIService.exchange_account_setup_token` into `AccountCLIService.mint_account_cli_token(...)` (returns `(jwt, CLIToken)`, emits `CLI_ACCOUNT_TOKEN_CREATED`); rewrite the exchange to call it (preserve the single-commit ordering).
- [ ] (Recommended) Lift `_RateLimiter` to `backend/app/services/cli/_rate_limiter.py`; update `account_api_proxy_service.py` import.
- [ ] Create `DeviceLoginService` (`backend/app/services/cli/device_login_service.py`): `start` / `poll` / `get_request_for_display` / `approve` / `reject` + `DeviceLoginError`; per-IP + per-`device_code` throttling; lazy expiry; mint-at-approval with transient `account_token_jwt` nulled on first `authorized` poll.
- [ ] Add the 5 routes in `cli.py` at `/cli/account/login/{start,poll,request,approve,reject}` (start/poll/request unauthenticated; approve/reject `CurrentUser` + `require_developer`); poll always returns 200.
- [ ] Add `CLI_DEVICE_LOGIN_APPROVED` / `CLI_DEVICE_LOGIN_REJECTED` constants in `security_event.py`.
- [ ] Create the Alembic migration (`down_revision = 3974f541ab0b` — verify head first); review autogenerate drift (tz columns, FK `ondelete`, unique index).

### Frontend
- [ ] `bash scripts/generate-client.sh` after routes land (new `CliService` methods).
- [ ] Create `frontend/src/routes/device.tsx` (`/device`, `beforeLoad` → `ensureSessionValid`, `?code=` search param, manual-entry fallback).
- [ ] Create `DeviceLoginConsentPage` (`frontend/src/components/Auth/DeviceLoginConsentPage.tsx`) modeled on `NativeAuthConsentPage` but non-redirecting: show `user_code`/`machine_name`/`machine_info`, Approve/Reject → `CliService` calls, terminal "return to terminal" success card, 403 developer-role message.

### Testing & validation (outline — what to verify)
- [ ] `start` returns RFC 8628 field names + a hashed (never plaintext) `device_code` in DB.
- [ ] `poll` before approval → `authorization_pending`; faster-than-`interval` → `slow_down`; after `expires_at` → `expired_token`; unknown `device_code` → `expired_token`. All HTTP 200.
- [ ] `approve` by a developer mints a `cli-account` `CLIToken`; next `poll` → `authorized` with the raw JWT once; a second `poll` → `expired_token` (single-use, `consumed`).
- [ ] Minted token works on `POST /api/v1/cli/account/agents/{id}/mint` (no 401).
- [ ] `reject` → poll `access_denied`; approve-after-reject → 409.
- [ ] Approve by a non-developer → 403, no token minted.
- [ ] `GET login/request` never leaks `device_code`/token/IP/approver; unknown `user_code` → 404.
- [ ] Per-IP throttle on `start`/`poll` returns 429 / `slow_down`.
- [ ] `CLI_ACCOUNT_TOKEN_CREATED` + `CLI_DEVICE_LOGIN_APPROVED` events recorded with `machine_name`/`machine_info`/`ip`.
- [ ] Backend tests follow `backend/tests/README.md` (API-only, scenario-based; no direct DB writes — drive everything through the endpoints).

---

## Open Questions / Decisions Made

- **Decision 2 (where to mint):** Mint at **approval time** inside the consent handler, atomically with the status flip and approver binding, reusing the existing minting block via `mint_account_cli_token`. The raw JWT (not derivable from `token_hash`) is stashed transiently in `account_token_jwt` and **nulled on the first `authorized` poll** (option 2(a)). Rejected 2(b) (mint at poll) — it would run the mint on an unauthenticated request and duplicate the approver/role logic outside an authenticated context.
- **Decision 3 (single-use / terminal invalidation):** `pending → approved → consumed` (first `authorized` poll consumes); `pending → denied`; `* → expired` (lazy). All of `consumed`/`denied`/`expired` return `expired_token`/`access_denied` and never re-yield a token.
- **Decision 6 (developer gate):** approve/reject require `CurrentUser` + `require_developer` (mirrors `create_account_setup_token` / `_require_developer_account`). A demoted user is 403'd; `approved_by_user_id` records the binding.
- **Decision 1 (entropy/codes):** `device_code = secrets.token_urlsafe(32)`; `user_code` = 8 chars from a 32-char unambiguous alphabet, normalized-uppercase storage, dashed display, collision-retry.
- **Decision 4 (slow_down):** per-row `last_polled_at` + per-IP `_RateLimiter`; poll faster than `interval` → `status="slow_down"`.
- **Decision 5 (browser endpoints):** public `GET /cli/account/login/request?user_code=` (display-only), `CurrentUser`+developer `POST .../approve` and `.../reject` taking `{user_code}`; `verification_uri = <frontend>/device`, `verification_uri_complete = <frontend>/device?code=<user_code>`.
- **Decision 7 (contract shape):** poll standardizes on **200 + `status`** (NOT RFC 400 + `error`), matching the CLI and the rest of `/cli/account/*`.
- **Decision 8 (cleanup):** **lazy-on-read** expiry (no scheduler). Row deletion is deferred housekeeping (§12).

**Open question (non-blocking):** should `cinna login` **supersede/revoke** the prior account token for the same machine (vs. minting alongside it)? v1 mints a fresh token without revoking; the requirements call superseding "fine" but not required. Defer to §12 unless product wants single-active-per-machine now.
