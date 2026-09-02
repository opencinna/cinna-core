# Local Agent Kit

## Purpose

Lets someone with **no Cinna account** and any local coding assistant (Claude
Code, Codex, opencode, …) start building AI agents on their own machine today,
using conventions that are byte-compatible with a Cinna cloud agent workspace —
so the agent moves to the cloud later without being rewritten.

The user pastes one prompt into their assistant:

```
read https://<instance>/agent-start and help me start making my agents
```

The assistant fetches a versioned, platform-maintained *kit* from the public
`/agent-start` surface: how to lay out a local folder tree, how to build an agent
whose layout and metadata mirror a cloud agent, a *capability ladder* that adds
only the artefacts a task actually needs, and a *go-cloud* playbook that turns
one of the local folders into a real Cinna account workspace and imports an
agent into it with one command.

## Core Concepts

- **The kit** — A tree of markdown guides, a JSON index, a manifest schema, scaffold
  templates, and a stdlib-only Python helper (`kit.py`), authored under
  `docs/local_agent_kit/` and served rendered (instance placeholders filled in)
  at the public `/agent-start` surface. See [tech](local_agent_kit_tech.md) for the
  routes, rendering and versioning.
- **Capability ladder** — The kit's core teaching device. An agent starts with
  nothing but a prompt and grows one rung at a time, only when a rung's trigger
  fires (prompts → scripts/data → credentials → schedules → status reporting →
  CLI commands → knowledge/local skills → multi-agent → go cloud). The assistant
  re-walks the ladder after every substantive change and is told, explicitly, to
  add nothing whose trigger hasn't fired — a hard anti-over-engineering rule, not
  a preference.
- **The three roles** — the assistant switches between *Orchestrator* (root
  folder; creates/lists/coordinates agents), *Builder* (inside one agent's
  folder; writes its scripts, prompts, config, manifest), and *Agent* (inside
  the same folder; acts as the agent by following its own workflow prompt). The
  kit tells the assistant to announce which hat it is wearing.
- **Cloud-mirroring layout** — A locally scaffolded agent has the exact same
  top-level folders a cloud agent workspace has: `docs/` (prompts,
  `CLI_COMMANDS.yaml`), `scripts/`, `knowledge/`, `files/`, `config/`,
  `credentials/` (local `.env`, never copied to the cloud), and
  `app-data/{storage,cache,uploads}/`. Nothing about the layout is
  kit-specific — it is the same convention [agent_prompts](../../agents/agent_prompts/agent_prompts.md)
  and [agent_bundles](../../agents/agent_bundles/agent_bundles.md) already use.
- **`cinna-agent.json`** — The manifest at an agent folder's root. It carries the
  same definitional metadata a bundle revision carries: `name`, `slug`,
  `description`, `example_prompts`, `router_trigger_prompt`, paths to the three
  prompt documents, `status_refresh_command`, declared `credentials[]` (slot
  name + platform credential type + `.env` field names — never a secret value),
  `schedules[]`, `handovers[]`, a `features` block, and a `cloud` block written
  only by the import step. Validated against
  `docs/local_agent_kit/schema/cinna-agent.schema.json`.
- **`kit.py`** — The stdlib-only helper the assistant runs through `uv run` (setup installs `uv` when missing; it provisions Python 3.10+ so the macOS system Python is never a blocker)
  (`uv run .cinna-kit/tools/kit.py <command>`, no install step): `new` scaffolds
  an agent from the template, `validate` checks it is coherent and (with
  `--cloud-ready`) import-ready, `list` tables every local agent and its ladder
  rungs, `refresh` compares and updates the kit itself, `export` produces the
  exact tree a cloud import pushes.
- **Go-cloud** — The migration playbook (`guides/11-go-cloud.md`). From here on
  an account is required: `cinna login <host> --dir Cloud` turns a folder into a
  real [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md),
  then `cinna agent import Local/<slug>` (cinna-cli, separate repo) creates the
  agent, writes its prompts/metadata, syncs and pushes its workspace, drafts its
  credentials, creates its schedules, and stamps the manifest's `cloud` block. A
  manual fallback using only long-standing CLI verbs covers an older `cinna-cli`
  that lacks `agent import`.
- **Instance toggle** — `ServerConfig.local_agent_kit_enabled` (default on).
  Turning it off makes every path under both `/agent-start` and `/api/agent-start` return
  **404** (not 403) on that instance, everywhere.

## User Flows

### Assistant — first contact, no account

1. User pastes `read https://<instance>/agent-start and help me start making my
   agents` into their coding assistant.
2. Assistant fetches `GET /agent-start` (markdown, since it isn't a browser) and reads
   `START.md`: who it is now, one-time setup (choose a root folder, default
   `~/Documents/MyAgents`; create `Local/` and `Cloud/`; download the kit
   tarball into `.cinna-kit/`; install `AGENTS.md` / `CLAUDE.md` / `.gitignore`
   at the root without ever overwriting an existing one), the three roles, the
   non-negotiables (never print a secret, keep the layout cloud-compatible, run
   the ladder check after every change), and where to go when the user wants the
   cloud.
3. Assistant reads `.cinna-kit/README.md` — the document index and the
   capability ladder — then follows `guides/01-first-agent.md`: interview the
   user, scaffold with `kit.py new <slug>`, build one capability at a time,
   test as the agent, validate.
4. No login, signup, or platform API call happens anywhere in this flow — the
   assistant is only ever reading static files and running a local Python
   script.

### Visitor — plain browser

1. Someone opens `https://<instance>/agent-start` directly in a browser.
2. Content negotiation serves a self-contained HTML landing page instead of raw
   markdown: headline, the copy-able starter prompt, a link to the raw markdown
   (`?format=md`), and the same instructions rendered in a `<pre>` block so
   nothing is lost to a browser-only reader.
3. If the instance disabled the kit, the proxy either 404s (if a `/agent-start` block
   exists) or falls through to the SPA shell — either way there is nothing to
   read and no login link points here (see below).

### Logged-in user — discovering the kit inside the app

1. **Getting Started** — a "Build agents locally with your coding assistant"
   article (`local-first`) sits in the Getting Started Modal, after "How to
   Build An Agent": the three-step flow (paste prompt → build in `Local/` → say
   "move it to the cloud"), the same copy-able prompt block, the resulting
   folder diagram, and a note that no account is needed until the cloud step.
   Cross-links to "How to Build An Agent" (the in-app equivalent) and
   "Conversation vs Building".
2. **Rotating Hints** — one hint ("Already use Claude Code or Codex? …") is
   appended to the shuffled hint pool and opens the same article.
3. **Settings → Security → Local Development card** — a collapsed line under
   the existing cloud-workspace Setup section: "Starting from scratch on a new
   machine? Paste into your coding assistant", expanding to the same copy
   button. Placed here because the Setup command above it bootstraps a *cloud*
   workspace and needs the account the visitor might not have yet.
4. **Login page** — a small muted link under the form, "Building agents locally
   with Claude Code or Codex? Start here", pointing at `/agent-start?format=html` in a
   new tab.

Every one of these four surfaces is conditional on the instance actually
publishing the kit — see **Business Rules** below.

### Admin — instance control

1. Superuser opens **Admin → Server Configuration → Interface** and finds the
   **"Public local-agent starter (`/agent-start`)"** card next to the Disclaimer card.
2. The card shows the instance's `/agent-start` URL (with a copy button) and a
   switch. Flipping it off immediately makes the whole surface 404, on both
   `/agent-start` and `/api/agent-start`, for every caller — including the four in-app
   surfaces above, which stop showing themselves within one query's staleness
   window (an infinite `staleTime` probe, invalidated on the same save that
   flips the switch).
3. No content is configurable per instance beyond this one switch — the kit's
   text is fixed platform content, rendered with this instance's own URLs.

### Going to the cloud

1. From inside `Local/<slug>`, the user tells the assistant to move the agent
   to the cloud (or the assistant recognizes the need itself — 24/7 runs,
   channels, sharing, a webapp).
2. The assistant follows `guides/11-go-cloud.md`: checks `uv`, `cinna-cli`
   (installs/upgrades if needed), an account (signs the user up if needed —
   email confirmation and the `agent-developer` role may gate agent creation),
   runs `cinna login <host> --dir Cloud` (device-flow browser approval), and
   runs `kit.py validate Local/<slug>` with the go-cloud gate (`--cloud-ready`)
   — a real description, at least one example prompt, a non-empty workflow
   prompt, no tracked secrets.
3. `cd Cloud && cinna agent import ../Local/<slug>` (cinna-cli, separate
   repo) creates the agent, writes its prompts and metadata, syncs and pushes
   its workspace (honouring the kit's exclude list — `credentials/` is never
   copied), creates credential drafts and schedules, sets the status refresh
   command, and stamps the local manifest's `cloud` block. It prints one setup
   URL per credential so the user fills secrets in the browser — the CLI never
   sees them.
4. The user verifies with `cinna chat --agent <slug> "<first example prompt>"`
   and decides, out loud with the assistant, which copy — local or cloud — is
   now authoritative. See
   [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)
   for everything that happens on the platform side of this step.

## Business Rules

- **Unauthenticated, read-only, static.** The whole `/agent-start` surface serves
  only rendered snapshot content, identical for every caller. No user, agent,
  credential, or session data is ever read or returned; the only database read
  on the entire surface is the instance's `local_agent_kit_enabled` flag.
- **Opt-out, not opt-in.** `local_agent_kit_enabled` defaults to `true` at both
  the model and the migration's server default, so every existing and new
  instance publishes the kit until an admin turns it off.
- **Disabled means 404, never 403.** An instance that opted out must not
  confirm the feature exists here at all — every path on both mounts 404s.
- **Every in-app pointer is conditional on the public probe, not on a
  privileged read.** `GET /admin/server-config` (which carries the real flag)
  is superuser-only, and the login page has no session at all, so all four
  human-facing surfaces (Getting Started article, Rotating Hints entry, Local
  Development card hint, login-page link) gate on a plain unauthenticated fetch
  of the kit's own `/api/agent-start/version` endpoint instead — the same request an
  assistant makes, cached for the session (infinite `staleTime`, silent on
  failure). A hint pointing at a URL that 404s is worse than no hint.
- **`credentials/` never travels.** Neither `kit.py export` nor
  `cinna agent import` ever copies the local `credentials/` folder or any
  `.env` file — secrets stay on the user's machine; only setup URLs cross the
  wire.
- **Anti-over-engineering is an explicit rule, not a suggestion.** The kit's own
  text tells the assistant never to add a capability-ladder rung whose trigger
  has not fired, and `kit.py validate` only ever advises (warnings) until the
  go-cloud gate (`--cloud-ready`) promotes readiness gaps to hard errors.
- **No account, no platform write, until the go-cloud step.** Everything before
  `guides/11-go-cloud.md` — scaffolding, building, testing, ladder growth — runs
  entirely on the user's machine with a stdlib-only Python tool. The first
  platform-authenticated action of the whole flow is `cinna login`.
- **The kit's own content is the shipped product, not repo documentation.**
  `docs/local_agent_kit/` is authored like code: it is synced into the backend
  image's knowledge template (see [tech](local_agent_kit_tech.md#kit-content-sync))
  and served byte-for-byte (after placeholder rendering); the platform's own
  documentation reference checker deliberately does not resolve its internal
  paths, since they describe the *user's* machine (`~/Documents/MyAgents`,
  `Local/`, `Cloud/`, `.cinna-kit/`), not this repository's tree.

## Integration Points

- **[Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)** —
  `Cloud/` *is* an account workspace once `cinna login --dir Cloud` runs;
  `cinna agent import` reuses the same account-CLI create/sync/credential-draft/
  schedule/status verbs documented there. The account workspace's context
  package also carries a rendered copy of the kit under `context/local-kit/`, so
  a cloud orchestrator agent knows the local conventions when a user asks it to
  import one.
- **[Agent Prompts](../../agents/agent_prompts/agent_prompts.md)** — a locally
  scaffolded agent's `docs/WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, and <!-- nocheck -->
  `REFINER_PROMPT.md` are the *same three files* the cloud reconciles into
  `Agent.workflow_prompt` etc. once imported.
- **[Agent Bundles](../../agents/agent_bundles/agent_bundles.md)** —
  `cinna-agent.json`'s `credentials[]` / `schedules[]` / `prompts` blocks mirror
  the definitional metadata a bundle revision carries, so a local agent has
  exactly the shape a publish would snapshot.
- **[Getting Started](../getting_started/getting_started.md)** — new article
  (`local-first`) and a Rotating Hints entry, both gated on the public probe.
- **[Server Configuration](../server_configuration/disclaimer.md)** — the
  instance toggle lives on the same Interface tab, next to the Disclaimer card,
  and shares its `ServerConfig` singleton row and `["serverConfig"]` query key.
- **[Cinna CLI Integration](../cinna_cli_integration/cinna_cli_integration.md)** —
  the go-cloud playbook's preconditions cover installing/upgrading `cinna-cli`
  itself (`CINNA_CLI_INSTALL_SPEC`, `MINIMUM_CLI_VERSION`).
- **[Nginx Setup](../../infrastructure/nginx_setup.md)** — `/agent-start` needs its own
  origin-root reverse-proxy block (like the `.well-known/*` routes); the
  `/api/agent-start` alias is the fallback that already works through the universal
  `/api/` block on every deployment.
- **cinna-cli (separate repo)** — `cinna agent import` and the go-cloud manual
  fallback are implemented in the `cinna-cli` repository
  (`src/cinna/local_import.py`), not in this backend. This platform's only
  server-side involvement in the go-cloud step is the pre-existing account-CLI
  endpoints the import command calls.

---

*Last updated: 2026-09-02*
