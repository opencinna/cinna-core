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

## Running a command locally

`docs/CLI_COMMANDS.yaml` is the single source. A local host turns each cloud-first
command into a local one with the `local_command_runner` rules in
`.cinna-kit/layout.json`: apply the first matching rule — a command starting with
`python ` or `python3 ` becomes `uv run ` when `pyproject.toml` exists — otherwise run
the command unchanged.

| In `docs/CLI_COMMANDS.yaml` | Run locally |
|-----------------------------|-------------|
| `python scripts/check_data.py --month` | `uv run scripts/check_data.py --month` |
| `python scripts/weekly_report.py` | `uv run scripts/weekly_report.py` |

In a terminal, run that localized line yourself from the agent root. Cinna Desktop
does the same for `/run:<name>`.

Older agents may still have a `Makefile`; nothing reads it and it never travels.

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
- Every command, localized, has been run once from the agent root.
- No command string contains a credential, a token or an absolute path.
- Command names are operation names and each is unique in the file.
