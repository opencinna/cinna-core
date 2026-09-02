# Config — {{name}}

User-editable parameters. Committed to git: this folder represents the agent's
current operational configuration, and in the cloud it is bundle-owned — shipped
with the agent and replaced on update.

Anything a user might reasonably want to change without editing code belongs here:

- date ranges and cut-offs
- thresholds ("flag anything above 10000")
- exclusion lists (accounts, senders, test records to skip)
- feature toggles
- mappings and display names

Pick the simplest format: JSON for mixed structured settings, CSV for flat lists,
YAML when a human edits it often.

```
config/
├── settings.json      # thresholds, date ranges, toggles
└── exclusions.csv     # ids to skip
```

Scripts load config at startup; they never hardcode a value that belongs here:

```python
import json
from pathlib import Path

settings = json.loads((Path(__file__).resolve().parent.parent / "config" / "settings.json").read_text())
threshold = settings["threshold"]
```

Document each config file in `docs/WORKFLOW_PROMPT.md` so the agent knows to edit
the file rather than the code when the user asks to change a parameter.

Never store a secret here. Secrets go to `credentials/.env`.
