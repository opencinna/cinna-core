# Any other assistant

You are a coding assistant that is not specifically covered by this kit. Nothing
here depends on a particular tool, model or harness. Follow the same rules everyone
else does.

## Start here

1. Read `AGENTS.md` in the directory you are working in. At the workshop root it
   defines the orchestrator role; inside `Local/<slug>` it defines that agent.
   The nearest `AGENTS.md` always wins.
2. Read `.cinna-kit/README.md` — the document index and the capability ladder.
3. Read a guide when its ladder trigger fires. Not before.

## Assumptions this kit makes about you

Only these:

- you can read and write files in the working directory;
- you can run shell commands, or ask the user to run them and report back;
- you can hold a short conversation with the user.

Everything else — a plan mode, a permissions file, sub-agents, background tasks, a
particular editor integration — is optional. If you do not have it, say so and work
without it. Never simulate a capability you lack, and never claim a command ran when
you only printed it.

## If you cannot run commands

You can still do the whole job. Print the exact command, ask the user to run it, and
wait for the real output before continuing. Do not guess what it would have said.
This applies especially to `uv run`, `kit.py validate` and anything under
`guides/11-go-cloud.md`.

## The rules that are not negotiable

- Never print, echo or log a credential value; never open `credentials/.env` or
  `credentials/credentials.json`.
- Never create an agent folder by hand — use `kit.py new`.
- Never add a ladder rung whose trigger has not fired.
- Keep `scripts/README.md`, `README.md` and `cinna-agent.json` true in the same
  change that makes them false.
- Announce which of the three roles (Orchestrator, Builder, Agent) you are in.

## Tool support

`.cinna-kit/tools/kit.py` is standard-library-only Python 3.10+, no install step:

```bash
uv run .cinna-kit/tools/kit.py list
uv run .cinna-kit/tools/kit.py new <slug> --name "Display Name"
uv run .cinna-kit/tools/kit.py validate Local/<slug>
```

Use it rather than reimplementing the scaffold. It exists precisely so every
assistant produces an identical, cloud-compatible layout.

## Offline

Download the kit once with the tarball command in `START.md` and read from
`.cinna-kit/` afterwards. Single files are available at
`{{KIT_BASE_URL}}/kit/<path>` as a fallback; do not fetch them one by one as a
routine, because the instance rate-limits per IP.
