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
analysis, cost reports, invoice matching. It is defined by a doc file describing its
workflow, plus the scripts it uses. "Local" distinguishes it from your assistant's
own built-in skills or slash commands; a local skill exists only inside this agent.

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

Keep `docs/WORKFLOW_PROMPT.md` lean by delegating detail to one doc per skill:

```
docs/
├── WORKFLOW_PROMPT.md
├── skill_timeoff_check.md
├── skill_cost_reports.md
└── skill_data_management.md
```

Each skill doc covers, in this order:

1. **When to use** — the trigger phrases or user intents that activate it.
2. **Workflow** — numbered steps: which scripts, which arguments, how to read the
   output, which checks to perform.
3. **How to present results** — what to tell the user and in what format, what to flag.
4. **Technical notes** — data sources, calculation logic, edge cases, known limits.

Reference it from `docs/WORKFLOW_PROMPT.md` with a trigger and a one-line quick
reference — never the full instructions:

```markdown
### Time-off check (user says "check timeoff of …")

When the user asks to check or verify time-off data for an employee, read
`docs/skill_timeoff_check.md` for the full workflow and how to present results.

Quick reference: `python scripts/timeoff/check_employee_timeoff.py --employee "<name>"`
```

**The pattern:** the workflow prompt says *when* and *where to look*; the skill doc
says *how*.

### When to split

| Make it a local skill | Keep it inline |
|-----------------------|----------------|
| Has its own scripts, multi-step workflow, its own checks and output format | A single command with obvious output |
| A user could ask about it in isolation | Only meaningful as part of another flow |
| Needs more than 5–10 lines to describe | Fits in one line |

## Organising scripts by skill

Once there are three or more skills with their own scripts, use subfolders:

```
scripts/
├── README.md              # documents ALL scripts, grouped by skill
├── cinna_credentials.py   # shared helpers stay at the top level
├── odoo_utils.py
├── timeoff/
│   ├── check_employee_timeoff.py
│   └── timeoff_overview_report.py
└── reports/
    ├── report_costs.py
    └── report_headcount.py
```

- Shared utilities stay at the top level of `scripts/`.
- Skill scripts go in a short, descriptively named subfolder.
- Standalone one-offs that belong to no skill may stay at the top level.
- Commands and Makefile targets use the full path: `python scripts/timeoff/check.py`.
- Scripts are always run from the agent root, so top-level helpers stay importable.

Keep it flat below ~8 scripts or when the boundaries are not crisp. Premature
organisation adds friction without clarity. It is fine to start flat and reorganise
later — when you do, update every path in `docs/WORKFLOW_PROMPT.md`,
`scripts/README.md`, `README.md`, `Makefile`, `docs/CLI_COMMANDS.yaml` and every
skill doc, in the same change.

## Done when

- `docs/WORKFLOW_PROMPT.md` fits on a screen or two and delegates the rest.
- Every local skill has one doc with When to use / Workflow / Presentation / Notes.
- Every skill doc is referenced from `docs/WORKFLOW_PROMPT.md` with its trigger.
- No skill's full instructions are duplicated in the workflow prompt.
- Domain knowledge lives in `knowledge/` and is referenced, not inlined.
- If scripts are foldered, `scripts/README.md` is grouped the same way and every
  path in every file points at the real location.
- No secret or personal data anywhere under `knowledge/` or `docs/`.
