# Cinna local agent kit — index and capability ladder

This is the index of `.cinna-kit/`. Read this file at the start of any session that
changes an agent. Then read only the guides whose trigger has fired.

Kit version `{{KIT_VERSION}}` · schema version `1` · instance {{PLATFORM_URL}}

## Documents

| Document | Read when |
|----------|-----------|
| `START.md` | First contact: setting up the root folder, roles, non-negotiables. |
| `guides/01-first-agent.md` | Creating a new agent, from interview to first working answer. |
| `guides/02-prompts-and-description.md` | Always, before the agent is tested for the first time. |
| `guides/03-scripts-and-data.md` | The agent must do something beyond answering from its prompt. |
| `guides/04-credentials.md` | Anything the agent touches needs a token, login, key or OAuth. |
| `guides/05-schedules.md` | The user says daily / weekly / every / at …, or wants unattended runs. |
| `guides/06-status-reporting.md` | The agent runs unattended, or the user wants to know it is healthy. |
| `guides/07-cli-commands.md` | The same operation gets run repeatedly and deserves a name. |
| `guides/08-knowledge-and-local-skills.md` | The agent has 3+ distinct capabilities, or domain docs longer than a page. |
| `guides/09-multi-agent.md` | A second agent appears, or one agent should hand work to another. |
| `guides/10-testing-locally.md` | Before declaring anything finished, and before going cloud. |
| `guides/11-go-cloud.md` | The user asks to move an agent to {{INSTANCE_NAME}}. |
| `guides/12-keeping-up-to-date.md` | The kit may be stale, or a convention changed under you. |
| `assistants/claude-code.md` | You are Claude Code. |
| `assistants/codex.md` | You are Codex or another sandboxed CLI assistant. |
| `assistants/other.md` | You are anything else. |
| `schema/cinna-agent.schema.json` | You need the exact manifest contract. |
| `templates/root/` | Setting up the root folder. |
| `templates/agent/` | The scaffold `kit.py new` copies. |
| `CHANGELOG.md` | After a kit refresh, to see what changed. |

Ignore rules that exclude *paths* ship as dotless `gitignore` files, so they
cannot hide scaffold files from the repository that stores the kit.
`kit.py new` restores the dot in the created agent; when you install the root
template by hand, copy `templates/root/gitignore` to `<root>/.gitignore`.
(`templates/agent/credentials/.gitignore` keeps its dot on purpose — it names
`.env` and `credentials.json`, which no repository should track.)

Remote fallback for any of these, one file per request:
`{{KIT_BASE_URL}}/kit/guides/01-first-agent.md` and so on.

## The capability ladder

An agent starts minimal and grows only when a trigger fires. Each rung below adds
concrete artefacts. Walk the table top-down after every substantive change.

| Rung | Read the guide when… | Adds to the agent |
|------|----------------------|-------------------|
| **Prompts & description** | always, before the first test | `docs/WORKFLOW_PROMPT.md`, `description`, `example_prompts`, `router_trigger_prompt` |
| **Scripts & data** | the agent does anything beyond answering from prompts | `scripts/`, `config/`, `app-data/storage/`, cache rules |
| **Credentials** | any external system needs a token, login, key or OAuth | `credentials/.env(.example)`, `credentials/README.md`, manifest `credentials[]`, `cinna_credentials.py` usage |
| **Schedules** | the user says daily / weekly / every / at …, or the agent should run unattended | manifest `schedules[]`, `docs/ENTRYPOINT_PROMPT.md` becomes mandatory, a `run-<name>` Makefile target |
| **Status reporting** | the agent runs unattended, or performs long-running checks | `scripts/update_status.py` usage, `app-data/storage/STATUS.md`, `status_refresh_command`, a `status` CLI command |
| **CLI commands** | the same operation is run repeatedly by name | `docs/CLI_COMMANDS.yaml` entries + matching Makefile targets |
| **Knowledge & local skills** | 3+ distinct capabilities, or domain docs beyond a page | `knowledge/<topic>/`, one doc per local skill, `scripts/<skill>/` subfolders |
| **Multi-agent** | a second agent appears, or one agent should delegate | root orchestrator conventions, manifest `handovers[]` |
| **Go cloud** | the user asks, or the agent needs 24/7, email/chat channels, sharing or webapps | everything in `guides/11-go-cloud.md` |

### The ladder check

Run this after every substantive change — a new script, a new capability, a new
requirement from the user:

1. Walk the table from top to bottom.
2. For each rung, ask: **has the trigger fired?**
3. If it has fired **and** the artefacts are missing → read that guide and add them.
4. If it has fired and the artefacts exist → check they still describe reality
   (a renamed script, a new credential, a changed schedule).
5. If it has **not** fired → add nothing. This is a hard rule, not a preference.
6. Report in one line: `Ladder: added <rung> because <trigger>.` or `Ladder: no change.`

**Anti-over-engineering rule.** Never add a rung speculatively. An agent with no
external system does not get a `credentials/` entry. An agent nobody schedules does
not get `STATUS.md`. Unused scaffolding costs the user real attention later.

## The tool

`uv run .cinna-kit/tools/kit.py <command>` — stdlib only, no install step.

| Command | What it does |
|---------|--------------|
| `new <slug> [--name N] [--root DIR]` | Scaffold `Local/<slug>/` from `templates/agent/`. |
| `validate <path> [--fix] [--json]` | Check an agent is coherent and cloud-ready. |
| `list [--root DIR]` | Table of local agents, which rungs each has, cloud state. |
| `refresh [--check]` | Compare and update the kit from {{PLATFORM_URL}}. |
| `export <path> --to DIR` | Produce the cloud-import tree (exclude list applied). |

## Folder meanings (identical locally and in the cloud)

| Folder | Owner | Survives a cloud update? | Use for |
|--------|-------|--------------------------|---------|
| `docs/` | the agent's author | replaced on update | prompts, `CLI_COMMANDS.yaml`, domain docs |
| `scripts/` | the agent's author | replaced on update | Python that does the work |
| `knowledge/` | the agent's author | replaced on update | static reference material, read-only at runtime |
| `files/` | the agent's author | replaced on update | static shipped assets (lookup tables, fixtures) |
| `config/` | the agent's author | replaced on update | user-tunable parameters |
| `credentials/` | the user | never copied to the cloud | `.env` locally, injected `credentials.json` in the cloud |
| `app-data/storage/` | runtime | yes, per install | results the agent produces, `STATUS.md` |
| `app-data/cache/` | runtime | yes, per install | disposable, rebuildable snapshots |
| `app-data/uploads/` | runtime | yes, per install | files the user attaches |

Write runtime output to `app-data/storage/`. Never to `files/` or `docs/`.

## Placeholders

Kit files carry a small fixed set of tokens that the platform substitutes before you
ever see them: the instance URL, the kit base URL, the start URL, the instance name,
the kit version, the CLI install spec and minimum version, and the signup and login
URLs. By the time this text reaches you they are already real values. No other
double-brace token appears anywhere in the kit.

**One exception, inside `templates/agent/` only:** two further tokens are left
deliberately unsubstituted — a lowercase `name` token and a lowercase `slug` token,
written in the same double-brace style. They are what `kit.py new` fills in when it
scaffolds an agent, using the display name and slug you gave it. They appear nowhere
outside that folder. The scaffold's own `README.md` repeats this note in a comment
block you delete after scaffolding.
