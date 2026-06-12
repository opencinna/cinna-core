# Agent Schedulers

## Purpose

Allows users to configure multiple automatic execution schedules per agent using natural language instead of CRON syntax. Each agent can have multiple schedules with different timings, types, and custom prompts, enabling varied actions on different cadences (e.g., daily data collection + weekly summary report + hourly health check script).

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Agent Schedule** | A named, recurring execution rule attached to an agent with its own timing, type, and configuration |
| **Schedule Type** | Discriminator that determines execution behavior: `static_prompt` or `script_trigger` |
| **Static Prompt** | Schedule type that always creates a new agent session with a prompt — best for agentic workflows that need a conversational starting point |
| **Script Trigger** | Schedule type that executes a shell command in the agent environment and only creates a session if the output is not "OK" — best for predefined workflows that only need agent interaction when something requires attention |
| **Natural Language Scheduling** | AI-powered conversion of phrases like "every workday at 7 AM" into CRON expressions |
| **Schedule Prompt** | Optional per-schedule message that overrides the agent's entrypoint prompt when a static_prompt schedule fires |
| **CRON String** | Standard 5-field expression (`minute hour day month day_of_week`) stored in UTC |
| **Next Execution** | Pre-calculated UTC timestamp of when the schedule will fire next |
| **Execution Log** | Immutable record of each schedule execution attempt with status, output, and session reference |

## User Stories / Flows

### Creating a Schedule

1. User navigates to Agent Config Tab > "Schedules" card
2. User clicks "New" button
3. **Type selector** appears with two cards: "Static Prompt" and "Script Trigger"
4. User selects a schedule type by clicking the corresponding card
5. Type-specific form opens (see below)

**Static Prompt form:**
- Name input, timing input + "Generate" button, optional prompt textarea
- Same as original schedule creation flow

**Script Trigger form:**
- Name input, timing input + "Generate" button, command input (single-line, max 2000 chars)
- No prompt textarea — the command replaces it
- Helper text explains: if command returns "OK", no session is started; any other output triggers a session with the execution context

6. User fills out the form and clicks "Create"

### Editing a Schedule

1. User clicks edit (pencil) button on a schedule row
2. Edit dialog opens pre-populated with current values
3. For `static_prompt`: shows name, timing, prompt fields
4. For `script_trigger`: shows name, timing, command field (no prompt)
5. Schedule type **cannot be changed** after creation
6. User clicks "Save"

### Viewing Execution Logs

1. User clicks the history (clock) button on a schedule row
2. Execution logs modal opens showing the last 50 executions
3. Each row shows: type badge, execution timestamp, color-coded status, expandable details
4. Expandable details include: command/prompt used, command output (monospace), exit code, session link (if any), error message (if any)

### Toggling and Deleting

- **Toggle**: Click power button to enable/disable without deleting the schedule
- **Delete**: Click trash button > confirmation dialog > schedule permanently removed

### Manual Execution (Run Now)

1. User clicks the "Run now" (Play) button on a schedule row
2. The backend resolves the agent's active environment status and responds immediately:
   - **Environment running** → schedule executes synchronously; success toast shows **"Schedule triggered successfully"**
   - **Environment suspended / stopped / activating / starting** → activation is kicked off in the background; success toast shows **"Environment is starting; the schedule will run automatically once it's ready."** The schedule executes as soon as the environment comes up
   - **No active environment or environment in error state** → toast shows a 400 error message; no execution is attempted

### Schedule Execution (Background)

1. Background scheduler polls every minute for due schedules
2. For each schedule where `next_execution <= now` and `enabled = true`:

**Static Prompt execution:**
- Determines message: schedule prompt > agent entrypoint prompt > "Start scheduled execution."
- Creates a new session and sends the message
- Creates execution log with `status="success"` and session reference
- Updates `last_execution` and recalculates `next_execution`

**Script Trigger execution:**
- Resolves the agent's active environment (auto-activates if suspended/stopped)
- Executes the command in the agent environment via the `/exec` endpoint
- **If exit code == 0 AND stdout.strip() == "OK"**: creates a `schedule_executed` activity, logs with `status="success"`, no session created, no tokens consumed
- **If output != "OK" or non-zero exit code**: builds a context message with command, output, and errors, then creates a session with that context; logs with `status="session_triggered"`
- **If execution fails** (timeout, network error, env unavailable): logs with `status="error"`, does not advance schedule

## Business Rules

### Schedule Types

| Type | When to Use | Token Usage | Session Creation |
|------|-------------|-------------|------------------|
| `static_prompt` | Agentic workflows that always need a conversational starting point | Every execution consumes tokens | Always |
| `script_trigger` | Direct scenarios covered by predefined scripts/workflows | Only when script output != "OK" | Conditional |

- Schedule type is set at creation and **cannot be changed** afterward
- `static_prompt`: `command` must be null; `prompt` is optional
- `script_trigger`: `command` must be non-empty (max 2000 chars); `prompt` is ignored

### Script Trigger Output Rules

- Output comparison: `stdout.strip() == "OK"` (case-sensitive, trimmed)
- Empty stdout with exit code 0: treated as NOT OK — explicit "OK" is required
- Stderr present with exit code 0 and stdout "OK": still treated as OK (stderr is informational)
- Non-zero exit code with any output: treated as NOT OK — session created with context
- Output truncated at 10,000 characters (with `[output truncated]` marker)

### Script Trigger Context Message

When a script trigger produces non-OK output, the session receives a context message:
- Schedule name, command executed, execution timestamp, exit code
- Full stdout (truncated if needed)
- Stderr (if any)
- Prompt: "Please review the output above and take appropriate action."

### Environment Auto-Activation

Both schedule types auto-activate the agent environment if it is not running:
- `suspended` → activates and waits until running
- `stopped` → starts and waits until running
- `activating`/`starting` → waits until running (another process may have triggered it)
- `error` → logs error, skips execution
- Activation timeout: 120 seconds

For **manual execution** (Run Now), the behavior differs from the cron path: instead of blocking the HTTP request for up to 120 s, the route returns immediately with a deferred-execution notification and the activation runs in a background task. The user sees the "Environment is starting" toast right away rather than waiting for the environment to come up.

### Command Execution

- Commands execute **inside** the agent's Docker container (same sandbox as agent SDK tool calls)
- Default timeout: 120 seconds, maximum 300 seconds
- Working directory: `/app/workspace/`
- Timeout or network errors are treated as errors (no session created)

### Execution Logging

All schedule executions (both types) create an immutable `AgentScheduleLog` record:

| Status | Meaning |
|--------|---------|
| `success` | static_prompt: session created; script_trigger: command returned "OK" |
| `session_triggered` | script_trigger only: command returned non-OK, session was created |
| `error` | Execution failed (timeout, network error, env not available) |

- Logs are append-only, never updated
- Deleted only via cascade when parent schedule or agent is deleted
- Last 50 logs are visible per schedule in the UI

### Frequency Constraints

**Minimum execution interval is per schedule type, enforced deterministically by the backend (not the AI):**

| Schedule Type | Minimum interval |
|---------------|------------------|
| `static_prompt` | **10 minutes** — every execution spins up a session and spends tokens |
| `script_trigger` | **None** — the command usually no-ops (returns "OK") without a session or tokens, so it may run as frequently as needed |

- The limit is computed from the actual CRON cadence — the smallest gap between two consecutive fire times — not the nominal phrase. For example `*/40` ("every 40 minutes") fires at `:00` and `:40`, a real 20-minute minimum gap, which is allowed for both types.
- The AI generator (`schedule_generator_prompt`) no longer gates on frequency; it faithfully translates any cadence to CRON. The backend (`AgentSchedulerService.validate_frequency`) is the single source of truth and rejects too-frequent `static_prompt` schedules on **generate-preview, create, and update**.
- `static_prompt` valid: "every hour", "every 30 minutes", "every 10 minutes", "every day at 9 AM"
- `static_prompt` invalid: "every 5 minutes", "every minute" (rejected as too frequent)
- `script_trigger`: any cadence accepted, including "every 5 minutes" or "every minute"

### Timezone Handling

- User's timezone is extracted from the browser (`Intl.DateTimeFormat().resolvedOptions().timeZone`)
- Timezone is passed transiently in create/update requests for CRON conversion — it is **NOT stored**
- AI interprets the schedule in user's local timezone, returns CRON in local time
- Backend converts CRON from local time to UTC before storage
- Next execution time is displayed in user's browser timezone

**Example**: User says "every day at 7 AM" in Europe/Berlin (UTC+1) → CRON stored as `0 6 * * *` (6 AM UTC) → Displayed as "7:00 AM CET"

### Schedule-Specific Prompts

Each static_prompt schedule can have its own prompt field:
- If `prompt` is set: that prompt is used when the schedule fires
- If `prompt` is null: the agent's `entrypoint_prompt` is used as fallback
- If both are null: "Start scheduled execution." is used as final fallback

### Cloned Agents and Scheduling

- Standalone agents and publisher installs have full CRUD over their schedules — create, edit, delete, toggle, Run now, view logs

### Bundle-propagated Schedules

When an agent developer publishes a bundle, their `AgentSchedule` rows are snapshotted into the revision (see [Agent Bundles & Installs](../agent_bundles/agent_bundles.md)). When another user installs the bundle, those schedules are materialised on their install as ordinary `AgentSchedule` rows with the publisher's `enabled` state and a freshly computed `next_execution`. The background scheduler then runs them inside the consumer's own environment and sessions — no difference from the consumer's perspective.

**Consumer read-only rule:** A foreign install (non-publisher consumer) cannot create, edit, or delete bundle schedules — the schedule definitions are publisher-authored. The consumer can:
- Toggle enable/disable (Power button)
- Run now (Play button)
- View execution logs (Logs button)

New/Edit/Delete controls are hidden in the UI and the routes enforce this server-side (see Tech details).

**Apply-update merge (behavioral signature):** When the consumer applies a bundle update, schedules are reconciled using a **behavioral signature** = `(schedule_type, cron_string, command, prompt)`. Name and description are cosmetic and excluded from identity.

- If the signature is unchanged: the existing row (with its `enabled` state, `next_execution`, `last_execution`, and logs) is kept; only cosmetic `name`/`description` fields are refreshed from the new revision
- If the signature changed (different cron, command, prompt, or type): the old row is deleted and a new one is created with the publisher's `enabled` state and a recomputed `next_execution`
- Schedules added by the publisher are created; schedules removed by the publisher are deleted

**Duplicate signature limitation:** If a revision contains two schedule definitions with the same behavioral signature, the later one wins (the earlier is silently discarded).

Publisher installs are unaffected — they retain full CRUD over their schedules as the source of truth for the next publish.

## Architecture Overview

```
User → Frontend (AgentSchedulesCard)
         │
         ├── Type Selector (cards: Static Prompt / Script Trigger)
         │     │
         │     ├── Static Prompt form (name, timing, prompt)
         │     └── Script Trigger form (name, timing, command)
         │
         └── Backend API (agents routes)
               │
               ├── POST /schedules/generate → AI Function (schedule_generator) → LLM
               ├── CRUD /schedules → Scheduler Service → Database (AgentSchedule)
               ├── GET /schedules/{id}/logs → AgentScheduleLog records
               │
               └── Background Scheduler (polls every 1 min)
                     │
                     ├── static_prompt → SessionService → Session Creation → AgentScheduleLog
                     └── script_trigger → auto-activate env → AgentEnvConnector.exec_command()
                           │
                           ├── OK → ActivityService (schedule_executed) → AgentScheduleLog
                           └── Other → SessionService (with context) → AgentScheduleLog
```

## Visual Layout

```
┌──────────────────────────────────────────────────────────────────┐
│ Schedules                                                  [New] │
│ Schedule execution times for this agent with different           │
│ prompts and cadences                                             │
├──────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Daily data collection  [Enabled] [Custom prompt]     L E T D │ │
│ │ Every weekday at 7:00 AM, CET                                │ │
│ │ Next: Monday, March 3, 2026 at 7:00 AM CET                  │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Health check           [Enabled] [Script trigger]    L E T D │ │
│ │ Every hour from 9 AM to 5 PM, Mon-Fri                        │ │
│ │ bash scripts/health_check.sh                                 │ │
│ │ Next: Monday, March 3, 2026 at 9:00 AM CET                  │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Weekly summary         [Enabled]                     L E T D │ │
│ │ Every Friday at 5:00 PM, CET                                 │ │
│ │ Next: Friday, March 7, 2026 at 5:00 PM CET                  │ │
│ └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘

L = Logs, E = Edit, T = Toggle enabled/disabled, D = Delete with confirmation
```

## Common Use Cases

**Daily data collection + weekly summary (static_prompt):**
- Schedule 1: "Daily data collection" — "every workday at 7 AM" — prompt: "Collect today's market data snapshots"
- Schedule 2: "Weekly summary" — "every Friday at 5 PM" — prompt: "Produce a weekly market summary report"

**Automated health check (script_trigger):**
- Schedule: "Hourly health check" — "every hour during work hours" — command: `bash scripts/health_check.sh`
- If script returns "OK": logged silently, no tokens used
- If script returns error details: agent session created with context to investigate

**Mixed monitoring setup:**
- Schedule 1 (script_trigger): "DB check" — `python scripts/check_db.py` — runs every 30 min, only alerts on issues
- Schedule 2 (static_prompt): "Daily incident report" — "every day at 6 PM" — prompt: "Generate daily incident summary from today's alerts"

**Error cases:**
- Too frequent `static_prompt` → backend returns error: "Execution frequency too high: minimum interval for static prompt schedules is 10 minutes, but this schedule would run every 5 minutes." (`script_trigger` has no such limit)
- Vague input → AI returns error: "Cannot extract schedule: 'sometimes' is too vague. Please specify exact time or frequency."

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Schedule execution creates new sessions via SessionService (always for static_prompt, conditionally for script_trigger)
- **[Agent Environments](../agent_environments/agent_environments.md)** — Script trigger executes commands inside the agent's Docker container via `/exec` endpoint; both types auto-activate environments
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — Falls back to agent's `entrypoint_prompt` when static_prompt schedule has no custom prompt
- **[Agent Bundles & Installs](../agent_bundles/agent_bundles.md)** — Schedules are snapshotted into bundle revisions at publish time; materialised onto consumer installs at install time; merged on apply-update using behavioral signatures; consumers can only enable/disable and run bundle-owned schedules
- **[Task Triggers](../../application/input_tasks/task_triggers.md)** — Separate but related feature: task-level CRON/webhook/date triggers use similar AI schedule generation
- **[AI Functions](../../development/backend/ai_functions_development.md)** — Schedule generation uses the AI function framework with multi-provider cascade
