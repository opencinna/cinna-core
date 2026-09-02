# 07 — CLI commands

## Read this when

The same operation is run repeatedly and deserves a name: a monthly check, a report,
a cache refresh, a reindex. If an operation has been run twice by hand, it is a
candidate.

## Why it matters

`docs/CLI_COMMANDS.yaml` declares named shell commands. Locally it is documentation
of what can be run. In the cloud each entry becomes a `/run:<name>` slash command, an
A2A skill and a webapp action, executed **without an LLM turn** — faster, cheaper and
more reliable than asking the model to remember which script to call.

Declaring commands early costs nothing and makes the cloud step free.

## The file

`docs/CLI_COMMANDS.yaml`:

```yaml
commands:
  - name: status
    description: Refresh the agent's self-reported STATUS.md.
    command: python scripts/update_status.py --status ok --summary "Ready"

  - name: check
    description: Monthly data-quality check. Run after the month closes.
    command: python scripts/check_data.py --month

  - name: report
    description: Generate the weekly report into app-data/storage.
    command: python scripts/weekly_report.py
```

| Field | Rule |
|-------|------|
| `name` | Required. `^[a-z][a-z0-9_-]{0,31}$`. Unique in the file — the first occurrence wins. |
| `command` | Required. Single line, 1–1024 characters. |
| `description` | Optional, up to 512 characters. Write one anyway; it is the tooltip users see. |

Limits: at most 50 commands, 64 KB file size. Unknown keys are ignored for
forward-compatibility, which also means a typo in a key is silent — check your
spelling.

## Cloud-first, always

Commands run **inside the agent's workspace with the workspace root as the working
directory**. So:

- use `python scripts/x.py`, never `uv run`;
- use paths relative to the workspace root, never absolute paths;
- do not rely on a virtualenv, a shell alias or anything in the user's home;
- keep it to one line — no `&&` chains that hide a failure, no interactive prompts.

If a command needs several steps, write a script that does them and call the script.

## Makefile parity

The `Makefile` is the local mirror. Every YAML entry gets a target with the same
name, running the same thing with `uv run`:

```makefile
check: ## Monthly data-quality check
	uv run scripts/check_data.py --month

report: ## Generate the weekly report
	uv run scripts/weekly_report.py
```

Add both in the same change. `kit.py validate` warns when a command name has no
matching Makefile target, because a drifted pair means one of the two is wrong and
you cannot tell which.

Also add a `## comment` to every target — `make help` is generated from those, and a
target nobody can discover is not a command.

## Naming

Name commands after the **operation**, not the script: `check`, `report`,
`cache-update`, `reindex`, `status`. A user types `/run:report`, not
`/run:generate_weekly_report_v2`.

Keep the set small. Six well-named commands beat twenty that nobody can tell apart.

## What not to declare

- Anything destructive without an explicit argument. A command is one click.
- Anything that prompts for input; there is no terminal on the other end.
- Anything that takes hours; use a `script_trigger` schedule instead
  (`05-schedules.md`).
- Anything that prints a secret. The command string itself is visible to users, so
  never put a token in it either.

## Done when

- `docs/CLI_COMMANDS.yaml` exists with at least the `status` command.
- Every declared command uses a relative path, `python` (not `uv run`), and one line.
- Every declared command has a matching Makefile target with a `##` description.
- Every command has been run successfully at least once, from the agent root.
- No command string contains a credential, a token or an absolute path.
- Command names are operation names and each is unique in the file.
