# {{name}}

This file wraps the agent for local use. It is **not** copied to the cloud — in the
cloud the platform supplies the equivalent runtime wrapper. The agent's real
instructions live in `docs/WORKFLOW_PROMPT.md` and nowhere else.

## When the user talks to you here, you are {{name}}

1. Read `docs/WORKFLOW_PROMPT.md` and follow it. That file is the single source of
   truth for how this agent behaves; nothing is duplicated here.
2. Run scripts with `uv run scripts/<script>.py` from this folder. `scripts/README.md`
   is the catalog of what exists and what each script outputs.
3. Get credentials through `scripts/cinna_credentials.py` — inside a script, never in
   the conversation. **Never print, echo or log a credential value.**
4. Write runtime output to `app-data/storage/`. Disposable snapshots go to
   `app-data/cache/`. Never write to `docs/`, `scripts/`, `knowledge/` or `files/`
   while acting as the agent.
5. Read tunable parameters from `config/`, not from hardcoded values in scripts.

## When the user asks to change or extend this agent

Switch to the **Builder** role and say so in one line. Then:

1. Read `../../.cinna-kit/README.md` (the kit index and the capability ladder).
2. Make the change.
3. Update `scripts/README.md`, `README.md` and `cinna-agent.json` in the same change.
4. Run the ladder check and report the result in one line.
5. Run `uv run ../../.cinna-kit/tools/kit.py validate .` before declaring done.

## Quick reference

```bash
make help              # available commands
make status            # refresh app-data/storage/STATUS.md
make validate          # kit validation for this agent
```

## Rules

- One step per script. Small, parameterised, composable.
- Secrets never appear in output, commits, `STATUS.md` or chat.
- `cinna-agent.json` describes this agent; keep it true.
