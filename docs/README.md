# Workflow Runner Core

A conversational AI agent platform where users create custom AI agents, run them in isolated Docker environments, and interact through persistent chat sessions. Agents can be shared, scheduled, triggered by email/webhooks, and exposed via A2A and MCP protocols.

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
| **Agent Share** | Clone-based sharing of an agent with another user, including credential requirements |
| **Guest Share** | Token-based limited access to an agent for unauthenticated users |
| **Handover** | Agent-to-agent task delegation within the platform |
| **Workspace** | Isolation boundary for user's agents, sessions, and resources |
| **AI Function** | LLM utility for text generation, classification, extraction with multi-provider cascade fallback |
| **Building Mode** | Agent environment state for configuration and development |
| **Conversation Mode** | Agent environment state for executing tasks and chat |
| **A2A** | Agent-to-Agent protocol for cross-platform agent communication |
| **MCP** | Model Context Protocol - exposes agents as tool servers to external LLM clients |
| **MCP Connector** | Configuration that connects an agent environment to an external MCP server |
| **Agent Webapp** | Lightweight data dashboard (HTML/CSS/JS) served from an agent's workspace via shareable URLs |
| **Webapp Share** | Token-based public access to an agent's webapp for unauthenticated viewers |

---

## Domain Map

| Domain | Description | Features |
|--------|-------------|----------|
| [agents](#agents) | Core agent lifecycle - creation, configuration, environments, sessions, chat, file management | 11 features |
| [tasks](#tasks) | Task submission, refinement, triggers, and scheduling | 3 features |
| [credentials](#credentials) | Credential management, encryption, AI provider keys | 1 feature |
| [application](#application) | User-facing platform features - authentication, integrations, real-time events, workspaces | 17 features |
| [knowledge](#knowledge) | Git-based knowledge sources, vector search, RAG | 1 feature |
| [sharing](#sharing) | Agent sharing, guest access, workspace collaboration | 3 features |
| [agentic_teams](#agentic_teams) | Visual org-chart builder for agent orchestration topology — teams, nodes, connections with AI-generated handover prompts | 1 feature |
| [development](#development) | Backend/frontend patterns, AI functions, debugging | 4 features |

---

## Feature Registry

### agents

| Feature | Description | Docs |
|---------|-------------|------|
| agent_environments | Docker container architecture, build layers, workspace isolation, multi-image templates | [business logic](agents/agent_environments/agent_environments.md) \| [tech](agents/agent_environments/agent_environments_tech.md) \| [multi-image](agents/agent_environments/agent_multi_image_environments.md) \| [credential rebuild](agents/agent_environments/affected_environments_rebuild.md) \| [sdk session persistence](agents/agent_environments/sdk_session_persistence.md) |
| agent_prompts | System prompt construction for building and conversation modes | [business logic](agents/agent_prompts/agent_prompts.md) \| [tech](agents/agent_prompts/agent_prompts_tech.md) |
| agent_commands | Slash commands in agent sessions — `/files`, `/session-recover`, `/session-reset`, `/rebuild-env` — with autocomplete popup UI | [business logic](agents/agent_commands/agent_commands.md) \| [tech](agents/agent_commands/agent_commands_tech.md) \| [files](agents/agent_commands/files_command.md) \| [recovery](agents/agent_commands/session_recovery_command.md) \| [reset](agents/agent_commands/session_reset_command.md) \| [rebuild-env](agents/agent_commands/rebuild_env_command.md) \| [autocomplete](agents/agent_commands/slash_command_autocomplete.md) |
| agent_plugins | Plugin marketplace integration, capability loading | [business logic](agents/agent_plugins/agent_plugins.md) \| [tech](agents/agent_plugins/agent_plugins_tech.md) |
| agent_schedulers | Multi-schedule CRON execution with natural language input and per-schedule prompts | [business logic](agents/agent_schedulers/agent_schedulers.md) \| [tech](agents/agent_schedulers/agent_schedulers_tech.md) |
| agent_handover | Agent-to-agent task delegation and inbox creation | [business logic](agents/agent_handover/agent_handover.md) \| [tech](agents/agent_handover/agent_handover_tech.md) |
| agent_environment_core | Server-side core running inside Docker containers: HTTP API, SDK adapters, prompt generation. Two SDK engines: Claude Code (Anthropic/MiniMax), OpenCode (Anthropic, OpenAI, Google, OpenAI-compatible). Each environment links separate AI credentials per mode (building/conversation) with optional per-mode model overrides. OpenCode uses MCP bridge servers for custom tools. All adapters emit unified lowercase tool names via `tool_name_registry.py`. | [business logic](agents/agent_environment_core/agent_environment_core.md) \| [tech](agents/agent_environment_core/agent_environment_core_tech.md) \| [multi-sdk](agents/agent_environment_core/multi_sdk.md) \| [multi-sdk tech](agents/agent_environment_core/multi_sdk_tech.md) \| [knowledge tool](agents/agent_environment_core/knowledge_tool.md) \| [create agent task tool](agents/agent_environment_core/create_agent_task_tool.md) \| [tools approval](agents/agent_environment_core/tools_approval_management.md) \| [tools approval tech](agents/agent_environment_core/tools_approval_management_tech.md) |
| agent_environment_data_management | Environment data flow, cloning, syncing operations | [business logic](agents/agent_environment_data_management/agent_environment_data_management.md) \| [tech](agents/agent_environment_data_management/agent_environment_data_management_tech.md) |
| agent_credentials | Credential syncing to agent environments, whitelisting, redaction, OAuth refresh | [business logic](agents/agent_credentials/agent_credentials.md) \| [tech](agents/agent_credentials/agent_credentials_tech.md) \| [oauth](agents/agent_credentials/oauth_credentials.md) \| [whitelist](agents/agent_credentials/credentials_whitelist.md) \| [google SA](agents/agent_credentials/google_service_account.md) \| [sharing](agents/agent_credentials/credential_sharing.md) \| [security hardening](agents/agent_credentials/credential_security_hardening.md) \| [security hardening tech](agents/agent_credentials/credential_security_hardening_tech.md) |
| agent_file_management | File upload/download, workspace file viewing, storage quota, garbage collection | [business logic](agents/agent_file_management/agent_file_management.md) \| [tech](agents/agent_file_management/agent_file_management_tech.md) \| [remote db viewer](agents/agent_file_management/remote_database_viewer.md) |
| agent_webapp | Lightweight data dashboards served from agent workspace via shareable URLs, with dynamic Python data endpoints | [business logic](agents/agent_webapp/agent_webapp.md) \| [tech](agents/agent_webapp/agent_webapp_tech.md) \| [chat widget](agents/agent_webapp/webapp_chat.md) \| [chat tech](agents/agent_webapp/webapp_chat_tech.md) \| [chat context](agents/agent_webapp/webapp_chat_context.md) \| [chat context tech](agents/agent_webapp/webapp_chat_context_tech.md) \| [chat actions](agents/agent_webapp/webapp_chat_actions.md) \| [chat actions tech](agents/agent_webapp/webapp_chat_actions_tech.md) \| [actions context](agents/agent_webapp/webapp_actions_context.md) \| [actions context tech](agents/agent_webapp/webapp_actions_context_tech.md) |

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
| agent_sessions | Persistent chat sessions between users/external systems and agent environments — lifecycle, modes, streaming, integration types, UI | [business logic](application/agent_sessions/agent_sessions.md) \| [tech](application/agent_sessions/agent_sessions_tech.md) \| [env panel widget](application/agent_sessions/app_env_panel_widget.md) |
| auth | User authentication - JWT tokens, password login, Google OAuth, domain whitelist, password recovery | [business logic](application/auth/auth.md) \| [tech](application/auth/auth_tech.md) \| [google oauth](application/auth/google_oauth.md) |
| ssh_keys | User SSH key management for private Git repository access | [business logic](application/ssh_keys/ssh_keys.md) \| [tech](application/ssh_keys/ssh_keys_tech.md) |
| knowledge_sources | Admin-only Git-based knowledge sources with article indexing, embeddings, and semantic search. Public sources are automatically available to all users' agents; private sources are owner-only | [business logic](application/knowledge_sources/knowledge_sources.md) \| [tech](application/knowledge_sources/knowledge_sources_tech.md) |
| user_workspaces | Workspace isolation for organizing agents, credentials, sessions by context | [business logic](application/user_workspaces/user_workspaces.md) \| [tech](application/user_workspaces/user_workspaces_tech.md) |
| email_integration | Email-to-agent automation overview, access control, security model | [business logic](application/email_integration/email_integration.md) \| [tech](application/email_integration/email_integration_tech.md) |
| mail_servers | IMAP/SMTP server configuration, credential encryption, connection testing | [business logic](application/email_integration/mail_servers.md) \| [tech](application/email_integration/mail_servers_tech.md) |
| email_sessions | Session modes, processing modes, email threading, outgoing queue, session context | [business logic](application/email_integration/email_sessions.md) \| [tech](application/email_integration/email_sessions_tech.md) |
| a2a_protocol | Agent-to-Agent protocol, JSON-RPC, task-based integration | [business logic](application/a2a_integration/a2a_protocol/a2a_protocol.md) \| [tech](application/a2a_integration/a2a_protocol/a2a_protocol_tech.md) \| [v1 support](application/a2a_integration/a2a_protocol/a2a_v1_support.md) |
| a2a_access_tokens | Scoped JWT tokens for external A2A client authentication | [business logic](application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md) \| [tech](application/a2a_integration/a2a_access_tokens/a2a_access_tokens_tech.md) |
| mcp_integration | Agent exposure as MCP server, OAuth 2.1, connector setup | [architecture](application/mcp_integration/agent_mcp_architecture.md) \| [implementation](application/mcp_integration/agent_mcp_connector.md) \| [setup](application/mcp_integration/mcp_connector_setup.md) |
| realtime_events | WebSocket event bus system, frontend-backend-agentenv streaming | [event bus](application/realtime_events/event_bus_system.md) \| [streaming](application/realtime_events/frontend_backend_agentenv_streaming.md) |
| plugin_marketplaces | Admin-managed Git-based plugin catalogs, sync, visibility control | [business logic](application/plugin_marketplaces/plugin_marketplaces.md) \| [tech](application/plugin_marketplaces/plugin_marketplaces_tech.md) |
| agent_activities | Activity feed, event logging, session summaries, sidebar bell indicator | [business logic](application/agent_activities/agent_activities.md) \| [tech](application/agent_activities/agent_activities_tech.md) |
| getting_started | New user and new instance onboarding — API key gate, Getting Started Modal, Rotating Hints | [business logic](application/getting_started/getting_started.md) \| [tech](application/getting_started/getting_started_tech.md) |
| chat_windows | Chat window rendering across session pages, guest shares, webapp widgets, and dashboard prompt actions — markdown, tool blocks, streaming display, auto-scroll | [business logic](application/chat_interface/chat_windows.md) \| [tech](application/chat_interface/chat_windows_tech.md) \| [tool rendering](application/chat_interface/tool_rendering.md) \| [tool tech](application/chat_interface/tool_rendering_tech.md) \| [markdown](application/chat_interface/markdown_rendering.md) \| [auto-scroll](application/chat_interface/auto_scroll_and_streaming_display.md) \| [ask user question](application/chat_interface/tool_answer_questions_widget.md) \| [tool approval](application/chat_interface/tool_approval_widget.md) \| [webapp widget](application/chat_interface/webapp_chat_widget.md) \| [file sending](application/chat_interface/file_sending_and_ui.md) \| [dashboard prompt actions](application/chat_interface/dashboard_prompt_actions.md) \| [dashboard prompt actions tech](application/chat_interface/dashboard_prompt_actions_tech.md) |
| user_dashboards | Customizable grid-based monitoring dashboards — per-user, workspace-independent, agent blocks with webapp/session/tasks views, and hover prompt actions that execute in-place with streaming display, session reuse, and webapp action forwarding | [business logic](application/user_dashboards/user_dashboards.md) \| [tech](application/user_dashboards/user_dashboards_tech.md) |
| general_assistant | System-created building-mode agent that helps users set up, configure, and manage agentic workflows by calling the platform API via Python scripts; singleton per user, workspace-agnostic, pre-loaded with platform docs and API reference | [business logic](application/general_assistant/general_assistant.md) \| [tech](application/general_assistant/general_assistant_tech.md) |

### knowledge

| Feature | Description | Docs |
|---------|-------------|------|
| knowledge_management | Admin-only Git-based knowledge sources, article indexing, vector search. Public sources auto-available to all users' agents | [business logic](application/knowledge_sources/knowledge_sources.md) \| [tech](application/knowledge_sources/knowledge_sources_tech.md) |

### sharing

| Feature | Description | Docs |
|---------|-------------|------|
| agent_sharing | Clone-based agent sharing, credential requirements, push updates, guest access | [business logic](agents/agent_sharing/agent_sharing.md) \| [tech](agents/agent_sharing/agent_sharing_tech.md) \| [accept wizard](agents/agent_sharing/accept_share_wizard_widget.md) \| [guest sharing](agents/agent_sharing/guest_sharing.md) \| [guest tech](agents/agent_sharing/guest_sharing_tech.md) |
| workspaces | Workspace isolation, entity separation, multi-workspace support | [business logic](application/user_workspaces/user_workspaces.md) |

### agentic_teams

| Feature | Description | Docs |
|---------|-------------|------|
| agentic_teams | Visual org-chart builder — users define named agentic teams, add agent nodes, and wire directed connections with handover prompts. Owner-only access, workspace-independent, MVP Blueprint phase. Sidebar switcher + Settings card + interactive React Flow chart with edit/view mode, auto-arrange (Dagre), and bulk position persistence. Teams define a `task_prefix` (e.g., "HR") used when generating short-code IDs for team-scoped tasks; directed connections enforce subtask delegation topology. Connection Edit Dialog shows colored agent badges and supports AI-generated handover prompts via a Generate button. | [business logic](agentic_teams/agentic_teams/agentic_teams.md) \| [tech](agentic_teams/agentic_teams/agentic_teams_tech.md) |

### development

| Feature | Description | Docs |
|---------|-------------|------|
| backend_patterns | SQLModel patterns, routes, services, CRUD, migrations | [reference](development/backend/backend_development_llm.md) |
| frontend_patterns | Component patterns, hooks, TanStack conventions | [reference](development/frontend/frontend_development_llm.md) |
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
                                      └──→ Email (IMAP/SMTP) ──→ Email Automation
```

---

*Last updated: 2026-04-02*
