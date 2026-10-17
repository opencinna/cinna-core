# OAuth Credentials - Technical Details

Business logic: [OAuth Credentials](oauth_credentials.md). Parent: [Agent Credentials Tech](agent_credentials_tech.md).

## File Locations

- `backend/app/services/credentials/oauth_refresh_service.py` - `OAuthRefreshService`, the single refresh primitive
- `backend/app/services/credentials/oauth_refresh_scheduler.py` - background sweep
- `backend/app/services/credentials/oauth_credentials_service.py` - OAuth flow, token exchange, metadata, `refresh_*` field names, `apply_token_response`
- `backend/app/api/routes/oauth_credentials.py` - owner OAuth routes
- `backend/app/api/routes/agent_env_credentials.py` - agent-environment endpoint
- `backend/app/models/credentials/credential.py` - `AgentCredentialAccessTokenRequest` / `AgentCredentialAccessTokenResponse`
- `backend/app/env-templates/app_core_base/core/cinna_api/credentials.py` - container SDK (`access_token`, `CredentialRefreshError`)
- `backend/app/services/sessions/stream_processor.py`, `backend/app/services/environments/environment_lifecycle.py` - refresh callers

## Authorization State and Callback

- Table `credential_oauth_state` (`backend/app/models/credentials/credential_oauth_state.py`, mig `90a3fecdf28b`): `state_hash` (unique sha256 hex), `credential_id`/`user_id` (FK CASCADE), `origin` (`web`|`desktop`), `created_at`, `expires_at`. Replaces the in-process `_oauth_states` dict.
- `OAuthCredentialsService.initiate_oauth_flow(session, credential_id, user_id, origin="web")` -> `{authorization_url, state, expires_at}`; purges expired rows, inserts the hashed state (TTL `OAUTH_STATE_TTL_SECONDS = 600`); desktop states are prefixed `DESKTOP_STATE_PREFIX = "desktop."`. 501 when Google OAuth is not configured (Desktop endpoints pre-check and answer 503 `oauth_not_configured`).
- `consume_state(session, state) -> ConsumedOAuthState | None` - one `DELETE ... RETURNING` plus commit; None when unknown, used or expired.
- `handle_oauth_callback(session, code, state, signed_in_user_id=None) -> OAuthCallbackResult {credential, origin, owner_email_masked}`: consumes the state, then 403 if `signed_in_user_id` is set and differs from the state's user (state already burned), then owner check, `_exchange_authorization_code` (the single token-endpoint call and test seam; 400 on failure). `mask_email` keeps 1-2 leading characters and the full domain.
- Route `POST /api/v1/credentials/oauth/callback` takes `OptionalCurrentUser` (`get_optional_current_user` in `deps.py`: missing or rejected token resolves to None, never fails). `OAuthCallbackResponse {credential_id, message, origin, owner_email_masked}`.
- Frontend `routes/credentials/oauth/callback.tsx` renders the desktop or web terminal view (from `origin` or the `desktop.` prefix) and the masked account.
- Tests: `backend/tests/api/credentials/test_oauth_state_db.py`.

## OAuthRefreshService

- `refresh_if_needed(session, credential, *, min_valid_seconds, force=False, raise_on_error=False) -> RefreshOutcome`. Non-Google types and undecryptable blobs are `skipped`. Outcome `status`: `refreshed`, `fresh`, `skipped`, `failed`, `busy`, `cooldown`. Outcomes never contain token values.
- Pre-check (skipped when `force`): token still valid for `min_valid_seconds` -> `fresh`; stored `refresh_error_kind == reauth_required` -> `failed`; `provider_error` within `OAUTH_REFRESH_COOLDOWN_SECONDS` of `refresh_error_at` -> `cooldown`.
- `locked(session, credential, needs_refresh)` - per-credential in-process `asyncio.Lock` plus `pg_try_advisory_xact_lock(0x4F415554, key)` on the caller's session (non-blocking try-lock polled every 0.25 s up to 5 s; no blocking `FOR UPDATE`, which would deadlock the event loop). `needs_refresh` is re-evaluated on a re-read after acquiring. States `acquired` / `not_needed` / `busy`. The commit on exit releases the lock. Caller session must have no pending changes.
- Re-auth race guard: the encrypted blob is snapshotted under the lock and re-checked before writing; if the OAuth callback changed it, the stale result is discarded (`fresh`).
- Bookkeeping in the encrypted blob (no migration): `refresh_attempted_at`, `refresh_error`, `refresh_error_kind` (`reauth_required` | `provider_error`), `refresh_error_at`. Cleared on success; the OAuth callback rebuilds the blob so re-authorization clears them. Kept out of the container by `AGENT_ENV_ALLOWED_FIELDS`. Metadata endpoint exposes `refresh_error` and `needs_reauthorization`.
- `issue_access_token(session, credential, *, min_ttl) -> AgentCredentialAccessTokenResponse` - the one place mapping a `RefreshOutcome` to `AgentEnvTokenError` codes; callers resolve access first. Used by `access_token_for_env`, by Desktop `DesktopOAuthService.access_token` (`POST /api/v1/external/credentials/{id}/access-token`) and by Desktop materialize. `oauth_status(data)` -> `pending_authorization` | `ready` | `reauth_required`, also surfaced in the Desktop list.
- `access_token_for_env(session, agent_id, credential_id, min_ttl, known_expires_at=None)` - link check + `issue_access_token` + env push; used by the env endpoint (below).
- `AgentEnvTokenError(code, status, message, retry_after=None)` - mapped to HTTP errors by the route.

## Refresh Callers

| Caller | Path |
|---|---|
| Env start, `_sync_dynamic_data`, `sync_credentials_to_agent_environments` | `CredentialsService.prepare_fresh_credentials_for_environment` -> `refresh_expiring_credentials_for_agent` (no events, no recursion, never raises) |
| Every stream turn (UI, channel follow-up, A2A, MCP) | `SessionStreamProcessor._process_inner` after `on_stream_starting`; syncs only if something refreshed |
| Sweep | `sweep_expiring_oauth_credentials` |
| Owner manual | `POST /api/v1/credentials/{id}/oauth/refresh` (`force=True`) |
| Container | env endpoint below |

## Agent-Environment Endpoint

`POST /api/v1/agent/credentials/{credential_id}/access-token` (tag `agent-credentials`, auth `AgentEnvContextDep`: env `AGENT_AUTH_TOKEN` as Bearer + `X-Agent-Env-Id`; `CurrentUser` tokens are rejected).

- Request `AgentCredentialAccessTokenRequest {min_ttl: int = 300, known_expires_at: int | None}` - `min_ttl` clamped to `[0, OAUTH_ON_DEMAND_MAX_MIN_TTL_SECONDS]` (1800); `known_expires_at` is the expiry of the token the env already holds (the SDK sends its local `expires_at`).
- Response `AgentCredentialAccessTokenResponse {access_token, token_type, expires_at, refreshed}`. Never carries the refresh token or bookkeeping.
- Scope: only credentials linked to the calling agent (shared included) and Google OAuth types. Unlinked and nonexistent ids give the same 404.
- Errors, `detail = {"code", "message"}`:

| HTTP | code | When |
|---|---|---|
| 404 | `credential_not_linked` | not linked to the agent / missing |
| 422 | `not_refreshable` | placeholder or non-Google-OAuth type (`mcp_provider` tokens never live in credentials.json) |
| 409 | `reauthorization_required` | never authorized, or Google rejected the refresh token |
| 502 | `provider_error` | Google failure or cooldown; `Retry-After` set |
| 502 | `refresh_in_progress` | another worker holds the refresh lock and the stored token is expired; `Retry-After: 1` |
| 503 | `oauth_not_configured` | Google OAuth not configured on the platform |

- The credentials payload is pushed to the agent's running environments before responding when the call refreshed, or the returned `expires_at` differs from `known_expires_at`, or that field is missing (raw HTTP callers always get a push), so env-core redaction knows the value. A push failure is logged and the response is still 200. Other agents sharing the credential rely on their own sync/sweep.

## Container SDK

`from core.cinna_api import credentials` -> `credentials.access_token(credential_id, *, min_ttl=300) -> str`. Reads `credentials.json` fresh; returns the synced token when `expires_at - now > min_ttl`; otherwise POSTs (stdlib `urllib`; `BACKEND_URL`, `AGENT_AUTH_TOKEN`, `ENV_ID` from the process env) to the endpoint. Platform unreachable but synced token still valid -> returns it with a warning; otherwise `CredentialRefreshError("unavailable", id)`. `min_ttl` is clamped to 1800 client-side too. `refresh_in_progress` is retried once after 1.5 s. HTTP 401/403 raises `unauthorized` (restart/rebuild the env; no local fallback). Other codes mirror the table plus `unavailable`; `str(exc)` is a relayable message. env-core writes `credentials.json` atomically. **Needs env rebuild** (`/app/core` is a per-env copy); the generated credentials README documents a `curl` fallback ("Google OAuth access tokens without Python", sends `min_ttl` only, so it always triggers a push) that works on any environment, and tells agents an `AttributeError` on `credentials.access_token` means the env needs a rebuild.

## Scheduler and Settings

`oauth_refresh_scheduler.py` is scheduled from a background thread and runs `sweep_expiring_oauth_credentials` on the main app event loop (`run_coroutine_threadsafe`), through `leader_session(OAUTH_REFRESH_SWEEP_LOCK_KEY)` so only one worker sweeps at a time. Each run processes all candidates: credentials linked to agents with a running environment, within the threshold of expiry (Google OAuth types and `mcp_provider` `oauth_dcr`). Credentials already in `reauth_required` are skipped without calling Google and are not counted as failures. One bad credential never stops the sweep; refreshed agents are synced. Never started under `settings.TESTING`; started/stopped from `backend/app/main.py`.

| Setting | Default | Notes |
|---|---|---|
| `OAUTH_REFRESH_THRESHOLD_SECONDS` | 1800 | valid 600..2400; must stay below token lifetime |
| `OAUTH_REFRESH_COOLDOWN_SECONDS` | 60 | retry delay after `provider_error` |
| `OAUTH_ON_DEMAND_MAX_MIN_TTL_SECONDS` | 1800 | clamp for endpoint `min_ttl` |
| `OAUTH_REFRESH_SWEEP_ENABLED` | true | |
| `OAUTH_REFRESH_SWEEP_INTERVAL_MINUTES` | 10 | |

## Known Gaps

- `mcp_provider` `oauth_dcr` refresh has no failure cooldown (retried on every sync, start and stream).
