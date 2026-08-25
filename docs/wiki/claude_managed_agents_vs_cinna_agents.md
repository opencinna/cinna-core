# Claude Managed Agents vs. Cinna Agents

Anthropic's **Claude Managed Agents** (beta, April 2026) is a hosted agent-as-a-service API: you call Anthropic's endpoints, they run Claude inside a cloud container, and you get results back via server-sent events. **Cinna** is a self-hosted agent platform where you own the infrastructure, the data, and the full agent lifecycle — with a complete web UI for managing everything.

Both platforms share the same three-layer vocabulary — **Agent, Environment, Session** — but they target fundamentally different ownership models and use cases. This article compares them across the dimensions that matter when choosing where to build.

---

## The Core Difference

| | Claude Managed Agents | Cinna Agents |
|---|---|---|
| **Mental model** | API building block — embed Claude into your product | Operational platform — deploy an AI workforce for your team |
| **Who runs it** | Anthropic hosts and scales everything | You host, you own, you control |
| **Who uses it** | Developers via Console UI, SDK, and CLI. End-user-facing UI is yours to build | End users working through a full-stack web UI (and developers extending via API) |
| **Container lifecycle** | Ephemeral — fresh container per session, no file carry-over | Persistent — workspace files, databases, and scripts survive across sessions and rebuilds |
| **LLM provider** | Claude only (Anthropic models) | Multi-provider: Claude, OpenAI, Google, Bedrock, Azure, Ollama, and 75+ via OpenCode engine |

---

## Point-by-Point Comparison

| Capability | Claude Managed Agents | Cinna Agents |
|---|---|---|
| **Runtime environment** | Anthropic-managed cloud containers. Configure packages (`pip`, `npm`, `apt`, `cargo`, `gem`, `go`) and networking (`unrestricted` or domain-restricted). Each session gets a fresh, isolated container instance | Self-managed Docker containers with two-layer architecture (immutable core + persistent workspace). Multiple image templates. Full control over OS, packages, and system tools. Blue-green deployment for safe rollbacks |
| **Persistence** | Ephemeral. Files exist only during a session. Output files retrievable via Files API. Cross-session state requires Memory Stores (research preview) | Persistent workspace per agent. Files, databases, scripts, and credentials survive across all sessions, restarts, and rebuilds. Docker volumes preserve everything |
| **Credential management** | Not built-in. You manage secrets in your application and inject them via environment config or custom tools | Encrypted vault with per-agent whitelisting — each agent sees only the credentials it needs. OAuth auto-refresh, credential sharing between users, redaction in logs, automatic environment rebuild on key rotation |
| **Memory** | Memory Stores (research preview): workspace-scoped document collections that agents auto-read/write across sessions. Up to 8 stores per session, 100KB per memory, versioned with audit trail and redaction | Persistent workspace files carry state implicitly. Knowledge sources provide searchable reference material via RAG. No explicit cross-session memory abstraction — the filesystem is the memory |
| **Knowledge / RAG** | Not built-in. Agent can web-search or you can pre-load files into the container | Git-based knowledge sources with article indexing, vector search, and auto-sync from repositories. Public and private sources available to agents without external infrastructure |
| **Scheduling & triggers** | Not built-in. You create sessions from your own scheduler, webhook handler, or CRON service | Built-in CRON schedules (natural language input), email triggers, webhook triggers, date-based triggers — all configured in the UI, no external tooling required |
| **Email automation** | Not built-in. Connecting email requires building a custom pipeline outside the platform | Native IMAP/SMTP integration. Agents receive, process, and reply to emails autonomously. Per-agent mail server configuration, sender access rules, and session isolation modes |
| **Multi-agent coordination** | Coordinator pattern: one orchestrator declares callable agents. Sub-agents share the filesystem but have isolated conversation contexts. One level of delegation only | Handover pattern: agents delegate via tasks to independent Docker containers. Unlimited delegation depth. Visual org-chart builder (Agentic Teams) with directed connections, team-scoped task routing, and AI-generated handover prompts |
| **MCP integration (consume)** | Yes — declare MCP servers in agent config, agent connects at runtime | Yes — MCP connectors configured per agent environment |
| **MCP integration (expose)** | No — agents cannot be called as MCP servers by external clients | Yes — agents exposed as MCP tool servers. App MCP Server provides universal routing with AI classification. Identity Routing adds person-level addressing |
| **A2A protocol** | No native support | Yes — agents exposed via A2A protocol with auto-extracted skills, scoped JWT tokens, and JSON-RPC interface. Any external agent can call Cinna agents over standard HTTP |
| **Outcome evaluation** | Research preview: define a rubric, agent iterates up to N times, a separate grader evaluates against criteria. Built-in quality loop with structured feedback | Agents use `update_session_state` to declare completion, needs-input, or error. Result state syncs with input tasks. Quality review is manual or driven by handover chains |
| **Custom tools** | Client-executed tools with JSON schema. Agent emits structured requests, your code runs the operation, result flows back | Plugins (marketplace), MCP bridge servers, custom scripts in the workspace, and tools from any connected MCP server |
| **Task management** | Not built-in — you model tasks as sessions or build your own abstraction | Structured input tasks with AI refinement, status tracking, comments, attachments, short-code IDs (TASK-1, HR-42), subtask delegation, and automated triggers |
| **Data dashboards** | Not built-in | Agent Webapp: agents build and serve HTML/CSS/JS dashboards from their workspace. Shareable via public URLs with optional chat widgets and Python data endpoints |
| **Sharing & collaboration** | Organization-scoped via API keys. No sharing model between users | Clone-based agent sharing with credential requirement mapping. Guest access via shareable tokens. Push updates from parent to clones. Team workspaces for isolation |
| **Monitoring** | Event stream per session. Session status polling | Activity feeds, session summaries, real-time WebSocket events, security event logging, customizable user dashboards with per-agent monitoring blocks |
| **User interface** | Anthropic Console UI for managing agents, environments, sessions, and memory stores. Plus API/SDK (Python, TypeScript, Go, Java, C#, Ruby, PHP) and CLI tool (`ant`) for programmatic access. End-user-facing UI is yours to build | Full-stack React web application: agent management, session chat, task boards, file browser, credential vault, team builder, settings, dashboards — ready to use out of the box |
| **Security model** | API key auth. Container networking controls. Data on Anthropic's cloud | JWT + Google OAuth. Docker container isolation. Credential whitelisting. Security event logging. Self-hosted — data stays in your infrastructure |
| **Cost model** | Pay-per-use Anthropic pricing (tokens + container compute). Built-in prompt caching and compaction for efficiency | Self-hosted: you pay for your own compute and bring your own LLM API keys. Multi-provider cascade fallback for cost optimization. No per-agent platform fees |

---

## What This Means in Practice

### Claude Managed Agents works best when...

- You are **building a product** that embeds AI agent capabilities — a coding assistant, a data analysis tool, a document processor — and want to focus on product logic, not infrastructure
- Your workloads are **task-oriented and discrete** — run a session, get a result, move on. Ephemeral containers are a feature, not a limitation
- You want **zero-ops**: no Docker hosts, no databases, no container orchestration to maintain
- You need **outcome-driven automation** with built-in rubric-based quality evaluation loops
- You are already in the **Anthropic ecosystem** and want the tightest integration with Claude models

### Cinna agents are the better fit when...

- **Your workflows span hours, days, or weeks** — an agent processing incoming support emails, updating a CRM, and generating weekly reports cannot live in an ephemeral container. It needs persistence, scheduling, and credentials that outlive any single session
- **You need real automation, not assisted API calls** — agents that receive emails, run on CRON schedules, serve dashboards, and delegate to other agents without anyone opening a chat window or writing client code
- **You need multi-provider flexibility** — switching between Claude, OpenAI, Google, or self-hosted models without rewriting agent logic or changing platforms
- **You manage sensitive credentials** that must stay in your infrastructure with per-agent access control, audit trails, and encrypted storage
- **Your agents need to be callable by external systems** — via A2A protocol or MCP, so other platforms and LLM clients can interact with your agents over standard protocols
- **Your team needs a complete platform** — not just an API, but a web UI for building, monitoring, sharing, and collaborating on AI workflows without writing code

---

## Summary

Claude Managed Agents is a powerful engine for developers embedding autonomous Claude into their products. Cinna is the operational platform where a team's AI workforce lives, works, and collaborates.

One is a building block you code against. The other is a workplace you log into.
