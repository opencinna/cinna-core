# 12 — Keeping the kit up to date

## Read this when

- The root `AGENTS.md` freshness rule fired (`.cinna-kit/.last_refresh_check` is
  missing or older than 7 days).
- A guide contradicts what you see, or references a file that does not exist.
- Before a cloud import, so the go-cloud playbook matches the platform.
- The user asks to "update the kit" or "refresh the conventions".

This is about the **kit**, not about the agents. Refreshing never touches anything
under `Local/`.

## Check

```bash
uv run .cinna-kit/tools/kit.py refresh --check
```

It reads `.cinna-kit/VERSION`, compares it with `{{KIT_BASE_URL}}/version`, reports
whether an update exists, and writes `.cinna-kit/.last_refresh_check`.

Offline or unreachable? It warns and exits 0. Say so in one line and continue with
the kit you have — a stale kit is fine; a blocked session is not.

## Update

```bash
uv run .cinna-kit/tools/kit.py refresh
```

It downloads the new tarball to a temporary directory, verifies it extracted
cleanly, and swaps `.cinna-kit/` in one move. The old tree is removed only after the
new one is in place, so an interrupted refresh leaves the previous kit working.

Then, always:

```bash
cat .cinna-kit/CHANGELOG.md      # top entry first
```

## Read the changelog properly

Entries are grouped in a fixed order:

1. **Breaking** — a convention that makes an existing agent invalid. Migrate now;
   the entry says how. A bump of `schema_version` is always breaking.
2. **Added** — new optional artefacts. Existing agents keep working untouched. Adopt
   only when the relevant ladder trigger has fired.
3. **Changed** — wording, defaults, reorganisation. No action.

Tell the user in one line what actually changed for them. "Kit updated" alone is not
useful; "Kit updated — schedules now need a timezone, I fixed both agents" is.

## What a refresh does and does not touch

| Touched | Not touched |
|---------|-------------|
| `.cinna-kit/` in full: guides, templates, schema, tools, VERSION | Anything under `Local/` |
| | Anything under `Cloud/` |
| | The root `AGENTS.md`, `CLAUDE.md`, `README.md`, `.gitignore` |

New `templates/agent/` content applies to agents created **after** the refresh.
Existing agents are never rewritten automatically — and you should not rewrite them
by hand either unless a Breaking entry says to.

If a root file needs a change, the changelog says so explicitly; apply it yourself,
preserving anything the user added.

## Kit version on an agent

Each agent records the kit version it was scaffolded with, in `cinna-agent.json`
`kit_version`. `kit.py validate` reports (at info level) when an agent predates the
current kit. That is information, not a defect: an older agent that validates cleanly
is fine, and there is no upgrade command. Migrate only what a Breaking entry
requires.

## After a refresh

1. Read the top changelog entry.
2. If it contains a Breaking item, run `kit.py validate` on **every** agent under
   `Local/` and fix what it flags.
3. Re-read any guide the changelog says changed, before acting on it from memory.
4. Report one line to the user.

## When the kit and reality disagree

If a guide describes something that does not match what you observe — a CLI verb
that does not exist, a field the platform rejects — trust the observed behaviour,
tell the user which guide is out of date, and refresh the kit before assuming
anything else in that guide is current. Do not silently work around it and leave the
next session to rediscover the same gap.

## Done when

- `.cinna-kit/.last_refresh_check` exists and is less than 7 days old.
- `.cinna-kit/VERSION` matches `{{KIT_BASE_URL}}/version`, or you have told the user
  why it cannot be checked right now.
- You have read the top entry of `.cinna-kit/CHANGELOG.md`.
- Every Breaking item in that entry has been applied, or explicitly declined by the
  user.
- `kit.py validate` passes for every agent under `Local/`.
- The user knows in one sentence what changed for them.
