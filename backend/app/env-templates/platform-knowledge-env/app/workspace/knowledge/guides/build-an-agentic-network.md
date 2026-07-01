# Build an Agentic Network — End-to-End Playbook

> Audience: a local coding agent (Claude Code or similar) running inside an
> account workspace. Every command in this guide is real and runnable. Steps
> marked **MANUAL** require a browser action; all other steps run entirely from
> the CLI.
>
> Pre-reading: `context/platform/agents/agentic_teams/agentic_teams.md` (delegation
> rules) and `context/api_reference/agentic_teams.md` (exact request/response
> shapes).

---

## What You Are Building

A **meeting-booking network** — four agents that delegate work to each other,
wired up with two distinct integration layers and registered as a team so the
platform enforces who may hand work to whom.

```
                    ┌──────────────────────────────┐
                    │  concierge  (team lead)       │
                    │  talks to the user; decides   │
                    │  who handles the booking      │
                    └───┬──────────────┬────────────┘
                        │              │
              agent-api │              │ a2a MCP connector
                        ▼              ▼
              ┌──────────────┐  ┌──────────────────┐
              │  crm-agent   │  │  calendar-agent   │
              │  looks up /  │  │  finds free slots │
              │  creates CRM │  │  books on Google  │
              │  contacts    │  │  Calendar         │
              └──────────────┘  └──────────────────┘
                        │
              delegation│ (CRON-triggered — runs on its own schedule)
                        ▼
              ┌──────────────────────┐
              │  cron-notifier       │
              │  sends reminders     │
              │  ahead of meetings   │
              └──────────────────────┘
```

---

## Two Wiring Layers — Keep These Straight

Before touching any command, understand this distinction. Confusion here is
the single most common mistake when building a network:

| Layer | What it does | How to set it up |
|-------|-------------|-----------------|
| **Capability wiring** | Gives agent X the _tools_ to call agent Y at runtime | `cinna connect agent-api` or `cinna connect mcp` |
| **Delegation wiring** | Declares that agent X _may hand a subtask to_ agent Y | `cinna api POST agentic-teams/{id}/connections/` |

A complete network usually needs _both_ for a given pair. For this scenario:

| Pair | Capability wiring | Delegation wiring |
|------|------------------|------------------|
| concierge → crm-agent | `cinna connect agent-api` (so concierge can call the CRM REST API) | team connection (so concierge may create a crm-agent subtask) |
| concierge → calendar-agent | `cinna connect mcp` (so concierge has calendar-agent's MCP tools) | team connection (so concierge may create a calendar-agent subtask) |
| concierge → cron-notifier | none needed at runtime — concierge just delegates the task | team connection (required to delegate) |

The team graph is **policy, not execution**. Drawing an edge does not run
anything; it _permits_ `mcp__agent_task__create_subtask` along that edge. You
still have to build the agents' logic (their prompts and scripts) separately.

---

## Prerequisites

- `cinna account setup` completed; you are running inside the account workspace.
- `context/` is present (run `cinna account refresh-context` if stale).
- You have the **agent-developer** role. `cinna agent create` returns 403
  without it; contact your platform admin if needed.
- For Step 4c (crm-agent REST API): you understand that the producer's
  `agent_api_enabled` flag must be true. If the agent has no running
  environment yet, enable its API after the first `cinna agent sync` and a
  brief `cinna exec --agent crm-agent -- echo ok` to confirm the env is up.

---

## Order of Operations

```
1. Create the four agents
2. Sync each agent and build its logic
3. Capability wiring (agent-api + MCP connections)
4. Team registration (the delegation topology)
5. Verify
```

Do not wire capabilities before the agents exist; do not register the team
before agent IDs are known.

---

## Step 1 — Create the Four Agents

```bash
cinna agent create concierge    --description "Front-desk agent; books meetings, delegates to specialists"
cinna agent create crm-agent    --description "Looks up and creates CRM contacts; exposes a REST API"
cinna agent create calendar-agent --description "Books meetings on Google Calendar via MCP"
cinna agent create cron-notifier --description "Sends meeting reminders; triggered by CRON"
```

Each command prints the created agent's **id**, name, and environment id.
**Capture all four agent IDs now** — you need them for team registration in
Step 4. Example output:

```
Created agent "concierge"
  id:     a1b2c3d4-...
  env_id: e5f6a7b8-...
```

If you missed them, recover via:

```bash
cinna account agents
```

This prints the full agents table with IDs, `can_build` flag, and
`is_foreign_install` flag. Copy the IDs into shell variables or a scratch
file:

```bash
CONCIERGE_ID="<id from create output>"
CRM_ID="<id from create output>"
CAL_ID="<id from create output>"
NOTIFIER_ID="<id from create output>"
```

---

## Step 2 — Sync and Build Each Agent's Logic

For each agent, mint a child token and open the local workspace, then build
its prompts and scripts:

```bash
cinna agent sync concierge
cinna agent sync crm-agent
cinna agent sync calendar-agent
cinna agent sync cron-notifier
```

Each `sync` writes an `agents/<name>/` workspace locally. Work inside each
workspace to build the agent's logic — edit prompts, write scripts — using
the normal development loop:

```bash
cinna dev                      # inside agents/concierge/
cinna exec --agent concierge -- cat workspace/AGENTS.md
```

Building each agent's logic is outside the scope of this playbook. For a
producer agent's REST API specifically (like crm-agent below), see
[building-an-agent-api.md](building-an-agent-api.md) — it covers the handler
files, `policy.yaml`, the per-user `scopes:` catalog, and verifying the spec.
Return here once the basic logic for each agent is sketched in (even a
placeholder prompt is enough to register the team; you can iterate afterwards).

> **⚠️ Prompts: the agent config (DB) is authoritative — editing `docs/*.md`
> alone does NOT update the live prompts.** When you "edit prompts" above, hand-
> editing the synced `agents/<name>/workspace/docs/{WORKFLOW,ENTRYPOINT,REFINER}_PROMPT.md`
> files is **not** sufficient to change what the runtime actually uses.
> `cinna agent show <name> --prompts` reads the prompts from the agent **config
> (database)**, which is the source of truth — a freshly created agent still has
> the placeholder template prompts there, so the runtime keeps serving the
> placeholders no matter what your local doc files say. Worse, editing the doc
> files *after* the env exists makes the next `cinna sync push` report a
> **conflict** on those files (both the DB-seeded env copy and your local copy
> changed), which you then have to resolve.
>
> **Fix / correct path — bulk-write the config, then sync it into the env:**
> ```bash
> # 1. Author the full set locally (description + the prompt fields).
> #    See authoring-agent-prompts.md for what each of the six fields is for.
> cinna api PUT agents/<agent_id> --data @agents/<name>/prompts.json
> # 2. Push the doc-backed prompts into the already-running env immediately
> #    (otherwise they seed DB→env only on the next env start):
> cinna api POST agents/<agent_id>/sync-prompts
> # 3. Verify what the runtime actually reads:
> cinna agent show <name> --prompts
> ```
> Pick **one** path — the bulk write **or** hand-editing `docs/*.md`, never both
> at once (editing both sides forces a three-way, last-writer-wins merge of the
> doc prompts). For the account orchestrator, the bulk write is the recommended
> single path. Full field reference and the finalize checklist:
> [authoring-agent-prompts.md](authoring-agent-prompts.md).

One important note for crm-agent: its REST API only works when
`agent_api_enabled` is set to true. You can do this from the CLI:

```bash
cinna api PUT agents/$CRM_ID --json '{"agent_api_enabled": true}'
```

Expected response contains `"agent_api_enabled": true`.

---

## Step 3 — Capability Wiring

### 3a. Connect concierge to crm-agent's REST API

crm-agent will expose a FastAPI-based REST API from its `agent_api/`
workspace directory. concierge will call it code-to-code (no LLM in the
request path).

```bash
cinna connect agent-api \
  --producer crm-agent \
  --consumer concierge \
  --label "CRM API"
```

Expected output:

```
Connected agent-api:
  credential_id: ...
  token_prefix:  abc12345
  base_url:      https://<platform>/api/v1/agent-api/<crm-agent-id>/
  spec_url:      https://<platform>/api/v1/agent-api/<crm-agent-id>/openapi.json
```

The CLI resolves agent names to IDs automatically. The resulting
`agent_api` credential is synced into concierge's container as
`credentials.json` — concierge reads `base_url` and `token` from there
to call the CRM REST API. No further token management is needed.

If the command returns 400 with "producer REST API is not enabled", run the
`PUT agents/$CRM_ID` enable command from Step 2 and retry.

### 3b. Create an agent-to-agent MCP connector on calendar-agent (producer side)

concierge will reach calendar-agent as a live MCP server (tools visible in
the SDK). To enable this, calendar-agent's owner must create an
`is_agent_to_agent=true` MCP connector on it. Because the connector
management route (`POST /api/v1/agents/{id}/mcp-connectors`) is NOT on the
escape-hatch denylist, this is fully doable from the CLI:

```bash
cinna api POST agents/$CAL_ID/mcp-connectors --json '{
  "name": "Calendar Agent A2A Connector",
  "is_agent_to_agent": true,
  "allow_token_access": true,
  "allowed_user_ids": []
}'
```

`allowed_user_ids: []` means only the owner (you) may consume it. If the
concierge is owned by a different user, add that user's UUID to
`allowed_user_ids`. `allow_token_access: true` is required — it lets the
`cinna connect mcp` helper mint a direct token without an OAuth round-trip.

Capture the returned `connector_id` from the response. Then confirm it
appears in the discoverable list:

```bash
cinna api GET agents/$CAL_ID/mcp-connectors
```

You should see the connector with `"is_agent_to_agent": true`.

### 3c. Connect concierge to calendar-agent's MCP connector (consumer side)

```bash
cinna connect mcp \
  --producer calendar-agent \
  --consumer concierge \
  --label "Calendar MCP"
```

What the CLI does under the hood:
1. `GET /account/connect/mcp/discoverable?consumer_agent_id=<concierge-id>` —
   resolves "calendar-agent" → the `connector_id` you just created.
2. `POST /account/connect/mcp` — mints a direct token, creates an
   `mcp_provider` credential on concierge.

Expected output:

```
Connected MCP provider:
  credential_id:  ...
  endpoint_url:   https://<platform>/mcp/<connector-id>/mcp
```

The `mcp_provider` credential is injected into concierge's SDK runtime
config (NOT into `credentials.json`). At session start, concierge's SDK
receives calendar-agent's tools as a named MCP server
(`cinna_mcp_<credential-id>`). No code change is needed in concierge's
workspace.

If `cinna connect mcp` returns 404, the connector is either not discoverable
(check `allowed_user_ids`) or `is_agent_to_agent` is false on the connector.

### 3d. Calendar-agent's Google Calendar access — MANUAL STEP

**MANUAL:** calendar-agent needs its own Google Calendar credential so its
scripts can actually book meetings. Google OAuth requires a browser session
and credentials never leave the platform.

To configure this:

1. Open calendar-agent's Integrations page in your browser:
   `<frontend_url>/agents/<calendar-agent-id>/integrations`
2. Navigate to the Credentials tab and add a Google OAuth credential
   (or a service-account key if your platform has one pre-configured).
3. In calendar-agent's building session (`cinna dev` inside
   `agents/calendar-agent/`), write `workspace/` scripts that read the
   Google credential from `credentials.json` and call the Calendar API to
   find free slots and create events.

There is no CLI path for completing a Google OAuth flow — it requires a
browser redirect through Google's consent page. This is intentional: the
platform never holds the OAuth flow in a machine-accessible path; credentials
live inside the agent container only.

Once the credential is configured, calendar-agent's logic is self-contained.
concierge hands off booking requests to it via the MCP connector (capability
layer) or via subtask delegation (delegation layer, configured in Step 4).

### 3e. cron-notifier — no capability wiring needed

cron-notifier runs on a CRON schedule and fires independently. concierge
delegates a "schedule a reminder" subtask to it; the notifier uses its own
credentials (email/messaging) to send the notification. Set up a CRON
schedule on cron-notifier from the platform UI (Integrations tab) or via:

```bash
cinna api POST agents/$NOTIFIER_ID/schedules --json '{
  "schedule": "0 8 * * *",
  "prompt": "Check upcoming meetings in the next 24 hours and send reminders.",
  "schedule_type": "static_prompt",
  "enabled": true
}'
```

Adjust the cron expression and prompt to match your reminder logic.

---

## Step 4 — Register the Team (Delegation Topology)

This is the "agentic teams" registration — it tells the platform which agents
form the network and which directed delegation edges are permitted.

All commands below go through the `cinna api` escape hatch. The `agentic-teams`
router prefix is NOT on the exclusion denylist, so every team endpoint is
reachable. The exit code is 0 for 2xx responses, non-zero for 4xx/5xx. Pipe
through a JSON parser (e.g. `python3 -c "import sys,json; d=json.load(sys.stdin); print(d['id'])"`)
to capture IDs.

### 4a. Create the team

```bash
TEAM_JSON=$(cinna api POST agentic-teams --json '{
  "name": "Meeting Booking Network",
  "icon": "users",
  "task_prefix": "BOOK"
}')
echo "$TEAM_JSON"
TEAM_ID=$(echo "$TEAM_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Team ID: $TEAM_ID"
```

`task_prefix` sets the short-code prefix for tasks in this team — subtasks
created by the agents will be `BOOK-1`, `BOOK-2`, etc.

### 4b. Add agent nodes

Each agent gets one node. The node name is auto-populated from the agent's
name; you only supply `agent_id` and whether this node is the team lead.
concierge is the lead (the entry point for future process invocation).

```bash
# concierge — team lead
CONCIERGE_NODE_JSON=$(cinna api POST agentic-teams/$TEAM_ID/nodes/ --json \
  "{\"agent_id\": \"$CONCIERGE_ID\", \"is_lead\": true}")
CONCIERGE_NODE_ID=$(echo "$CONCIERGE_NODE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# crm-agent
CRM_NODE_JSON=$(cinna api POST agentic-teams/$TEAM_ID/nodes/ --json \
  "{\"agent_id\": \"$CRM_ID\"}")
CRM_NODE_ID=$(echo "$CRM_NODE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# calendar-agent
CAL_NODE_JSON=$(cinna api POST agentic-teams/$TEAM_ID/nodes/ --json \
  "{\"agent_id\": \"$CAL_ID\"}")
CAL_NODE_ID=$(echo "$CAL_NODE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# cron-notifier
NOTIFIER_NODE_JSON=$(cinna api POST agentic-teams/$TEAM_ID/nodes/ --json \
  "{\"agent_id\": \"$NOTIFIER_ID\"}")
NOTIFIER_NODE_ID=$(echo "$NOTIFIER_NODE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

If a `POST nodes/` returns 409, that agent is already in the team — skip that
node and read back its ID via `GET agentic-teams/$TEAM_ID/chart` (see Step 5).

If it returns 404, the `agent_id` does not belong to you. Agents must be
owned by the same user who owns the team.

### 4c. Draw the delegation edges

Each edge means "concierge _may_ hand a subtask to this agent." Without an
edge, `mcp__agent_task__create_subtask` is rejected by the task system.

```bash
# concierge → crm-agent
CONN1_JSON=$(cinna api POST agentic-teams/$TEAM_ID/connections/ --json \
  "{\"source_node_id\": \"$CONCIERGE_NODE_ID\",
    \"target_node_id\": \"$CRM_NODE_ID\",
    \"connection_prompt\": \"Delegate contact lookup and CRM data tasks to crm-agent.\",
    \"enabled\": true}")
CONN1_ID=$(echo "$CONN1_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# concierge → calendar-agent
CONN2_JSON=$(cinna api POST agentic-teams/$TEAM_ID/connections/ --json \
  "{\"source_node_id\": \"$CONCIERGE_NODE_ID\",
    \"target_node_id\": \"$CAL_NODE_ID\",
    \"connection_prompt\": \"Delegate calendar operations (free-slot search, event creation) to calendar-agent.\",
    \"enabled\": true}")
CONN2_ID=$(echo "$CONN2_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# concierge → cron-notifier
CONN3_JSON=$(cinna api POST agentic-teams/$TEAM_ID/connections/ --json \
  "{\"source_node_id\": \"$CONCIERGE_NODE_ID\",
    \"target_node_id\": \"$NOTIFIER_NODE_ID\",
    \"connection_prompt\": \"Delegate meeting reminder scheduling to cron-notifier.\",
    \"enabled\": true}")
CONN3_ID=$(echo "$CONN3_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

Self-connections (source == target) return 400. Duplicate edges return 409.
Both nodes must be in the same team or the create returns 404.

### 4d. (Optional) Let the platform AI draft better handover prompts

The platform can read both agents' prompts and generate a more context-aware
handover description. Review and save the result:

```bash
# Generate a prompt for the concierge → crm-agent edge
GENERATED=$(cinna api POST agentic-teams/$TEAM_ID/connections/$CONN1_ID/generate-prompt)
echo "$GENERATED"
# Response: {"success": true, "connection_prompt": "When the user asks about ..."}

# If you like it, save it:
cinna api PUT agentic-teams/$TEAM_ID/connections/$CONN1_ID \
  --json '{"connection_prompt": "<paste generated text here>"}'
```

`generate-prompt` never modifies the connection itself — it only returns
text. Use the `PUT` to apply it.

---

## Step 5 — Verify

Read back the full team graph in one call:

```bash
cinna api GET agentic-teams/$TEAM_ID/chart
```

The response shape is:

```json
{
  "team": { "id": "...", "name": "Meeting Booking Network", "task_prefix": "BOOK", ... },
  "nodes": [
    { "id": "<concierge-node-id>", "name": "concierge", "is_lead": true, ... },
    { "id": "<crm-node-id>",       "name": "crm-agent",  "is_lead": false, ... },
    { "id": "<cal-node-id>",       "name": "calendar-agent", "is_lead": false, ... },
    { "id": "<notifier-node-id>",  "name": "cron-notifier",  "is_lead": false, ... }
  ],
  "connections": [
    { "source_node_name": "concierge", "target_node_name": "crm-agent",      ... },
    { "source_node_name": "concierge", "target_node_name": "calendar-agent", ... },
    { "source_node_name": "concierge", "target_node_name": "cron-notifier",  ... }
  ]
}
```

Confirm: four nodes, exactly one `is_lead: true` (concierge), three directed
edges all originating from concierge, `task_prefix: "BOOK"`.

For a visual check, open the team in the browser:
`<frontend_url>/agentic-teams/<TEAM_ID>`

The team you just built from the CLI is a first-class team — identical to
one drawn in the UI. The chart shows the lead badge (crown) on concierge and
the three edges you created.

---

## Business Rules Quick Reference

These come from the platform and are enforced on every API call. Know them
before you start:

| Rule | What happens |
|------|-------------|
| Nodes require agents you own | 404 if `agent_id` belongs to another user |
| One node per agent per team | 409 on duplicate |
| At most one team lead | Setting a new lead auto-unsets the previous one |
| No self-connections | 400 if `source_node_id == target_node_id` |
| One edge per `(source, target)` pair | 409 on duplicate |
| Both nodes must be in the same team | 404 if not |
| `task_prefix` is 1–10 chars, uppercase alphanumeric | 422 validation error otherwise |
| Team access is owner-only | 404 (not 403) on unauthorized access — no existence leak |
| The graph is policy, not execution | Drawing an edge permits handover; it does not run anything |

---

## How `cinna api` Exit Codes Work

| Exit code | Meaning |
|-----------|---------|
| 0 | Inner API returned 2xx — success |
| 1 | Inner API returned 4xx or 5xx — the response body explains the error |
| 2 | Platform policy refused the call (403 `excluded_path` or `excluded_method`) — not an inner API error |

When exit code is 2, the path you called is on the escape-hatch denylist.
Check `context/api_reference/` for the correct endpoint. The `agentic-teams`
surface is NOT on the denylist, so all team/node/connection operations go
through cleanly.

If an inner call returns 4xx (exit 1), the JSON body has a `"detail"` field
with the platform's error message. Read it before retrying.

---

## ID-Capture Recovery

If you lost an ID mid-build:

```bash
# Recover all node IDs and connection IDs from the chart
cinna api GET agentic-teams/$TEAM_ID/chart | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
print('=== nodes ===')
for n in d['nodes']:
    print(n['id'], n['name'], 'lead=' + str(n['is_lead']))
print('=== connections ===')
for c in d['connections']:
    print(c['id'], c['source_node_name'], '->', c['target_node_name'])
"
```

---

## Teardown / Cleanup

When you are done with the network or want to reset:

```bash
# Remove a single connection
cinna api DELETE agentic-teams/$TEAM_ID/connections/$CONN1_ID

# Remove a node (CASCADE deletes all its connections)
cinna api DELETE agentic-teams/$TEAM_ID/nodes/$CRM_NODE_ID

# Delete the whole team (CASCADE: all nodes and connections)
cinna api DELETE agentic-teams/$TEAM_ID
```

Deleting the team does NOT delete the agents — their workspaces, credentials,
and environments are unaffected. Capability credentials (agent-api,
mcp_provider) created in Step 3 also persist; delete them separately from
each agent's Credentials tab or via:

```bash
# Disconnect the agent-api connection (use the credential_id from Step 3a)
cinna api DELETE credentials/$CREDENTIAL_ID
```

Unsyncing an agent when done:

```bash
cinna agent unsync concierge
cinna agent unsync crm-agent
cinna agent unsync calendar-agent
cinna agent unsync cron-notifier
```

`unsync` revokes the child token and removes the local `agents/<name>/`
workspace. The platform agent and its environment are untouched.

---

## Adapting This Pattern

The meeting-booking network is one example. The same four-step sequence works
for any multi-agent topology:

1. **Create agents** — one `cinna agent create` per role.
2. **Build their logic** — each agent is independent; build in parallel.
3. **Wire capabilities** — use `cinna connect agent-api` for code-to-code REST
   calls; use `cinna connect mcp` for tool exposure via MCP (requires an
   agent-to-agent connector on the producer, created via
   `cinna api POST agents/{id}/mcp-connectors`); handle external OAuth
   integrations manually in the browser.
4. **Register the team** — `cinna api POST agentic-teams`, add nodes, draw
   edges. This encodes the delegation policy; the task system enforces it.

Teams can contain agents from any workspace you own. A single agent can belong
to multiple teams. The chart the `GET …/chart` endpoint returns is your ground
truth; always read it back to verify.

For endpoint details beyond what this playbook shows, consult:

- `context/api_reference/agentic_teams.md` — full request/response shapes for
  all 17 agentic-teams endpoints.
- `context/api_reference/agents.md` — agent CRUD, MCP connectors, agent-api,
  schedules.
- `context/platform/application/mcp_integration/agent_to_agent_mcp_connector.md`
  — producer/consumer model, auth modes, SDK injection.
- `context/platform/agents/agent_api/agent_api.md` — REST API producer/consumer
  model, policy.yaml guardrails, credential lifecycle.
