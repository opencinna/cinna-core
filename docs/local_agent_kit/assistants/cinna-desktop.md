# Cinna Desktop's in-app assistant

Notes for the assistant that builds agents **inside Cinna Desktop**, rather than in a
terminal. Everything in `START.md`, the kit `README.md` and the guides applies
unchanged; this file only covers what is specific to that host.

**This document is not part of the contract, on purpose.** Cinna Desktop ships the
*contract* — `kit.json`, `layout.json`, `CONTRACT_VERSION`, `CHANGELOG.md`, `schema/`
and `templates/` — bundled inside the app, and that is the whole of what the two hosts
have agreed on. The guides, these assistant notes and `tools/kit.py` are kit-only and
travel in the kit tarball, not the contract one. So nothing written here may be treated
as a rule another host must implement: the rules live in `layout.json`, the schema and
the folder templates. This is advice about working in the app, filed with the guides
because that is what it is.

## What you have, and what you do not

You are sandboxed. There is no terminal beyond whatever bash tool the engine hands you,
network access may be absent, and you cannot ask the user to run something in a shell
they do not have open.

- Run commands from the agent folder, with relative paths only. This is the same rule
  the cloud runtime enforces, so it costs nothing and buys portability.
- `.cinna-kit/` may hold **only the contract**. There may be no `guides/` and no
  `tools/kit.py`. Check before you reach for either, and never hand the user a
  `uv run .cinna-kit/tools/kit.py …` line you have not confirmed exists.
- Scaffolding and validation are the app's own actions here. The desktop builds and
  checks folders from the same contract `kit.py` reads, so a folder it created is one
  `kit.py new` would have created, and the other way round. Use the app's action rather
  than assembling a folder by hand — that rule does not soften just because the tool has
  a different name in this host.

## Never run `kit.py refresh`

The desktop owns `.cinna-kit/`. It installs the contract from its bundled copy and
replaces it when the app updates.

`refresh` downloads the full kit tarball and swaps the whole `.cinna-kit/` directory in
one move. In this host that replaces the app's contract with a different version behind
the app's back, and leaves it with no record of what it now has — the one shared file
set the two hosts agree on, changed by neither of them deliberately.

If the contract looks stale — a guide contradicts what you see, or a manifest field the
app rejects is documented as valid — say so in one line and tell the user to update
Cinna Desktop. Do not work around it silently, and do not update the contract yourself.

## Testing: use the local API, never role-play

The app runs the agent for real. That is the reason to build in it, and it changes the
testing rule in `guides/10-testing-locally.md`:

1. Open a **fresh** conversation with the agent — not the one you built it in.
2. Send each entry of `example_prompts` verbatim.
3. Read what the agent actually answered, and judge that.

`example_prompts` is the acceptance suite; run **every** entry, not the first one. A
prompt the agent cannot answer is either a broken agent or a wrong example — decide
which, and fix that one. The symptom table in `guides/10-testing-locally.md` §1 reads
the same way here: you are still looking for the gap between what the build session
knows and what `docs/WORKFLOW_PROMPT.md` says.

What does **not** apply to you is the role-switch fallback. Role-play exists for
assistants with no runtime to talk to. You have one, so imitating the agent is never the
test — and never present your own answer to an example prompt as the agent's. A user who
cannot tell whether they read the agent or an assistant impersonating it has learned
nothing, which is the whole reason the real path exists.

## `app-data/desktop.json` is not yours

The app writes that file to tell tools where its loopback API is and which token speaks
for this agent. See `guides/03-scripts-and-data.md`.

- Read-only to you, and to every other tool. Never write it, never commit it, never
  print or quote its contents — it holds a bearer token, and an error message that
  interpolates the file is the cheapest way to leak one.
- It is git-ignored and never travels to the cloud. Do not "fix" either of those.
- Its absence means the agent is simply not connected. That is a state, not a fault.

## Secrets

Never open `credentials/.env` or `credentials/credentials.json`, not even to check the
format — `credentials/.env.example` is there for that. Never echo a credential value
into the conversation, a script's output, `STATUS.md` or a commit. If a tool result ever
contains one, do not repeat it, and tell the user which file leaked it.

## Roles and instruction files

`AGENTS.md` in the directory you are working in is the instruction file, and the nearest
one always wins: the workshop root gives you the orchestrator role, `Local/<slug>` gives
you that agent's. Say which role you switched into, in one line, whenever you change
folder. `CLAUDE.md` only points at `AGENTS.md`; there is nothing in it to read.

## When you cannot do something

Say so plainly and stop. Do not simulate a capability you lack, do not claim a command
ran when you only printed it, and do not substitute a plausible answer for a real one.
Everything in this kit is easier to recover from than a result nobody can trust.
