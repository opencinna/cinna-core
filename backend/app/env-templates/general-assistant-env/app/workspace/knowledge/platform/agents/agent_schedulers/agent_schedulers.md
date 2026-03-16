# Agent Schedulers

## Purpose

Allows users to configure multiple automatic execution schedules per agent using natural language instead of CRON syntax. Each agent can have multiple schedules with different timings and custom prompts, enabling varied actions on different cadences (e.g., daily data collection + weekly summary report).

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Agent Schedule** | A named, recurring execution rule attached to an agent with its own timing and optional prompt |
| **Natural Language Scheduling** | AI-powered conversion of phrases like "every workday at 7 AM" into CRON expressions |
| **Schedule Prompt** | Optional per-schedule message that overrides the agent's entrypoint prompt when the schedule fires |
| **CRON String** | Standard 5-field expression (`minute hour day month day_of_week`) stored in UTC |
| **Next Execution** | Pre-calculated UTC timestamp of when the schedule will fire next |

## User Stories / Flows

### Creating a Schedule

1. User navigates to Agent Config Tab > "Schedules" card
2. User clicks "New" button
3. Create dialog opens with name input, timing input + "Generate" button, and optional prompt textarea
4. User types a schedule name (e.g., "Daily data collection")
5. User types timing in natural language (e.g., "every workday in the morning at 7")
6. User clicks "Generate" — AI processes input with user's browser timezone
7. Preview displays: refined description + next execution time in user's local timezone
8. User optionally enters a custom prompt (leave empty to use agent's entrypoint prompt)
9. User clicks "Create" — new AgentSchedule record is saved

### Editing a Schedule

1. User clicks edit button on a schedule row
2. Edit dialog opens pre-populated with current values (name, description, next execution, prompt)
3. User can change name, prompt, and/or regenerate timing via the "Generate" button
4. User clicks "Save"

### Toggling and Deleting

- **Toggle**: Click power button to enable/disable without deleting the schedule
- **Delete**: Click trash button > confirmation dialog > schedule permanently removed

### Schedule Execution (Background)

1. Background scheduler polls every minute for due schedules
2. For each schedule where `next_execution <= now` and `enabled = true`:
   - Determines message: schedule prompt > agent entrypoint prompt > "Start scheduled execution."
   - Creates a new session and sends the message
   - Updates `last_execution` and recalculates `next_execution`

## Business Rules

### Frequency Constraints

**Minimum execution interval: 30 minutes**

This prevents excessive API usage, server overload, unintentional infinite loops, and cost escalation.

- Valid: "every hour", "every 30 minutes", "every day at 9 AM", "every Monday at 3 PM"
- Invalid: "every 5 minutes", "every minute", "every 15 minutes" (all rejected as too frequent)

### Timezone Handling

- User's timezone is extracted from the browser (`Intl.DateTimeFormat().resolvedOptions().timeZone`)
- Timezone is passed transiently in create/update requests for CRON conversion — it is **NOT stored**
- AI interprets the schedule in user's local timezone, returns CRON in local time
- Backend converts CRON from local time to UTC before storage
- Next execution time is displayed in user's browser timezone

**Example**: User says "every day at 7 AM" in Europe/Berlin (UTC+1) → CRON stored as `0 6 * * *` (6 AM UTC) → Displayed as "7:00 AM CET"

### Schedule-Specific Prompts

Each schedule can have its own prompt field:
- If `prompt` is set: that prompt is used when the schedule fires
- If `prompt` is null: the agent's `entrypoint_prompt` is used as fallback
- If both are null: "Start scheduled execution." is used as final fallback

This enables different actions on different cadences from the same agent.

### Cloned Agents and Scheduling

- Each clone has **independent** scheduler configuration from its parent
- Clone owners can create, edit, and delete their own schedules
- When an agent is shared/cloned, scheduler configurations are **NOT copied** to the clone
- When the parent pushes updates to clones, scheduler configs are **NOT synced** (only workspace files sync)

## Architecture Overview

```
User → Frontend (AgentSchedulesCard) → Backend API (agents routes)
                                            │
                                            ├── POST /schedules/generate → AI Function (schedule_generator) → LLM
                                            ├── CRUD /schedules → Scheduler Service → Database (AgentSchedule)
                                            │
Background Scheduler (polls every 1 min) → Service → Session Creation → Agent Environment
```

## Visual Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Schedules                                              [New] │
│ Schedule execution times for this agent with different       │
│ prompts and cadences                                         │
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Daily data collection    [Enabled] [Custom prompt]  E T D│ │
│ │ Every weekday at 7:00 AM, CET                           │ │
│ │ Next: Monday, March 3, 2026 at 7:00 AM CET             │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Weekly summary           [Enabled]                  E T D│ │
│ │ Every Friday at 5:00 PM, CET                            │ │
│ │ Next: Friday, March 7, 2026 at 5:00 PM CET             │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Hourly monitoring        [Disabled]                 E T D│ │
│ │ Every hour from 9 AM to 5 PM, Mon-Fri                   │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘

E = Edit, T = Toggle enabled/disabled, D = Delete with confirmation
```

## Common Use Cases

**Daily data collection + weekly summary:**
- Schedule 1: "Daily data collection" — "every workday at 7 AM" — prompt: "Collect today's market data snapshots"
- Schedule 2: "Weekly summary" — "every Friday at 5 PM" — prompt: "Produce a weekly market summary report"

**Different monitoring cadences:**
- Schedule 1: "Hourly health check" — "every hour during work hours" — no custom prompt (uses entrypoint)
- Schedule 2: "Daily incident report" — "every day at 6 PM" — prompt: "Generate daily incident summary"

**Error cases:**
- Too frequent input → AI returns error: "Execution frequency too high: minimum interval is 30 minutes."
- Vague input → AI returns error: "Cannot extract schedule: 'sometimes' is too vague. Please specify exact time or frequency."

## Integration Points

- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Schedule execution creates new sessions via SessionService
- **[Agent Prompts](../agent_prompts/agent_prompts.md)** — Falls back to agent's `entrypoint_prompt` when schedule has no custom prompt
- **[Agent Sharing](../agent_sharing/agent_sharing.md)** — Clone agents have independent scheduler configs; schedules are not copied or synced
- **[Task Triggers](../../application/input_tasks/task_triggers.md)** — Separate but related feature: task-level CRON/webhook/date triggers use similar AI schedule generation
- **[AI Functions](../../development/backend/ai_functions_development.md)** — Schedule generation uses the AI function framework with multi-provider cascade
