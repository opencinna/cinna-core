# Building Mode System Prompt

## Overview

When an agent environment runs in **building mode**, it uses a specialized system prompt that instructs the agent to create reusable Python scripts and applications for workflow automation. This prompt combines Claude Code's preset system prompt with custom instructions specific to script development.

## Prompt Architecture

The building mode system prompt is composed of three layers:

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

### 2. SDK Manager Initialization

**File**: `backend/app/env-templates/python-env-advanced/app/server/sdk_manager.py`

**Method**: `__init__()`
- Calls `_load_building_agent_prompt()` to read `BUILDING_AGENT.md` into memory
- Stores content in `self.building_agent_prompt` for reuse across requests

### 3. Runtime Prompt Assembly

**File**: `backend/app/env-templates/python-env-advanced/app/server/sdk_manager.py`

**Method**: `send_message_stream()` with `use_building_mode=True`

**Prompt Construction Logic**:
1. Start with base `building_prompt = self.building_agent_prompt`
2. Call `_load_scripts_readme()` to check for existing scripts catalog
3. If scripts/README.md exists and is not empty:
   - Append it to building_prompt with context header
   - Inform agent these are existing scripts and to keep catalog updated
4. Create `SystemPromptPreset`:
   ```python
   {
       "type": "preset",
       "preset": "claude_code",
       "append": building_prompt  # BUILDING_AGENT.md + scripts catalog
   }
   ```

### 4. Request Routing

**File**: `backend/app/env-templates/python-env-advanced/app/server/routes.py`

**Endpoints**: `/chat` and `/chat/stream`

**Logic**:
- When `request.mode == "building"`, pass `use_building_mode=True` to SDK manager
- Does NOT use `_workflow_prompt` (that's for conversation mode)
- Only uses explicit `request.system_prompt` if provided (for overrides)

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

When building mode is activated with existing scripts:

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
```

## Benefits

1. **Consistency**: All script development follows the same patterns
2. **Context Preservation**: Agent knows about existing work across sessions
3. **Self-Documenting**: Scripts catalog provides built-in documentation
4. **Reusability**: Scripts are designed for workflow automation from the start
5. **Maintainability**: Clear structure and documentation requirements

## Future Enhancements

Potential improvements to the building prompt system:

- Load and include `requirements.txt` in prompt if it exists
- Include recent error logs to help debug failing scripts
- Add workspace statistics (file counts, script usage metrics)
- Support for multi-language environments (not just Python)
