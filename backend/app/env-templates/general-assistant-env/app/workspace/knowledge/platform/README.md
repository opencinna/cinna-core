# Cinna Core

A conversational AI agent platform where users create custom AI agents, run them in isolated Docker environments, and interact through persistent chat sessions. Agent developers publish versioned bundles to a catalog; other users install them. Agents can be scheduled, triggered by email/webhooks, and exposed via A2A and MCP protocols.

**Stack:** FastAPI + PostgreSQL | React + TypeScript + TanStack | Docker isolation | SQLModel ORM

## Core Idea

The platform separates three distinct layers:

- **Agent** (logical definition) — what the agent does: custom prompts, credentials, SDK configuration
- **Environment** (runtime instance) — where it runs: a Docker container with the agent's workspace, tools, and files
- **Session** (conversation) — how users interact: a persistent chat thread with independent message history

One agent can have multiple environments (for testing, production, or rollback via blue-green deployment). One environment can host multiple sessions that share the same file workspace but maintain separate conversation histories.

Agents operate in two modes:
- **Building mode** — development state; agent uses a larger context window, can create/modify scripts and configure integrations
- **Conversation mode** — execution state; agent runs pre-built workflows with a lightweight prompt for faster, cheaper responses

Sessions can be started manually, by automated triggers (CRON, email, webhook), or by other agents via handover. External systems can connect through A2A (Agent-to-Agent protocol) or MCP (Model Context Protocol).

---

## Glossary

| Term | Definition |
|------|-----------|
| **Agent** | User-defined AI assistant with custom prompts, credentials, and SDK configuration |
| **Agent Environment** | Runtime instance (Docker container or remote server) where an agent executes |
| **Session** | Persistent chat conversation between a user and an agent environment |
| **Message** | Single communication unit within a session (user or agent) |
| **Activity** | Logged event or summary of agent actions within a session |
| **Credential** | Encrypted API key or service account used by agents (e.g., Gmail, Odoo) |
| **AI Credential** | LLM provider API key (Claude, OpenAI, etc.) for agent runtime |
| **Knowledge Source** | Git-based repository of documentation agents can query via RAG |
| **Agent Plugin** | Marketplace capability that extends agent functionality |
| **Input Task** | User-submitted task that goes through refinement before agent execution; extended with short-code IDs, comments, attachments, status history, and subtask delegation |
| **Task Trigger** | Automated rule (CRON, webhook, date) that creates tasks for an agent |
| **Agent Bundle** | Publisher-owned, versioned packaging of an agent identified by a reverse-DNS bundle ID. Publishing an agent creates a bundle; each publish creates an immutable revision |
| **Bundle Revision** | Immutable snapshot of a bundle's workspace content (scripts, docs, knowledge, files, prompts, SDK settings, agent schedules) taken at publish time |
| **Install** | A user's running copy of a published bundle — an `Agent` row seeded from a bundle revision with its own persistent App Data volume |
| **App Data** | Per-user, per-bundle persistent storage volume (`/app/workspace/app-data`) that survives uninstall and reattaches on reinstall |
| **Guest Share** | Token-based, time-limited, revocable link that gives unauthenticated (or authenticated) viewers chat-only access to a specific agent install; protected by a 4-digit security code, scoped to conversation mode, and optionally exposes a read-only environment panel |
| **Handover** | Agent-to-agent task delegation within the platform |
| **Workspace** | Isolation boundary for user's agents, sessions, and resources |
| **AI Function** | LLM utility for text generation, classification, extraction with multi-provider cascade fallback |
| **Building Mode** | Agent environment state for configuration and development |
| **Conversation Mode** | Agent environment state for executing tasks and chat |
| **Agent User** | Default role for new signups — can install, chat, and manage settings; cannot create agents or publish bundles |
| **Agent Developer** | Admin-promoted role — unlocks agent creation, building mode, publishing, and all developer UI |
| **A2A** | Agent-to-Agent protocol for cross-platform agent communication |
| **MCP** | Model Context Protocol - exposes agents as tool servers to external LLM clients |
| **App MCP Server** | Universal MCP endpoint that routes messages to agents via pattern matching or AI classification |
| **App Agent Route** | Binding between an agent and users with routing rules for the App MCP Server |
| **Router Trigger Prompt** | Short natural-language sentence on an agent (and snapshotted into bundle revisions) that the App MCP router uses to classify incoming messages and pick the right agent; auto-populated at install time from the revision snapshot |
| **Identity MCP Server** | Person-level routing layer on top of the App MCP Server — users expose themselves as a named identity that other users can address by name, with two-stage routing (person resolution then agent selection) |
| **Identity Agent Binding** | Configuration linking one of a user's agents to their identity, with a trigger prompt and per-caller access control |
| **Identity Binding Assignment** | Per-caller access record granting a specific user access to a specific identity agent binding |
| **MCP Connector** | Configuration that connects an agent environment to an external MCP server |
| **Agent Webapp** | Lightweight data dashboard (HTML/CSS/JS) served from an agent's workspace via shareable URLs |
| **Webapp Share** | Token-based public access to an agent's webapp for unauthenticated viewers |
| **Agent REST API** | A capability-narrowed HTTP API that a producer agent builds from plain Python functions decorated with the `cinna_api` SDK and exposes through the platform proxy. The powerful upstream credential stays in the producer container; consumers get only the surface the producer chose to expose. A deterministic, no-LLM-at-call-time code-to-code channel (contrast with A2A and MCP). |
| **`agent_api` credential** | A `CredentialType.AGENT_API` credential storing `{ base_url, token, spec_url, label, producer_agent_id }` — it **is** the connection between a consumer agent and a producer's REST API. Created by the "Connect Agent API" helper, it rides the existing credential sync / whitelist / redaction pipeline; `token` is redacted in agent prompts. Deleting it disconnects (cascade-deletes the token). Cross-user sharing is safe because the shared value is the narrowed proxy, not the upstream secret. |
| **`agent_api_token`** | Opaque scoped token (SHA256 hash at rest, 8-char display prefix) that authenticates consumer calls to a producer agent's REST API. Internal machine credential — never created or revoked by hand: it is minted by the connect helper and bound to its `agent_api` credential via `credential_id` (`ON DELETE CASCADE`), so deleting the credential is the only way to revoke. Never expires. `read_only_override` may only narrow the producer's `policy.yaml`, never widen it. |
| **System Notification** | A platform-generated alert about a backend event (e.g. a session error), dispatched to the event owner via email. Distinct from agent-authored messages and the in-app activity feed. Governed by the Notification Catalog and per-user preferences. |
| **Notification Catalog** | The code-only registry (in `notification_catalog.py`) of every system notification type the platform supports. Each entry defines user-facing copy, the default opt-in state, the email template filename, the subject builder, and the dedup key. Adding a new type requires only a new enum value, a catalog entry, and a built email template — no schema migration. |
| **App Sync** | Zero-knowledge server-side sync substrate for native clients (Cinna Desktop, future Cinna Mobile). Stores E2E-encrypted documents partitioned by `collection`; the server never holds a key that can decrypt them. |
| **App Sync Record** | One row in `app_sync_record` representing a single synced entity (a note, a job, a folder, etc.). Keyed by `(user_id, collection, client_entity_id)`; carries an opaque ciphertext payload and a per-user monotonic `seq` for delta pulls. |
| **User Master Key (UMK)** | A per-account 256-bit random key that is the root of the App Sync key hierarchy. Generated once on the first device; never transmitted or stored in plaintext anywhere — the server holds only wrapped (encrypted) copies in `app_sync_key_envelope`. |
| **Key Envelope** | A row in `app_sync_key_envelope` containing the UMK sealed/wrapped under one unlock method (`device`, `recovery`, or `passphrase`). The server stores and returns the `wrapped_key` verbatim and cannot open it. |
| **Device Pairing** | Mechanism for sharing the UMK with a new device via a hardened commit-then-reveal handshake. The joining device generates an ephemeral keypair and a commitment (`blake2b(pubkey ‖ nonce_J)`) and calls `POST /pairing/start`. A trusted unlocked device discovers the request from `GET /pairing/inbox`, posts its `sealer_nonce` via `POST /pairing/inbox/{id}/sealer-nonce`, and seals the UMK via `POST /pairing/inbox/{id}/complete` after the joiner reveals its nonce and the commitment is verified client-side. A 6-digit SAS (`trunc6(blake2b(pubkey ‖ nonce_J ‖ nonce_S))`) is compared out-of-band. The server is a dumb relay: it stores and forwards opaque strings and never verifies the commitment or decrypts the UMK. |

---

## Domain Map

| Domain | Description |
|--------|-------------|
| [agents](#agents) | Core agent lifecycle - creation, configuration, environments, sessions, chat, file management |
| [tasks](#tasks) | Task submission, refinement, triggers, and scheduling |
| [credentials](#credentials) | Credential management, encryption, AI provider keys |
| [application](#application) | User-facing platform features - authentication, roles, integrations, real-time events, workspaces |
| [knowledge](#knowledge) | Git-based knowledge sources, vector search, RAG |
| [sharing](#sharing) | Guest access, workspace collaboration |
| [agentic_teams](#agentic_teams) | Visual org-chart builder for agent orchestration topology — teams, nodes, connections with AI-generated handover prompts |
| [development](#development) | Backend/frontend patterns, AI functions, debugging |
| [infrastructure](#infrastructure) | Deployment-level concerns — nginx reverse proxy, well-known URIs, origin-root routing |

---

## Wiki

| Article | Description |
|---------|-------------|
| [Why Cinna Agents?](wiki/skills_plugins_vs_cinna_agents.md) | Skills & plugins vs. Cinna agents — point-by-point comparison for SMBs evaluating AI automation approaches |
| [Claude Managed Agents vs. Cinna](wiki/claude_managed_agents_vs_cinna_agents.md) | Claude Managed Agents (Anthropic's hosted agent API) vs. Cinna agents — architecture, capabilities, and trade-offs |

---

## Feature Registry

### agents

| Feature | Description | Docs |
|---------|-------------|------|
| agent_environments | Docker container architecture, build layers, workspace isolation, multi-image templates | [business logic](agents/agent_environments/agent_environments.md) \| [tech](agents/agent_environments/agent_environments_tech.md) \| [multi-image](agents/agent_environments/agent_multi_image_environments.md) \| [credential rebuild](agents/agent_environments/affected_environments_rebuild.md) \| [sdk session persistence](agents/agent_environments/sdk_session_persistence.md) |
| agent_prompts | System prompt construction for building and conversation modes; three bidirectional prompt docs (workflow/entrypoint/refiner) use three-way reconcile + LWW tiebreak between DB and env-container; pull-only caches (STATUS.md, CLI_COMMANDS.yaml) unified under the Synced Workspace File Registry | [business logic](agents/agent_prompts/agent_prompts.md) \| [tech](agents/agent_prompts/agent_prompts_tech.md) |
| agent_commands | Slash commands in agent sessions — `/files`, `/session-recover`, `/session-reset`, `/rebuild-env`, `/agent-status`, and the `/run:*` family — with autocomplete popup UI. Command output with `include_in_llm_context=True` is forwarded to the next LLM turn via a `<prior_commands>` XML block | [business logic](agents/agent_commands/agent_commands.md) \| [tech](agents/agent_commands/agent_commands_tech.md) \| [files](agents/agent_commands/files_command.md) \| [recovery](agents/agent_commands/session_recovery_command.md) \| [reset](agents/agent_commands/session_reset_command.md) \| [rebuild-env](agents/agent_commands/rebuild_env_command.md) \| [autocomplete](agents/agent_commands/slash_command_autocomplete.md) \| [agent-status](agents/agent_commands/agent_status_command.md) \| [non-llm context bridging tech](agents/agent_commands/non_llm_context_bridging_tech.md) |
| agent_plugins | Plugin marketplace integration, capability loading | [business logic](agents/agent_plugins/agent_plugins.md) \| [tech](agents/agent_plugins/agent_plugins_tech.md) |
| agent_schedulers | Multi-schedule CRON execution with natural language input, two schedule types (static prompt, script trigger), and execution logging | [business logic](agents/agent_schedulers/agent_schedulers.md) \| [tech](agents/agent_schedulers/agent_schedulers_tech.md) |
| agent_webhooks | Per-agent authenticated HTTP webhooks — two trigger types (session: starts a new agent session seeded with the payload; script: runs a shell command in the agent's Docker environment), bearer-token auth with Fernet encryption and one-time reveal, immutable invocation logs | [business logic](agents/agent_webhooks/agent_webhooks.md) \| [tech](agents/agent_webhooks/agent_webhooks_tech.md) |
| agent_handover | Agent-to-agent task delegation and inbox creation | [business logic](agents/agent_handover/agent_handover.md) \| [tech](agents/agent_handover/agent_handover_tech.md) |
| agent_environment_core | Server-side core running inside Docker containers: HTTP API, SDK adapters, prompt generation. Two SDK engines: Claude Code (Anthropic/MiniMax), OpenCode (Anthropic, OpenAI, Google, OpenAI-compatible). Each environment links separate AI credentials per mode (building/conversation) with optional per-mode model overrides. OpenCode uses MCP bridge servers for custom tools. All adapters emit unified lowercase tool names via `tool_name_registry.py`. | [business logic](agents/agent_environment_core/agent_environment_core.md) \| [tech](agents/agent_environment_core/agent_environment_core_tech.md) \| [multi-sdk](agents/agent_environment_core/multi_sdk.md) \| [multi-sdk tech](agents/agent_environment_core/multi_sdk_tech.md) \| [knowledge tool](agents/agent_environment_core/knowledge_tool.md) \| [create agent task tool](agents/agent_environment_core/create_agent_task_tool.md) \| [tools approval](agents/agent_environment_core/tools_approval_management.md) \| [tools approval tech](agents/agent_environment_core/tools_approval_management_tech.md) |
| agent_environment_data_management | Environment data flow — bundle-owned vs. App Data vs. credentials vs. runtime data; install creation and apply-update flows | [business logic](agents/agent_environment_data_management/agent_environment_data_management.md) \| [tech](agents/agent_environment_data_management/agent_environment_data_management_tech.md) |
| agent_bundles | Desktop-app-style bundle / install model — agents published as versioned bundles, installed by other users, push-updated; bundle ID format, visibility/grants, revisions, publisher working install; auto-creates an App MCP route at install time from the revision's router trigger prompt; publisher schedules are snapshotted into the revision and materialised on consumer installs (consumers can enable/disable and run, but not create/edit/delete); credential-sharing drift detection (publish-vs-live `provided_by` diff) with republish nudge in the Bundle tab | [business logic](agents/agent_bundles/agent_bundles.md) \| [tech](agents/agent_bundles/agent_bundles_tech.md) |
| agent_app_data | Per-user, per-bundle persistent App Data volume (`/app/workspace/app-data`) keyed by `(user_id, bundle_id, catalog_type)` — survives uninstall/reinstall, Settings tab management, manual wipe on orphaned volumes, plus a background GC that reclaims on-disk dirs with no DB representation after account/install deletion | [business logic](agents/agent_app_data/agent_app_data.md) \| [tech](agents/agent_app_data/agent_app_data_tech.md) |
| agent_credentials | Credential syncing to agent environments, whitelisting, redaction, OAuth refresh, three sharing modes (user / publisher / template), blast-radius-gated deletion (Tier 0/1/2, HTTP 409 + force escape hatch for PBP credentials in published bundles); `service_uri` slot-id matcher (Tier 0a/0b) enables per-user token auto-detection across differently-named credentials at bundle install time; publish-vs-live `provided_by` drift detection with republish nudge | [business logic](agents/agent_credentials/agent_credentials.md) \| [tech](agents/agent_credentials/agent_credentials_tech.md) \| [oauth](agents/agent_credentials/oauth_credentials.md) \| [whitelist](agents/agent_credentials/credentials_whitelist.md) \| [google SA](agents/agent_credentials/google_service_account.md) \| [ssh key](agents/agent_credentials/ssh_key_credentials.md) \| [ssh key tech](agents/agent_credentials/ssh_key_credentials_tech.md) \| [sharing](agents/agent_credentials/credential_sharing.md) \| [sharing tech](agents/agent_credentials/credential_sharing_tech.md) \| [security hardening](agents/agent_credentials/credential_security_hardening.md) \| [security hardening tech](agents/agent_credentials/credential_security_hardening_tech.md) \| [add credential widget](agents/agent_credentials/add_credential_widget.md) |
| agent_file_management | File upload/download, workspace file viewing, storage quota, garbage collection | [business logic](agents/agent_file_management/agent_file_management.md) \| [tech](agents/agent_file_management/agent_file_management_tech.md) \| [remote db viewer](agents/agent_file_management/remote_database_viewer.md) |
| agent_webapp | Lightweight data dashboards served from agent workspace via shareable URLs, with dynamic Python data endpoints | [business logic](agents/agent_webapp/agent_webapp.md) \| [tech](agents/agent_webapp/agent_webapp_tech.md) \| [chat widget](agents/agent_webapp/webapp_chat.md) \| [chat tech](agents/agent_webapp/webapp_chat_tech.md) \| [chat context](agents/agent_webapp/webapp_chat_context.md) \| [chat context tech](agents/agent_webapp/webapp_chat_context_tech.md) \| [chat actions](agents/agent_webapp/webapp_chat_actions.md) \| [chat actions tech](agents/agent_webapp/webapp_chat_actions_tech.md) \| [actions context](agents/agent_webapp/webapp_actions_context.md) \| [actions context tech](agents/agent_webapp/webapp_actions_context_tech.md) |
| agent_status_tracking | Agent self-reported status via `app-data/storage/STATUS.md` (per-install App Data, not bundle content) — optional YAML frontmatter (severity/summary/timestamp), DB-cached snapshot, `/agent-status` slash command, REST endpoints, agent-card footer in the agents list, post-action event-driven pull, A2A `agent/status` method, and a configurable **status refresh command** (shell or `/run:<name>` CLI reference) that runs inside the container before every forced/live fetch; managed via the Integrations tab **Agent status** card | [business logic](agents/agent_status_tracking/agent_status_tracking.md) \| [tech](agents/agent_status_tracking/agent_status_tracking_tech.md) \| [/agent-status command](agents/agent_commands/agent_status_command.md) | <!-- nocheck -->
| cli_commands | Agents declare named shell commands via `docs/CLI_COMMANDS.yaml` in their workspace. The platform syncs and caches the parsed list on `AgentEnvironment`, exposes commands as dynamic `/run:<name>` entries in the slash-command autocomplete popup (with tooltip showing the resolved shell string) plus a `/run-list` discovery entry (shown when the cache is non-empty), hides the bare `/run` from the popup, and fires `CLI_COMMANDS_UPDATED` events. `/run:<name>` queues a pending message, streams execution via agent-env `POST /command/stream` through `SessionStreamProcessor`'s batch loop, emits `STREAM_COMPLETED`/`STREAM_INTERRUPTED`/`STREAM_ERROR` on finish (wires up `session_interaction_status_changed` WS fanout, activity log, task/CLI/status cache refresh), and persists output as a terminal/markdown system message. CLI commands also surface as `cinna.run.*` A2A skills on the authenticated extended agent card. Command output is forwarded to the next LLM turn via the non-LLM context bridging feature. | [business logic](agents/cli_commands/cli_commands.md) \| [tech](agents/cli_commands/cli_commands_tech.md) | <!-- nocheck -->
| agent_api | Producer agent exposes a capability-narrowed REST API from plain decorated Python functions (`cinna_api` SDK) inside its container. env-core supervises the uvicorn child (lazily spawned on first call, idle-reaped after 5 min), harvests the OpenAPI spec import-only, and caches it on `AgentEnvironment`. Backend enforces declarative `policy.yaml` guardrails (method allowlist / body cap / rate limit / path allowlist) at the proxy edge. Consumers connect via a new `agent_api` credential type that rides the existing credential sync / whitelist / redaction pipeline. Cross-user sharing via `CredentialShare`. One-click "Connect to another agent" helper. `agent_api` connection credentials appear under **Automatic Credentials** in the global Credentials view (derived from `type == agent_api`). Bundle publisher-provided `agent_api` credentials supported via the one-shared-token model; pair with a per-user `api_token` + `service_uri` for scoped access. A2A is intelligence-to-intelligence; MCP is LLM-to-tool; `agent_api` is **code-to-code** (no LLM at call time). | [business logic](agents/agent_api/agent_api.md) \| [tech](agents/agent_api/agent_api_tech.md) \| [spec viewer](agents/agent_api/spec_viewer.md) \| [spec viewer tech](agents/agent_api/spec_viewer_tech.md) |

### tasks

| Feature | Description | Docs |
|---------|-------------|------|
| input_tasks | Task submission, AI refinement, execution workflow, comments (agent findings/results), file attachments, short-code IDs (TASK-1/HR-42), status history, subtask delegation, team-scoped tasks | [business logic](application/input_tasks/input_tasks.md) \| [tech](application/input_tasks/input_tasks_tech.md) |
| task_triggers | Automated triggers - CRON schedules, webhooks, date-based | [business logic](application/input_tasks/task_triggers.md) \| [tech](application/input_tasks/task_triggers_tech.md) |
| tools_approval | Agent tool execution approval management | [business logic](agents/agent_environment_core/tools_approval_management.md) \| [tech](agents/agent_environment_core/tools_approval_management_tech.md) |

### credentials

| Feature | Description | Docs |
|---------|-------------|------|
| ai_credentials | LLM provider API keys, named credentials, prioritized default resolution, environment linking, sharing. Supported types: Anthropic, MiniMax, OpenAI, OpenAI-compatible, Google | [business logic](application/ai_credentials/ai_credentials.md) \| [tech](application/ai_credentials/ai_credentials_tech.md) \| [anthropic types](application/ai_credentials/anthropic_credential_types.md) \| [affected envs](application/ai_credentials/affected_environments_widget.md) \| [ai functions routing](application/ai_credentials/ai_functions_sdk_routing.md) |

### application

| Feature | Description | Docs |
|---------|-------------|------|
| agent_management | Agent definition lifecycle — identity, prompts, SDK, credentials, integrations, sharing — the config entry point for all platform features | [business logic](application/agent_management/agent_management.md) \| [creation wizard](application/agent_management/new_agent_wizard.md) |
| agent_sessions | Persistent chat sessions between users/external systems and agent environments — lifecycle, modes, streaming, integration types, UI | [business logic](application/agent_sessions/agent_sessions.md) \| [tech](application/agent_sessions/agent_sessions_tech.md) \| [env panel widget](application/agent_sessions/app_env_panel_widget.md) \| [channel ingestion](application/agent_sessions/channel_ingestion.md) \| [channel ingestion tech](application/agent_sessions/channel_ingestion_tech.md) |
| auth | User authentication - JWT tokens, password login, Google OAuth, domain whitelist, password recovery | [business logic](application/auth/auth.md) \| [tech](application/auth/auth_tech.md) \| [google oauth](application/auth/google_oauth.md) |
| user_2fa | Optional per-user two-factor authentication — WebAuthn passkeys and TOTP; single-use recovery codes; step-up re-auth for destructive mutations; login challenge discriminated union (`LoginToken \| MfaChallenge`) shared by password and Google OAuth paths; last-factor auto-disable (removing the final factor turns 2FA off automatically); inline-error wave UX on the login challenge TOTP form | [business logic](application/user_2fa/user_2fa.md) \| [tech](application/user_2fa/user_2fa_tech.md) |
| user_roles | Three-value `UserRole` enum (`agent-user`, `agent-developer`, `admin`) layered on top of `is_superuser`. `agent-user` is the default and gets Configuration, Credentials, Environments, and Integrations tabs on install detail pages (Integrations shows only the MCP Connectors card in simplified form); `agent-developer` unlocks agent CRUD, building-mode sessions, publishing, sync-prompts; `admin` is paired with the existing superuser tier. Promote / demote from the Edit User dialog on Admin → Users; `USER_ROLE_CHANGED` WebSocket event re-routes the affected user on demote. | [business logic](application/user_roles/user_roles.md) \| [tech](application/user_roles/user_roles_tech.md) |
| ssh_keys | User SSH key management for private Git repository access | [business logic](application/ssh_keys/ssh_keys.md) \| [tech](application/ssh_keys/ssh_keys_tech.md) |
| knowledge_sources | Admin-only Git-based knowledge sources with article indexing, embeddings, and semantic search. Public sources are automatically available to all users' agents; private sources are owner-only | [business logic](application/knowledge_sources/knowledge_sources.md) \| [tech](application/knowledge_sources/knowledge_sources_tech.md) |
| user_workspaces | Workspace isolation for organizing agents, credentials, sessions by context | [business logic](application/user_workspaces/user_workspaces.md) \| [tech](application/user_workspaces/user_workspaces_tech.md) |
| email_integration | Email-to-agent automation overview, access control, security model | [business logic](application/email_integration/email_integration.md) \| [tech](application/email_integration/email_integration_tech.md) |
| mail_servers | IMAP/SMTP server configuration in Settings > Channels tab, compact card/list UI, credential encryption, connection testing | [business logic](application/email_integration/mail_servers.md) \| [tech](application/email_integration/mail_servers_tech.md) |
| email_sessions | Session modes, processing modes, email threading, outgoing queue, session context | [business logic](application/email_integration/email_sessions.md) \| [tech](application/email_integration/email_sessions_tech.md) |
| a2a_protocol | Agent-to-Agent protocol, JSON-RPC, task-based integration | [business logic](application/a2a_integration/a2a_protocol/a2a_protocol.md) \| [tech](application/a2a_integration/a2a_protocol/a2a_protocol_tech.md) \| [v1 support](application/a2a_integration/a2a_protocol/a2a_v1_support.md) |
| a2a_access_tokens | Scoped JWT tokens for external A2A client authentication | [business logic](application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md) \| [tech](application/a2a_integration/a2a_access_tokens/a2a_access_tokens_tech.md) |
| mcp_integration | Agent exposure as MCP server, OAuth 2.1, connector setup | [architecture](application/mcp_integration/agent_mcp_architecture.md) \| [implementation](application/mcp_integration/agent_mcp_connector.md) \| [setup](application/mcp_integration/mcp_connector_setup.md) |
| app_mcp_server | Universal MCP endpoint that routes messages to the right agent via pattern matching or AI classification; automatically strips routing prefixes ("ask cinna to...") so agents receive clean task messages; any agent owner can add their agent via the agent's Integrations tab (MCP Connectors card); bundle installs auto-create a route from the revision's router trigger prompt (`is_auto_managed=True`); Settings > Channels > MCP Server card shows shared routes with enable/disable toggles; `agent-user` role sees a simplified MCP Connectors card (single auto-route + toggle) on install detail pages | [business logic](application/app_mcp_server/app_mcp_server.md) \| [tech](application/app_mcp_server/app_mcp_server_tech.md) \| [prompt examples](application/app_mcp_server/prompt_examples.md) \| [prompt examples tech](application/app_mcp_server/prompt_examples_tech.md) |
| identity_mcp_server | Person-level abstraction on top of the App MCP Server — users expose themselves as a routable identity; callers address people by name and two-stage routing (person resolution → agent selection) handles the rest; sessions run in the identity owner's space; managed via Settings > Channels > Identity Server card and Agent > Integrations > MCP Connectors | [business logic](application/identity_mcp_server/identity_mcp_server.md) \| [tech](application/identity_mcp_server/identity_mcp_server_tech.md) |
| realtime_events | WebSocket event bus system, frontend-backend-agentenv streaming | [event bus](application/realtime_events/event_bus_system.md) \| [streaming](application/realtime_events/frontend_backend_agentenv_streaming.md) |
| plugin_marketplaces | Admin-managed Git-based plugin catalogs, sync, visibility control | [business logic](application/plugin_marketplaces/plugin_marketplaces.md) \| [tech](application/plugin_marketplaces/plugin_marketplaces_tech.md) |
| admin_agent_environments | Superuser-only console at /admin/agent-envs — lists every AgentEnvironment across the fleet with owner, template, staleness (current vs expected image tag), and in-use flags; supports targeted and bulk rebuild with real-time status updates via WebSocket; writes SecurityEvent audit rows for every admin-triggered rebuild | [business logic](application/admin_agent_environments/admin_agent_environments.md) \| [tech](application/admin_agent_environments/admin_agent_environments_tech.md) |
| agent_activities | Activity feed, event logging, session summaries, sidebar bell indicator | [business logic](application/agent_activities/agent_activities.md) \| [tech](application/agent_activities/agent_activities_tech.md) |
| getting_started | New user and new instance onboarding — API key gate, Getting Started Modal, Rotating Hints | [business logic](application/getting_started/getting_started.md) \| [tech](application/getting_started/getting_started_tech.md) |
| chat_windows | Chat window rendering across session pages, guest shares, webapp widgets, and dashboard prompt actions — markdown, tool blocks, streaming display, auto-scroll | [business logic](application/chat_interface/chat_windows.md) \| [tech](application/chat_interface/chat_windows_tech.md) \| [tool rendering](application/chat_interface/tool_rendering.md) \| [tool tech](application/chat_interface/tool_rendering_tech.md) \| [markdown](application/chat_interface/markdown_rendering.md) \| [auto-scroll](application/chat_interface/auto_scroll_and_streaming_display.md) \| [ask user question](application/chat_interface/tool_answer_questions_widget.md) \| [tool approval](application/chat_interface/tool_approval_widget.md) \| [webapp widget](application/chat_interface/webapp_chat_widget.md) \| [file sending](application/chat_interface/file_sending_and_ui.md) \| [dashboard prompt actions](application/chat_interface/dashboard_prompt_actions.md) \| [dashboard prompt actions tech](application/chat_interface/dashboard_prompt_actions_tech.md) |
| user_dashboards | Customizable grid-based monitoring dashboards — per-user, workspace-independent, agent blocks with webapp/session/tasks views, and hover prompt actions that execute in-place with streaming display, session reuse, and webapp action forwarding | [business logic](application/user_dashboards/user_dashboards.md) \| [tech](application/user_dashboards/user_dashboards_tech.md) |
| general_assistant | **(Prototype Draft)** System-created building-mode agent that helps users set up, configure, and manage agentic workflows by calling the platform API via Python scripts; singleton per user, workspace-agnostic, pre-loaded with platform docs and API reference. Not yet available in the UI | [business logic](application/general_assistant/general_assistant.md) \| [tech](application/general_assistant/general_assistant_tech.md) |
| cinna_cli_integration | Local development CLI — setup tokens, continuous Mutagen-based bidirectional workspace sync over a WebSocket tunnel, foreground `cinna dev` TUI, per-user `~/.cinna/agents.json` registry, remote exec streaming, MCP knowledge proxy, building context with inline `prompt_files` companions; credentials stay on the platform | [business logic](application/cinna_cli_integration/cinna_cli_integration.md) \| [tech](application/cinna_cli_integration/cinna_cli_integration_tech.md) \| [local dev](application/cinna_cli_integration/local_cli_development.md) |
| desktop_auth | Native-client App Authentication — OAuth 2.0 + PKCE flow for Cinna Desktop and Cinna Mobile; instance discovery, authorization code issuance, access + refresh token pair, token rotation with replay detection, per-device revocation from Settings that takes effect immediately on the next request (not just on next refresh). Mobile reuses the same service/tables/logic through a parallel `/app-auth` route surface (discovery via `/.well-known/cinna-app`, consent at `/app-auth/consent`) — only the URL namespace and native redirect schemes differ; the consent screen adapts its copy via the redirect-derived `client_kind` | [business logic](application/desktop_auth/desktop_auth.md) \| [tech](application/desktop_auth/desktop_auth_tech.md) |
| external_agent_access | REST + A2A surface for native clients (Desktop/Mobile) — agent discovery across personal agents, MCP shared routes, and identity contacts; A2A chat with all three target types; session metadata REST layer with thread-list restore; soft-hide; client attribution from JWT claims | [business logic](application/external_agent_access/external_agent_access.md) |
| cinna_mcp_descriptor | `cinna.mcp` descriptor on the external A2A surface — published in `capabilities.extensions[]` on the A2A card (uri `urn:cinna:mcp`) and mirrored as a top-level `mcp` field on each discovery target; lets Cinna Desktop wrap any reachable agent as an emulated MCP tool without a second OAuth flow; shared `send_message` contract in `tool_contracts.py` prevents core/desktop drift; deterministic slug deconfliction across the user's full reachable set; identity contacts carry `mcp=null` | [business logic](application/external_agent_access/cinna_mcp_descriptor.md) \| [tech](application/external_agent_access/cinna_mcp_descriptor_tech.md) |
| system_notifications | Generic platform notification layer — typed catalog, per-user preferences (`user_notification_setting`), in-memory throttle (dedup + rate cap), failure-isolated dispatch; first type: `session_error` email to the agent owner when a session ends with an error; Settings → My profile → Notifications card | [business logic](application/system_notifications/system_notifications.md) \| [tech](application/system_notifications/system_notifications_tech.md) |
| app_sync | Zero-knowledge native-client data sync — opaque E2E-encrypted document store for Cinna Desktop (and future Mobile) personal data (notes, jobs, folders). Delta sync protocol with per-user gap-free seq cursor, LWW conflict resolution on cleartext metadata, tombstones, per-user quotas. Mandatory E2E encryption: UMK hierarchy, commit-then-reveal device pairing (blind server relay, grind-proof SAS, inbox-based auto-discovery), recovery key, optional passphrase. No server payload crypto — ciphertext stored verbatim. Phase 1 MVP: notes + jobs + folders; chats and job runs in Phase 2/3 | [business logic](application/app_sync/app_sync.md) \| [tech](application/app_sync/app_sync_tech.md) |

### knowledge

| Feature | Description | Docs |
|---------|-------------|------|
| knowledge_management | Admin-only Git-based knowledge sources, article indexing, vector search. Public sources auto-available to all users' agents | [business logic](application/knowledge_sources/knowledge_sources.md) \| [tech](application/knowledge_sources/knowledge_sources_tech.md) |

### sharing

> Note: `agent_sharing` (clone-based sharing) has been removed and replaced by the bundle/install model. The `docs/agents/agent_sharing/` directory has been deleted. <!-- nocheck -->


| Feature | Description | Docs |
|---------|-------------|------|
| guest_sharing | Token-based unauthenticated chat access to an agent install — disposable URLs with 4-digit security codes, expiration, lockout protection, optional env-panel access, and grant activation for authenticated users | [business logic](agents/guest_sharing/guest_sharing.md) \| [tech](agents/guest_sharing/guest_sharing_tech.md) |
| workspaces | Workspace isolation, entity separation, multi-workspace support | [business logic](application/user_workspaces/user_workspaces.md) |

### agentic_teams

| Feature | Description | Docs |
|---------|-------------|------|
| agentic_teams | Visual org-chart builder — users define named agentic teams, add agent nodes, and wire directed connections with handover prompts. Owner-only access, workspace-independent, MVP Blueprint phase. Sidebar switcher + Settings card + interactive React Flow chart with edit/view mode, auto-arrange (Dagre), and bulk position persistence. Teams define a `task_prefix` (e.g., "HR") used when generating short-code IDs for team-scoped tasks; directed connections enforce subtask delegation topology. Connection Edit Dialog shows colored agent badges and supports AI-generated handover prompts via a Generate button. | [business logic](agents/agentic_teams/agentic_teams.md) \| [tech](agents/agentic_teams/agentic_teams_tech.md) |

### infrastructure

| Feature | Description | Docs |
|---------|-------------|------|
| nginx_setup | Nginx reverse-proxy location blocks required by the platform — `/api/`, `/mcp/`, `/ws/`, and origin-root `/.well-known/*` URIs (MCP OAuth, Cinna Desktop) — with feature cross-references | [reference](infrastructure/nginx_setup.md) |

### development

| Feature | Description | Docs |
|---------|-------------|------|
| backend_patterns | SQLModel patterns, routes, services, CRUD, migrations | [reference](development/backend/backend_development_llm.md) |
| frontend_patterns | Component patterns, hooks, TanStack conventions | [reference](development/frontend/frontend_development_llm.md) |
| user_selector_pattern | Shared user-picker (`UserAllowlistPicker`) + `GET /users/search` endpoint used by credential sharing, App MCP / identity assignments, and bundle access grants | [reference](development/frontend/user_selector_pattern.md) |
| ai_functions | LLM utility development, multi-provider cascade fallback | [reference](development/backend/ai_functions_development.md) |
| security | Credentials whitelist, encryption at rest, access control | [reference](development/security/security.md) <!-- nocheck --> |

---

## Architecture Overview

```
User ──→ Frontend (React) ──→ Backend API (FastAPI) ──→ Services ──→ PostgreSQL
                                      │
                                      ├──→ Docker Environments ──→ Agent SDK (Claude/OpenAI)
                                      ├──→ WebSocket (Socket.IO) ──→ Real-time Events
                                      ├──→ A2A Protocol ──→ External Agents
                                      ├──→ MCP Server ──→ External LLM Clients
                                      │       └──→ App MCP Server ──→ Stage 1 Router ──→ Stage 2 Identity Router
                                      └──→ Email (IMAP/SMTP) ──→ Email Automation
```

---

*Last updated: 2026-06-04* <!-- agent-api-automatic-credentials -->
