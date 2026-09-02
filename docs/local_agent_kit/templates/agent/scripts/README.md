# Scripts catalog — {{name}}

**This file is mandatory and must never fall behind reality.** Every time a script
is added, renamed, changed or removed, update the matching entry here in the same
change. An out-of-date catalog makes the agent give wrong instructions.

Run everything from the agent root:

```bash
uv run scripts/<script>.py [args]
```

In the cloud the same scripts run as `python scripts/<script>.py` with the workspace
root as the working directory — which is why no script may use an absolute path.

## Shipped helpers

### `cinna_credentials.py`

Portability shim for credentials. Import it; do not read credential files directly.

```python
from cinna_credentials import require_credential
config = require_credential("email_imap")
```

Reads the platform-injected `credentials/credentials.json` when it exists (cloud),
otherwise assembles the values from `credentials/.env` using the `env_prefix` and
`fields` declared for that slot in `cinna-agent.json` (local). Never prints anything.

### `update_status.py`

Writes `app-data/storage/STATUS.md` atomically with `status`, `summary` and
`timestamp` frontmatter.

```bash
uv run scripts/update_status.py --status ok --summary "All clear"
```

Severity is one of `ok`, `info`, `warning`, `error`. STATUS.md is a public artefact:
never put a secret or a personal identifier in the summary or details.

## Agent scripts

<!-- One subsection per script you add. Keep this template shape: -->

<!--
### `<script_name>.py`

**Purpose**: one line.
**Usage**: `uv run scripts/<script_name>.py --<arg> <value>`
**Reads**: config/settings.json, credential slot `<name>`
**Writes**: `app-data/storage/<file>` (format)
**Output**: what it prints to stdout, in what format.
-->

_No agent scripts yet._

## Conventions

- One step per script. If a script needs a paragraph to explain, split it.
- Scripts take arguments; they do not hardcode values that belong in `config/`.
- Large results are passed between scripts through files in `app-data/storage/`,
  not through stdout.
- Paginated fetching always specifies a deterministic sort order.
- Shared helpers stay at the top level of `scripts/`; scripts belonging to one local
  skill go into `scripts/<skill>/`.
