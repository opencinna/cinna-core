# Desktop Credential Delivery — Tech

## File Locations

### Backend

- `backend/app/api/routes/external_credentials.py` — HTTP only: claims extraction, ETag/cache
  headers, `DesktopCredentialError` → `HTTPException` mapping. No business logic.
- `backend/app/services/credentials/desktop_credential_service.py` — `DesktopCredentialService`,
  the whole authorization rule, list/materialize, rate limiting, audit.
- `backend/app/models/credentials/desktop_credential.py` — request/response schemas
  (`DesktopCredentialList`, `DesktopCredentialListItem`, `DesktopCredentialMaterializeRequest`,
  `DesktopCredentialMaterializeResponse`, `DesktopMaterializedCredential`,
  `DesktopCredentialRefusal`, `DesktopCredentialRefusalReason`). No tables — response/request
  shapes only.
- `backend/app/services/credentials/credentials_service.py` — `CredentialsService.credential_to_env_dict`
  (the single pre-whitelist entry shape, shared with cloud sync's
  `get_agent_credentials_with_data`), `prepare_credentials_for_environment` (accepts
  `selected_credentials` + `desktop_owner` kwargs so Desktop delivery reuses the exact same
  whitelist/redaction/service-account-split/`current_user`-block pipeline as environment sync, with
  no `Agent` row involved), `classify_credential_category`, `classify_owned_credentials`.
- `backend/app/models/credentials/credential.py` — `Credential.allow_local_use`,
  `Credential.updated_at` (the revision source).
- `backend/app/models/events/security_event.py` — `CREDENTIAL_MATERIALIZED_LOCAL` event-type
  constant.
- `backend/app/services/common/rate_limiter.py` — `RateLimiter`, the per-process sliding window
  used for the 60/minute cap.
- `backend/app/services/desktop_auth/desktop_auth_service.py` —
  `DesktopAuthService.is_cli_exchanged_session`, consulted both in `_require_live_client` and via
  the route's `ensure_not_cli_exchanged_session` call.
- `backend/app/api/deps.py` — `CurrentClientClaims`, `ensure_not_cli_exchanged_session` (the shared
  credential-export gate also used by the desktop-auth minting surfaces).

### Frontend

- `frontend/src/utils/credentialLocalUse.ts` — `LOCALLY_DELIVERABLE`, a `Record<CredentialType,
  boolean>` mirror of the backend's `LOCALLY_COMPATIBLE_TYPES`; `isLocallyDeliverableCredentialType`.
  The `Record` shape means a new backend `CredentialType` fails the frontend typecheck here until
  someone decides whether it is locally deliverable.
- `frontend/src/components/Credentials/CredentialSharing.tsx` — the "Allow use on recipients'
  computers" `Switch`, gated on `allowSharing && isLocallyDeliverableCredentialType(credential.type)`;
  its mutation calls `CredentialsService.updateCredential({ requestBody: { allow_local_use } })`.
- `frontend/src/components/Credentials/EditCredential.tsx` — unrelated to consent, but shares the
  credential edit surface: `agent_api` and `mcp_provider` credentials save metadata only, so a
  server-minted or -refreshed token field is never echoed back on save (`SECRET_MANAGED_TYPES` at
  the top of the file).

### Local Agent Kit

- `docs/local_agent_kit/templates/agent/scripts/cinna_credentials.py` (mirrored to
  `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/local-kit/templates/agent/scripts/cinna_credentials.py`
  by `sync_platform_knowledge.py`) — `HELPER_VERSION = "1.4.0"`. Reads, in order:
  `CINNA_CREDENTIALS_PATH` (if set), then `credentials/credentials.json` (array or `{"credentials":
  [...]}` envelope), then the legacy root object, then environment variables, then
  `credentials/.env`. `by_slot` / `require_slot` match on `service_uri` only, never on name or
  type. An attached entry the host could not deliver carries `unavailable_reason` (the Core refusal
  reason string, e.g. `local_use_not_allowed`); `_UNAVAILABLE_HINTS` turns it into a human sentence,
  and it blocks the environment-variable fallback exactly like a placeholder does.

### Migrations

- `backend/app/alembic/versions/dc920creds01_desktop_credential_delivery.py` (after `ba517c930def`)
  — adds `credential.allow_local_use` (boolean, default false) and `credential.updated_at`
  (timestamp, default now); creates `credential_touch_revision()` (`BEFORE UPDATE ON credential`)
  and `credential_share_touch_revision()` (`AFTER INSERT OR UPDATE OR DELETE ON credential_shares`,
  bumps the parent credential's `updated_at`), so every credential write and every share
  insert/update/delete changes the revision. Downgrade drops both triggers/functions and both
  columns.

### Tests

- `backend/tests/api/external/test_desktop_credentials.py` — owner/recipient/stranger/superuser
  access across the share lifecycle, list metadata + ETag transitions, materialize token gating
  (revoked client, CLI-exchanged session), incompatible types neither listed nor delivered,
  service-account side-file delivery + the 50-id request bound, editing an OAuth credential keeps
  its server-refreshed tokens (regression guard, not itself desktop-specific), materialization
  audit events for requester and owner, the 60/minute rate limit, cloud-env-sync and desktop
  delivery producing the same `api_token` shape, `email_smtp` completeness without `from_email`,
  OpenAPI naming the response models, automatic (`agent_api` connection) and bundle/skill-install
  shares neither listed nor delivered.
- `backend/tests/unit/test_local_kit_credentials_reader.py` — `cinna_credentials.py`'s precedence
  order and `unavailable_reason` handling.
- `backend/tests/utils/desktop_credentials.py` — shared test helpers for this suite.

## Database Schema

- `credential` table (existing, from `backend/app/models/credentials/credential.py`) gains
  `allow_local_use: bool` (default `False`, owner-only to set) and `updated_at: datetime`
  (server-maintained, never written by application code — it exists purely as the revision
  source).
- No new tables. `credential_shares` is unchanged in shape; its insert/update/delete now also
  touches its parent credential's `updated_at` via trigger.

## API Endpoints

- `GET /api/v1/external/credentials` (`external_credentials.py:list_credentials`, tag `external`) —
  `CurrentUser` auth. Reads `If-None-Match`; on a match returns `304` with the same `ETag` /
  `Cache-Control: private, no-cache` headers and no body. Otherwise decrypts only the visible rows
  and returns `DesktopCredentialList`.
- `POST /api/v1/external/credentials/materialize` (`external_credentials.py:materialize_credentials`)
  — `CurrentUser` + `CurrentClientClaims` (must resolve to `kind == "desktop"` with a `client_id`,
  else `403`). Calls `ensure_not_cli_exchanged_session` before touching the service. Body:
  `DesktopCredentialMaterializeRequest {credential_ids: UUID[0..50], include_current_user: bool}`.
  Response: `Cache-Control: no-store`; `DesktopCredentialMaterializeResponse`.

## Services & Key Methods

`DesktopCredentialService` (`desktop_credential_service.py`):

- `local_access_refusal(credential, user_id, shared_sources, categories) ->
  DesktopCredentialRefusalReason | None` — the single authorization rule: `not_found` → `no_access`
  (not owned and (no share or `allow_sharing` now false)) → `not_local_category` (category ≠
  "mine") → `local_use_not_allowed` (not owned and `allow_local_use` false). Used identically by
  list visibility and by materialize.
- `revision(credential) -> str` — `sha256(str(credential.updated_at))[:32]`, the opaque per-item
  revision surfaced in both list items and materialized items.
- `_shared_sources` / `_categories` — batch helpers: one query for every `CredentialShare.source`
  the caller holds, one call to `CredentialsService.classify_owned_credentials` for owned rows plus
  `classify_credential_category` per shared row.
- `list_credentials(session, user, if_none_match) -> (etag, DesktopCredentialList | None)` — selects
  `Credential` rows that are owned-or-shared **and** already type-filtered to
  `LOCALLY_COMPATIBLE_TYPES` in SQL; visibility keeps rows refused with `None` or
  `local_use_not_allowed` only (a shared-but-not-consented row is still listed, flagged
  `local_use_allowed=false`, so Desktop can explain the gap). The ETag hashes the full metadata
  list; a match short-circuits before any `decrypt_credential_data` call.
- `materialize(session, user, client_id, request) -> DesktopCredentialMaterializeResponse` —
  `_require_live_client` (401 on missing/revoked/CLI-exchanged), `_check_rate_limit` (429 + `Retry-
  After`), then per requested id: `local_access_refusal`, and if `None`, an additional
  `credential.type not in LOCALLY_COMPATIBLE_TYPES` check yielding `unsupported_type`. Selected
  entries are shaped with `CredentialsService.credential_to_env_dict` and passed as
  `selected_credentials` to `prepare_credentials_for_environment(session, None,
  selected_credentials=selected, desktop_owner=user)` — the `agent_id=None` / `desktop_owner=user`
  combination is what makes the shared pipeline attach a `current_user` block built from the
  Desktop caller instead of an agent's owner. Anything the builder itself dropped (e.g. an
  `mcp_provider` row that slipped through, defensively) is refused as `unsupported_type` rather than
  silently omitted.
- `_audit_delivery(session, user, client_id, credential_id, owner_id)` — always logs
  `CREDENTIAL_MATERIALIZED_LOCAL` (severity `high`) on the requester; if the credential's owner
  differs from the requester (a shared credential), logs a second event on the owner naming the
  recipient's id and email. Neither event carries a value.
- `LOCALLY_COMPATIBLE_TYPES: frozenset[CredentialType]` — `EMAIL_IMAP`, `EMAIL_SMTP`, `ODOO`,
  `API_TOKEN`, `GOOGLE_SERVICE_ACCOUNT`.
- `MATERIALIZE_LIMIT_PER_MINUTE = 60`.

`CredentialsService` additions this feature relies on (`credentials_service.py`):

- `credential_to_env_dict(credential, credential_data) -> dict` — the pre-whitelist entry shape
  (`id`, `name`, `type`, `notes`, `service_uri`, `is_placeholder`, `credential_data`), including the
  `api_token` → HTTP-header-pair rewrite. Used by both `get_agent_credentials_with_data` (cloud
  sync) and Desktop delivery.
- `prepare_credentials_for_environment(session, agent_id, *, selected_credentials=None,
  desktop_owner=None)` — accepts either an `agent_id` (cloud path, looks up linked credentials and
  the agent's owner) or an explicit `selected_credentials` list with `desktop_owner` (Desktop path,
  no `Agent` row). Same drop/whitelist/redact/`current_user`-block pipeline either way; see
  [Credential Materialization](../../flows/credential_materialization.md) for the full internal
  ordering.

## Frontend Components

- `frontend/src/components/Credentials/CredentialSharing.tsx` — sharing card; renders the
  local-use `Switch` only when `allowSharing && isLocallyDeliverableCredentialType(credential.type)`;
  copy next to it states that disabling cannot recall already-delivered values.
- `frontend/src/utils/credentialLocalUse.ts` — the frontend's compatibility table, kept in sync by
  hand with `LOCALLY_COMPATIBLE_TYPES`; its `Record<CredentialType, boolean>` type fails to compile
  if a new `CredentialType` is added without an explicit `true`/`false` here.

## Configuration

No dedicated settings or environment variables. `MATERIALIZE_LIMIT_PER_MINUTE` (60) and
`LOCALLY_COMPATIBLE_TYPES` are both plain module constants in `desktop_credential_service.py`, not
config-driven.

## Security

- Authorization is a single ordered rule (`local_access_refusal`) applied identically to list
  visibility and to materialize, so the two surfaces can never disagree about who may see what.
- Materialize requires `CurrentClientClaims` to resolve to an interactive `kind="desktop"` client
  and is refused for a session whose current grant came from the CLI account-token exchange
  (`ensure_not_cli_exchanged_session` in the route, `_require_live_client`'s
  `is_cli_exchanged_session` check in the service — the same predicate enforced twice, once per
  request stage).
- `POST /materialize` responses carry `Cache-Control: no-store`; `GET` list responses carry
  `Cache-Control: private, no-cache`.
- `DesktopMaterializedCredential.ssh_key` and `DesktopCredentialMaterializeResponse.owner_identity`
  are typed fields that are always `None` — kept in the schema only so an older Desktop client that
  still parses them does not break; neither `ssh_key` nor `agent_api`/`mcp_provider` credentials are
  ever delivered.
- Rate limiting is per-process (`RateLimiter`), keyed on `f"{user_id}:{client_id}"`, not a
  distributed quota.
- Auditing never records a credential value, only ids/emails, via `SecurityEventService.create_event`.
