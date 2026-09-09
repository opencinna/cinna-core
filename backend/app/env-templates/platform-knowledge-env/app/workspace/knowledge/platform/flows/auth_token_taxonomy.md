---
feature: flow_auth_token_taxonomy
domain: flows
one_liner: "Follows every token and credential the platform mints — from the function that creates it, through the dependency that trusts it, to the write that kills it."
primary_label: flow
---

# Auth Token Taxonomy

## Purpose

The platform mints roughly thirty distinct bearer secrets. Some are JWTs signed with
`settings.SECRET_KEY`; some are opaque random strings whose only meaning is a database row; a few
are Fernet ciphertexts synced into a container. They are not variations on one design — they differ
in what they authenticate *as*, which dependency in `backend/app/api/deps.py` will accept them, how
long they live, whether the plaintext survives at rest, and what a user has to click to make one
stop working. This flow follows that whole population end to end: **mint → verify → revoke**, one
narrative per kind, so a change to the signing helper, the claim vocabulary, or a revocation cascade
can be checked against every other token it touches.

The entry points are the mint sites (`create_access_token` in `backend/app/core/security.py`, the
per-service `jwt.encode` calls, and the `secrets.token_urlsafe` sites). The exit is the set of
verification dependencies in `backend/app/api/deps.py` — `get_current_user`,
`get_current_user_or_guest`, `get_webapp_chat_user`, `get_cli_context`, `get_account_cli_context`,
`get_agent_env_context` — plus the route-local verifiers in `backend/app/api/routes/a2a.py`,
`backend/app/api/routes/agent_api_public.py`, `backend/app/mcp/token_verifier.py` and
`backend/app/mcp/app_token_verifier.py`. The single most load-bearing fact here is that
**`get_current_user` decodes without an `audience=` argument**, so *any* token carrying an `aud`
claim is rejected there by PyJWT — that is what keeps agent-environment and `owner_identity` tokens
from resolving to a full user.

## Participants

Grouped by audience. "Storage" is what survives at rest **on the server**; "Revocation" is the DB
write that ends the token's life.

### A. Human sessions

| Token | Feature | Carried by | Scope | Lifetime | Storage | Revocation |
|---|---|---|---|---|---|---|
| User access JWT | [auth](../application/auth/auth.md) | `backend/app/core/security.py` `create_access_token` | full `CurrentUser` | `ACCESS_TOKEN_EXPIRE_MINUTES` = 8 days | none (stateless); client keeps it in `localStorage["access_token"]` | none — expiry only (deactivate the user) |
| Google OAuth state | [auth](../application/auth/google_oauth.md) | `backend/app/services/users/auth_service.py` `generate_oauth_state` | one CSRF handshake | 600 s | in-process dict `AuthService._oauth_states` | consumed / swept |
| Google ID token | [auth](../application/auth/google_oauth.md) | `backend/app/core/security.py` `verify_google_token` | proves the Google account | Google-set `exp` | never stored | n/a (Google-issued) |
| Email-confirmation token | [email_confirmation](../application/auth/email_confirmation.md) | `backend/app/utils.py` `generate_email_confirmation_token` | sets `email_confirmed` | `EMAIL_CONFIRM_TOKEN_EXPIRE_HOURS` = 48 h | none (stateless JWT, `purpose="email_confirm"`) | none — expiry only |
| Password-reset token | [auth](../application/auth/auth.md) | `backend/app/utils.py` `generate_password_reset_token` | sets a password | `EMAIL_RESET_TOKEN_EXPIRE_HOURS` = 48 h | none (stateless JWT, **no** `purpose`) | none — expiry only |
| Invitation token | [server_configuration](../application/server_configuration/access_policy.md) | `backend/app/utils.py` `generate_invitation_token` | claims one pre-provisioned account | `INVITATION_EXPIRE_DAYS` = 7 | only the `jti` (`user_invitation.token_jti`) | `revoked_at = now`, or `jti` rotation on resend |
| MFA challenge handle | [user_2fa](../application/user_2fa/user_2fa.md) | `backend/app/services/users/mfa_service.py` `issue_challenge` | one second-factor attempt | `MFA_CHALLENGE_TTL_SECONDS` = 300 | plaintext `user_mfa_challenge.challenge_token` | single-use (`consumed_at`) |
| Trusted-device token | [user_2fa](../application/user_2fa/user_2fa.md) | `backend/app/services/users/mfa_service.py` `register_trusted_device` | skips the 2FA prompt | 1 / 7 / 30 days (`MFA_TRUSTED_DEVICE_ALLOWED_DAYS`) | **bcrypt hash** in `user_trusted_device.token_hash` | rows wiped when 2FA is disabled; hourly purge |

### B. Native clients and the CLI

| Token | Feature | Carried by | Scope | Lifetime | Storage | Revocation |
|---|---|---|---|---|---|---|
| Desktop/mobile access token | [desktop_auth](../application/desktop_auth/desktop_auth.md) | `backend/app/services/desktop_auth/desktop_auth_service.py` `_create_token_pair` | full `CurrentUser` + `client_kind`/`external_client_id` claims | `DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES` = 15 | none (stateless) | `DesktopOAuthClient.is_revoked = True` — checked per request |
| Desktop/mobile refresh token | [desktop_auth](../application/desktop_auth/desktop_auth.md) | `backend/app/services/desktop_auth/desktop_auth_crypto.py` `generate_refresh_token` | redeems one new pair | `DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS` = 30 | **SHA-256** `desktop_refresh_token.token_hash` | `is_revoked = True` (family-wide on replay) |
| CLI setup token | [cinna_cli_integration](../application/cinna_cli_integration/cinna_cli_integration.md) | `backend/app/services/cli/cli_service.py` `create_setup_token` | one bootstrap exchange | 15 min, single use | **plaintext** `cli_setup_token.token` | single-use flag + sweep |
| Per-agent CLI token | [cinna_cli_integration](../application/cinna_cli_integration/cinna_cli_integration.md) | `backend/app/services/cli/cli_auth.py` `create_cli_jwt` | one agent's sync/exec surface | rolling 7 days | SHA-256 + 12-char prefix in `cli_token` | `is_revoked = True` |
| Account CLI token | [account_cli_workspace](../application/cinna_cli_integration/account_cli_workspace.md) | `backend/app/services/cli/account_cli_service.py` `mint_account_cli_token` | `/cli/account/*` only — mint & discover | rolling 7 days | SHA-256 + prefix in `cli_token` | `is_revoked = True` **plus cascade** |
| Device-login `device_code` | [account_device_login](../application/cinna_cli_integration/account_device_login.md) | `backend/app/services/cli/device_login_service.py` `start` | polls for one account token | 900 s | **SHA-256** `device_code_hash` | expiry sweep |
| Account-proxy inner JWT | [account_cli_workspace](../application/cinna_cli_integration/account_cli_workspace.md) | `backend/app/services/cli/account_api_proxy_service.py` | one in-process re-dispatch | 8 s | never stored, never returned | expiry |

### C. Agents and machines

| Token | Feature | Carried by | Scope | Lifetime | Storage | Revocation |
|---|---|---|---|---|---|---|
| `AGENT_AUTH_TOKEN` (env token) | [agent_environments](../agents/agent_environments/agent_environments.md) | `backend/app/services/environments/environment_lifecycle.py` `_generate_auth_token` | one `(env, agent, owner)` triple | `AGENT_ENV_TOKEN_EXPIRE_DAYS` = 365 | **plaintext** in `AgentEnvironment.config["auth_token"]` + SHA-256 `auth_token_hash` | rotate by reconfigure — the hash is the anchor |
| A2A access token | [a2a_access_tokens](../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md) | `backend/app/services/a2a/access_token_service.py` `_create_a2a_jwt` | one agent, one `mode` × `scope` | 1825 days | SHA-256 + 8-char prefix in `agent_access_tokens` | `is_revoked = True`, or row delete |
| `agent_api` connection token | [agent_api](../agents/agent_api/agent_api.md) | `backend/app/services/agent_api/agent_api_token_service.py` `create_token` | proxy calls to one producer agent | none | SHA-256 + prefix; raw value Fernet-encrypted inside the paired credential | delete the credential → `ON DELETE CASCADE` |
| `agent_api` external key | [agent_api](../agents/agent_api/agent_api.md) | `backend/app/services/agent_api/agent_api_key_service.py` `create_key` | same proxy, identity pinned to `subject_user_id` | optional `expires_at` | SHA-256 + prefix | `is_active = False` |
| `owner_identity_token` | [agent_api](../agents/agent_api/agent_api.md) | `backend/app/services/agent_api/agent_api_identity_service.py` `mint` | attribution only (`aud="agent_api_caller"`) | `AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS` = 30 | **never persisted** — lives only in the container's `credentials.json` | drop the grant; stop injecting on next sync |
| MCP connector OAuth tokens | [mcp_integration](../application/mcp_integration/agent_mcp_architecture.md) | `backend/app/services/mcp/mcp_oauth_service.py` | one connector, `scope` string | access 1 h / refresh 30 d | **plaintext** `mcp_token.token` | `revoked = True` |
| MCP direct token | [agent_to_agent_mcp_connector](../application/mcp_integration/agent_to_agent_mcp_connector.md) | `backend/app/services/mcp/mcp_direct_token_service.py` `create_token` | one connector, acts as its owner | 5 years | **plaintext** `mcp_token.token`, 8-char prefix in projections | `revoked = True`, or row delete |
| MCP OAuth client secret | [mcp_integration](../application/mcp_integration/agent_mcp_architecture.md) | `backend/app/services/mcp/mcp_oauth_service.py` `register_client` | identifies a DCR client | none | SHA-256 `client_secret_hash` | only connector deletion (cascade) |
| App MCP token | [app_mcp_server](../application/app_mcp_server/app_mcp_server.md) | `backend/app/services/app_mcp/app_mcp_oauth_service.py` | the user's whole App MCP surface | access 1 h / refresh 30 d | **SHA-256** `app_mcp_token.token_hash` | `is_revoked = True`, **plus** a live availability re-check |

### D. Shares and one-off links

| Token | Feature | Carried by | Scope | Lifetime | Storage | Revocation |
|---|---|---|---|---|---|---|
| Guest-share link token | [guest_sharing](../agents/guest_sharing/guest_sharing.md) | `backend/app/services/sharing/agent_guest_share_service.py` `create_guest_share` | exchanges for a guest JWT | 1–720 h | SHA-256 hash **and the plaintext** (`agent_guest_share.token`) | `is_revoked`, or row delete |
| Guest chat JWT | [guest_sharing](../agents/guest_sharing/guest_sharing.md) | same service, `_create_guest_jwt` | chat-only, `role="chat-guest"` | ≤ 24 h, capped by the share | stateless | none — expiry only |
| Webapp-share link token | [agent_webapp](../agents/agent_webapp/agent_webapp.md) | `backend/app/services/webapp/agent_webapp_share_service.py` `create_webapp_share` | exchanges for a viewer JWT | optional, ≤ 1 year | SHA-256 hash **and the plaintext** | `is_active = False`, or delete |
| Webapp viewer JWT | [agent_webapp](../agents/agent_webapp/agent_webapp.md) | same service, `_create_webapp_jwt` | `role="webapp-viewer"` | ≤ 24 h | stateless | none — expiry only |
| Webhook token | [agent_webhooks](../agents/agent_webhooks/agent_webhooks.md) | `backend/app/services/agents/agent_webhook_service.py` `generate_webhook_credentials` | fires one configured action | none | **Fernet-encrypted** `webhook_token_encrypted` + 8-char prefix | regenerate, disable, or delete |
| Workspace-view JWT | [agent_environments](../agents/agent_environments/agent_environments.md) | `backend/app/services/environments/agent_workspace_token_service.py` | read one path in one env | 1 h | none | **none** — expiry only |
| File-download JWT | [agent_message_attachments](../agents/agent_file_management/agent_message_attachments.md) | same service, `create_file_download_token` | download one `FileUpload` | 1 h | none | **none** — expiry only |

## The flow

### A. Human sessions

1. **User access JWT.** `POST /api/v1/login/access-token` authenticates via
   `UserService.authenticate`, runs `AccessPolicyService.require_password_auth`, then calls
   `security.create_access_token(user.id, expires_delta=8 days)`
   (`backend/app/api/routes/login.py`). The JWT carries `sub` and `exp` and **nothing else** — no
   `aud`, no `token_type`, no `role`. `get_current_user` decodes it, rejects any `aud`, rejects
   `token_type == "agent_env"`, loads the `User`, checks `AccessPolicyService.is_account_valid`, and
   only then — when `client_kind == "desktop"` — consults `DesktopAuthService.verify_active_or_raise`.
   There is no revocation: a web session dies when it expires or when the user row is deactivated.

2. **Google OAuth.** `GET /api/v1/auth/google/authorize` returns a `secrets.token_urlsafe(32)` state
   held in `AuthService._oauth_states` (in-process, 10 min). `POST /api/v1/auth/google/callback`
   exchanges the code, and `verify_google_token` validates the ID token's RS256 signature against
   Google's cached JWKS with a *required* issuer and audience, then additionally requires
   `email_verified`. Success ends in an ordinary user access JWT (or an MFA challenge).

3. **Email confirmation and password reset** are both stateless JWTs from `backend/app/utils.py`,
   both 48 h. They are told apart by a `purpose` claim:
   `verify_email_confirmation_token` **requires** `purpose == "email_confirm"`;
   `verify_password_reset_token` cannot require one (old links in inboxes carry none) so it
   **rejects any token that has one**. Without that rejection an invitation token — same signature,
   same `sub`-is-an-email shape — would redeem at `POST /api/v1/reset-password/` (commit
   `f3d6c00c`). Neither is revocable.

4. **Invitation.** `InvitationService.create_invitation` commits the row, rotates
   `token_jti = uuid4()`, and only then mints the JWT *from the committed value* — a token minted
   from a `jti` that never lands is indistinguishable from a forgery, by design. `_resolve` verifies
   signature + `purpose == "invite"`, looks the row up **by `jti`, never by address**, and requires
   `pending` + active + unclaimed + a case-insensitive email match.
   `POST /api/v1/users/{user_id}/invitation/revoke` stamps `revoked_at`; a resend revokes the old
   link implicitly by overwriting `token_jti`.

5. **MFA.** A password that clears the first factor produces a `UserMfaChallenge` with an opaque
   `secrets.token_urlsafe(32)` handle, 300 s, single-use. `POST /api/v1/login/mfa/verify` consumes
   it and — when the user asked to remember the browser — calls `register_trusted_device`, which
   stores **only a bcrypt hash** and returns the plaintext exactly once. On the next login the
   frontend replays it in the `X-Trusted-Device` header
   (`frontend/src/utils/trustedDevice.ts`); `consume_trusted_device` bcrypt-verifies it against the
   requesting user's own live rows and returns `False` silently on a miss, so a forged token is
   indistinguishable from no token at all.

### B. Native clients and the CLI

6. **Desktop / mobile pair.** `POST /api/v1/desktop-auth/token` (or its `/app-auth` mirror — same
   service, same tables) runs the PKCE code exchange and `_create_token_pair` mints a 15-minute
   access JWT with `client_kind` + `external_client_id` claims, plus an opaque
   `secrets.token_urlsafe(48)[:64]` refresh token stored as a SHA-256 hash with a `token_family`.
   Refresh rotates: the presented row gets `is_revoked = True` **and `revoked_at = now`**, and a new
   pair is issued in the same family. `DELETE /api/v1/desktop-auth/clients/{client_id}` sets
   `DesktopOAuthClient.is_revoked`, which `get_current_user` honours on the next request rather than
   waiting out the 15 minutes.

7. **Refresh reuse grace.** Replaying an already-revoked refresh token inside
   `DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS` (60) is treated as a lost-response retry: the family
   collapses to a single live token and re-rotates. Outside the window it is a replay and
   `revoke_token_family` kills the family. The discriminator is `revoked_at is not None` — which is
   why the *hard* revocation paths (`revoke_token_family`, and grant supersession in `_stamp_grant`)
   set `is_revoked` and deliberately leave `revoked_at` NULL.

8. **CLI bootstrap.** `POST /api/v1/cli/setup-tokens` and `POST /api/v1/cli/account/setup-tokens`
   mint a `secrets.token_urlsafe(24)` stored **in plaintext**, 15 minutes, single use, redeemed
   unauthenticated at `POST /api/cli-setup/{token}` / `POST /api/cli-setup/account/{token}` — the
   setup token *is* the credential. The exchange produces a `CLIToken` row and a JWT from
   `CLIAuthService.create_cli_jwt` whose `sub` is the **token row id**, not a user id.

9. **CLI token verification.** `_resolve_cli_context` requires `token_type == "cli"`, loads the row
   by `sub`, checks `is_revoked` / `expires_at`, asserts `agent.owner_id == cli_token.owner_id`,
   and rolls the 7-day expiry via `refresh_token_usage`. `_resolve_account_cli_context` requires
   `token_type == "cli-account"` and re-checks the type **on the DB row too**. Each dep rejects the
   other's type, so an account token structurally cannot reach a per-agent sync/exec route.

10. **Device login.** `POST /api/v1/cli/account/login/start` mints a `device_code`
    (`token_urlsafe(32)`, stored as SHA-256) and a human-typable `user_code` (plaintext — not a
    secret), 900 s. `POST /api/v1/cli/account/login/poll` always answers **200** with a `status`
    field (`authorization_pending` / `slow_down` / `access_denied` / `expired_token` / `authorized`),
    never a 4xx. Browser approval at `POST /api/v1/cli/account/login/approve` mints the account
    token, parks it in `account_token_jwt`, and the first successful poll hands it over and nulls
    the column.

11. **Account-token revoke cascade.** `DELETE /api/v1/cli/account/tokens/{token_id}` →
    `AccountCLIService.revoke_account_token` sets `is_revoked` on the account token, on every child
    `CLIToken` with `minted_by_account_token_id == token.id`, **and** on every `DesktopOAuthClient`
    stamped with the same provenance (`DesktopAuthService.revoke_clients_for_account_token`), in one
    transaction.

12. **Account → desktop exchange.** `POST /api/v1/cli/account/desktop-token` converts an account CLI
    token into a real desktop session — a full user JWT that reaches routes no `/cli/account/*` verb
    can. The loop that would make the cascade meaningless is closed by
    `ensure_not_cli_exchanged_session` (`backend/app/api/deps.py`): a session whose client is
    stamped `origin="cli_exchange"` cannot mint setup tokens, approve a device login, or approve a
    consent. Read its docstring before adding a minting route — the gate is defined by a *property*,
    not by a route list.

13. **Account API proxy.** The `/cli/account/api-proxy` escape hatch normalises the path, runs the
    single allow/deny chokepoint, then mints an **8-second** ordinary user JWT and re-dispatches
    in-process through the real route stack. That JWT is never returned to the caller.

### C. Agents and machines

14. **Env token mint.** Every configure cycle (create / start / restart / rebuild) calls
    `EnvironmentLifecycle._generate_auth_token`, which mints a JWT with `sub = owner_id` **plus**
    `aud = "agent_env"`, `token_type = "agent_env"`, `env_id`, `agent_id`, 365 days. The same pass
    writes the raw value into `AgentEnvironment.config["auth_token"]`, rotates
    `auth_token_hash = sha256(token)`, and injects the value as the `AGENT_AUTH_TOKEN` line of the
    container's `.env`. `settings.AGENT_AUTH_TOKEN` in `backend/app/core/config.py` is a legacy
    process-wide default with the same name — nothing verifies against it.

15. **Env token verify.** `_resolve_agent_env_context` decodes with `audience="agent_env"`, requires
    `token_type`, matches the `env_id` claim against any `X-Agent-Env-Id` header or path `{id}`, then
    compares `sha256(presented)` against `auth_token_hash` — **the hash, not the TTL, is the
    revocation anchor**. A legacy path (plain owner JWT + header + verbatim compare) stays open only
    while `AGENT_ENV_TOKEN_ACCEPT_LEGACY` is True. The same raw token is also the HMAC key for
    `sign_session_context` in `backend/app/services/sessions/session_context_signer.py`.

16. **A2A access token.** `POST /api/v1/agents/{agent_id}/access-tokens/` mints a JWT with
    `sub = token_row_id`, `agent_id`, `mode`, `scope`, `token_type = "agent"` and **no `aud`**, for
    1825 days; only its SHA-256 hash and an 8-char prefix are stored. Verification is route-local in
    `backend/app/api/routes/a2a.py`: decode, require `token_type == "agent"`, then
    `validate_token_for_agent` re-derives the hash and checks the row. It cannot impersonate a user
    on `CurrentUser` because its `sub` is a token id, so the `User` lookup 404s.
    `PUT /api/v1/agents/{agent_id}/access-tokens/{token_id}` sets `is_revoked`.

17. **`agent_api` connection vs. external key.** Both are opaque `secrets.token_urlsafe(32)` stored
    as SHA-256 + prefix in `agent_api_token`, both validated by
    `AgentApiTokenService.validate_token` behind `_validate_token_or_401` in
    `backend/app/api/routes/agent_api_public.py`. They differ in identity and in how you kill them: a
    **connection** *is* the `agent_api` credential (its raw value rides Fernet-encrypted inside
    `Credential.credential_data`), so deleting the credential cascades the token away; an
    **external key** is bound to `subject_user_id`, revealable once through the audited
    `POST /api/v1/credentials/{id}/agent-api-key/reveal`, and revoked by `is_active = False`.
    `Agent.agent_api_external_access_enabled` is a kill switch checked at validation time.

18. **`owner_identity_token`.** Synthesised host-side on every credential sync as a synthetic
    `credentials.json` entry (`id="owner_identity"`), `aud = "agent_api_caller"`,
    `type = "agent_api_identity"`, 30 days, never persisted server-side. The proxy verifies it,
    turns it into trusted `X-Cinna-Caller-*` headers, and **strips the raw header before forwarding**
    — the producer never sees the token. Its capability is not in the token: scopes come from the
    live grant rows resolved by `AgentApiGrantService`, which is where revocation lives.

19. **MCP.** `POST /mcp/oauth/register` mints a DCR client whose secret is stored SHA-256-hashed;
    consent at `POST /api/v1/mcp/consent/{nonce}/approve` mints a 5-minute auth code; and
    `POST /mcp/oauth/token` exchanges it for an access token (1 h) and refresh token (30 d) stored
    **in plaintext** in `mcp_token.token`. `MCPTokenVerifier.verify_token` accepts `token_type` of
    `"access"` or `"direct"` — the latter being the owner-minted, effectively-permanent connector
    token from `POST /api/v1/agents/{agent_id}/mcp-connectors/{connector_id}/tokens`, revealed once
    and thereafter shown as an 8-char prefix. `POST /mcp/oauth/revoke` sets `revoked = True`.

20. **App MCP** shares the same OAuth routes but a different store: `app_mcp_token` keeps a
    **SHA-256 hash**, and `AppMCPTokenVerifier` adds a second gate — `is_app_mcp_available(user_id)`
    re-checks the channel kill switch and per-user ACL on every request (cached briefly, fail-closed).
    A live token can therefore be switched off without touching its row.

### D. Shares and one-off links

21. **Guest and webapp shares** are one shape used twice. The owner mints a
    `secrets.token_urlsafe(32)` link token; a visitor exchanges it at
    `POST /api/v1/guest-share/{token}/auth` (or `POST /api/v1/webapp-share/{token}/auth`) for a short
    JWT carrying `sub = share_id`, `agent_id`, `owner_id` and a `role`/`token_type` pair —
    `chat-guest`/`guest_share`, or `webapp-viewer`/`webapp_share`. `get_current_user_or_guest` and
    `get_webapp_chat_user` accept exactly those pairs and return a context object, never a `User`.
    Both derived JWTs are capped at 24 h and never outlive their share row.

22. **Webhooks.** `generate_webhook_credentials` mints a public `token_urlsafe(8)` slug plus a
    `token_urlsafe(32)` secret **Fernet-encrypted** at rest. `POST /agent-hooks/{webhook_id}`
    decrypts and compares with `hmac.compare_digest` — this is a timing-safe bearer compare, not a
    payload signature. A disabled webhook answers 404, not 401, so the slug leaks nothing.
    `POST /api/v1/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token` rotates in place.

23. **Signed URLs.** `AgentWorkspaceTokenService` mints two stateless 1-hour JWTs discriminated by a
    `type` claim: `workspace_view` (one env, one path, redeemed at
    `GET /api/v1/shared/workspace/{env_id}/view/{path}`) and `file_download` (one `FileUpload`,
    embedded by `a2a_event_mapper` into A2A `FilePart` links and redeemed at
    `GET /api/v1/files/{file_id}/download`). Neither has a database row, so **neither can be
    revoked** — the 1-hour TTL is the entire control.

## Decision points

| Where | Branches on | Outcomes |
|---|---|---|
| `backend/app/api/deps.py` `get_current_user` | presence of any `aud` claim (decode has no `audience=`) | env tokens and `owner_identity` tokens → 403; everything else continues |
| `backend/app/api/deps.py` `get_current_user` | `token_type == "agent_env"` (secondary gate) | 401 |
| `backend/app/api/deps.py` `get_current_user` | `client_kind == "desktop"` | consult `DesktopOAuthClient.is_revoked` → 401 if revoked |
| `backend/app/api/deps.py` `get_current_user_or_guest` | `role`/`token_type` pair | `chat-guest`+`guest_share` → `GuestShareContext`; else user resolution |
| `backend/app/api/deps.py` `get_webapp_chat_user` | `role != "webapp-viewer"` | 403 |
| `backend/app/api/deps.py` `_resolve_cli_context` | `token_type != "cli"` | 401 "Account token cannot be used on a per-agent route" |
| `backend/app/api/deps.py` `_resolve_account_cli_context` | `token_type != "cli-account"` (claim *and* row) | 401 |
| `backend/app/api/deps.py` `_resolve_agent_env_context` | new-format decode succeeds? | new path (claim-bound) vs. legacy path gated by `AGENT_ENV_TOKEN_ACCEPT_LEGACY` |
| `backend/app/api/deps.py` `_resolve_agent_env_context` | `auth_token_hash` NULL? | hash compare (authoritative) vs. verbatim `config["auth_token"]` compare |
| `backend/app/api/deps.py` `_resolve_platform_user_from_token` | `token_type`/`role` in the console denylist | WS close 1008 |
| `backend/app/api/deps.py` `ensure_not_cli_exchanged_session` | client `origin == "cli_exchange"` | 403 on minting routes only; never on revoke/read |
| `backend/app/utils.py` `verify_password_reset_token` | any `purpose` claim present | reject |
| `backend/app/utils.py` `verify_email_confirmation_token` | `purpose != "email_confirm"` | reject |
| `backend/app/services/desktop_auth/desktop_auth_service.py` `refresh_tokens` | `revoked_at` within the grace window | re-rotate the family vs. kill the family |
| `backend/app/services/users/mfa_service.py` `consume_trusted_device` | bcrypt match against the user's live rows | skip the challenge vs. issue one (silently) |
| `backend/app/services/agent_api/agent_api_service.py` `enforce_policy` | `policy.yaml` + `read_only_override` | narrow only, never widen; 405/413/429 |
| `backend/app/api/routes/agent_api_public.py` `consumer_proxy` | `AgentApiToken.kind` | `connection` → read the identity header; `external` → use `subject_user_id`, ignore the header |
| `backend/app/mcp/app_token_verifier.py` | `is_app_mcp_available(user_id)` | 401 indistinguishable from a bad token |
| `backend/app/core/security.py` `verify_google_signed_jwt` | `GoogleCertsUnavailable` vs. `JoseError`/`ValueError` | "cannot verify" propagates; "invalid" returns `None` |

## Where to change what

| If you want to… | Edit | Re-check |
|---|---|---|
| add a claim to the login JWT | `create_access_token` in `backend/app/core/security.py` | every decode site in `backend/app/api/deps.py`; `TokenPayload` ignores unknowns but the console denylist does not |
| introduce a new token kind | pick `aud` **and** `token_type` deliberately | `get_current_user` (does `aud` lock it out of user routes?), `_DISALLOWED_CONSOLE_TOKEN_TYPES`, `get_current_user_or_guest` |
| add a route that mints a credential | the route | `ensure_not_cli_exchanged_session` — re-derive the gated set from the property in its docstring, not from the current call list |
| change refresh rotation | `DesktopAuthService.refresh_tokens` | the `revoked_at is NULL` invariant used by every hard-revoke path (`revoke_token_family`, `_stamp_grant`) |
| change the account-token blast radius | `AccountCLIService.revoke_account_token` | child `CLIToken`s, `DesktopAuthService.revoke_clients_for_account_token`, and the exchange route's docstring |
| rotate or scope the env token | `EnvironmentLifecycle._generate_auth_token` | `auth_token_hash` write in the configure pass, `_resolve_agent_env_context`, and `session_context_signer` (the token is also the HMAC key) |
| revoke an `agent_api` connection | delete the `agent_api` credential | the `credential_id` cascade is the *only* revocation for a connection token; external keys use `is_active` instead |
| add an `agent_api` scope | `AgentApiGrantService` + `policy.yaml` handling in `agent_api_service.py` | `owner_identity_token` carries no scopes — grants are resolved live, so nothing in the token needs changing |
| make a share expire sooner | the `expires_at` on the share row | the derived JWT is `min(share_expiry, now + 24 h)` — shortening the row shortens the JWT, but already-issued JWTs live out their cap |
| shorten signed-URL exposure | `WORKSPACE_VIEW_TOKEN_EXPIRY` / `FILE_DOWNLOAD_TOKEN_EXPIRY` in `agent_workspace_token_service.py` | nothing else — but note these are the only tokens with no revocation at all |
| add an MFA factor | `MfaService.verify_challenge` | `register_trusted_device` (the skip must not outlive a disabled 2FA) |

## Invariants and traps

- **`get_current_user` decodes without `audience=`, and that is load-bearing.** Adding
  `options={"verify_aud": False}` there to make some other check "fire" would let both the env token
  and the `owner_identity` token resolve to the full owner `User`. The docstring in
  `backend/app/api/deps.py` says so at length; treat it as a fence, not as tidying.

- **The env token used to be a ten-year plain owner JWT.** Commit `04672206` gave it
  `aud`/`token_type`/`env_id`/`agent_id`, a 365-day TTL, and `auth_token_hash`. The legacy path is
  still open behind `AGENT_ENV_TOKEN_ACCEPT_LEGACY`, and that flag is still `True` in
  `backend/app/core/config.py`.

- **`revoked_at is NULL` means "hard revoked".** It reads like missing data. It is the discriminator
  the refresh reuse-grace window uses to refuse re-rotation; `revoke_token_family` and
  `_stamp_grant` leave it NULL on purpose, while rotation and grace-collapse stamp it.

- **A `purpose` claim is a rejection, not a requirement, on the reset path.** `verify_password_reset_token`
  accepts any `SECRET_KEY`-signed token with a `sub` — which included the invitation token until
  commit `f3d6c00c` taught it to refuse anything carrying a foreign `purpose`. An invite link would
  otherwise have been a week-long account-takeover primitive.

- **Not every token is verified against its stored hash.** `cli_token.token_hash` exists but
  `_resolve_cli_context` never compares against it: CLI auth is JWT signature + row lookup by `sub`
  + `is_revoked`. The hash and `prefix` are for uniqueness and display. Rotating `SECRET_KEY`
  invalidates every CLI token instantly; nothing else does, short of `is_revoked`.

- **Three stores keep a usable plaintext secret at rest.** `mcp_token.token` (both OAuth and direct
  tokens), `cli_setup_token.token`, and `AgentEnvironment.config["auth_token"]`. Guest and webapp
  shares keep *both* a SHA-256 hash and the plaintext, deliberately, so the owner can re-copy the
  link — the hash is the lookup key, not a confidentiality measure. Contrast the deliberate hashing
  in `user_trusted_device` (bcrypt), `desktop_refresh_token`, `app_mcp_token`, `agent_api_token`,
  `agent_access_tokens`, and the Fernet encryption in `agent_webhook`.

- **Two tokens cannot be revoked at all.** The workspace-view and file-download JWTs have no
  database row. A leaked signed URL is live for its full hour and the only kill switch is rotating
  `SECRET_KEY`, which logs out the entire platform.

- **The account CLI token's exclusions describe the token, not its holder.**
  `POST /api/v1/cli/account/desktop-token` converts it into a full user session that reaches
  everything the account routes cannot. Both halves belong in any blast-radius analysis of a leaked
  `account.json`; the `AccountCLIContext` docstring in `backend/app/api/deps.py` says exactly that.

- **Two revocation gaps are recorded, not fixed.** `ensure_not_cli_exchanged_session` notes that MCP
  OAuth consent (`backend/app/api/routes/mcp_consent.py`) is reachable from a CLI-exchanged session
  and mints a credential that outlives the cascade with no owner-reachable revocation; and
  `MCPOAuthClient` secrets have no rotate or revoke endpoint, only connector deletion.

- **An in-handler gate must be written as "not the safe action".** The consent endpoints branch on a
  body field, so they call `ensure_not_cli_exchanged_session` on the `!= "deny"` branch. Keyed on
  `== "approve"` it would silently stop firing the day a third action is added.

- **`GoogleCertsUnavailable` must not become a `ValueError`.** `verify_google_signed_jwt` catches
  `(JoseError, ValueError)` to turn Authlib's bare `ValueError` into "invalid token"; re-parenting
  the availability error under it would report a Google outage as a forgery, and a test guards that.

## See also

- Humans — [auth](../application/auth/auth.md),
  [tech](../application/auth/auth_tech.md),
  [google_oauth](../application/auth/google_oauth.md),
  [email_confirmation](../application/auth/email_confirmation.md),
  [user_2fa](../application/user_2fa/user_2fa.md),
  [access policy](../application/server_configuration/access_policy.md)
- Native clients — [desktop_auth](../application/desktop_auth/desktop_auth.md),
  [desktop_onboarding](../application/desktop_onboarding/desktop_onboarding.md),
  [cinna_cli_integration](../application/cinna_cli_integration/cinna_cli_integration.md),
  [account_cli_workspace](../application/cinna_cli_integration/account_cli_workspace.md),
  [account_device_login](../application/cinna_cli_integration/account_device_login.md)
- Machines — [a2a_access_tokens](../application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md),
  [a2a_protocol](../application/a2a_integration/a2a_protocol/a2a_protocol.md),
  [agent_api](../agents/agent_api/agent_api.md),
  [tech](../agents/agent_api/agent_api_tech.md),
  [mcp_integration](../application/mcp_integration/agent_mcp_architecture.md),
  [agent_to_agent_mcp_connector](../application/mcp_integration/agent_to_agent_mcp_connector.md),
  [app_mcp_server](../application/app_mcp_server/app_mcp_server.md)
- Shares and runtime — [guest_sharing](../agents/guest_sharing/guest_sharing.md),
  [agent_webapp](../agents/agent_webapp/agent_webapp.md),
  [agent_webhooks](../agents/agent_webhooks/agent_webhooks.md),
  [agent_environments](../agents/agent_environments/agent_environments.md),
  [agent_message_attachments](../agents/agent_file_management/agent_message_attachments.md),
  [agent_credentials](../agents/agent_credentials/agent_credentials.md)
