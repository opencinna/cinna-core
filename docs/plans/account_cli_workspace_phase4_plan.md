# Account CLI Workspace — Phase 4 Implementation Plan (Agentic Networks)

> Builds on shipped Phases 1–3 (see `account_cli_workspace_plan.md`,
> `account_cli_workspace_phase3_plan.md`,
> `docs/application/cinna_cli_integration/account_cli_workspace.md` + `_tech.md`).
>
> Phase 4 — the **smallest** phase — lets the local orchestrator agent register
> an **agentic network** (an `agentic-team` graph: team + agent nodes + directed
> connections that encode the subtask-delegation topology) from the account
> workspace, and ships a worked **"build an agentic network" playbook** in the
> context package that walks the orchestrator through the motivating scenario
> end-to-end.
>
> The Blueprint phase of agentic-teams is **static** (it does not execute), so
> "registering a network" means *creating the team graph that encodes delegation
> policy* — not running it. The graph is load-bearing: the task system enforces
> that `mcp__agent_task__create_subtask` is only permitted along a drawn
> connection (see `docs/agents/agentic_teams/agentic_teams.md` §Integration
> Points → Subtask delegation topology). Wiring the team is therefore exactly how
> the orchestrator turns a pile of connected agents into a delegating network.

---

## The Central Decision (A / B / C) — and whether agentic-teams is escape-hatch-reachable

### Finding: agentic-teams IS fully escape-hatch-reachable (verified)

The Phase 3 escape hatch (`POST /api/v1/cli/account/api-proxy`, single chokepoint
`assert_api_proxy_allowed` in
`backend/app/services/cli/account_api_proxy_policy.py`) uses a **denylist**
(default-allow). I checked the agentic-teams router prefix against that denylist:

- Router prefix is **`/agentic-teams`** (`backend/app/api/routes/agentic_teams.py`
  L32: `APIRouter(prefix="/agentic-teams", tags=["agentic-teams"])`; registered in
  `backend/app/api/main.py` L91).
- `EXCLUDED_PREFIXES` in `account_api_proxy_policy.py` contains: `credentials`,
  `ai-credentials`, `oauth-credentials`, `credential-shares`, `users`, `admin`,
  `admin-environments`, `private`, `cli`, `desktop-auth`, `app-auth`, `app-sync`,
  `mfa`, `security-events`, `login`, `oauth`, `auth`, `token`. **`agentic-teams`
  is NOT present.** `STREAMING_DENY` is only `agents/create-flow-stream` /
  `agents/create-flow`.

Therefore **every** agentic-teams endpoint is reachable through `cinna api`:

- `GET/POST /agentic-teams/`, `GET/PUT/DELETE /agentic-teams/{id}`
- `GET /agentic-teams/{id}/chart`
- `GET/POST /agentic-teams/{id}/nodes/`, `GET/PUT/DELETE
  /agentic-teams/{id}/nodes/{node_id}`, `PUT /agentic-teams/{id}/nodes/positions`
- `GET/POST /agentic-teams/{id}/connections/`,
  `GET/PUT/DELETE /agentic-teams/{id}/connections/{conn_id}`
- `POST /agentic-teams/{id}/connections/{conn_id}/generate-prompt`

All are plain JSON request/response endpoints — including `generate-prompt`,
which is a **buffered** `POST` returning `GenerateConnectionPromptResponse` (it is
**not** SSE; verified at `agentic_teams.py` L371–391), so it is NOT caught by the
streaming denial and is fully proxyable.

The whole surface authenticates via `CurrentUser` and is **strictly owner-only**
(no superuser bypass; 404 on unauthorized access). Because the escape hatch
re-dispatches the inner call as the **real account-token user** (Phase 3's
request-scoped user JWT), ownership is satisfied naturally for that user's own
teams and own agents. There is no impedance mismatch.

> **Conclusion: agentic-teams is escape-hatch-reachable.** Dedicated endpoints are
> therefore **not mandatory**. (Had it been on the denylist or otherwise blocked,
> dedicated `/account/*` endpoints would have been mandatory — it is not.)

### Decision: **Option (A) — escape-hatch-only + playbook.**

No new backend, no new dedicated CLI verbs. The orchestrator builds networks by
calling `cinna api POST agentic-teams …`, `cinna api POST
agentic-teams/{id}/nodes/ …`, `cinna api POST agentic-teams/{id}/connections/ …`,
guided by a rich worked playbook in the context package and the generated
`api_reference/agentic_teams.md`.

#### Rationale

1. **Decision 3 (thin client) + the parent plan's explicit CLI-surface rule —
   "high-leverage verbs only … do NOT wrap every endpoint."** Agentic-teams CRUD
   is *17 endpoints*. Wrapping them as `cinna team create / add-node / connect /
   set-lead / generate-prompt / …` (Option B) is precisely the "wrap every
   endpoint" anti-pattern the parent plan warns against. The escape hatch exists
   **for exactly this case**: control-plane CRUD the orchestrator occasionally
   needs but that doesn't justify a bespoke verb. Phase 3 shipped it as the
   designated home for "anything not yet wrapped"; team registration is the
   canonical "anything."

2. **Consistency with how Phases 1–3 landed.** Phase 3 deliberately gave
   *dedicated* verbs only to the three highest-frequency, highest-leverage
   grants (`agent create`, `connect agent-api`, `connect mcp`) — operations that
   mint credentials/tokens and recur constantly in any build session — and put
   **everything else** behind `cinna api`. Team registration is a *one-shot,
   low-frequency* act (you wire a network once, then iterate on the agents). It
   sits firmly on the escape-hatch side of that already-drawn line.

3. **The one real friction — name→id resolution — is already solved upstream and
   is a playbook concern, not an endpoint concern.** The orchestrator knows agent
   *names*; the team API wants agent/node/team *UUIDs*. But:
   - Agent name→id is already available: `cinna account agents` (Phase 1) returns
     the id for every accessible agent, and `cinna agent create` (Phase 3) returns
     the full `AgentPublic` (with id) on creation. The orchestrator already has the
     ids of the agents it just made.
   - Node-id and connection-id resolution is a **read-back** pattern: `POST
     …/nodes/` returns the created `AgenticTeamNodePublic` (with `id`); `GET
     …/{team_id}/chart` returns the whole graph (team + nodes + connections with
     `source_node_name`/`target_node_name` already resolved to human-readable
     names). So the orchestrator captures ids from create responses and/or reads
     the chart — a standard "create → capture id → reference" loop a coding agent
     does fluently. The playbook teaches this loop explicitly.

   This friction is **ergonomic guidance**, which the playbook delivers, not a
   structural gap that needs a server-side resolver. A coding agent piping JSON
   through `jq`/its own parsing is the intended consumer; it does not need a verb
   to extract an `id` field it already received.

4. **Zero new surface area; maximal thin-client consistency.** Option A adds *no*
   backend route, *no* DB change, *no* new CLI verb, *no* new audit constant, *no*
   client regen. The blast radius is one documentation artifact (the playbook) plus
   the assembler change that ships it. This is correct for "the smallest phase."

#### Why not B or C

- **(B) dedicated `cinna team` verbs — rejected.** Violates the thin-client rule;
  17-endpoint wrapping; adds CLI verbs, backend `/account/*` routes (the team API
  is owner-scoped to `CurrentUser`, so each would need an account-token wrapper
  delegating to `AgenticTeam*Service`), audit constants, and client regen — all
  for a once-per-network operation. High surface area, low marginal ergonomic win
  over a good playbook. Not justified.

- **(C) hybrid (one "materialize a whole team graph from a JSON spec" endpoint or a
  name→id resolver) — rejected, but it is the *only* B/C variant with a defensible
  motivation, so it is recorded as Open Question O1 rather than dismissed
  outright.** A single `POST /account/teams/materialize {spec}` that takes
  `{name, task_prefix?, nodes:[{agent_name|agent_id, is_lead?}],
  connections:[{from, to, handover_prompt?, enabled?}]}` and does the create →
  add-nodes → connect dance server-side in one transaction *would* collapse the
  multi-call loop and the name→id resolution into one call, and would make the
  whole network atomic (today a partial failure leaves a half-built team). That is
  a genuine convenience. **It is nonetheless deferred** because: (i) it is *not*
  needed for the scenario to work — the playbook's multi-call loop is completely
  functional today against the unchanged API; (ii) it introduces a *new*
  request-shape contract (the "team spec") that duplicates the team/node/connection
  models and must be versioned/maintained; (iii) atomicity is nice but a coding
  agent recovering from a partial build by reading `GET …/chart` and continuing is
  acceptable for a developer tool; (iv) shipping it now would re-introduce exactly
  the bespoke-endpoint surface area Decision 3 tells us to avoid until proven
  necessary. **Recommendation: ship A now; revisit a `materialize` endpoint only
  if real orchestrator transcripts show the multi-call loop is a repeated pain
  point.** (See O1.)

---

## Backend Work

**None required for the core feature.** Verified explicitly:

- **No new route.** The team API is reached through the existing Phase 3
  `api-proxy`. No `/account/*` endpoint is added.
- **No denylist change.** `agentic-teams` stays *off* `EXCLUDED_PREFIXES` (it
  must remain allowed for Option A to work). No edit to
  `account_api_proxy_policy.py`. Its existing unit test already asserts a
  representative allowed path includes `GET /agentic-teams`
  (`test_account_api_proxy_policy.py`, per Phase 3 plan) — keep that assertion; it
  is now load-bearing for Phase 4, so the plan calls out *not* to remove it.
- **No DB migration.** No model/schema change. (`alembic heads` should still be
  single-headed; Phase 4 adds no revision.)
- **No new SecurityEvent constant.** Team CRUD via the hatch follows the Phase 3
  audit policy: *allowed* proxy calls are not per-call audited (only exclusion
  hits are). Team mutations are ordinary owner-scoped writes; this is consistent
  and intentional.

The **only** backend-adjacent change is the **context-package assembler** addition
to ship the playbook (below) — that is a packaging change, not feature logic.

### One verified data-model nuance worth recording (no action needed)

The agentic-teams `_tech.md` doc is **stale** on one point: it lists
`AgenticTeamCreate`/`AgenticTeamUpdate` as `{name, icon}` only. The actual model
(`backend/app/models/agentic_teams/agentic_team.py`, L20/L26/L38) carries
`task_prefix: str | None (max_length=10)` on **both** the Base and Update models.
So the orchestrator **can** set `task_prefix` at team-create time through the
escape hatch (`POST agentic-teams {"name": "...", "task_prefix": "BOOK"}`). The
playbook should use this (the task prefix gives the network's subtasks readable
short-codes, e.g. `BOOK-1`). This also means the agentic-teams `_tech.md` should be
corrected as a small doc fix (flagged in Integration/Docs below; not a Phase 4
blocker).

---

## The Playbook (the real deliverable)

### What it is

`build-an-agentic-network.md` — a single, worked, end-to-end guide that takes the
orchestrator agent from "four agents I want to coordinate" to "a registered team
graph that permits the right delegations," using only commands that already exist
after Phases 1–3 (`cinna agent create`, `cinna connect agent-api`, `cinna connect
mcp`, `cinna agent sync` + `cinna exec`/`cinna dev` for building each agent's
logic, and `cinna api …` for team registration).

### The motivating scenario it walks through

A **meeting-booking network**:

| Agent | Role | How it's wired |
|---|---|---|
| **front-desk** (team lead) | Talks to the user; books meetings; decides who to delegate to | Lead node; outbound connections to the other three |
| **crm-agent** | Looks up / creates CRM contacts | Exposes an **Agent REST API** (agent-api *producer*); front-desk consumes it via `cinna connect agent-api` |
| **calendar-agent** | Finds free slots and books on Google Calendar | Uses a **Gmail/Calendar MCP** connector; front-desk delegates booking to it |
| **cron-notifier** | Sends reminders ahead of the meeting | front-desk delegates "schedule a reminder" to it |

The delegation policy = the directed connections front-desk → {crm-agent,
calendar-agent, cron-notifier}. Drawing those connections is what makes the
network real (subtask delegation is enforced along edges).

### Content outline (the actual sections of the playbook)

1. **Mental model (read first).** The team graph is *static delegation policy*,
   not an executor. A connection from A→B means "A may hand a subtask to B."
   Nodes require **agents you own**; teams are **owner-only and
   workspace-independent**. You build the *agents' logic* with the normal dev loop;
   you build the *network topology* with team registration. Distinguish the two
   wiring layers explicitly:
   - **Capability wiring** (how agent X can *use* agent Y's tools): `cinna connect
     agent-api` / `cinna connect mcp` — creates a credential on the consumer.
   - **Delegation wiring** (whether agent X may *hand a subtask to* agent Y):
     team connections — enforced by the task system.
   A complete network usually needs *both* for a given pair (front-desk both
   *delegates to* calendar-agent **and** doesn't itself need calendar-agent's MCP;
   crm-agent's data is consumed by front-desk via agent-api). The playbook makes
   this two-layer distinction crisp because it is the single most confusing thing
   for an orchestrator.

2. **Order of operations (the canonical sequence).**
   1. **Create the agents** (`cinna agent create front-desk`, … ×4). Capture each
      returned `id` (from the `AgentPublic` response). *Requires
      agent-developer.*
   2. **Build each agent's logic** — `cinna agent sync <agent>` then `cinna dev` /
      `cinna exec` inside `agents/<agent>/` to author its prompts/skills (the
      normal per-agent loop; the playbook links to the existing per-agent
      building guide, it does not re-teach it).
   3. **Capability wiring**:
      - `cinna connect agent-api --producer crm-agent --consumer front-desk`
        (front-desk can call crm-agent's REST API).
      - For calendar-agent's Google Calendar access: connect its
        Gmail/Calendar MCP via the agent's own credentials (the playbook points
        to the MCP/credentials docs for the provider connector; this is
        per-agent credential setup, done in calendar-agent's workspace, not a
        team concern).
   4. **Register the network (team graph)** — the escape-hatch sequence:
      ```
      # 1) create the team (task_prefix gives subtasks readable short-codes)
      cinna api POST agentic-teams \
        --json '{"name":"Meeting Booking","icon":"users","task_prefix":"BOOK"}'
      #   → capture team id  (response.id)

      # 2) add a node per agent (agent_id from `cinna account agents` /
      #    the create response); mark front-desk as lead
      cinna api POST agentic-teams/<team_id>/nodes/ \
        --json '{"agent_id":"<front-desk id>","is_lead":true}'
      cinna api POST agentic-teams/<team_id>/nodes/ \
        --json '{"agent_id":"<crm-agent id>"}'
      cinna api POST agentic-teams/<team_id>/nodes/ \
        --json '{"agent_id":"<calendar-agent id>"}'
      cinna api POST agentic-teams/<team_id>/nodes/ \
        --json '{"agent_id":"<cron-notifier id>"}'
      #   → capture each node id (response.id), OR read them all back:
      cinna api GET agentic-teams/<team_id>/chart
      #   → nodes[] carry id + name; connections[] carry source/target names

      # 3) draw delegation edges front-desk → each delegate
      cinna api POST agentic-teams/<team_id>/connections/ \
        --json '{"source_node_id":"<front-desk node id>",
                 "target_node_id":"<crm-agent node id>",
                 "connection_prompt":"Hand off contact lookups to the CRM agent..."}'
      #   (repeat for calendar-agent and cron-notifier)

      # 4) (optional) let AI draft a better handover prompt, then save it
      cinna api POST agentic-teams/<team_id>/connections/<conn_id>/generate-prompt
      #   → returns {success, connection_prompt}; review, then:
      cinna api PUT agentic-teams/<team_id>/connections/<conn_id> \
        --json '{"connection_prompt":"<edited text>"}'
      ```
   5. **Verify** — `cinna api GET agentic-teams/<team_id>/chart` and confirm the
      lead, the four nodes, and the three edges. Open the team in the UI
      (`/agentic-teams/<id>`) for a visual check — the CLI-built graph is a
      first-class team, identical to one drawn in the UI.

3. **How the pieces reference each other (the id-capture loop).** A short, explicit
   subsection on the create→capture→reference pattern: every `POST` returns the
   created object with its `id`; the orchestrator stores `team_id`, the four
   `node_id`s, and the three `conn_id`s; `GET …/chart` is the read-back if any id
   was lost. This is where the name→id "friction" is addressed pedagogically (the
   chart resolves node ids ↔ agent names for you).

4. **Business rules the orchestrator must respect (the failure-mode cheat-sheet).**
   Lifted faithfully from `agentic_teams.md` so the agent doesn't trip over them:
   - Nodes require **agents you own** → 404 if you reference an agent you don't own
     (create them via `cinna agent create` first).
   - One node per agent per team → **409** on duplicate.
   - At most one lead; setting a new lead auto-unsets the old one.
   - Connections: source ≠ target (**400** on self-loop); one edge per
     `(source,target)` (**409** on duplicate); both nodes must be in the same team.
   - Team access is **owner-only**; a 404 (not 403) means "not yours / doesn't
     exist."
   - `task_prefix` is 1–10 uppercase alphanumeric.
   - **The graph is policy, not execution**: drawing the edge *permits* delegation
     (unlocks `mcp__agent_task__create_subtask` along it); it does not *run*
     anything. The agents still need their own logic (step 2) to actually delegate.

5. **Capability-vs-delegation worked table** for the scenario — a small matrix
   showing, for each ordered pair, whether it needs an agent-api/MCP connection,
   a team edge, both, or neither. This is the "aha" artifact that makes the
   two-layer model concrete.

6. **Pointers, not duplication.** Links to `context/platform/agents/agentic_teams/`
   (business rules), `context/api_reference/agentic_teams.md` (exact request/response
   shapes), and the connect/credentials docs — the playbook is a *walkthrough*, the
   reference docs are the *spec*. It must not restate endpoint schemas (they're
   generated and would drift).

### File location & delivery mechanism (decisive — verified)

The package is assembled in
`backend/app/services/cli/context_package_service.py::_build_tarball` from two
committed source dirs resolved by `ga_knowledge_assets.py`:
`ga_platform_knowledge_dir()` → GA snapshot `…/knowledge/platform/` (→ packaged as
`context/platform/` + `context/api_reference/`) and `ga_example_scripts_dir()` →
`…/scripts/examples/` (→ `context/examples/`), plus a generated
`context/README.md` index.

**The playbook must NOT live inside `knowledge/platform/`.** That directory is
**wiped and regenerated** on every docs sync: `.cinna-core-kit/scripts/
sync_ga_knowledge.py` does `shutil.rmtree(TARGET)` on the whole
`…/knowledge/platform/` tree (verified, L127) before re-copying business-logic
docs and regenerating `api_reference/`. A hand-authored playbook placed there would
be **silently destroyed** the next time anyone runs `make`-the-knowledge-sync. (It
likewise must not go in `scripts/examples/`, which is for runnable script
patterns, not prose guides.)

**Chosen home: a new committed, sync-safe source dir, shipped as its own
`context/guides/` tree.**

- **Source location (committed):**
  `backend/app/env-templates/general-assistant-env/app/workspace/knowledge/guides/build-an-agentic-network.md`
  — a sibling of `knowledge/platform/`, **outside** the rmtree target, so the docs
  sync never touches it. (Equivalently a top-level
  `…/general-assistant-env/account-guides/` dir; `knowledge/guides/` is preferred
  because it sits with the other knowledge assets and is the obvious place a future
  reader looks.)
- **Assembler change (minimal):** add a small step to `_build_tarball` (mirroring
  the existing `examples_dir` handling) that, given a new
  `ga_account_guides_dir()` helper in `ga_knowledge_assets.py`, copies every file
  under it to `context/guides/<rel>`. Treat it as **non-essential like
  `examples/`** (warn + omit if absent — do **not** 503 on a missing guides dir;
  only the platform snapshot is fail-loud). Update the cache-version key
  (`_snapshot_version`) to include the guides dir so a guide edit invalidates the
  memoized tarball (same mtime+count scheme already used for the other two dirs).
- **Index + orchestrator pointer:** add one row to `_render_index()` (the
  `context/README.md` table) — `| guides/build-an-agentic-network.md | Worked
  walkthrough: stand up a delegating multi-agent network end-to-end. |` — and a
  one-line nudge in the orchestrator `CLAUDE.md` (in the GA env workspace root,
  the Phase-2 orchestrator prompt) pointing at it for "build a multi-agent
  network" requests.

This delivery requires **regenerating nothing about the GA platform snapshot** —
the playbook is independent committed content. It *does* require the account
workspace to **re-download the context package** to receive it (next `cinna
account setup` or `cinna account refresh-context`), and a **backend image rebuild**
so the new committed source files are baked in (the package is assembled from
in-image files, not from `docs/`). Both are normal deploy steps; no special
migration.

> **Why ship it through the context package at all (vs. the per-agent
> `BUILDING_AGENT.md`)?** Because team-building is an *account-orchestrator*
> activity (it spans multiple agents and the account root), not a single agent's
> in-workspace concern. The context package is the orchestrator's knowledge base
> (Phase 2's whole premise); the playbook belongs there next to `platform/` and
> `api_reference/`.

---

## cinna-cli Companion Work (separate repo: `/Users/evgenyl/dev/ml-llm/cinna-cli`)

Under Option A there are **no new CLI verbs**. The team-registration commands in
the playbook are all `cinna api …` invocations that the Phase-3 `cinna api` verb
already supports verbatim. Companion work is limited to:

- **`cinna api --help` discoverability (optional polish).** Phase 3 already points
  `--help` at `context/api_reference/`. Optionally extend the help text (and/or a
  short README note) to also mention `context/guides/` as the home of worked
  walkthroughs, so an agent reaching for "how do I build a network" finds the
  playbook. This is a one-line copy change, not new surface.
- **Nothing else.** No `cinna team` group, no new client methods, no new config.

If, post-Phase-4, transcripts show the multi-call team loop is painful, the
follow-up would be either a `cinna team` group **or** the O1 `materialize`
endpoint — but that is explicitly out of scope here.

---

## Frontend Impact

**None.** No backend route, no schema, no client change. `bash
scripts/generate-client.sh` is **not** required (nothing new in the OpenAPI spec).
The existing agentic-teams UI already renders any team the orchestrator builds
(the CLI-built team is identical to a UI-built one), so the visual verification
step in the playbook works with zero UI changes.

---

## Database Migrations

**None.** Confirm `alembic heads` is single-headed at implementation time per the
repo's standing multi-head caution, but Phase 4 adds no revision.

---

## Testing Implications

Proportional to a docs-plus-packaging phase. Read `backend/tests/README.md` first.

1. **Context-package assembler test (the only code under test).** Extend the
   existing context-package test (Phase 2's `test_account_cli` /
   context-package coverage) with:
   - **Guides present** → the downloaded tarball contains
     `context/guides/build-an-agentic-network.md` (the real committed file is
     packaged).
   - **Guides absent (graceful)** → with the guides dir missing, the package still
     builds (200, not 503), omits `context/guides/`, and logs a warning — mirroring
     the `examples/` degradation test. (Assert the platform snapshot remains the
     only fail-loud/503 source.)
   - **Cache invalidation** → editing a guide file changes `_snapshot_version` and
     the served tarball (no stale memoization). (Light assertion; mirror the
     existing snapshot-version test if one exists.)
   - **Index row** → `context/README.md` lists the `guides/` entry.

2. **Escape-hatch reachability regression (guard the decision).** Confirm the
   Phase 3 chokepoint **still allows** agentic-teams: in
   `test_account_api_proxy_policy.py`, keep/add an assertion that `GET
   /agentic-teams`, `POST /agentic-teams`, `POST /agentic-teams/{id}/nodes/`,
   `POST /agentic-teams/{id}/connections/`, and `POST
   /agentic-teams/{id}/connections/{id}/generate-prompt` are **allowed** (not
   denied). This pins the load-bearing fact that agentic-teams must never be added
   to `EXCLUDED_PREFIXES` without breaking Phase 4. (Cheap, pure unit assertions.)

3. **End-to-end-through-the-hatch scenario (one API test, high value).** A single
   scenario in the account-CLI test suite that, **using an account token through
   `POST /account/api-proxy`**, replays the playbook's core: create a team → add
   two owned-agent nodes → connect them → `GET …/chart` shows the edge; and a
   negative: adding a node for an agent the user does **not** own returns the
   inner **404** transparently through the hatch. This proves the playbook's
   commands actually work via the escape hatch (not just that the team API works
   directly) — i.e. it tests the *Phase 4 promise*, not the agentic-teams feature
   (which has its own tests). Reuse the Phase-1 account-token + Phase-3 api-proxy
   test helpers.

4. **Playbook accuracy (doc review, not automated).** Verify every command,
   endpoint path, request body, and error code in the playbook against the actual
   API (`agentic_teams.py` / `_tech.md` after its correction) and against the
   shipped Phase 1–3 CLI verbs. The api-proxy scenario test (#3) is the executable
   backstop for the team-registration commands; the connect/create commands are
   already covered by Phase 3 tests.

CLI-side: no new verbs → no new cinna-cli tests (beyond the optional `--help` copy,
which is not test-worthy).

---

## Context Package / GA Snapshot Regeneration

- **GA *platform* snapshot (`knowledge/platform/`)**: **no regeneration needed.**
  The playbook lives outside it (in `knowledge/guides/`), precisely so the
  `sync_ga_knowledge.py` rmtree doesn't touch it and so we don't have to re-run the
  doc/api-reference sync for this phase.
- **Context *package* (the downloaded tarball)**: changes content (now includes
  `context/guides/`). It is assembled at request time from in-image files and
  memoized; the cache-version bump (guides dir mtime+count) invalidates it
  automatically after a deploy. Account workspaces receive the playbook on their
  next `cinna account setup` / `cinna account refresh-context`.
- **Backend image**: must be **rebuilt** so the new committed guide file +
  assembler change are baked in (consistent with how the existing snapshot ships).

---

## Implementation Order

1. **Write the playbook** `…/general-assistant-env/app/workspace/knowledge/guides/
   build-an-agentic-network.md` (the deliverable). Validate every command/path/body
   against the live API and the Phase 1–3 verbs.
2. **Assembler + index**: add `ga_account_guides_dir()` to `ga_knowledge_assets.py`;
   add the `guides/` copy step (warn-and-omit if absent) + the index row +
   cache-key inclusion in `context_package_service.py`.
3. **Correct the stale `_tech.md`** (`task_prefix` is in Create/Update) — small doc
   fix so the playbook's `task_prefix` usage matches the documented contract.
4. **Tests**: assembler guides-present/absent/cache/index; chokepoint
   agentic-teams-allowed assertions; the one api-proxy team-build scenario.
5. **Docs**: lift the Phase-4 roadmap note in
   `docs/application/cinna_cli_integration/account_cli_workspace.md` + `_tech.md`
   to "Phase 4 shipped"; add a short "Registering an agentic network" subsection
   pointing at the playbook and the escape-hatch sequence.
6. **(Optional)** `cinna api --help` copy nudge in the cinna-cli repo.
7. Rebuild the backend image; verify a fresh `cinna account refresh-context`
   delivers `context/guides/build-an-agentic-network.md`.

---

## Open Questions (genuinely open)

- **O1 — A "materialize team from a JSON spec" convenience endpoint (the only
  serious B/C candidate).** Should we add `POST /api/v1/cli/account/teams/
  materialize {name, task_prefix?, nodes:[{agent_name|agent_id, is_lead?}],
  connections:[{from, to, handover_prompt?, enabled?}]}` that performs
  create→add-nodes→connect atomically server-side (collapsing the multi-call loop
  and name→id resolution, and making a network build all-or-nothing)? **Recommend
  NOT now** — ship Option A; revisit only if real orchestrator transcripts show the
  multi-call loop is a repeated pain point or partial-build recovery is a real
  problem. Recording it because it is the one place the raw API is genuinely awkward
  for a one-shot network build, and atomicity has real value.

- **O2 — Playbook home directory name.** `knowledge/guides/` (sibling of
  `knowledge/platform/`, recommended) vs. a top-level
  `general-assistant-env/account-guides/`. Both are sync-safe (outside the
  `knowledge/platform/` rmtree). Recommend `knowledge/guides/` for locality with
  the other knowledge assets. Confirm no other tooling globs
  `…/knowledge/**` in a way that would pick up `guides/` unintentionally (a quick
  check during implementation; the docs sync targets `knowledge/platform/`
  specifically, so it's clear).

- **O3 — Calendar-agent's Google Calendar MCP in the scenario.** The playbook's
  calendar-agent needs Google Calendar access via an MCP connector that uses *its
  own* Google credentials (per-agent credential setup), which is distinct from the
  agent-to-agent `cinna connect mcp` (which wires one agent to *another agent's*
  MCP provider). The playbook should be explicit about which kind of MCP wiring the
  scenario uses for the calendar capability (provider-connector / user OAuth
  credential on the agent), and link the right credentials/MCP doc — confirm the
  exact current command/flow for attaching a Google Calendar MCP to an agent from
  the CLI (it may itself be a `cinna api`/credentials operation rather than `cinna
  connect mcp`, which is agent-to-agent specific). This is a *playbook-accuracy*
  question, not an architecture one, but it must be nailed before the playbook
  ships so the worked example is runnable end-to-end.
```
