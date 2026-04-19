# Complex Agent Design Guide

Read this guide when the user asks you to build something **beyond a single workflow script** — an agent that handles multiple distinct capabilities, works with large datasets that need caching, exposes configurable parameters, or runs scheduled health checks. Building mode basics (scripts, files, credentials, package management) are covered in the main system prompt — this document adds the structure for more ambitious setups.

Skip this guide for simple one-script workflows. Use the default `./scripts/` + `./files/` layout from the main system prompt instead.

## When to Use This Guide

Apply the patterns below when at least one of these is true:

- The agent needs to handle **3+ distinct capabilities** (e.g. an HR agent that does employee CRUD, time-off analysis, and cost reports)
- The agent fetches **large or slow-to-retrieve data** that should be cached between runs (thousands of API records, remote DB extracts)
- The agent has **user-tunable parameters** (date ranges, thresholds, exclusion lists) that should live in config files rather than be hardcoded
- The agent produces **derived results worth preserving** across runs (historical reports, snapshots)
- The agent needs **scheduled health checks** that only page a human when something is wrong
- The agent relies on **domain knowledge** (business rules, external system quirks) that doesn't fit into the entrypoint / workflow prompt

For anything simpler — one script, one purpose, no persistence beyond a single output file — use the default workflow layout described in the main building-mode prompt.

## Workspace Layout

Everything lives under `/app/workspace/`. Keep the workspace root clean: only add top-level folders that already have a role.

```
/app/workspace/
├── scripts/                    # All Python scripts (organized by local skill — see below)
│   ├── README.md               # Catalog of every script, grouped by local skill
│   ├── shared_utils.py         # Top-level shared helpers
│   └── <local_skill>/          # Scripts belonging to one local skill
│       ├── some_action.py
│       └── another_action.py
├── docs/                       # Agent-facing documentation
│   ├── WORKFLOW_PROMPT.md      # System prompt for conversation mode
│   ├── ENTRYPOINT_PROMPT.md    # 1-2 sentence trigger message
│   ├── REFINER_PROMPT.md       # Task refinement rules
│   ├── <local_skill>.md        # One doc per local skill (workflow, triggers, how to present)
│   └── <domain_topic>.md       # Business logic / external system behavior
├── files/                      # All data the agent reads or writes
│   ├── cache/                  # Disposable snapshots of external data (NEVER the source of truth)
│   │   └── .last_updated       # ISO timestamp written after a successful cache refresh
│   ├── config/                 # User-tunable parameters (JSON / YAML / CSV)
│   │   ├── settings.json
│   │   └── exclusions.csv
│   ├── data/                   # Derived results worth preserving (reports, exports, snapshots)
│   │   └── 2026-04-19_summary.csv
│   └── ...                     # Ad-hoc intermediate files used between script steps
├── credentials/                # See main prompt — access programmatically only
├── webapp/                     # Optional, see WEBAPP_BUILDING.md
└── workspace_requirements.txt  # Persistent Python dependencies
```

Rules:

- **Never create folders like `cache/`, `data/`, or `config/` at the workspace root.** Those all live under `./files/` so that the workspace root stays small and the `./files/` tree holds everything script-owned.
- **Do not create `.venv`, `node_modules`, or other tooling folders** — the platform manages the runtime.
- **Do not create a `Makefile`, `pyproject.toml`, or `uv.lock`** in the workspace. Dependencies are declared in `./workspace_requirements.txt`; the agent invokes scripts directly via `uv run python scripts/...`.

## Local Skills — Structuring Multi-Capability Agents

A **local skill** is one standalone capability of the agent — its own scripts, its own workflow, its own output format, triggerable on its own. An HR agent might have three local skills: `employee_data_management`, `employee_timeoff_check`, `employee_reports`.

Use local skills to keep `WORKFLOW_PROMPT.md` lean. Without this split, the workflow prompt becomes a wall of text that mixes unrelated capabilities.

### How to structure a local skill

1. **Create one doc per local skill** in `./docs/`:

   ```
   docs/
   ├── employee_timeoff_check.md
   ├── employee_data_management.md
   └── employee_reports.md
   ```

   Each local skill doc must cover:
   - **When to use** — trigger phrases or user intents ("check timeoff of ...", "generate cost report for ...")
   - **Workflow** — step-by-step instructions: which scripts to run, how to chain them, how to interpret output
   - **How to present results** — what to say to the user, what format, what issues to flag
   - **Technical notes** — data sources, calculation logic, edge cases, known limitations

2. **Reference local skills from `WORKFLOW_PROMPT.md`** with a short trigger + pointer:

   ```markdown
   ## Local Skills

   ### Employee Time-Off Check
   Trigger: user asks to check, review, or verify time-off for a specific employee.
   Read `./docs/employee_timeoff_check.md` for the full workflow.
   Quick reference: `uv run python scripts/timeoff/check_employee_timeoff.py --employee="Name"`

   ### Employee Reports
   Trigger: user asks for headcount, cost, or salary reports.
   Read `./docs/employee_reports.md` for the full workflow.
   ```

   The pattern: **`WORKFLOW_PROMPT.md` says _when_ to activate and _where_ to read the instructions. The local skill doc says _how_ to execute.**

3. **Do not inline a local skill's workflow into `WORKFLOW_PROMPT.md`.** If the capability needs more than 5–10 lines of workflow description, split it out.

### When to split

- **Separate local skill**: own scripts, multi-step workflow, its own analysis / checks, its own output format.
- **Inline in `WORKFLOW_PROMPT.md`**: single-command operations ("list companies", "get user by ID") where the agent just runs one script and reports the output.

## Organizing Scripts by Local Skill

Once the agent has 3+ local skills with their own scripts, organize `./scripts/` into subfolders:

```
scripts/
├── README.md                        # documents EVERY script across all subfolders
├── shared_utils.py                  # shared helpers stay at top level
├── fetch_model_metadata.py          # general/standalone scripts stay at top level
├── timeoff/                         # local skill: time-off analysis
│   ├── check_employee_timeoff.py
│   └── timeoff_overview_report.py
├── reports/                         # local skill: employee reports
│   ├── report_employee_costs.py
│   └── report_headcount.py
└── data_management/                 # local skill: CRUD
    ├── manage_users.py
    └── manage_employees.py
```

Guidelines:

- **Shared utilities** (e.g. `odoo_utils.py`, `fetch_metadata.py`) stay at the top level.
- **Local skill scripts** go into a subfolder named after the local skill.
- **Simple standalone scripts** that don't belong to any local skill can stay at the top level.
- **`scripts/README.md`** documents every script, grouped by subfolder. It is a single file, not one per subfolder.
- **Invoke with full path**: `uv run python scripts/timeoff/check_employee_timeoff.py`.
- **Fewer than ~8 scripts total?** Keep flat. Reorganize later as the agent grows.

## Cache — Snapshots of Large External Data

When the agent pulls large or slow datasets (thousands of API records, remote DB extracts, full customer lists), cache them under `./files/cache/` instead of re-fetching on every run.

### When to cache

- Fetching is slow or expensive (API calls, remote queries)
- The same data is read multiple times within a session or across sessions
- The agent compares current state to a previous snapshot

### Cache must be fully restorable

Cache is a **disposable local snapshot**. Deleting everything under `./files/cache/` and running the cache-update script must rebuild it from scratch, every time.

This means:

- Cache-update scripts **replace** data — full re-fetch or full table replacement. No incremental patching.
- Never mutate individual cached records in place (e.g. updating one row's status). If the source changed, re-fetch the whole set.
- No processing script may depend on a specific previous cache state to produce a correct new cache.

If the cache is corrupted, the only recovery step is to re-run the cache-update script.

### CSV vs SQLite

| | **CSV files** | **SQLite database** |
|---|---|---|
| **Best for** | Read-all-and-process workflows | Filtering, joining, aggregation |
| **Example** | Dump all exchange rates, then iterate | "Top 10 customers by revenue last quarter" |
| **Pros** | Human-readable, easy to inspect | Indexes, joins, GROUP BY; no need to load everything in memory |
| **Cons** | Every query reads the full file | Heavier setup; harder to eyeball |

**Rule of thumb**: if post-processing is "read everything and iterate" — CSV. If it involves filtering/sorting/grouping/joining — SQLite at `./files/cache/data.db`.

Start with CSV; migrate to SQLite if query complexity grows.

### Cache-aware script split

Split the workflow into two kinds of scripts:

1. **Cache-update scripts** — fetch from source and write to `./files/cache/`. Document these in `scripts/README.md`.
2. **Processing scripts** — read only from `./files/cache/` and write results to `./files/data/` (or print a summary).

### Deterministic pagination

When fetching in batches (offset/limit), **always specify an explicit sort order** — usually `id ASC`. Without it, data sources may return rows in unstable order between pages, causing rows to be skipped or duplicated. The row count can look right while the actual set is wrong — a subtle bug.

```python
# Bad — unstable order may skip or duplicate rows across pages
records = api.search_read(domain, fields, limit=500, offset=offset)

# Good — explicit sort guarantees each row appears exactly once
records = api.search_read(domain, fields, limit=500, offset=offset, order="id ASC")
```

Applies to Odoo XML-RPC, REST APIs with `?page=N`, SQL `LIMIT/OFFSET`, etc. If the source has no sort parameter, fetch all IDs first, then retrieve records by ID.

### Cache freshness

After a successful cache update, write `./files/cache/.last_updated` with an ISO timestamp. Processing scripts should read this file and print when the cache was last refreshed.

Define the freshness policy in `WORKFLOW_PROMPT.md`. Example:

```markdown
## Cache rules

- Cache lives in `./files/cache/`. Do not commit or edit cache files by hand.
- Before any processing command, check `./files/cache/.last_updated`:
  - Missing → run the cache-update script first
  - Older than 1 hour → inform the user and suggest refreshing
  - Fresh → proceed
- If the user says "refresh", always re-fetch regardless of age.
- After any cache update, print the timestamp to confirm success.
```

## Config — User-Tunable Parameters

Store values the user might want to change without editing script code under `./files/config/`:

```
files/config/
├── settings.json              # Structured parameters (date ranges, thresholds, flags)
└── exclusions.csv             # Flat lists (ignored IDs, test accounts)
```

### What belongs in config

- Date ranges ("parse data starting from 2025-01-01")
- Exclusion lists ("skip these manager IDs", "ignore these test companies")
- Thresholds ("flag orders above 10,000")
- Feature toggles ("include archived records: false")
- Entity mappings (currency code → display name)

### Format

- **JSON** — structured settings with nesting
- **YAML** — same, when readability matters
- **CSV** — flat lists and lookup tables

### Scripts must read from config

Never hardcode values that belong in config:

```python
# Good — reads from config
import json
with open("./files/config/settings.json") as f:
    settings = json.load(f)
start_date = settings["start_date"]

# Bad — hardcoded
start_date = "2025-01-01"
```

### Document config in WORKFLOW_PROMPT.md

Tell the conversation agent which config files exist and what they control, so that it can update them when the user asks to change a parameter:

```markdown
## Configuration

- `./files/config/settings.json` — general parameters (start_date, threshold, etc.)
- `./files/config/exclusions.csv` — list of entity IDs to skip during processing

When the user asks to change a parameter ("start from February instead"), update the
relevant config file — do not modify script code.
```

## Data — Derived Results Worth Preserving

When scripts produce meaningful output that should survive across runs (reports, exports, point-in-time snapshots), write it to `./files/data/`:

```
files/data/
├── 2026-04-19_rate_gaps_report.csv
└── quarterly_summary.md
```

Guidelines:

- Use descriptive filenames, ideally with dates or identifiers (`2026-04-19_validation_results.csv`).
- Keep `./files/cache/` for raw source snapshots and `./files/data/` for derived results — do not mix them.
- Ad-hoc intermediate files used between script steps can stay at the top of `./files/` (e.g. `./files/invoices_parsed.csv`) — reserve `./files/data/` for outputs the user would want to keep.
- Overwriting a report file on re-run is fine. Consider keeping dated historical versions when the result represents a point-in-time snapshot.

## Domain Docs — Business Logic and External System Behavior

When the agent relies on domain knowledge that doesn't fit the workflow prompt — business rules, external system quirks, decision rationale, entity relationships — put each topic in its own file under `./docs/`:

```
docs/
├── currency_rates_refreshing_logic.md
├── odoo_multi_company_setup.md
└── employee_cost_calculation.md
```

Reference these from `WORKFLOW_PROMPT.md` or from the relevant local skill doc so the agent knows where to look:

```markdown
## References

- `./docs/currency_rates_refreshing_logic.md` — how Odoo fetches and applies exchange rates
- `./docs/odoo_multi_company_setup.md` — per-company field conventions
```

Keep `WORKFLOW_PROMPT.md` focused on _how to execute_, local skill docs on _when/how to activate a capability_, and domain docs for the deeper _why_.

## Scheduled Script Triggers — the "OK" Pattern

Complex agents often need to run lightweight checks on a schedule — monitor a mailbox, watch a queue, compare DB counts — and only involve the conversation agent when something actually needs attention. The platform supports this via the **script trigger** schedule type.

### How script triggers work

When a script trigger fires, the platform executes a shell command inside the agent environment (working directory `/app/workspace/`) and inspects the output:

- **If `stdout.strip() == "OK"` AND exit code `0`** → silently logged, **no session created, no tokens spent**.
- **Anything else** (different stdout, non-empty stderr, non-zero exit code, empty stdout) → a new session is created, seeded with the command, its output, stderr, and exit code, plus the prompt *"Please review the output above and take appropriate action."*
- **If execution fails** (timeout, env unavailable) → the failure is logged; no session is created and the schedule does not advance.

Output is compared **case-sensitively** and **trimmed**. Only the literal string `OK` counts as OK. Empty stdout with exit 0 is **not** OK — the trigger will create a session.

Stdout is truncated at 10,000 characters. Default command timeout: 120 seconds (max 300 seconds).

Minimum allowed frequency: **30 minutes**. Anything more frequent is rejected at schedule creation time.

### Writing an OK-pattern script

Design scheduled-trigger scripts so the common "nothing to do" case ends with a single line: `OK`. Any interesting state produces a short, human-readable report with enough context for the conversation agent to act on without re-running the check.

```python
#!/usr/bin/env python3
"""
Script: scripts/monitoring/check_unpaid_invoices.py
Run as a scheduled script trigger. Prints "OK" when nothing needs attention,
otherwise prints a short report that will seed a new agent session.
"""

import json
import sys
from pathlib import Path


def main():
    try:
        overdue = find_overdue_invoices()
    except Exception as exc:
        # Non-zero exit + stderr → platform creates a session with the error context
        print(f"check_unpaid_invoices failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not overdue:
        print("OK")
        return

    # Non-OK path: concise, agent-readable summary
    print(f"Found {len(overdue)} overdue invoices that need follow-up:")
    for inv in overdue[:20]:
        print(f"- {inv['number']} | {inv['customer']} | {inv['amount']} | due {inv['due_date']}")
    if len(overdue) > 20:
        print(f"... and {len(overdue) - 20} more.")

    # Drop the full list on disk so the conversation agent can pick it up
    out = Path("./files/data/overdue_invoices_latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(overdue, indent=2))
    print(f"Full list: {out}")


def find_overdue_invoices():
    # ... query cache or source
    return []


if __name__ == "__main__":
    main()
```

### Rules for OK-pattern scripts

- **Print exactly `OK`** (uppercase, no punctuation, no extra whitespace) on the happy path. A trailing newline is fine; extra characters are not.
- **Never print progress chatter** (`"Connecting..."`, `"Fetched 500 rows"`) on the happy path — it breaks the exact-match check.
- **Keep non-OK output short and actionable.** The first line should summarize the situation ("3 overdue invoices found"), and the rest should give the conversation agent enough context to act. Big payloads belong in a file under `./files/data/` — reference the path from the output.
- **Use `sys.exit(1)` + a stderr message for errors.** This still creates a session (with the error as context) and makes the failure visible in execution logs.
- **Never print credential values or secrets.** Scheduled output is surfaced to the agent session and stored in execution logs.
- **Be fast and idempotent.** Commands time out at 120s by default and may run unattended for months — running the same check twice in a row must be safe.
- **Update `STATUS.md` on state transitions.** When a check moves from OK to a problem state (or recovers), call `scripts/update_status.py` to publish the new state. Do not update on every run — only when the severity or summary actually changes. See the [Agent Self-Reported Status](#agent-self-reported-status) section.
- **Work from cache when possible.** If the check involves a large dataset, run against `./files/cache/` and let a separate cache-update schedule handle refreshes. Do not push slow API calls into the minute-by-minute scheduler.

### The command to put in the schedule

The schedule's **command** field is a single line executed at `/app/workspace/`. For an OK-pattern script:

```
uv run python scripts/monitoring/check_unpaid_invoices.py
```

For quick bash-only checks, a one-liner is fine too:

```
[ -s ./files/cache/inbox.json ] && echo "OK" || echo "inbox cache missing"
```

Add `./workspace_requirements.txt` entries for any packages the script imports, so the dependency survives environment rebuilds (see the main building-mode prompt).

### Document every scheduled script

In `scripts/README.md`, mark scheduled-trigger scripts explicitly:

```markdown
## monitoring/check_unpaid_invoices.py
**Purpose**: Scheduled health check — reports overdue invoices that need follow-up.
**Scheduled trigger**: yes (OK-pattern — prints "OK" when no invoices overdue).
**Usage**: `uv run python scripts/monitoring/check_unpaid_invoices.py`
**Output**: stdout "OK" or short report; full list dumped to `./files/data/overdue_invoices_latest.json`.
```

And mention the schedule in `WORKFLOW_PROMPT.md` so the conversation agent knows the context when a session is opened by the trigger:

```markdown
## Scheduled Checks

- `scripts/monitoring/check_unpaid_invoices.py` runs every 2 hours during business hours.
  When a session is created from this trigger, the seed message will contain the list of
  overdue invoices. Read `./files/data/overdue_invoices_latest.json` for the full payload
  and follow the workflow in `./docs/overdue_invoice_followup.md`.
```

## Agent Self-Reported Status

Complex agents can publish a lightweight status snapshot that surfaces in the `/agent-status` command, the REST API, and the dashboard tile — without starting a session or spending any tokens. The mechanism is a single file the agent maintains in the workspace root.

### File location

```
/app/workspace/STATUS.md
```

### Purpose

Write a brief description of what the agent is currently doing or how its last scheduled run went. The platform reads this file on demand (and on mtime change) and caches the result. Users and A2A clients can query the status at any time — even when the environment is stopped.

### Recommended format

Plain markdown is fine. Adding a YAML frontmatter block enables structured parsing (severity, summary, timestamp):

```markdown
---
timestamp: 2026-04-19T14:32:05Z
status: ok
summary: Invoice poll caught up; 0 pending items
---

## Now
- Inbox polling every 10 min — last ran 14:30 UTC, 0 unread.
```

- `timestamp` — ISO 8601 with timezone. Used as the "reported at" time; falls back to file mtime if absent.
- `status` — Severity: `ok`, `warning`, `error`, or `info`. Anything else (or absent) → `unknown`.
- `summary` — One-line description, truncated to 512 characters. Falls back to first non-blank body line.

### Update-on-change rule

**Overwrite in place — do not append.** Only update the file when state actually changes (e.g., a scheduled check transitions from OK to error, or recovers). Writing on every run without a state change wastes disk I/O and produces noisy activity entries. If state has not changed, leave the file alone.

### Secret hygiene

Never write credential values, tokens, or API keys into `STATUS.md`. The file is readable by any user with dashboard access to the agent. Apply the same rule as for OK-pattern script output.

### Helper script

`scripts/update_status.py` is pre-shipped in the workspace. It handles frontmatter generation, atomic writes, and the change-detection check so the agent does not have to inline this logic. Call it from any scheduled script that needs to update agent state:

```
uv run python scripts/update_status.py --status ok --summary "All monitors green"
```

Run `uv run python scripts/update_status.py --help` for full options.

---

## Documentation Sync (non-negotiable)

Every time scripts are added, modified, or removed, update in the same session:

- **`./scripts/README.md`** — script catalog (purpose, usage, key args, output). Group by subfolder when using local skill organization.
- **`./docs/WORKFLOW_PROMPT.md`** — local skill triggers, scheduled-check entries, script chains, config references.
- **`./docs/<local_skill>.md`** — if the change touches a local skill's workflow or output format.
- **`./docs/REFINER_PROMPT.md`** — if the change added a new required parameter or a sensible default.

Outdated docs make the agent give wrong instructions and users run stale commands. Treat doc updates as part of the same change, not as follow-up.

## Checklist for a Complex Agent

1. Decide which capabilities are **local skills** — list them before writing any script.
2. Create `./docs/<local_skill>.md` for each — fill in triggers, workflow, result presentation.
3. Organize `./scripts/` into subfolders by local skill once you have 3+ skills; keep shared helpers at top level.
4. Maintain `./scripts/README.md` as the single catalog, grouped by subfolder.
5. For every large/slow dataset, split into cache-update and processing scripts; store under `./files/cache/`; write `.last_updated`; document freshness rules in `WORKFLOW_PROMPT.md`.
6. Move tunable parameters into `./files/config/`; reference them from `WORKFLOW_PROMPT.md`.
7. Write preserved results to `./files/data/` with descriptive, dated filenames.
8. Put domain knowledge into `./docs/<topic>.md` and reference it from workflow / local skill docs.
9. Persist every integration-specific Python package via `./workspace_requirements.txt`.
10. For scheduled monitoring, write OK-pattern scripts — `OK` on the happy path, short actionable report otherwise — and wire them up as script-trigger schedules with at least 30 minutes between executions.
11. Keep all three prompt files (`WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, `REFINER_PROMPT.md`) in sync with the current capabilities.

## Pointers

- Basic building-mode rules (scripts location, credentials handling, `uv`, `./workspace_requirements.txt`, `WORKFLOW_PROMPT.md` / `ENTRYPOINT_PROMPT.md` / `REFINER_PROMPT.md` formats) → main building-mode prompt.
- Web apps and dashboards → `/app/core/prompts/WEBAPP_BUILDING.md`.
