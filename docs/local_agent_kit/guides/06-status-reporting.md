# 06 — Status reporting

## Read this when

The agent runs unattended (it has a schedule), or performs long-running or periodic
checks the user will want to see the result of without starting a conversation.

## What it is

The agent publishes a single file, `app-data/storage/STATUS.md`, describing its
current state. It always reflects *now* — the agent overwrites it, never appends.

In the cloud that same file drives the agent card footer, a status dialog, a REST
endpoint, an A2A method and an `/agent-status` chat command, all without spending an
LLM turn. Locally it is a plain file the user (or you) can read. Same convention,
same path, no changes at import.

## The format

```markdown
---
status: ok
summary: "Checked 42 invoices, 0 missing a PO"
timestamp: 2026-09-01T06:05:11Z
---

# Agent Status

Last run at 06:05 UTC. 42 invoices scanned, none flagged.
Next scheduled check: tomorrow 06:00 Europe/Berlin.
```

| Key | Rule |
|-----|------|
| `status` | One of `ok`, `info`, `warning`, `error`. Anything else normalises to `unknown`. |
| `summary` | One short line. It is what shows up in a list of agents. |
| `timestamp` | UTC, ISO-8601. Falls back to the file mtime if absent. |

Frontmatter is optional but always worth writing: without it the severity is
`unknown` and the summary falls back to the first body line.

**STATUS.md is a public artefact.** It is rendered in the UI, returned by the API and
shared over A2A. Never write a credential value, a token, a customer name or any
personal identifier into the summary or the body.

## Writing it

Use the shipped helper — it writes atomically (temp file + rename), so a reader
never sees a half-written file:

```bash
uv run scripts/update_status.py --status ok --summary "All clear"
uv run scripts/update_status.py --status warning --summary "3 invoices missing a PO" \
  --details "INV-A, INV-B and INV-C have no purchase order.
Run 'make report' for the full list."
```

From inside another script:

```python
import subprocess, sys
subprocess.run(
    [sys.executable, "scripts/update_status.py", "--status", "ok", "--summary", "All clear"],
    check=True,
)
```

Call it at the **end of every unattended run**, on both the success and the failure
path. A schedule that only updates the status when it succeeds leaves a stale green
`ok` behind after it starts failing — which is worse than no status at all.

## Wire up the refresh command

The platform can regenerate `STATUS.md` on demand, immediately before reading it.
Two pieces:

1. A named CLI command in `docs/CLI_COMMANDS.yaml` (the scaffold ships one):

   ```yaml
   commands:
     - name: status
       description: Refresh the agent's self-reported STATUS.md.
       command: python scripts/update_status.py --status ok --summary "Ready"
   ```

2. `status_refresh_command` in `cinna-agent.json`, referencing it:

   ```json
   "status_refresh_command": "/run:status"
   ```

`/run:<name>` resolves against `docs/CLI_COMMANDS.yaml`. If the name is not there,
the platform emits a warning and reads the file anyway — so a broken reference is
silent-ish and easy to miss. Keep the two in sync.

Replace the scaffold's placeholder command with one that actually recomputes the
state: a real check that decides between `ok`, `warning` and `error`. A refresh
command that hardcodes `--status ok` is a lie the UI will repeat.

The command is cloud-first: `python scripts/…`, relative path, cwd is the workspace
root. The Makefile mirrors it as `make status` with `uv run`.

A blank `status_refresh_command` is a deliberate opt-out and produces no warning.

## Choosing a severity

| Severity | Use for |
|----------|---------|
| `ok` | The last run completed and nothing needs attention. |
| `info` | Completed, with something worth noticing but no action needed. |
| `warning` | Something needs a human eventually: degraded source, partial data, growing backlog. |
| `error` | The agent could not do its job: missing credential, unreachable source, crash. |

Do not invent a scale. Four values, chosen by whether a human must act.

## Done when

- `app-data/storage/STATUS.md` exists and was produced by `scripts/update_status.py`.
- Its frontmatter has `status`, `summary` and `timestamp`, and the severity is one
  of the four valid values.
- Every unattended entry point updates the status on both success and failure.
- `docs/CLI_COMMANDS.yaml` has a `status` command that genuinely recomputes state.
- `cinna-agent.json` `status_refresh_command` matches a name in that file.
- `make status` runs and rewrites the file.
- No secret, token or personal identifier appears anywhere in STATUS.md.
