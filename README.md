# Cinna Core

**Open-source platform for building, running, and orchestrating AI agents.**

Create custom AI agents with their own prompts, credentials, and tools — each running in an isolated Docker environment. Chat with them, schedule them, connect them to email, expose them via APIs, or wire them into multi-agent teams.

## Why Cinna

Cinna gives you the full operational layer on top (not just SDK):

- **Isolated runtime per agent** — each agent runs in its own Docker container with a persistent workspace, so agents can create files, install packages, and maintain state across sessions
- **Two-mode architecture** — build your agent interactively (building mode with full context), then switch to a lightweight conversation mode for production use at lower cost
- **Multi-SDK support** — use Claude (Anthropic), OpenAI, Google, or any OpenAI-compatible provider. Switch between them without changing your agent
- **No vendor lock-in** — self-hosted, GPL-3.0 licensed, runs anywhere Docker runs

## Key Features

### Agent Management
- Create agents with custom system prompts, credentials, and SDK configuration
- Multiple environment templates (general, Python-advanced, custom Dockerfiles)
- Blue-green deployments — switch between environments instantly for rollback
- Plugin marketplace for extending agent capabilities

### Chat & Sessions
- Persistent chat sessions with streaming responses
- File upload/download within agent workspace
- Slash commands (`/files`, `/rebuild-env`, `/session-reset`)
- Markdown rendering with tool call visualization

### Automation & Triggers
- CRON-based scheduling with natural language input
- Email-to-agent automation (IMAP/SMTP integration)
- Webhook triggers for external system integration
- Agent-to-agent handover for task delegation

### Interoperability
- **A2A Protocol** — expose agents as Agent-to-Agent services for cross-platform communication
- **MCP Server** — make any agent available as a tool server for external LLM clients (Claude Desktop, Cursor, etc.)
- **MCP Connectors** — connect agents to external MCP servers for additional tools

### Knowledge & RAG
- Git-based knowledge sources with automatic article indexing
- Vector embeddings and semantic search
- Public/private knowledge scoping

### Collaboration
- Clone-based agent sharing between users
- Guest access via shareable links (no account required)
- Agent webapps — lightweight dashboards served from agent workspace
- Customizable monitoring dashboards with per-agent blocks

### Agentic Teams
- Visual org-chart builder for multi-agent orchestration
- Directed connections with handover prompts
- Team-scoped task management with short-code IDs

### Developer Experience
- CLI tool ([cinna-cli](https://github.com/opencinna/cinna-cli)) for local agent development
- Workspace sync, Docker build context, credential pull
- MCP knowledge proxy for AI coding tools

## Architecture

```
User ──> Frontend (React) ──> Backend API (FastAPI) ──> Services ──> PostgreSQL
                                      |
                                      |──> Docker Environments ──> Agent SDK (Claude/OpenAI/Google)
                                      |──> WebSocket (Socket.IO) ──> Real-time Events
                                      |──> A2A Protocol ──> External Agents
                                      |──> MCP Server ──> External LLM Clients
                                      +──> Email (IMAP/SMTP) ──> Email Automation
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI (Python), PostgreSQL, SQLModel ORM, Alembic migrations |
| Frontend | React, TypeScript, TanStack Router & Query, Tailwind CSS, shadcn/ui |
| Agent Runtime | Claude SDK, OpenAI SDK, Google ADK, OpenAI-compatible providers |
| Isolation | Docker containers with mounted volumes |
| Real-time | Socket.IO (WebSocket) |
| Auth | JWT tokens, Google OAuth, bcrypt |

## Quick Start

```bash
git clone https://github.com/opencinna/cinna-core.git
cd cinna-core
make install
```

The install wizard will walk you through configuring admin credentials, database settings, and security keys. It copies `.env.example`, applies your values, builds Docker images, runs migrations, and seeds the admin user — everything needed to go from clone to running instance.

Open http://localhost:5173 and log in with the admin credentials you chose during setup.

See [DEVELOPMENT.md](DEVELOPMENT.md) for the full development setup, workflow, and Makefile reference.

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd like to change.

## License

[GPL-3.0](LICENSE)
