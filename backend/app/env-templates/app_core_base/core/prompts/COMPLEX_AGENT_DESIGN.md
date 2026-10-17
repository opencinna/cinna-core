# Complex Agent Design Guide

Read this guide when the user asks you to build something **beyond a single workflow script** — an agent that answers several kinds of request, works with large datasets that need caching, keeps durable state or a queue, performs external side effects, exposes configurable parameters, or runs scheduled health checks. Building mode basics (scripts, files, credentials, package management) are covered in the main system prompt. The reasoning behind each pattern — and what to recommend to the user before building — is in `/app/core/prompts/AGENT_DESIGN_PATTERNS.md`. This document adds the concrete structure.

Skip this guide for simple one-script workflows: `./scripts/` plus runtime output in `./app-data/storage/` is enough.

## When to Use This Guide

Apply the patterns below when at least one of these is true:

- The agent answers **two or more distinct kinds of request** (e.g. an HR agent asked "how many days do I have", "who is away in August" and "cost report for Q3")
- The agent fetches **large or slow-to-retrieve data** that should be cached between runs (thousands of API records, remote DB extracts)
- The agent must **remember** what it did — a queue, retries, "already sent", polling state
- The agent **sends, creates, signs or posts** something outside the platform
- The agent has **user-tunable parameters** (date ranges, thresholds, exclusion lists) that should live in config files rather than be hardcoded
- The agent produces **derived results worth preserving** across runs (historical reports, snapshots)
- The agent needs **scheduled health checks** that only page a human when something is wrong
- The agent relies on **domain knowledge** (business rules, external system quirks) that doesn't fit into the workflow prompt

## Workspace Layout

Everything lives under `/app/workspace/`. Keep the workspace root clean: only add top-level folders that already have a role.

```
/app/workspace/
├── docs/                       # Bundle-owned
│   ├── WORKFLOW_PROMPT.md      # Scope + routing table (conversation-mode system prompt)
│   ├── ENTRYPOINT_PROMPT.md    # 1-2 sentence trigger message
│   ├── REFINER_PROMPT.md       # Task refinement rules
│   ├── CLI_COMMANDS.yaml       # /run:<name> commands
│   ├── AGENT_DEVELOPMENT.md    # Builder-facing map, decisions, defects log (never loaded at runtime)
│   ├── test_scenarios/         # Recorded Say | Expect regression set
│   └── <domain_topic>.md       # Business logic / external system behavior
├── skills/                     # Bundle-owned — one folder per kind of request (see Skills)
│   └── <skill-name>/
│       ├── SKILL.md
│       └── scripts/            # Helpers only this skill uses
├── scripts/                    # Bundle-owned — shared helpers and scripts that belong to no skill
│   └── README.md               # Catalog of EVERY script, including those inside skills/
├── knowledge/                  # Bundle-owned — static reference material
├── config/                     # Bundle-owned — shipped, user-tunable parameters (JSON / YAML / CSV)
├── files/                      # Bundle-owned — static assets (lookup tables, templates, fixtures)
├── agent_api/                  # Optional — see REST_API_BUILDING.md
├── app-data/                   # Per-install runtime data — survives updates, never shipped
│   ├── storage/                #   Durable: SQLite state, derived results, STATUS.md
│   │   └── <area>/
│   └── cache/                  #   Disposable snapshots of external data
│       └── <area>/
├── artifacts/                  # Per-user runtime — what the agent builds to publish (artifacts_publish), never shipped
│   └── <name>/
├── credentials/                # See main prompt — access programmatically only
├── webapp/                     # Optional, see WEBAPP_BUILDING.md
└── workspace_requirements.txt  # Persistent Python dependencies
```

Rules:

- **Runtime output never goes into bundle-owned folders** (`docs/`, `skills/`, `scripts/`, `knowledge/`, `config/`, `files/`) — they are replaced whenever the publisher pushes an update. Durable results and state go to `./app-data/storage/<area>/`; anything rebuildable goes to `./app-data/cache/<area>/`.
- **Never create `cache/` or `data/` at the workspace root**, or any other runtime-data folder beside `app-data/`. The one exception is `artifacts/<name>/`, where the agent builds what it publishes as a Cinna artifact.
- **Do not create `.venv`, `node_modules`, or other tooling folders** — the platform manages the runtime.
- **Do not create a `Makefile`, `pyproject.toml`, or `uv.lock`** in the workspace. Dependencies are declared in `./workspace_requirements.txt`; scripts run as `uv run --quiet python /app/workspace/scripts/...`.
- Agents built earlier keep caches, config and results under `./files/cache/`, `./files/config/` and `./files/data/`. They still run. Move runtime output to `app-data/` when you next touch the script that writes it (an update would otherwise wipe it); shipped config may stay where it is.

## Skills — Structuring Multi-Capability Agents

Each distinct kind of request is one **skill**: `skills/<name>/SKILL.md`, with its own `scripts/` and `references/` when needed (frontmatter rules: "Agent Skills" in the main building prompt). `docs/WORKFLOW_PROMPT.md` stops describing procedures and becomes **scope + routing**:

```markdown
## Scope

You answer questions about employee time off and HR costs. Anything else, however
easy: reply in one sentence that it is outside what you do and name two things you
can do. Invoke no skill and run nothing for it.

## Skills — invoke the matching one before writing anything

| Skill | When the user asks… |
|---|---|
| `timeoff-check` | about **one named employee's** time off — "check timeoff of …", "is … over-booked" |
| `team-planning` | who on **a team** is away and when — "who's off in august", "do we have cover" |
| `employee-reports` | for headcount, cost or salary **reports** |

Deciding between them: one named person → `timeoff-check`; a team or a period →
`team-planning`. A request spanning two skills → invoke both.
```

The pattern: **`WORKFLOW_PROMPT.md` says _which_ skill; `SKILL.md` says _how_.**

- **The `description` carries the triggers and the boundary** — *"…Use for 'check timeoff of …'. Not for team calendars (team-planning)."* It is the only text the model reads before choosing.
- **Every `SKILL.md` body** covers when to use, the command and its flags, how to read the output, how to present the answer, and failure handling — and **ends with a short pre-answer reminder** of the rules that break at the moment of writing (no text before the answer, never compute a figure, names or they/them). A rule stated only in the workflow prompt's tone section is not followed reliably; the same rule at the end of the skill is.
- **Self-contained.** A skill reaches nothing outside its own folder, so it can be published and installed into another agent.
- **Do not inline a skill's procedure into `WORKFLOW_PROMPT.md`** — that brings back the per-turn context cost the skill exists to remove.

### When to split

- **Its own skill**: its own trigger, its own multi-step procedure or scripts, its own output format.
- **Inline in `WORKFLOW_PROMPT.md`**: what the agent does on almost every message, or a single command with obvious output.

### The legacy form

Agents built earlier keep one `./docs/<capability>.md` per capability, referenced from the workflow prompt as "Trigger: … Read `./docs/<capability>.md`". That still works but gets no on-demand loading. Migrate one capability when you next touch it — move the doc to `skills/<name>/SKILL.md`, add the frontmatter, move the scripts only it uses, replace the prompt block with a routing row — and never leave a capability half in each form.

## Organizing Scripts

```
scripts/
├── README.md                        # documents EVERY script, including those inside skills/
├── shared_utils.py                  # shared helpers stay at top level
├── <system>_core/                   # a package that skills, schedules and agent_api/ all import
└── monitoring/                      # scripts that belong to no skill, grouped once there are many
skills/
└── timeoff-check/
    └── scripts/
        └── check_employee_timeoff.py   # used only by this skill
```

Guidelines:

- **A script only one skill uses** lives in that skill's `scripts/`; inside `SKILL.md` call it as `python ${CLAUDE_SKILL_DIR}/scripts/<script>.py`.
- **Shared helpers** and scripts that belong to no skill stay in `scripts/`; group them into subfolders once there are ~8 or more.
- **`scripts/README.md`** is the single catalog, grouped by folder.
- **Full paths** everywhere else — schedules, `CLI_COMMANDS.yaml`, the catalog: `uv run --quiet python /app/workspace/scripts/monitoring/check_queue.py`.
- **One call per question.** Keep each step small, but give every kind of question one entry-point script that prints the whole answer — pre-computed totals and verdicts, spelled-out weekdays, `data_fetched_at`, `truncated` — so conversation mode runs one command and quotes it. If an answer needs a number the output lacks, add it to the script; never leave the arithmetic to the model (`AGENT_DESIGN_PATTERNS.md` §4).

## Cache — Snapshots of Large External Data

When the agent pulls large or slow datasets (thousands of API records, remote DB extracts, full customer lists), cache them under `./app-data/cache/<area>/` instead of re-fetching on every run.

### When to cache

- Fetching is slow or expensive (API calls, remote queries)
- The same data is read multiple times within a session or across sessions
- The agent compares current state to a previous snapshot

### Cache must be fully restorable

Cache is a **disposable local snapshot**. Deleting everything under `./app-data/cache/` and running the cache-update script must rebuild it from scratch, every time.

This means:

- Cache-update scripts **replace** data — full re-fetch or full table replacement. No incremental patching.
- Never mutate individual cached records in place (e.g. updating one row's status). If the source changed, re-fetch the whole set.
- No processing script may depend on a specific previous cache state to produce a correct new cache.

If the cache is corrupted, the only recovery step is to re-run the cache-update script. State the agent must *remember* does not belong here — see Durable State below.

### CSV vs SQLite

| | **CSV files** | **SQLite database** |
|---|---|---|
| **Best for** | Read-all-and-process workflows | Filtering, joining, aggregation |
| **Example** | Dump all exchange rates, then iterate | "Top 10 customers by revenue last quarter" |
| **Pros** | Human-readable, easy to inspect | Indexes, joins, GROUP BY; no need to load everything in memory |
| **Cons** | Every query reads the full file | Heavier setup; harder to eyeball |

**Rule of thumb**: if post-processing is "read everything and iterate" — CSV. If it involves filtering/sorting/grouping/joining — SQLite at `./app-data/cache/<area>/data.db`.

Start with CSV; migrate to SQLite if query complexity grows.

### Cache-aware script split

Split the workflow into two kinds of scripts:

1. **Cache-update scripts** — fetch from source and write to `./app-data/cache/<area>/`. Document these in `scripts/README.md`.
2. **Processing scripts** — read only from the cache and write results to `./app-data/storage/<area>/` (or print a summary).

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

After a successful cache update, write `./app-data/cache/<area>/.last_updated` with an ISO timestamp. Every processing script prints when its data was fetched: payloads carry `data_fetched_at` and `from_cache`, and every script takes `--fresh` to bypass the cache.

Define the freshness policy in `WORKFLOW_PROMPT.md` (or the skill that reads the cache). Example:

```markdown
## Cache rules

- Cache lives in `./app-data/cache/invoices/`. Never edit cache files by hand.
- Before any processing command, check `./app-data/cache/invoices/.last_updated`:
  - Missing → run the cache-update script first
  - Older than 1 hour → inform the user and suggest refreshing
  - Fresh → proceed
- If the user says "refresh", "check again" or "I just fixed it", pass `--fresh`.
- After any cache update, print the timestamp to confirm success.
```

## Durable State — SQLite Under `app-data/storage/`

Anything the agent must **remember** — a submission queue, "already sent" records, retry state, a mute list, a watermark — lives in SQLite at `./app-data/storage/<area>/<name>.db`. It survives bundle updates, environment rebuilds and reinstalls, and it is never shipped in a bundle revision. CSV or JSON files are fine for a report; they are not a queue.

```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/app/workspace/app-data/storage/documents/queue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key         TEXT    NOT NULL UNIQUE,  -- deterministic: what the job is about
    status          TEXT    NOT NULL,         -- queued | processing | done | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (     -- append-only history, one row per transition
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    status     TEXT    NOT NULL,
    message    TEXT,
    created_at TEXT    NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # readers never block the processor
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive only: add columns, never drop or rename."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "caller_email" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN caller_email TEXT")
    # Indexes on migrated columns are created AFTER the migration adds them.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_caller ON jobs(caller_email)")


def claim_next(conn: sqlite3.Connection):
    """Atomically move the oldest due job to `processing`; None when nothing is due."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 1",
        (now(),),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE jobs SET status = 'processing', attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (now(), row["id"]),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, status, created_at) VALUES (?, 'processing', ?)",
            (row["id"], now()),
        )
    conn.commit()
    return row
```

Rules:

- **The status-guarded `UPDATE`** (`WHERE id = ? AND status = 'queued'`) is what stops two overlapping schedule ticks — or an inline API call and a tick — from processing the same job twice.
- **Retry transient failures across ticks** (timeouts, 429, 5xx): back to `queued` with `next_attempt_at` pushed out (e.g. 10 minutes × attempts, capped) up to a maximum number of attempts. Permanent failures (4xx, validation, missing config) go straight to `failed`.
- **One `job_events` row per transition**, so "why is this stuck" has a history.
- **Additive migrations**, guarded by `PRAGMA table_info`.
- **Watermarks only move forward**: upsert with `MAX(excluded.value, current.value)`.
- **Bound the work per tick** (`MAX_PER_TICK`) so a backlog cannot push a scheduled run past its timeout.
- **Give operators an `inspect_<area>.py` script** instead of asking them to open sqlite3.

## Config — User-Tunable Parameters

Store values the owner might want to change without editing script code in `./config/`, shipped with the agent:

```
config/
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

A malformed config file fails loudly with a clear error; silently falling back to defaults changes behaviour nobody asked to change. A missing optional file may mean "defaults".

### Scripts must read from config

Never hardcode values that belong in config:

```python
# Good — reads from config
import json
from pathlib import Path

settings = json.loads(Path("/app/workspace/config/settings.json").read_text())
start_date = settings["start_date"]

# Bad — hardcoded
start_date = "2025-01-01"
```

### Per-install overrides

`config/` is bundle-owned: it is replaced when the publisher pushes an update, so a conversation-mode edit to it does not last. When the users of an installed agent should be able to change a parameter by asking ("start from February instead"), write the override to `./app-data/storage/config/settings.json` and have scripts read it on top of the shipped defaults.

### Document config in WORKFLOW_PROMPT.md

Tell the conversation agent which config files exist and what they control, so it can change a parameter when the user asks:

```markdown
## Configuration

- `./config/settings.json` — shipped defaults (start_date, threshold, …)
- `./app-data/storage/config/settings.json` — this install's overrides
- `./config/exclusions.csv` — entity IDs to skip during processing

When the user asks to change a parameter ("start from February instead"), write the
override file — do not modify script code or the shipped defaults.
```

## Derived Results Worth Preserving

When scripts produce meaningful output that should survive across runs (reports, exports, point-in-time snapshots), write it to `./app-data/storage/<area>/`:

```
app-data/storage/reports/
├── 2026-04-19_rate_gaps_report.csv
└── quarterly_summary.md
```

Guidelines:

- Use descriptive filenames, ideally with dates or identifiers (`2026-04-19_validation_results.csv`).
- Keep `./app-data/cache/` for raw source snapshots and `./app-data/storage/` for derived results — do not mix them.
- Overwriting a report file on re-run is fine. Keep dated or versioned copies (`v2`, `v3`, …) when the result is a point-in-time snapshot or you are comparing prompt variants.

## Domain Docs — Business Logic and External System Behavior

When the agent relies on domain knowledge that doesn't fit the workflow prompt — business rules, external system quirks, decision rationale, entity relationships — put each topic in its own file under `./docs/`:

```
docs/
├── currency_rates_refreshing_logic.md
├── odoo_multi_company_setup.md
└── employee_cost_calculation.md
```

Reference these from the skill that needs them (or from `WORKFLOW_PROMPT.md`) so the agent knows where to look:

```markdown
## References

- `./docs/currency_rates_refreshing_logic.md` — how Odoo fetches and applies exchange rates
- `./docs/odoo_multi_company_setup.md` — per-company field conventions
```

Keep `WORKFLOW_PROMPT.md` focused on scope and routing, skills on _how to execute_, and domain docs on the deeper _why_.

### Builder-facing docs

Two documents are for whoever builds on the agent next and are never referenced from the workflow prompt, so they cost no runtime context:

- **`docs/AGENT_DEVELOPMENT.md`** — topology (the agents it calls or serves, with ids), file map, the design decisions made with the user, invariants a change must not break, how to extend it, how to test it, and a dated list of **defects this agent already had** with cause and fix (`AGENT_DESIGN_PATTERNS.md` §11).
- **`docs/test_scenarios/`** — a `README.md` (fixtures, how to run, the bisect rule) plus one file per kind of question with numbered invariants and a **Say | Expect** table phrased like a hurried user. Re-run it after every change to a prompt, a skill, the model, the provider or a producer's response shape (`AGENT_DESIGN_PATTERNS.md` §9).

Both ship with the agent: never put personal data in them.

## Scheduled Script Triggers — the "OK" Pattern

Complex agents often need to run lightweight checks on a schedule — monitor a mailbox, work a queue, compare DB counts — and only involve the conversation agent when something actually needs attention. The platform supports this via the **script trigger** schedule type.

### How script triggers work

When a script trigger fires, the platform executes a shell command inside the agent environment (working directory `/app/workspace/`) and inspects the output:

- **If `stdout.strip() == "OK"` AND exit code `0`** → silently logged, **no session created, no tokens spent**.
- **Anything else** (different stdout, non-zero exit code, empty stdout) → a new session is created, seeded with the command, its output, stderr, and exit code, plus the prompt *"Please review the output above and take appropriate action."*
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

OUT = Path("/app/workspace/app-data/storage/monitoring/overdue_invoices_latest.json")


def main():
    try:
        overdue = find_overdue_invoices()
    except Exception as exc:
        # Non-zero exit + stderr → platform creates a session with the error context
        print(f"check_unpaid_invoices failed: {exc}", file=sys.stderr)
        sys.exit(1)

    new = [inv for inv in overdue if not already_reported(inv)]  # escalate once
    if not new:
        print("OK")
        return

    # Non-OK path: concise, agent-readable summary
    print(f"Found {len(new)} newly overdue invoices that need follow-up:")
    for inv in new[:20]:
        print(f"- {inv['number']} | {inv['customer']} | {inv['amount']} | due {inv['due_date']}")
    if len(new) > 20:
        print(f"... and {len(new) - 20} more.")

    # Drop the full list on disk so the conversation agent can pick it up
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(new, indent=2))
    mark_reported(new)
    print(f"Full list: {OUT}")


def find_overdue_invoices():
    # ... query cache or source
    return []


def already_reported(invoice) -> bool:
    # ... look the invoice up in app-data/storage/monitoring/state.db
    return False


def mark_reported(invoices) -> None:
    # ... persist the reported flag so the next tick stays quiet
    pass


if __name__ == "__main__":
    main()
```

### Rules for OK-pattern scripts

- **Print exactly `OK`** (uppercase, no punctuation, no extra whitespace) on the happy path. A trailing newline is fine; extra characters are not.
- **Never print progress chatter** (`"Connecting..."`, `"Fetched 500 rows"`) on stdout on the happy path — it breaks the exact-match check. Informational lines go to stderr.
- **Invoke through `uv run --quiet`, not plain `uv run`.** uv prints resolver/sync progress to stdout the first time it resolves dependencies (e.g. after an environment rebuild); `--quiet` suppresses it so the happy-path output stays exactly `OK` and no session is spuriously created.
- **Keep non-OK output short and actionable.** The first line should summarize the situation ("3 overdue invoices found"), and the rest should give the conversation agent enough context to act. Big payloads belong in a file under `./app-data/storage/<area>/` — reference the path from the output.
- **Use `sys.exit(1)` + a stderr message for errors.** This still creates a session (with the error as context) and makes the failure visible in execution logs.
- **Escalate a condition once.** Persist that it was reported (a flag in the script's SQLite state) and print `OK` on later ticks until it changes — otherwise every tick opens a new session about the same thing.
- **Never print credential values or secrets.** Scheduled output is surfaced to the agent session and stored in execution logs.
- **Be fast and idempotent.** Commands time out at 120s by default and may run unattended for months — running the same check twice in a row must be safe. Claim queued work with the guarded update from Durable State.
- **Update `STATUS.md` at the end of every run** — on the success path and the failure path — through `scripts/update_status.py`, which writes only when severity or summary changed. See the [Agent Self-Reported Status](#agent-self-reported-status) section.
- **Work from cache when possible.** If the check involves a large dataset, run against `./app-data/cache/` and let a separate cache-update schedule handle refreshes. Do not push slow API calls into the minute-by-minute scheduler.

### The command to put in the schedule

The schedule's **command** field is a single line executed at `/app/workspace/`. For an OK-pattern script, use the one canonical form:

```
uv run --quiet python /app/workspace/scripts/monitoring/check_unpaid_invoices.py
```

For quick bash-only checks, a one-liner is fine too:

```
[ -s /app/workspace/app-data/cache/inbox/inbox.json ] && echo "OK" || echo "inbox cache missing"
```

Add `./workspace_requirements.txt` entries for any packages the script imports, so the dependency survives environment rebuilds (see the main building-mode prompt).

### Document every scheduled script

In `scripts/README.md`, mark scheduled-trigger scripts explicitly:

```markdown
## monitoring/check_unpaid_invoices.py
**Purpose**: Scheduled health check — reports newly overdue invoices that need follow-up.
**Scheduled trigger**: yes (OK-pattern — prints "OK" when nothing new is overdue).
**Usage**: `uv run --quiet python /app/workspace/scripts/monitoring/check_unpaid_invoices.py`
**Output**: stdout "OK" or short report; full list in `./app-data/storage/monitoring/overdue_invoices_latest.json`.
```

And mention the schedule in `WORKFLOW_PROMPT.md` so the conversation agent knows the context when a session is opened by the trigger:

```markdown
## Scheduled Checks

- `scripts/monitoring/check_unpaid_invoices.py` runs every 2 hours during business hours.
  When a session is created from this trigger, the seed message will contain the list of
  overdue invoices. Read `./app-data/storage/monitoring/overdue_invoices_latest.json` for
  the full payload and follow the `overdue-invoice-followup` skill.
```

Schedules are not part of the bundle: document the recommended ones (name, cadence, command) in the agent's `README.md` so installers can recreate them.

## External Side Effects — Gate, Dry-Run, Idempotency, Audit

Any script or endpoint that sends, creates, signs, posts, books or deletes something outside the platform follows six rules. They are what make "the schedule fired twice" and "the user said yes twice" harmless (`AGENT_DESIGN_PATTERNS.md` §6).

1. **Confirmation gate.** In conversation mode, show a short summary of exactly what will happen — recipients, document, amounts — and wait for an explicit yes before running the write. Mark the step **REQUIRED** in the skill or workflow prompt, and say it in the script's docstring: *"This WRITES to the live system. Only call it after the user confirms."*
2. **`--dry-run`.** The script validates and resolves everything and prints the normalized payload without calling upstream. Use it while building, and let the conversation agent use it to produce the confirmation summary.
3. **Job files, not inline JSON.** Multi-field input is written to `./app-data/storage/jobs/<name>.json`, scaffolded from a shipped template by `--init-job` (which refuses to overwrite). The runtime re-parses command-line arguments through a shell, which mangles quotes and spaces in inline JSON.
4. **Deterministic key.** Derive an idempotency key from what the action is about — `daily-summary-2026-09-14`, the document name, the source record id — never from the clock. Check it against the audit table before calling upstream.
5. **Audit every attempt** — success, repeat and failure — and return the stored result on a repeat:

   ```sql
   CREATE TABLE IF NOT EXISTS action_attempts (
       id             INTEGER PRIMARY KEY AUTOINCREMENT,
       action_key     TEXT NOT NULL,   -- deterministic key from rule 4
       target         TEXT NOT NULL,   -- recipient, folder, record
       outcome        TEXT NOT NULL,   -- done | already_done | failed
       upstream_id    TEXT,            -- what the external system called it
       error          TEXT,            -- failure text, never a secret
       caller_user_id TEXT,
       caller_email   TEXT,
       created_at     TEXT NOT NULL
   );
   ```

   A repeat returns `already_done: true` with the stored `upstream_id`; only an explicit `--force` (or `force=true` on an endpoint) performs the action again.
6. **Escalate once.** A condition that persists (a document overdue, a mailbox unreachable) is reported on the first tick and flagged, not re-reported every tick.

Scripts emit **facts** — the rows a user checks before approving (the "receipts"), counts, ids. The **wording** of a message lives in the skill or a template. When the model drafts text that contains facts (totals, counts), the finalizing script recomputes those facts from the data and ignores the draft's values.

## Agent Self-Reported Status

Complex agents can publish a lightweight status snapshot that surfaces in the `/agent-status` command, the REST API, and the dashboard tile — without starting a session or spending any tokens. The mechanism is a single file the agent maintains under the per-install **App Data** storage area.

### File location

```
/app/workspace/app-data/storage/STATUS.md
```

This lives under `app-data/`, **not** the bundle-owned `docs/` folder. Status reflects the current health of *this specific install* — it depends on the user's data, credentials, and runtime state — so it must persist across bundle updates and never be overwritten when the publisher pushes a new revision. App Data is the per-user, per-bundle persistent volume that survives apply-update and uninstall/reinstall; bundle folders (`docs/`, `scripts/`, `knowledge/`, `files/`) are replaced wholesale on update.

### Purpose

Write a brief description of what the agent is currently doing or how its last scheduled run went. The platform reads this file on demand and after every backend-triggered action in the env (session completion, CRON run) and caches the result. Users and A2A clients can query the status at any time — even when the environment is stopped.

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

**Overwrite in place — do not append.** Write the file only when the severity or summary actually changed; a run that finds the same state leaves it alone. That is a rule for the writer, not for its callers: every scheduled run calls the helper at its end, on the success and the failure path alike, and the helper compares. A check that only reports on success leaves a stale `ok` behind once it starts failing.

### Secret hygiene

Never write credential values, tokens, or API keys into `STATUS.md`. The file is readable by any user with dashboard access to the agent. Apply the same rule as for OK-pattern script output.

### Helper script

Keep the frontmatter, the atomic write and the change detection in one `scripts/update_status.py` so no script inlines them. It is not pre-shipped in every environment — check `scripts/README.md`, and if it is missing, write it: parse `--status`, `--summary` and an optional `--details`; write through a temp file and a rename so a reader never sees half a file; skip the write when status and summary are unchanged; and expose a `trigger_quiet()` function that never raises and never writes to stdout, so an OK-pattern script can call it without breaking its `OK`. Document it in `scripts/README.md`.

```
uv run --quiet python /app/workspace/scripts/update_status.py --status ok --summary "All monitors green"
```

### The `/run:status` refresh convention

Every agent has a configurable **status-refresh command** that the platform runs inside the container right before a forced/live `/agent-status` fetch — so the snapshot a user, A2A client, or dashboard tile sees is freshly recomputed rather than stale. **Its default value is `/run:status`**: i.e. the platform expects a CLI command named `status` declared in `CLI_COMMANDS.yaml` (see [Exposed CLI Commands](#exposed-cli-commands)). Because a `/run:<name>` command executes as a plain shell command with **no LLM turn**, it is safe to invoke from cron / scheduled contexts and never starts a session.

To honour this default, expose a `status` command that recomputes the agent's health and refreshes `STATUS.md`:

```yaml
commands:
  - name: status
    description: Recompute and publish the current agent status
    command: uv run --quiet python /app/workspace/scripts/health_check.py
```

where `health_check.py` checks the agent's state mechanically — no LLM, no slow network calls — and calls `update_status.py` (or writes `STATUS.md` directly) to publish the result.

- **Keep the name exactly `status`** unless the owner has changed the configured status-refresh command — the platform looks for `/run:status` by default.
- **Run it through `uv run --quiet` and keep stdout clean** — the same cron-safety rules apply (see [Exposed CLI Commands → Keep output quiet](#keep-output-quiet--so-commands-are-cron--and-status-safe)).
- If you do **not** expose a `status` command, the refresh step is simply a no-op and `/agent-status` reads the last-written `STATUS.md` as-is.

---

## Exposed CLI Commands

Agents can expose a small set of named shell commands that users and A2A clients can run directly — without spending tokens on an LLM turn. These commands power the `/run:<name>` slash command in chat, surface as A2A skills in the agent card, and appear in the autocomplete popup.

### File location

```
/app/workspace/docs/CLI_COMMANDS.yaml
```

### Purpose

Declare deterministic operations the agent owner wants to be callable on demand: monthly checks, report generation, cache refreshes, reindex jobs. The platform reads this file when the environment starts, refreshes after each backend-triggered action, and caches the parsed list. Users invoke commands via `/run:<name>`; A2A clients discover them as `cinna.run.<name>` skills on the agent card; both paths execute the same shell string inside this environment with no LLM involvement.

### Format

```yaml
commands:
  - name: check                      # required, slug [a-z][a-z0-9_-]{0,31}
    description: Monthly data check  # optional, 1–512 chars
    command: uv run --quiet python /app/workspace/scripts/check_data.py --month  # required, single-line shell string
```

- `name` — identifier the user types after `/run:`. Must be unique.
- `description` — one-line explanation shown in autocomplete tooltips, the `/run` listing, and the A2A skill description. Be concrete about what the command does and when to use it.
- `command` — the shell command to execute inside this environment. Write it exactly as you would at the shell prompt. No shell expansion tricks — keep it one line.

Unknown top-level keys and unknown per-command keys are ignored, so the platform can add fields (`tags`, `timeout`, etc.) in the future without breaking existing files.

### When to maintain this file

- Add an entry whenever you write a script or one-liner the user (or a caller) should be able to trigger directly — treat it as the public CLI for this agent.
- Remove entries when you remove or rename the underlying script.
- Update the description when a command's behaviour changes materially.

### Security hygiene

Commands run with the agent's full environment access. Do not declare commands that:
- Accept raw input from the user (the `command` string is fixed; users cannot pass arguments in MVP).
- Leak secrets to stdout.
- Perform an external side effect without the guards in [External Side Effects](#external-side-effects--gate-dry-run-idempotency-audit) — a `/run:` command has no conversation in which to confirm.

The resolved `command` string is visible in the A2A agent card description and the UI tooltip — do not embed credentials.

### Keep output quiet — so commands are cron- and status-safe

The shell string you declare here rarely stays in chat only. The platform reuses the exact same command when you wire it to a **scheduled script trigger** (the "OK" pattern above), set it as the agent's **status-refresh command** (run inside the container before every `/agent-status` fetch), or when an A2A client invokes it. On the script-trigger path the platform inspects stdout and **creates a new agent session — spending tokens — whenever stdout is not exactly `OK`**. Tooling chatter on stdout breaks that match and spawns sessions that were never needed.

So author every command to produce deterministic, chatter-free stdout:

- **Use `uv run --quiet`, never plain `uv run`.** uv writes resolver/sync progress (`Resolved N packages`, `Installed …`, download bars) to the stream the first time it has to resolve or sync dependencies — e.g. right after an environment rebuild. `--quiet` suppresses it so that noise never leaks into `/run:<name>` output, `/agent-status` snapshots, or OK-pattern matching.
- **Print only what the caller needs.** A command meant to be a scheduled/status check must print exactly `OK` on the happy path (see [Scheduled Script Triggers](#scheduled-script-triggers--the-ok-pattern)). Route progress and debug lines to stderr, not stdout.
- **Keep commands non-interactive and idempotent** — they may run unattended on a schedule and must be safe to run twice in a row.

```yaml
commands:
  - name: status
    description: Health check — prints OK when all monitors are green
    command: uv run --quiet python /app/workspace/scripts/health_check.py
```

> The `status` command above is special: `/run:status` is the platform's **default status-refresh command**, run before every live `/agent-status` fetch. See [Agent Self-Reported Status → The `/run:status` refresh convention](#the-runstatus-refresh-convention).

### Helper

Prefer writing scripts under `/app/workspace/scripts/` and referencing them by path in `command:`. This keeps the YAML readable and centralises logic.

### Example

See `/app/workspace/docs/CLI_COMMANDS.yaml` — a starter file is shipped with the environment.

---

## Documentation Sync (non-negotiable)

Every time scripts or skills are added, modified, or removed, update in the same session:

- **`./scripts/README.md`** — script catalog (purpose, usage, key args, output), grouped by folder, including scripts inside `skills/`.
- **`./docs/WORKFLOW_PROMPT.md`** — scope, the routing table, scheduled-check entries, config references.
- **`./skills/<name>/SKILL.md`** — if the change touches a skill's command, output or presentation.
- **`./docs/REFINER_PROMPT.md`** — if the change added a new required parameter or a sensible default.
- **`./docs/AGENT_DEVELOPMENT.md`** — the decision behind the change, and a defects-log entry when it fixed one.
- **`./docs/test_scenarios/`** — add or adjust the affected cases, then re-run the set.

Outdated docs make the agent give wrong instructions and users run stale commands. Treat doc updates as part of the same change, not as follow-up.

## Checklist for a Complex Agent

1. List the kinds of request the agent answers — one skill each — before writing any script. Give `WORKFLOW_PROMPT.md` a scope section and a routing table.
2. Write each `SKILL.md`: a `description` with trigger phrases and the "Not for …" boundary; a body with command, reading, presentation and failures; a closing pre-answer reminder.
3. Put skill-only scripts in the skill's `scripts/` and shared helpers in `./scripts/`; give each kind of question one entry-point script that prints the whole, pre-computed answer. Keep `./scripts/README.md` as the single catalog.
4. For every large/slow dataset, split into cache-update and processing scripts under `./app-data/cache/<area>/`; write `.last_updated`; support `--fresh`; document freshness rules.
5. Keep queues, "already sent" records and retry state in SQLite under `./app-data/storage/<area>/`, with guarded claims, an events table and additive migrations.
6. Give every external side effect a confirmation gate, `--dry-run`, a deterministic key, an audit row per attempt, a `force` override and escalate-once.
7. Move tunable parameters into `./config/` (per-install overrides under `./app-data/storage/config/`); reference them from `WORKFLOW_PROMPT.md`.
8. Write preserved results to `./app-data/storage/<area>/` with descriptive, dated filenames.
9. Put domain knowledge into `./docs/<topic>.md` (or `./knowledge/`) and reference it from the skills that need it.
10. Persist every integration-specific Python package via `./workspace_requirements.txt`.
11. For scheduled monitoring, write OK-pattern scripts invoked as `uv run --quiet python /app/workspace/scripts/...`, at least 30 minutes apart, updating `STATUS.md` at the end of every run and escalating each condition once.
12. Ship `./docs/AGENT_DEVELOPMENT.md` (decisions, invariants, defects log) and — before anyone else uses the agent — `./docs/test_scenarios/`, re-run after every prompt, skill, model or provider change.
13. Keep all three prompt files (`WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, `REFINER_PROMPT.md`) in sync with the current capabilities.

## Pointers

- Basic building-mode rules (scripts location, credentials handling, `uv`, `./workspace_requirements.txt`, prompt file formats, the `SKILL.md` contract) → main building-mode prompt.
- What to recommend to the user and why, one section per pattern → `/app/core/prompts/AGENT_DESIGN_PATTERNS.md`.
- A narrow producer API in front of an external credential, and response design → `/app/core/prompts/REST_API_BUILDING.md`.
- Web apps and dashboards → `/app/core/prompts/WEBAPP_BUILDING.md`.
