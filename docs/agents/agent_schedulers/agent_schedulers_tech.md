# Agent Schedulers — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/agents/agent_schedule.py` — AgentSchedule database model + request/response schemas

**Routes:**
- `backend/app/api/routes/agents.py` — Schedule CRUD and AI generation endpoints (nested under agent routes)

**Services:**
- `backend/app/services/agents/agent_scheduler_service.py` — Schedule CRUD, CRON conversion, next execution calculation
- `backend/app/services/agents/agent_schedule_scheduler.py` — Background scheduler (APScheduler) that polls and executes due schedules

**AI Function:**
- `backend/app/agents/schedule_generator.py` — Natural language to CRON conversion via LLM
- `backend/app/agents/prompts/schedule_generator_prompt.md` — Prompt template for CRON generation

**Migrations:**
- `backend/app/alembic/versions/7ef8eae8f523_add_agent_schedule_table.py` — Creates `agent_schedule` table
- `backend/app/alembic/versions/a4c8d9e0f1b2_add_name_prompt_to_agent_schedule.py` — Adds `name` and `prompt` fields

**Tests:**
- `backend/tests/api/agents/agent_schedules_test.py` — Integration tests (lifecycle, CRON conversion, permissions)
- `backend/tests/utils/schedule.py` — Test utilities (generate, create, list, update, delete)

### Frontend

**Components:**
- `frontend/src/components/Agents/AgentSchedulesCard.tsx` — Main schedule management card (list, create dialog, edit dialog, toggle, delete)

**Integration:**
- `frontend/src/components/Agents/AgentConfigTab.tsx` — Renders `AgentSchedulesCard` alongside `AgentHandovers` in a 2-column grid

## Database Schema

**Table:** `agent_schedule`

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID (PK) | Schedule identifier |
| `agent_id` | UUID (FK → agent.id, CASCADE) | Parent agent |
| `name` | str | User-friendly label (e.g., "Daily data collection") |
| `cron_string` | str | CRON expression in UTC |
| `description` | str | Human-readable description from AI |
| `enabled` | bool (default: true) | Whether schedule is active |
| `prompt` | Text, nullable | Schedule-specific prompt (null = use agent's entrypoint_prompt) |
| `last_execution` | datetime, nullable | When schedule last ran |
| `next_execution` | datetime | Pre-calculated next run time (UTC) |
| `created_at` | datetime | Record creation timestamp |
| `updated_at` | datetime | Last modification timestamp |

**Relationship:** Many-to-one with Agent (`agent.schedules`, cascade delete)

## API Endpoints

All endpoints in `backend/app/api/routes/agents.py`, nested under `/api/v1/agents/{id}/schedules`. All verify agent ownership.

| Method | Path | Purpose | Request | Response |
|--------|------|---------|---------|----------|
| POST | `/{id}/schedules/generate` | AI CRON generation (stateless preview) | `ScheduleRequest` | `ScheduleResponse` |
| POST | `/{id}/schedules` | Create schedule | `CreateScheduleRequest` | `AgentSchedulePublic` |
| GET | `/{id}/schedules` | List all schedules | — | `AgentSchedulesPublic` |
| PUT | `/{id}/schedules/{schedule_id}` | Update schedule (partial) | `UpdateScheduleRequest` | `AgentSchedulePublic` |
| DELETE | `/{id}/schedules/{schedule_id}` | Delete schedule | — | `Message` |

### Request/Response Models

Defined in `backend/app/models/agents/agent_schedule.py`:

- **ScheduleRequest** — `natural_language: str`, `timezone: str` (for AI generation)
- **ScheduleResponse** — `success: bool`, `description`, `cron_string`, `next_execution` (ISO 8601), `error`
- **CreateScheduleRequest** — `name`, `cron_string`, `timezone` (transient, for conversion), `description`, `prompt` (optional), `enabled`
- **UpdateScheduleRequest** — All fields optional; `timezone` required when `cron_string` changes
- **AgentSchedulePublic** — Full schedule fields for API response
- **AgentSchedulesPublic** — `data: list[AgentSchedulePublic]`, `count: int`

## Services & Key Methods

### Scheduler Service — `backend/app/services/agents/agent_scheduler_service.py`

| Method | Purpose |
|--------|---------|
| `convert_local_cron_to_utc(cron_string, timezone)` | Converts CRON from user's local timezone to UTC |
| `calculate_next_execution(cron_string)` | Calculates next run time from UTC CRON using croniter |
| `generate_schedule_preview(natural_language, timezone)` | Orchestrates AI call + CRON conversion + next execution calculation |
| `create_schedule(session, agent_id, name, cron_string, timezone, description, prompt, enabled)` | Creates AgentSchedule record with CRON conversion |
| `get_agent_schedules(session, agent_id)` | Lists all schedules for an agent, ordered by created_at |
| `get_schedule_by_id(session, schedule_id)` | Gets single schedule |
| `update_schedule(session, schedule_id, **fields)` | Partial update; recalculates next_execution if cron_string changes |
| `delete_schedule(session, schedule_id)` | Deletes schedule by ID |
| `get_all_enabled_schedules(session)` | Returns all enabled schedules (for background polling) |
| `update_execution_time(session, schedule_id, last_execution)` | Updates timestamps after execution |
| `verify_agent_access(session, agent_id, user)` | Validates user owns the agent |
| `get_schedule_for_agent(session, schedule_id, agent_id)` | Validates schedule belongs to agent |

### Background Scheduler — `backend/app/services/agents/agent_schedule_scheduler.py`

- Uses **APScheduler BackgroundScheduler**
- Polls every 1 minute for due schedules (`next_execution <= now`, `enabled = true`)
- For each due schedule: verifies agent is active, resolves prompt (schedule > agent entrypoint > fallback), creates session via `SessionService.send_session_message()`, updates execution times
- Error handling: logs errors without advancing schedule on failure
- Started/stopped via app lifecycle hooks in `backend/app/main.py`

### AI Schedule Generator — `backend/app/agents/schedule_generator.py`

- Loads prompt from `backend/app/agents/prompts/schedule_generator_prompt.md`
- Passes user input + current time + timezone to LLM via provider manager (cascade selection)
- Returns `{ success, description, cron_string }` or `{ success: false, error }`
- CRON output is in **local time** — backend converts to UTC

## Frontend Components

### AgentSchedulesCard — `frontend/src/components/Agents/AgentSchedulesCard.tsx`

- **Props:** `{ agentId: string }`
- **Query:** `useQuery` with key `["agent-schedules", agentId]`, calls `AgentsService.listSchedules()`
- **Mutations:** create, update, toggle (`{enabled: !current}`), delete — all invalidate query key
- **Pattern:** Follows `McpConnectorsCard` pattern (same state management, dialog approach, list layout)

**Create Dialog:**
1. Name input (required)
2. Natural language timing input + "Generate" button (calls `/schedules/generate`, shows preview)
3. Prompt textarea (optional — placeholder: "Leave empty to use agent's entrypoint prompt")
4. "Create" button (disabled until name + generated schedule present)

**Edit Dialog:**
1. Name input (pre-populated)
2. Current schedule info display (description, next execution)
3. Natural language input + "Generate" button (to change timing)
4. Prompt textarea (pre-populated)
5. "Save" button

**Schedule Row:** Name (bold), description (muted), next execution time, badges (enabled/disabled, "Custom prompt"), action buttons (edit, toggle, delete with confirmation)

### Integration in AgentConfigTab — `frontend/src/components/Agents/AgentConfigTab.tsx`

Renders `AgentSchedulesCard` and `AgentHandovers` in a 2-column responsive grid (`grid-cols-1 lg:grid-cols-2`).

## Configuration

- No feature flags — scheduling is always available
- Minimum CRON interval (30 minutes) enforced in the AI prompt template, not as a backend config
- Background scheduler poll interval: 1 minute (hardcoded in `agent_schedule_scheduler.py`)

## Security

- All schedule endpoints verify agent ownership before any operation
- PUT/DELETE endpoints additionally verify `schedule.agent_id == agent_id` (prevents cross-agent access)
- Schedule generation endpoint is stateless — does not persist anything, safe for preview
- Background scheduler runs server-side with direct DB access (no user auth context)
