# Account Device Login (`cinna login`)

## Purpose

Refreshes an expired **account CLI token** through a browser-based approval instead of pasting a new setup token. When `cinna doctor` reports authentication failures because the account token has expired, `cinna login` opens the platform's consent page in the browser — the user approves it while already signed in — and the CLI receives a fresh token without any copy-paste.

This is an implementation of the [OAuth 2.0 Device Authorization Grant (RFC 8628)](https://datatracker.ietf.org/doc/html/rfc8628), adapted to the `/cli/account/*` surface.

## Relationship to the Account CLI Workspace

The account CLI workspace ([account_cli_workspace.md](account_cli_workspace.md)) ships the core bootstrap flow: a developer runs `cinna account setup` once to mint an account workspace. That account token expires after 7 days. `cinna login` is the renewal mechanism — it replaces the account token without forcing another copy-paste of a setup token, so `cinna doctor`'s per-agent child-token re-mints can resume immediately.

The minted token is identical in scope and lifetime to the token produced by `cinna account setup`. All downstream flows (`cinna agent sync`, `cinna agent-api`, `cinna connect`, the `cinna api` escape hatch) continue working without any extra steps.

## Why Not Just Paste Another Setup Token?

A new account setup token requires navigating to **Settings → Security → Local Development**, generating the one-liner, and pasting it in the terminal. When the account token expires, this forces the developer — or the local orchestrator agent — to reach into the browser, which breaks unattended workflows and interrupts the coding loop. `cinna login` keeps the entire refresh in the browser where the user is already authenticated.

## High-Level Flow

```
 CLI                        Backend                    Browser (signed-in user)
  │  POST login/start         │                               │
  │ ────────────────────────▶ │  create CLIDeviceLoginRequest │
  │ ◀──────────────────────── │  {device_code, user_code, …}  │
  │  print WX7K-9Q2P, open ────────────────────────────────▶ │ /device?code=WX7K-9Q2P
  │  verification_uri_complete│                               │ (login redirect if needed)
  │  POST login/poll (loop)   │  GET login/request ◀───────── │ fetch machine_name/info
  │ ────────────────────────▶ │  status=authorization_pending │ show user_code + Approve/Reject
  │ ◀──────────────────────── │                               │ user clicks Approve
  │                           │  mint cli-account token ◀──── │ POST login/approve
  │  POST login/poll          │  status=approved              │
  │ ────────────────────────▶ │  return account_token ────────│
  │  write .cinna/account.json│  status=consumed              │ "Return to your terminal"
```

## User Story: Refreshing an Expired Account Token

1. The developer (or automated tooling) runs `cinna login` in the account workspace.
2. The CLI reads `.cinna/account.json` for `platform_url`, `frontend_url`, and `machine_name`.
3. The CLI prints a short code such as `WX7K-9Q2P` and opens the platform's `/device?code=WX7K-9Q2P` page in the browser.
4. The user is prompted to log in if not already authenticated; any configured 2FA challenge is satisfied here.
5. The consent page shows the machine name, machine info, and the user code — confirming which device is requesting access.
6. The user clicks **Approve**. The backend mints a fresh account CLI token and stores it transiently.
7. The CLI's polling loop receives `status="authorized"` and the raw account token.
8. The CLI writes the new token to `.cinna/account.json` (preserving all other fields such as `user_workspace_id`).
9. The CLI prints `✓ Signed in — account token refreshed`.
10. `cinna doctor` immediately re-mints any blocked per-agent child tokens via `POST /api/v1/cli/account/agents/{id}/mint` — no further user action required.

If the user clicks **Reject**, the CLI's polling loop receives `status="access_denied"` and exits with "Authorization was denied in the browser."

If the user takes no action and the 15-minute window elapses, the poll returns `status="expired_token"` and the CLI exits with a timeout message.

## Device Login State Machine

Each `cinna login` attempt corresponds to one `CLIDeviceLoginRequest` row with a server-side state machine:

```
pending ──approve──▶ approved ──first authorized poll──▶ consumed   (terminal)
   │  └─reject──▶ denied                                            (terminal)
   └─expires_at < now (lazy, on any read)──▶ expired                (terminal)
```

- **pending**: awaiting user action in the browser.
- **approved**: user has approved; token minted and stored transiently; will be handed to the next `poll` call.
- **consumed**: token was handed to the CLI on the first `authorized` poll; the request is now terminal and single-use.
- **denied**: user rejected; no token minted.
- **expired**: the 15-minute window elapsed before the user acted (only `pending` rows expire this way; an `approved` row that outlives `expires_at` still yields its token on the next poll).

## API Contract

### `POST /api/v1/cli/account/login/start` — unauthenticated

Begins a device-login request. Returns the raw `device_code` (the CLI's polling credential), a human `user_code`, and the verification URLs. Rate-limited per source IP (10 requests/minute).

**Request body:** `{machine_name, machine_info?}`

**Response (200):**

```json
{
  "device_code": "<opaque, ≥256-bit entropy>",
  "user_code": "WX7K-9Q2P",
  "verification_uri": "https://app.example.com/device",
  "verification_uri_complete": "https://app.example.com/device?code=WX7K-9Q2P",
  "interval": 5,
  "expires_in": 900
}
```

### `POST /api/v1/cli/account/login/poll` — unauthenticated

Polls for the flow result. **Always returns HTTP 200** — pending, slow_down, denied, and expired are flow states, not transport errors. This is a deliberate deviation from RFC 8628's 400+`error` shape; it matches the rest of the `/cli/account/*` surface and the CLI implementation.

**Request body:** `{device_code: str}`

**Response (always 200):**

| `status` | Meaning | Extra fields on this status |
|---|---|---|
| `authorization_pending` | User has not acted yet | — |
| `slow_down` | CLI polled too fast; add 5 s to the interval | — |
| `authorized` | User approved | `account_token`, `platform_url`, `frontend_url`, `machine_name` |
| `access_denied` | User rejected | — |
| `expired_token` | Request expired, unknown, or already consumed | — |

The `authorized` response is **single-use**: a second poll returns `expired_token`.

### `GET /api/v1/cli/account/login/request?user_code=` — unauthenticated

Browser display metadata. Returns `{user_code, machine_name, machine_info, status}`. Never exposes `device_code`, the token, the source IP, or the approver. Unknown or consumed codes return 404 (existence-leak-safe).

### `POST /api/v1/cli/account/login/approve` — authenticated, any role

Approve a pending request. Mints a fresh account CLI token bound to the approver and flips the row to `approved`. The approver can be **any authenticated platform user** — there is no developer-role gate on this endpoint (see [Business Rules](#business-rules)).

**Request body:** `{user_code: str}`

**Response codes:** 200 (approved) / 401 (not signed in) / 404 (unknown/expired) / 409 (already resolved).

### `POST /api/v1/cli/account/login/reject` — authenticated, any role

Reject a pending request. Flips the row to `denied`; no token is minted.

**Request body:** `{user_code: str}`

**Response codes:** 200 (rejected) / 401 / 404 / 409.

## Business Rules

### Any Authenticated User May Approve

The approve and reject endpoints require only `CurrentUser` — any role, any level. There is no `require_developer` gate. The reasoning: the consent page is opened by the user whose CLI session is expiring; the only requirement is that whoever opens the browser and clicks Approve is signed in to the platform. Restricting to developers would block the flow for accounts that were demoted between sessions.

This is a documented deviation from the plan's draft Decision 6.

### Minted Token Is Identical to the Setup-Token Path

The device-approval path calls the same `AccountCLIService.mint_account_cli_token` helper that `cinna account setup` uses. The minted token has:

- `token_type="cli-account"`
- `agent_id=NULL`
- 7-day rolling expiry
- Accepted by `AccountCLIContextDep` and `POST /account/agents/{id}/mint`

There is no distinction in scope or identity between a token produced by `cinna account setup` and one produced by `cinna login`. Both emit a `CLI_ACCOUNT_TOKEN_CREATED` security event with `machine_name` and source IP.

### Token Minted at Approval, Not at Poll

The account token is minted during `POST /approve` (the only moment the platform user is authenticated and the approver is known). The raw JWT is stored transiently in the `account_token_jwt` column and nulled as soon as the first `authorized` poll picks it up. This keeps the approver-binding atomic with the status flip and avoids running minting logic on an unauthenticated endpoint.

### Single-Use Guarantee

The first `authorized` poll flips the row to `consumed` and nulls `account_token_jwt`. Any subsequent poll sees `consumed` and returns `expired_token`. The raw JWT is never recoverable after that point.

### No Token Supersession in v1

`cinna login` mints a new account token alongside any existing one — it does not revoke the prior token. If the existing token is still valid, both tokens are live until expiry or manual disconnection from Settings → Security → Local Development. Supersession (single-active-per-machine) is a deferred enhancement.

### Security Properties

| Property | Mechanism |
|---|---|
| `device_code` ≥128-bit entropy | `secrets.token_urlsafe(32)` — ~256-bit |
| `device_code` never stored plaintext | Only SHA-256 hash stored; raw value returned to CLI once |
| `device_code` single-use | First `authorized` poll sets `status="consumed"`, nulls `account_token_jwt` |
| `user_code` unambiguous | 8 characters from alphabet with no 0/O/1/I/L, dashed display form |
| Per-IP rate limit on `start` | 10 requests/minute; 429 on breach |
| Per-IP rate limit on `poll` | 60 requests/minute; `slow_down` response (still HTTP 200) |
| Per-request `slow_down` | `last_polled_at` per row; polling faster than 5 s interval returns `slow_down` |
| Lazy expiry | Any read that sees `expires_at < now` flips `pending` → `expired` atomically |
| Anti-enumeration | Unknown `device_code` → `expired_token`; unknown `user_code` → 404; both are indistinguishable from expired |
| No secrets in display metadata | `GET /request` returns only `user_code`/`machine_name`/`machine_info`/`status` |
| Audit trail | `CLI_ACCOUNT_TOKEN_CREATED` + `CLI_DEVICE_LOGIN_APPROVED` (or `CLI_DEVICE_LOGIN_REJECTED`) |

## Browser Consent Page

The browser consent page is **fully implemented** at the `/device` route in the React frontend (`frontend/src/routes/device.tsx` → `frontend/src/components/Auth/DeviceLoginConsentPage.tsx`):

- The route accepts `?code=<user_code>` (from `verification_uri_complete`) and preserves it through the login bounce, so the user lands back on the consent screen with the code still prefilled. When no code is present, the page falls back to a manual code-entry input.
- Access is auth-gated via `ensureSessionValid` — an unauthenticated visitor is redirected to log in (and through any 2FA challenge) and returned to `/device?code=…`.
- It loads display metadata via `GET /api/v1/cli/account/login/request` and prominently shows the `user_code` (anti-phishing confirmation) alongside `machine_name` and `machine_info`.
- **Approve** calls `POST /api/v1/cli/account/login/approve`; **Reject** calls `POST /api/v1/cli/account/login/reject`. Success renders a "Device authorized — return to your terminal" card; reject renders a "Request denied" card; an expired/unknown code renders a "run `cinna login` again" card.
- The component reuses the established consent UI from the native desktop/mobile app approval flow (`NativeAuthConsentPage`) — same Card/Button/loading-success-denied-error states and the "Signed in as {user}" block — but is non-redirecting (the CLI polls in the background rather than bouncing to an app scheme).

The CLI itself (`cinna login` in the separate `cinna-cli` repo, at `src/cinna/account.py::run_login`) implements the full polling loop against the backend endpoints. The feature is end-to-end complete.

## Security Events

| Event | Trigger | Severity | Details |
|---|---|---|---|
| `CLI_ACCOUNT_TOKEN_CREATED` | Successful approval — emitted by the shared `mint_account_cli_token` helper | medium | `{machine_name, ip}` |
| `CLI_DEVICE_LOGIN_APPROVED` | Successful `POST /approve` | medium | `{user_code, machine_name, machine_info, minted_token_id, ip}` |
| `CLI_DEVICE_LOGIN_REJECTED` | Successful `POST /reject` | low | `{user_code, machine_name, ip}` |

No event is emitted for `start` (the caller is unauthenticated; no user is known). Expired and unknown `device_code` lookups on `poll` produce no event (anti-enumeration).
