# Agent Smart Scheduler

## Overview

The **Agent Smart Scheduler** allows users to configure automatic execution of agents using natural language instead of manually crafting CRON expressions. Users can type phrases like "every workday in the morning at 7" and an AI function converts this to a proper CRON string with timezone awareness.

## User Experience Flow

### 1. Scheduler Configuration UI

**Location**: `frontend/src/routes/_layout/agent/$agentId.tsx` → Config Tab (first tab) → Above prompts section

**Components**:
- **Enable/Disable Toggle** - Switch to enable scheduler feature (similar to mode toggle in `frontend/src/routes/_layout/index.tsx:334`)
- **Scheduler Form** (appears only when toggle is enabled):
  - **Text input field** - For natural language schedule input
  - **"Schedule Smart" button** - Triggers AI conversion (also triggered by Enter key)
  - **Explanation display** - Shows AI's interpretation in human-readable format
  - **Next execution display** - Shows when the agent will run next
  - **"Apply" button** - Saves scheduler configuration (appears only when changes detected)

### 2. User Interaction Sequence

```
1. User navigates to Agent Config page → Config tab
2. User sees "Scheduler" section above prompts with enable/disable toggle
3. User toggles the switch to enable scheduler
4. Scheduler form appears below the toggle
5. User types: "every workday in the morning at 7"
6. User clicks "Schedule Smart" (or presses Enter)
7. AI function processes input with user's timezone (e.g., CET from browser)
   - AI returns: CRON string and refined description
8. Backend calculates next execution time from CRON string
9. System displays:
   - Description: "Every weekday at 7:00 AM, Central European Time"
   - Next execution: "Monday, December 30, 2025 at 7:00 AM CET" (calculated by backend)
   - CRON string: "0 6 * * 1-5" (in UTC, optionally shown)
10. User clicks "Apply" to save the schedule
11. AgentSchedule record is created/updated in database with enabled=true
```

**Disabling Scheduler**:
```
1. User toggles the switch to disable
2. Confirmation dialog may appear (if schedule exists)
3. Schedule is updated with enabled=false (or deleted based on UX preference)
4. Scheduler form collapses
```

### 3. Visual Layout Example

**When Scheduler is Disabled**:
```
┌─────────────────────────────────────────────────────────┐
│ Config Tab                                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Scheduler                                               │
│ Enable automatic execution  [○─────]  (OFF)             │
│                                                         │
│ ─────────────────────────────────────────────────────── │
│                                                         │
│ Prompts                                                 │
│ ...                                                     │
└─────────────────────────────────────────────────────────┘
```

**When Scheduler is Enabled**:
```
┌─────────────────────────────────────────────────────────┐
│ Config Tab                                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ Scheduler                                               │
│ Enable automatic execution  [─────●]  (ON)              │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ every workday in the morning at 7                   │ │
│ └─────────────────────────────────────────────────────┘ │
│ [Schedule Smart]                                        │
│                                                         │
│ ✓ Schedule: Every weekday at 7:00 AM, Central European │
│   Time                                                  │
│ ⏰ Next run: Monday, December 30, 2025 at 7:00 AM CET  │
│                                                         │
│ [Apply]                                                 │
│                                                         │
│ ─────────────────────────────────────────────────────── │
│                                                         │
│ Prompts                                                 │
│ ...                                                     │
└─────────────────────────────────────────────────────────┘
```

## Business Rules

### Frequency Constraints

**Minimum execution interval: 30 minutes**

This prevents:
- Excessive API usage
- Server overload
- Unintentional infinite loops
- Cost escalation

**Valid examples**:
- ✅ "every hour"
- ✅ "every 30 minutes"
- ✅ "every day at 9 AM"
- ✅ "every Monday at 3 PM"

**Invalid examples**:
- ❌ "every 5 minutes" (too frequent)
- ❌ "every minute" (too frequent)
- ❌ "every 15 minutes" (too frequent)

### Timezone Handling

**User timezone is passed from browser to AI function**

- Frontend extracts timezone: `Intl.DateTimeFormat().resolvedOptions().timeZone`
- Example values: `"America/New_York"`, `"Europe/Berlin"`, `"Asia/Tokyo"`
- AI function interprets schedule in user's timezone
- CRON string is stored in UTC for consistent execution
- Next execution time is displayed in user's local timezone

**Example**:
```
User input: "every day at 7 AM"
User timezone: "Europe/Berlin" (UTC+1)
CRON string: "0 6 * * *" (6 AM UTC = 7 AM CET)
Display: "Every day at 7:00 AM, Central European Time"
```

### Configuration Decoupling

**Scheduler configuration is independent from prompts**

- Scheduler has its own "Apply" button
- Prompts section has separate "Save prompts" button
- User can update scheduler without affecting prompts
- User can update prompts without affecting scheduler
- Both can be updated in the same session but saved separately

### Cloned Agents and Scheduler

**Key Principle**: Each clone has independent scheduler configuration from its parent.

**Clone Owner Capabilities**:
- Clone owners (both "user" and "builder" modes) can create, edit, and delete their own schedules
- Scheduler UI is always available in the Configuration tab for clone owners
- Clone's schedule is completely independent from parent's schedule

**Behavior During Agent Sharing**:
- When an agent is shared/cloned, the scheduler configuration is **NOT copied** to the clone
- The clone starts with **no scheduler** configured
- Clone owner must set up their own schedule if needed

**Behavior During Push Updates**:
- When the parent agent owner pushes updates to clones, **scheduler configs are NOT synced**
- Push updates only sync workspace files (scripts, docs, knowledge)
- Each clone's scheduler configuration remains completely independent
- This is intentional: clone owners may have different:
  - Automation needs and frequencies
  - Timezones
  - Business hours and availability

**Example Scenario**:
```
Original Agent (Owner: Alice, Timezone: US/Eastern)
├── Schedule: "Every weekday at 9 AM Eastern"
│
└── Clone (Owner: Bob, Timezone: Europe/Berlin)
    ├── Initially: No schedule configured
    └── Bob creates: "Every weekday at 8 AM Berlin time"

When Alice pushes updates:
- Bob's prompts and scripts are updated
- Bob's schedule remains unchanged (still "Every weekday at 8 AM Berlin time")
```

### State Management

**Changes are not auto-saved**

- User must explicitly click "Apply" to save scheduler changes
- "Apply" button only appears when changes are detected
- Changes detected when:
  - AI generates new CRON string different from saved one
  - User clears the scheduler (removes schedule)
  - User modifies and re-processes the input

### Enable/Disable Toggle Behavior

**Initial State**:
- If agent has no schedule: toggle is OFF, form is hidden
- If agent has schedule with `enabled=true`: toggle is ON, form is visible with schedule details
- If agent has schedule with `enabled=false`: toggle is OFF, form is hidden

**Enabling Scheduler** (toggle OFF → ON):
- Scheduler form appears
- If previous schedule exists, load it into the form
- Otherwise, show empty form for new schedule

**Disabling Scheduler** (toggle ON → OFF):
- Immediate action: delete the schedule or set `enabled=false` in database
- Scheduler form collapses
- User confirmation recommended if active schedule exists

**UX Considerations**:
- Toggle state should persist across page reloads
- Disabling should be quick (no confirmation for better UX, unless there are active executions)
- Re-enabling should restore previous configuration if available

## AI Function Specification

### Function Name
`generate_agent_schedule`

### Input

**Parameters**:
```python
{
    "natural_language": str,  # User's natural language input
    "timezone": str,          # IANA timezone (e.g., "Europe/Berlin")
}
```

**Example**:
```json
{
    "natural_language": "every workday in the morning at 7",
    "timezone": "Europe/Berlin"
}
```

### Output

**Success Response**:
```python
{
    "success": True,
    "description": str,        # Human-readable interpretation
    "cron_string": str,        # Standard CRON expression (in UTC)
}
```

**Example Success**:
```json
{
    "success": true,
    "description": "Every weekday at 7:00 AM, Central European Time",
    "cron_string": "0 6 * * 1-5"
}
```

**Note**: The backend will calculate `next_execution` from the CRON string after receiving the AI function response.

**Error Response**:
```python
{
    "success": False,
    "error": str,              # Explanation of why conversion failed
}
```

**Example Errors**:
```json
{
    "success": false,
    "error": "Cannot extract schedule: the phrase 'sometimes' is too vague. Please specify exact time or frequency."
}

{
    "success": false,
    "error": "Execution frequency too high: minimum interval is 30 minutes. Your input 'every 5 minutes' is not allowed."
}

{
    "success": false,
    "error": "Cannot extract schedule: please specify when you want the agent to run (e.g., time of day, day of week)."
}
```

### AI Function Behavior

**The AI function should**:

1. **Parse natural language with timezone context**
   - Understand common phrases: "morning" (7-9 AM), "evening" (6-8 PM), "noon" (12 PM)
   - Interpret "workday" as Monday-Friday
   - Handle relative terms: "every hour", "daily", "weekly"

2. **Generate valid CRON string in UTC**
   - Convert user's local time to UTC
   - Use standard CRON format: `minute hour day month day_of_week`
   - Example: `0 6 * * 1-5` = Every weekday at 6 AM UTC (7 AM CET)

3. **Validate minimum frequency (30 minutes)**
   - Reject schedules that run more frequently than once per 30 minutes
   - Return error with clear explanation

4. **Return precise human-readable description**
   - Include exact time (not vague terms like "morning")
   - Include timezone name or abbreviation
   - Use natural language: "Every weekday at 7:00 AM, Central European Time"

5. **Handle ambiguous or incomplete input**
   - Identify missing information (e.g., "every day" without time)
   - Request clarification in error message
   - Suggest what information is needed

### Implementation Pattern

Following the **AI Functions Development Guide** pattern:

**File**: `backend/app/agents/schedule_generator.py`

```python
from google.genai import Client
from pathlib import Path
import json
from datetime import datetime
import pytz

PROMPTS_DIR = Path(__file__).parent / "prompts"
SCHEDULE_PROMPT = PROMPTS_DIR / "schedule_generator_prompt.md"

def generate_agent_schedule(
    natural_language: str,
    timezone: str,
    api_key: str
) -> dict:
    """
    Convert natural language schedule to CRON string.

    Args:
        natural_language: User's input (e.g., "every workday at 7 AM")
        timezone: IANA timezone (e.g., "Europe/Berlin")
        api_key: Google API key

    Returns:
        Dict with success, description, cron_string, or error
    """
    client = Client(api_key=api_key)

    # Load prompt template
    template = SCHEDULE_PROMPT.read_text(encoding="utf-8")

    # Construct prompt with user input
    prompt = f"""{template}

---

## User Input

Natural language: {natural_language}
User timezone: {timezone}
Current time: {datetime.now(pytz.timezone(timezone)).isoformat()}

Generate the schedule configuration in JSON format.
"""

    # Call LLM
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )

    # Parse response (expected to be JSON)
    result = json.loads(response.text.strip())

    return result
```

**Note**: The AI function only returns the CRON string and description. The backend service (`agent_scheduler_service.py`) will calculate `next_execution` time.

**Prompt Template**: `backend/app/agents/prompts/schedule_generator_prompt.md`

Should include:
- Task description
- CRON format explanation
- Timezone conversion instructions
- Minimum frequency validation (30 minutes)
- Examples of good vs bad interpretations
- JSON output format specification
- Common time phrases and their meanings
- Error handling guidelines

## Backend Implementation

### Architecture Overview

**Component Responsibilities**:

1. **AI Function** (`backend/app/agents/schedule_generator.py`)
   - Converts natural language to CRON string
   - Returns: `{ success, description, cron_string }` OR `{ success: false, error }`
   - Does NOT calculate next execution time

2. **Scheduler Service** (`backend/app/services/agent_scheduler_service.py`)
   - Calculates next execution time from CRON string
   - Creates/updates `AgentSchedule` records
   - Manages schedule CRUD operations
   - Business logic layer between API and database

3. **API Routes** (`backend/app/api/routes/agents.py`)
   - `POST /{agent_id}/schedule` - Generate schedule from natural language
   - `PUT /{agent_id}/schedule` - Save schedule configuration
   - `GET /{agent_id}/schedule` - Get current schedule
   - `DELETE /{agent_id}/schedule` - Delete schedule

4. **Database Model** (`AgentSchedule`)
   - Stores schedule configuration
   - Tracks execution times
   - Many-to-one relationship with Agent

**Data Flow**:
```
User Input ("every workday at 7 AM")
  ↓
Frontend → POST /api/v1/agents/{id}/schedule
  ↓
API Route → AIFunctionsService.generate_schedule()
  ↓
AI Function → Returns { description, cron_string }
  ↓
API Route → AgentSchedulerService.calculate_next_execution()
  ↓
API Route → Returns { description, cron_string, next_execution }
  ↓
Frontend → Displays to user
  ↓
User clicks "Apply"
  ↓
Frontend → PUT /api/v1/agents/{id}/schedule
  ↓
API Route → AgentSchedulerService.create_or_update_schedule()
  ↓
Database → AgentSchedule record created/updated
```

### Database Schema

**New Model** (`backend/app/models.py`):

Create a separate `AgentSchedule` model with **many-to-one** relationship to `Agent`:

```python
from datetime import datetime
import uuid
from sqlmodel import Field, SQLModel, Relationship

class AgentSchedule(SQLModel, table=True):
    """
    Agent execution schedule configuration.

    Relationship: Many AgentSchedule → One Agent
    (An agent can have multiple schedules, though initially only one will be used)
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")

    # Schedule configuration
    cron_string: str  # CRON expression in UTC (e.g., "0 6 * * 1-5")
    timezone: str  # User's IANA timezone (e.g., "Europe/Berlin")
    description: str  # Human-readable description from AI
    enabled: bool = Field(default=True)  # Allow disabling without deleting

    # Execution tracking
    last_execution: datetime | None = Field(default=None)  # Last run timestamp
    next_execution: datetime  # Calculated next run timestamp

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationship
    agent: "Agent" = Relationship(back_populates="schedules")
```

**Update Agent model** to add relationship:

```python
class Agent(AgentBase, table=True):
    # ... existing fields ...

    # Relationship
    schedules: list["AgentSchedule"] = Relationship(
        back_populates="agent",
        cascade_delete=True
    )
```

**Note**: By default, agents have no schedules (the relationship list will be empty).

### Scheduler Service

**File**: `backend/app/services/agent_scheduler_service.py`

This service handles all scheduler-related business logic:

```python
from datetime import datetime
import pytz
from croniter import croniter
from sqlmodel import Session
import uuid
from app.models import AgentSchedule

class AgentSchedulerService:
    @staticmethod
    def calculate_next_execution(cron_string: str, timezone: str) -> datetime:
        """
        Calculate next execution time from CRON string.

        Args:
            cron_string: CRON expression in UTC
            timezone: User's IANA timezone

        Returns:
            Next execution datetime in user's timezone
        """
        user_tz = pytz.timezone(timezone)
        cron = croniter(cron_string, datetime.now(pytz.utc))
        next_run_utc = cron.get_next(datetime)
        next_run_local = next_run_utc.astimezone(user_tz)
        return next_run_local

    @staticmethod
    def create_or_update_schedule(
        *,
        session: Session,
        agent_id: uuid.UUID,
        cron_string: str,
        timezone: str,
        description: str,
        enabled: bool = True
    ) -> AgentSchedule:
        """
        Create or update agent schedule.

        For now, we only support one schedule per agent, so this will
        update the existing one or create new one.
        """
        # Check if schedule exists
        existing = session.query(AgentSchedule).filter(
            AgentSchedule.agent_id == agent_id
        ).first()

        next_exec = AgentSchedulerService.calculate_next_execution(
            cron_string, timezone
        )

        if existing:
            # Update existing
            existing.cron_string = cron_string
            existing.timezone = timezone
            existing.description = description
            existing.enabled = enabled
            existing.next_execution = next_exec
            existing.updated_at = datetime.utcnow()
            schedule = existing
        else:
            # Create new
            schedule = AgentSchedule(
                agent_id=agent_id,
                cron_string=cron_string,
                timezone=timezone,
                description=description,
                enabled=enabled,
                next_execution=next_exec
            )
            session.add(schedule)

        session.commit()
        session.refresh(schedule)
        return schedule

    @staticmethod
    def get_agent_schedule(
        session: Session,
        agent_id: uuid.UUID
    ) -> AgentSchedule | None:
        """Get active schedule for an agent."""
        return session.query(AgentSchedule).filter(
            AgentSchedule.agent_id == agent_id
        ).first()

    @staticmethod
    def delete_schedule(
        session: Session,
        agent_id: uuid.UUID
    ) -> bool:
        """Delete agent schedule."""
        schedule = AgentSchedulerService.get_agent_schedule(session, agent_id)
        if schedule:
            session.delete(schedule)
            session.commit()
            return True
        return False
```

### API Endpoints

**New routes**: `backend/app/api/routes/agents.py`

```python
from app.services.agent_scheduler_service import AgentSchedulerService
from app.services.ai_functions_service import AIFunctionsService

@router.post("/{agent_id}/schedule", response_model=ScheduleResponse)
def generate_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: ScheduleRequest
) -> ScheduleResponse:
    """Generate CRON schedule from natural language using AI."""
    agent = crud.get_agent(session=session, id=agent_id)
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Call AI function to generate CRON string
    ai_result = AIFunctionsService.generate_schedule(
        natural_language=data.natural_language,
        timezone=data.timezone
    )

    # If successful, calculate next execution
    if ai_result.get("success"):
        next_exec = AgentSchedulerService.calculate_next_execution(
            ai_result["cron_string"],
            data.timezone
        )
        ai_result["next_execution"] = next_exec.isoformat()

    return ScheduleResponse(**ai_result)

@router.put("/{agent_id}/schedule", response_model=AgentSchedulePublic)
def save_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
    data: SaveScheduleRequest
) -> AgentSchedule:
    """Save schedule configuration for agent."""
    agent = crud.get_agent(session=session, id=agent_id)
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Create or update schedule using service
    schedule = AgentSchedulerService.create_or_update_schedule(
        session=session,
        agent_id=agent_id,
        cron_string=data.cron_string,
        timezone=data.timezone,
        description=data.description,
        enabled=data.enabled
    )

    return schedule

@router.get("/{agent_id}/schedule", response_model=AgentSchedulePublic | None)
def get_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> AgentSchedule | None:
    """Get current schedule for agent."""
    agent = crud.get_agent(session=session, id=agent_id)
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentSchedulerService.get_agent_schedule(session, agent_id)

@router.delete("/{agent_id}/schedule")
def delete_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID,
) -> dict:
    """Delete agent schedule."""
    agent = crud.get_agent(session=session, id=agent_id)
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")

    deleted = AgentSchedulerService.delete_schedule(session, agent_id)
    return {"success": deleted}
```

**Request/Response Models**:
```python
class ScheduleRequest(SQLModel):
    """Request to generate schedule from natural language."""
    natural_language: str
    timezone: str

class ScheduleResponse(SQLModel):
    """Response from AI schedule generation."""
    success: bool
    description: str | None = None  # Human-readable explanation
    cron_string: str | None = None  # CRON expression in UTC
    next_execution: str | None = None  # ISO 8601 timestamp (calculated by backend)
    error: str | None = None  # Error message if failed

class SaveScheduleRequest(SQLModel):
    """Request to save schedule configuration."""
    cron_string: str
    timezone: str
    description: str
    enabled: bool = True

class AgentSchedulePublic(SQLModel):
    """Public response model for AgentSchedule."""
    id: uuid.UUID
    agent_id: uuid.UUID
    cron_string: str
    timezone: str
    description: str
    enabled: bool
    last_execution: datetime | None
    next_execution: datetime
    created_at: datetime
    updated_at: datetime
```

## Frontend Implementation

### Component Structure

**File**: `frontend/src/components/Agent/SmartScheduler.tsx`

```typescript
interface SmartSchedulerProps {
    agentId: string;
    currentSchedule?: {
        id: string;
        cron_string: string;
        timezone: string;
        description: string;
        enabled: boolean;
        next_execution: string;
    };
}

export function SmartScheduler({ agentId, currentSchedule }: SmartSchedulerProps) {
    // State management
    const [enabled, setEnabled] = useState(currentSchedule?.enabled || false);
    const [input, setInput] = useState("");
    const [schedule, setSchedule] = useState(currentSchedule);
    const [hasChanges, setHasChanges] = useState(false);

    // Get user timezone
    const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

    // API calls
    const generateMutation = useMutation({
        mutationFn: (naturalLanguage: string) =>
            AgentsService.generateSchedule({
                agentId,
                requestBody: { natural_language: naturalLanguage, timezone: userTimezone }
            }),
        onSuccess: (data) => {
            if (data.success) {
                setSchedule(data);
                setHasChanges(true);
            }
        }
    });

    const saveMutation = useMutation({
        mutationFn: (data: { cron_string: string; description: string; enabled: boolean }) =>
            AgentsService.saveSchedule({
                agentId,
                requestBody: {
                    cron_string: data.cron_string,
                    timezone: userTimezone,
                    description: data.description,
                    enabled: data.enabled
                }
            }),
        onSuccess: () => {
            setHasChanges(false);
            toast.success("Schedule saved successfully");
        }
    });

    const deleteMutation = useMutation({
        mutationFn: () => AgentsService.deleteSchedule({ agentId }),
        onSuccess: () => {
            setSchedule(null);
            setEnabled(false);
            setInput("");
            toast.success("Schedule disabled");
        }
    });

    const handleSchedule = () => {
        generateMutation.mutate(input);
    };

    const handleApply = () => {
        if (schedule?.cron_string) {
            saveMutation.mutate({
                cron_string: schedule.cron_string,
                description: schedule.description,
                enabled: true
            });
        }
    };

    const handleToggle = (checked: boolean) => {
        setEnabled(checked);

        if (!checked && currentSchedule) {
            // Disable existing schedule
            deleteMutation.mutate();
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between">
                <Label>Scheduler</Label>

                {/* Enable/Disable Toggle (similar to mode toggle in index.tsx:334) */}
                <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">
                        {enabled ? "Enabled" : "Disabled"}
                    </span>
                    <label className="flex cursor-pointer select-none items-center">
                        <div className="relative">
                            <input
                                type="checkbox"
                                checked={enabled}
                                onChange={(e) => handleToggle(e.target.checked)}
                                className="sr-only"
                            />
                            <div
                                className={`block h-6 w-11 rounded-full transition-colors ${
                                    enabled ? "bg-orange-400" : "bg-gray-300 dark:bg-gray-600"
                                }`}
                            ></div>
                            <div
                                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                                    enabled ? "translate-x-5" : ""
                                }`}
                            ></div>
                        </div>
                    </label>
                </div>
            </div>

            {/* Scheduler form - only visible when enabled */}
            {enabled && (
                <div className="space-y-4">
                    <div className="flex gap-2">
                        <Input
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && handleSchedule()}
                            placeholder="e.g., every workday in the morning at 7"
                        />
                        <Button onClick={handleSchedule} disabled={!input || generateMutation.isPending}>
                            Schedule Smart
                        </Button>
                    </div>

                    {schedule?.success && (
                        <div className="bg-secondary p-4 rounded-md space-y-2">
                            <div className="flex items-center gap-2">
                                <Check className="h-4 w-4 text-green-600" />
                                <span className="font-medium">{schedule.description}</span>
                            </div>
                            <div className="flex items-center gap-2 text-sm text-muted-foreground">
                                <Clock className="h-4 w-4" />
                                <span>Next run: {formatNextExecution(schedule.next_execution)}</span>
                            </div>
                        </div>
                    )}

                    {schedule?.error && (
                        <Alert variant="destructive">
                            <AlertCircle className="h-4 w-4" />
                            <AlertDescription>{schedule.error}</AlertDescription>
                        </Alert>
                    )}

                    {hasChanges && (
                        <Button onClick={handleApply} className="w-full" disabled={saveMutation.isPending}>
                            Apply Schedule
                        </Button>
                    )}
                </div>
            )}
        </div>
    );
}
```

### Integration in Agent Config Page

**File**: `frontend/src/routes/_layout/agent/$agentId.tsx`

Add SmartScheduler component in Config tab, above prompts section:

```typescript
<TabsContent value="config">
    <div className="space-y-6">
        {/* Smart Scheduler Section */}
        <SmartScheduler
            agentId={agent.id}
            currentSchedule={agent.schedule}  // Fetched from GET /agents/{id}/schedule
        />

        <Separator />

        {/* Existing Prompts Section */}
        <PromptsEditor
            agentId={agent.id}
            prompts={agent.prompts}
        />
    </div>
</TabsContent>
```

**Loading Initial Schedule State**:

When the agent config page loads, it should:
1. Fetch agent data (existing functionality)
2. Call `GET /api/v1/agents/{id}/schedule` to get current schedule (if exists)
3. Pass schedule data to `SmartScheduler` component
4. Component initializes:
   - If schedule exists and `enabled=true`: toggle ON, form visible with schedule details
   - If schedule exists and `enabled=false`: toggle OFF, form hidden
   - If no schedule: toggle OFF, form hidden

```typescript
// Example in agent config page
const { data: agent } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => AgentsService.getAgent({ agentId })
});

const { data: schedule } = useQuery({
    queryKey: ["agentSchedule", agentId],
    queryFn: () => AgentsService.getSchedule({ agentId }),
    enabled: !!agentId
});

// Pass to component
<SmartScheduler agentId={agentId} currentSchedule={schedule} />
```

## Future: Schedule Execution

**Note**: The current implementation focuses on **schedule configuration**. Actual automated execution will be handled by a separate backend script.

### Execution Script (Future Implementation)

**File**: `backend/scripts/schedule_runner.py`

This script will:
1. Run as a background service (cron daemon or systemd timer)
2. Query all `AgentSchedule` records with `enabled=True`
3. For each schedule:
   - Check if `next_execution` time has passed
   - If yes, trigger agent execution:
     - Create a new session for automated execution
     - Send initial message (from agent's entrypoint_prompt)
     - Update `last_execution` to current time
     - Calculate and update `next_execution` using `AgentSchedulerService.calculate_next_execution()`
4. Log execution results in `ScheduledExecution` table (see below)

**Database additions** (future):
```python
class ScheduledExecution(SQLModel, table=True):
    """
    Audit log for scheduled agent executions.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    schedule_id: uuid.UUID = Field(foreign_key="agentschedule.id", ondelete="CASCADE")
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    session_id: uuid.UUID | None = Field(
        foreign_key="session.id",
        ondelete="SET NULL"
    )  # Session created for this execution

    scheduled_time: datetime  # When it was supposed to run (from next_execution)
    actual_time: datetime  # When it actually ran
    status: str  # success, failed, skipped
    error_message: str | None

    created_at: datetime = Field(default_factory=datetime.utcnow)
```

**Key Workflow**:
1. Script queries: `SELECT * FROM agentschedule WHERE enabled = true AND next_execution <= NOW()`
2. For each record found:
   - Execute agent → create session
   - Log to `ScheduledExecution` table
   - Update `AgentSchedule.last_execution` and `AgentSchedule.next_execution`

## Common Use Cases

### Example 1: Daily Morning Report

**User Input**: "every day at 8 AM"
**Timezone**: "America/New_York"

**AI Function Response**:
```json
{
  "success": true,
  "description": "Every day at 8:00 AM, Eastern Time",
  "cron_string": "0 13 * * *"
}
```
(8 AM EST = 1 PM UTC)

**Backend Calculation**:
- Next execution: "2025-12-30T08:00:00-05:00" (calculated by `AgentSchedulerService`)

---

### Example 2: Weekly Summary

**User Input**: "every Monday at noon"
**Timezone**: "Europe/London"

**AI Function Response**:
```json
{
  "success": true,
  "description": "Every Monday at 12:00 PM, Greenwich Mean Time",
  "cron_string": "0 12 * * 1"
}
```

**Backend Calculation**:
- Next execution: "2025-12-30T12:00:00+00:00"

---

### Example 3: Business Hours Check

**User Input**: "every hour during work hours on weekdays"
**Timezone**: "Europe/Berlin"

**AI Function Response**:
```json
{
  "success": true,
  "description": "Every hour from 9:00 AM to 5:00 PM, Monday through Friday, Central European Time",
  "cron_string": "0 8-16 * * 1-5"
}
```
(9 AM-5 PM CET = 8 AM-4 PM UTC)

**Backend Calculation**:
- Next execution: "2025-12-30T09:00:00+01:00"

---

### Example 4: Error - Too Frequent

**User Input**: "every 10 minutes"
**Timezone**: "Asia/Tokyo"

**AI Function Response**:
```json
{
  "success": false,
  "error": "Execution frequency too high: minimum interval is 30 minutes. Your input 'every 10 minutes' is not allowed."
}
```

---

### Example 5: Error - Ambiguous

**User Input**: "sometimes in the afternoon"
**Timezone**: "America/Los_Angeles"

**AI Function Response**:
```json
{
  "success": false,
  "error": "Cannot extract schedule: the phrase 'sometimes' is too vague. Please specify exact time or frequency (e.g., 'every day at 3 PM')."
}
```

## Key Design Principles

1. **User-Friendly**: Natural language input instead of complex CRON syntax
2. **Smart**: AI understands context and common phrases
3. **Transparent**: Show exactly what the AI understood
4. **Safe**: Validate frequency to prevent abuse
5. **Timezone-Aware**: Handle user's local time correctly
6. **Decoupled**: Scheduler is independent from prompts configuration
7. **Explicit**: Changes require user confirmation (Apply button)
8. **Informative**: Show next execution time for verification
9. **Progressive Disclosure**: Form only appears when toggle is enabled, reducing UI clutter
10. **Quick Enable/Disable**: Toggle provides instant on/off control without complex forms

## Success Metrics

A successful implementation should allow users to:
- ✅ Enable/disable scheduling with a single toggle click
- ✅ Configure schedules in under 30 seconds (once enabled)
- ✅ Understand exactly when their agent will run
- ✅ Modify schedules without technical knowledge of CRON
- ✅ See schedule changes before committing them
- ✅ Avoid accidentally creating high-frequency executions
- ✅ Work in their local timezone naturally
- ✅ Have clean UI when scheduler is not needed (toggle off = form hidden)
