# 03 — Scripts and data

## Read this when

The agent must do anything beyond answering from its prompt: fetch, parse, compute,
compare, export. Read it before you write the first script.

## One step per script

Never write a long script that does everything. Split the workflow into
single-purpose scripts that take arguments and can be composed.

*"Get time-off details and book a vacation"* is two scripts:
`get_timeoff_details.py` and `book_vacation.py`.

Each script:

- does one step and exits;
- takes parameters via `argparse` — no hardcoded values that belong in `config/`;
- prints a machine-readable result (JSON or CSV) for small data;
- writes a file under `app-data/storage/` for large data, and prints the path.

Run them with `uv run scripts/<x>.py` locally. In the cloud the same file runs as
`python scripts/<x>.py` with the workspace root as the working directory — so
**never use an absolute path** and never assume a virtualenv.

## Passing data between scripts

| Data size | How |
|-----------|-----|
| Small (ids, counts, a single value) | Command-line arguments, and stdout you read in the conversation. |
| Large (records, parsed results, tables) | A CSV/JSON file in `app-data/storage/`; script 2 takes `--input <path>`. |

```
parse_invoices.py           -> app-data/storage/invoices.csv
process_invoices.py --input app-data/storage/invoices.csv -> app-data/storage/flagged.json
```

Never pipe thousands of rows through the conversation. It is slow, lossy and
expensive.

## Where things go

| Folder | Owner | Contents | Survives a cloud update |
|--------|-------|----------|-------------------------|
| `scripts/` | author | Python that does the work | replaced on update |
| `docs/` | author | prompts, `CLI_COMMANDS.yaml`, domain docs | replaced on update |
| `knowledge/` | author | static reference material, read-only at runtime | replaced on update |
| `files/` | author | **static shipped assets**: lookup tables, fixtures, sample data | replaced on update |
| `config/` | author | user-tunable parameters | replaced on update |
| `app-data/storage/` | runtime | results the agent produces, `STATUS.md` | yes, per install |
| `app-data/cache/` | runtime | disposable, rebuildable snapshots | yes, per install |
| `app-data/uploads/` | runtime | files the user attached | yes, per install |

The rule that matters: **`files/` is what the author ships; `app-data/storage/` is
what the agent produces.** Writing runtime output to `files/` means it is silently
replaced the next time the agent is updated in the cloud.

## Config, not constants

Anything a user might want to change without editing code goes in `config/`: date
ranges, thresholds, exclusion lists, toggles, mappings.

```python
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
threshold = settings["threshold"]
```

Choose JSON for mixed settings, CSV for flat lists, YAML when a human edits it
often. Document each file in `config/README.md` and mention it in
`docs/WORKFLOW_PROMPT.md`, so the agent edits the config file rather than the code
when the user asks for a different threshold.

## Dependencies

`uv` handles everything; there is no manual install step.

```bash
uv add requests          # updates pyproject.toml and uv.lock
uv run scripts/fetch.py  # creates .venv on first use
```

Prefer the standard library. Every dependency you add must also appear in
`workspace_requirements.txt`, which is the list the cloud installs —
`kit.py validate --fix` regenerates it from `pyproject.toml`.

## Caching large data sets

Add `app-data/cache/` usage only when fetching is slow or expensive, the same data
is read repeatedly, or the agent compares current state against a snapshot.

**Cache must be restorable.** Deleting the whole cache and re-running the update
command must produce the same result. Therefore:

- cache-update scripts **replace** data; they never patch individual records;
- no processing script depends on a particular previous cache state;
- recovery from a corrupt cache is exactly one command.

Split the workflow into two kinds of commands: **cache-update** (fetch source →
write cache) and **processing** (read cache → produce results).

**Format:** CSV when post-processing is "read everything and iterate". SQLite when
it involves filtering, joining, grouping or sorting across entities. Start with CSV;
migrate later if queries grow.

**Freshness:** write `app-data/cache/.last_updated` (ISO timestamp) after every
successful update, and state the rule in `docs/WORKFLOW_PROMPT.md` — for example:
*"If `.last_updated` is missing or older than 1 hour, run the cache update first. If
the user says 'refresh', always re-fetch."*

## Pagination: always specify a sort order

When fetching in offset/limit batches, **always request a deterministic order**,
normally `id ASC`. Without it the source may return rows in an unstable order: rows
shift between pages, some are skipped and others appear twice. The total count looks
right and the data is wrong — a very expensive bug to find later.

```python
# Wrong — default order may be unstable across pages
rows = api.search_read(domain, fields, limit=500, offset=offset)

# Right — every row appears exactly once
rows = api.search_read(domain, fields, limit=500, offset=offset, order="id ASC")
```

If the API has no sort parameter, fetch all ids first and then retrieve records by
id in batches.

## Keep the catalog honest

`scripts/README.md` documents every script: purpose, usage, what it reads, what it
writes, what it prints. Update it **in the same change** that adds or alters a
script. This is not optional — a stale catalog makes the agent hand out commands
that do not exist.

## Done when

- Every script does one step, takes arguments and uses only relative paths.
- Large results move between scripts through files in `app-data/storage/`.
- Nothing the agent produces at runtime is written to `files/` or `docs/`.
- Tunable values live in `config/` and are read at startup.
- Every new dependency is in both `pyproject.toml` and `workspace_requirements.txt`.
- Every paginated fetch specifies an explicit sort order.
- If a cache exists: it is fully rebuildable, has a `.last_updated` file, and
  `docs/WORKFLOW_PROMPT.md` states the freshness rule.
- `scripts/README.md` lists exactly the scripts that exist.
