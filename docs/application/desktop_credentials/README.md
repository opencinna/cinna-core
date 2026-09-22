# Desktop credential delivery

Desktop can list owned/shared service credentials and explicitly attach allowed records to local agents. Owner access is always allowed. Recipient delivery requires both a live share (`allow_sharing`) and owner consent (`allow_local_use`, false by default). Only the caller's "mine" credentials qualify (the My Credentials category from `CredentialsService.classify_credential_category`): owned credentials other than automatic ones (Agent API connections, agent2agent MCP providers), and credentials shared directly. Automatic and bundle credentials, including shares created by a bundle install (`bundle_install`) or a catalog skill install (`skill_install`), are never listed or delivered. Superuser status does not bypass these rules.

Only credential types that work on a computer out of the box are ever listed or delivered: email IMAP, email SMTP, Odoo, API token and Google service account (its full key JSON is delivered). Every other type is incompatible and never reaches a Desktop: the six Google OAuth types (Gmail, Drive, Calendar and their read-only variants), because refreshing their access token needs the OAuth client secret that only the backend server holds; SSH keys, because the private key never leaves Core, so the entry would be useless locally; Agent API credentials; and MCP providers, which are SDK MCP-server manifest entries rather than credential files. Attach those to cloud agents instead. The sharing page's "Allow use on recipients' computers" switch is shown only for compatible types. The sharing page explains local storage and that disabling consent cannot recall copied values.

## API

- `GET /api/v1/external/credentials`: authenticated metadata only, with relation, category (always `mine`), owner email, slot, completeness, local-use permission, revision, optional expiry, and workspace. Incompatible types and every non-"mine" credential are excluded. ETag/If-None-Match supports 304; removing a share changes the visible-list ETag. The ETag is computed from metadata plus each credential's revision, so an unchanged poll answers 304 without decrypting anything; owner emails are fetched in one batched query.
- `POST /api/v1/external/credentials/materialize`: `{credential_ids: UUID[0..50], include_current_user: boolean}`. Requires an interactive Desktop client JWT, including live client revocation checks. Browser/account CLI tokens and Desktop sessions whose current grant came from an account-token exchange cannot export values (the shared `ensure_not_cli_exchanged_session` gate, 403); a missing or revoked Desktop client gets 401. Responses are `Cache-Control: no-store`.

The result contains `items` (id, revision, cloud-shaped entry, optional full service-account JSON), `refused` (not_found/no_access/not_local_category/local_use_not_allowed/unsupported_type), and optional `current_user`. Refusals are checked per item in a fixed order (access, category, consent, then type), so a caller without access learns nothing about the credential; a requested incompatible type is refused `unsupported_type`. Service-account files are split from their ordinary entry. The `ssh_key` item field and the `owner_identity` response field are always null (SSH keys and Agent API credentials are never delivered); they stay in the schema so existing Desktop clients keep parsing.

The existing environment builder accepts an explicit selected list and a Desktop owner, keeping the transformations and field allowlists shared with cloud delivery. Authorization is one pass; nothing awaits between the check and the delivery except the audit write. The kit reader surfaces a refusal when the host writes it into the injected entry as `unavailable_reason`.

Each delivered item emits a `CREDENTIAL_MATERIALIZED_LOCAL` security event for the requester (credential and Desktop client IDs). When a recipient copies a credential shared with them, a second event goes to the owner (credential ID, recipient user ID and email), so owners can see who copied their secret locally. Events never carry values. A per-process user/client limiter permits 60 calls per minute. It is not a distributed quota.

## Implementation

- Route: `backend/app/api/routes/external_credentials.py` (HTTP only: claims, ETag and cache headers, error mapping).
- Service: `backend/app/services/credentials/desktop_credential_service.py`. `local_access_refusal` is the single authorization rule (access, then category, then consent), applied to materialize and to list visibility; categories are computed in one batch per call (`classify_owned_credentials` for owned rows, the caller's `CredentialShare.source` for shared ones); `LOCALLY_COMPATIBLE_TYPES` is the type allowlist, filtered in SQL for the list and checked per item on materialize (mirrored on the frontend in `frontend/src/utils/credentialLocalUse.ts`, which gates the "Allow use on recipients' computers" switch together with `allow_sharing`).
- Entry shaping reuses `CredentialsService.credential_to_env_dict`, the same helper cloud sync uses in `get_agent_credentials_with_data`.
- Schemas: `backend/app/models/credentials/desktop_credential.py` (`DesktopCredentialList`, `DesktopCredentialMaterializeRequest`, `DesktopCredentialMaterializeResponse`); both endpoints declare them as `response_model`, so they are typed in OpenAPI and the generated client.
- The credential edit dialog saves metadata only for `agent_api` and `mcp_provider`, so it never echoes minted or server-refreshed tokens back.

## Database and deployment

Apply Alembic revision `dc920creds01` after `ba517c930def`. It adds `allow_local_use` and `updated_at`; database triggers bump credential revisions for updates, OAuth changes, and share insert/update/delete. Existing shares remain cloud-only until consent is enabled. No data export backfill or secret migration is needed.

Deploy Core and its frontend before Desktop. Desktop uses only this delivery API, with no with-data fallback. CLI remains a metadata/import tool and cannot export secrets. The shared kit contract is 1.4.0, with one Python reader and whole-directory credential export exclusion; Core's `VERSION` remains a rendered content hash while `CONTRACT_VERSION` is the semantic contract authority.

## Validation

`tests/api/external/test_desktop_credentials.py` covers owners, allowed/disallowed shares, strangers, superusers, ETags, rotation, revocation, audit, device revocation, service-account side files, and the compatible-type rule (every incompatible type, owned or shared with consent, is neither listed nor delivered). Existing SMTP and service-URI environment tests exercise the shared builder. Kit serving/scaffolding tests cover the published contract.

Desktop's live local-Core E2E uses disposable owner/recipient accounts and interactive PKCE Desktop authorizations, attaches the same record under both profiles, rotates it, opts out recipient delivery, switches back to the owner, and verifies logout cleanup. Separate pinned-runtime probes verify external credential-file access by Claude, Codex, and OpenCode. Local admin CLI testing confirms metadata access and a 403 for materialization.
