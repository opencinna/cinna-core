# Agent Handover Management

## What is Agent Handover?

**Agent Handover** is a mechanism that allows one conversational agent to trigger another agent when specific conditions are met, passing relevant context from the first agent to the second. This enables **agent-to-agent collaboration** where specialized agents can work in sequence, each handling their domain expertise.

### Example Use Case

A "Cryptocurrency Rate Analytic" agent analyzes market data and identifies the top 3 cryptocurrencies with growth potential. Instead of executing trades itself, it hands over to a "Cryptocurrency Trader" agent with the analysis results. The trader agent then processes the recommendations according to its own specialized workflow.

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

**File**: `backend/app/api/routes/agents.py` (lines 502-698)

## User Interface

### Component Structure

**Primary Component**: `frontend/src/components/Agents/AgentHandovers.tsx`

Located in the **Agent Configuration Tab** (`AgentPromptsTab.tsx`), below the Workflow Prompt section.

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

## Integration with Agent Runtime

### Current Implementation (Configuration Only)

The current implementation provides **configuration management**:
- UI to define handover conditions
- Storage of handover prompts
- AI-assisted prompt generation

**What's NOT Implemented Yet**:
- Runtime handover execution
- Tool registration in agent sessions
- Automatic session creation for target agent
- Context passing between agents

### Future Runtime Integration

When handover logic is implemented, the flow will be:

1. **Agent Session Initialization**
   - Query handover configs for agent
   - Register each enabled config as a tool
   - Tool name: `handover_to_{target_agent_name}`
   - Tool description: `handover_prompt` content

2. **Handover Execution**
   - Agent calls handover tool with context message
   - System creates new session for target agent
   - Uses target agent's active environment
   - Sets session mode (conversation mode by default)
   - Sends handover message as first message to target session

3. **Context Passing**
   - Source agent formats message according to handover prompt
   - Message includes: results, data, files, recommendations
   - Target agent receives as entrypoint prompt override

**File References for Future Implementation**:
- `backend/app/services/message_service.py` - Session message handling
- `backend/app/services/agent_service.py` - Session creation logic
- Agent runtime tool registration (not yet implemented)

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
- `backend/app/models/agent_handover.py` - Database model and request/response schemas
- `backend/app/models/__init__.py` - Model exports (lines 95-103, 195-201)
- `backend/app/models/agent.py` - Agent relationship to handovers (lines 54-59)

**API Routes**:
- `backend/app/api/routes/agents.py` - Handover CRUD endpoints (lines 31-37, 502-698)

**AI Functions**:
- `backend/app/agents/handover_generator.py` - Handover prompt generation
- `backend/app/agents/prompts/handover_generator_prompt.md` - Generation prompt template
- `backend/app/agents/__init__.py` - Agent exports (line 6, 8)
- `backend/app/services/ai_functions_service.py` - Service layer (lines 13, 152-205)

**Database**:
- `backend/app/alembic/versions/b26f2c36507c_add_agent_handover_config_table.py` - Migration

### Frontend

**Components**:
- `frontend/src/components/Agents/AgentHandovers.tsx` - Main handover UI component
- `frontend/src/components/Agents/AgentPromptsTab.tsx` - Integration point (lines 29, 318)

**Generated Client**:
- `frontend/src/client/sdk.gen.ts` - AgentsService methods
- `frontend/src/client/types.gen.ts` - TypeScript types

## Testing Considerations

### Manual Testing Checklist

1. **Create Handover**
   - Can add handover to different agent
   - Cannot add handover to same agent (self-handover)
   - Cannot add duplicate handover to same target

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

5. **Delete**
   - Confirmation dialog appears
   - Delete removes configuration
   - Deleted handover disappears from list

6. **Access Control**
   - Cannot configure handover to another user's agent
   - Can only see/edit own agent handovers

### Edge Cases

- **No other agents**: Shows message "No other agents available"
- **All agents configured**: "Add" button disappears when all possible handovers exist
- **Agent deletion**: Handover configs cascade delete with source agent
- **Concurrent edits**: Last write wins (no conflict resolution needed)

## Future Enhancements

### Runtime Execution
- Implement tool registration in agent sessions
- Automatic target session creation
- Context message passing
- Error handling for failed handovers

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

Agent Handover Management provides the **configuration layer** for agent-to-agent collaboration. Users can define when and how agents should trigger each other, with AI assistance to draft effective handover prompts. The feature is designed for future runtime integration where agents will automatically create sessions and pass context to downstream agents, enabling complex multi-agent workflows.

**Key Principle**: Keep configuration simple and flexible, deferring complex execution logic to future runtime implementation.
