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

- **The kit** — A tree of markdown guides, a JSON index, a folder model, a
  manifest schema, scaffold templates, and a stdlib-only Python helper
  (`kit.py`), authored under `docs/local_agent_kit/` and served rendered
  (instance placeholders filled in) at the public `/agent-start` surface. See
  [tech](local_agent_kit_tech.md) for the routes, rendering and versioning.
- **The contract, and the guides** — The kit has two halves with two audiences,
  and only one of them is a promise to anybody.
  - The **contract** is the machine-readable half: `kit.json`, `layout.json`,
    `CONTRACT_VERSION`, `CHANGELOG.md`, `schema/**` and `templates/**`. It is
    what a *second host* — Cinna Desktop — needs in order to create, validate
    and export agent folders that `kit.py` accepts unchanged, and the other way
    round. It is published on its own as `GET /agent-start/contract.tar.gz`
    (rooted at `cinna-contract/`) with `GET /agent-start/contract/version`
    beside it, so a host that wants none of the prose can pull and pin just
    this.
  - The **guides and tools** — `START.md`, `README.md`, `guides/`,
    `assistants/`, `tools/kit.py` — are prose for a human or a coding
    assistant. They ship in the full kit only, and nothing outside this
    repository depends on their shape.
  - **The contract is a declared member subset of the one tree, never a second
    tree.** There is no `contract/` directory in this repository and no copy to
    keep in step: the same rendered snapshot is packed twice, once whole and
    once filtered. Two trees would be two truths, and the drift would surface
    only when a desktop scaffold and a `kit.py new` scaffold stopped matching —
    which is exactly the failure the contract exists to prevent.
- **Three version numbers, three questions.** They are easy to confuse and each
  answers something the others cannot.
  - `kit_version` — a content hash over the rendered tree, moving on any
    guide, template or tool edit. Answers *"is my copy of the kit current?"*,
    and it is what `kit.py refresh` polls and what every ETag is keyed on. It
    gates nothing.
  - `contract_version` — a hand-maintained semantic version (`1.0.0` today),
    carried in three places that must agree: the `CONTRACT_VERSION` file,
    `kit.json` and `layout.json`. Every manifest records the one it was
    scaffolded with. Answers *"may this tool operate this folder?"*
  - `schema_version` — the manifest's pre-1.0.0 legacy marker. It is still
    parsed, and **nothing branches on its value**; a manifest carrying it with
    neither `contract_version` nor `id` is read, warned about and asked to be
    re-stamped, never rejected. It answers nothing any more and survives only so
    those folders keep parsing. It is *not* in the `/agent-start/version`
    response or in `kit.json` — both dropped it, so a second number that decides
    nothing is not published on a wire anyone polls.
- **The compatibility gate is the major version, and only the major version.**
  `kit.py validate` compares the folder's `contract_version` against the kit's:
  the same major runs silently whatever the minor (minor releases are additive
  by definition), a **newer** folder major is an error telling the user to
  `kit.py refresh`, an **older** folder major is a warning saying the folder can
  be migrated — read `CHANGELOG.md`'s Breaking entries — and an unparseable or
  absent one is reported as unknown. The two directions are not one symmetric
  "mismatch" because the remedies differ. Cinna Desktop implements the same
  four-way verdict from the same table in `CHANGELOG.md`.
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
- **`cinna-agent.json`** — The manifest at an agent folder's root, and the one
  file every host agrees on. Identity first: a stable `id` (UUID v4, written
  once at scaffold and never rewritten, so a folder move or a slug rename does
  not detach the agent from its chats and publications), the
  `contract_version` it was built against, and `created_at`. Then the same
  definitional metadata a bundle revision carries: `name`, `slug`,
  `description`, `example_prompts`, `router_trigger_prompt`, paths to the three
  prompt documents, `status_refresh_command`, declared `credentials[]` (slot
  name + platform credential type + `.env` field names — never a secret value),
  `schedules[]`, `handovers[]`, a `features` block, a `runtime` block whose
  `credential` is always a *reference* and never a value, and a deprecated
  `cloud` block from before the ledger moved out. Validated against
  `docs/local_agent_kit/schema/cinna-agent.schema.json`.
- **`publications.json`** — Where the agent has been published, one entry per
  Cinna instance (`platform_url`, `agent_id`, `workspace`, `imported_at`,
  `updated_at`, `contract_version`, `content_hash`), in a **sibling file at the
  agent root, never inside the manifest**. Two reasons, and the first is a hard
  one: every host hashes `cinna-agent.json`, so a `content_hash` of the exported
  tree stored *in* the manifest would be a value inside the file it is a hash of
  — writing it changes the bytes it describes, and the folder would read
  "unpublished changes" the instant a publish succeeded, forever. The second is
  that a mutable per-instance sync ledger was never identity, and a manifest's
  job is metadata that travels unchanged. The ledger is excluded from cloud
  import, so it never travels. The legacy `cloud` block migrates into it at the
  next manifest *write*, never at export.
- **`app-data/desktop.json` — desktop-owned, two frozen keys.** When Cinna
  Desktop manages a workshop it writes this file into each agent folder. The
  contract freezes exactly two keys of it — `api_base_url` (the desktop's
  loopback API, re-written on every start because the port is random) and
  `agent_token` (a bearer token scoped to that one agent, absent or empty when
  the agent's Connected toggle is off) — plus an optional `chat_path` that
  defaults to `/chat`. Everything else in the file is the desktop's to shape.
  A tool reads those keys and no others; it never writes the file, never commits
  it, and never prints its contents — the URL included, since an error message
  that echoes the object it read is the cheapest way to leak the token beside
  it. Either frozen key missing or empty, or the file absent, means "not
  connected".
- **`kit.py`** — The stdlib-only helper the assistant runs through `uv run` (setup installs `uv` when missing; it provisions Python 3.10+ so the macOS system Python is never a blocker)
  (`uv run .cinna-kit/tools/kit.py <command>`, no install step): `new` scaffolds
  an agent from the template, `validate` checks it is coherent and (with
  `--cloud-ready`) import-ready, `list` tables every local agent with its ladder
  rungs and whether the desktop has it connected, `refresh` compares and updates
  the kit itself, `export` produces the exact tree a cloud import pushes (and
  with `--hash`, that tree's `content_hash`), and `chat` sends one prompt to an
  agent running under Cinna Desktop and prints its answer. The tool reads the
  folder model from `layout.json` rather than knowing it: which directories the
  workshop has, which paths inside an agent are the manifest / the ledger / the
  prompt files / the command catalog / the status file, which files never
  travel, which are secret, and which the desktop owns.
- **`Cloud/<host>/` — one workspace per instance.** A cinna-cli account
  workspace is per Cinna instance, because an account on two instances is two
  accounts: `Cloud/api.example.com/`, `Cloud/other.example.io/`, each with its
  own `.cinna/account.json` and `agents/`. `kit.py list` enumerates all of them,
  and still reads the older flat layout (`Cloud/.cinna/account.json`) where it
  exists — a workshop that silently stopped being listed would look like data
  loss to the person whose workshop it is.
- **Go-cloud** — The migration playbook (`guides/11-go-cloud.md`). From here on
  an account is required: `cinna login <host> --dir Cloud/<host>` turns that
  folder into a real
  [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md),
  then `cinna agent import ../../Local/<slug>` (cinna-cli, separate repo) creates the
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
   `~/Documents/CinnaAgents`; create `Local/` and `Cloud/`; download the kit
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
   runs `cinna login <host> --dir Cloud/<host>` (device-flow browser approval),
   and runs `kit.py validate Local/<slug>` with the go-cloud gate
   (`--cloud-ready`)
   — a real description, at least one example prompt, a non-empty workflow
   prompt, no tracked secrets.
3. `cd Cloud/<host> && cinna agent import ../../Local/<slug>` (cinna-cli,
   separate repo) creates the agent, writes its prompts and metadata, syncs and pushes
   its workspace (honouring the kit's exclude list — `credentials/` is never
   copied), creates credential drafts and schedules, sets the status refresh
   command, and stamps the local publication record. (cinna-cli still writes
   the deprecated manifest `cloud` block today; moving it to `publications.json`
   is a cinna-cli follow-up in that repo, not a change this platform makes.) It
   prints one setup
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
- **`credentials/` never travels — the whole directory, and this is a
  deliberate divergence from the desktop's finer list.** Cinna Desktop's
  authored exclude list is per-file inside `credentials/`, which would let the
  directory's `README.md` and `.env.example` ride to the cloud. Ours does not,
  and the reason is measurable rather than cautious: on the platform side
  `AgentEnvService.update_credentials`
  (`backend/app/env-templates/app_core_base/core/server/agent_env_service.py`)
  **overwrites `credentials/README.md` on every credential sync**, and
  `PromptGenerator._load_credentials_readme`
  (`.../core/server/prompt_generator.py`) reads that file straight into the
  agent's system prompt — so a travelling kit README would inject
  local-machine `.env` instructions (`cp credentials/.env.example
  credentials/.env`) into a *cloud* agent's prompt until the first credential
  sync silently replaced it. `credentials/` is in `BUNDLE_EXCLUDED_TOPLEVEL`
  (`backend/app/services/environments/workspace_classification.py`) as well, so
  the two files would not have reached a published bundle anyway. Every other
  refinement in the desktop's list was adopted. Beyond the list, neither
  `kit.py export` nor `cinna agent import` copies any `.env` file — secrets
  stay on the user's machine; only setup URLs cross the wire.
- **A rule two hosts must both apply lives in `layout.json` as data, not in
  each host's code.** Folder names, the exclude list and its matching
  semantics, the dotless-to-dotted scaffold renames, the dotenv secret rule,
  the desktop-owned file — all declared once and read by both. A rule
  re-implemented from prose on each side diverges, and the divergence is
  invisible until it isn't: the two hosts hash the file set the exclude list
  selects, so a single file's difference makes the hashes disagree forever
  while every individual step still looks like it worked.
- **When a rule cannot be evaluated, the safe direction is a property of the
  consequence — not a house style.** A `layout.json` this build cannot read
  fails one way for secrets and the opposite way for hashes, on purpose. An
  unevaluable *secret* rule withholds the **file**, because a leaked credential
  is unrecoverable. An unevaluable *exclude list* withholds the
  **`content_hash`** — the export still runs on the built-in fallback and says
  so — because a plausible wrong drift number is untraceable where a missing
  one is merely visible. A `desktop_owned` block that is present and
  unreadable makes `kit.py chat` refuse outright rather than guess which file
  holds the bearer token. A host that copies one of these directions instead of
  deriving it will get the next case backwards.
- **Both contract representations degrade together; the kit surface does not
  degrade with them.** A snapshot missing `kit.json`, `layout.json` or
  `CONTRACT_VERSION`, or whose three `contract_version` declarations disagree,
  makes `/agent-start/contract.tar.gz` and `/agent-start/contract/version` both
  **503**. `START.md`, `/version`, `kit.tar.gz` and `/kit/{path}` keep serving:
  the anonymous kit surface is designed to work for a stranger whatever state
  the contract is in. Two representations of one thing reporting different
  health is the asymmetry the guard exists to remove — an endpoint a host
  *polls*, publishing an unvalidated number a compatibility gate keys on, is
  worse than one that refuses, because a false compatibility fails silently
  where a refusal at least stops. A thin `schema/` or `templates/` is a
  *content* problem and stays a 200; it must not be dressed up as an identity
  failure.
- **`kit.py chat` never falls back to role-play.** Every failure — desktop not
  connected, connection refused, 401, any non-2xx — exits non-zero with one
  line and prints nothing that could be mistaken for the agent's answer. A
  tester who cannot tell whether they were talking to the agent or to an
  assistant imitating it has learned nothing, which is the entire reason the
  verb exists.
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
  paths, since they describe the *user's* machine (`~/Documents/CinnaAgents`,
  `Local/`, `Cloud/`, `.cinna-kit/`), not this repository's tree.

## Integration Points

- **[Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md)** —
  `Cloud/<host>/` *is* an account workspace once `cinna login --dir Cloud/<host>`
  runs — one per Cinna instance, since an account on two instances is two
  accounts;
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
- **Cinna Desktop (separate repo, external consumer)** — the contract's second
  host. It pulls `/agent-start/contract.tar.gz`, detects a contract tree by
  `kit.json` + `layout.json` at the root, pins `contract_version` through the
  same major-version gate, scaffolds from the same `templates/agent/`, and owns
  `app-data/desktop.json` in each agent folder. `kit.py chat` calls its
  loopback API. Nothing in this repository imports from it, and **no *contract*
  endpoint is desktop-specific**: the contract endpoints inherit the anonymous
  `/agent-start` surface's behaviour unchanged. One platform endpoint outside the
  contract does exist for the desktop —
  [`POST /api/v1/cli/account/desktop-token`](../cinna_cli_integration/account_cli_workspace.md#7g-exchanging-the-account-token-for-a-desktop-session),
  which trades a `Cloud/<host>/.cinna/account.json` account token for a desktop
  session so the desktop can link a workshop it finds already logged in. It is
  authenticated by the account CLI token, not by the anonymous kit surface, and
  no kit or contract member depends on it.
- **cinna-cli (separate repo)** — `cinna agent import` and the go-cloud manual
  fallback are implemented in the `cinna-cli` repository
  (`src/cinna/local_import.py`), not in this backend. This platform's only
  server-side involvement in the go-cloud step is the pre-existing account-CLI
  endpoints the import command calls.

---

*Last updated: 2026-09-03*
