# Skills — {{NAME}}

One folder per skill. A **skill** is one standalone capability of this agent —
employee time-off analysis, cost reports, invoice matching — packaged so the
engine can load it *only when it is invoked* rather than carrying it in the
prompt all the time.

```
skills/
├── README.md               # this file
├── timeoff-check/
│   ├── SKILL.md            # required — frontmatter + instructions
│   └── scripts/
│       └── check.py
└── cost-reports/
    ├── SKILL.md
    ├── references/
    │   └── cost_centres.md
    └── assets/
        └── report_template.xlsx
```

`SKILL.md` is the only required file. `scripts/`, `references/` and `assets/`
are optional and mean what their names say: things the model runs, things it
reads on demand, things it hands over or fills in.

## SKILL.md

```markdown
---
name: timeoff-check
description: Check and verify an employee's time-off balance and history. Use when the user asks to check, verify or explain time off for a named person.
---

# Time-off check

## When to use
...

## Workflow
1. `python skills/timeoff-check/scripts/check.py --employee "<name>"`
2. ...

## How to present results
...

## Technical notes
...
```

Two frontmatter fields are required and validated:

| Field | Rule |
|-------|------|
| `name` | lowercase letters, digits and single hyphens (`^[a-z0-9]+(-[a-z0-9]+)*$`), 1–64 characters, and it **must equal the folder name**. |
| `description` | 1–1024 characters. It is the only thing the engine sees before it decides to open the skill, so say *what it does* **and** *when to use it*. |

Everything else you write in the frontmatter is passed through untouched
(`allowed-tools`, `disable-model-invocation`, `user-invocable`, `argument-hint`,
`model`, `license`, …). Keep the body under 64 KB — move the long material into
`references/` and point at it, which is the whole point of the folder.

A skill name must not collide with a platform command name: `files`, `files-all`,
`run`, `run-list`, `skills`, `session-recover`, `session-reset`, `session-improve`,
`webapp`, `rebuild-env`, `agent-status`.

## Limits

- 50 skills per agent.
- 16 MB total across `skills/`.

Past either limit the extra skills are dropped, not truncated — so keep large
fixtures in `files/` and large outputs in `app-data/storage/`.

## What goes where

- **A skill** (`skills/<name>/`) — a capability with its own trigger, workflow and
  output format, that a user could ask about in isolation.
- **Knowledge** (`knowledge/`) — reference material the agent reads to be correct.
  Not a capability, so not a skill.
- **A script** (`scripts/`) — shared helpers and anything a skill does not own.
  Scripts used by exactly one skill may live inside that skill's `scripts/`.
- **The workflow prompt** (`docs/WORKFLOW_PROMPT.md`) — the orchestration
  narrative. It names skills and says when each applies; it never repeats their
  instructions.

Never put a credential, a tokenised URL or personal data in a skill. `skills/`
ships with the agent and travels to the cloud.

Add this folder's contents only when the ladder's **Knowledge & local skills**
trigger has fired; for *this* folder that means three or more distinct capabilities.
The rung's full trigger is in the ladder (`.cinna-kit/README.md`); the guide is
`.cinna-kit/guides/08-knowledge-and-local-skills.md`, which also covers publishing a
finished skill to the catalog.
