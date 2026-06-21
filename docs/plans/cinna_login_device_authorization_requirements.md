# Backend requirements: `cinna login` (account device-authorization flow)

**Audience:** the engineer/agent implementing this in **cinna-core** (the platform backend). The CLI half already exists in `cinna-cli` (`cinna login` → `src/cinna/account.py::run_login`) and calls the two endpoints specified below. Nothing in this document should be built in `cinna-cli`.

## Why

An **account workspace** holds an account CLI token in `.cinna/account.json`, used only for the `/api/v1/cli/account/*` routes. That token expires. Today the only way to refresh it is to mint a new account **setup token** in the UI (Settings → Local Development) and paste it into `cinna account setup`. Worse, per-agent CLI tokens minted from the account (`cinna agent sync`) can only be re-minted while the account token is valid — so when the account token expires, `cinna doctor`'s sub-agent re-mints all fail with 401 (observed in production):

```
✗ Odoo Agent API Builder: Authentication failed ... (CLI token has expired)
✗ OneFlow API Workflow Agent: Authentication failed ... (CLI token has expired)
✗ OneFlow-Odoo Document Agent: Authentication failed ... (CLI token has expired)
```

`cinna login` replaces the paste with a browser **device-authorization flow** (OAuth 2.0 Device Authorization Grant, [RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)): the CLI starts a request, the user authorizes it in a browser already signed in to the platform, and the CLI polls until the backend returns a fresh account token. The CLI writes the new token into `.cinna/account.json` in place; **no setup token is pasted.**

## What the CLI already does (so you can match it)

1. Reads `.cinna/account.json` for `platform_url`, `frontend_url`, `machine_name`.
2. `POST {platform_url}/api/v1/cli/account/login/start` with `{machine_name, machine_info}`.
3. Prints `user_code` + the verification URL and opens it in the browser (`webbrowser.open`).
4. Polls `POST {platform_url}/api/v1/cli/account/login/poll` with `{device_code}` every `interval` seconds, honoring `slow_down`, until `expires_in` elapses.
5. On `authorized`, writes `account_token` (and any returned `platform_url` / `frontend_url` / `machine_name`) back to `.cinna/account.json`, preserving all other fields (e.g. `user_workspace_id`).

The CLI tolerates missing optional fields and several status spellings (see "Compatibility notes"). If `start` returns **404**, the CLI tells the user the platform doesn't support `cinna login` yet and falls back to the `cinna account setup` paste path — so shipping the backend later is non-breaking.

---

## Endpoint 1 — start

```
POST /api/v1/cli/account/login/start
```

- **Auth:** none. (The whole point is the caller's account token may be dead. The user authenticates in the browser, not on this call.)
- **Request body:**
  ```json
  { "machine_name": "evgeny's laptop", "machine_info": "Darwin/arm64" }
  ```
  Both are non-secret labels for display/audit on the approval screen and the resulting token row.
- **200 response:**
  ```json
  {
    "device_code": "<opaque, single-use, unguessable>",
    "user_code": "WX7K-9Q2P",
    "verification_uri": "https://app.example.com/device",
    "verification_uri_complete": "https://app.example.com/device?code=WX7K-9Q2P",
    "interval": 5,
    "expires_in": 900
  }
  ```
  | field | required | notes |
  |---|---|---|
  | `device_code` | **yes** | Opaque secret the CLI echoes back when polling. Never shown to the user. High entropy (≥128 bits), single-use. |
  | `user_code` | recommended | Short human code the user confirms matches in the browser (anti-phishing). The CLI prints it. |
  | `verification_uri` | yes (this or `_complete`) | URL the user opens to authorize. |
  | `verification_uri_complete` | recommended | URL with `user_code` pre-filled; the CLI prefers this for `webbrowser.open`. |
  | `interval` | optional (default 5) | Minimum seconds between polls. |
  | `expires_in` | optional (default 900) | Seconds until `device_code` expires. |

  The CLI also accepts the aliases `verification_url` / `verification_url_complete` / `verify_url`, but please emit the RFC 8628 names above.

---

## Endpoint 2 — poll

```
POST /api/v1/cli/account/login/poll
```

- **Auth:** none (the `device_code` is the bearer of this exchange).
- **Request body:**
  ```json
  { "device_code": "<from start>" }
  ```
- **200 response — one of:**

  | `status` | meaning | other fields |
  |---|---|---|
  | `authorization_pending` | user hasn't approved yet | — |
  | `slow_down` | poll too fast; CLI adds 5s to its interval | — |
  | `authorized` | approved | **`account_token`** (required), and optionally `platform_url`, `frontend_url`, `machine_name` |
  | `access_denied` | user rejected the request | — |
  | `expired_token` | `device_code` expired / unknown | — |

  Authorized example:
  ```json
  {
    "status": "authorized",
    "account_token": "<new account CLI JWT>",
    "platform_url": "https://app.example.com",
    "frontend_url": "https://app.example.com",
    "machine_name": "evgeny's laptop"
  }
  ```

  Return **HTTP 200** for all of the above statuses (pending/slow_down/denied/expired are normal flow states, not transport errors). The CLI maps `authorization_pending`/`slow_down` to "keep waiting", `access_denied`/`expired_token` to a clear error, and anything else to "unexpected status". A non-200 is surfaced as a generic "Login polling failed: {detail}".

> RFC 8628 purists use HTTP 400 + an `error` field for the pending/slow_down/denied/expired cases. This flow standardizes on **200 + `status`** to match the rest of the `/cli/account/*` surface and the CLI implementation. If you prefer the RFC's 400+`error` shape, say so and the CLI poll handler can be adjusted — but 200+`status` is the expected contract.

---

## The token that comes back

`account_token` must be a **fresh account CLI token** equivalent to what `POST /cli-setup/account/{token}` issues today (same scope: only the `/api/v1/cli/account/*` routes; same identity binding the existing `cinna account setup` produces). After `cinna login`, `cinna doctor` immediately re-mints the dependent per-agent tokens via the existing `POST /api/v1/cli/account/agents/{id}/mint` — so the new token must be accepted there with no extra steps.

Re-using the same account session/identity as the prior token is ideal (so synced agents and `cinna agent sync` history stay associated), matching how the UI "Local Development" card re-issues. Rotating/superseding the old token is fine; don't require the user to re-run `cinna account setup`.

## Authorization screen behavior

- The browser request must be authenticated as a **platform user** (existing web session, or a login redirect). The device request is bound to whoever approves it.
- Show `user_code`, `machine_name`, and `machine_info` so the user confirms they're approving the right device.
- Approving mints the account token and flips the poll to `authorized`; rejecting flips it to `access_denied`.

## Security checklist

- `device_code`: ≥128-bit entropy, single-use, server-side expiry (`expires_in`), invalidated after a terminal poll result.
- `user_code`: short, human-typeable, rate-limited, also expiring with the request.
- Rate-limit `poll` per `device_code`; enforce `slow_down` if the client polls faster than `interval`.
- `start`/`poll` are unauthenticated by design — protect with per-IP rate limiting and short expiries.
- Scope the minted `account_token` exactly as the existing account setup token; never broader.
- Log `machine_name` / `machine_info` against the issued token for auditability, same as account setup.

## Acceptance test (end-to-end)

1. In an account workspace whose token is expired, run `cinna login`.
2. CLI prints a `user_code` + verification URL and opens the browser.
3. Approve in the browser (signed-in user); CLI prints `✓ Signed in — account token refreshed`.
4. `cinna account status` reports `valid token`.
5. `cinna doctor` re-mints the previously-blocked sub-agent tokens with no 401s.
6. Denying in the browser instead makes the CLI exit with "Authorization was denied in the browser."
7. Letting it sit past `expires_in` makes the CLI exit with "The login request expired…".
