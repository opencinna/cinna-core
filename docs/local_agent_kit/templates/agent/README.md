<!-- cinna-kit:scaffold-note
This file came from the kit scaffold. Seven double-brace placeholders are
written inside `templates/agent/`: NAME, SLUG, DESCRIPTION, ID,
CONTRACT_VERSION, CREATED_AT and KIT_VERSION. They are UPPER_SNAKE, the same
shape as the platform-rendered tokens (PLATFORM_URL, INSTANCE_NAME and
friends), so shape never separates the two classes — the lists do, except for
KIT_VERSION, which is on both and which the lists therefore cannot settle. What
settles it is where the kit came from: the platform substitutes KIT_VERSION
across every file when it renders a kit for download, and `kit.py new` fills it
only in a kit that was never rendered. Any kit you downloaded is a rendered one,
so there KIT_VERSION is already a value and only the other six are still waiting
for the scaffolder.
Nothing here should still be in braces by the time you read this. Delete this
comment block.
-->

# {{NAME}}

One paragraph: what this agent does and who it is for. Rewrite it once the agent
actually works.

## Install

```bash
cd Local/{{SLUG}}
cp credentials/.env.example credentials/.env
# fill in credentials/.env — see credentials/README.md for what each value is
uv sync
```

`credentials/.env` is git-ignored and never leaves this machine.

## Usage

Talk to the agent through your coding assistant from inside this folder, or run the
commands directly:

```bash
make help
```

| Command | What it does |
|---------|--------------|
| `make status` | Refresh `app-data/storage/STATUS.md`. |
| `make validate` | Check the agent against the kit conventions. |

## Layout

| Folder | Contents |
|--------|----------|
| `docs/` | Prompts, `CLI_COMMANDS.yaml`, domain documentation. |
| `skills/` | One folder per capability. See `skills/README.md`. |
| `scripts/` | The Python that does the work. See `scripts/README.md`. |
| `config/` | Tunable parameters you may edit. |
| `knowledge/` | Static reference material. |
| `files/` | Static assets shipped with the agent. |
| `credentials/` | `.env` (git-ignored) and a redacted `README.md`. |
| `app-data/` | Runtime output, caches and uploads. Not part of the definition. |

## Scripts

See `scripts/README.md` — it is kept in sync with every script in this folder.
