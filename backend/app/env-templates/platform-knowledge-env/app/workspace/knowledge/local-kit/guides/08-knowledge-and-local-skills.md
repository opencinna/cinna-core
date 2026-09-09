# 08 — Knowledge and local skills

## Read this when

The agent has three or more distinct capabilities, or needs domain documentation
longer than a page. Also read it when `docs/WORKFLOW_PROMPT.md` has grown into a wall
of text nobody can follow, or when a finished skill is worth publishing to the
catalog for other agents to install.

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

### Designing around skills

The split table above judges one capability at a time. Use it the other way round
when the agent is still being designed: if it has several distinct internal
workflows, **the default is one skill per workflow**, and `docs/WORKFLOW_PROMPT.md`
keeps only the orchestration — what the agent is, and which trigger reaches which
skill.

Worked example — an agent that reconciles vendor bills:

- **"Generate the reconciliation report"** → a **skill**. Its own trigger, its own
  multi-step procedure, its own scripts, a report format nothing else produces:
  `skills/bill-reconciliation/SKILL.md`.
- **"Answer questions about a bill"** → **stays in the workflow prompt**. It is what
  the agent does on almost every message; there is no separate trigger to name and
  no separate artefact to produce.

That default holds unless a workflow argues its way out of it. The tiebreak, for the
ones that do: **separate trigger + separate output + reusable by another agent ⇒
skill.** All three is a skill without further thought. Two out of three usually still
is — a workflow with its own trigger and its own output earns a folder even if no
other agent would ever want it. One out of three is a paragraph in the workflow
prompt.

When the user describes the agent as "it does X, and also Y, and also Z", that is
three skill folders and one short prompt — not one prompt with three chapters.

### Publishing a skill

A skill that would serve *another* agent belongs in the platform's skills catalog,
where whoever the owner chooses — everyone on the instance, or a named few — can
install it into their **cloud** agents. Publishing is a deliberate act by the owner
and it goes through the platform.

**Publishing reads the cloud agent, not this folder.** The command resolves the
agent's environment on the instance and packages the `skills/<name>/` it finds in
*that* workspace. Nothing in the path ever reads your local kit. Two consequences,
and the second is the one that bites quietly:

- **The agent has to be in the cloud already.** `cinna skills publish` looks the
  agent up in your account's cloud listing, so a local-only agent never reaches the
  server at all — the CLI stops first with *"No accessible agent matches
  '<slug>'"*, followed by the agents it does know and a nudge to run
  `cinna account agents`. If you see that, the missing rung is
  `guides/11-go-cloud.md`, three above this one; go and climb it first. A cloud
  agent whose environment has never been started gets a different answer, from the
  server: *"This agent's workspace is not on disk yet. Start the environment once
  so its files are materialised, then publish."*
- **The local edit has to have travelled first.** If you changed `skills/<name>/`
  here after the last import or sync, publish packages the *older cloud copy* — and a
  published revision is immutable, so you cannot replace it, only append another
  beside it. Push first — `cinna agent import --update` from the workshop, or, once
  you have moved to the account workspace's copy under `agents/<slug>`, `cinna dev`
  (guide 11 step 9; `cinna dev` syncs that copy, not this folder). Then publish, then
  report what you published.

Prepare the folder before either:

- **Self-contained.** Every path the skill needs is inside `skills/<name>/` or is an
  explicit, documented input. Nothing reaches back into this agent's top-level
  `scripts/`, `docs/` or `config/`.
- **No secrets — and nothing checks the contents for you.** Publish refuses a skill
  containing a file *named* like a credential (`.env`, `*.pem`, `id_rsa` and
  friends), and that is the whole of the automated gate: no tool reads inside the
  files. A token pasted into `SKILL.md`, a customer name in a fixture, an internal
  hostname in a script — all publish cleanly. Read the folder yourself before you
  call it ready; it is copied verbatim into someone else's agent.
- **A `description` written for discovery.** Someone browsing the catalog reads that
  one sentence and nothing else, so it must say what the skill does and when to use
  it without assuming this agent's context.
- **Valid by the rules publish actually enforces.** `name` matches the folder
  exactly, keeps the shape in the table above, and is not one of the reserved
  platform command names; `description` is present and at most 1024 characters; the
  folder is at most 16 MB — plus the structural failures that speak for themselves,
  a missing `SKILL.md`, frontmatter that will not parse, a file that cannot be read.
  Those are refusals; the two-field table above and `skills/README.md` in the
  scaffold carry the same rules, because they are the ones that keep a skill in the
  engine's index. A body over 64 KB is only a flag: it publishes. **`kit.py validate` checks
  none of it** — it has no `SKILL.md` validator at all, so its silence is not a
  verdict. A broken skill is refused by the publish call, with `skill_invalid`
  naming the rule it broke.

- **A `version` in the frontmatter, or none — either is fine.** Publish never
  refuses over a version. If `SKILL.md` carries a `version:` that has not been
  published yet, that is the version published. If it has been (or there is no
  line at all), publish takes the next one after the last release — `1.0.0` for
  a skill that has never been published, `1.0.1` after `1.0.0` — and **writes it
  back into `skills/<name>/SKILL.md` on the cloud workspace** before
  snapshotting, so the published bytes carry their own version. The consequence
  worth knowing: that write lands on the *cloud* copy, so your local folder
  keeps whatever version it had until the next `cinna dev` sync brings the line
  back down. Set the line by hand only when you want a particular number — a
  `2.0.0` you write is honoured over the automatic `1.0.1`.

Then publish it from the account:

```bash
cinna skills publish <slug> <name> --visibility public
```

**Name the audience or nobody gets it.** A package is `private` by default, so the
bare command succeeds, prints a catalog URL, and shares the skill with no one. Pass
`--visibility public` for everyone on the instance, or `--visibility users --grant
<email>` for a named few.

`--grant` without `--visibility users` is refused before anything is sent: a grant
is consulted only under the `users` visibility, so a private or public package
ignores its grant list entirely. The CLI says so and stops — *"--grant only has an
effect on a package whose visibility is 'users'… Re-run with --visibility users
(harmless on a package that already is), or drop --grant."* Naming the visibility
again on a package that is already `users` costs a re-publish nothing. The server
enforces the same rule for any other caller, refusing the publish with
`grants_require_users_visibility` rather than writing grants that share nothing.

The same act is in the web UI on the agent's page: the **Addons** tab, then the
skill row's **Share…** (**Update published skill…** once it has been published
before). Both create an immutable revision in the catalog; publishing again appends
a new revision rather than editing the old one, so a consumer's install never
changes underneath them.

Once a skill is in the catalog, other people get it by installing it from there.
Copying `skills/<name>/` into a second agent folder forks one capability into two
files that drift apart, and it is the wrong move for any agent that could reach the
catalog instead.

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
- Any skill worth sharing reaches **other people** through the catalog
  (`cinna skills publish`, once the agent is in the cloud), not through a copy of
  its folder.
