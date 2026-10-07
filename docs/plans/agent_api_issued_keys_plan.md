# Agent API Issued Keys: issue API access to a user

Status: RECONCILED with Desktop plan, approved for build (2026-10-07). Nothing built.
Feature docs: `docs/agents/agent_api/agent_api.md` (External Keys, Caller Identity, Cross-User Sharing), `agent_api_tech.md`, `docs/application/desktop_credentials/desktop_credentials.md`, `docs/agents/agent_credentials/credential_sharing.md`.
Counterpart: cinna-desktop `plans/agent-api-issued-keys.md` (consumes §7; its §9 questions are answered by D15–D25).

## §0 Index

### Working-tree rules
- Shared tree on `main`; one commit for the whole feature (code + regenerated client + this plan), no push.
- No backward compatibility: the "Acts as" model is removed, not kept beside the new one. Old envs/Desktops degrade (see §6, §7), they are not shimmed.
- Backend tests are API-only (`backend/tests/README.md`); unit tests only where listed (SDK, kit reader). Regression scope per phase is in the phase table.

### Decisions taken (R = recommendation adopted; one-line rejected alternatives)
| # | Decision |
|---|---|
| D1 | "Acts as" becomes **Issued to** (`subject_user_id`, default = issuer). The `agent_api` credential is created with `owner_id = subject_user_id`. Issuer stays on `AgentApiToken.owner_id`. Self-issue = same flow. |
| D2 | Mint response carries `token = null` when recipient ≠ issuer. Reveal stays credential-owner-only, so recipient-only. No superuser bypass (unchanged rule in `reveal_external_key`). |
| D3 | Revocation is **soft**: the token row survives with `is_active=false`, `revoked_at`, `revoked_reason`; the recipient's credential is never deleted by the issuer side. Revoked rows stay **until the recipient deletes the credential** (R). Rejected: auto-purge after N days (silent loss of the explanation). |
| D4 | One status vocabulary, computed in one function (`key_status`), precedence top-down: `revoked_by_issuer` > `producer_removed` > `issuer_removed` > `expired` > `api_disabled` > `external_access_paused` > `active`. The first four are terminal; the last two recover by themselves. |
| D5 | FK redesign on `agent_api_token`: `agent_id` and `owner_id` become NULLABLE + `ON DELETE SET NULL` (status derived from NULL). `subject_user_id` stays CASCADE. `credential_id` stays CASCADE (recipient deleting their copy = revoking). |
| D6 | Rotation: recipient-only `POST /credentials/{id}/agent-api-key/rotate`, swaps the secret in place (same credential id, same token row). Previous hash valid for `AGENT_API_KEY_ROTATION_GRACE = timedelta(minutes=5)`; a second rotate inside the window replaces the previous hash. Terminal statuses refuse rotate (409). |
| D7 | Env sync: an external key syncs only when `credential.owner_id == token.subject_user_id == env agent.owner_id`. Non-active keys sync as a **token-less entry with `unavailable_reason` + `key_status`** (D21), not dropped. |
| D8 | Desktop: `agent_api` becomes locally compatible ONLY for owned issued keys where caller == credential owner == subject, and only when the client opts in (D22). Connections keep refusing as `unsupported_type`. Non-active keys are listed and refused on materialize with typed reasons. Entry carries the PUBLIC URLs (D18). |
| D9 | Sharing ban unchanged (`assert_sharing_allowed` already rejects external keys and template sharing). |
| D10 | Connections stay (skill distribution, bundle PBP). Steering only: an `agent_api` connection's Sharing card, when the viewer owns the producer, links to the producer's API access surface. |
| D11 | Producer UI: `AgentApiExternalKeysCard` + `AgentApiAccessScopesCard` merge into one per-person **API access** surface fed by a new aggregate endpoint. Kill switch and identity switch live there. |
| D12 | Data migration: external keys with `subject_user_id != owner_id` move credential ownership to the subject, drop issuer-side `agent_credential_link` rows, clear `user_workspace_id`, set `issuer_saw_value = true`. Self-keys untouched (except D23 backfill). |
| D13 | No notification to the recipient. Security events: CREATED (issuer), REVOKED (issuer + recipient), REVEALED, ROTATED, DELETED_BY_HOLDER (issuer account), CREDENTIAL_MATERIALIZED_LOCAL (existing). |
| D14 | Accepted risk, documented not mitigated: a recipient linking the key to an agent others can reach lets those requests hit the producer as the recipient. UI: recipient sees "Linked to agents"; issuer sees only a count. |
| D15 | (Desktop Q1) Grace stays **5 minutes**, no "rotate and end now" option. Reason: Desktop hot-writes key-only changes atomically mid-turn (its D5/D7) and the kit helper re-reads per request, so a running turn picks up the new value within one poll. |
| D16 | (Desktop Q2) `DesktopCredentialListItem.agent_api: DesktopAgentApiKeyFacts | None` = `{producer_agent_id, producer_name, issuer_email, base_url, spec_url, expires_at}` (null for non-keys); existing `expires_at` filled with the token's expiry for keys. All of it is in the ETag inputs (§6). |
| D17 | (Desktop Q3) Kit `HELPER_VERSION` 1.4.0 → **1.7.0** in the same change as contract 1.7.0. |
| D18 | (Desktop Q4) Public URLs for Desktop are **recomputed at delivery** from `settings.backend_base_url` (`BACKEND_BASE_URL` → `WEBHOOK_BASE_URL` → `FRONTEND_HOST`) via `AgentApiTokenService.build_base_url(token.agent_id)`, never the mint-time stored value, never `AGENT_ENV_BACKEND_URL`. That origin is the one Desktop must authenticate against (single-origin deploys: `FRONTEND_HOST` proxying `/api`; split deploys: `BACKEND_BASE_URL` = the API host Desktop is configured with). Desktop logs a mismatch, Core does not guess from `Host`. |
| D19 | (Desktop Q5) Kit `agent_api_session(slot, *, requests=False)` is stdlib: small client, bearer only to `base_url`'s origin including across redirects (custom redirect handler); `requests=True` returns an origin-pinned `requests.Session` when importable (§9). |
| D20 | (Desktop Q6) Kit guide 13 §3 paragraphs ("two things: token + identity header", "Locally: there is no agent API") rewritten in the same kit release. |
| D21 | (Desktop Q7) One `unavailable_reason` vocabulary for cloud `credentials.json` and Desktop entries: `key_revoked | key_expired | key_paused`, plus `key_status` (detail). Mapping lives in `unavailable_reason_for(status)` in `agent_api_key_status.py`, used by env sync and Desktop. |
| D22 | (Desktop Q8) Opt-in: `?include=agent_api_keys` on BOTH `GET /external/credentials` and `POST /external/credentials/materialize`. Without it no `agent_api` item is listed and materialize refuses any `agent_api` id `unsupported_type` (safer and consistent: "not listed ⇒ not deliverable"; a shipped Desktop can never receive a key it would mislabel). |
| D23 | (Desktop Q9) Issued key credential gets `service_uri` default `<slug(producer name)>-api` (lowercase, runs of non-`[a-z0-9]` → `-`, trimmed, slug capped 40, empty → `agent`; "CRM Agent" → `crm-agent-api`). Issuer may override (`AgentApiKeyCreate.service_uri`); recipient edits via the existing credential update; shown in recipient detail. Migration backfills NULL `service_uri` on existing external-key credentials with the same rule. Producers have no API slug today; rejected: inventing one. |
| D24 | (Desktop Q10) Stale `owner_identity` comment in `models/credentials/desktop_credential.py` rewritten in P3. |
| D25 | (Desktop Q11) Key `revision` = `sha256(f"{updated_at}|{key_status}|{expires_at}|{base_url}")[:32]`; `key_status` is computed at read time, so expiry, revoke, pause and producer/issuer removal all change `revision` without any write. Non-key revisions keep today's formula byte-for-byte. |
| D26 | (core §13.1) Recipient-deleted key: row cascades, only DELETED_BY_HOLDER audit remains. |
| D27 | (core §13.2) Terminal keys stay in the issuer's list behind a disclosure; no issuer-side purge. |
| D28 | (core §13.3) Several keys per (producer, recipient) allowed, grouped under the person, scopes shared via the grant. |
| D29 | (core §13.4) Paused keys refused on Desktop with `key_paused`; Desktop keeps polling. |
| D30 | (core §13.5) Issuer sees linked-agent count only, never names. |
| D31 | (core §13.6) User deletion has no hook; envs see `issuer_removed` on next sync, proxy 401s at once. |
| D32 | (core §13.7) Superuser-issued key flips to `issuer_removed` when that superuser is deleted; accepted. |
| D33 | (core §13.8) `GET .../agent-api/keys` and `DELETE .../keys/{id}` removed, no shim (web app is the only caller). |
| D34 | (core §13.9) "Agent API Key" stays a working name; `agentApiKeyCopy.ts` is the single rename point; dialog title "Give API access". |

### Phase table
| Phase | Files touched (main) | Plan sections | Tests (run) |
|---|---|---|---|
| P1 Data + validation core | `models/agent_api/agent_api_token.py`, new migration, `services/agent_api/agent_api_token_service.py` (`validate_token`, `is_restricted_*`, `get_kinds_by_credential`), new `services/agent_api/agent_api_key_status.py` | §1, §2 | `tests/api/agents/agent_api/` |
| P2 Issue / revoke / rotate / views | `agent_api_key_service.py` (+ `default_service_uri`), `api/routes/agent_api.py`, `api/routes/credentials.py`, `credentials_service.py` (delete hook, projection), `agent_service.delete_agent`, `models/credentials/credential.py`, `models/events/security_event.py` | §3, §4 | `tests/api/agents/agent_api/`, `tests/api/credentials/` |
| P3 Env sync + Desktop | `credentials_service.py` (`_shape_agent_api_keys_for_env`, `prepare_credentials_for_environment`), `desktop_credential_service.py`, `models/credentials/desktop_credential.py`, `api/routes/external_credentials.py` (`include` query), env SDK `app_core_base/core/cinna_api/credentials.py` | §5, §6, §7 | `tests/api/external/test_desktop_credentials.py`, `tests/api/agents/agent_api/`, `tests/unit/test_cinna_api_credentials_slots.py` |
| P4 Frontend | regenerated client, `AgentRestApiCard.tsx`, new `AgentApiAccessCard.tsx` (replaces two cards), `AgentApiKeyDialog.tsx`, `AgentApiKeyView.tsx`, `agentApiKeyCopy.ts`, `agentApiKeyMintHandoff.ts`, `useAgentApiKeys.ts`, credential list rows, connection Sharing card | §8 (+ UI Specification by `cinna-core.ui.design`) | typecheck on touched files; Playwright smoke per spec |
| P5 Kit, CLI, docs | `docs/local_agent_kit/**` (contract 1.7.0, `HELPER_VERSION` 1.7.0, guide 13), kit `cinna_credentials.py`, docs in §11; CLI as follow-up | §9, §10, §11 | `tests/unit/test_local_kit_credentials_reader.py`, `kit.py validate`, `make check-docs` |

P1 and P2 ship together. P3 depends on P2's status helper. P4 needs P2+P3 client. P5 last (Desktop's `make kit-sync` follows it).

---

## §1 Data model (P1)

`backend/app/models/agent_api/agent_api_token.py` — `AgentApiToken`:
- `agent_id: uuid | None` FK `agent.id` **ON DELETE SET NULL**, nullable (was CASCADE, NOT NULL).
- `owner_id: uuid | None` FK `user.id` **ON DELETE SET NULL**, nullable (was CASCADE). Docs call it "issuer"; column name stays.
- New `revoked_at` (timestamptz), `revoked_reason` varchar(32) (only `revoked_by_issuer` stored), `previous_token_hash` varchar(64) indexed non-unique, `previous_token_expires_at`, `rotated_at` (timestamptz), `issuer_saw_value: bool` server_default false.
- Keep `subject_user_id` (CASCADE), `credential_id` (CASCADE), `expires_at`, `is_active`, `last_used_at`. Update the module docstring: issuer vs recipient, soft revoke, grace window.

Status type: `AgentApiKeyStatus(str, Enum)` (seven D4 values) in the same file; `AgentApiKeyUnavailableReason = Literal["key_revoked","key_expired","key_paused"]`. Constant `AGENT_API_KEY_ROTATION_GRACE` in `agent_api_key_status.py`.

Schemas (same file):
- `AgentApiKeyCreate`: `subject_user_id` → `issued_to_user_id: uuid | None` (None ⇒ issuer); new `service_uri: str | None` (None ⇒ D23 default; same validation as `CredentialUpdate.service_uri`).
- `AgentApiKeyPublic` (issuer view): `issued_to: AgentApiKeyUser | None`, `status`, `revoked_at`, `rotated_at`, `linked_agent_count: int`; `is_usable = status == active`.
- `AgentApiKeyCreated`: `token: str | None` (null when issued to someone else), `base_url`, `spec_url`.
- `AgentApiKeyHolderInfo` (recipient view): `credential_id, token_prefix, status, service_uri, producer {id, name, ui_color_preset} | None, issuer: AgentApiKeyUser | None, scopes, expires_at, last_used_at, rotated_at, previous_valid_until, issuer_saw_value, base_url, spec_url, linked_agents: list[AgentApiConnectedAgent]`.
- `AgentApiKeyRotateResponse`: `token, token_prefix, previous_valid_until`.
- `AgentApiAccessEntry`: `user, grant_id | None, scopes, keys: list[AgentApiKeyPublic]`; `AgentApiAccessList`: `data, count, external_access_enabled, identity_enabled`.
- `CredentialPublic` (`models/credentials/credential.py:297`): `agent_api_key_status: str | None = None` (external-key rows only).

## §2 Migration and validation core (P1)

Migration `add_agent_api_issued_key_columns.py` (`down_revision` = `alembic heads`):
1. Add the columns (§1); `issuer_saw_value` with `server_default=sa.false()`.
2. Recreate FKs `agent_api_token_agent_id_fkey`, `agent_api_token_owner_id_fkey` as `ON DELETE SET NULL`; `DROP NOT NULL`. Check real names with `\d agent_api_token`; do not guess.
3. Data (D12, D23), one SQL statement each:
   - `UPDATE credential c SET owner_id = t.subject_user_id, user_workspace_id = NULL FROM agent_api_token t WHERE t.credential_id = c.id AND t.kind='external' AND t.subject_user_id IS NOT NULL AND t.subject_user_id <> t.owner_id`.
   - `DELETE FROM agent_credential_link l USING agent_api_token t, agent a WHERE l.credential_id = t.credential_id AND l.agent_id = a.id AND t.kind='external' AND a.owner_id <> t.subject_user_id`.
   - `UPDATE agent_api_token SET issuer_saw_value = true WHERE kind='external' AND subject_user_id <> owner_id`.
   - Delete any `credential_share` rows for those credentials (expected zero).
   - Backfill `credential.service_uri` where NULL for external keys: `coalesce(nullif(left(trim(both '-' from regexp_replace(lower(a.name), '[^a-z0-9]+', '-', 'g')), 40), ''), 'agent') || '-api'` joined via token→agent. Must equal `default_service_uri` (test pins one name).
4. Downgrade: drop columns, restore CASCADE/NOT NULL after deleting rows with NULL `agent_id`/`owner_id`; ownership transfer and backfill not reversed (one-way, documented).

Known gap kept: key credentials orphaned before this migration cannot be identified; they stay as today.

`agent_api_key_status.py` (new, pure, no DB writes):
- `key_status(token, agent: Agent | None, now) -> AgentApiKeyStatus` per D4: `revoked_at` set or `is_active=False` → `revoked_by_issuer`; `agent_id is None` → `producer_removed`; `owner_id is None` → `issuer_removed`; expired → `expired`; `agent.agent_api_enabled` false → `api_disabled`; `agent.agent_api_external_access_enabled` false → `external_access_paused`.
- `unavailable_reason_for(status) -> AgentApiKeyUnavailableReason | None`: terminal three → `key_revoked`; `expired` → `key_expired`; `api_disabled | external_access_paused` → `key_paused`; `active` → None (D21).
- `key_states_by_credential(session, credential_ids) -> dict[cred_id, KeyState]`; `KeyState = (token, agent | None, issuer_email | None, status)`. One query token ⟕ agent ⟕ user. Used by projections, env sync, Desktop.

`AgentApiTokenService.validate_token` (`agent_api_token_service.py:638`):
- Look up by `token_hash == h OR (previous_token_hash == h AND previous_token_expires_at > now)`, still `agent_id == agent_id` (NULL never matches) and `is_active`.
- `kind == external`: reject when `owner_id is None`, kill switch off, expired. Same 401 for all; no `last_used_at` bump on rejection.
- Connection rows with `agent_id NULL` are dead (already never match).

`is_restricted_agent_api_credential` (`:739`) and `get_kinds_by_credential` unchanged. Connection rows with token `agent_id IS NULL` are dropped from env sync (P3, §5).

## §3 Services (P2)

`AgentApiKeyService` (`agent_api_key_service.py`):
- `create_key` (`:77`): `recipient = issued_to_user_id or owner_user_id` (404 if unknown). Grant upsert on `(producer, recipient)`. Credential `owner_id = recipient.id` (`:203`), `service_uri = data.service_uri or default_service_uri(agent.name)`; workspace = producer's workspace only when `agent.owner_id == recipient.id` (`:180`); notes "Issued by {issuer.email} for {agent.name}". `token` returned only when `recipient.id == owner_user_id`. Audit CREATED on issuer with `issued_to`. Compensation path unchanged.
- New `default_service_uri(name) -> str` (static, D23).
- `list_keys` → `list_access(session, producer_id, user_id, is_superuser) -> AgentApiAccessList`: union of grants and external tokens, grouped by user; `linked_agent_count` batched. Terminal keys included.
- `revoke_key` (`:293`): no credential delete. `is_active=False, revoked_at=now, revoked_reason='revoked_by_issuer', previous_token_hash=None`. Touch credential `updated_at`, then `CredentialsService.sync_credentials_to_agent_environments` for the recipient's linked agents. Idempotent (200). Audit REVOKED on issuer and recipient.
- `rotate_key(session, credential_id, user_id) -> AgentApiKeyRotateResponse`: credential owner only (404 otherwise, no superuser bypass); 400 if not a key; 409 if terminal. New value; `previous_token_hash = old`, `previous_token_expires_at = now + GRACE`, `rotated_at = now`, `issuer_saw_value = False`; write `credential_data.token` through the existing credential update path (keeps `:2017` carry-forward; `service_uri` untouched) so env sync and `updated_at` fire. Audit ROTATED.
- `reveal_external_key` (`:385`): unchanged plus 409 on terminal status.
- `get_holder_info(session, credential_id, user_id)`: owner-only, 404 otherwise; scopes from `(producer, subject)` grant.
- `on_holder_delete(session, credential)` from `CredentialsService.delete_credential` (`:2270`) for external keys: audit DELETED_BY_HOLDER on issuer. Token cascades.
- `is_external_key_credential` docstring: drop subject wording.

Other services:
- `AgentService.delete_agent` (`agent_service.py:620`): collect the producer's external-key credential ids first; after delete, env sync for agents linked to them. User deletion: no hook (D31).
- Kill switch / `agent_api_enabled` toggle in `AgentService.update_agent`: after commit, sync envs linked to that producer's external-key credentials and touch their `updated_at`.
- Projections (`routes/credentials.py:177`, `routes/agents.py:348`): fill `agent_api_key_status` from `key_states_by_credential` (batched).
- `security_event.py`: `AGENT_API_EXTERNAL_KEY_ROTATED`, `AGENT_API_EXTERNAL_KEY_DELETED_BY_HOLDER`.

## §4 Routes (P2)

| Method + path | Change | Auth |
|---|---|---|
| `POST /agents/{id}/agent-api/keys` | body `issued_to_user_id`, `service_uri`; `token` null for others | producer owner / superuser |
| `GET /agents/{id}/agent-api/keys` | **removed** → `GET /agents/{id}/agent-api/access` → `AgentApiAccessList` | same |
| `DELETE /agents/{id}/agent-api/keys/{key_id}` | **removed** → `POST /agents/{id}/agent-api/keys/{key_id}/revoke` → `AgentApiKeyPublic` | same |
| `GET/POST/PUT/DELETE /agents/{id}/agent-api/grants*` | unchanged | same |
| `GET /credentials/{id}/agent-api-key` | new → `AgentApiKeyHolderInfo` | credential owner only |
| `POST /credentials/{id}/agent-api-key/rotate` | new → `AgentApiKeyRotateResponse` | credential owner only |
| `POST /credentials/{id}/agent-api-key/reveal` | 409 on terminal status | credential owner only |
| `GET /credentials/{id}/agent-api-key/spec` | new: producer spec for the holder; 409 on terminal | credential owner only |

Routes stay thin: map `AgentApiError` via `_handle_agent_api_error`.

## §5 Env sync (P3)

Replace `_drop_external_agent_api_keys` (`credentials_service.py:274`) with `_shape_agent_api_keys_for_env(session, credentials, env_owner_id, for_desktop: bool)`:
- Connection rows: kept only when kind == connection AND token `agent_id IS NOT NULL` (fail-closed: lookup fails ⇒ drop every agent_api).
- External rows: kept only when `credential.owner_id == token.subject_user_id == env_owner_id`; otherwise dropped (logged).
- Kept non-active external rows (D21): `credential_data` reduced to `{base_url, spec_url, label, producer_agent_id}`, entry gets `unavailable_reason = unavailable_reason_for(status)` and `key_status = status`, no `token`. Same shape cloud and Desktop.
- `for_desktop`: `base_url`/`spec_url` replaced by `build_base_url/build_spec_url(token.agent_id)` (D18); for `producer_removed` the stored URLs are kept (nothing to rebuild from).
- `prepare_credentials_for_environment` (`:1509`) passes `env_owner_id` = agent owner (cloud) or `desktop_owner.id`.
- README render: an entry with `unavailable_reason` shows "Unavailable: <reason>" instead of the token line.
- `_rewrite_agent_api_urls_for_env` (`:696`) runs only for cloud (skip when `desktop_owner` is set).
- `owner_identity_token` block (`:1650`): skipped when `desktop_owner` is set (no warning log).

Env-core SDK `app_core_base/core/cinna_api/credentials.py`: `require_slot` / `agent_api_session` raise `CredentialMissing(slot, reason=<unavailable_reason>)` (reason ∈ `not_linked | not_configured | key_revoked | key_expired | key_paused`, `.key_status` attribute) for an entry carrying `unavailable_reason`; `_find_slot` prefers available entries. `str(exc)` names the reason in §8 words. Requires env rebuild; an old container sees a token-less entry and gets a 401 (accepted).

Expiry is time-based: a container keeps an expired key until its next sync; the proxy 401s meanwhile.

## §6 Desktop delivery (P3)

`api/routes/external_credentials.py`: both handlers take `include: str | None = Query(None)` (comma-separated; unknown values ignored); `include_agent_api_keys = "agent_api_keys" in values`, passed to the service.

`desktop_credential_service.py`:
- `LOCALLY_COMPATIBLE_TYPES` (`:60`) gains `AGENT_API`; update the comment (`:58`).
- `local_access_refusal` (`:93`) gains `include_agent_api_keys`; for `agent_api` after the category check: flag absent, not owned, bound kind ≠ external, or `subject_user_id != user_id` ⇒ `unsupported_type`.
- Status gate (materialize only): `unavailable_reason_for(status)` non-null ⇒ refusal with that reason and `key_status`.
- `list_credentials(session, user, if_none_match, include_agent_api_keys)`: keys listed whatever their status. One `key_states_by_credential` call for listed keys; metadata (ETag input) gains `key_status`, `expires_at` (token expiry, ISO-8601 UTC) and `agent_api` facts (D16; `producer_agent_id/base_url/spec_url` null when `producer_removed`, `issuer_email` null when `issuer_removed`). Non-key items: `key_status`, `agent_api` null; their `expires_at` stays from the payload (outside metadata as today).
- `revision(credential, key_state=None)`: D25 for keys; unchanged formula otherwise. Used by list and materialize.
- Audit unchanged: `CREDENTIAL_MATERIALIZED_LOCAL` on the requester.
- `models/credentials/desktop_credential.py`: `DesktopCredentialRefusalReason` + `key_revoked | key_expired | key_paused`; `DesktopCredentialRefusal.key_status: str | None`; `DesktopCredentialListItem.key_status: str | None`, `agent_api: DesktopAgentApiKeyFacts | None`; new `DesktopAgentApiKeyFacts`. Rewrite the `owner_identity` comment (D24): "Always null: issued keys bind identity to the key, connections are never delivered to a device. Kept so existing Desktop clients parse."

## §7 Desktop-facing contract (final, self-contained)

### Contract changes after reconciliation (none yet)

**Endpoints.** `GET /api/v1/external/credentials?include=agent_api_keys` (ETag / `If-None-Match` → 304) and `POST /api/v1/external/credentials/materialize?include=agent_api_keys` (`{credential_ids: [...]}`). Auth unchanged: live, interactive (non-CLI-exchanged) Desktop client; 60 materialize calls/min per (user, client). **Without `include=agent_api_keys`** no `agent_api` item is listed and materialize refuses any `agent_api` id as `unsupported_type`. Shipped Desktops (≤ 0.6.0) never send it, so they never see keys.

**Eligibility.** Type `agent_api` only when the caller owns the credential AND it is an issued key (not a connection) AND it was issued to the caller. Every other `agent_api` credential is refused `unsupported_type` and never listed.

**List item** (all other fields unchanged), for keys:
- `type: "agent_api"`, `relation: "owned"`, `local_use_allowed: true`, `status: "complete"`, `service_uri`: the slot (default `<producer-slug>-api`, recipient-editable).
- `key_status: "active" | "revoked_by_issuer" | "producer_removed" | "issuer_removed" | "expired" | "api_disabled" | "external_access_paused"`; null for non-keys. Unknown future values: treat as unavailable.
- `expires_at`: the key's expiry, ISO-8601 UTC string, or null (never expires).
- `agent_api: {producer_agent_id, producer_name, issuer_email, base_url, spec_url, expires_at}` (no secrets); null for non-keys. `producer_agent_id/base_url/spec_url` null once the producer is removed; `issuer_email` null once the issuer is removed.

**Materialize, delivered entry** (`items[].entry`, active keys only), same shape as a cloud `credentials.json` entry:
```
{ "id": "<credential uuid>", "name": "...", "type": "agent_api", "notes": "...",
  "service_uri": "<slot>", "is_placeholder": false,
  "credential_data": { "base_url": "<public origin>/api/v1/agent-api/<producer id>",
                       "spec_url": "<public origin>/api/v1/agent-api/<producer id>/openapi.json",
                       "token": "<secret>", "label": "...", "producer_agent_id": "<uuid>" } }
```
Call with `Authorization: Bearer <token>`. Identity is bound to the key; no `X-Cinna-Caller-Identity` header is needed or honoured. `owner_identity` in the response stays null.

**Public origin rule.** `<public origin>` = `settings.backend_base_url` = first non-empty of `BACKEND_BASE_URL`, `WEBHOOK_BASE_URL`, `FRONTEND_HOST` (no trailing slash), recomputed on every list/materialize, never the Docker-internal `AGENT_ENV_BACKEND_URL`. Operators must configure Desktop's server URL to this origin; Desktop logs (does not refuse) a mismatch.

**Refusals** (`refused[]`, per item, never batch-fatal; each key refusal carries `key_status`): existing `not_found`, `no_access`, `not_local_category`, `local_use_not_allowed`, `unsupported_type`, plus:
- `key_revoked` (`revoked_by_issuer | producer_removed | issuer_removed`): terminal; user deletes it or asks the issuer for a new key.
- `key_expired` (`expired`): terminal.
- `key_paused` (`api_disabled | external_access_paused`): recovers without user action; keep polling.

**`unavailable_reason` vocabulary (shared cloud/Desktop).** A token-less entry carries `unavailable_reason ∈ {key_revoked, key_expired, key_paused}` and `key_status` (detail), `credential_data = {base_url, spec_url, label, producer_agent_id}`. Cloud env sync writes exactly this; Desktop writes the same on a key refusal (dropping the token is the recall; Core does not recall copied values).

**Revision / ETag.** Key `revision` = `sha256(updated_at | key_status | expires_at | base_url)[:32]`, `key_status` computed at read time. So: rotate (`updated_at`), revoke, kill switch, API disabled, producer/issuer removed, expiry (no write; flips on the first call after `expires_at`), and a change of the public origin all change `revision` and the list ETag. Producer rename / issuer email change the ETag only. Recipient deletes the credential: item disappears, materialize `not_found`. Non-key revisions unchanged.

**Rotation grace.** The previous token keeps working **5 minutes** after a rotate. Desktop hot-writes key-only changes atomically mid-turn and the kit helper re-reads per request, so no "rotate and end now" variant exists.

**Kit.** Local Agent Kit contract **1.7.0**, `cinna_credentials.py` `HELPER_VERSION = "1.7.0"`. `agent_api_session(slot, *, requests=False)`:
- Resolves the slot like `get_credential`; non-`agent_api` type ⇒ `CredentialError`; `unavailable_reason` ⇒ `CredentialError` naming the human reason and code, e.g. "credential slot 'crm-api' is unavailable: the key was revoked by its issuer (key_revoked)".
- Default: stdlib client with `.base_url`, `.spec_url`, `request(method, path, *, params, json, data, headers, timeout=30)`, `get/post/put/patch/delete`, `get_json/post_json`; responses expose `.status`, `.headers`, `.text`, `.json()`, `.ok`, `raise_for_status()`. Relative paths join `base_url`. Token re-read from the credential files on every request.
- `Authorization` is sent only when the target origin (scheme, host, port) equals `base_url`'s, including on every redirect hop (custom `HTTPRedirectHandler`: re-attach on same-origin, strip otherwise; https→http is a different origin).
- `requests=True`: returns a `requests.Session` with the same origin pin (`rebuild_auth` override) and token fixed at creation; `CredentialError` if `requests` is not importable.

## §8 Frontend surfaces (P4) — inventory only

Composition by `cinna-core.ui.design` (appends `## UI Specification`). Status vocabulary (one source, `agentApiKeyCopy.ts`): active "Active"; revoked_by_issuer "Revoked by issuer"; producer_removed "Producer removed"; issuer_removed "Issuer account removed"; expired "Expired"; api_disabled "API turned off by producer"; external_access_paused "External access paused". Terminal = neutral/destructive; paused/disabled = warning; must pass contrast.

1. **Producer API access** — host `AgentRestApiCard.tsx:420-430`; replaces `AgentApiExternalKeysCard.tsx`, `AgentApiAccessScopesCard.tsx`. Intent: see who has access, scopes, cut it. Data: per person — user, scopes, keys (prefix, status, last used, expiry, linked agent count); kill switch; identity switch. Actions: give access, edit/remove scopes, revoke key, toggle external access, toggle identity. API: `GET .../agent-api/access`, grant routes, `POST .../keys`, `POST .../keys/{id}/revoke`, agent update. Lists capped at 5 + Show all; ≤ 2 inline row actions.
2. **Give API access dialog** — `AgentApiKeyDialog.tsx` (also from `/credentials` → New → "Agent API Key"). Data: producer, **Issued to** (default self, `UserAllowlistPicker` with `includeSelf`), scopes, expiry, optional slot (`service_uri`, prefilled with the D23 default). Copy: key lands in the recipient's credentials; you will not see its value; you can revoke it anytime. Result: self ⇒ existing handoff (`agentApiKeyMintHandoff.ts`); other ⇒ confirmation in place.
3. **Recipient key detail** — `AgentApiKeyView.tsx` via `AgentApiCredentialDetail.tsx`. Data: status, prefix, masked value, slot (`service_uri`, editable), base URL, curl, producer, issuer, scopes (read-only), expiry, last used, rotated at, previous-valid-until, `issuer_saw_value` notice "The issuer has seen this value; rotate it", linked agents + D14 note. Actions: reveal, copy, rotate (confirm mentions the 5-minute overlap), view spec, delete. Terminal: value/curl/rotate/reveal unavailable, "delete this key" path.
4. **Credential list rows and agent Credentials tab rows** — `agent_api_key_status` badge when not active.
5. **Connection Sharing card steering** — hint + link to the producer's API access surface, only when the viewer owns the producer.

State: query keys `["agentApiAccess", agentId]`, `["agentApiKeyHolder", credentialId]`; rotate/revoke/reveal invalidate those plus `["credentials"]` and the agent credentials key. `useAgentApiKeys.ts` rewritten. Remove every "Acts as"/subject string.

## §9 Local Agent Kit (P5)

Source `docs/local_agent_kit/` (rendered copy under `backend/app/env-templates/platform-knowledge-env/.../local-kit/`, regenerated by tooling, never hand-edited).
- Contract 1.6.0 → **1.7.0**: `CONTRACT_VERSION`, `kit.json`, `layout.json` agree. `CHANGELOG.md`: Desktop may deliver `agent_api` entries (issued keys only); `unavailable_reason` gains `key_revoked | key_expired | key_paused` with `key_status` detail (same on cloud); `agent_api_session`; `HELPER_VERSION` 1.7.0.
- `templates/agent/scripts/cinna_credentials.py`: `HELPER_VERSION = "1.7.0"` (`:30`); `_UNAVAILABLE_HINTS` (`:73`) gains `key_revoked` "the key was revoked by its issuer or its producer is gone", `key_expired` "the key has expired", `key_paused` "the producer has paused API access"; `_unavailable_hint` appends `key_status` when present. New `agent_api_session` per §7 Kit (stdlib only, `urllib.request`, no new dependency). `_match` already skips entries with `unavailable_reason`.
- Guide 13 (`guides/13-design-patterns.md:162-177`): replace "every call carries **two** things" with: a connection carries token + identity header; an issued key carries only the token (identity is the key's recipient); in the cloud `credentials.agent_api_session("<slot>")`, locally `cinna_credentials.agent_api_session("<slot>")` with the key attached in Desktop. Replace "*Locally:* there is no agent API" with: locally an agent can call a producer through an issued key; keep the boundary module rule; the producer itself still lives only in the cloud.
- Guide 04 (credentials): one sentence — an `agent_api` slot works locally only with an issued key, matched by its `service_uri`.

## §10 Account CLI (follow-up, not in this feature)

Follow-up: `cinna agent-api access list|issue|revoke <producer>` via `/account/*` (never prints a token for someone else; once for self-issue), and `cinna credentials` showing `agent_api_key_status`.

## §11 Docs (P5)

- `docs/agents/agent_api/agent_api.md`: "External Keys" → "Issued keys" (issued to, recipient ownership, slot default, no secret for issuer, soft revoke + statuses, rotation grace, env sync rule, Desktop opt-in), update "Two products", "Token Model", "UI States", "Cross-User Sharing" (D10), D14 risk.
- `agent_api_tech.md`: token columns + FKs, `key_status`/`unavailable_reason_for`, routes table, `validate_token` grace lookup.
- `desktop_credentials.md` + `_tech.md`: `include=agent_api_keys`, issued keys, `agent_api` facts, refusal reasons, public origin rule, revision rule.
- `credential_sharing.md`: "issued keys are owned by the recipient, never shareable".
- `docs/agents/agent_credentials/agent_credentials.md`: `unavailable_reason` + `key_status` on cloud `credentials.json` entries.
- `docs/flows/flow_auth_token_taxonomy`, `flow_credential_materialization`: key lifecycle and Desktop path.
- `docs/application/local_agent_kit/local_agent_kit.md`: contract 1.7.0, helper 1.7.0.
- Frontmatter `affects` unchanged unless `docs_index.py check` says otherwise.

## §12 Tests (checklist by file)

`tests/api/agents/agent_api/agents_agent_api_external_keys_test.py` (rewrite):
- [ ] self-issue: credential owned by issuer, token in response, reveal works, `service_uri` = default slug (`"CRM Agent"` → `crm-agent-api`).
- [ ] issue to B with explicit `service_uri`: stored as given; B edits it via credential update; rotate keeps it.
- [ ] issue to B: credential owned by B, `token` null, A cannot reveal/read (404), B can.
- [ ] B's key authenticates; caller headers name B; scopes from `(producer, B)` grant.
- [ ] revoke: 401, B's credential survives, `revoked_by_issuer`, reveal/rotate 409, idempotent.
- [ ] producer deleted ⇒ `producer_removed`; issuer deleted ⇒ `issuer_removed`, 401.
- [ ] kill switch off ⇒ `external_access_paused`, 401, no `last_used_at` bump; on ⇒ works. Expiry ⇒ `expired`, 401.
- [ ] rotate: new works, old works within grace, old 401 after grace (patch constant), second rotate kills first old hash, credential id and links unchanged, `issuer_saw_value` cleared.
- [ ] A cannot rotate (404); superuser cannot rotate or reveal B's key (404).
- [ ] B deletes credential: token gone, A's access list drops it, DELETED_BY_HOLDER on A.
- [ ] sharing/template sharing refused for B; `/access` groups grants + keys per user; non-owner 404.
- [ ] security events: CREATED, REVOKED (A and B), REVEALED, ROTATED.

`tests/api/agents/agent_api/agents_agent_api_test.py`: connections unaffected; connection with deleted producer not synced.

`tests/api/agents/agent_api/agents_agent_api_key_env_sync_test.py` (new):
- [ ] B's key on B's agent in `credentials.json` with public→internal URL rewrite.
- [ ] revoked ⇒ token-less, `unavailable_reason: key_revoked`, `key_status: revoked_by_issuer`; paused ⇒ `key_paused`; expired ⇒ `key_expired`.
- [ ] never syncs to an agent not owned by B.

`tests/api/external/test_desktop_credentials.py`:
- [ ] without `include`: B's key not listed; materialize of its id ⇒ `unsupported_type`.
- [ ] with `include`: listed with `key_status`, `expires_at` (token expiry), `agent_api` facts (producer name, issuer email, URLs); materialize ⇒ public URLs + token + `service_uri`.
- [ ] public origin: settings patched `BACKEND_BASE_URL=""`, `WEBHOOK_BASE_URL=""`, `FRONTEND_HOST="https://cinna.example.test"` ⇒ `base_url == "https://cinna.example.test/api/v1/agent-api/<id>"` in list facts and entry, and no `AGENT_ENV_BACKEND_URL` netloc anywhere; `BACKEND_BASE_URL="https://api.example.test"` ⇒ that origin wins, `revision` changes.
- [ ] A (issuer) never sees it; connection (owned or shared) ⇒ `unsupported_type` even with `include`.
- [ ] revoke ⇒ ETag + `revision` change, `key_revoked` + `key_status`; paused ⇒ `key_paused`; expired (past `expires_at`, no write) ⇒ `revision` change + `key_expired`.
- [ ] rotate ⇒ `revision` change, new token delivered; producer rename ⇒ ETag change, `revision` unchanged.
- [ ] non-key credential `revision` identical to the pre-change formula.

Migration test (`tests/infra/` scratch-DB group): subject≠issuer key moves to subject, issuer links removed, flag set; self-key untouched except `service_uri` backfill matching `default_service_uri`.

`tests/unit/test_cinna_api_credentials_slots.py`: `require_slot` / `agent_api_session` on an `unavailable_reason` entry raise `CredentialMissing` with that reason and `key_status`; `by_slot` prefers an available entry.

`tests/unit/test_local_kit_credentials_reader.py`: `HELPER_VERSION == "1.7.0"`; hint for each new reason; `agent_api_session` joins relative paths, sends bearer to base origin, re-reads the token per request, strips `Authorization` on a cross-origin redirect and keeps it on a same-origin one (local `http.server` pair), raises on unavailable entry and on wrong type; `requests=True` path skipped when `requests` absent.

## §13 Risks and open questions — all ADOPTED (D26–D34)

1. Issuer's view of a recipient-deleted key: row cascades, audit only. **Adopted (D26).**
2. Terminal rows in the issuer's list: disclosure, no purge. **Adopted (D27).**
3. Multiple keys per (producer, recipient): allowed, grouped. **Adopted (D28).**
4. Paused keys on Desktop: refuse `key_paused`, keep polling. **Adopted (D29).**
5. Linked-agent visibility for the issuer: count only. **Adopted (D30).**
6. User deletion has no hook: accept, proxy 401s at once. **Adopted (D31).**
7. Superuser-issued key ⇒ `issuer_removed` on superuser deletion: accept. **Adopted (D32).**
8. Removal of `GET .../keys`, `DELETE .../keys/{id}`: accept (no external caller; cinna-cli checked). **Adopted (D33).**
9. Product name: copy file is the rename point; "Give API access". **Adopted (D34).**

## UI Specification

_Produced by cinna-core.ui.design on 2026-10-07. Composition decisions here override §8; data and API decisions stay with the plan._

**Overrides of §8, stated once.** (a) S1 is **not** nested inside `AgentRestApiCard` (§8 "host `AgentRestApiCard.tsx:420-430`"): that card already holds status, boot error, a button row and the Connections list, and two bordered sub-cards inside it are cards-in-a-card (§2 A card, ≤ 7 blocks). `AgentApiAccessCard` is its own half-width card in the Integrations grid. (b) The producer picker in S2 is a trigger + `Popover`, not the sibling `AgentSelectorDialog` the dialog uses today (§2 Choosing one entity in a form, R22). (c) Scopes on S3 are read-only (as §8 says) — the `KeyScopesSection` editor leaves the recipient view. (d) S2's slot is a **placeholder** showing the D23 default, sent as `null` when untouched, so the server stays the one source of the slug; the plan's "prefilled" is honoured visually.

### Shared status treatment (all surfaces)

One module, `agentApiKeyCopy.ts`, exports `agentApiKeyStatusLabel(status)` (the §8 vocabulary; unknown value → "Unavailable", via `||` per §2 Wire strings), `agentApiKeyStatusSentence(status, {issuerLabel, expiresAt})` (the one-sentence explanation below), `isTerminalAgentApiKeyStatus(status)` (the four terminal values **and** any unknown value), and `isPausedAgentApiKeyStatus(status)`. One new component, `Credentials/AgentApiKeyStatusBadge.tsx`, renders the badge everywhere a badge is used (S3 header, both S4 lists — §2 The same entity, before and after).

| Status | Label | Badge (recipient views) | Row dot, recipient | Row dot, issuer (S1) |
|---|---|---|---|---|
| active | Active | none (absence of a problem renders nothing, §2 Row state / Absent facts) | `on` | `on` |
| api_disabled, external_access_paused | per §8 | `border-transparent bg-warning text-warning-foreground` (solid) | `warning` | `warning` |
| revoked_by_issuer, producer_removed, issuer_removed, expired, unknown | per §8 | `variant="destructive"` (solid, shared variant) | `error` | `off` + `muted` (history, behind the D27 disclosure) |

Contrast rule (R14a, and the addons-P3 measurement): a status colour is **solid with its `-foreground` partner, or a dot** — never `text-warning`/`text-destructive` text on a `/10` tint of itself (measures ~2–3:1). No raw palette classes (`amber-*`, `rose-*`) on anything this phase writes. Badge `h-5` so a badged line is not taller.

Sentences (tooltip on S4 badges, body of S3's state `Alert`): revoked_by_issuer "{issuer} revoked this key. It no longer works — delete it, or ask them for a new one."; producer_removed "The agent this key called was deleted. The key no longer works — delete it."; issuer_removed "The account that issued this key was removed. The key no longer works — delete it."; expired "This key expired {RelativeTime}. Delete it, or ask the issuer for a new one."; api_disabled "The producer turned its API off. The key works again when it is turned back on — nothing to do."; external_access_paused "The producer paused external access. The key works again when it is turned back on — nothing to do."

### Surfaces

| # | Surface | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---|---|---|---|---|---|---|
| S1 | API access card | agent page › Integrations grid, after Agent REST API | View (who has access) | card, half-width (§2 A card, Card width) | P5 preview + P3 rows, row-as-control | 4 blocks; 5 person rows + Show all (N) → Sheet; per row 0 inline + `⋯` (Give another key · Edit scopes · — · Remove scopes) | checklist + screenshots |
| S1a | Person access dialog | opened by an S1 row | View (one person's keys) | dialog (§2 Disclosure depth, Dialog body) | read-only detail dialog; key rows = connected-machines shape (P5) | 2 blocks; key rows uncapped in a `max-h-[40vh]` pane; 1 hover-revealed inline action (Revoke) per non-terminal key; ended keys behind 1 disclosure | checklist + screenshots (S1's 2nd capture) |
| S1b | Edit scopes / Grant scopes dialog | S1 row menu, S1 secondary link | Edit | dialog, 1–2 fields (§1 Edit) | existing `GrantDialog`, moved | 2 fields | checklist |
| S2 | Give API access dialog | S1 header; `/credentials` › New › Agent API Key | Create | dialog, ≤ 3 visible fields + Advanced (§1 Create) | Create dialog + P6 success panel | visible: (Producer) · Issued to · Scopes; Advanced: Expires · Name · Slot | checklist |
| S3 | Recipient key detail | `/credential/$id` (agent_api key branch) | View (+ rotate / delete of one key) | route body: top card + 2-card grid (unchanged layout) | P7-like single card + two fact/form cards | top card ≤ 5 blocks; Details 1 form; Access 2 blocks | checklist |
| S4 | Key status badge on credential lists | `/credentials` (`CredentialCard`), agent › Credentials tab table | View | passive state (§5 Badge) | `AgentApiKeyStatusBadge` | 1 badge, only when ≠ active | checklist |
| S5 | Connection Sharing steering hint | `/credential/$id` (agent_api connection branch) › Sharing card | View | one line in an existing card (§2 Links in a fact list) | copy + plain link | 1 line, 1 link | checklist |

### S1 — API access card (`Agents/AgentApiAccessCard.tsx`)
- **Intent:** the producer's owner wants to see who can call this API from outside, with what scopes, and cut one key off.
- **Layout:** shared card skeleton (§3) — `KeyRound h-5 w-5` + "API access", primary `Button size="sm"` **"Give access"** (`Plus`) on the title row; `CardDescription`: "People who can call this agent's API with a key, and what they may do." Body, in order: (1) **External access** `Switch` line — label + one-line hint, on: "Issued keys work. Turn off to pause every key at once."; off: "Every issued key is paused until you turn this back on."; (2) **Per-user scopes** `Switch` line — on: "Callers carry the scopes you grant; your API reads them via `caller.scopes`."; off: "Callers are identified but carry no scopes."; `Separator`; (3) paused `Alert` (only when external access is off **and** ≥ 1 non-terminal key): "{n} keys are paused while external access is off." — no tint text, default `Alert` with `PauseCircle`; it clears itself when the switch is turned on (§6 Result feedback that stays); (4) `PreviewList` of person rows. Below the list, only when Per-user scopes is on: `Button variant="link"` "Grant scopes without a key" → S1b in grant mode (for people who call through their own agents' connections). Rendered only when `agent_api_enabled`; when it is off the card is absent (the REST API card says to enable first).
- **Siblings:** Integrations grid cards (`GuestShareCard`, `McpConnectorsCard`, `WebappShareCard`, `AgentRestApiCard`, `GitVersioningCard`) all use icon `h-5 w-5` + noun title, half width — matches. Placed in `AgentIntegrationsTab.tsx` directly after `<AgentRestApiCard>`.
- **Rows (`ApiAccessPersonRow.tsx`, shared by card and Sheet):** `ListRow`, whole row is the Details control opening S1a (§2 Row as the control: `div role="button"`, no `rounded-*`, `currentTarget.contains` + `closest("button")` guards, ignores clicks while pending, `focus-visible:ring-inset`). Status dot = best key: `on` if any active key; else `warning` if any paused; else `off` + `muted` if keys exist and all ended; **no dot** for a scopes-only person. Title = full name || email. `meta` = `email · {n} keys` (email only when a name is shown; `{n}` counts non-terminal keys and is omitted at 0 — §2 Absent facts). Flags: `RowFlag ShieldCheck` "Scopes: a, b, c" (only when scopes > 0); `RowFlag Lock` "Read-only key" when any non-terminal key is read-only; `RowInfo`: "Last used {formatRelativeTimestamp}" or "Never used", "{k} ended keys" (when k > 0), "Linked to {m} agents" (sum of `linked_agent_count`, D30 — count only). Sort: active first, then paused, then most recent `last_used_at`, then name.
- **Actions:** header "Give access" → S2 with producer fixed; **disabled** with a `Tooltip` "Turn on External access to give keys" while the switch is off (wrap the button in a `span` for the tooltip). Row `⋯` (`RowActionsMenu label="{person}'s API access"`): "Give another key" (`KeyRound`, S2 with producer fixed and Issued to prefilled), "Edit scopes" (`Pencil`, S1b), separator, "Remove scopes" (`variant="destructive"`, row-owned `AlertDialog` driven by a `deleteTarget`): "Remove {person}'s scopes on {agent}?" / "Their keys keep working but carry no scopes. Revoke keys from their access details." / button "Remove scopes".
- **Interaction model:** auto-save (the two `Switch`es persist on change with a toast); row menu items are entity actions that open their own dialogs, not a card form — same split as `UserSettings/Channels/` (§2 Interaction model).
- **States:** loading — `PreviewList` skeleton rows `h-[48px]`; error — `QueryErrorAlert` with Retry (separate from empty); empty — "Nobody has API access yet." (header button is the Create action); pending — switches disabled while their mutation runs; row spinner in the `⋯` slot labelled "Removing scopes…" (`variables?.grantId === row.grant_id`).
- **Facts & values:** timestamps → `formatRelativeTimestamp` (RowInfo strings); no links on rows.
- **Components:** `PreviewList`, `ListRow`, `RowFlag`, `RowInfo`, `RowActionsMenu`, `Switch`, `QueryErrorAlert`, `AlertDialog`; Show all → new `AllApiAccessSheet.tsx` (no search/pagination, no own lifecycle → Sheet, P5). Header icon `KeyRound`. Data: `["agentApiAccess", agentId]` (`GET .../agent-api/access`).
- **Anti-patterns:** `AgentApiExternalKeysCard` / `AgentApiAccessScopesCard` are A10 + A11 instances — both files are **deleted** here, closing those A10/A11 entries. In `AgentRestApiCard.tsx` (touched to remove them): the description's "external key" sentence becomes "code outside the platform calls in with a key from API access"; the unbounded bordered Connections list (A4/A10 shape) and the raw-palette `STATE_BADGE` (R14a) are **deferred** — the plan does not change connections' data and the fix is its own surface; log them as fix-on-touch.
- **Screenshots:** `/agent/<id>#integrations` at 1440 and 1024 (new card in a grid, §9); capture 2 = S1a open after clicking the first person row (`--element "[role=dialog]"`). If the dev DB has no issued keys, review the empty state and report S1a "required but not capturable".

### S1a — Person access dialog (`Agents/ApiAccessPersonDialog.tsx`)
- **Intent:** see one person's keys on this agent and revoke one.
- **Layout:** `DialogContent sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0`, `onOpenAutoFocus` preventDefault + focus body (the first focusable is a tooltipped Revoke). Title = person's name, `DialogDescription` = "{email} · API access to {agent}". Blocks: (1) fact list — Scopes ("3 scopes" + the names as `secondary` badges, or "No scopes"), Read-only/Read-write is not shown here (per key, in RowInfo); (2) Keys — `ListRowGroup` in `max-h-[40vh] overflow-y-auto pr-2` (§2 Scrolling lists), non-terminal keys only; then `Button variant="link"` "Show {k} ended keys" expanding the terminal rows in place below (read-only detail, §2 Card height exception; D27).
- **Key row (connected-machines shape, P5):** dot per the status table (issuer column), tooltip = status label; title = key label || "API key" (`||`); no `meta`; flags: `RowFlag Lock` when read-only; `RowInfo`: "Prefix {prefix}…", "Last used {rel}" / "Never used", "Expires {rel}" / "Never expires", "Linked to {n} agents", "Issued {rel}" (`created_at`), and for ended keys "Revoked {rel}" when `revoked_at`. Inline: hover-revealed `Ban` ghost icon "Revoke key" on non-terminal rows only; ended rows have no action (D27: no purge).
- **Revoke confirm (`AlertDialog`, the one allowed stack):** "Revoke {label} for {person}?" / "Calls with this key stop at once. It stays in {person}'s credentials, marked Revoked by issuer, until they delete it." / "Revoke key". Pending: spinner in the row's action slot labelled "Revoking…", `variables?.keyId === row.id`. Success toast "Key revoked"; the row moves under ended keys.
- **Footer:** "Give another key" — **chained** (closes this dialog, then opens S2 prefilled), never nested (§2 Disclosure depth).
- **States:** data comes from the S1 query (no own fetch): no loading branch; if the person disappears after a refetch, the dialog closes.

### S1b — Scopes dialog
`GrantDialog` from `AgentApiAccessScopesCard` moves to `Agents/ApiAccessScopesDialog.tsx` unchanged in shape (`UserAllowlistPicker` + `AgentApiScopeEditor`, `sm:max-w-md`). Edit title "Edit scopes", description "Scopes for {person}. They apply to every key and connection {person} uses on this agent, from the next call."; grant title "Grant scopes", button "Grant scopes". Invalidates `["agentApiAccess", agentId]`.

### S2 — Give API access dialog (`Credentials/AgentApiKeyDialog.tsx`, reworked)
- **Intent:** issue a key to myself or to a colleague in as few steps as possible.
- **Layout:** `DialogContent sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0`. Title **"Give API access"** (D34); description "Issue a key so code outside the platform can call {producer || "an agent"}'s REST API."
- **Fields:** (1) **Producer** — only on the `/credentials` path: `Select`-shaped trigger showing the chosen `AgentBadge` or placeholder "Pick an agent that exposes a REST API", opening a `Popover` with `AgentSelectorList mode="single"` (reference `Catalog/AddSkillToAgentDialog.tsx`); candidates = own agents with `agent_api_enabled`; when the chosen one has external access off, one line under the field: "External access is off for {name}. Turn it on in its API access card first." with "API access card" a plain link to `/agent/{id}#integrations`, and submit disabled. Empty candidates: "None of your agents expose a REST API yet." (2) **Issued to** — `UserAllowlistPicker includeSelf`, preselected to me. When the recipient ≠ me, a hint under it: "The key goes to {name}'s credentials. You won't see its value, and you can revoke it anytime." (3) **Scopes (optional)** — `AgentApiScopeEditor`; hint "Shared by every key {name} holds on this agent. Leave empty to keep what they have." (4) one **Advanced** `Collapsible` (closed by default): **Expires** (`Select`, default Never — unchanged options), **Name** (placeholder "{producer} API key"), **Slot** (`service_uri`; placeholder = D23 slug of the producer name, value sent `null` when empty; hint "Code finds the key by this name: `agent_api_session(\"{slot}\")`. The recipient can change it.").
- **Actions:** Cancel / `LoadingButton` "Give access". Escape and outside-click do nothing while pending (§6).
- **Result:** recipient = me → today's handoff unchanged (`stashMintedAgentApiKey` + navigate `?new=1`, toast "{AGENT_API_KEY_LABEL} created"). Recipient ≠ me → the body swaps to a P6 success panel: `CheckCircle2` + "{name} has a new key", "It is in their credentials as {label}. You won't see its value. Revoke it from this agent's API access card." Footer "Done". No navigation, no toast.
- **States:** catalog/agents loading → field skeleton; mutation error → toast, form stays.
- **Anti-patterns:** sibling `AgentSelectorDialog` (R22) **fixed** here; "Acts as" wording removed; raw `text-amber-600` warning line (R14a) **fixed** → `text-muted-foreground` sentence with `AlertTriangle` in `text-warning` (glyph only, not text).

### S3 — Recipient key detail (`Credentials/AgentApiKeyView.tsx`, reworked)
- **Intent:** the holder wants the value and the call recipe, to know whether the key still works, and to rotate or delete it.
- **Layout:** unchanged page shape — a full-width top card, then `grid gap-6 lg:grid-cols-2 items-start` with Details | Access. The route header keeps its Delete button.
- **Top card:** `KeyRound` + `{AGENT_API_KEY_LABEL}` + `AgentApiKeyStatusBadge` (≤ 1 badge; the read-only badge moves to the Access facts). Description "Lets code outside the platform call {producer}'s REST API as you." Blocks: (1) **one** state `Alert`, precedence terminal > paused > issuer-saw: terminal → `variant="destructive"`, title "This key no longer works", body = status sentence, button "Delete key" (opens the route's `DeleteCredential`); paused → default `Alert` + `PauseCircle`, title = status label, body = sentence; `issuer_saw_value` (active/paused only) → default `Alert` + `Eye`, "The issuer has seen this value" / "Rotate it so only you hold it." + button "Rotate key". (2) Key value row (masked prefix, Reveal/Hide, Copy) — unchanged mechanics. (3) Base URL (`CopyableValue`-style row) + "View spec" (opens the holder spec via `GET /credentials/{id}/agent-api-key/spec`, not the owner-gated producer spec). (4) Try-it curl. (5) `Button variant="outline"` "Rotate key" (`RefreshCw`); below it, while `previous_valid_until` is in the future: "The previous value keeps working until {RelativeTime previous_valid_until}." **Terminal:** blocks 2–5 are not rendered; the card is header + Alert.
- **Rotate confirm (`AlertDialog`):** "Rotate this key?" / "You get a new value now. The current value keeps working for 5 minutes so running code can switch over, then stops." / `LoadingButton` "Rotate key" ("Rotating…"). On success the response's token is shown revealed in block 2 (no reveal call, no extra audit), toast "Key rotated".
- **Details card (left):** `FileText` + "Details", description "Naming is for you; the slot is how code finds the key." Fields Name, **Slot** (`ServiceUriField`, editable), Notes; Save/Reset only when dirty (form model).
- **Access card (right):** `ShieldCheck` + "Access", description "Set by the issuer; read-only here." One fact list (labels nouns, values with units): Producer (`AgentBadge` linked; producer_removed → "Deleted"), Issued by (name + email; issuer_removed → "Account removed"), Scopes ("{n} scopes" + names, or "No scopes"), Access ("Read-only" / "Read and write"), Expires (`RelativeTime` or "Never"), Last used (`RelativeTime` or "Never"), Rotated (`RelativeTime` or "Never"), Linked agents ("{n} agents" + up to 5 `AgentBadge`s, "+{k} more"). Under the list, when n > 0, the D14 note in `text-xs text-muted-foreground`: "Anyone who can run these agents can call {producer} as you."
- **States:** loading `PendingItems` → `Skeleton` blocks shaped like the cards; error → `QueryErrorAlert` with Retry (today's Alert has no Retry); pending per button (LoadingButton).
- **Facts & values:** every timestamp → `RelativeTime` — **fixes** the A11 instance (`new Date(apiKey.expires_at)` + `formatDistanceToNow`) in this file; links: AgentBadge only, no glyphs; Base URL is a value not otherwise shown → boxed copy is right (§2 Copying a visible value).
- **Anti-patterns:** A9 on the Details/Access headers (`text-base`, no icon) **fixed**; raw amber "Blocked" badge and amber banner (R14a) **removed** (status badge + Alert replace them).

### S4 — Key status badge on credential lists
- `CredentialCard.tsx`: `AgentApiKeyStatusBadge` right after the type badge, only when `agent_api_key_status` is set and ≠ `active`; `Tooltip` = status sentence. `AgentCredentialsTab.tsx`: same badge in the name cell after "Setup needed". Same component, same tones in both (§2 same entity).
- **Deferred in `AgentCredentialsTab.tsx`:** `Table` in a card, `ExternalLink` glyph on the name link (A12), raw amber/blue badges (R14a) — the phase adds one badge to one cell; rebuilding the table on `ListRow` is its own surface. Log as fix-on-touch.

### S5 — Connection Sharing steering
- In `AgentApiCredentialDetail`'s connection branch, `CredentialSharing` receives an optional `hint` node rendered as the first line of its `CardContent` (no prop for other types). Shown only when the viewer owns the producer: "To let a person call {producer} from their own code, give them a key." + plain link "Open API access" → `/agent/{producerId}#integrations` (`hover:underline`, no glyph, same tab).

### Frontend phase order
All P4, after P2+P3 client regeneration. S4 needs `CredentialPublic.agent_api_key_status`; S1/S1a/S1b need `GET .../agent-api/access`, `POST .../keys/{id}/revoke`, grant routes; S2 needs `POST .../keys` with `issued_to_user_id` + `service_uri`; S3 needs `GET/rotate/reveal/spec` under `/credentials/{id}/agent-api-key`; S5 needs the gap below. Build order: copy module + status badge → S3 → S2 → S1/S1a/S1b → S4 → S5.

### Open questions / contract gaps
1. **S5 gap:** `AgentApiConnectionInfo` has no viewer-owns-producer fact. Add `producer_owned_by_viewer: bool` (or `producer_owner_id`). Without it the hint is not shown — do not infer from a 404 on `GET /agents/{id}`.
2. S2's Slot placeholder needs the D23 slug in the client; a display-only mirror of `default_service_uri` in `agentApiKeyCopy.ts` is acceptable (a mismatch is cosmetic because `null` is sent). If the planner prefers zero duplication, expose the default on the scope-catalog or agent response.
