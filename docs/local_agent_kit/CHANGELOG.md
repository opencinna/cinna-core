# Kit changelog

Conventions that changed between kit versions. Read this after every
`kit.py refresh` that reports a new version. Newest entry first.

Your current kit is version `{{KIT_VERSION}}`.

## Unreleased — manifest `schema_version` 1

First published kit.

- **`uv` is the kit runtime.** Setup checks for `uv` and installs it when missing;
  the kit tool is always invoked as `uv run .cinna-kit/tools/kit.py …` (it carries
  PEP 723 inline metadata, `requires-python >= 3.10`), so the macOS system Python
  3.9 is never a blocker. A bare `python3` below 3.10 now prints the `uv` fix
  instead of a dead end.
- `cinna-agent.json` **`schema_version` is `1`**. Every agent scaffolded by this
  kit carries it, and `kit.py` refuses a manifest whose `schema_version` is higher
  than the one it understands.
- Local agent layout mirrors the cloud workspace one-for-one: `docs/`, `scripts/`,
  `knowledge/`, `files/`, `config/`, `credentials/`, `app-data/{storage,cache,uploads}/`.
- Prompts live in `docs/WORKFLOW_PROMPT.md`, `docs/ENTRYPOINT_PROMPT.md` and
  `docs/REFINER_PROMPT.md` — the same three files the platform reconciles.
- `docs/CLI_COMMANDS.yaml` is cloud-first: commands use `python scripts/x.py` with
  paths relative to the workspace root. The `Makefile` mirrors each command with
  `uv run` for local use.
- `scripts/cinna_credentials.py` is the portability shim: `credentials.json` in the
  cloud, `credentials/.env` locally, one `get_credential()` call either way.
- `scripts/update_status.py` writes `app-data/storage/STATUS.md` atomically with
  YAML frontmatter (`status`, `summary`, `timestamp`).
- The capability ladder in `README.md` is the discovery mechanism; nothing is added
  to an agent until its trigger fires.
- Ignore rules that exclude paths ship as dotless `gitignore` files
  (`templates/agent/gitignore`, `templates/agent/app-data/cache/gitignore`,
  `templates/root/gitignore`). `kit.py new` restores the dot in the created agent;
  when you install `templates/root/` by hand, copy `gitignore` to
  `<root>/.gitignore`. (Shipping them dotted would make them live ignore rules
  wherever the kit is stored, hiding scaffold files from that repository.)
  `credentials/.gitignore` keeps its dot — it names files no repository should track.

## How to read a future entry

Each release lists, in this order:

1. **Breaking** — a convention that makes an existing agent invalid. `kit.py validate`
   will flag it; the entry says how to migrate.
2. **Added** — new optional artefacts. Existing agents keep working untouched.
3. **Changed** — wording, defaults, guide reorganisation. No action needed.

A bump of `schema_version` is always a Breaking entry.
