# Account Device Login — Technical Details

## File Locations

### Backend — Model

- `backend/app/models/cli/cli_device_login.py` — `CLIDeviceLoginRequest` (table) plus all Pydantic request/response schemas (no `table=True`): `DeviceLoginStartRequest`, `DeviceLoginStartResponse`, `DeviceLoginPollRequest`, `DeviceLoginPollResponse`, `DeviceLoginRequestPublic`, `DeviceLoginResolveBody`
- `backend/app/models/__init__.py` — re-exports `CLIDeviceLoginRequest`

### Backend — Service

- `backend/app/services/cli/device_login_service.py` — `DeviceLoginService` (all static methods, mirrors `DesktopAuthService`); `DeviceLoginError`; `_normalize_user_code`; `_dash_user_code`; module constants `DEVICE_LOGIN_EXPIRY_SECONDS`, `DEVICE_LOGIN_POLL_INTERVAL`, `USER_CODE_ALPHABET`

### Backend — Shared Minting Helper

- `backend/app/services/cli/account_cli_service.py` — `AccountCLIService.mint_account_cli_token(db, *, owner_id, machine_name, machine_info, request) -> tuple[str, CLIToken]`. The single source of truth for account-token minting, shared by `exchange_account_setup_token` (the setup-token paste path) and `DeviceLoginService.approve` (the device-approval path). The caller that needs to persist additional rows atomically (e.g. marking a setup token used) must `db.add(...)` them before calling so they ride the helper's single `db.commit()`.

### Backend — Rate Limiter

- `backend/app/services/cli/rate_limiter.py` — `RateLimiter` class (process-local in-memory sliding window). Lifted from `account_api_proxy_service.py` to a shared module so both `DeviceLoginService` and `AccountApiProxyService` can import it without circular coupling. `DeviceLoginService._rate_limiter = RateLimiter()` is a class-level instance.

### Backend — Routes

- `backend/app/api/routes/cli.py` — five routes appended in the `# Account Device-Login Flow` block (around line 1496). All five mount on the existing `router = APIRouter(prefix="/cli", tags=["cli"])` so the final paths are `/api/v1/cli/account/login/{start,poll,request,approve,reject}`. `start` / `poll` / `request` carry no auth dependency; `approve` / `reject` declare `current_user: CurrentUser`.

### Backend — Scheduler

- `backend/app/services/cli/device_login_scheduler.py` — `DeviceLoginScheduler`: `start_scheduler()` / `shutdown_scheduler()` / `run_cleanup()`. Runs every 15 minutes via APScheduler `BackgroundScheduler`. Calls `DeviceLoginService.cleanup_expired(session)` which hard-deletes rows past `expires_at` that are **not** in `approved` status (approved rows still hold a live token and must not be deleted before the first poll). Follows the same pattern as `cli_setup_token_scheduler.py` / `desktop_auth_scheduler.py`. Gated by the `TESTING` flag (not started during the test suite).

### Backend — Security Event Constants

- `backend/app/models/events/security_event.py` — two new free-form string constants (no enum migration needed):
  - `CLI_DEVICE_LOGIN_APPROVED = "CLI_DEVICE_LOGIN_APPROVED"`
  - `CLI_DEVICE_LOGIN_REJECTED = "CLI_DEVICE_LOGIN_REJECTED"`

### Backend — Migration

- `backend/app/alembic/versions/c70a14722869_add_cli_device_login_request.py` — `down_revision = "3974f541ab0b"`. Creates `cli_device_login_request` with all columns from the model, unique index on `device_code_hash`, btree index on `user_code`, btree index on `status`, FK `approved_by_user_id → user.id ON DELETE CASCADE`, FK `minted_token_id → cli_token.id ON DELETE SET NULL`. Downgrade: `op.drop_table("cli_device_login_request")`.

### Frontend

- `frontend/src/client/sdk.gen.ts` — regenerated after the backend routes landed; `CliService` includes `deviceLoginStart`, `deviceLoginPoll`, `deviceLoginRequestMetadata`, `deviceLoginApprove`, `deviceLoginReject` (exact names follow the OpenAPI operationId generated from the route function names). The consent page calls `deviceLoginRequestMetadata`, `deviceLoginApprove`, `deviceLoginReject`.
- `frontend/src/routes/device.tsx` — public-but-auth-gated route at `/device`. `validateSearch: { code: z.string().optional() }`; `beforeLoad` → `ensureSessionValid("/device?code=<code>")` (preserves the `code` through the login bounce); renders `<DeviceLoginConsentPage code={code} />`. Auto-registered in `routeTree.gen.ts`.
- `frontend/src/components/Auth/DeviceLoginConsentPage.tsx` — the consent component. Modelled on `NativeAuthConsentPage` (same Card/Button/loading-success-denied-error states + "Signed in as {user}" block from `UsersService.readUserMe`) but **non-redirecting** (the CLI is polling — no `window.location.href` app-scheme bounce). Loads metadata via `deviceLoginRequestMetadata`, falls back to a manual code-entry input when no `code` prop is present, shows `user_code` / `machine_name` / `machine_info`, and renders terminal "return to your terminal" / "request denied" / "run `cinna login` again" cards. `redirectToLoginPreservingTarget()` powers the "Use another account" link.

## Database Schema

### `cli_device_login_request`

| Field | Type | Constraints / notes |
|-------|------|---------------------|
| `id` | UUID | PK, `default_factory=uuid4` |
| `device_code_hash` | VARCHAR(128) | unique, indexed — SHA-256 of the raw `device_code`; raw value never stored |
| `user_code` | VARCHAR(16) | indexed — normalized uppercase no-dash (e.g. `WX7K9Q2P`); displayed as `WX7K-9Q2P`; not column-unique (generation-time collision retry) |
| `status` | VARCHAR(20) | indexed, default `"pending"` — one of `pending`, `approved`, `denied`, `expired`, `consumed` |
| `machine_name` | VARCHAR(100) | display + becomes the minted `CLIToken.name` |
| `machine_info` | VARCHAR(200) | nullable — display + minted `CLIToken.machine_info` |
| `approved_by_user_id` | UUID | nullable, FK → `user.id` ON DELETE CASCADE — set at approval |
| `minted_token_id` | UUID | nullable, FK → `cli_token.id` ON DELETE SET NULL — prevents orphan-delete of audit row when token is revoked |
| `account_token_jwt` | TEXT | nullable — transient raw JWT held between approval and the first `authorized` poll, then nulled |
| `client_ip` | VARCHAR(64) | nullable — source IP at `start` for audit |
| `last_polled_at` | TIMESTAMP WITH TZ | nullable — drives per-request `slow_down` |
| `created_at` | TIMESTAMP WITH TZ | `default_factory=lambda: datetime.now(UTC)` |
| `expires_at` | TIMESTAMP WITH TZ | `created_at + 900 s`; non-null |

## API Endpoints

All five endpoints live under the `/cli` router prefix and are registered in `backend/app/api/routes/cli.py`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/v1/cli/account/login/start` | None | Per-IP rate limit 10/min → 429; `DeviceLoginStartRequest` body; `DeviceLoginStartResponse` |
| `POST` | `/api/v1/cli/account/login/poll` | None | Always HTTP 200; `response_model_exclude_none=True`; `DeviceLoginPollRequest` body; `DeviceLoginPollResponse` |
| `GET` | `/api/v1/cli/account/login/request` | None | `?user_code=` query param; `DeviceLoginRequestPublic`; 404 if unknown/consumed |
| `POST` | `/api/v1/cli/account/login/approve` | `CurrentUser` (any role) | `DeviceLoginResolveBody` body; `Message`; 404 / 409 on `DeviceLoginError` |
| `POST` | `/api/v1/cli/account/login/reject` | `CurrentUser` (any role) | `DeviceLoginResolveBody` body; `Message`; 404 / 409 on `DeviceLoginError` |

`DeviceLoginError` is mapped by the `_map_device_login_error` helper: `reason="already_resolved"` → 409, all other reasons → 404. The `poll` handler must never let a `DeviceLoginError` surface as non-200; it returns flow states only.

## Service Layer

### `DeviceLoginService` (`device_login_service.py`)

All methods are `@staticmethod`. The class holds one process-local `_rate_limiter = RateLimiter()` instance.

**`start(db, machine_name, machine_info, request) -> DeviceLoginStartResponse`**
1. Per-IP rate check → 429 if over `START_LIMIT_PER_MIN = 10`.
2. Generate `device_code = secrets.token_urlsafe(32)` (~256-bit entropy); SHA-256 hash it.
3. Generate `user_code` via `_generate_unique_user_code` — 8 chars from `USER_CODE_ALPHABET` with up to 5 collision-retries against non-terminal rows.
4. Insert `CLIDeviceLoginRequest` with `status="pending"`, `expires_at = now + 900 s`. Commit.
5. Build `verification_uri = <FRONTEND_HOST>/device`, `verification_uri_complete = <FRONTEND_HOST>/device?code=<dashed_user_code>`.
6. Return `DeviceLoginStartResponse` with the raw `device_code` (the only moment it leaves the server).

**`poll(db, device_code, request) -> DeviceLoginPollResponse`** (`async`)
1. Per-IP rate check → `slow_down` (still HTTP 200).
2. Hash `device_code`; look up by `device_code_hash`. Not found → `expired_token`.
3. Lazy expiry: if `pending` and `expires_at < now`, flip to `expired`, commit → `expired_token`.
4. Per-request slow_down: if `last_polled_at` set and `now - last_polled_at < 5 s` → `slow_down`. Otherwise stamp `last_polled_at`, commit.
5. Dispatch on `status`:
   - `pending` → `authorization_pending`
   - `denied` → `access_denied`
   - `consumed` or `expired` → `expired_token`
   - `approved` → return `authorized` + `account_token` (the stored `account_token_jwt`), then flip to `consumed`, null `account_token_jwt`, commit.
   - Unknown status (defensive) → `expired_token`.

**`get_request_for_display(db, user_code) -> DeviceLoginRequestPublic | None`**

Normalizes `user_code` (uppercase, strip dashes/spaces), loads the newest non-`consumed` row with that code, applies lazy expiry, returns `DeviceLoginRequestPublic`. Returns `None` if not found (route → 404).

**`approve(db, user, user_code, request) -> None`** (`async`)
1. `_load_for_resolution` — normalizes `user_code`, loads live row, lazy-expires it, asserts `status == "pending"` (else raises `DeviceLoginError("already_resolved", ...)`).
2. `await AccountCLIService.mint_account_cli_token(...)` — creates the `CLIToken` row, commits, emits `CLI_ACCOUNT_TOKEN_CREATED`.
3. Set `row.status = "approved"`, `approved_by_user_id = user.id`, `minted_token_id = cli_token.id`, `account_token_jwt = jwt_value`. Commit.
4. Emit `CLI_DEVICE_LOGIN_APPROVED` security event (`severity="medium"`).

**`reject(db, user, user_code, request) -> None`** (`async`)
1. `_load_for_resolution` — same validation as `approve`.
2. Set `row.status = "denied"`, `approved_by_user_id = user.id`. Commit.
3. Emit `CLI_DEVICE_LOGIN_REJECTED` security event (`severity="low"`).

**`cleanup_expired(db) -> int`**

Hard-deletes rows where `expires_at <= now` AND `status != "approved"`. Approved-but-not-yet-polled rows are spared so the token can still be handed back on the next poll. Returns count of deleted rows.

### Internal Helpers

**`_lazy_expire(db, row) -> bool`**

Flips a `pending` row to `expired` when `expires_at < now`. Returns `True` iff the row is expired. `approved` rows are never expired here (see cleanup note above); `denied`/`consumed`/`expired` are already terminal and pass through.

**`_load_live_by_user_code(db, normalized_user_code) -> CLIDeviceLoginRequest | None`**

Loads the newest non-`consumed` row with the given normalized user code (ordered by `created_at DESC`).

**`_load_for_resolution(db, user_code) -> CLIDeviceLoginRequest`**

Combined load + validation for approve/reject: normalizes, loads live row (None → `not_found` DeviceLoginError), lazy-expires (expired → `expired` DeviceLoginError), asserts `pending` (else → `already_resolved` DeviceLoginError).

### `DeviceLoginError`

```python
class DeviceLoginError(Exception):
    def __init__(self, reason: str, message: str): ...
```

`reason` is one of `"not_found"`, `"expired"`, `"already_resolved"`. The route's `_map_device_login_error` maps `"already_resolved"` → HTTP 409, everything else → HTTP 404 (existence-leak-safe).

## Module Constants

```python
DEVICE_LOGIN_EXPIRY_SECONDS = 900     # 15 minutes
DEVICE_LOGIN_POLL_INTERVAL  = 5       # seconds
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USER_CODE_LENGTH   = 8
_USER_CODE_GEN_RETRIES = 5
START_LIMIT_PER_MIN    = 10
POLL_IP_LIMIT_PER_MIN  = 60
```

## Shared Minting Helper (`AccountCLIService.mint_account_cli_token`)

```python
@staticmethod
async def mint_account_cli_token(
    db: Session,
    *,
    owner_id: uuid.UUID,
    machine_name: str,
    machine_info: str | None,
    request: Request,
) -> tuple[str, CLIToken]:
```

Creates a `CLIToken(token_type="cli-account", agent_id=None, owner_id=owner_id, name=machine_name, ...)` with a 7-day expiry, commits, and emits `CLI_ACCOUNT_TOKEN_CREATED`. Returns `(raw_jwt, cli_token)`.

**Commit ordering guarantee:** the helper owns the single `db.commit()`. A caller that needs to atomically persist additional rows (e.g. `exchange_account_setup_token` marking the setup token as used) must call `db.add(those_rows)` before invoking the helper so they ride the same commit.

## Key Security Invariants

- **`device_code` is never stored.** Only `CLIAuthService.hash_token(device_code)` (SHA-256 hex) is persisted in `device_code_hash`. The raw value appears only in the `DeviceLoginStartResponse` and is never logged.
- **`account_token_jwt` is transient.** It exists in the DB row only between `approve` and the first `authorized` poll. `cleanup_expired` skips `approved` rows precisely so this window is never short-circuited.
- **Anti-enumeration.** Unknown `device_code` → `expired_token` (not a distinct "not found"). Unknown `user_code` → 404 (same as expired). Neither leaks whether a code ever existed.
- **`slow_down` on poll is always HTTP 200.** Per-IP flood returns `slow_down` (not 429) so the CLI can distinguish it from a transport error and add 5 s gracefully.
- **Expiry applies only to `pending`.** `approved` rows live until polled (or until manual cleanup). `expired` / `denied` / `consumed` are terminal and do not re-yield tokens.

## Integration Points

| System | Integration |
|--------|-------------|
| `AccountCLIContextDep` | Accepts the minted `cli-account` token — no changes required |
| `POST /account/agents/{id}/mint` | Accepts the minted token immediately after `cinna login` completes |
| `CLIAuthService.hash_token` | Reused for `device_code_hash` (mirrors `CLIToken.token_hash`) |
| `SecurityEventService` | `CLI_ACCOUNT_TOKEN_CREATED` + `CLI_DEVICE_LOGIN_APPROVED` / `CLI_DEVICE_LOGIN_REJECTED` |
| `RateLimiter` | Shared with `AccountApiProxyService`; imported from `services/cli/rate_limiter.py` |
| `_client_ip` | Imported from `account_cli_service.py` |
| `_get_platform_url`, `_ensure_utc` | Imported from `cli_service.py` |

## Tests

Backend tests live in `backend/tests/api/cli/` following the project's API-only, scenario-based pattern (see `backend/tests/README.md`). The test suite covers:

- `start` returns RFC 8628 field names; `device_code_hash` is stored (not plaintext).
- `poll` before approval → `authorization_pending`; faster-than-interval → `slow_down`; after `expires_at` → `expired_token`; unknown `device_code` → `expired_token`. All HTTP 200.
- `approve` by any authenticated user mints a `cli-account` `CLIToken`; next `poll` → `authorized` with raw JWT once; a second `poll` → `expired_token`.
- Minted token works on `POST /account/agents/{id}/mint` (no 401).
- `reject` → next `poll` → `access_denied`; approve-after-reject → 409.
- `GET /request` never leaks `device_code`, token, IP, or approver; unknown `user_code` → 404.
- Per-IP throttle on `start` → 429; per-IP throttle on `poll` → `slow_down` (200).
- `CLI_ACCOUNT_TOKEN_CREATED` + `CLI_DEVICE_LOGIN_APPROVED` security events recorded with `machine_name`, `minted_token_id`, `ip`.
