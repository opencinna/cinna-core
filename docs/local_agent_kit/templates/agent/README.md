<!-- cinna-kit:scaffold-note
This file came from the kit scaffold. `{{name}}` and `{{slug}}` are the only two
placeholders the scaffolder substitutes, and they are used only inside
`templates/agent/`. No other double-brace token appears in this folder except the
platform-rendered ones (`{{KIT_VERSION}}` and friends), which the platform has
already replaced by the time you read this. Delete this comment block.
-->

# {{name}}

One paragraph: what this agent does and who it is for. Rewrite it once the agent
actually works.

## Install

```bash
cd Local/{{slug}}
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
| `scripts/` | The Python that does the work. See `scripts/README.md`. |
| `config/` | Tunable parameters you may edit. |
| `knowledge/` | Static reference material. |
| `files/` | Static assets shipped with the agent. |
| `credentials/` | `.env` (git-ignored) and a redacted `README.md`. |
| `app-data/` | Runtime output, caches and uploads. Not part of the definition. |

## Scripts

See `scripts/README.md` — it is kept in sync with every script in this folder.
