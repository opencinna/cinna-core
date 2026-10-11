# Google Antigravity

Notes for Google Antigravity's agent when it builds agents with this kit. The
rules in `START.md`, the kit `README.md` and the guides apply unchanged, and so
does everything in `assistants/other.md`. This page covers only what is
different for Antigravity.

## Instructions file

Do not assume your harness loads `AGENTS.md` for you. Antigravity's ACP server
does not read `AGENTS.md`, `GEMINI.md` or any rules file. Read it yourself at the
start of every session: at the workshop root for the orchestrator role, and
inside `Local/<slug>` for that agent. The nearest `AGENTS.md` wins. The root
`CLAUDE.md` only imports `AGENTS.md`, so you can ignore it.

## Running an agent on Antigravity

An agent runs on Antigravity when its `cinna-agent.json` says so:

```json
"runtime": { "engine": "antigravity" }
```

Antigravity uses the user's own Google login, not a platform credential. Leave
`runtime.credential` out: `kit.py validate` warns if it is there, and the host
ignores it. A host that does not run Antigravity runs the agent on OpenCode
instead, and cloud import does the same. So keep the agent's prompts and scripts
engine-neutral: do not rely on Antigravity-only tools or settings.

Set the engine only when the user asks for Antigravity. Hosts never pick it on
their own.

## Files you must leave alone

Cinna Desktop keeps an isolated Antigravity home at `.cinna/antigravity` inside
the agent folder. It holds that agent's conversations and settings. It carries its
own `.gitignore`, so it never reaches a commit or a publish. Do not edit, move or
delete it, and do not add it to anything the agent reads.

## Approvals

The kit sets up no sandbox for you. If the user runs you without approval prompts,
every command runs under their account. State what you are about to do in one
line before a command that writes outside the agent folder, deletes files or
reaches the network, and keep `git push` and `cinna` as explicit per-use
decisions.

## Tool support

Use `.cinna-kit/tools/kit.py` exactly as `assistants/other.md` describes, through
`uv run` (it needs Python 3.10+). Run `kit.py validate Local/<slug>` before you
call an agent finished.
