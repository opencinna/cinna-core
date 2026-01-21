# Agent Task Creation & Handover Management

## What is Agent Task Creation?

**Agent Task Creation** is a mechanism that allows conversational agents to create tasks in two modes:

1. **Direct Handover**: Agent specifies a target agent → task auto-executes
2. **Inbox Task Creation**: Agent creates task without target → goes to user's inbox

This enables both **agent-to-agent collaboration** (specialized agents working in sequence) and **agent-to-user handoff** (agents creating follow-up work for human review).

### Example Use Cases

**Direct Handover:**
A "Cryptocurrency Rate Analytic" agent analyzes market data and identifies the top 3 cryptocurrencies with growth potential. Instead of executing trades itself, it hands over to a "Cryptocurrency Trader" agent with the analysis results. The trader agent then processes the recommendations according to its own specialized workflow.

**Inbox Task:**
A code review agent identifies several refactoring opportunities during analysis. Instead of implementing them (outside its scope), it creates inbox tasks for each opportunity. The user later reviews these tasks, assigns appropriate agents, and executes them.

## Why Agent Handover?

### Problem Statement

Users often need multi-step workflows that span different domains:
- **Research → Action** (analyze data, then execute trades)
- **Processing → Notification** (complete task, then alert stakeholders)
- **Validation → Escalation** (detect issues, then notify security team)

Without handover:
- Users must manually copy results between agents
- Workflow interruption breaks automation
- Context loss when switching between sessions

### Solution Benefits

1. **Automation** - Agents can trigger follow-up agents automatically
2. **Specialization** - Each agent focuses on its domain expertise
3. **Reusability** - Same agents can be composed in different workflows
4. **Context Preservation** - Source agent defines what context to pass
5. **Flexibility** - Multiple conditional handovers per agent

## Architecture

### Data Model

**Table**: `agent_handover_config`

Represents a configured handover from one agent to another.

**Key Fields**:
- `source_agent_id` - Agent that performs the handover
- `target_agent_id` - Agent that receives the handover
- `handover_prompt` - Instructions defining WHEN and HOW to handover
- `enabled` - Toggle to disable without deleting

**Relationships**:
- Many handover configs → One source agent
- Many handover configs → One target agent
- Cascade delete when source agent is deleted

**File**: `backend/app/models/agent_handover.py`

### Handover Prompt Structure

The `handover_prompt` is a **compact instruction** (2-3 sentences) that defines:

1. **Trigger Condition** - When should handover happen?
   - Example: "Once you've identified the top 3 cryptocurrencies..."

2. **Context to Pass** - What data should be included?
   - Example: "...with the list of coins and your analysis summary"

3. **Message Format** - How should the handover message look?
   - Example: "Example: 'Here are the top 3 cryptos: BTC, ETH, SOL with analysis...'"

**Why Compact?**
The handover prompt will be added as a **tool description** in the conversational agent mode. LLMs work better with concise tool documentation, so we keep prompts to 2-3 sentences maximum.

### AI-Assisted Generation

**Challenge**: Users need to understand both agents' workflows to write effective handover prompts.

**Solution**: AI function generates draft prompts by analyzing:
- Source agent's workflow and entrypoint prompts
- Target agent's workflow and entrypoint prompts
- Logical connection points between the two

**Implementation**:
- **Agent**: `backend/app/agents/handover_generator.py`
- **Prompt Template**: `backend/app/agents/prompts/handover_generator_prompt.md`
- **Service Method**: `AIFunctionsService.generate_handover_prompt()`

The AI generates an **initial draft** that users can refine based on their specific needs.

## API Design

### Handover CRUD Operations

**List Handovers**
- `GET /agents/{id}/handovers`
- Returns all handover configs where agent is the source
- Includes target agent names for UI display

**Create Handover**
- `POST /agents/{id}/handovers`
- Body: `{ target_agent_id, handover_prompt }`
- Validates: target agent exists, user has access, no self-handover
- Creates with `enabled=true` by default

**Update Handover**
- `PUT /agents/{id}/handovers/{handover_id}`
- Body: `{ handover_prompt?, enabled? }`
- Allows updating prompt text or toggling enabled state
- Updates `updated_at` timestamp

**Delete Handover**
- `DELETE /agents/{id}/handovers/{handover_id}`
- Permanently removes configuration
- No cascade effects (only deletes config, not agents)

**Generate Prompt**
- `POST /agents/{id}/handovers/generate`
- Body: `{ target_agent_id }`
- Returns AI-generated handover prompt draft
- User can then edit and save

**Create Agent Task**
- `POST /agents/tasks/create`
- Body: `{ task_message, source_session_id, target_agent_id?, target_agent_name? }`
- Called by agent-env `create_agent_task` tool during runtime
- **Direct Handover Mode** (target_agent_id provided):
  - Creates InputTask with `agent_initiated=true`, `auto_execute=true`
  - If target agent has `refiner_prompt`, auto-refines the task message
  - Creates session linked to task and sends the (possibly refined) message
  - Logs system message in source session with task and session metadata
  - Returns task ID and session ID
- **Inbox Task Mode** (target_agent_id not provided):
  - Creates InputTask with `agent_initiated=true`, `auto_execute=false`
  - Does NOT auto-refine (user will refine manually)
  - Does NOT execute (user will select agent and execute)
  - Logs system message in source session with task metadata and `inbox_task=true`
  - Returns task ID only (no session ID)

**Execute Handover (Deprecated)**
- `POST /agents/handover/execute`
- Deprecated: Use `/agents/tasks/create` instead
- Kept for backward compatibility, delegates to create_agent_task

**File**: `backend/app/api/routes/agents.py`

## User Interface

### System Message Display

**Component**: `frontend/src/components/Chat/MessageBubble.tsx`
- Detects task creation messages via `message_metadata.task_created === true`
- Renders with distinctive blue styling
- **Direct Handover**: Displays clickable "View session" link using `session_id` from metadata
- **Inbox Task**: Displays clickable "View task" link using `task_id` from metadata (when `inbox_task=true`)
- Metadata includes:
  - `task_id`: UUID of created task (always present)
  - `inbox_task`: true for inbox tasks, absent for direct handover
  - `session_id`: UUID of created session (only for direct handover)
  - `target_agent_id`, `target_agent_name`: Target agent info (only for direct handover)

### Handover Configuration

**Primary Component**: `frontend/src/components/Agents/AgentHandovers.tsx`

Located in the **Agent Configuration Tab** (`AgentConfigTab.tsx`), below the Workflow Prompt section.

### UI Flow

1. **Add Handover**
   - Click "Add Agent Handover" button
   - Select target agent from dropdown
   - Dropdown filters out: current agent, already configured agents
   - Click "Add Handover" to create empty config

2. **Generate Draft Prompt**
   - Click "Generate" button (sparkles icon)
   - AI analyzes both agents and creates draft
   - Draft appears in textarea
   - User can accept or modify

3. **Edit Prompt**
   - Type in textarea to modify prompt
   - "Apply Prompt" button appears when changed
   - Click to save changes

4. **Enable/Disable**
   - Toggle switch next to target agent name
   - Temporarily disable without losing configuration
   - Useful for testing or debugging workflows

5. **Delete**
   - Trash icon button
   - Confirms deletion
   - Permanently removes configuration

### State Management

**Local State**:
- `editingPrompts` - Tracks unsaved changes per handover
- `dirtyPrompts` - Set of handover IDs with unsaved changes
- `selectedTargetAgent` - Currently selected agent in dropdown
- `isAddingHandover` - Toggle for add handover UI

**Server State** (TanStack Query):
- `agentHandovers` - List of configs for current agent
- `agents` - All agents for dropdown selection
- Mutations for create, update, delete, generate

## Business Rules

### Validation Rules

1. **No Self-Handover** - Source and target must be different agents
2. **Access Control** - User must own both source and target agents
3. **No Duplicates** - Cannot create multiple handovers to same target agent
4. **Cascade Delete** - Deleting agent removes all its handover configs

### Enable/Disable Logic

**Enabled** (`enabled=true`):
- Handover is active and will be presented as a tool to the agent
- Used in production workflows

**Disabled** (`enabled=false`):
- Configuration preserved but not active
- Useful for:
  - Testing alternative workflows
  - Temporarily pausing automation
  - Debugging issues without losing config

### Cloned Agents and Handover Configuration

**Key Principle**: Each clone has independent handover configuration from its parent.

**Why Handovers Are Clone-Specific**:
1. **Access Control** - Handover requires ownership of both source and target agents
2. **Different Target Agents** - Clone owner likely has different agents than the original owner
3. **Independent Workflows** - Clones may be used in different workflow contexts

**Behavior During Agent Sharing**:
- When an agent is shared/cloned, handover configs are **NOT copied** to the clone
- The clone starts with **empty handover configuration**
- The workspace file (`agent_handover_config.json`) may contain stale data from the parent, but this is overwritten on first environment activation via `_sync_dynamic_data()`

**Clone Owner Capabilities**:
- Clone owners (both "user" and "builder" modes) can create, edit, and delete their own handover configs
- Target agents must be agents the clone owner has access to
- Handover UI is available in the Configuration tab for all clone owners

**Behavior During Push Updates**:
- When the parent agent owner pushes updates to clones, **handover configs are NOT synced**
- Push updates only sync workspace files (scripts, docs, knowledge)
- Each clone's handover configuration remains completely independent
- This is intentional: handover targets are user-specific and should not be overwritten

**Example Scenario**:
```
Original Agent (Owner: Alice)
├── Handover Config → "Analysis Agent" (owned by Alice)
│
└── Clone (Owner: Bob)
    ├── Initially: No handover configs
    └── Bob creates: Handover Config → "Bob's Action Agent"

When Alice pushes updates:
- Bob's prompts and scripts are updated
- Bob's handover config remains unchanged (still points to "Bob's Action Agent")
```

## Integration with Agent Runtime

### Runtime Implementation

The handover feature is fully integrated with the agent runtime system:

#### 1. Configuration Sync to Agent-Env

When handover configs are created/updated/deleted, they are synced to the agent's environment:

**Service Method**: `AgentService.sync_agent_handover_config()` in `backend/app/services/agent_service.py`
- Queries all enabled handover configs for the agent
- Formats handover list with target agent ID, name, and prompt
- Generates consolidated handover prompt with tool usage instructions
- Pushes config to agent-env via adapter

**Automatic Sync on Environment Activation**:
Handover config is also synced as part of `_sync_dynamic_data()` in `environment_lifecycle.py`:
- Called every time an environment starts or activates
- Ensures cloned agents get empty handover config (from DB) instead of stale parent config
- Critical for shared agents where workspace files are copied but handover configs are not

**Environment Storage**: `docs/agent_handover_config.json` in agent workspace
- Contains array of configured handovers (id, name, prompt)
- Contains overall handover_prompt for system prompt inclusion

**Adapter Methods**:
- `DockerEnvironmentAdapter.set_agent_handover_config()` in `backend/app/services/adapters/docker_adapter.py`
- Calls agent-env endpoint `POST /config/agent-handovers`

#### 2. Agent-Env Configuration Management

**API Endpoints** in `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`:
- `GET /config/agent-handovers` - Retrieve current config
- `POST /config/agent-handovers` - Update config from backend

**Service Methods** in `agent_env_service.py`:
- `get_agent_handover_config()` - Loads config from JSON file
- `update_agent_handover_config()` - Saves config to JSON file

#### 3. Tool Registration in Conversation Mode

**SDK Manager** in `adapters/claude_code.py`:
- Registers `create_agent_task` tool only in conversation mode
- Tool available as `mcp__task__create_agent_task`
- Imported from `tools/create_agent_task.py`

**Tool Implementation** in `backend/app/env-templates/python-env-advanced/app/core/server/tools/create_agent_task.py`:
- Supports two modes based on parameters:
  - **Direct Handover**: Validates target agent ID against configured handovers in JSON
  - **Inbox Task**: No target validation needed, creates task for user review
- Retrieves SDK session ID via `get_current_sdk_session_id()` helper function
- Retrieves backend session ID via `get_backend_session_id()` helper function
- Calls backend API `POST /agents/tasks/create` with source session ID
- Backend handles task creation, optional session creation, and source session logging
- Returns success confirmation to agent with mode-appropriate message

#### 4. System Prompt Integration

**Prompt Generator** in `prompt_generator.py`:
- `_load_task_creation_prompt()` - Loads handover_prompt from config JSON
- Appends task creation instructions to conversation mode system prompt
- Provides agents with context about:
  - Available direct handover targets (configured agents)
  - Inbox task creation capability (always available)

#### 5. Task Creation Flow

**Business Logic**: `AgentService.create_agent_task()` in `backend/app/services/agent_service.py`
- Async method that handles both direct handover and inbox task creation
- Delegates task creation and execution to `InputTaskService`:

**Direct Handover Mode** (target_agent_id provided):

**Task Creation**: `InputTaskService.create_task_with_auto_refine()` in `backend/app/services/input_task_service.py`
- Creates InputTask with `agent_initiated=true`, `auto_execute=true`, `source_session_id`
- If target agent has `refiner_prompt`, automatically refines the handover message using `AIFunctionsService.refine_task()`
- Returns `(task, message_to_send)` tuple with possibly-refined message

**Task Execution**: `InputTaskService.execute_task()` in `backend/app/services/input_task_service.py`
- Creates session for target agent linked to task via `source_task_id`
- Links session back to task via `InputTaskService.link_session()`
- Sends the (possibly refined) message using `SessionService.send_session_message()`
- Returns `(success, session, error)` tuple

**System Message Logging** for direct handover:
- Logs system message in source session with metadata:
  - `task_created: true`
  - `task_id`: UUID of created task
  - `session_id`: UUID of created session
  - `target_agent_id`, `target_agent_name`
- Returns success status, task ID, and session ID

**Inbox Task Mode** (target_agent_id not provided):

**Task Creation**: `InputTaskService.create_task()` in `backend/app/services/input_task_service.py`
- Creates InputTask with `agent_initiated=true`, `auto_execute=false`, `source_session_id`
- Does NOT auto-refine (user will refine manually in task detail view)
- Does NOT execute (user will select agent and execute manually)

**System Message Logging** for inbox task:
- Logs system message in source session with metadata:
  - `task_created: true`
  - `task_id`: UUID of created task
  - `inbox_task: true`
- Returns success status and task ID (no session ID)

**Backend Endpoints** in `backend/app/api/routes/agents.py`:
- `POST /agents/tasks/create` - Primary endpoint for task creation
  - Async endpoint that delegates all logic to `AgentService.create_agent_task()`
  - Returns `{ success, task_id, session_id, message, error }`
- `POST /agents/handover/execute` - Deprecated alias, delegates to same logic

**Session Context**: Backend session ID passed via ChatRequest payload and tracked globally
- `session_id`: Claude SDK session ID (for SDK resumption)
- `backend_session_id`: Backend database session UUID (for handover tracking)
- Global state in `sdk_manager.py` maps SDK session IDs to backend session IDs
- Helper functions `get_current_sdk_session_id()` and `get_backend_session_id()` provide access to tools
- Async lock `_sdk_session_lock` serializes SDK sessions to prevent race conditions
- Note: Context variables don't propagate to tool execution contexts, requiring global state

#### 6. Technical Implementation Notes

**Async Context Challenge**:
Python's `contextvars.ContextVar` does not propagate to new async tasks created by the Claude SDK during tool execution. Tools run in separate async contexts without access to parent context variables.

**Solution Architecture**:
- Global variables (`_current_sdk_session_id`, `_backend_session_map`) store session state
- Async lock (`_sdk_session_lock`) in `send_message_stream()` serializes SDK sessions
- Lock prevents race conditions when multiple concurrent requests arrive
- Helper functions provide controlled access to global state
- Cleanup in `finally` block ensures state is cleared after each session

**Trade-offs**:
- Global state simplifies tool access but requires serialization
- Lock prevents concurrency within single agent environment (acceptable since SDK is stateful)
- Alternative approaches (thread-local storage, context propagation) proved incompatible with SDK architecture

### Integration Points

**Backend Services**:
- `backend/app/services/agent_service.py` - Handover orchestration and config sync
- `backend/app/services/input_task_service.py` - Task creation with auto-refine, task execution
- `backend/app/services/session_service.py` - Session creation
- `backend/app/services/message_service.py` - Message creation and backend session ID propagation
- `backend/app/services/ai_functions_service.py` - Task refinement AI function
- `backend/app/services/adapters/docker_adapter.py` - Environment communication

**Agent-Env Components**:
- `backend/app/env-templates/python-env-advanced/app/core/server/routes.py` - Config and chat endpoints, passes backend_session_id to SDK manager
- `backend/app/env-templates/python-env-advanced/app/core/server/models.py` - ChatRequest with backend_session_id field
- `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py` - Config storage
- `backend/app/env-templates/python-env-advanced/app/core/server/adapters/claude_code.py` - Tool registration, global session state with async lock, helper functions for session ID access
- `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py` - Prompt injection
- `backend/app/env-templates/python-env-advanced/app/core/server/tools/create_agent_task.py` - Task creation tool implementation, uses helper functions to retrieve session IDs

**Frontend Components**:
- `frontend/src/components/Agents/AgentHandovers.tsx` - Handover configuration management
- `frontend/src/components/Chat/MessageBubble.tsx` - System message rendering with session links

## Design Decisions

### Why Compact Prompts?

**Alternative**: Store detailed workflow configurations with structured fields (conditions, data schema, etc.)

**Chosen Solution**: Natural language prompts (2-3 sentences)

**Rationale**:
- Conversational agents use tool descriptions in LLM context
- Shorter descriptions reduce token usage and improve LLM comprehension
- Natural language allows flexibility for edge cases
- Users can express nuanced conditions without rigid schemas
- Easier to generate with AI assistance

### Why Store at Agent Level?

**Alternative**: Store handovers at session level

**Chosen Solution**: Agent-level configuration

**Rationale**:
- Handover logic is part of agent's **capabilities**, not session state
- Multiple sessions of same agent should have consistent handover behavior
- Easier to manage and version agent configurations
- Aligns with other agent-level configs (entrypoint, workflow prompts)

### Why Enable/Disable vs Delete?

**Design Pattern**: Soft disable before hard delete

**Rationale**:
- Prevents accidental loss of carefully crafted prompts
- Allows A/B testing of workflows
- Supports debugging (disable problematic handovers temporarily)
- Common pattern in production systems (feature flags, circuit breakers)

### Why AI Generation?

**Challenge**: Writing effective handover prompts requires understanding:
- Source agent's workflow output
- Target agent's workflow input
- Logical handoff points between them

**Solution**: AI analyzes both agents' prompts and generates draft

**Benefits**:
- Reduces cognitive load on users
- Suggests appropriate handover conditions
- Provides starting point for refinement
- Ensures consistent prompt structure

## File Reference Map

### Backend

**Models**:
- `backend/app/models/agent_handover.py` - Database models and request/response schemas
- `backend/app/models/__init__.py` - Model exports
- `backend/app/models/agent.py` - Agent relationship to handovers

**API Routes**:
- `backend/app/api/routes/agents.py` - Handover CRUD and execution endpoints

**Services**:
- `backend/app/services/agent_service.py` - `sync_agent_handover_config()` method
- `backend/app/services/session_service.py` - `create_session()` method
- `backend/app/services/adapters/base.py` - `set_agent_handover_config()` abstract method
- `backend/app/services/adapters/docker_adapter.py` - `set_agent_handover_config()` implementation
- `backend/app/services/environment_lifecycle.py` - `get_adapter()` method

**AI Functions**:
- `backend/app/agents/handover_generator.py` - Handover prompt generation agent
- `backend/app/agents/prompts/handover_generator_prompt.md` - Generation prompt template
- `backend/app/services/ai_functions_service.py` - `generate_handover_prompt()` service method

**Database**:
- `backend/app/alembic/versions/b26f2c36507c_add_agent_handover_config_table.py` - Migration

### Agent-Env

**Configuration**:
- `backend/app/env-templates/python-env-advanced/app/core/server/routes.py` - Config API endpoints
- `backend/app/env-templates/python-env-advanced/app/core/server/models.py` - Request/response models
- `backend/app/env-templates/python-env-advanced/app/core/server/agent_env_service.py` - Config file management

**Runtime**:
- `backend/app/env-templates/python-env-advanced/app/core/server/adapters/claude_code.py` - Tool registration
- `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py` - Prompt injection
- `backend/app/env-templates/python-env-advanced/app/core/server/tools/create_agent_task.py` - Task creation tool implementation

**Storage**:
- `{workspace}/docs/agent_handover_config.json` - Runtime handover configuration

### Frontend

**Components**:
- `frontend/src/components/Agents/AgentHandovers.tsx` - Main handover UI component
- `frontend/src/components/Agents/AgentConfigTab.tsx` - Integration point

**Generated Client**:
- `frontend/src/client/sdk.gen.ts` - AgentsService methods
- `frontend/src/client/types.gen.ts` - TypeScript types

## Testing Considerations

### Manual Testing Checklist

1. **Create Handover**
   - Can add handover to different agent
   - Cannot add handover to same agent (self-handover)
   - Cannot add duplicate handover to same target
   - Config syncs to agent-env automatically on creation

2. **Generate Prompt**
   - AI generates relevant prompt based on agent workflows
   - Generated prompt appears in textarea
   - Can edit generated prompt before saving

3. **Edit and Save**
   - Changes to prompt show "Apply Prompt" button
   - Save persists changes
   - Reload page shows saved prompt

4. **Enable/Disable**
   - Toggle switch changes enabled state
   - Disabled handovers are preserved
   - Re-enabling restores original prompt
   - Config syncs to agent-env automatically

5. **Delete**
   - Confirmation dialog appears
   - Delete removes configuration
   - Deleted handover disappears from list

6. **Access Control**
   - Cannot configure handover to another user's agent
   - Can only see/edit own agent handovers

7. **Runtime Execution**
   - Start conversation session with source agent
   - Trigger handover condition in conversation
   - Agent calls handover tool with context message
   - Task created with `agent_initiated=true` flag
   - If target agent has `refiner_prompt`, message is auto-refined
   - New session created for target agent linked to task
   - System message appears in source session with link to new session
   - Click link to navigate to new session
   - Verify (possibly refined) handover message appears in target agent's new session
   - Verify task appears in Tasks list with correct status

### Edge Cases

**Configuration**:
- **No other agents**: Shows message "No other agents available"
- **All agents configured**: "Add" button disappears when all possible handovers exist
- **Agent deletion**: Handover configs cascade delete with source agent
- **Concurrent edits**: Last write wins (no conflict resolution needed)

**Runtime**:
- **Target agent not configured**: Tool validates and returns error message
- **Target agent has no active environment**: Session creation fails, error returned
- **Target agent deleted**: Handover config auto-deleted on cascade
- **Disabled handover**: Not included in tool config, agent cannot call it
- **Invalid agent ID**: Tool rejects with validation error

## Future Enhancements

### Advanced Features
- **Conditional handovers**: Multiple conditions per target (if A then handover, if B don't)
- **Handover chains**: A → B → C workflows
- **Bidirectional handovers**: Target can hand back to source
- **Handover history**: Track which handovers were executed
- **Analytics**: Success rate, frequency, bottlenecks

### UI Improvements
- Visual workflow diagram showing handover relationships
- Handover testing ("dry run" without creating session)
- Template library of common handover patterns
- Bulk enable/disable for workflow testing

## Summary

Agent Task Creation & Handover Management provides **end-to-end agent-to-agent collaboration** and **agent-to-user task handoff**. Users configure handover targets through the UI with AI-assisted prompt generation. At runtime, conversational agents use the `create_agent_task` tool in two modes:

**Direct Handover Mode**:
- Agent specifies target agent → task auto-executes
- If target agent has `refiner_prompt`, message is auto-refined
- New session created for target agent
- User can view session via link in system message

**Inbox Task Mode**:
- Agent creates task without target → goes to user's inbox
- User reviews, selects agent, refines if needed
- User executes when ready
- User can view task via link in system message

**Key Features**:
- Configuration management with AI-assisted prompt generation
- Automatic sync to agent runtime environments
- Tool registration in conversation mode only (`mcp__task__create_agent_task`)
- **Task-based flow**: Creates InputTask with `agent_initiated=true`
- **Two modes**: Direct handover (auto-execute) and inbox task (user review)
- **Auto-refinement**: For direct handover, if target agent has `refiner_prompt`
- Session/task linking for tracking and navigation

**Architecture Principle**: Simple configuration layer with powerful runtime execution, keeping handover logic flexible through natural language prompts. Task-based flow enables visibility and tracking of both agent-to-agent handovers and agent-to-user task creation.
