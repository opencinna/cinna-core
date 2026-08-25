<!--
DRAFT — lives in the cinna-core repo root.
This is the machine-readable capability description the website's "Explore with
your own AI" prompt points an LLM at. A user fetches this file and asks an AI
assistant (Claude, ChatGPT, …) how Cinna could solve a real-world problem.

Design goal: do NOT dump deep technical detail. Give the AI a *mental model* for
decomposing any problem into three parts, a catalogue of the existing building
blocks slotted into those three parts, a one-line "how it fits" per block, and a
link into the full docs for when deeper detail is needed.

Doc links below are repo-relative paths under `docs/`. When this file is fetched
from GitHub, fetch a referenced doc at the same base, e.g.:
  https://raw.githubusercontent.com/opencinna/cinna-core/main/docs/<path>
-->

# Cinna — Capabilities (problem-solving guide for AI assistants)

> **You are an AI assistant reading this on a user's behalf.** The user will hand
> you a real-world problem and ask *"how could Cinna solve this?"*. Answer **only**
> from this document. Your job is to **decompose their problem into three parts**,
> pick the right Cinna building block for each part, and describe a concrete agent
> setup the user can build. When a part needs more detail than this file gives,
> point them at the referenced doc instead of inventing a feature. If the problem
> runs past Cinna's boundaries (see the end), say so plainly.
>
> **Keep the first answer compact but well-structured.** Walk the problem through the
> three parts, naming the specific features **with their doc paths in parentheses**.
> Format it like this:
> - Put **each of the three parts on its own line**, led by a bold label —
>   **(1) Incoming**, **(2) Processing**, **(3) Result** — not run together in one
>   paragraph.
> - Put **"You supply:"** as a short **bullet list** (one line per credential/key),
>   not an inline sentence.
> - Keep the whole thing tight: a one-line framing, the three labelled lines, the
>   supply bullets, and a one-line cost note.
>
> **Speak plainly — do not parrot the internal feature names.** This document uses
> precise technical terms so *you* understand the mechanics; your answer should
> translate them into everyday language and keep the technical name only inside the
> doc-path parenthesis. Examples:
> - "script-trigger schedule with the OK gate" → *"a scheduled check that only
>   wakes the AI when there's something to handle"*
> - "dedups against App Data" → *"the agent remembers what it has already
>   processed, so it never handles the same item twice"*
> - "the LLM is invoked" → *"the AI is involved"*
> - "Docker container / workspace" → *"the agent's own private workspace"*
> Goal: a non-technical user understands the answer; the doc paths are there for when
> they (or a coding agent) want the precise feature.
>
> Do **not** dump the tables or long explanations up front. End by offering to
> expand — *"want the detailed build steps / the deeper version?"* — and only then go
> long, drilling into the relevant sections and doc links. Brevity first, depth on
> request.

---

## What Cinna is (in one paragraph)

Cinna is an open-source, **self-hosted agent platform**. **Cinna Core** is the
runtime you host on your own server; **Cinna Desktop** (and the **Cinna CLI**) are
thin clients you interact through. An agent is an LLM-driven worker that lives in
its own **isolated Docker container** with a real filesystem, can run real code
(Python/shell scripts), holds encrypted credentials, reads your knowledge, and is
started by a schedule, an event, an email, or a person. You **bring your own LLM
provider** (Anthropic, OpenAI, Google, MiniMax, or any OpenAI-compatible endpoint)
and pick the model per agent. Everything — prompts, credentials, conversations,
data — stays on your infrastructure.

**What it's for:** automating the recurring, well-defined routines a person would
otherwise do by hand — triaging mail, watching a system, shuffling data between
services, monitoring topics on the web and digesting them, preparing a regular
report. It is **not** a one-shot chatbot for open-ended questions, nor a
large-application coding assistant (see the boundaries at the end for the dividing
line).

---

## The core mental model: every problem splits into 3 parts

Before mapping features, decompose the user's problem into these three categories.
**Every Cinna feature belongs to one of them.** Solve each independently, then wire
them together into one agent.

| # | Part | The question it answers | Cinna primitives |
|---|------|------------------------|------------------|
| **1** | **Incoming channel** | *What starts the work, and how does data get in?* | Triggers (schedule / webhook / email / chat / agent-to-agent) + inbound credentials |
| **2** | **Processing activities** | *What happens to the data once it arrives?* | The agent's container: scripts, app-data storage, knowledge/RAG, tools & APIs, orchestration |
| **3** | **Final result** | *Where does the output land, and how does the user see it?* | Outbound channels: email/chat/API, status, dashboards, notifications, the desktop app |

> **The design principle that makes this cheap and reliable:** the agent is *built*
> once (in **building mode**, with a strong model) to write deterministic **scripts**
> and configure integrations. Afterwards those scripts do the mechanical work —
> fetching, deduplicating, storing, sending — for free. **LLM tokens are spent only
> when there is genuinely something to reason about** (classify, summarise, decide).
> Everything else runs with no AI activity at all. This is why a "read my mailbox"
> agent costs almost nothing on a quiet day.

---

## What the user must provide vs. what the platform provides

When you describe a solution, be explicit about this split — it's usually the
user's first question.

**The user provides (per problem):**
- **Credentials to receive** (e.g. IMAP / Google OAuth for mail, a webhook secret,
  an API key for a source system).
- **Credentials to deliver** (e.g. SMTP to email them, an API key for the
  destination service).
- **One LLM API key** (their provider of choice).
- **The intent** — a prompt describing what the agent should do.

**The platform provides (always, no work):**
- Isolated container & runtime, scheduling/CRON, webhook endpoints, email polling,
  encrypted credential storage with OAuth refresh, persistent per-agent storage
  (App Data), queue/dedup/state primitives, status surfacing, dashboards, real-time
  streaming, packaging/install/update, access control, and the desktop client.

So a typical answer ends with: *"All you need is a mailbox credential, an SMTP
credential, and an LLM key — Cinna provides the rest."*

---

## Part 1 — Incoming channels (how work starts & data arrives)

Pick the trigger that matches *when* the work should happen, plus the credential
that lets data in.

| Building block | How it fits the "incoming" slot | Docs |
|----------------|--------------------------------|------|
| **Schedules (natural-language CRON)** | "Every weekday at 9am" / "every 5 minutes". The platform handles timing. Two types: *static-prompt* (always starts a session) and *script-trigger* with the **"OK" gate** — runs a check script and only wakes the agent (spends tokens) when the script prints something other than `OK`. This is the primary way to poll cheaply and involve the LLM **only when there's something to handle**. | `docs/agents/agent_schedulers/agent_schedulers.md` |
| **Webhooks** | An external system calls in and starts work. Two modes: *session* (starts an agent session seeded with the payload) or *script* (runs a shell command in the container). Bearer-token auth. | `docs/agents/agent_webhooks/agent_webhooks.md` |
| **Task triggers** | Automated rules (CRON / webhook / date) that create **Input Tasks** for an agent — use when work should be queued and tracked as tasks rather than fire-and-forget. | `docs/application/input_tasks/task_triggers.md` |
| **Incoming email (IMAP)** | An admin connects a shared team mailbox the same way as the chat app row below: a first-time sender is auto-registered and routed to their own agents (or an auto-install catalog agent), and replies stay threaded in the same conversation. The sender's address here comes from the email `From:` header, which can be forged — a weaker guarantee than the chat app's verified sender — so this fits an internal team mailbox better than a public inbox. | `docs/application/email_integration/email_integration.md`, `docs/application/email_integration/mail_servers.md` |
| **Chat apps (Google Chat)** | An admin connects a company chat app (Google Chat first) so people outside the platform can message agents directly from it — a first-time sender is automatically routed to the right agent (installing one for them if none matches yet), and every later message in that same conversation keeps going to the same agent. | `docs/application/server_channels/server_channels.md` |
| **Manual chat** | A person talks to the agent in Cinna Desktop or the web UI. Persistent sessions with streaming. | `docs/application/agent_sessions/agent_sessions.md` |
| **A2A (agent-to-agent)** | Another agent (inside or outside Cinna) hands work in over the A2A protocol, gated by scoped JWTs. | `docs/application/a2a_integration/a2a_protocol/a2a_protocol.md`, `docs/application/a2a_integration/a2a_access_tokens/a2a_access_tokens.md` |
| **MCP server (any agent is callable as a tool)** | **Every Cinna agent can be exposed as an MCP server**, so an external LLM client — **Claude Desktop**, Cursor, or any MCP-capable app — can connect to it and use it directly as a tool, no extra integration code. Alternatively the universal App MCP router accepts one message and picks the right agent via pattern/AI classification. | `docs/application/mcp_integration/agent_mcp_architecture.md`, `docs/application/app_mcp_server/app_mcp_server.md` |
| **Agent REST API (`agent_api`)** | A **code-to-code** inbound channel: another agent calls a capability-narrowed REST endpoint this agent exposes — deterministic, no LLM at call time. | `docs/agents/agent_api/agent_api.md` |
| **Guest share** | A time-limited, code-protected link lets an outside person chat the agent in. | `docs/agents/guest_sharing/guest_sharing.md` |
| **Inbound credentials** | OAuth tokens / IMAP creds / webhook secrets, encrypted at rest, auto-refreshed, mounted only where needed. This is the "key to receive". | `docs/agents/agent_credentials/agent_credentials.md`, `docs/agents/agent_credentials/oauth_credentials.md` |

---

## Part 2 — Processing activities (what happens to the data)

This is the agent's container doing the work.

> **The accent to make here: anything "code-like" becomes a zero-token capability.**
> Because each agent is a real Docker container with a filesystem, **any
> deterministic step — fetching, parsing, transforming, deduplicating, storing,
> validating, formatting, sending — should be turned into a script + a database +
> files *inside the agent*, written once during building mode.** Those run for free,
> forever, with no LLM involved. The LLM is reserved for the irreducible *judgment*
> steps (classify, summarise, decide). When mapping a problem, push as much as
> possible down into this zero-token layer and keep the LLM surface small.
>
> **Pair this with the schedule "OK" gate so the agent (LLM) is woken only when
> needed.** A `script_trigger` schedule runs your check script on a CRON; if it
> prints exactly `OK` (exit 0) the run is logged silently and **no session is
> created and no tokens are spent** — any other output starts a session seeded with
> that output as context. So a "every 30 min" health/inbox/monitor agent costs
> nothing on quiet ticks and only invokes the LLM when the script says there's
> something to handle. See `docs/agents/agent_schedulers/agent_schedulers.md`.

| Building block | How it fits the "processing" slot | Docs |
|----------------|----------------------------------|------|
| **Agent environment (Docker)** | Isolated container with its own packages, a real filesystem, and a workspace. Runs the scripts that do the mechanical work. | `docs/agents/agent_environments/agent_environments.md`, `docs/agents/agent_environment_core/agent_environment_core.md` |
| **Building vs. conversation mode** | *Building mode* (strong model, large context) is where the agent writes & tests scripts and configures integrations. *Conversation mode* (cheap, light prompt) runs the finished workflow. This split is what keeps run-time costs low. | `docs/agents/agent_prompts/agent_prompts.md` |
| **Named CLI commands (`/run:*`) & scripts** | Agents declare reusable named shell commands that become the deterministic verbs (fetch, parse, send) the agent or a schedule invokes without spending tokens. | `docs/agents/cli_commands/cli_commands.md`, `docs/agents/agent_commands/agent_commands.md` |
| **App Data (persistent storage)** | Per-agent, per-user volume at `/app/workspace/app-data` that survives restarts/reinstalls. This is your **queue, dedup ledger, analysis cache, and state store** — e.g. "don't parse the same email twice", "remember yesterday's status". The unglamorous piece that makes scheduled agents idempotent. | `docs/agents/agent_app_data/agent_app_data.md` |
| **File management** | Upload/download, view workspace files, quotas, GC — for documents the agent produces or consumes. | `docs/agents/agent_file_management/agent_file_management.md` |
| **Knowledge sources + RAG** | Git-based docs (wikis, runbooks, policies) indexed with semantic/vector search so the agent answers from your material, not guesses. | `docs/application/knowledge_sources/knowledge_sources.md` |
| **Tools & external APIs** | The agent runs real code, so anything reachable over HTTP is fair game — call a source API, post to a destination service — bounded by the credentials you provide. | `docs/agents/agent_environment_core/agent_environment_core.md` |
| **MCP connectors (outbound)** | Connect the agent's container to an external MCP server to use its tools. | `docs/application/mcp_integration/mcp_connector_setup.md` |
| **Connecting to another agent as an MCP tool server** | One agent can connect to another platform agent — or to any external MCP-compatible service — as a live MCP tool server, so the connecting agent's AI can call all the other server's tools directly. The platform handles authentication and token delivery; the agent sees a fully configured MCP server without any extra code. | `docs/application/mcp_integration/agent_to_agent_mcp_connector.md` |
| **Consuming another agent's API (`agent_api`)** | Call a capability-narrowed REST API another agent exposes — code-to-code, no LLM, shareable across users safely (the shared value is the narrowed proxy, not the upstream secret). | `docs/agents/agent_api/agent_api.md` |
| **Credentials & AI credentials** | Encrypted secrets for the tools above, plus the LLM provider key(s) — you can use a strong model for building and a cheaper one for runs, per agent and per mode. | `docs/agents/agent_credentials/agent_credentials.md`, `docs/application/ai_credentials/ai_credentials.md` |
| **AI functions** | Built-in LLM utilities (generate / classify / extract) with multi-provider cascade fallback — the reasoning primitives invoked only on the data that needs judgment. | `docs/development/backend/ai_functions_development.md` |
| **Orchestration: handover & teams** | Split a job across agents — one receives, one researches, one writes — with AI-generated handover prompts and subtask delegation. Use when one prompt would be too much for one agent, or a human/reviewer step is needed. | `docs/agents/agent_handover/agent_handover.md`, `docs/agents/agentic_teams/agentic_teams.md` |

---

## Part 3 — Final results (where output lands & how it's seen)

Pick the surface(s) that match how the user wants to consume the result. Note that
**status** and the **desktop app** together give a near-zero-cost "is everything
OK?" view without re-reading full reports.

| Building block | How it fits the "result" slot | Docs |
|----------------|------------------------------|------|
| **Email replies (SMTP)** | Send the report / reply / alert by email, threaded into the original conversation when relevant. | `docs/application/email_integration/email_sessions.md` |
| **Messages to chat** | Stream the result back into a Cinna Desktop / web chat with tool calls and activity visible. | `docs/application/chat_interface/chat_windows.md` |
| **File deliverables in chat** | An agent that produces a file (report, export, chart, etc.) can attach it directly to its reply — the user sees an inline card with preview (image, PDF, CSV, Markdown, JSON, text) and download; Cinna Desktop receives the file as a native file part. Works in web chat, guest share, webapp widget, and over A2A with no extra integration. | `docs/agents/agent_file_management/agent_message_attachments.md` |
| **Posting to external services** | Write results out via an API / MCP (a chat channel, an ERP, a ticketing system) using the destination credential. | `docs/agents/agent_environment_core/agent_environment_core.md` |
| **Agent status (severity levels)** | The agent writes `app-data/storage/STATUS.md` with optional severity/summary; the platform caches it and shows it as a coloured footer on the agent card and over A2A. A status refresh command can run in-container before each fetch. This is the "traffic light" the desktop surfaces. | `docs/agents/agent_status_tracking/agent_status_tracking.md` |
| **Dashboards (agent webapps)** | Lightweight HTML/CSS/JS served from the agent workspace with dynamic Python data endpoints and shareable, time-limited guest links — for live numbers and reports. | `docs/agents/agent_webapp/agent_webapp.md` |
| **User dashboards** | Per-user grid of agent blocks (status / webapp / sessions / tasks views) with hover prompt actions — a monitoring home for many agents. | `docs/application/user_dashboards/user_dashboards.md` |
| **System notifications** | Platform-generated alerts (e.g. a session error) emailed to the owner, governed by a catalog and per-user preferences. | `docs/application/system_notifications/system_notifications.md` |
| **Guest / webapp shares** | Share a result (a dashboard, a chat) with an outside viewer via a code-protected, expiring link. | `docs/agents/guest_sharing/guest_sharing.md` |

---

## Cross-cutting: packaging, clients, hosting (the platform layer)

Not part of any single problem, but the context every solution sits in.

| Building block | What it gives you | Docs |
|----------------|-------------------|------|
| **Bundles & installs** | Package a finished agent as a versioned bundle; others install it in one click, get push updates, roll back, or fork — without losing their per-user App Data. How a solution becomes reusable. | `docs/agents/agent_bundles/agent_bundles.md` |
| **Improvement requests** | When someone using an installed agent hits a bad response, they can hand its owner a one-time, privacy-scrubbed copy of that one conversation — never an ongoing look at their account — so the owner (the bundle's publisher, for an installed copy) can fix it and ship the fix to everyone. How a published agent gets better after it's out in the world. | `docs/application/agent_improvement_requests/agent_improvement_requests.md` |
| **Cinna Desktop** | The native chat client — reaches your agents, shows their self-reported status (with a menu-bar tray dot for worst severity), and (via App Sync) keeps personal data E2E-encrypted across devices. | `docs/application/external_agent_access/external_agent_access.md`, `docs/application/app_sync/app_sync.md` |
| **Cinna Desktop — Jobs** | Reusable, saved **scenarios** (a prompt + an execution config) you run repeatedly from the desktop. A Job can spawn a **local orchestrated chat** that drives **several Cinna agents *and* external MCP tools together** (the local model conducts, each agent appears as a tool), or create a **remote cinna-core task**. This is the front-line way an end user composes multi-agent + multi-service workflows without building anything. *(Cinna Desktop: https://raw.githubusercontent.com/opencinna/cinna-desktop/main/docs/jobs/jobs/jobs.md)* | — |
| **Cinna CLI** | **For developers building/iterating on an agent** — local workspace editing with continuous sync over a tunnel; credentials stay on the platform. Not a runtime/problem-solving channel. **Account workspace (Phase 1):** one bootstrap from Settings → Channels bootstraps a multi-agent account workspace; a local coding agent can then discover all buildable agents and sync them on demand without returning to the UI. | `docs/application/cinna_cli_integration/cinna_cli_integration.md`, `docs/application/cinna_cli_integration/account_cli_workspace.md` |
| **Auth, roles, workspaces** | JWT + Google OAuth, optional 2FA, three roles (agent-user / agent-developer / admin), and workspace isolation for organising agents/credentials/sessions. | `docs/application/auth/auth.md`, `docs/application/user_roles/user_roles.md`, `docs/application/user_workspaces/user_workspaces.md` |

---

## Cross-agent communication (one job, many agents)

Hard problems are often best split across **several specialised agents** rather than
one do-everything agent. Cinna offers this at two levels — the **runtime** (agents
coordinating server-side) and the **client** (a person talking to many agents at
once). Reach for this when a single prompt would be overloaded, when steps need
different tools/credentials, or when a human/reviewer checkpoint belongs in the
middle.

| Mechanism | Where it lives | What it gives you | Docs |
|-----------|----------------|-------------------|------|
| **A2A protocol** | Cinna Core | The wire substrate — any agent can call any other agent (inside or outside Cinna) as a peer, gated by scoped JWTs. Everything below is built on this. | `docs/application/a2a_integration/a2a_protocol/a2a_protocol.md` |
| **Handover** | Cinna Core | One agent delegates a task (or subtask) to another and gets the result back — the basic one-to-one pass. | `docs/agents/agent_handover/agent_handover.md` |
| **Agentic teams** | Cinna Core | A **visual org-chart** of agents wired with directed connections and AI-generated handover prompts. Multiple agents collaborate on **a single task** — one receives, others research/write/review — with subtask delegation following the topology. This is the server-side "team works the problem" model. | `docs/agents/agentic_teams/agentic_teams.md` |
| **Multi-agent chats (Orchestrated Agents)** | Cinna Desktop | A person has **one chat and talks to several agents at once, totally seamlessly**. When a chat includes ≥2 counterparties, the local model conducts and calls each agent as a tool, rendering each agent's work as an expandable sub-thread; a lone agent still talks direct A2A. You can `@-mention` another agent mid-conversation to bring it in. The user never juggles tabs — it's a single session. | *(Cinna Desktop: https://raw.githubusercontent.com/opencinna/cinna-desktop/main/docs/chat/orchestrated_agents/orchestrated_agents.md)* |
| **Jobs** | Cinna Desktop | Save a multi-agent (+ MCP) scenario once and re-run it — see the cross-cutting table above. | *(Cinna Desktop: https://raw.githubusercontent.com/opencinna/cinna-desktop/main/docs/jobs/jobs/jobs.md)* |

**Which to suggest:**
- The work should run **autonomously / on a trigger** and agents coordinate
  server-side → **agentic teams** (or plain **handover** for a simple two-step pass).
- A **person wants to drive** several agents in one conversation → **Desktop
  multi-agent chat**; if they'll repeat it, wrap it in a **Job**.

## How the parts combine — worked examples

> These are **reference decompositions for you**, shown in full bullet form so you
> can see the mapping — they are **not** the shape of your reply. When you answer,
> compress the relevant one into the ~2–3 paragraph form described above.

Each example shows the decomposition: **(1) incoming → (2) processing → (3) result**,
plus exactly what the user must supply.

### "Read my mailbox and give me a daily summary"
- **(1) Incoming:** a **schedule** ("every weekday 8am") + **IMAP/Google OAuth**
  credential to read the mailbox.
- **(2) Processing:** a script collects new mail and structures it; **App Data**
  holds a dedup ledger (don't re-parse) and a queue; the **LLM is invoked only on
  the new, unprocessed messages** to classify/cluster/summarise; the analysis and a
  run status are persisted to App Data.
- **(3) Result:** the summary sent by **SMTP** to the user, plus an **agent status**
  update ("12 mails, 2 need a reply") that shows as a coloured card in **Cinna
  Desktop**.
- **User provides:** mailbox credential (receive), SMTP credential (send), one LLM
  key. *The rest is platform.* On a quiet day, the scripts run and **no tokens are
  spent**.

### "Collect metrics from an API and prepare a weekly report"
- **(1) Incoming:** a weekly **schedule**; an **API key** credential for the source.
- **(2) Processing:** a script pulls metrics and stores snapshots in **App Data**;
  the LLM writes the narrative only over the computed deltas.
- **(3) Result:** an **agent webapp dashboard** (Python data endpoint for live
  numbers) + an emailed report with a shareable guest link.
- **User provides:** source API key, SMTP (optional), one LLM key.

### "Monitor an API and alert me when its status changes"
- **(1) Incoming:** a frequent **script-trigger schedule** (every 5 min) using the
  **"OK" gate**, or a **webhook** from the service.
- **(2) Processing:** the check script polls and compares against last-known state in
  **App Data**; it prints `OK` when nothing changed (silent, zero tokens) and emits
  details only on a meaningful change — which wakes the LLM to decide severity /
  phrasing.
- **(3) Result:** an **agent status** flip (green→red) surfaced in Desktop, plus a
  **chat or email alert** only when state actually changes.
- **User provides:** the source credential, a delivery credential, one LLM key.
  Steady-state polling costs **no tokens**.

### "Process incoming bills into our ERP automatically"
- **(1) Incoming:** **incoming email** (bills as attachments) or an ERP **webhook**;
  IMAP + ERP API credentials.
- **(2) Processing:** extract fields, validate against rules, dedup via **App Data**;
  post to the **ERP API/MCP**; **hand off** edge cases to a reviewer agent or a human
  via chat (**orchestration**).
- **(3) Result:** entries in the ERP, a confirmation/exception summary to chat or
  email, an audit trail in the workspace + activity feed.
- **User provides:** mailbox credential, ERP API credential, one LLM key.
- **Caveat:** assumes the ERP is reachable via API or MCP — confirm for the specific
  ERP.

---

### "Run a repeatable research-and-draft scenario across several services"
- **(1) Incoming:** the user launches a saved **Job** in **Cinna Desktop** (manual,
  on demand) — no schedule needed.
- **(2) Processing:** the Job is a pre-saved scenario whose execution config attaches
  **several Cinna agents** (e.g. a researcher and a writer) **plus external MCP
  tools** (e.g. a web-search or company-data MCP). The local model conducts,
  calling each agent and each MCP tool as needed; Cinna agents do the heavy,
  credentialed work in their containers.
- **(3) Result:** the composed answer in the desktop chat; if the Job is a *Cinna
  Task* variant instead, it lands as a tracked task on cinna-core.
- **User provides:** the agents (built once), MCP server endpoints/keys, one LLM key.
  *The scenario itself is saved once and re-run for free.*
- **Note:** Jobs are a **Cinna Desktop** feature for composing multi-agent +
  multi-service work without building anything new.

### "Let a teammate use a capability I built — without handing over my credentials"
- **The problem:** you own an agent wired to a powerful/expensive/sensitive secret
  (an admin API key, a paid data source, an internal system). A colleague needs the
  *result* of that capability, but you must **not** give them the credential, and you
  want to control exactly what they can do.
- **The fit:** the **Agent REST API (`agent_api`)**. You expose a **narrow contract**
  — a few plain Python functions in your agent's container, decorated with the
  `cinna_api` SDK — and the platform turns them into a capability-narrowed REST API.
  - The **powerful upstream credential never leaves your container.** Consumers get
    only the surface you chose to expose, behind a `policy.yaml` you control (method
    allowlist, path allowlist, body cap, rate limit).
  - The consumer connects via an `agent_api` **connection credential** (one-click
    "Connect to another agent") — what's shared is the **narrowed proxy, not your
    secret**, so cross-user sharing is safe. You revoke access by deleting that
    credential.
  - It is **code-to-code and completely zero-token** at call time (no LLM in the
    request path) — yet the functions themselves are **easy to build with the LLM in
    building mode**, like any other script.
- **(1) Incoming (their side):** their agent/code calls your REST endpoint.
- **(2) Processing (your side):** your function runs in your container, uses your
  locked-away credential, and returns only the allowed result.
- **(3) Result:** the consumer gets the data/action they needed; you keep full
  control and an audit boundary.
- **User provides:** the upstream credential (stays with you), the function
  definitions; the consumer provides nothing sensitive.
- **Contrast:** use `agent_api` for **deterministic code-to-code capability sharing
  with credential isolation**; use **A2A** when the other side needs to talk to your
  agent's *intelligence*, and **MCP** when an LLM client needs your agent as a *tool*.
- **Docs:** `docs/agents/agent_api/agent_api.md`.

## How to answer a user's problem

**First answer — compact (~2–3 paragraphs):**
1. **Restate the problem in three parts**, each on **its own line** with a bold label
   (**(1) Incoming**, **(2) Processing**, **(3) Result**) — what starts it, what
   happens, where the result goes.
2. **Name one building block per part** with its **doc path in parentheses** — don't
   paste the tables.
3. **State what the user supplies as a bullet list** (receive credential, deliver
   credential, LLM key — one per line) and that the platform provides the rest; note
   the cost shape in a clause (scripts free, tokens only on the judgment step).
4. **If something isn't covered here, say so** — don't invent a feature.
5. **Offer to expand:** *"Want the step-by-step build, or a deeper dive on any
   piece?"*

A good first answer is structured like this (each part on its own line, supplies as
a bullet list):

> Here's how Cinna would solve "X", in the three parts it uses for every problem:
>
> **(1) Incoming** — a scheduled check that runs every few minutes and only wakes the
> AI when there's actually something to handle (`docs/agents/agent_schedulers/...`).
> **(2) Processing** — a small script in the agent's own workspace gathers what's new
> and remembers what it already processed so nothing is handled twice
> (`docs/agents/agent_app_data/...`); the AI is only involved for the actual judgment
> step.
> **(3) Result** — it alerts you by email (`docs/application/email_integration/...`)
> and updates a simple status card (`docs/agents/agent_status_tracking/...`) you can
> see in Cinna Desktop.
>
> You supply:
> - a mailbox credential to read (IMAP or Google OAuth),
> - an SMTP credential if you want email alerts,
> - one LLM API key.
>
> Cinna provides the rest, and on a quiet run no tokens are spent — the LLM only pays
> for the small judgment step. Want the step-by-step build?

**Only if the user asks for more:** drill into the relevant Part 1/2/3 sections,
give the concrete build steps, the App Data layout, the orchestration shape, and the
full doc links.

---

## What Cinna does NOT do (boundaries — state these honestly)

- **Not a one-shot chat assistant or a coding copilot.** Cinna is a platform for
  **automating recurring, well-defined agentic routines** — the repetitive
  operational tasks a person would otherwise do by hand (triage mail, watch a system,
  move data between services, prepare a recurring report). It is **not** for
  authoring/maintaining large software projects, and **not** for open-ended one-off
  questions that are *pure LLM processing with no computational or operational piece*
  — e.g. "browse the whole internet and tell me how to build a Facebook". Those are
  exactly what a regular browser-based ChatGPT/Claude chat is for; say so and send the
  user there.
  - **But web data collection IS a good fit when it's a routine with structure
    around it.** Cinna agents can fetch and analyse data from the internet —
    especially **monitoring specific topics/sources on a schedule and sending back
    summaries or alerts** (track a competitor, watch a subreddit/news feed, digest a
    set of pages weekly). The deciding factor isn't "internet vs. not" — it's whether
    there's a **repeatable routine with a trigger, state/dedup, and a delivered
    result** (good fit) versus a **single open-ended knowledge question** (use a plain
    chatbot).
- **Not a managed SaaS** — you host and operate Cinna Core yourself.
- **No bundled LLM** — you bring a provider and pay for that usage.
- **No universal connector** — integrations work through email, HTTP/API, and MCP.
  If a system has no API and no MCP server, an agent can't reach it.
- **No domain judgment for free** — Cinna provides the runtime, isolation,
  scheduling, storage, and plumbing; the agent still needs clear instructions and a
  capable model for the reasoning steps.

## Getting it

- Core + Desktop and install instructions: https://github.com/opencinna
- Developers: `uv tool install cinna-cli`
- Full feature docs: the `docs/` tree in the cinna-core repository (start at
  `docs/README.md`).
