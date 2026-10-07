# Agent API Issued Keys: account CLI support (`cinna agent-api access`)

Status: A1 built (2026-10-07). Follow-up of `docs/plans/agent_api_issued_keys_plan.md` §10.
Repos: cinna-core (this repo, Part A) and cinna-cli `/Users/evgenyl/dev/ml-llm/cinna-cli` (Part B).
Feature docs: `docs/agents/agent_api/agent_api.md` (Issued Keys), `docs/application/cinna_cli_integration/account_cli_workspace.md` (§4c, §5, §6), cinna-cli `docs/features/agent_api/agent_api.md`.

## §0 Index

### Working-tree rules
- Core: shared tree on `main`, the issued-keys feature is still uncommitted. This work lands in the **same** core commit as the issued-keys feature unless that one is committed first; never revert or reshape its files beyond what §2 lists.
- CLI: one feature commit on `main` in the CLI repo ("agent-api: ..." prose message, house style), **no release** (a release is a separate `release vX.Y.Z` commit bumping `pyproject.toml` + `uv.lock`; not part of this plan). The CLI has no CHANGELOG file.
- Part B builds against §1 only. If §1 must change, edit §1 and note it under "Contract changes" before touching code in either repo.
- Backend tests are API-only (`backend/tests/README.md`), except the one listed unit file.

### Decisions (R = recommendation adopted; rejected alternatives one line each)
| # | Decision |
|---|---|
| C1 | Four CLI verbs: `access list`, `access issue`, `access revoke`, `access scopes`. Scopes ship (R): `AgentApiGrantService.upsert_grant` / `delete_grant` already exist, and without a scopes verb the only CLI way to change a person's scopes is revoke + reissue, which breaks a working key. |
| C2 | **No key value ever crosses the account CLI**, self-issue included. CLI issue response model inherits `AgentApiKeyPublic` (no `token` field exists on it); the service drops `AgentApiKeyCreated.token` in-process. Self-issue returns `credential_url` (web page where the user reveals it). The CLI additionally drops any `token` key defensively before printing or `--json`. |
| C3 | `rotate` stays **web-only** (R: no CLI verb). Rotation exists to replace a value someone outside the platform holds; after a CLI rotate that holder still needs the new value, which the CLI cannot give, so the flow is always half done. Worse, a coding agent can trigger it unprompted and start a 5-minute countdown that breaks every external user of the old value, invisibly to it. Platform-delivered copies (cloud env, Desktop) pick up rotations on their own. Reveal stays web-only (C2). Rejected: "rotate is value-free, so allow it". |
| C4 | Recipient is resolved **server-side** from an email (`issued_to_email`, null = caller): exact match, case-insensitive, active users only; 2+ matches (legacy case duplicates) ⇒ 409 `ambiguous_recipient`. CLI `--to me` sends null. Existence leak accepted (same as `GET /users/search`). |
| C5 | Gating mirrors siblings: producer owner or superuser (404 no-leak, from the reused services); writes (`issue`, `revoke`, `scopes`) also `_require_developer_account` (`cli.py:865`); `list` open to any account token. |
| C6 | Audit unchanged: the reused services already write CREATED / REVOKED / grant events. No new `CLI_ACCOUNT_*` event type (R). Rejected: a CLI-tagged duplicate of every key event. |
| C7 | Coded errors: the new routes answer `{"detail": {"code", "message"}}` (CLI's `CodedRefusal` shape). `AgentApiError.__init__` gains `code: str | None = None`; set at the four raise sites in §1.4. Web routes keep string `detail` (`_handle_agent_api_error` unchanged). |
| C8 | Close the escape-hatch leak: `cinna api POST agents/{id}/agent-api/keys` today returns a self-issued `token` through `/account/api-proxy`. Deny `agents/*/agent-api/keys` (and children) in `account_api_proxy_policy.py`; the dedicated verbs replace it. |
| C9 | `GET /cli/account/credentials` fills `agent_api_key_status` (today `AccountCLIService._credential_public` leaves it null). One status helper, shared with `routes/credentials.py`. |
| C10 | Account CLI refuses to **draft** an `agent_api` credential (400): an empty `agent_api` row has no token binding and is never usable; the message names `cinna agent-api access issue` and `cinna connect agent-api`. `cinna agent import` skips `agent_api` manifest credentials and prints that hint instead of drafting (R). |
| C11 | CLI `SUPPORTED_CONTRACT_VERSION` 1.6.0 → **1.7.0** (R). The check is same-major, so nothing passes or fails differently; the bump records that this CLI now speaks 1.7.0's vocabulary (`key_revoked|key_expired|key_paused`, issued-key slots for local agents), as the 1.6.0 bump did for delegation. Offline fallbacks unchanged (1.7.0 `layout.json` diff is the version string only). |
| C12 | External access opt-in (`agent_api_external_access_enabled`) and the identity switch stay web-only; refusal `external_access_disabled` carries the agent's Integrations URL. Rejected: a CLI toggle (it is the deliberate human act that exposes the API outside the platform). |
| C13 | Terminal keys (revoked, producer/issuer removed, expired) hidden in `access list` table unless `--all` (mirrors D27's disclosure); `--json` is always the full payload. |
| C14 | Core skill readiness: `SkillSlotIndex._usable` (`services/skills/skill_credential_requirements.py`) treats a linked issued `agent_api` key whose key status is anything but `active` (revoked, expired, producer/issuer removed, api_disabled, external_access_paused) as not usable, so its slot reads `access_revoked`. Key states load in one batched `key_states_by_credential` call in `build_for_agent`; connections (no external key) are unaffected. |
| C15 | Deny sweep done (§2a): `EXCLUDED_PATTERNS` (any method: issued keys, raw workspace file download/view, dashboard env-file), `SHARE_PATTERNS` (any method: guest and webapp shares, token-keyed guest/webapp share auth; C18) and `EXCLUDED_WRITE_PATTERNS` (POST: A2A access token, MCP and ACP connector tokens, agent webhook create/regenerate, task webhook trigger create/regenerate) refuse with `excluded_path`. The policy now matches on the percent-decoded path (an encoded `%63redentials` reached the router past the prefix denylist) and refuses as `malformed_path` a decoded path with `..`, a control character or space (a trailing `%0A` is routed by Starlette but missed by `fullmatch`), a leftover `%` (double encoding) or `//`. Prefix-only listings stay reachable. |
| C16 | No CLI-tagged audit (C6 confirmed): the reused services' CREATED / REVOKED / grant events are the record. |
| C17 | Case-only duplicate emails answer 409 `ambiguous_recipient` (C4 confirmed); no auto-pick. |
| C18 | Share URL/security-code routes denied through the hatch (team-lead decision): `agents/*/guest-shares…` and `agents/*/webapp-shares…` for any method (create, list, read, update, delete all carry `share_url` with the share token and `security_code`), plus `guest-share/*/auth` and `webapp-share/*/auth` (answer a guest session JWT). Message: share links are managed in the web app. |
| C19 | `context/api_reference/` is generated by core: `platform_knowledge_assets.generate_api_reference` (run by `make sync-platform-knowledge` from `frontend/openapi.json` into the platform-knowledge-env snapshot, served by `ContextPackageService`). Each route the hatch refuses is now marked in place, driven by the policy's settings-free `route_refusal` (+ `dedicated_verb`): "> Not available via `cinna api` (excluded_path). Use `cinna agent-api access`." for issued keys, the reason alone elsewhere; the README index explains the marker. Snapshot regenerated. |

### Phase table
| Phase | Files touched | Sections | Tests (run) |
|---|---|---|---|
| A1 Core service + routes | `models/cli/account_convenience.py`, `services/cli/account_cli_service.py`, `api/routes/cli.py`, `services/agent_api/agent_api_service.py` (`AgentApiError.code`), `agent_api_key_service.py` (codes), `agent_api_key_status.py` (value helper), `api/routes/credentials.py` (delegate), `services/users/user_service.py`, `services/cli/account_api_proxy_policy.py` | §1, §2 | §5 core list |
| A2 Core tests, client, docs | new `tests/api/cli/test_account_cli_agent_api_access.py`, `tests/utils/agent_api_keys.py`, `tests/unit/test_api_proxy_policy.py`, regenerated `frontend/src/client/`, docs §6 | §5, §6 | `tests/api/cli/`, `tests/api/agents/agent_api/`, `tests/api/credentials/`, unit; `make check-docs` |
| B1 CLI client + verbs | `src/cinna/client.py`, `src/cinna/main.py`, `src/cinna/account.py` | §1, §3 | §5 CLI list |
| B2 CLI listings, import, kit, templates, docs | `account.py` (credential cells, slot readiness), `local_import.py`, `kit_contract.py`, `templates/ACCOUNT_CLAUDE.md.template`, `README.md`, `docs/features/agent_api/*`, `docs/features/account_workspace/*` | §4, §6 | `uv run pytest -q`; `python3 scripts/check_docs_references.py` |

A1 → A2. B1 can start as soon as §1 is read (mocks only). B2 after B1. Live acceptance (§5 last block) after A2 + B2.

---

## §1 Frozen CLI ⇄ core contract

### Contract changes after freeze
- 1.4: `scopes` is required but nullable. Omitting it is a 422; send `"scopes": null` explicitly to remove a grant (an omitted field can no longer silently delete a grant).
- 1.6: the api-proxy also refuses with 403 `excluded_path`: any method on `environments/*/workspace/download|view-file` and `dashboards/*/blocks/*/env-file`; POST on A2A access-tokens, MCP/ACP connector tokens, agent webhook create/regenerate-token, and task trigger webhook create/regenerate-token (see §2a). Paths are matched percent-decoded.

All routes under `/api/v1/cli`, auth `Authorization: Bearer <account CLI token>` (`AccountCLIContextDep`). `agent_id` travels in query/body like the existing `/account/agent-api/*` siblings, never in the path. No response ever contains a key value or a field named `token`.

### 1.1 `GET /cli/account/agent-api/access?agent_id=<uuid>` → `AccountAgentApiAccess`
`AgentApiAccessList` (unchanged shape: `data: [AgentApiAccessEntry]`, `count`, `external_access_enabled`, `identity_enabled`) plus:
- `scope_catalog: [{name, description|null}]` (from `AgentApiGrantService.get_scope_catalog`; empty when none declared)
- `manage_url: str` = `{FRONTEND_HOST}/agent/{agent_id}#integrations`

`AgentApiAccessEntry` = `{user: {id, email|null, full_name|null}, grant_id|null, scopes: [str], keys: [AgentApiKeyPublic]}`; `AgentApiKeyPublic` = `{id, credential_id|null, agent_id|null, label|null, token_prefix, issued_to, read_only, is_active, status, is_usable, expires_at|null, last_used_at|null, revoked_at|null, rotated_at|null, linked_agent_count, created_at}`. Terminal keys included. `status` ∈ `active | revoked_by_issuer | producer_removed | issuer_removed | expired | api_disabled | external_access_paused` (unknown future values: treat as unavailable).

### 1.2 `POST /cli/account/agent-api/keys` → `AccountAgentApiKeyIssued`
Body `AccountAgentApiKeyIssueBody`:
```
{ agent_id: uuid, issued_to_email: str|null (null = caller), scopes: [str]|null (null = leave grant as is; [] = explicit empty grant),
  expires_in_days: int|null (1..3650), service_uri: str|null (null = "<slug(producer)>-api"), label: str|null, read_only: bool = false }
```
Response: every `AgentApiKeyPublic` field plus `base_url`, `spec_url`, `service_uri: str|null` (the credential's slot), `issued_to_self: bool`, `credential_url: str|null` (`{FRONTEND_HOST}/credential/{credential_id}`, self-issue only), `unknown_scopes: [str]` (requested scopes not in a non-empty catalog; never a refusal).

### 1.3 `POST /cli/account/agent-api/keys/revoke` → `AgentApiKeyPublic`
Body `{agent_id: uuid, key_id: uuid}`. Idempotent: an already revoked key answers 200 with `status: "revoked_by_issuer"`. Grant untouched.

### 1.4 `PUT /cli/account/agent-api/access/scopes` → `AccountAgentApiScopesResult`
Body `{agent_id: uuid, user_email: str|null (null = caller), scopes: [str]|null}`. `scopes` list ⇒ upsert the `(producer, user)` grant to exactly that list; `null` ⇒ remove the grant (idempotent when none). Response `{entry: AgentApiAccessEntry|null, unknown_scopes: [str]}` (`entry` null when the person has neither grant nor keys afterwards).

### 1.5 `GET /cli/account/credentials` (changed) and `PUT /cli/account/credentials/{id}`
Each `CredentialPublic` row now carries `agent_api_key_status` (same vocabulary as 1.1) for issued keys, `null` for connections and non-`agent_api` rows.
`POST /cli/account/credentials` with `type: "agent_api"` ⇒ **400** plain detail: "Agent API credentials are not drafted. Give yourself a key with `cinna agent-api access issue <producer> --to me`, or wire a cloud agent with `cinna connect agent-api`."

### 1.6 `POST /cli/account/api-proxy` (changed)
Any method on `agents/{id}/agent-api/keys` or below ⇒ 403 `excluded_path` (existing hatch refusal shape; CLI exit 2).

### 1.7 Errors (new routes)
| HTTP | `detail.code` | When |
|---|---|---|
| 401 | (string) | account token invalid (existing) |
| 403 | (string) | not a developer, writes only (existing `_require_developer_account`) |
| 404 | `agent_not_found` | producer missing or not owned (no leak) |
| 404 | `recipient_not_found` | no active user with that email |
| 409 | `ambiguous_recipient` | email matches several users case-insensitively |
| 400 | `external_access_disabled` | producer's external access is off; message ends with `manage_url` |
| 400 | `api_disabled` | producer REST API off |
| 404 | `key_not_found` | key id not an external key of this producer |
| 400/404/409 | `invalid_request` / `not_found` / `conflict` | any other `AgentApiError`, by status |
| 422 | (FastAPI) | body validation (`expires_in_days` range etc.) |

---

## §2 Part A — core (A1)

**Error codes.** `agent_api_service.py:118` `AgentApiError.__init__(message, status_code=400, code=None)` stores `self.code`; `AgentApiNotFoundError` (`:127`) defaults `code="agent_not_found"`. `agent_api_key_service.py`: `create_key` opt-in raises (`:155-163`) get `external_access_disabled` / `api_disabled`, recipient 404 (`:168`) `recipient_not_found`; `_load_owned_key` (`:844`) `key_not_found`. No message text changes.

**Status helper (C9).** `agent_api_key_status.py`: add `key_status_value(session, credential, key_states=None) -> str | None` (body of today's `routes/credentials.py:122` `_agent_api_key_status`); that route helper becomes a one-line delegate (route-level logic leaves the route).

**Recipient lookup (C4).** `user_service.py` near `get_user_by_email` (`:345`): `find_active_users_by_email_ci(session, email) -> list[User]` (`func.lower(User.email) == email.strip().lower()`, `is_active`).

**Models.** `models/cli/account_convenience.py` (beside `AccountAgentApiCallBody` `:91`): `AccountAgentApiKeyIssueBody`, `AccountAgentApiKeyIssued(AgentApiKeyPublic)`, `AccountAgentApiKeyRevokeBody`, `AccountAgentApiAccess(AgentApiAccessList)`, `AccountAgentApiScopesBody`, `AccountAgentApiScopesResult` exactly per §1. Re-export where the sibling bodies are exported.

**Service** — new section in `account_cli_service.py` after `call_agent_api` (`:936`), thin wrappers only:
- `_resolve_recipient(db, user, email) -> User`: None/blank ⇒ `user`; else lookup; 0 ⇒ `AgentApiError(404, recipient_not_found)`; >1 ⇒ `AgentApiError(409, ambiguous_recipient)`.
- `list_agent_api_access(db, user, agent_id)`: `AgentApiKeyService.list_access(..., is_superuser=user.is_superuser)` + `AgentApiGrantService.get_scope_catalog` + `manage_url` (`settings.FRONTEND_HOST.rstrip("/")`, as `:1829`).
- `issue_agent_api_key(db, user, body)`: resolve recipient → `AgentApiKeyCreate(issued_to_user_id, scopes, expires_in_days, service_uri, label, read_only_override)` → `AgentApiKeyService.create_key` → build `AccountAgentApiKeyIssued` from `created.model_dump(exclude={"token"})`, `service_uri` from `db.get(Credential, created.credential_id)`, `credential_url` only when recipient is caller, `unknown_scopes` via catalog. **Invariant:** the `AgentApiKeyCreated` object never leaves this method; nothing logs it.
- `revoke_agent_api_key(db, user, agent_id, key_id)`: `AgentApiKeyService.revoke_key`.
- `set_agent_api_scopes(db, user, body)`: resolve; list ⇒ `AgentApiGrantService.upsert_grant`; `None` ⇒ find `grant_id` in `list_access` entry, `delete_grant` if present; return the person's entry from a fresh `list_access` + `unknown_scopes`.
- `list_credentials` (`:1719`): one `key_states_by_credential` call for the page's `agent_api` ids; `_credential_public(db, c, key_states=None)` (`:1668`) sets `agent_api_key_status=key_status_value(...)` (also covers `update_credential_metadata`).
- `create_credential_draft` (`:1779`): `agent_api` ⇒ 400 per §1.5, raised before any write.

**Routes** — `api/routes/cli.py` after `account_agent_api_call` (`:1063`): the four §1 routes with `response_model`s; helper `_raise_agent_api_coded(e)` → `HTTPException(e.status_code, {"code": e.code or _FALLBACK[e.status_code], "message": e.message})`. Writes call `_require_developer_account` first. Route for drafts: map the new 400 in `account_create_credential` (`:1550`).

**Escape hatch (C8).** `account_api_proxy_policy.py`: add `EXCLUDED_PATTERNS: tuple[str, ...] = (r"agents/[^/]+/agent-api/keys(?:/.*)?",)` matched (fullmatch, after stripping `API_V1_STR/`) in `assert_api_proxy_allowed` right after the prefix denylist, raising `ApiProxyDenied("excluded_path", "Issued API keys are managed with 'cinna agent-api access'; the escape hatch never carries key values.")`. Module docstring: one line on pattern entries.

**Client.** `source ./backend/.venv/bin/activate && make gen-client` (new `cli`-tagged routes enter OpenAPI; the web app does not call them).

---

## §2a Deny sweep findings

Every `/api/v1` route reachable through `POST /cli/account/api-proxy` (not under an `EXCLUDED_PREFIXES` entry) whose 2xx response carries a secret. The inner call is a plain user JWT (no `client_kind`, no env audience). Patterns are full-match on the decoded API-relative path.

| Path | Method | Returns | Decision |
|---|---|---|---|
| `agents/{id}/agent-api/keys` and below | any | self-issued key value (issue); rotate starts a countdown on a value the CLI cannot deliver | **Deny** (C8), message names `cinna agent-api access` |
| `environments/{env}/workspace/download/{path}`, `.../workspace/view-file/{path}` | GET | raw workspace files; `credentials/credentials.json` holds every linked credential's decrypted value, a folder download zips it | **Deny** |
| `dashboards/{d}/blocks/{b}/env-file` | GET | any workspace file (path in query, invisible to the policy) | **Deny**; `env-files` (names only) allowed |
| `agents/{id}/access-tokens` | POST | A2A access token value | **Deny** POST; GET/PUT/DELETE return `token_prefix` only |
| `agents/{id}/mcp-connectors/{c}/tokens` | POST | MCP direct token value | **Deny** POST; list/update return `prefix` |
| `agents/{id}/acp-connectors/{c}/tokens` | POST | ACP token value | **Deny** POST; list/revoke return `prefix` |
| `agents/{id}/webhooks/{git-source,script,session}`, `.../webhooks/{pk}/regenerate-token` | POST | `webhook_token` | **Deny**; reads return `webhook_token_prefix` |
| `tasks/{t}/triggers/webhook`, `.../triggers/{tr}/regenerate-token` | POST | `webhook_token` | **Deny**; schedule/exact-date triggers allowed |
| `agents/{id}/guest-shares` and below, `agents/{id}/webapp-shares` and below | any | `share_url` carrying the share token, `security_code` (create also `token`) | **Deny** (C18, team-lead decision) |
| `guest-share/{token}/auth`, `webapp-share/{token}/auth` | POST | guest session JWT | **Deny** (C18) |
| `guest-share/{token}/activate`, `.../info`, `webapp-share/{token}/info` | POST/GET | share id, agent name, code-required flag | Safe: no token or code returned |
| `agent/credentials/{cred}/access-token` | POST | OAuth access token | Safe: agent-env auth (audience `agent_env`) rejects the inner user JWT |
| `external/account-config` | GET | decrypted AI provider `api_key` | Safe: requires `client_kind` desktop/mobile; inner JWT has none ⇒ 403 |
| `invitations/accept` | POST | login JWT for the invitee | Safe: public route keyed on an invite token the caller must already hold; the hatch adds no power |
| `shared/workspace/{env}/view/{path}`, `webapp/{token}/...` | GET/POST | workspace file / webapp | Safe: authorised by a token in the URL, not the user; the hatch adds no power |
| `ssh-keys/*` | any | `public_key`, `fingerprint` only | Safe: no private key in any response |
| `agents/{id}/agent-api/connect`, `.../agent-api/connections`, `.../agent-api/access` | POST/GET | `token_id`, `token_prefix` | Safe: prefix only |
| `mail-servers/*` | any | `has_password` flag | Safe |
| `knowledge-sources/{id}/export` | GET | article markdown (superuser) | Safe: no secrets |
| credentials, ai-credentials, oauth-credentials, users (except `me`/`search`), admin (server channels), app-sync (pairing reveal), cli, auth/login | any | various | Already denied by prefix |

## §3 Part B — CLI verbs (B1)

**Client** (`src/cinna/client.py`, `AccountClient`, beside agent-api methods `:482-535`):
- `_handle_response(response, coded=False)` (`:273`): when `coded`, raise `_coded_refusal(response)` (`:27`) if it parses, else today's path.
- `list_agent_api_access(agent_id)`, `issue_agent_api_key(agent_id, *, issued_to_email, scopes, expires_in_days, service_uri, label, read_only)`, `revoke_agent_api_key(agent_id, key_id)`, `set_agent_api_scopes(agent_id, user_email, scopes)` — all `coded=True`, bodies per §1, `None` fields omitted. `issue_agent_api_key` pops `token` from the dict before returning (C2 belt and braces).
- Old core: a 404 whose detail is exactly `"Not Found"` on these four paths ⇒ `PlatformError` message "This platform predates API access from the CLI (needs cinna-core with issued keys)."

**Commands** (`src/cinna/main.py`, under the `agent-api` group `:1467`): sub-group `access` ("Give people API access to a producer: keys and scopes. Never prints a key value."):
```
cinna agent-api access list <producer> [--all] [--json]
cinna agent-api access issue <producer> --to <email|me> [--scopes a,b] [--expires-in-days N] [--service-uri SLOT] [--label TEXT] [--read-only] [--json]
cinna agent-api access revoke <producer> <key-ref> [--yes] [--json]
cinna agent-api access scopes <producer> --to <email|me> (--set a,b | --clear) [--json]
```
`--to` required on issue (deliberate recipient). `--expires-in-days` `click.IntRange(1, 3650)`. `--scopes ""` = explicit empty grant. `--set`/`--clear` mutually exclusive, one required.

**Runners** (`src/cinna/account.py`, after `run_agent_api_call`): `run_agent_api_access_list|issue|revoke|scopes`. Producer via `_resolve_account_agent(client.list_account_agents()["data"], ref)` (`:346`), as siblings.
- `list`: table "API access — <producer>" with columns Person · Scopes · Key (prefix) · Status · Expires · Last used · Agents (linked count) · ID (fold, no ellipsis). Grant-only person ⇒ one row, Key `—`. Terminal keys hidden unless `--all`; footer "N ended keys hidden (--all)". Footer lines: `External access: on/off` (off ⇒ warn + `manage_url`), available scopes from `scope_catalog`. `--json` ⇒ raw payload.
- `issue`: self ⇒ "Key `<prefix>` is in your credentials as slot `<service_uri>`. Its value is never shown here; reveal it on the web: `<credential_url>`. A local agent (Cinna Desktop) calls it with `cinna_credentials.agent_api_session("<slot>")`. To wire a cloud agent use `cinna connect agent-api` instead." Other ⇒ "Issued key `<prefix>` to <email>. It lands in their credentials; you never see its value. Revoke anytime: `cinna agent-api access revoke <producer> <prefix>`." `unknown_scopes` ⇒ warn. Coded `external_access_disabled` ⇒ print message + "turn it on in the web: <manage_url>".
- `revoke`: `<key-ref>` = key id or exact `token_prefix`, resolved from `list_agent_api_access`; elided refs (`…`/`...`) refused locally (existing rule); no match / several ⇒ `ClickException` listing candidates. Confirm "Revoke key <prefix> issued to <email>? It stops working at once and cannot be restored." `--yes` skips; `--json` needs `--yes` (`_require_yes_for_json`, `:5081`). Already revoked ⇒ "already revoked", exit 0.
- `scopes`: print the person's resulting scopes or "grant removed"; `unknown_scopes` warn.
- Status words (one dict `_KEY_STATUS_LABELS` in `account.py`, same as core §8): Active (green); API turned off by producer / External access paused (yellow); Revoked by issuer / Producer removed / Issuer account removed / Expired (red); unknown ⇒ raw value, dim.

## §4 Part B — listings, import, kit, templates (B2)

- **Credentials list** (`run_credentials_list` `:3639`): for rows with `agent_api_key_status`, the Status cell shows the key label (§3 dict) instead of complete/needs setup. `_credential_summary` (`:3561`, used by `agent show`/skills) appends the label when not `active`. `_ACCOUNT_AUTHORITATIVE_FIELDS` (`:3593`) gains `agent_api_key_status`.
- **Skill slot readiness** (`_slot_readiness` `:4054`): a carrier whose `agent_api_key_status` is set and not `active` is not usable; state = `key_revoked` (revoked_by_issuer/producer_removed/issuer_removed), `key_expired`, `key_paused` (api_disabled/external_access_paused) — same mapping as core `unavailable_reason_for`. `_slot_remedy` (`:4119`): revoked/expired ⇒ "ask the producer's owner for a new key (yours: `cinna agent-api access issue <producer> --to me --service-uri <slot>`), then delete this one"; paused ⇒ "the producer paused its API; it recovers by itself".
- **Import** (`local_import.py` `_sync_credentials` `:1118-1166`): specs with `type == "agent_api"` are not drafted; print "  '<name>' needs an Agent API key: `cinna agent-api access issue <producer> --to me --service-uri <slot>` (local) or `cinna connect agent-api --producer <producer> --consumer <agent>` (cloud)." Matches core C10.
- **Kit** (`kit_contract.py:62`): `SUPPORTED_CONTRACT_VERSION = "1.7.0"`; fallbacks untouched. No vendored helper code (the CLI ships no `cinna_credentials.py`).
- **`templates/ACCOUNT_CLAUDE.md.template`**: Agent REST API table (`:120-127`) gains the four `access` rows; a rule box after it: "API access is a key issued to a person; its value never appears in the CLI — do not ask the user to paste it. Platform agent ⇒ `cinna connect agent-api`. Person or outside tool ⇒ `access issue --to <email>`. Local agent ⇒ `access issue --to me --service-uri <slot matching its manifest>`, attached in Cinna Desktop, called with `cinna_credentials.agent_api_session("<slot>")`. `key_revoked`/`key_expired` are terminal (new key), `key_paused` recovers. Rotate and reveal live on the web credential page." Intent mapping (`:180`): "give X access to the API / an API key for X" ⇒ `cinna agent-api access issue`.
- `templates/CLAUDE.md.template` unchanged: per-agent cloud workspaces consume producers through connections; the env-core guide already covers `credentials.agent_api_session`.

---

## §5 Tests (checklist by file)

**Core — `backend/tests/api/cli/test_account_cli_agent_api_access.py`** (new; account token via `tests/utils/cli.py` `bootstrap_account_token`; producer setup via new `tests/utils/agent_api_keys.py` holding `setup_producer` / `make_developer` moved from `agents_agent_api_external_keys_test.py:139-170`, that file importing them)
- [ ] list: owner sees grouped entries incl. terminal keys, `scope_catalog`, `manage_url`; non-owner 404 `agent_not_found`; superuser allowed
- [ ] issue self: 200, `issued_to_self`, `credential_url` ends `/credential/<id>`, `service_uri` default slug; response JSON has no `token` key and does not contain the value revealed afterwards via web `POST /credentials/{id}/agent-api-key/reveal`
- [ ] issue other by email (mixed case): recipient owns the credential, `credential_url` null; CREATED audit on issuer
- [ ] issue: unknown email 404 `recipient_not_found`; inactive user 404; external access off 400 `external_access_disabled`; API off 400 `api_disabled`; non-developer 403; `expires_in_days=0` 422; `--scopes` unknown ⇒ `unknown_scopes`
- [ ] revoke: 200 `revoked_by_issuer`, idempotent; foreign key id 404 `key_not_found`; recipient's credential kept; non-owner 404
- [ ] scopes: set ⇒ grant scopes exact; `null` removes; removing when none 200; non-owner 404
- [ ] credentials list: issued key row `agent_api_key_status` `active` → `revoked_by_issuer` after revoke; connection row null
- [ ] draft `type=agent_api` 400, no row created
- [ ] api-proxy `POST agents/{id}/agent-api/keys` 403 `excluded_path`, no key created (access list empty); `GET agents/{id}/agent-api/access` via proxy still 200
**Core — `backend/tests/unit/test_api_proxy_policy.py`**: one test for the pattern (keys, keys/{id}/revoke denied; `agent-api/access`, `agent-api/grants` allowed).
Run: `docker compose exec backend python -m pytest tests/api/cli/test_account_cli_agent_api_access.py -v`, then `tests/api/cli/`, `tests/api/agents/agent_api/`, `tests/api/credentials/`, `tests/unit/test_api_proxy_policy.py`.

**CLI — `tests/test_client.py`** (respx): four methods hit §1 paths/bodies; coded 400 ⇒ `CodedRefusal` with code; `token` popped from an issue response that (wrongly) carries one; bare `Not Found` 404 ⇒ "predates" message.
**CLI — `tests/test_agent_api_access.py`** (new; `mock_client_cls` pattern from `tests/test_account.py:209`): list hides terminal / `--all` shows / `--json` raw; issue self prints `credential_url` + slot hint and no value even if mock returns `token`; issue other; `--to` missing ⇒ usage error; `--to me` sends no email; revoke by prefix and by id, ambiguous/unknown ref, confirm declined ⇒ no call, `--json` without `--yes` ⇒ usage error, already revoked; scopes `--set`/`--clear`; `external_access_disabled` prints `manage_url`.
**CLI — `tests/test_account.py`**: credentials list status cell per key status; `_slot_readiness` key states + remedies.
**CLI — `tests/test_local_import.py`**: `agent_api` spec not drafted, hint printed; version message names 1.7.0. **`tests/test_kit_contract.py`**: adjust if it asserts the constant.
Run: `cd /Users/evgenyl/dev/ml-llm/cinna-cli && uv run pytest -q`.

**Live acceptance** (after A2 + B2, add as scenarios 13-17 in cinna-cli `docs/features/agent_api/agent_api_acceptance.md`): enable external access in web; `access issue <p> --to me` ⇒ URL, no value in output; `account credentials list` shows Active; `access issue --to <other>`; `access list`; `access revoke` ⇒ list `--all` shows Revoked by issuer, recipient's `credentials list` shows it; `cinna api POST agents/<p>/agent-api/keys` exits 2.

## §6 Docs

Core:
- `docs/application/cinna_cli_integration/account_cli_workspace.md` §4c: subsection "Giving API access (`cinna agent-api access`)" — verbs, "never a key value", rotate/reveal web-only; verb table (`:316-319`) gains four rows (route, service, gate). §5: listing carries `agent_api_key_status`; `agent_api` drafts refused. §6: escape hatch denies `agents/*/agent-api/keys`.
- `account_cli_workspace_tech.md`: the four routes, models, `AccountCLIService` methods, coded error table (§1.7), `EXCLUDED_PATTERNS`.
- `docs/agents/agent_api/agent_api.md` Integration Points (`:436`): one sentence for the access verbs and the no-value rule. `agent_api_tech.md` Tests (`:89`): new test file; Issued Keys (`:333`): `AgentApiError.code` values.
- `make check-docs`.

CLI:
- `docs/features/agent_api/agent_api.md`: flow "Give API access", business rules (never a value, rotate web-only, terminal hidden, `--to` required, key-ref forms).
- `agent_api_tech.md`: command surface, client methods, §1 routes under External contracts.
- `agent_api_acceptance.md`: scenarios above.
- `docs/features/account_workspace/account_workspace.md:157` and `_tech.md:76,220`: key status in the Status cell.
- `README.md`: new `### cinna agent-api access list | issue | revoke | scopes` section near `:452`; one line at `:177`.
- `python3 scripts/check_docs_references.py`.

## §7 Risks and open questions (recommendations)

1. **Other token-minting routes reachable via `cinna api`**: Resolved: see C14–C17 (C15, §2a).
2. **Skill readiness in core**: Resolved: see C14–C17 (C14).
3. **CLI-tagged audit**: Resolved: see C14–C17 (C16).
4. **Email case duplicates**: Resolved: see C14–C17 (C17).
5. **Older CLI against new core**: unaffected (new routes only, `agent_api_key_status` is an extra field). **New CLI against older core**: "predates" message (§3).
6. **Rotate via CLI** (C3) revisit only if a value-free external delivery path appears (e.g. Desktop-only consumers); then rotate becomes complete without a value.
