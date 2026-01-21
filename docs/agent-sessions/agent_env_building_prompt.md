# Agent Environment System Prompts

## Overview

Agent environments support two distinct modes, each with tailored system prompts and configurations:

1. **Building Mode**: Development-focused mode for creating workflows, scripts, and configurations
2. **Conversation Mode**: Execution-focused mode for running pre-built workflows and tasks

This document describes the prompt architecture, construction, and usage patterns for both modes.

## Prompt Design Principles

These principles guide how we construct prompts for the building agent, based on real-world workflow development experience:

### 1. Systematic Building Process

The building agent should follow a clear, step-by-step process:
1. **Analyze Requirements** - Understand the goal
2. **Check Credentials** - Verify what's available, ask for missing ones
3. **Plan Script Architecture** - Design single-purpose, composable scripts
4. **Generate Scripts** - Create focused, simple scripts
5. **Update Scripts README** - IMMEDIATELY document each script
6. **Update Workflow Prompt** - Document orchestration and decision logic
7. **Define Entrypoint** - Create human-like trigger message
8. **Define Refiner Prompt** - Document task refinement guidelines (default values, mandatory fields)

### 2. Single-Purpose Scripts Principle

**Critical**: Each script should handle ONLY ONE operation.

- ❌ **Wrong**: One script that fetches time-off data AND books vacation
- ✅ **Correct**: `get_timeoff_details.py` (fetch) + `book_vacation.py` (book)

**Why**:
- Scripts become reusable building blocks
- Conversation agent can orchestrate them step-by-step
- Easier to debug and maintain
- Agent can track workflow progress by executing standalone pieces

**Data Passing Between Scripts**:

For effective script composition, use appropriate data passing methods:

**Small Data** (simple values, IDs, counts):
- Use command-line arguments: `python script2.py --user-id=12345 --count=42`
- Print to stdout and capture in conversation mode

**Large Data** (lists, records, parsed results):
- **Use CSV/JSON files in `workspace/files/` folder**
- Script 1 (producer) outputs: `./files/parsed_data.csv`
- Script 2 (consumer) reads: `./files/parsed_data.csv`

**Example File-Based Workflow**:
```
1. parse_invoices.py → Saves results to ./files/invoices.csv
2. process_invoices.py --input=./files/invoices.csv → Processes CSV
3. generate_report.py --data=./files/invoices_processed.json → Final report
```

**Benefits of File-Based Data Passing**:
- ✅ Handles large datasets efficiently
- ✅ Agent can inspect intermediate results between steps
- ✅ Scripts can run independently for debugging
- ✅ Clear data flow and state tracking
- ✅ Supports restart from any point in workflow
- ✅ Human-readable intermediate data (CSV/JSON)

### 3. Human-Like Entrypoint Prompts

**Critical**: ENTRYPOINT_PROMPT.md must be conversational, not technical.

- ❌ **Wrong**: "You are an Odoo assistant. Use this API: {jsonrpc...}"
- ✅ **Correct**: "Query from Odoo my time-offs and show me the summary"

**Guidelines**:
- Write how users naturally communicate
- Avoid API details, JSON structures, technical jargon
- Focus on WHAT the user wants, not HOW it's implemented
- Keep it simple - 1-2 sentences maximum
- No markdown formatting, no headers, just plain text

### 4. Mandatory Documentation Updates

**Critical**: Scripts README must be updated IMMEDIATELY after each script creation/modification.

**Why this matters**:
- The README is auto-loaded into prompts for future sessions
- If not updated, the agent (and users) won't know what scripts exist
- Prevents duplicate script creation
- Enables proper workflow orchestration

**Enforcement in prompt**:
- Use strong language: "CRITICAL", "IMMEDIATELY", "MUST"
- Explain consequences of not updating
- Provide clear DO/DON'T lists
- Remind that this enables future session awareness

### 5. Script Orchestration Documentation

**Critical**: WORKFLOW_PROMPT.md must explain how scripts work together.

**Include**:
- Execution sequences (script A → script B → script C)
- How to use output from one script as input to another
- Decision points (when to use which script)
- Error handling between steps

**Example**:
```
First run `get_timeoff_details.py` to fetch available days.
Then use its output to call `book_vacation.py --days=5 --type=annual`.
If booking fails, run `check_conflicts.py` to diagnose issues.
```

### 6. Mandatory User Follow-up with AskUserQuestion

**CRITICAL**: Agents must use `AskUserQuestion` tool when they cannot proceed without user input.

**Building Mode - Ask when:**
- Requirements are vague/ambiguous
- Required credentials are missing
- Design decisions need user preference
- Critical assumptions would be needed

**Conversation Mode - Ask when:**
- Required parameters are missing from request
- Request has multiple valid interpretations
- Workflow cannot proceed without user choice

**Never guess or assume** - always ask when information is mandatory.

### 7. Credentials Security Awareness

**Always emphasize**:
- NEVER read `credentials.json` during building mode conversations
- Only access credentials programmatically within scripts
- Review `credentials/README.md` to see available credentials
- Ask users to share missing credentials before proceeding

### 8. Conversation Agent as Bridge Between Scripts and Humans

**Critical**: The conversation agent doesn't just run scripts and exit—it's a bridge.

**The agent's role**:
1. **Execute scripts** to fetch/process data
2. **Parse outputs** (JSON, CSV, text)
3. **Rephrase results** into natural, human-friendly language
4. **Communicate** with the user conversationally

**Implications for Documentation**:

**ENTRYPOINT_PROMPT.md**:
- Simple user question: "What is my time-off balance?"
- NOT execution details: "Query Odoo API and return JSON"

**WORKFLOW_PROMPT.md**:
- Document script execution: "Run `get_timeoff_balance.py` → outputs JSON"
- Document data presentation: "Parse JSON and rephrase: 'You have 15 days available...'"
- Include example outputs: `{"annual_leave": 15, "sick_leave": 10}`
- Explain how to handle results in user-friendly way

**Three-Part Structure**:
1. **Building Request** (User to building agent): "I want an agent that checks my time-off balance"
2. **Entrypoint** (User trigger): "What is my time-off balance?"
3. **Workflow** (Execution + Presentation): Run script → Parse JSON → Rephrase → Present

**Example**:

❌ **Wrong**:
- Entrypoint: "Query from Odoo my time-offs and show me the summary" (too technical)
- Workflow: "Run get_timeoff_balance.py" (missing presentation instructions)

✅ **Correct**:
- Entrypoint: "What is my time-off balance?" (simple, natural)
- Workflow: "Run script → Parse `{"annual_leave": 15}` → Rephrase as 'You have 15 days of annual leave available'" (complete instructions)

## Session Modes

### Building Mode

**Purpose**: Develop and configure workflow capabilities

**Model**: Claude Sonnet (default) - Superior code generation and reasoning

**System Prompt**: Claude Code preset + comprehensive documentation

**Use Cases**:
- Create Python scripts for automation
- Configure integrations and credentials
- Update workflow documentation
- Define entry points for scheduled execution

### Conversation Mode

**Purpose**: Execute pre-built workflows and interact with users

**Model**: Claude Haiku - Faster and more cost-effective

**System Prompt**: Workflow-specific instructions (no Claude Code preset)

**Use Cases**:
- Execute tasks using existing scripts
- Process user requests based on workflow capabilities
- Generate reports and summaries
- Interact with APIs and data sources

## Building Mode Prompt Architecture

The building mode system prompt is composed of multiple layers:

1. **Claude Code Preset** (`preset: "claude_code"`)
   - Official Claude Code system prompt with all standard tools and capabilities
   - Provides base functionality for file operations, code editing, and bash commands

2. **BUILDING_AGENT.md** (Static Template)
   - Custom instructions for the building agent role
   - Defines workspace structure (`./scripts/`, `./files/`)
   - Specifies development guidelines (use `uv` for packages, maintain scripts catalog)
   - Located at: `backend/app/env-templates/python-env-advanced/app/BUILDING_AGENT_EXAMPLE.md`

3. **scripts/README.md** (Dynamic Context)
   - Catalog of existing scripts in the workspace
   - Loaded at runtime if file exists and is not empty
   - Provides agent with awareness of what scripts already exist
   - **CRITICAL**: Building agent MUST update this IMMEDIATELY after creating/modifying any script
   - If not updated, future sessions won't know what scripts exist, leading to duplicates and confusion

4. **docs/WORKFLOW_PROMPT.md** (Dynamic Context)
   - System prompt for conversation mode agent
   - Loaded at runtime and included in building mode prompt
   - Agent updates this as workflow capabilities evolve

5. **docs/ENTRYPOINT_PROMPT.md** (Dynamic Context)
   - Trigger message for workflow execution
   - Loaded at runtime and included in building mode prompt
   - Agent defines concise command to start the workflow

6. **docs/REFINER_PROMPT.md** (Dynamic Context)
   - Instructions for refining incoming task descriptions
   - Loaded at runtime and included in building mode prompt
   - Agent defines default values, mandatory fields, and enhancement guidelines

7. **credentials/README.md** (Dynamic Context)
   - Documentation of credentials shared with the agent
   - Redacted sensitive data (shows structure, hides values)
   - Loaded at runtime if file exists and is not empty
   - Provides agent with awareness of available credentials for script development

8. **knowledge/** (Integration Knowledge Base)
   - Directory containing integration-specific documentation organized by topic
   - Only topic folder names are included in the prompt (minimal footprint)
   - Agent reads specific files on-demand when building integrations
   - Example topics: `odoo-erp`, `salesforce`, `stripe`
   - Files contain API guides, data schemas, and best practices

## Workflow Documentation Prompts

The building agent maintains three key documentation files that define how the completed workflow will be used:

### 1. WORKFLOW_PROMPT.md (System Prompt for Conversation Mode)

**Purpose**: Comprehensive system prompt for the conversation mode agent that will execute the workflow.

**Location**: `{instance_dir}/app/docs/WORKFLOW_PROMPT.md`

**Template**: `backend/app/env-templates/python-env-advanced/app/docs/WORKFLOW_PROMPT.md`

**Content**:
- Agent's role and responsibilities
- **Workflow execution steps**: How to run scripts and handle outputs
- **Data presentation**: How to rephrase script results for users in natural language
- Available scripts and what they output (JSON, CSV, etc.)
- Decision-making guidelines and error handling
- Example outputs from scripts (JSON structure, CSV columns)

**Critical Understanding**:
The conversation agent's job is to:
1. **Execute scripts** to fetch/process data
2. **Parse script outputs** (JSON, CSV, etc.)
3. **Rephrase results** into human-friendly responses
4. **Communicate with user** in natural language

The agent is a **bridge between scripts and humans**, not just a script runner.

**Example**: Odoo Time-Off Balance workflow
```markdown
## Workflow Steps
1. Run: `python scripts/get_timeoff_balance.py`
   - Outputs JSON: `{"annual_leave": 15, "sick_leave": 10, "unpaid": 5}`
2. Parse the JSON and rephrase for user:
   - "You have 15 days of annual leave, 10 days of sick leave, and 5 days of unpaid leave available."
```

**Loading**: `sdk_manager.py::_load_workflow_prompt()` - included in building mode system prompt

**Sync**: `MessageService.sync_agent_prompts_from_environment()` - synced to backend Agent model after building sessions

### 2. ENTRYPOINT_PROMPT.md (Trigger Message)

**Purpose**: Concise, human-like user message (1-2 sentences) that triggers workflow execution.

**Location**: `{instance_dir}/app/docs/ENTRYPOINT_PROMPT.md`

**Template**: `backend/app/env-templates/python-env-advanced/app/docs/ENTRYPOINT_PROMPT.md`

**Content Requirements**:
- **Human-like, conversational language** - as if user is starting a conversation on a daily basis
- **NO technical details** - no API references, JSON structures, or system prompt language
- **NO markdown formatting** - plain text only, no headers or explanations
- **Short and actionable** - 1-2 sentences maximum
- **Focus on WHAT, not HOW** - user intent, not implementation details
- **Used for scheduled/automated execution** - sent as first user message

**Good Examples** (Natural, conversational):

*Odoo Time-Off Balance*:
- User's building request: "I want an agent that provides info about my time-off balances in Odoo ERP"
- **Entrypoint**: "What is my time-off balance?"
- Workflow: Run script → Get JSON → Rephrase for user

*Invoice Parser*:
- User's building request: "Build an agent that checks my email for invoices"
- **Entrypoint**: "Check my email for new invoices"
- Workflow: Fetch emails → Detect invoices → Summarize findings

*Sales Report*:
- User's building request: "Create an agent that generates daily sales reports"
- **Entrypoint**: "Generate yesterday's sales report"
- Workflow: Query database → Process data → Format report

**Bad Examples** (Too technical, system-prompt-like):
- ❌ "You are an Odoo assistant. Use this API: {jsonrpc...}" (This is system prompt language, not a user message)
- ❌ "Query from Odoo my time-offs and show me the summary" (Too detailed/technical for a simple balance check)
- ❌ Headers like "# Entrypoint Prompt" or explanatory text
- ❌ Technical implementation details or system instructions

**Remember**: Entrypoint is what the USER asks, not how the agent executes. Keep it simple and natural.

**Loading**: `sdk_manager.py::_load_entrypoint_prompt()` - included in building mode system prompt

**Sync**: `MessageService.sync_agent_prompts_from_environment()` - synced to backend Agent model after building sessions

**Critical for Building Agent**: The building agent must understand that this is NOT a system prompt, but rather how a real user would naturally ask for the workflow to run.

### 3. REFINER_PROMPT.md (Task Refinement Instructions)

**Purpose**: Instructions for refining incoming task descriptions before execution, used by the task queue system.

**Location**: `{instance_dir}/app/docs/REFINER_PROMPT.md`

**Template**: `backend/app/env-templates/python-env-advanced/app/workspace/docs/REFINER_PROMPT.md`

**Content Requirements**:
- **Default values** for common parameters (date ranges, output formats, priorities)
- **Mandatory fields** that must be clarified by users before execution
- **Enhancement guidelines** for transforming vague requests into detailed instructions
- **Examples** of good vs. bad task descriptions

**Example**:
```markdown
## Default Values
- Date range: Last 7 days (unless specified)
- Output format: Summary table with key metrics
- Priority: Normal (unless urgent mentioned)

## Mandatory Clarifications
- Target system/data source must be specified
- Required output format (report, email, notification)
- If a specific date is needed, it must be provided

## Enhancement Guidelines
- Add specific metric names when user mentions "performance"
- Include comparison period when user asks for "trends"
- Default to including visualizations for data-heavy reports

## Examples
Good: "Generate a sales report for last quarter with regional breakdown"
Bad: "Generate a report" (missing: report type, time period, breakdown)
```

**When to Create/Update**:
- When finalizing the workflow's expected inputs
- When identifying common parameters with sensible defaults
- When discovering users often forget to specify certain details
- When the workflow has specific requirements that should be enforced

**Loading**: `prompt_generator.py::_load_refiner_prompt()` - included in building mode system prompt

**Sync**: `MessageService.sync_agent_prompts_from_environment()` - synced to backend Agent model after building sessions

**Usage**: The task refiner (`backend/app/agents/task_refiner.py`) uses this prompt when refining task descriptions in the task queue, applying defaults and requesting clarifications as specified.

### Bidirectional Sync

**From Environment → Backend** (automatic):
- Triggered after building mode sessions complete
- `messages.py` route calls `MessageService.sync_agent_prompts_from_environment()`
- Reads prompts via `docker_adapter.py::get_agent_prompts()` (calls `/config/agent-prompts`)
- Updates `Agent.workflow_prompt`, `Agent.entrypoint_prompt`, and `Agent.refiner_prompt` fields

**From Backend → Environment** (manual):
- User edits prompts in backend UI
- Calls `agents.py::sync_agent_prompts()` route
- Pushes via `docker_adapter.py::set_agent_prompts()` (calls `/config/agent-prompts`)
- Updates files in `{instance_dir}/app/docs/` (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, REFINER_PROMPT.md)

### Building Agent Instructions

The building agent sees all three prompts in its system prompt and is instructed to:
- Update `WORKFLOW_PROMPT.md` as scripts and capabilities are developed
- Fill in `ENTRYPOINT_PROMPT.md` with a concise trigger message
- Define `REFINER_PROMPT.md` with task refinement guidelines
- Keep all three files current as the workflow evolves

**Reference**: `BUILDING_AGENT_EXAMPLE.md` section "Workflow Documentation"

## File Locations

### Template Files (Version Controlled)

- **BUILDING_AGENT_EXAMPLE.md**: `backend/app/env-templates/python-env-advanced/app/BUILDING_AGENT_EXAMPLE.md`
  - Template for building agent instructions
  - Copied to `BUILDING_AGENT.md` during environment initialization

- **scripts/README.md Template**: `backend/app/env-templates/python-env-advanced/app/scripts/README.md`
  - Initial empty catalog template
  - Agent maintains this file as scripts are created

### Instance Files (Per Environment)

- **BUILDING_AGENT.md**: `{instance_dir}/app/BUILDING_AGENT.md`
  - Created from BUILDING_AGENT_EXAMPLE.md during environment setup
  - Can be customized per environment

- **scripts/README.md**: `{instance_dir}/app/scripts/README.md`
  - Dynamically maintained catalog of scripts
  - Updated by agent whenever scripts are created/modified/removed

## Implementation Flow

### 1. Environment Creation

**File**: `backend/app/services/environment_lifecycle.py`

**Method**: `create_environment_instance()`

**Step 3**: `_setup_building_agent_prompt()`
- Copies `BUILDING_AGENT_EXAMPLE.md` → `BUILDING_AGENT.md` to instance directory
- Ensures each environment has its own copy that can be customized

### 2. Prompt Generator Initialization

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/prompt_generator.py`

**Class**: `PromptGenerator`

**Initialization**:
- Accepts `workspace_dir` path in constructor
- Calls `_load_building_agent_prompt()` to read `/app/BUILDING_AGENT.md`
- Stores content in `self.building_agent_prompt` (cached for reuse)

**Purpose**: Centralized prompt loading and generation logic

### 3. SDK Manager Initialization

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`

**Class**: `ClaudeCodeSDKManager`

**Initialization**:
- Creates `PromptGenerator` instance with workspace path
- Creates `SessionLogger` instance for debugging
- No direct prompt loading (delegated to PromptGenerator)

**Purpose**: Orchestrate SDK sessions, coordinate with services

### 4. Runtime Prompt Assembly

**Building Mode** (`PromptGenerator.generate_building_mode_prompt()`):
1. Start with cached `self.building_agent_prompt`
2. Call `_load_scripts_readme()` - append scripts catalog if exists
3. Call `_load_workflow_prompt()` - append workflow docs if exists
4. Call `_load_entrypoint_prompt()` - append entry point if exists
5. Return `SystemPromptPreset` dict:
   ```python
   {
       "type": "preset",
       "preset": "claude_code",
       "append": building_prompt  # All docs combined
   }
   ```

**Conversation Mode** (`PromptGenerator.generate_conversation_mode_prompt()`):
1. Call `_load_workflow_prompt()` - main system prompt
2. Call `_load_scripts_readme()` - available scripts
3. Combine into plain string (no preset)
4. Return string prompt

### 5. Request Routing

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/routes.py`

**Endpoints**: `/chat` and `/chat/stream`

**Logic**:
- Validate `agent_sdk` parameter (currently only "claude" supported)
- Extract `mode` and `agent_sdk` from request
- Call `sdk_manager.send_message_stream()` with mode and SDK parameters
- SDK Manager delegates prompt generation to PromptGenerator
- Model selection happens in SDK Manager based on mode

### 6. Model and Prompt Configuration

**File**: `backend/app/env-templates/python-env-advanced/app/core/server/sdk_manager.py`

**Method**: `send_message_stream()`

**Steps**:
1. Set model based on mode:
   - Conversation: `options.model = "haiku"`
   - Building: Don't set (defaults to Sonnet)
2. Generate system prompt:
   - Call `self.prompt_generator.generate_prompt(mode)`
   - Assign to `options.system_prompt`
3. Create SDK client with configured options
4. Stream responses to frontend

## Key Features

### Dynamic Context Awareness

The agent knows about existing scripts because `scripts/README.md` is loaded fresh on each request. This means:
- Agent sees what scripts were created in previous sessions
- Can update/modify existing scripts intelligently
- Avoids recreating scripts that already exist
- Can maintain the catalog accurately

### Catalog Maintenance Requirement

The prompt explicitly requires the agent to:
- Update `./scripts/README.md` whenever creating/modifying/removing scripts
- Use a specific format (Purpose, Usage, Key arguments, Output)
- Keep descriptions SHORT and ACTIONABLE

### Workspace Organization

The prompt enforces strict organization:
- **All scripts** → `./scripts/`
- **Machine-format files** (JSON, CSV, binaries) → `./files/`
- **Human documentation** (Markdown, reports) → `./docs/`
- **All packages** → installed via `uv`

This separation ensures:
- Clear distinction between code, data, and documentation
- Users know where to find readable reports (docs/) vs. raw data (files/)
- Scripts know where to look for input/output data (files/)
- Consistent organization across all workflows

## Example Assembled Prompt

When building mode is activated with existing scripts and credentials:

```
[Claude Code Preset System Prompt]

[Content from BUILDING_AGENT.md]
- Role definition
- Workspace structure
- Development guidelines
- Script catalog format
- Common tasks

---

## Existing Scripts in Workspace

The following is the current contents of `./scripts/README.md` which catalogs all existing scripts in this workspace:

```markdown
# Scripts Catalog

## process_data.py
**Purpose**: Process CSV data and generate summary statistics
**Usage**: `python scripts/process_data.py --input data.csv --output summary.json`
**Key arguments**: `--input` (required), `--output` (required)
**Output**: JSON file saved to ./files/
```

**Important**: When you create, modify, or remove scripts, you MUST update this file to keep it accurate.

---

## Available Credentials

The following is the current contents of `./credentials/README.md`:

```markdown
# Available Credentials

## email_imap (my_gmail_account)
- **host**: imap.gmail.com
- **port**: 993
- **login**: user@example.com
- **password**: [REDACTED]
- **is_ssl**: true
```

**CRITICAL SECURITY RULES**:
- **NEVER** read `./credentials/credentials.json` directly in this conversation
- **NEVER** log or print credential values in your messages
- **ONLY** access credentials programmatically in the scripts you create
```

---

## Integration Knowledge Base

If you need specific integration knowledge (APIs, data schemas, best practices),
check `./knowledge/` directory which contains following topics (folders): odoo-erp

Check these folders for documentation files if needed.
```

## Conversation Mode Prompt Architecture

The conversation mode system prompt is lightweight and execution-focused:

### Prompt Components

1. **WORKFLOW_PROMPT.md** (Main System Prompt)
   - Describes workflow's purpose, capabilities, and responsibilities
   - Execution flow and decision-making guidelines
   - Database schemas, file formats, and data structures
   - Error handling patterns and success criteria
   - **File organization guidelines** (see below)

2. **scripts/README.md** (Available Tools)
   - Catalog of existing scripts and their usage
   - Appended to system prompt for script awareness
   - Formatted as "Available Scripts" section

3. **credentials/README.md** (Available Credentials)
   - Documentation of credentials shared with the agent
   - Redacted sensitive data (shows structure, hides values)
   - Appended to system prompt for credential awareness
   - Formatted as "Available Credentials" section

4. **knowledge/** (Integration Knowledge Base)
   - Topic folder names listed in prompt for awareness
   - Agent reads specific knowledge files on-demand during execution
   - Provides access to integration documentation when needed

### File Organization for Conversation Mode

**CRITICAL**: Conversation agents must save files to the appropriate workspace folders:

**Machine-Format Files** → `./files/`
- JSON data files (API responses, processed data, configurations)
- CSV files (reports, datasets, exports)
- Binary files (images, PDFs, archives)
- Temporary data files (intermediate processing results)
- **Purpose**: Data that scripts and agents read/write programmatically

**Human Documentation** → `./docs/`
- Markdown files (reports, summaries, documentation)
- Text-based reports (analysis results, findings)
- Human-readable summaries (workflow results, logs)
- **Purpose**: Documentation meant for users to read

**Examples**:
- ✅ **Correct**: Save API response → `./files/api_response.json`
- ✅ **Correct**: Save user report → `./docs/daily_summary.md`
- ✅ **Correct**: Save CSV export → `./files/sales_data.csv`
- ✅ **Correct**: Save analysis → `./docs/performance_analysis.md`
- ❌ **Wrong**: Save user report → `./files/report.md` (should be in docs/)
- ❌ **Wrong**: Save JSON data → `./docs/data.json` (should be in files/)

**Why This Matters**:
- Clear separation between machine data and human documentation
- Users know where to find readable reports (docs/) vs. raw data (files/)
- Scripts know where to look for input/output data (files/)
- Consistent organization across all workflows

### Differences from Building Mode

**What's EXCLUDED**:
- ❌ Claude Code preset (no development overhead)
- ❌ BUILDING_AGENT.md (no development instructions)
- ❌ ENTRYPOINT_PROMPT.md (not needed for execution)

**What's INCLUDED**:
- ✅ WORKFLOW_PROMPT.md (workflow-specific instructions)
- ✅ scripts/README.md (available automation tools)
- ✅ credentials/README.md (available credentials with redacted sensitive data)
- ✅ knowledge/ (topic folder names for integration documentation)

**Prompt Format**:
- **Building Mode**: SystemPromptPreset dict (Claude Code + appended docs)
- **Conversation Mode**: Plain string (direct system prompt text)

**Model Selection**:
- **Building Mode**: Default (Sonnet) for code quality
- **Conversation Mode**: Haiku for speed and cost efficiency

### Example Conversation Prompt

```
# Mailbox Invoice Parser Workflow

## Role and Responsibilities
You are an automated invoice extraction and reporting agent...

## Available Scripts
- `scripts/fetch_emails.py`: Connect to email account and retrieve unread emails
- `scripts/detect_invoices.py`: Identify emails containing invoices
- `scripts/extract_invoice_data.py`: Parse invoice documents
- `scripts/generate_summary.py`: Create summary reports

## Available Credentials
- **email_imap** (my_email_account): IMAP access to user@example.com
  - host: imap.gmail.com, port: 993, login: user@example.com, password: [REDACTED]
- **odoo** (erp_system): Odoo ERP API access
  - url: https://erp.example.com, database: production, api_token: [REDACTED]

**IMPORTANT**:
- The information above shows all available credentials
- **DO NOT** read ./credentials/credentials.json directly - use the information above when discussing credentials with users
- Scripts you execute can read ./credentials/credentials.json to access the actual credential data
- Sensitive values (passwords, tokens) are shown as [REDACTED] above but are available to scripts

## Integration Knowledge Base

If you need specific integration knowledge (APIs, data schemas, best practices),
check `./knowledge/` directory which contains following topics (folders): odoo-erp

Check these folders for documentation files if needed.
```

## Implementation

### Module Responsibilities

**PromptGenerator** (`prompt_generator.py`):
- Loads prompt files from workspace and templates
- Constructs mode-specific system prompts
- Caches static prompts for performance

**Key Methods**:
- `generate_building_mode_prompt()`: Returns SystemPromptPreset dict
- `generate_conversation_mode_prompt()`: Returns plain string
- `generate_prompt(mode)`: Factory method routing by mode
- `_get_knowledge_topics()`: Returns comma-separated list of knowledge topic folders

**SDK Manager** (`sdk_manager.py`):
- Coordinates with PromptGenerator for system prompts
- Sets model based on mode (Haiku vs Sonnet)
- Creates SDK client with appropriate configuration

**Agent Environment Service** (`agent_env_service.py`):
- Reads/writes prompt files in workspace
- Provides business logic for prompt management
- Used by HTTP endpoints for prompt sync

### Prompt Loading Flow

**Building Mode**:
1. SDK Manager receives request with `mode="building"`
2. Calls `PromptGenerator.generate_building_mode_prompt()`
3. PromptGenerator loads:
   - `/app/BUILDING_AGENT.md` (cached at initialization)
   - `/app/workspace/scripts/README.md` (fresh load)
   - `/app/workspace/docs/WORKFLOW_PROMPT.md` (fresh load)
   - `/app/workspace/docs/ENTRYPOINT_PROMPT.md` (fresh load)
   - `/app/workspace/docs/REFINER_PROMPT.md` (fresh load)
   - `/app/workspace/credentials/README.md` (fresh load)
   - `/app/workspace/knowledge/` (scans for topic folder names only)
4. Constructs SystemPromptPreset dict with Claude Code preset
5. SDK Manager passes to ClaudeAgentOptions

**Conversation Mode**:
1. SDK Manager receives request with `mode="conversation"`
2. Calls `PromptGenerator.generate_conversation_mode_prompt()`
3. PromptGenerator loads:
   - `/app/workspace/docs/WORKFLOW_PROMPT.md` (fresh load)
   - `/app/workspace/scripts/README.md` (fresh load)
   - `/app/workspace/credentials/README.md` (fresh load)
   - `/app/workspace/knowledge/` (scans for topic folder names only)
4. Constructs plain string prompt
5. SDK Manager passes to ClaudeAgentOptions

### Model Selection

**Implementation** (`sdk_manager.py`):
```python
# Set model based on mode
if mode == "conversation":
    options.model = "haiku"
    logger.info("Using Haiku model for conversation mode")
# For building mode, don't set model parameter (uses default Sonnet)
```

**Rationale**:
- Conversation mode prioritizes speed and cost (Haiku)
- Building mode prioritizes code quality (Sonnet)
- Model selection automatic based on session mode

## SDK Support

### Agent SDK Parameter

**Purpose**: Support multiple AI SDKs in the future

**Current**: `agent_sdk="claude"` (only option)

**Future**: Can add `"openai"`, `"google-adk"`, etc.

**Request Flow**:
1. Backend creates session with `agent_sdk` parameter
2. Request includes `agent_sdk` in payload to environment
3. Routes.py validates SDK support
4. SDK Manager handles SDK-specific configuration

**Benefits**:
- Extensible architecture for multi-SDK support
- SDK preference stored at session level
- Easy to add new AI providers

## Integration Knowledge Base

### Purpose

The knowledge base provides agents with access to integration-specific documentation, API guides, data schemas, and best practices. This enables agents to build high-quality integrations without requiring the full documentation in the system prompt.

### Structure

**Location**: `{instance_dir}/app/workspace/knowledge/`

**Organization**:
- Topic-based subdirectories (e.g., `odoo-erp/`, `salesforce/`, `stripe/`)
- Markdown files containing integration documentation
- Each topic folder can contain multiple documentation files

**Example Structure**:
```
workspace/knowledge/
├── odoo-erp/
│   ├── general_info.md      # Connection methods, authentication, architecture
│   ├── vendor_bills.md       # Vendor bill fields, workflows
│   └── sales_orders.md       # Sales order operations
├── salesforce/
│   ├── api_guide.md          # API endpoints, authentication
│   └── data_model.md         # Object schemas, relationships
└── stripe/
    └── webhooks.md           # Webhook handling, event types
```

### Minimal Footprint Design

**Key Principle**: Only topic folder names are included in the prompt, not file contents.

**Prompt Addition**:
```
## Integration Knowledge Base

If you need specific integration knowledge (APIs, data schemas, best practices),
check `./knowledge/` directory which contains following topics (folders): odoo-erp, salesforce, stripe

Check these folders for documentation files if needed.
```

**Benefits**:
- Minimal prompt size (3 lines + comma-separated topic names)
- Agent-driven discovery (reads files only when needed)
- Scalable (can add many topics without bloating prompt)
- Fast context loading (no upfront file reading)

### Usage Pattern

**Building Mode**:
1. Agent sees available topics in system prompt
2. When building integration, agent reads relevant knowledge files
3. Agent follows patterns and best practices from documentation
4. Agent implements integration according to guidelines

**Conversation Mode**:
1. Agent aware of knowledge topics
2. Can reference documentation during workflow execution
3. Useful for troubleshooting or extending workflows

### Implementation

**Method**: `PromptGenerator._get_knowledge_topics()`
- Scans `workspace/knowledge/` for subdirectories
- Returns comma-separated list of folder names
- Ignores hidden folders (starting with `.`)
- Lightweight operation (no file reading)

**Loading**: Called during both building and conversation mode prompt generation

### Content Guidelines

Knowledge base files should contain:
- **API Documentation**: Endpoints, authentication, request/response formats
- **Data Schemas**: Field definitions, validation rules, relationships
- **Best Practices**: Architectural patterns, error handling, performance tips
- **Code Examples**: Common operations, edge cases, integration patterns
- **Domain Knowledge**: Business logic, workflows, terminology

### Example Content

**File**: `knowledge/odoo-erp/general_info.md`
- Connection methods (XML-RPC)
- Authentication patterns
- Separation of concerns (OdooClient wrapper)
- Batch processing best practices
- Multi-company awareness
- Error handling patterns

## Benefits

1. **Mode Separation**: Clear distinction between development and execution
2. **Performance**: Haiku provides 2-5x faster responses in conversation mode
3. **Cost Efficiency**: Haiku reduces costs by ~90% for execution tasks
4. **Context Preservation**: Building mode agent knows about existing work
5. **Consistency**: All script development follows the same patterns
6. **Self-Documenting**: Scripts catalog provides built-in documentation
7. **Reusability**: Scripts designed for workflow automation from the start
8. **Maintainability**: Clear structure and documentation requirements
9. **Knowledge Discovery**: Minimal-footprint integration knowledge base enables high-quality integrations without bloating prompts

## Lessons Learned from Real Workflows

### Odoo Integration Experience

When building an Odoo time-off tracking workflow, we discovered several critical improvements needed:

#### Problem 1: Technical Entrypoint Prompts

**What happened**: Building agent created ENTRYPOINT_PROMPT.md with technical details:
```
You are an Odoo time-off assistant. Your primary function is to inform users
about their available leave days. You will use the provided Odoo API query:
{"jsonrpc": "2.0", "method": "call", "params": {...}}
```

**Issue**: This is system prompt language, not a user message. Too technical, includes API details.

**Solution**: Rewrote as human-like trigger message:
```
Query from Odoo my time-offs and show me the summary
```

**Learning**: Emphasize in building agent prompt that entrypoint should be conversational, like "how user would start communication on a daily basis."

#### Problem 2: Scripts README Not Updated

**What happened**: Building agent created multiple scripts but didn't update `scripts/README.md`.

**Issue**:
- Conversation mode agent didn't know what scripts existed
- User couldn't see available capabilities
- Future building sessions couldn't see existing scripts

**Solution**:
- Added **CRITICAL** warnings in building agent prompt
- Emphasized **IMMEDIATE** updates, not "later"
- Explained consequences: "If you don't update it, you (and the conversation mode agent) won't know what scripts exist!"

**Learning**: Use strong, direct language about mandatory documentation. Explain the "why" not just the "what."

#### Problem 3: Finding Entrypoint Prompt Location

**What happened**: Agent didn't know where ENTRYPOINT_PROMPT.md was located or when to update it.

**Issue**: Location unclear, update triggers undefined.

**Solution**:
- Clearly state location: `workspace/docs/ENTRYPOINT_PROMPT.md`
- Add instruction: "Update ENTRYPOINT_PROMPT.md whenever entrypoint logic changes"
- Include in building mode prompt so agent always sees it

**Learning**: Make file locations and update responsibilities crystal clear in the prompt.

#### Problem 4: Complex, Multi-Purpose Scripts

**What happened**: Initial tendency to create one large script that does everything.

**Issue**: Hard to debug, not reusable, conversation agent can't track progress.

**Solution**:
- Added "Single-Purpose Scripts Principle" to building process
- Provided clear example: time-off workflow = 2 scripts (get details + book vacation)
- Explained benefits: composability, progress tracking, reusability

**Learning**: Script architecture must be explicitly guided. Provide concrete examples of good vs. bad structure.

#### Problem 5: Passing Large Data Between Scripts

**What happened**: Need to pass large datasets (e.g., parsed lists, bulk records) between scripts.

**Issue**:
- Command-line arguments not suitable for large data
- stdout/print output gets mixed with logs
- No way to inspect intermediate results
- Conversation agent can't verify each step succeeded

**Solution**:
- Use CSV/JSON files in `workspace/files/` folder as intermediate storage
- Script 1 outputs to file: `./files/invoices_parsed.csv`
- Script 2 reads from file: `./files/invoices_parsed.csv`
- Document file formats in scripts README (columns, fields, structure)

**Example**:
```
1. parse_invoices.py → ./files/invoices_parsed.csv (vendor, amount, date, invoice_id)
2. process_invoices.py --input=./files/invoices_parsed.csv → ./files/invoices_processed.json
3. generate_report.py --data=./files/invoices_processed.json → Display report
```

**Benefits**:
- ✅ Agent can inspect intermediate files between steps
- ✅ Clear data flow and state tracking
- ✅ Scripts can run independently
- ✅ Supports restart from any point
- ✅ Human-readable data formats (CSV/JSON)

**Learning**:
- File-based data passing is crucial for multi-step workflows with large data
- Document expected file formats in both scripts README and WORKFLOW_PROMPT.md
- Scripts should print what files they created and what's in them

#### Problem 6: Confusion Between Entrypoint and Workflow Prompts

**What happened**: Building agent created entrypoint prompts that were too detailed or technical, sometimes mixing them with workflow instructions.

**Issue**:
- Entrypoint prompts contained execution details: "Query from Odoo my time-offs and show me the summary"
- Confusion about what goes in entrypoint vs. what goes in workflow prompt
- Missing the relationship between user's building request, entrypoint, and workflow execution

**Solution - Clear Three-Part Structure**:

1. **User's Building Request** (What user tells building agent):
   - "I want an agent that provides info about my time-off balances in Odoo ERP"

2. **ENTRYPOINT_PROMPT.md** (Simple user question to trigger):
   - "What is my time-off balance?"
   - Keep it SHORT and NATURAL - how user would normally ask
   - No technical details, no execution steps

3. **WORKFLOW_PROMPT.md** (System prompt with execution details):
   - Run: `python scripts/get_timeoff_balance.py`
   - Parse JSON output: `{"annual_leave": 15, "sick_leave": 10, ...}`
   - Rephrase for user: "You have 15 days of annual leave..."

**Key Insight**:
- **Entrypoint** = What USER asks (short, natural)
- **Workflow** = How AGENT executes (detailed, technical)
- Conversation agent is a **bridge between scripts and humans**:
  1. Execute scripts
  2. Parse outputs (JSON, CSV)
  3. Rephrase in natural language
  4. Communicate with user

**Example Comparison**:

❌ **Wrong Entrypoint** (too technical):
```
Query from Odoo my time-offs and show me the summary
```

✅ **Correct Entrypoint** (simple, natural):
```
What is my time-off balance?
```

✅ **Workflow Prompt explains the details**:
```markdown
## Workflow Steps
1. Run: `python scripts/get_timeoff_balance.py`
2. Parse JSON output
3. Rephrase for user in conversational language
```

**Learning**:
- Building agent must understand entrypoint vs. workflow distinction
- Entrypoint is USER-facing (simple question/command)
- Workflow prompt is AGENT-facing (execution instructions + presentation guidelines)
- Emphasize that conversation agent processes script outputs and communicates results, doesn't just run and exit

### Key Takeaways for Future Prompt Development

1. **Be Explicit About File Locations**
   - Always state full paths: `workspace/docs/ENTRYPOINT_PROMPT.md`
   - Don't assume agent knows where files live
   - Reference paths in building agent prompt

2. **Use Strong, Direct Language for Critical Requirements**
   - "CRITICAL", "IMMEDIATELY", "MUST" for non-negotiable items
   - Explain consequences of not following requirements
   - Use DO/DON'T lists with visual markers (✅ ❌)

3. **Provide Good vs. Bad Examples**
   - Show what to do AND what not to do
   - Explain WHY each example is good or bad
   - Use real examples from workflow development

4. **Emphasize User Perspective**
   - Entrypoints: "How would a user naturally ask for this?"
   - Scripts: "What does the user need to know to use this?"
   - Documentation: "What will make this clear in conversation mode?"

5. **Make Architecture Principles Explicit**
   - Don't assume agent knows best practices
   - State principles clearly (single-purpose scripts, composability, etc.)
   - Provide reasoning, not just rules

6. **Explain the "Why" Behind Requirements**
   - "Update README because it auto-loads in future sessions"
   - "Single-purpose scripts enable progress tracking"
   - "Human-like entrypoints for natural user interaction"

7. **Test Prompts with Real Integrations**
   - Theoretical prompts miss practical issues
   - Real workflow development reveals gaps
   - Iterate based on actual agent behavior

8. **Provide Data Passing Patterns**
   - Specify when to use command-line args vs. file-based passing
   - Document file naming conventions and formats
   - Show concrete examples of producer/consumer scripts
   - Emphasize documenting file structure (columns, fields)
   - Explain benefits so agent understands the pattern

9. **Clarify Entrypoint vs. Workflow Prompt Distinction**
   - Entrypoint = SHORT, NATURAL user question ("What is my time-off balance?")
   - Workflow = DETAILED execution + presentation instructions
   - Show three-part structure: Building request → Entrypoint → Workflow
   - Emphasize conversation agent is a "bridge": Execute → Parse → Rephrase → Communicate
   - Workflow prompt must include BOTH script execution AND result presentation
   - Provide side-by-side examples of good vs. bad entrypoints

10. **Enforce File Organization Standards**
   - Machine-format files (JSON, CSV, binaries) → `./files/`
   - Human documentation (Markdown, reports) → `./docs/`
   - Explain benefits: clear separation, users know where to find reports, scripts know where data lives
   - Provide examples of correct and incorrect file placement
   - Include in both building and conversation mode prompts for consistency

## Quick Reference: Modifying Building Agent Prompts

When you need to update the building agent prompt (`BUILDING_AGENT_EXAMPLE.md`), use this checklist:

### ✅ Before Making Changes

1. **Read this document** - Review "Prompt Design Principles" and "Lessons Learned"
2. **Identify the issue** - What specific problem are you solving?
3. **Check existing structure** - Does a section already address this?
4. **Test with real workflow** - Has this been validated with actual agent behavior?

### ✅ Writing Effective Instructions

1. **Use strong language for critical items**
   - "CRITICAL", "IMMEDIATELY", "MUST" for non-negotiable requirements
   - "IMPORTANT", "SHOULD", "RECOMMENDED" for strong suggestions
   - Plain language for general guidance

2. **Explain the "why"**
   - Don't just say what to do, explain why it matters
   - Example: "Update README because it auto-loads in future sessions"

3. **Provide examples**
   - Good examples (✅) and bad examples (❌)
   - Explain why each example is good or bad
   - Use real scenarios from workflow development

4. **Make locations explicit**
   - Always use full paths: `workspace/docs/ENTRYPOINT_PROMPT.md`
   - Don't assume agent knows file structure
   - State where files are created/updated

5. **Use visual markers**
   - ✅ ❌ for do/don't
   - Bullet points for lists
   - Code blocks for examples
   - Bold for emphasis

### ✅ Testing Changes

1. **Create a test workflow** - Build a real integration (API, database, etc.)
2. **Verify behavior** - Does agent follow new instructions?
3. **Check outputs** - Are files created/updated as expected?
4. **Review quality** - Are entrypoints human-like? Scripts single-purpose?
5. **Iterate** - Refine based on actual agent behavior

### ✅ Common Additions

**Adding new script types or patterns:**
- Update "Common Tasks" section
- Provide example script structure
- Explain when to use this pattern

**Adding new documentation requirements:**
- State location of file
- Explain when to create/update
- Provide format/template
- Show good vs. bad examples

**Adding new security rules:**
- Use "CRITICAL" or "NEVER" language
- Explain consequences of violation
- Provide safe alternative approach
- Add to credentials section

**Adding new workflow patterns:**
- Update "Building Workflow Development Process"
- Provide concrete examples
- Explain benefits of the pattern

**Adding data passing patterns:**
- Specify when to use the pattern (small vs. large data)
- Provide producer script example (outputs to file)
- Provide consumer script example (reads from file)
- Document file formats and naming conventions
- Show how to document in scripts README
- Explain benefits: inspectable, debuggable, restartable

### ✅ After Making Changes

1. **Update this documentation** - Add to "Lessons Learned" if based on real experience
2. **Rebuild test environment** - New environments will get updated prompt
3. **Monitor behavior** - Watch how agents use new instructions
4. **Document edge cases** - Note any issues discovered

### 📝 File to Modify

**Primary file**: `backend/app/env-templates/python-env-advanced/app/BUILDING_AGENT_EXAMPLE.md`

This file is copied to each new environment as `BUILDING_AGENT.md` during initialization.

**Related files**:
- `docs/agent-sessions/agent_env_building_prompt.md` (this file) - Architecture documentation
- `docs/agent-sessions/agent_env_credentials_management.md` - Credentials documentation
- Template files in `backend/app/env-templates/python-env-advanced/app/`

## Future Enhancements

Potential improvements to the prompt system:

- Load and include `requirements.txt` in prompt if it exists
- Include recent error logs to help debug failing scripts
- Add workspace statistics (file counts, script usage metrics)
- Support for multi-language environments (not just Python)
- Multi-SDK support (OpenAI, Google, etc.)
- Prompt versioning and change tracking
- A/B testing different prompt structures
- Auto-generate script catalog format examples from existing workflows
