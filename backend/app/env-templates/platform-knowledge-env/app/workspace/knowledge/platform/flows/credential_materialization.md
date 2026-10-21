---
feature: flow_credential_materialization
domain: flows
one_liner: "Follows a Credential or AICredential row from creation and sharing to the files, MCP manifest and env vars a running agent container can actually read, and back on every change."
primary_label: flow
---

# Credential Materialization

## Purpose

A secret is useless while it is an encrypted column. This flow follows the path from a
`Credential` or `AICredential` row in Postgres to the artifacts a running agent container can
actually read: `credentials/credentials.json` and its redacted `README.md`, standalone service-account
JSON files, private keys under `~/.ssh/`, the per-mode MCP manifest `mcp/user_mcp.json`, and the
provider API keys baked into the environment's `.env` / SDK config files. Entry points are numerous —
creating or editing a credential, linking it to an agent, installing a bundle, an admin provisioning a
company LLM key, an OAuth token approaching expiry, a user editing their profile details, and every
environment start / restart / rebuild — but they all converge on two backend functions
(`CredentialsService.prepare_credentials_for_environment` and
`CredentialsService.collect_mcp_provider_manifest`) plus one config-generation pass
(`EnvironmentLifecycleManager._update_environment_config`).

The exit condition is that an SDK session or a script running in `/app/workspace` sees the credential
— through `credentials.json`, through an MCP server the SDK connected to, or through a provider key the
engine authenticates with — and that the same artifact is refreshed or removed when its source row
changes. The two halves of the flow have different refresh semantics and that difference is the single
most important thing to know before changing anything here: **integration credentials push live into a
running container; AI credentials only materialize when the environment's config files are
regenerated.**

## Participants

| Feature | Role in this flow | Module |
|---|---|---|
| [Agent Credentials](../agents/agent_credentials/agent_credentials.md) | Owns `Credential`, the whitelist, redaction, and the env-sync entry points | `backend/app/services/credentials/credentials_service.py` |
| [Credential Sharing](../agents/agent_credentials/credential_sharing.md) | Decides whether a non-owner may link a credential at all | `backend/app/services/credentials/credential_share_service.py` |
| [AI Credentials](../application/ai_credentials/ai_credentials.md) | Owns `AICredential`, defaults, and per-user access | `backend/app/services/credentials/ai_credentials_service.py` |
| [Admin AI Credential Provisioning](../application/ai_credentials/admin_ai_credential_provisioning.md) | Reconciles an admin-managed parent record into per-user child `AICredential` rows | `backend/app/services/credentials/managed_ai_credentials_service.py` |
| [Agent REST API](../agents/agent_api/agent_api.md) | Creates `agent_api` connection credentials; mints the synthetic `owner_identity_token` | `backend/app/services/agent_api/agent_api_token_service.py`, `backend/app/services/agent_api/agent_api_identity_service.py` |
| [Agent-to-Agent MCP Connector](../application/mcp_integration/agent_to_agent_mcp_connector.md) | Creates `mcp_provider` credentials that bypass `credentials.json` entirely | `backend/app/services/mcp_providers/mcp_provider_service.py` |
| [Agent Bundles](../agents/agent_bundles/agent_bundles.md) | Creates placeholders and publisher shares at install time | `backend/app/services/bundles/install_service.py` |
| [Agent Environments](../agents/agent_environments/agent_environments.md) | Runs the config-generation and dynamic-data-sync passes | `backend/app/services/environments/environment_lifecycle.py` |
| [Multi-SDK Support](../agents/agent_environment_core/multi_sdk.md) | Turns a resolved provider key into engine-specific config | `backend/app/services/environments/sdk_constants.py`, `backend/app/services/environments/model_catalog.py` |
| [Agent Environment Core](../agents/agent_environment_core/agent_environment_core.md) | Receives the payloads and writes the files inside the container | `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` |

## The flow

### 1. A row comes into existence

Every integration credential is a `Credential` row created through
`CredentialsService.create_credential` (`backend/app/services/credentials/credentials_service.py`),
which encrypts `credential_data` with `encrypt_field`. Four provenances feed it:

- **User-authored.** `POST /api/v1/credentials/` with a type and a data blob.
- **Automatic `agent_api` connection.** "Connect Agent API" mints a proxy token and then calls
  `create_credential` with `type=AGENT_API`, `allow_sharing=False`, and
  `credential_data={base_url, spec_url, token, label, producer_agent_id}`
  (`agent_api_token_service.py`). The `AgentApiToken` row is then pointed at the credential so
  deleting the credential cascade-revokes the token. An **issued key**
  (`agent_api_key_service.py`, `kind="external"`) uses the same type but is *not* a connection:
  its credential is created **owned by the recipient** (the person the producer's owner issued
  it to), with a default `service_uri` of `<producer-slug>-api`.
- **Automatic `mcp_provider`.** Connecting to a producer agent mints a connector-scoped direct token
  and calls `create_credential` with `type=MCP_PROVIDER`, `mcp_auth_mode="agent2agent"`,
  `mcp_consumer_agent_id`, per-mode flags `mcp_mode_conversation` / `mcp_mode_building`, and
  `credential_data={endpoint_url, transport, auth_mode, token, ...}`
  (`backend/app/services/mcp_providers/mcp_provider_service.py`).
- **Bundle install.** `InstallService._setup_install_credentials`
  (`backend/app/services/bundles/install_service.py`) owns the transaction and hands the revision's
  `required_credential_specs` to `CredentialProvisioner.provision(..., BUNDLE_INSTALL_POLICY)`.
  A `provided_by="publisher"` spec validates that the publisher still owns the row and still has
  `allow_sharing=True`, inserts a `CredentialShare` stamped `source="bundle_install"`, and inserts
  the `AgentCredentialLink`. A `provided_by="user"` spec creates an installer-owned placeholder
  instead; `CredentialsService.update_credential` flips `is_placeholder` off once
  `check_credential_completeness` passes.
- **Catalog skill install.** The same `CredentialProvisioner`, under `SKILL_INSTALL_POLICY`, called
  from `SkillCatalogService.install_into_agent` in the **same transaction as the plugin link**. The
  specs come from the skill revision instead of the bundle revision, and the difference is policy:
  a slot the installer already has (matched by `service_uri` through
  `CredentialsService.find_slot_match`) is auto-linked rather than duplicated, a created placeholder
  is stamped with the slot and the agent's workspace, the share is stamped
  `source="skill_install"`, and **no slot ever fails the install**. The route then syncs credentials
  before the plugin sync, because plugin sync does not carry them. Uninstall reverses only what it
  created: `release_skill_slots` unlinks installer-owned placeholders for slots no other catalog
  skill on the agent still declares, except credentials its bundle may still require,
  and deletes one nothing links any more. See
  [Agent Skills](../agents/agent_skills/agent_skills.md).

`CredentialsService.classify_credential_category` is the single source of truth that turns
(ownership, type, `share_source`, `mcp_auth_mode`, `agent_api_kind`) into the `mine` / `automatic` /
`bundle` discriminator the UI tabs use.

### 2. The link is the sync boundary

Nothing reaches a container because it was *shared*; it reaches a container because it is *linked*.
`CredentialsService.get_agent_credentials` selects `Credential` joined to `AgentCredentialLink` for
one `agent_id`, and that list is the input to everything downstream.
`CredentialsService.link_credential_to_agent` gates the insert on
`CredentialShareService.can_user_access_credential` (owner or share recipient) plus agent ownership,
and for an agent2agent `mcp_provider` it enforces one-consumer-per-pair by binding
`mcp_consumer_agent_id` on first link. Both link and unlink end by calling an event handler that
re-runs the whole materialization pass for the affected agent.

### 3. Something triggers a materialization pass

Three distinct triggers exist:

- **Live push.** `CredentialsService.sync_credentials_to_agent_environments` selects the agent's
  environments with `status == "running"`, builds the payload once, and pushes to each. Its callers are
  `event_credential_updated`, `event_credential_deleted`, `event_credential_shared`,
  `event_credential_unshared` and `user_details_service.event_user_details_updated` (which fans out
  over every agent the user owns, because the `current_user` block is per-user, not per-credential).
- **Environment start / resume / rebuild.** `EnvironmentLifecycleManager._sync_dynamic_data`
  (`backend/app/services/environments/environment_lifecycle.py`) runs the same preparation and push as
  part of the "Syncing credentials..." progress step. A failure here is not swallowed: if the container
  is still alive, the environment is put into `critical_state` with
  `cause="credential_sync_failed"`, and the exception is logged as `type(e).__name__: e` so the
  payload never lands in the log.
- **OAuth refresh (before push).** Env start and `sync_credentials_to_agent_environments` call
  `CredentialsService.prepare_fresh_credentials_for_environment`, which first runs
  `refresh_expiring_credentials_for_agent`: Google OAuth credentials expiring within
  `OAUTH_REFRESH_THRESHOLD_SECONDS` (30 min) refresh through `OAuthRefreshService`, and `mcp_provider`
  `auth_mode="oauth_dcr"` via `MCPProviderOAuthService.refresh_access_token`. Per stream turn,
  `SessionStreamProcessor` (`backend/app/services/sessions/stream_processor.py`) runs the same refresh
  after `on_stream_starting` (covers UI, channel follow-ups, A2A) and calls
  `sync_credentials_to_agent_environments` only if something was refreshed. A 10-minute sweep
  (`oauth_refresh_scheduler.py`) keeps idle running environments fresh, and a script can pull a token
  via `POST /api/v1/agent/credentials/{id}/access-token`. Every failure path is graceful — the push
  proceeds with the stale token. See [OAuth Credentials](../agents/agent_credentials/oauth_credentials.md).

### 4. `prepare_credentials_for_environment` builds the payload

This one function (`credentials_service.py`) is the whole transformation, and its ordering is
load-bearing:

1. **Decrypt.** `get_agent_credentials_with_data` decrypts each row into
   `{id, name, type, notes, service_uri, is_placeholder, credential_data}`. The last two are
   **top-level and type-agnostic**, outside `credential_data`, so steps 5 and 7 — which act on
   `credential_data` alone — can neither drop nor mask them: that is what lets a script find a
   credential by **slot** whatever its type, and tell a filled one from an empty one. `api_token`
   rows are additionally rewritten by `_process_api_token_credential` into a ready-to-use
   `http_header_name` / `http_header_value` pair, and keep the older in-data `service_uri` copy.
2. **Shape `agent_api` credentials.** `_shape_agent_api_keys_for_env` resolves `AgentApiToken.kind`,
   the credential owner and the issued-key status in batch. Connections are kept when their producer
   still exists. An **issued key** is kept only when credential owner == key recipient == the
   environment owner (the Desktop caller on the device path); otherwise dropped. An active key ships
   with its token; a non-active key ships **token-less** with top-level `unavailable_reason`
   (`key_revoked` | `key_expired` | `key_paused`) and `key_status`, and `credential_data` cut to
   `{base_url, spec_url, label, producer_agent_id}`. On the Desktop path key URLs are recomputed from
   the public origin. It fails closed twice: an orphan with no bound token is dropped, and if the
   lookup raises **every** `agent_api` credential is dropped. This runs first so keys are marked
   before the README render and the `has_agent_api_cred` test in step 6, which counts connections only.
3. **Split out-of-band payloads.** `google_service_account` rows have their full JSON moved to a
   sibling `service_account_files` list and their in-file data replaced by
   `_process_service_account_credential` (a `{file_path, project_id, client_email}` reference).
   `ssh_key` rows have `private_key` / `passphrase` moved to a sibling `ssh_keys` list and their
   in-file data replaced by `_process_ssh_key_for_env` (public metadata only).
4. **Rewrite URLs for the container network.** `_rewrite_agent_api_urls_for_env` swaps only the
   scheme+host of `base_url` / `spec_url` to `settings.AGENT_ENV_BACKEND_URL`, preserving the path,
   because the stored value is the public `FRONTEND_HOST` proxy URL the UI shows. Done *before*
   filtering so the whitelist and the README both carry the rewritten value.
5. **Whitelist.** `filter_credential_data_for_agent_env` keeps only fields listed in
   `AGENT_ENV_ALLOWED_FIELDS` for that type, and returns `{}` for an unknown type. OAuth types expose
   `access_token` and metadata but never `refresh_token` or `client_secret`. `mcp_provider` rows are
   skipped entirely by an explicit `continue` before this step — the empty whitelist is only a second
   line of defence.
6. **Append synthetic blocks.** `user_details_service.build_current_user_block` appends an entry with
   `id="current_user"` carrying the owner's username, email, timezone, locale, conversation style and
   parsed `custom_details`. If the agent has ≥1 `agent_api` credential in the unfiltered list,
   `AgentApiIdentityService.build_owner_identity_block` appends an `id="owner_identity"` entry with a
   freshly minted JWT, its header name, and usage text. Both are appended (not prepended), both are
   wrapped in `try/except` so a missing owner can never break credential sync, and neither has a DB row.
7. **Render the README.** `generate_credentials_readme` runs over the *filtered* list so the README
   structure mirrors `credentials.json` exactly, then `redact_credential_data` replaces
   `SENSITIVE_FIELDS` values with `***REDACTED***` — but only when the field has a non-empty value, so
   an unconfigured field still reads as empty.

The return value is `{credentials_json, credentials_readme, service_account_files, ssh_keys}`.

### 5. The MCP manifest is built separately

`CredentialsService.collect_mcp_provider_manifest(session, agent_id, mode)` is called once per mode.
For each linked `MCP_PROVIDER` credential whose `mcp_mode_<mode>` flag is on, it decrypts, applies
`_rewrite_mcp_endpoint_for_env` (netloc swap to `settings.MCP_SERVER_CONTAINER_URL`, only for
`auth_mode="agent2agent"`), and emits
`{key: "cinna_mcp_<credential_id>", url, transport, headers: {"Authorization": "Bearer <token>"}}`.
The `cinna_mcp_` prefix is what keeps these from colliding with the knowledge / agent_task bridges or
plugin-declared servers.

### 6. Transport into the container

`DockerEnvironmentAdapter.set_credentials`
(`backend/app/services/environments/adapters/docker_adapter.py`) POSTs the payload to
`{base_url}/config/credentials`; `set_mcp_servers` POSTs `{"conversation": [...], "building": [...]}`
to `{base_url}/config/mcp-servers`. Both carry the env auth token. The interface is declared on
`EnvironmentAdapter` (`backend/app/services/environments/adapters/base.py`). Credential sync raises on
failure; MCP sync is always caught and treated as non-blocking by both callers
(`sync_credentials_to_agent_environments` and `_sync_mcp_servers_to_environment`).

### 7. Env-core writes the files

`POST /config/credentials` (`backend/app/env-templates/app_core_base/core/server/routes.py`) calls
`AgentEnvService.update_credentials`
(`backend/app/env-templates/app_core_base/core/server/agent_env_service.py`), which:

- writes `credentials/credentials.json` and `credentials/README.md`;
- writes each `{credential_id}.json` service-account file, then **deletes every other `*.json`** in
  the credentials dir that is not `credentials.json` and not in the current id set — orphan
  reconciliation on every sync;
- calls `update_ssh_keys`, which materializes private keys into `~/.ssh/` (never under
  `credentials/`, which is a user-visible workspace surface), seeds `known_hosts`, and reconciles
  orphaned `id_<uuid>` files. Its failure is caught so it cannot block the rest of the sync;
- feeds the *whitelisted* values to `credential_guard.update_values`
  (`backend/app/env-templates/app_core_base/core/server/security/credential_guard.py`) for SSE output
  redaction. SSH private keys are deliberately not fed to the guard — they never appear in any
  agent-readable file.

`POST /config/mcp-servers` calls `AgentEnvService.set_mcp_servers`, which normalises each entry
(dropping any without a `key` or `url`), overwrites `mcp/user_mcp.json` wholesale, and `chmod 0600`s
it because entries carry bearer tokens. Overwrite-wholesale is the declarative contract: a
disconnected provider simply stops appearing and disappears.

### 8. AI credentials take the other road

`AICredential` rows never travel through `credentials.json`. They are resolved into an in-memory
credential bag inside `EnvironmentLifecycleManager._update_environment_config`, which runs only on
create / start / resume / rebuild:

1. `_usable_assigned_credential` decides, **per mode**, whether the stored
   `conversation_ai_credential_id` / `building_ai_credential_id` counts as assigned — it must exist
   *and* satisfy `is_credential_compatible_with_sdk` for that mode's SDK.
2. `_resolve_assigned_credential_into_bag` fetches the row via
   `ai_credentials_service.get_credential_for_use` (owner or share recipient), files it by its
   **actual** type via `apply_credential_to_bag`, fills only empty bag slots, and captures the
   admin-curated `default_model` into a per-mode carrier.
3. For any mode without a usable assigned id, `_fallback_fill_bag_for_sdk` fills that slot from the
   user's named default for the expected type (`get_default_for_type`), then from the legacy encrypted
   profile for the three types it stores.
4. `_generate_env_file` writes `.env` with `ANTHROPIC_API_KEY` *or* `CLAUDE_CODE_OAUTH_TOKEN` (chosen
   by `detect_anthropic_credential_type`), `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `SDK_ADAPTER_*` and the
   per-mode `MODEL_BUILDING` / `MODEL_CONVERSATION` resolved through
   `resolve_model` (`backend/app/services/environments/model_catalog.py`) with precedence
   *env per-mode override → credential default → catalog tier default*.
5. MiniMax gets `_generate_minimax_settings_files` (`app/core/.claude/{mode}_settings.json` with
   `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`); OpenCode gets `_generate_opencode_config_files`
   (`app/core/.opencode/{mode}/opencode.json` with the API key written directly into
   `provider.<id>.options.apiKey`, since `opencode serve` reads the file at start).

### 9. The container consumes it

- **Scripts** read `/app/workspace/credentials/credentials.json` directly, or through
  `backend/app/env-templates/app_core_base/core/cinna_api/credentials.py`, which re-reads the file on
  **every** call so a just-refreshed OAuth token is picked up by a long-running serving child.
  Beyond `get` / `by_type`, a script can ask by **slot** — `by_slot`, `require_slot` and
  `agent_api_session`, which read the top-level `service_uri` and `is_placeholder` written in step 4.
  `require_slot` raises `CredentialMissing` with a sentence naming the slot and the fix, so a skill
  whose credential was never provisioned fails loudly and repairably rather than silently. The
  synthetic `current_user` / `owner_identity_token` entries never satisfy a slot. These helpers are
  new SDK code and need an **environment rebuild**; the two keys themselves reach any container.
  The README's Slots section tells the agent the same: how to check for `require_slot`, and, if it
  is missing, to ask for a rebuild and read `credentials.json` by `service_uri` meanwhile.
- **Prompts** get the redacted README: `PromptGenerator._load_credentials_readme`
  (`backend/app/env-templates/app_core_base/core/server/prompt_generator.py`) inlines it into the
  building prompt under "Available Credentials" with rules telling the agent to read the values only
  from scripts, never in conversation.
- **SDK sessions** merge the MCP manifest at session start.
  `AgentEnvService.get_user_mcp_servers_for_mode(mode, engine)` reads `user_mcp.json`, filters by
  mode, and translates each entry per engine — `{"type": "http"|"sse", url, headers}` for
  `claude_code`, `{"type": "remote", url, headers, "enabled": true}` for `opencode`. The Claude Code
  adapter merges the result into `options.mcp_servers` and appends `mcp__<key>` to `allowed_tools`
  (`.../adapters/claude_code_sdk_adapter.py`); the OpenCode adapter merges it into the config's `mcp`
  section (`.../adapters/opencode_sdk_adapter.py`). The bearer token in `headers` is the only place
  an `mcp_provider` token reaches the container.

## Decision points

| Where | Decides on | Outcomes |
|---|---|---|
| `credentials_service.py` `link_credential_to_agent` | share/ownership + agent2agent pair binding | link inserted / `ValueError` / `mcp_consumer_agent_id` bound on first link |
| `credentials_service.py` `unlink_credential_from_agent` | is the unlinked agent the recorded agent2agent consumer? | plain unlink + sync, or credential auto-deleted with its bound token |
| `credentials_service.py` `sync_credentials_to_agent_environments` | `AgentEnvironment.status == "running"` | push now, or defer to the next `_sync_dynamic_data` |
| `credentials_service.py` `_shape_agent_api_keys_for_env` | `AgentApiToken.kind`, credential owner vs recipient vs env owner, key status (lookup may raise) | keep connections / keep a recipient's key (token-less when not active) / drop other keys / drop **all** `agent_api` on lookup failure |
| `credentials_service.py` `prepare_credentials_for_environment` | `type == "mcp_provider"` | row excluded from `credentials.json` and README entirely |
| `credentials_service.py` `filter_credential_data_for_agent_env` | field ∈ `AGENT_ENV_ALLOWED_FIELDS[type]` | field synced, or silently excluded (`{}` for unknown types) |
| `credentials_service.py` `prepare_credentials_for_environment` | any `agent_api` **connection** present (never for an issued key or on Desktop)? | `owner_identity` block appended, or omitted |
| `credentials_service.py` `_rewrite_agent_api_urls_for_env` | `AGENT_ENV_BACKEND_URL` has a netloc | rewrite to container origin, or warn and keep the public URL |
| `credentials_service.py` `_rewrite_mcp_endpoint_for_env` | `auth_mode == "agent2agent"` and `MCP_SERVER_CONTAINER_URL` set | netloc swapped, or endpoint used verbatim |
| `credentials_service.py` `collect_mcp_provider_manifest` | `mcp_mode_<mode>` flag, `endpoint_url` present | entry emitted / skipped for this mode |
| `credentials_service.py` `refresh_expiring_credentials_for_agent` | `expires_at <= now + threshold`, `auth_mode`, `refresh_error_kind` | refresh (then push), or proceed with the stale token; `reauth_required` skips attempts |
| `install_service.py` `_try_link_publisher_credential` | publisher still owns it and `allow_sharing` still true | share + link, or fall through to an installer-owned placeholder |
| `environment_lifecycle.py` `_usable_assigned_credential` | id exists **and** type matches the mode's SDK | treated as assigned (fallback suppressed) or not-assigned (fallback runs) |
| `environment_lifecycle.py` `_generate_env_file` | `detect_anthropic_credential_type(key)` | `ANTHROPIC_API_KEY=` or `CLAUDE_CODE_OAUTH_TOKEN=` |
| `environment_lifecycle.py` `_sync_dynamic_data` | `set_credentials` raised and container still alive | environment enters `critical_state` `credential_sync_failed` |
| `agent_env_service.py` `update_credentials` | `*.json` in credentials dir not in the current SA id set | file deleted as an orphan |
| `agent_env_service.py` `_translate_user_mcp_for_engine` | `engine`, `transport` | Claude `http`/`sse` config, OpenCode `remote` config, or `None` |

## Where to change what

| If you want to… | Edit | Re-check |
|---|---|---|
| Expose a new field of an existing credential type to scripts | `AGENT_ENV_ALLOWED_FIELDS` in `credentials_service.py` | `SENSITIVE_FIELDS` (does it need redacting in the README?), `generate_credentials_readme` output, `credential_guard.update_values` now guards the new value |
| Add a whole new credential type | `AGENT_ENV_ALLOWED_FIELDS` + `SENSITIVE_FIELDS` + `REQUIRED_FIELDS` in `credentials_service.py` | `classify_credential_category`, `check_credential_completeness`, whether it needs an out-of-band payload like `ssh_keys` / `service_account_files` (which also needs an env-core writer + orphan reconciler) |
| Change what a container sees as `current_user` | `build_current_user_block` in `backend/app/services/users/user_details_service.py` | `event_user_details_updated` fan-out, the `## Current User` README section, any script reading the block |
| Change the MCP server shape the SDK receives | `_translate_user_mcp_for_engine` in `agent_env_service.py` | both SDK adapters' merge sites, `_normalise_mcp_entry`, and that pre-feature containers have an old `/app/core` copy |
| Add a field to the MCP manifest | `collect_mcp_provider_manifest` in `credentials_service.py` **and** `_normalise_mcp_entry` in `agent_env_service.py` | the normaliser drops unknown/invalid entries, so both sides must ship together |
| Change which env var an AI credential lands in | `sdk_constants.py` (`SDK_TO_CREDENTIAL_TYPE`, `CREDENTIAL_TYPE_TO_BAG_KEY`, `apply_credential_to_bag`) | `_generate_env_file`, `_generate_opencode_config_files`, `_generate_minimax_settings_files`, `_usable_assigned_credential` |
| Make an AI credential change reach a container without a restart | there is no live path today — add one beside `sync_credentials_to_agent_environments` | `EnvironmentService.get_environments_for_credential` already computes the affected set (explicit link + default resolution) |
| Change bundle or skill credential provisioning | `CredentialProvisioner` in `backend/app/services/credentials/credential_provisioner.py` — the policy field first, the shared mechanism only if both must change | `CredentialShare.source` stamping (drives the Credentials tabs), `is_placeholder` flip in `update_credential`, deletion blast-radius gate, and the **bundle install suites, which must pass unedited** |
| Change what a skill can declare | `parse_credential_declarations` in `backend/app/services/agents/skill_manifest.py` — **and the identical vendored copy** in `core/server/skill_manifest.py` | the `invalid_credentials` message, `AgentSkillsService._normalise_credential_declarations` (the tolerant cache reader), and whether publish's `resolve_for_publish` needs the new field |
| Add a new re-sync trigger | call `CredentialsService.sync_credentials_to_agent_environments` | it filters to `running` envs only — a stopped env picks the change up at its next `_sync_dynamic_data` |

## Invariants and traps

- **Unlink narrows to users without any remaining path.** Revoking a direct share, a group share, removing a group member or deleting a group deletes links and re-syncs only for users who are neither owner, direct recipient nor covered by another group share (`CredentialShareService.users_without_access`). Group removal precomputes the covered (credential, user) pairs first and releases only those; no `credential_share_revocation` rows are written. For AI credentials the lost user's env pin columns are NULLed and running environments restarted, since `AICredential` changes otherwise land only on restart. See [User Groups](../application/user_groups/user_groups.md).
- **The link, not the share, is what syncs.** A `CredentialShare` only makes a credential *linkable*.
  Every downstream read starts from `AgentCredentialLink`.
- **A slot is a plain column, and that is the point.** `Credential.service_uri` is non-secret,
  user-editable and type-agnostic, so it reaches `credentials.json` at the **top level** rather than
  through `AGENT_ENV_ALLOWED_FIELDS`. Putting it in the whitelist would mean every new credential
  type had to re-earn the ability to answer a skill's slot lookup — and for `agent_api`, the type the
  feature exists for, the whitelist was already dropping it.
- **Two refresh classes, deliberately.** `Credential` changes push live to running containers.
  `AICredential` changes do not — they land only when `_update_environment_config` regenerates `.env`
  and the SDK config files, i.e. on start / restart / rebuild. Rotating a company LLM key does not
  reach a running container until it is restarted.
- **`mcp_provider` never appears in `credentials.json`.** It is excluded by an explicit `continue`
  *and* by an empty whitelist. Its token exists in the container only inside the `headers` of a
  `user_mcp.json` entry (mode `0600`).
- **`ssh_key` private material never appears in `credentials.json` either.** It travels on the sibling
  `ssh_keys` payload straight into `~/.ssh/`. A consequence noted in the code: `CredentialGuard` is
  fed only the whitelisted values, so private-key bodies are not output-redacted — they are simply
  never in an agent-readable file.
- **Issued `agent_api` keys reach only their recipient's own environments or Desktop.** A key never
  lands where someone else's agent runs, and a key that is no longer active is delivered **without
  its token** (`unavailable_reason` + `key_status`) rather than silently dropped, so the slot can
  explain itself. `_shape_agent_api_keys_for_env` fails closed on a lookup error by dropping *all*
  `agent_api` rows, because `token` is a whitelisted field; a key never triggers the
  `owner_identity` block.
- **Stored URL ≠ container URL.** `agent_api` `base_url`/`spec_url` and agent2agent MCP `endpoint_url`
  are stored with the public host for UI display and rewritten to the internal Docker origin during
  preparation. Both rewrites swap only the netloc; both no-op with a warning if the internal setting
  has no host.
- **Ordering inside `prepare_credentials_for_environment` is load-bearing.** Key-dropping precedes the
  `has_agent_api_cred` test; SA/SSH extraction precedes whitelisting; URL rewriting precedes both the
  whitelist and the README render. The README is generated from the *filtered* list precisely so its
  structure matches `credentials.json`.
- **Synthetic blocks are appended, never prepended,** so index-based readers of `credentials.json` are
  undisturbed, and both are wrapped so a missing owner degrades to omission rather than a failed sync.
- **Fresh reads matter.** `cinna_api.credentials` re-reads the file on every call by design; caching it
  at import time would serve stale secrets across an OAuth refresh. The same reasoning drives the
  refresh being followed by an immediate resync.
- **A trap that has bitten: per-mode AI credential fallback.** An earlier all-or-nothing gate ("fall
  back only if *neither* mode is assigned") silently left e.g. `ANTHROPIC_API_KEY` empty for a
  `claude-code/anthropic` building mode whenever the *other* mode happened to pin a credential. The
  fallback is now gated per mode; the comment at the call site in `_update_environment_config` records
  why.
- **A trap that has bitten: unlink ordering.** `unlink_credential_from_agent` captures
  `get_affected_agents` *before* removing the link, because on the agent2agent auto-delete path the
  link is already gone by delete time and a post-delete re-query would skip the consumer's env sync —
  leaving a dead MCP server live in its container until some unrelated later sync.
- **A trap that has bitten: `/app/core` is a per-environment copy, not baked into the image.** An
  environment created before `POST /config/mcp-servers` existed returns 404 for it. Because MCP sync is
  non-blocking, this fails silently; only a rebuild refreshes the core.
- **Orphan reconciliation is by convention, not by manifest.** `update_credentials` deletes any
  `*.json` in the credentials dir outside the current service-account id set, and `update_ssh_keys`
  owns anything named `id_<uuid>` under `~/.ssh/`. Hand-authored files matching those shapes will be
  removed.
- **Credential sync failure is loud; MCP sync failure is quiet.** `set_credentials` failing on a live
  container drives the environment into `critical_state`; `set_mcp_servers` failing is logged as a
  warning and swallowed at every call site.

## See also

- [Agent Credentials](../agents/agent_credentials/agent_credentials.md) · [tech](../agents/agent_credentials/agent_credentials_tech.md) · [whitelist](../agents/agent_credentials/credentials_whitelist.md) · [whitelist tech](../agents/agent_credentials/credentials_whitelist_tech.md)
- [Credential Sharing](../agents/agent_credentials/credential_sharing.md) · [tech](../agents/agent_credentials/credential_sharing_tech.md)
- [SSH Key Credentials](../agents/agent_credentials/ssh_key_credentials.md) · [Google Service Account](../agents/agent_credentials/google_service_account.md) · [OAuth Credentials](../agents/agent_credentials/oauth_credentials.md)
- [AI Credentials](../application/ai_credentials/ai_credentials.md) · [tech](../application/ai_credentials/ai_credentials_tech.md) · [Admin AI Credential Provisioning](../application/ai_credentials/admin_ai_credential_provisioning.md)
- [Agent REST API](../agents/agent_api/agent_api.md) · [tech](../agents/agent_api/agent_api_tech.md)
- [Agent-to-Agent MCP Connector](../application/mcp_integration/agent_to_agent_mcp_connector.md) · [tech](../application/mcp_integration/agent_to_agent_mcp_connector_tech.md)
- [Agent Environment Core](../agents/agent_environment_core/agent_environment_core.md) · [Multi-SDK Support](../agents/agent_environment_core/multi_sdk.md) · [tech](../agents/agent_environment_core/multi_sdk_tech.md)
- [Agent Bundles](../agents/agent_bundles/agent_bundles.md) · [Agent Environments](../agents/agent_environments/agent_environments.md)
