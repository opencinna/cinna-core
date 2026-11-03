# 14 — Dashboards

## Read this when

The owner wants to see, track or keep an eye on something over time — a pipeline,
an inbox ranked by their rules, a row of KPIs, this week's meetings — and they use
Cinna Desktop. The agent that runs in Cinna learns the details from the platform
skill `cinna-dashboards` (`skills/cinna-dashboards/SKILL.md` in this kit); this
guide is for you, building the agent.

## What a component is

A **dashboard component** is a folder in the agent, `dashboards/<name>/`, with a
`component.json` that says what to show and where the data comes from. The owner
adds it to a dashboard in Cinna Desktop as a tile; the tile refreshes while they
look at it, and a chat docked beside the dashboard asks the agent about what is on
screen. The contract is `protocols/dashboards.schema.json`
(`cinna_protocol: dashboards/1`).

| | Component | Artifact |
|---|---|---|
| What | a live view the owner keeps | a frozen copy |
| For | the owner, every day | people you hand it to |
| Where | `dashboards/<name>/`, committed, travels with the agent | `artifacts/<name>/`, one person's output, never travels |
| Data | recomputed by its endpoint or re-read from a file | what you computed when you published |

Asked for both? Build both.

An agent in the cloud has no dashboards yet: there, `dashboards/` travels with the
agent and waits.

## Layout

```
dashboards/
└── deals-pipeline/
    ├── component.json
    └── api/
        ├── deals.py
        └── deal_history.mjs
app-data/
└── dashboards/
    └── deals-pipeline/          # the component's data, written by scripts or schedules
        └── deals.json
```

- The folder name is the component name: `^[a-z0-9][a-z0-9-]{0,63}$`.
- `dashboards/` is authored and committed, like `skills/`. It is copied to teammates
  and by cloud import. Nothing is ever written into it at runtime.
- `app-data/dashboards/<name>/` holds the data. It never travels.
- The scaffold creates no `dashboards/`; it appears with the first component.

## component.json

```json
{
  "schema_version": 1,
  "title": "Deals pipeline",
  "description": "Open deals by stage with value and next step.",
  "view": "table",
  "source": { "endpoint": "deals" },
  "endpoints": {
    "deals":        { "script": "api/deals.py",          "description": "Open deals. Params: month, stage." },
    "deal_history": { "script": "api/deal_history.mjs",  "description": "One deal's activity. Params: deal_id.", "timeout_s": 30 }
  },
  "params": {
    "month": { "type": "month", "label": "Month", "default": "current" },
    "stage": { "type": "enum",  "label": "Stage", "values": ["lead", "proposal", "negotiation"] }
  },
  "refresh": { "max_age": "15m", "after_turn": true, "on_app_start": false,
               "while_open_every": null, "command": null, "schedule": null },
  "size": "l",
  "requires_credentials": ["crm"],
  "context": "Each row is one open deal. value is in EUR. Stage order: lead, proposal, negotiation.",
  "prompts": [ { "label": "At risk", "prompt": "Which deals in view are at risk this month, and why?" } ]
}
```

- **`view`**: `table`, `list`, `metrics` and `markdown` are drawn by the app in its
  own design. `html` is the agent's own page; use it only for what the native views
  cannot draw.
- **`source`**: one of the component's `endpoints`, or a `.md`, `.json` or `.csv`
  file under `app-data/` or `files/`.
- **`params`**: `enum`, `month`, `date`, `date_range`, `text`, `number`,
  `boolean`. The app draws controls and sends the values on stdin.
- **`refresh`**: `max_age` is how stale a visible tile may be; `after_turn`
  recomputes when the agent's turn ends; `while_open_every` (≥ `1m`) polls while the
  dashboard is open; `on_app_start` warms up at launch, which costs the owner at every
  launch. `command` (`/run:<name>`) rewrites a source file first; `schedule` names
  the schedule that writes it.
- **`requires_credentials`**: names from the manifest's `credentials[]`, so a
  teammate without one sees what is missing instead of an error.
- **`context`** and **`prompts`**: what the owner's chat needs to read the view, and
  the questions this owner would ask.

## Endpoints

An endpoint is a script in `api/`. Params arrive on stdin as one JSON object; it
prints one JSON value on stdout; cwd is the agent folder; it has the agent's
credentials, as `/run:` commands do. `.py` runs with Python (through `uv run` when
the agent has a `pyproject.toml`), `.js`, `.mjs` and `.cjs` with Node. Pick the
language the agent already uses.

Keep endpoints fast: read what a script, a `refresh.command` or a schedule already
prepared. Anything that needs the model's judgment, such as ranking emails by the
owner's rules, comes from a prompt schedule (guide 05) that writes the file the
component reads.

## Starters

- `templates/dashboard-list/`: a `list` view and `api/items.py`.
- `templates/dashboard-html/`: a web view with `index.html`, `app.js`, a
  dependency-free SVG bar and line helper (`chart.js`), `api/data.py` and a Node
  drill-down. It shows `cinna.onData`, `cinna.setParams`, `cinna.setContext` and
  `cinna.call`. No CDN, relative paths only: web views get no network.

Copy one to `dashboards/<name>/`, rename, change.

## Checking

```bash
uv run python .cinna-kit/skills/cinna-dashboards/scripts/check_component.py dashboards/<name> --run
uv run .cinna-kit/tools/kit.py dashboards check Local/<slug> --run
```

Both apply the same rules; `kit.py validate` runs the static part over every
component, including those in `skills/*/dashboards/`. `--run` runs the source with
the default params and checks the result's shape.

## Offering

The agent offers a component by printing its reference on a line of its own, never
in backticks: `cinna://component/id:<manifest id>/<name>`, or
`cinna://component/bundle:<bundle id>/<name>` once the agent is shared. The app
draws a card with **Add to dashboard**. The host's prompt tells the agent which
prefix to print.

## Components inside a skill

A skill can ship components in its own `dashboards/<name>/`; see guide 08.

## Done when

- [ ] Each component is a folder under `dashboards/` with a `component.json`.
- [ ] Its data is written under `app-data/dashboards/<name>/`, never into `dashboards/`.
- [ ] Every endpoint was run with sample stdin and returns the view's shape quickly.
- [ ] `check_component.py dashboards/<name> --run` prints no error.
- [ ] `context` and `prompts` are written for this owner.
