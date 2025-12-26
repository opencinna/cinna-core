# Agent Environment System Prompts

## Overview

Agent environments support two distinct modes, each with tailored system prompts and configurations:

1. **Building Mode**: Development-focused mode for creating workflows, scripts, and configurations
2. **Conversation Mode**: Execution-focused mode for running pre-built workflows and tasks

This document describes the prompt architecture, construction, and usage patterns for both modes.

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

4. **docs/WORKFLOW_PROMPT.md** (Dynamic Context)
   - System prompt for conversation mode agent
   - Loaded at runtime and included in building mode prompt
   - Agent updates this as workflow capabilities evolve

5. **docs/ENTRYPOINT_PROMPT.md** (Dynamic Context)
   - Trigger message for workflow execution
   - Loaded at runtime and included in building mode prompt
   - Agent defines concise command to start the workflow

6. **credentials/README.md** (Dynamic Context)
   - Documentation of credentials shared with the agent
   - Redacted sensitive data (shows structure, hides values)
   - Loaded at runtime if file exists and is not empty
   - Provides agent with awareness of available credentials for script development

## Workflow Documentation Prompts

The building agent maintains two key documentation files that define how the completed workflow will be used:

### 1. WORKFLOW_PROMPT.md (System Prompt for Conversation Mode)

**Purpose**: Comprehensive system prompt for the conversation mode agent that will execute the workflow.

**Location**: `{instance_dir}/app/docs/WORKFLOW_PROMPT.md`

**Template**: `backend/app/env-templates/python-env-advanced/app/docs/WORKFLOW_PROMPT.md`

**Content**:
- Agent's role and responsibilities
- Available scripts, APIs, and data sources
- Execution flow and decision-making guidelines
- Database schemas and file formats
- Error handling patterns

**Loading**: `sdk_manager.py::_load_workflow_prompt()` - included in building mode system prompt

**Sync**: `MessageService.sync_agent_prompts_from_environment()` - synced to backend Agent model after building sessions

### 2. ENTRYPOINT_PROMPT.md (Trigger Message)

**Purpose**: Concise user message (1-2 sentences) that triggers workflow execution.

**Location**: `{instance_dir}/app/docs/ENTRYPOINT_PROMPT.md`

**Template**: `backend/app/env-templates/python-env-advanced/app/docs/ENTRYPOINT_PROMPT.md`

**Content**:
- Short, actionable command (e.g., "Collect from my mailbox unread emails, detect invoices, and give summary")
- NOT a system prompt - this is a user message
- Used for scheduled workflow execution
- Can include parameters or configuration

**Loading**: `sdk_manager.py::_load_entrypoint_prompt()` - included in building mode system prompt

**Sync**: `MessageService.sync_agent_prompts_from_environment()` - synced to backend Agent model after building sessions

### Bidirectional Sync

**From Environment → Backend** (automatic):
- Triggered after building mode sessions complete
- `messages.py` route calls `MessageService.sync_agent_prompts_from_environment()`
- Reads prompts via `docker_adapter.py::get_agent_prompts()` (calls `/config/agent-prompts`)
- Updates `Agent.workflow_prompt` and `Agent.entrypoint_prompt` fields

**From Backend → Environment** (manual):
- User edits prompts in backend UI
- Calls `agents.py::sync_agent_prompts()` route
- Pushes via `docker_adapter.py::set_agent_prompts()` (calls `/config/agent-prompts`)
- Updates files in `{instance_dir}/app/docs/`

### Building Agent Instructions

The building agent sees both prompts in its system prompt and is instructed to:
- Update `WORKFLOW_PROMPT.md` as scripts and capabilities are developed
- Fill in `ENTRYPOINT_PROMPT.md` with a concise trigger message
- Keep both files current as the workflow evolves

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
- **All output files** → `./files/`
- **All packages** → installed via `uv`

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

## Conversation Mode Prompt Architecture

The conversation mode system prompt is lightweight and execution-focused:

### Prompt Components

1. **WORKFLOW_PROMPT.md** (Main System Prompt)
   - Describes workflow's purpose, capabilities, and responsibilities
   - Execution flow and decision-making guidelines
   - Database schemas, file formats, and data structures
   - Error handling patterns and success criteria

2. **scripts/README.md** (Available Tools)
   - Catalog of existing scripts and their usage
   - Appended to system prompt for script awareness
   - Formatted as "Available Scripts" section

3. **credentials/README.md** (Available Credentials)
   - Documentation of credentials shared with the agent
   - Redacted sensitive data (shows structure, hides values)
   - Appended to system prompt for credential awareness
   - Formatted as "Available Credentials" section

### Differences from Building Mode

**What's EXCLUDED**:
- ❌ Claude Code preset (no development overhead)
- ❌ BUILDING_AGENT.md (no development instructions)
- ❌ ENTRYPOINT_PROMPT.md (not needed for execution)

**What's INCLUDED**:
- ✅ WORKFLOW_PROMPT.md (workflow-specific instructions)
- ✅ scripts/README.md (available automation tools)
- ✅ credentials/README.md (available credentials with redacted sensitive data)

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
   - `/app/workspace/credentials/README.md` (fresh load)
4. Constructs SystemPromptPreset dict with Claude Code preset
5. SDK Manager passes to ClaudeAgentOptions

**Conversation Mode**:
1. SDK Manager receives request with `mode="conversation"`
2. Calls `PromptGenerator.generate_conversation_mode_prompt()`
3. PromptGenerator loads:
   - `/app/workspace/docs/WORKFLOW_PROMPT.md` (fresh load)
   - `/app/workspace/scripts/README.md` (fresh load)
   - `/app/workspace/credentials/README.md` (fresh load)
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

## Benefits

1. **Mode Separation**: Clear distinction between development and execution
2. **Performance**: Haiku provides 2-5x faster responses in conversation mode
3. **Cost Efficiency**: Haiku reduces costs by ~90% for execution tasks
4. **Context Preservation**: Building mode agent knows about existing work
5. **Consistency**: All script development follows the same patterns
6. **Self-Documenting**: Scripts catalog provides built-in documentation
7. **Reusability**: Scripts designed for workflow automation from the start
8. **Maintainability**: Clear structure and documentation requirements

## Future Enhancements

Potential improvements to the prompt system:

- Load and include `requirements.txt` in prompt if it exists
- Include recent error logs to help debug failing scripts
- Add workspace statistics (file counts, script usage metrics)
- Support for multi-language environments (not just Python)
- Multi-SDK support (OpenAI, Google, etc.)
- Prompt versioning and change tracking
- A/B testing different prompt structures
