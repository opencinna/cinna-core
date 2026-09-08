# 08 — Knowledge and local skills

## Read this when

The agent has three or more distinct capabilities, or needs domain documentation
longer than a page. Also read it when `docs/WORKFLOW_PROMPT.md` has grown into a wall
of text nobody can follow.

## Two different things

**Knowledge** is reference material the agent reads to be *correct*: business rules,
how an external system really behaves, terminology, decision rationale. It lives in
`knowledge/` and is read-only at runtime.

**A local skill** is one standalone capability of the agent — employee time-off
analysis, cost reports, invoice matching. It is a folder under `skills/` holding a
`SKILL.md` and whatever that capability needs. "Local" distinguishes it from your
assistant's own built-in skills; a local skill belongs to this agent and travels
with it.

The reason a skill is a folder and not a paragraph in the prompt: **in the cloud**
the engine reads only the *name and description* of every skill up front and loads a
body when that skill is actually needed. The workflow prompt stays short, and the
detail arrives the moment it matters.

**Locally that is instruction, not machinery.** Nothing on your machine registers
`skills/` with your coding assistant — the folder earns the same behaviour because
`AGENTS.md` tells the assistant to open `skills/<name>/SKILL.md` when the workflow
prompt names a skill. Same layout, same files, same result; one is the engine's doing
and the other is a rule the assistant follows. Write skills for the cloud shape and
both work.

## Knowledge

```
knowledge/
├── invoice_matching_rules.md
└── vendor_portal/
    ├── api_quirks.md
    └── field_mapping.md
```

Use it for the *why* and the *how it really works*, not for instructions. Reference
each topic from `docs/WORKFLOW_PROMPT.md` so the agent knows where to look:

```markdown
## References
- `knowledge/invoice_matching_rules.md` — how a PO number is matched to an invoice.
```

Never put a credential, a tokenised URL or personal data in `knowledge/`. It ships
with the agent and, in the cloud, may travel to other installs.

## Local skills

One folder per skill, under the agent's top-level `skills/`:

```
skills/
├── README.md
├── timeoff-check/
│   ├── SKILL.md
│   └── scripts/
│       └── check_employee_timeoff.py
├── cost-reports/
│   ├── SKILL.md
│   └── references/
│       └── cost_centres.md
└── data-management/
    └── SKILL.md
```

`SKILL.md` is the only required file. `scripts/`, `references/` and `assets/` are
optional: things the model runs, things it reads on demand, things it hands over or
fills in.

### SKILL.md

```markdown
---
name: timeoff-check
description: Check and verify an employee's time-off balance and history. Use when the user asks to check, verify or explain time off for a named person.
---

# Time-off check

## When to use
The user names an employee and asks about time off, holiday balance or absences.

## Workflow
1. `python skills/timeoff-check/scripts/check_employee_timeoff.py --employee "<name>"`
2. Read the JSON it prints; `balance_days` below 0 means an over-booking.
3. Cross-check `pending[]` against the approval list before reporting.

## How to present results
A short table: period, days, status. Flag anything over-booked in the first line.

## Technical notes
Source is the HR portal export; see `knowledge/hr_portal/field_mapping.md`.
```

The body keeps the same four sections the agent needs from any capability: **When to
use** (the trigger), **Workflow** (numbered steps, scripts, arguments, checks), **How
to present results**, **Technical notes** (data sources, edge cases, limits).

### The two validated fields

| Field | Rule |
|-------|------|
| `name` | `^[a-z0-9]+(-[a-z0-9]+)*$`, 1–64 characters, and it **must equal the folder name**. `skills/timeoff-check/SKILL.md` declares `name: timeoff-check` or it is rejected. |
| `description` | 1–1024 characters. It is the *only* thing read before the skill is opened, so it must say what the skill does **and** when to use it. |

Everything else in the frontmatter is passed through untouched — `allowed-tools`,
`disable-model-invocation`, `user-invocable`, `argument-hint`, `model`, `license`
and friends. Keep the body under 64 KB; longer material belongs in `references/`.

A skill name must not collide with a platform command name: `files`, `files-all`,
`run`, `run-list`, `skills`, `session-recover`, `session-reset`, `session-improve`,
`webapp`, `rebuild-env`, `agent-status`.

### Limits

- **50 skills** per agent.
- **16 MB** total across `skills/`.

Past either limit the extra skills are dropped, not truncated. Large fixtures belong
in `files/`, large outputs in `app-data/storage/`.

### Referencing a skill from the workflow prompt

The workflow prompt names the skill and its trigger. It never repeats the skill's
instructions — that duplication is exactly what the folder exists to remove:

```markdown
### Time-off check (user says "check timeoff of …")

Use the `timeoff-check` skill.
```

**The pattern:** the workflow prompt says *when*; `SKILL.md` says *how*.

### When to split

| Make it a skill | Keep it inline |
|-----------------|----------------|
| Has its own scripts, multi-step workflow, its own checks and output format | A single command with obvious output |
| A user could ask about it in isolation | Only meaningful as part of another flow |
| Needs more than 5–10 lines to describe | Fits in one line |

### The legacy form: `docs/skill_<name>.md`

Kits before contract 1.1.0 taught one *doc* per skill, under `docs/`:

```
docs/
├── WORKFLOW_PROMPT.md
└── skill_timeoff_check.md
```

**Those agents keep working.** The docs are still shipped, still imported to the
cloud, and still read by the agent when the workflow prompt points at them — nothing
about that path was removed. What they do not get is the cloud engine's own
progressive disclosure: a `docs/skill_*.md` is opened because the prompt told the
agent to open it, never because the engine offered it by name. Locally the two forms
behave the same, which is why this is a migration you do when you next touch the
capability and not a reason to stop what you are doing.

Migrate one when you next touch it, and not before:

1. `mkdir -p skills/<name>` — the folder name is the skill name, hyphenated
   (`skill_timeoff_check.md` → `timeoff-check`).
2. Move the doc to `skills/<name>/SKILL.md` and add the `name` / `description`
   frontmatter.
3. Move the scripts only that skill uses into `skills/<name>/scripts/` and fix their
   paths in `docs/CLI_COMMANDS.yaml`, the `Makefile` and `scripts/README.md`.
4. Replace the workflow prompt's block with the two-line trigger above.
5. `kit.py validate .`

Do not do half of it. A skill split across `docs/skill_x.md` and `skills/x/SKILL.md`
is two sources of truth for one capability, and the agent will find whichever it
reads first.

## Organising scripts

Once there are three or more skills with their own scripts:

```
scripts/
├── README.md              # documents ALL scripts, including the ones inside skills/
├── cinna_credentials.py   # shared helpers stay here
└── odoo_utils.py

skills/
├── timeoff-check/
│   └── scripts/
│       └── check_employee_timeoff.py
└── reports/
    └── scripts/
        └── report_costs.py
```

- Shared utilities stay in `scripts/` at the top level, so every skill imports one copy.
- A script used by exactly one skill lives in that skill's `scripts/`.
- A script that belongs to no skill stays in `scripts/`.
- Commands and Makefile targets use the full path from the agent root:
  `python skills/timeoff-check/scripts/check_employee_timeoff.py`.
- Scripts are always run from the agent root, so top-level helpers stay importable.

Keep it flat below ~8 scripts or when the boundaries are not crisp. Premature
organisation adds friction without clarity. It is fine to start flat and reorganise
later — when you do, update every path in `docs/WORKFLOW_PROMPT.md`,
`scripts/README.md`, `README.md`, `Makefile`, `docs/CLI_COMMANDS.yaml` and every
`SKILL.md`, in the same change.

## Done when

- `docs/WORKFLOW_PROMPT.md` fits on a screen or two and delegates the rest.
- Every skill is a folder under `skills/` whose name matches its `SKILL.md` `name`.
- Every `description` says what the skill does and when to use it, in one sentence.
- Every `SKILL.md` body covers When to use / Workflow / Presentation / Notes.
- No skill's full instructions are duplicated in the workflow prompt.
- No capability lives in both `docs/skill_*.md` and `skills/`.
- Domain knowledge lives in `knowledge/` and is referenced, not inlined.
- If scripts are foldered, `scripts/README.md` is grouped the same way and every
  path in every file points at the real location.
- No secret or personal data anywhere under `knowledge/`, `docs/` or `skills/`.
