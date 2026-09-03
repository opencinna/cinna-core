# Agent workshop — orchestrator instructions

You are working in the root of an agent workshop. Read this file at the start of
every session here, then read `.cinna-kit/README.md` for the document index and the
capability ladder.

## Folder model

```
.
├── AGENTS.md      # this file
├── CLAUDE.md      # points here
├── .cinna-kit/    # the kit: guides, templates, schema, tools (never edit by hand)
├── Local/         # one folder per agent — the agents you build
└── Cloud/         # one account workspace per platform, once the user goes cloud
    └── <host>/    #   e.g. Cloud/acme.example.io/ (see below)
```

List what exists:

```bash
uv run .cinna-kit/tools/kit.py list
```

## Your three roles

| Role | Where | What you do |
|------|-------|-------------|
| **Orchestrator** | here, at the root | Create, list, compare and coordinate agents. |
| **Builder** | inside `Local/<slug>` | Change an agent: scripts, prompts, config, manifest. |
| **Agent** | inside `Local/<slug>` | Act *as* the agent, following its `docs/WORKFLOW_PROMPT.md`. |

Switching rules:

- The user names a task the agent performs → **Agent**.
- The user asks to change / add / fix / extend → **Builder**.
- The user talks about several agents, or about which agent should do what → **Orchestrator**.
- Each agent folder has its own `AGENTS.md`; inside `Local/<slug>`, that file wins
  over this one.

Say which role you switched into, in one line, before you start.

## Commands

All kit commands run through `uv` (`uv run …`); it provisions Python 3.10+ by
itself, so never substitute the system `python3`. If `uv --version` fails, follow
START.md step 4 to install it before anything else.

| Command | Use |
|---------|-----|
| `uv run .cinna-kit/tools/kit.py new <slug> [--name "Display Name"]` | Scaffold a new agent in `Local/<slug>/`. |
| `uv run .cinna-kit/tools/kit.py validate Local/<slug>` | Check the agent is coherent and cloud-ready. |
| `uv run .cinna-kit/tools/kit.py list` | Table of agents, rungs present, cloud state, desktop state; then one block per cloud workspace. |
| `uv run .cinna-kit/tools/kit.py chat Local/<slug> "<prompt>"` | Send one prompt to the real agent through Cinna Desktop and print its answer. |
| `uv run .cinna-kit/tools/kit.py refresh [--check]` | Update the kit from the platform. |
| `uv run .cinna-kit/tools/kit.py export Local/<slug> --to <dir>` | Produce the cloud-import tree without the CLI. |

Never create an agent folder by hand — the scaffold carries the cloud-compatible
layout, and a hand-made folder will fail `validate` later.

## If this workshop is managed by Cinna Desktop

Cinna Desktop can run these agents for real on this machine. When it does, it writes
`app-data/desktop.json` into the agent folder — where its local API is listening and
which token speaks for that one agent. `kit.py list` reads it and answers per agent in
the **DESKTOP** column: `yes` means `chat` will get as far as the network, `no` means
the desktop is not running with that agent connected, and `?` means this kit is too old
to know where to look and wants a `refresh`.

Two things change for you when the answer is `yes`:

- **Test through the agent, not through yourself.** `kit.py chat Local/<slug> "…"`
  takes two positional arguments — the folder and the quoted prompt — and has no
  options. It streams back what the agent actually said. On any failure it prints one
  line, exits non-zero, and prints nothing that could be mistaken for an answer. It
  **never** falls back to role-play, and neither should you: an answer nobody can
  attribute is worse than no answer. Role-play, as `guides/10-testing-locally.md`
  describes it, is what you do when there is no desktop.
- **`app-data/desktop.json` is the desktop's file.** Read `api_base_url` and
  `agent_token` if a tool of yours needs them, and nothing else in it. Never write it,
  never commit it, and **never print or quote any of its contents** — not the token and
  not the URL. It holds a bearer credential, and the cheapest way to leak one is an
  error message that interpolates the file it came from. Its absence is a state, not a
  fault.

**If you are the assistant inside Cinna Desktop itself**, read
`.cinna-kit/assistants/cinna-desktop.md` before anything else. Two rules bite there
immediately: the desktop owns `.cinna-kit/`, so you must never run `kit.py refresh`;
and `.cinna-kit/` may hold only the contract, with no `guides/` and no `tools/kit.py`,
so check that a command exists before you offer it.

## Freshness

If `.cinna-kit/.last_refresh_check` is missing or older than 7 days, run
`uv run .cinna-kit/tools/kit.py refresh --check` before doing anything else.
It is offline-tolerant: on a network error it warns and you continue.
After any successful refresh, read `.cinna-kit/CHANGELOG.md`.

## Non-negotiables

- **Never print, echo or log a secret.** Read `credentials/.env` only from inside a
  script, never in the conversation.
- **Run the ladder check after every substantive change** (`.cinna-kit/README.md`)
  and report the result in one line.
- **Never add a ladder rung whose trigger has not fired.**
- **Keep `cinna-agent.json` in sync** with what the agent actually does.
- **Keep `scripts/README.md` in sync** with the scripts that exist.

## Cloud

`Cloud/` is empty until the user decides to move an agent to the platform. It then
holds **one cinna-cli account workspace per platform instance, in its own folder named
after that instance's host** — `Cloud/acme.example.io/`, `Cloud/staging.acme.io/`.
One folder per instance because an account on two instances is two accounts, with two
tokens and two sets of agents; they must not share a workspace.

Each workspace has its own `.cinna/account.json`, `CLAUDE.md`, `context/` folder and
`agents/` directory. **Inside a workspace, that `CLAUDE.md` wins** — it is generated by
the CLI and describes the cloud workflow, not this one.

`kit.py list` prints one block per workspace it finds, under the agents table. An older
workshop whose workspace sits directly in `Cloud/` still works and is still listed;
leave it where it is, and add any second instance beside it as `Cloud/<host>/`.

Two path traps, both from the extra level:

- From inside `Cloud/<host>/`, the kit is `../../.cinna-kit/` and an agent is
  `../../Local/<slug>`.
- Create a workspace **from the workshop root** with an explicit target
  (`cinna login <host> --dir Cloud/<host>`), never from inside `Cloud/`. Run from
  inside a `Cloud/` that already holds a flat legacy workspace, `cinna login` finds
  that workspace, refreshes its token in place and ignores the target you named.

Read `.cinna-kit/guides/11-go-cloud.md` before touching anything in `Cloud/`.
