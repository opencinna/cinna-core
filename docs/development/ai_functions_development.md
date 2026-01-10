# AI Functions Development Guide

## Overview

AI Functions provide simple, fast LLM-powered utilities for text generation tasks like creating conversation titles, generating agent configurations, and other quick processing needs. They use Google's Gemini Flash model for cheap, fast inference.

## Architecture

### Key Components

- **`backend/app/services/ai_functions_service.py`** - Service layer that encapsulates AI function calls
- **`backend/app/agents/`** - Individual agent implementations for specific tasks
- **`backend/app/core/config.py`** - Configuration for `GOOGLE_API_KEY`

### Current Implementations

1. **Title Generator** (`backend/app/agents/title_generator.py`)
   - Generates concise conversation titles from first user message
   - Used in: `backend/app/api/routes/messages.py:155-175`

2. **Agent Config Generator** (`backend/app/agents/agent_generator.py`)
   - Generates agent name, entrypoint_prompt, and workflow_prompt from description
   - Uses prompt templates from `backend/app/agents/prompts/`:
     - `entrypoint_generator_prompt.md` - Creates natural, conversational trigger messages
     - `workflow_generator_prompt.md` - Creates simple draft workflow prompts (2-4 sentences)
   - Used in: `backend/app/services/agent_service.py:132-156`
   - **Note**: Generated prompts are **initial drafts** that building agents will refine later

3. **SQL Query Generator** (`backend/app/agents/sql_generator.py`)
   - Generates SQLite SQL queries from natural language descriptions
   - Takes database schema context (tables, views, columns, types)
   - Returns JSON: `{success: true, sql: "..."}` or `{success: false, error: "..."}`
   - Used in: `backend/app/api/routes/workspace.py` (database viewer)

## Implementation Pattern

All AI functions follow this simple pattern:

1. **Use Google GenAI Client directly** (not ADK)
   - Import: `from google.genai import Client`
   - Create client with API key: `Client(api_key=api_key)`

2. **Simple synchronous calls**
   - Use `client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)`
   - Extract response: `response.text.strip()`

3. **Graceful fallbacks**
   - Always include fallback logic if API call fails
   - Return sensible defaults

## Adding New AI Functions

### Step 1: Create Agent File

Create a new file in `backend/app/agents/` (e.g., `my_new_agent.py`):

```
def my_function(input_data: str, api_key: str) -> str:
    client = Client(api_key=api_key)
    prompt = f"Your prompt here with {input_data}"
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    return response.text.strip()
```

### Step 2: Export from `__init__.py`

Add to `backend/app/agents/__init__.py`:
```python
from .my_new_agent import my_function
__all__ = ["...", "my_function"]
```

### Step 3: Add Service Method

Add method to `AIFunctionsService` in `backend/app/services/ai_functions_service.py`:

```python
@staticmethod
def my_service_method(input_data: str) -> str:
    try:
        api_key = AIFunctionsService._get_api_key()
        result = my_function(input_data, api_key)
        logger.info(f"Generated result: {result}")
        return result
    except ValueError:
        raise  # Re-raise config errors
    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        return "fallback value"
```

### Step 4: Use in Application Code

Call from routes or services:
```python
if AIFunctionsService.is_available():
    result = AIFunctionsService.my_service_method(input_data)
else:
    result = fallback_value
```

## Best Practices

### 1. Keep Functions Simple
- AI functions should do ONE thing
- Use fast models (`gemini-2.5-flash-lite`)
- Avoid complex multi-turn conversations

### 2. Always Provide Fallbacks
- Check `AIFunctionsService.is_available()` before calling
- Wrap calls in try/except blocks
- Return sensible defaults on failure

### 3. Use Synchronous Calls
- These are simple text generation tasks
- No need for ADK Runner/Session complexity
- Direct `Client.models.generate_content()` is sufficient

### 4. Prompt Engineering
- Be specific in prompts
- Request exact output format (JSON, plain text, etc.)
- Include examples in prompts when needed

### 5. Response Parsing
- Clean up responses (remove markdown, quotes, etc.)
- Validate expected fields for structured output (JSON)
- Handle parsing failures gracefully

## Configuration

### Environment Variable

Add to `.env`:
```
GOOGLE_API_KEY=your-api-key-here
```

Get API key from: https://makersuite.google.com/app/apikey

### Model Selection

Current model: `gemini-2.5-flash-lite`
- Fast and cheap
- Good for simple text generation
- Sufficient for titles, names, short descriptions

For more complex tasks, consider upgrading to `gemini-2.5-flash` or `gemini-2.5-pro`.

## Testing

When adding new AI functions:

1. **Test with API key configured** - Verify actual LLM calls work
2. **Test without API key** - Verify fallbacks work correctly
3. **Test with malformed responses** - Ensure parsing errors are handled
4. **Test edge cases** - Empty inputs, very long inputs, special characters

## Common Pitfalls

### ❌ Don't Use ADK for Simple Tasks
Google ADK (Agent Development Kit) is for complex multi-agent workflows. For simple text generation, use `google.genai.Client` directly.

### ❌ Don't Make Async Functions
Unless integrating with async routes, keep AI functions synchronous. The GenAI client handles I/O internally.

### ❌ Don't Forget Error Handling
Network issues, API errors, and quota limits can occur. Always wrap calls and provide fallbacks.

### ❌ Don't Hardcode API Keys
Always use `settings.GOOGLE_API_KEY` from config, never hardcode keys in code.

## Prompt Template Files

For complex generation tasks that require detailed instructions, use external prompt template files:

### Agent Config Generator Pattern

The agent generator uses prompt templates stored in markdown files:

**Location**: `backend/app/agents/prompts/`

**Files**:
- `entrypoint_generator_prompt.md` - Instructions for generating human-like trigger messages
- `workflow_generator_prompt.md` - Instructions for generating draft workflow prompts

**Why use template files?**
- Easy to update prompts without code changes
- Better version control for prompt iterations
- Clear separation of prompt engineering from code logic
- Non-technical users can review and suggest improvements

**Implementation Pattern**:
```python
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
ENTRYPOINT_PROMPT = PROMPTS_DIR / "entrypoint_generator_prompt.md"

def _load_prompt_template(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8")

def generate_entrypoint(description: str, api_key: str) -> str:
    template = _load_prompt_template(ENTRYPOINT_PROMPT)
    prompt = f"{template}\n\n---\n\n## User's Description\n\n{description}"
    # ... generate with LLM
```

**Key Principles for Prompt Templates**:
1. **Clear instructions** - Explain the task and requirements
2. **Good vs. bad examples** - Show what to do and what to avoid
3. **Format guidance** - Specify exact output format expected
4. **Constraints** - Define length limits, style requirements
5. **Context** - Explain why the output will be used (e.g., "This is a draft that will be refined later")

**Reference**: See `backend/app/agents/agent_generator.py` for full implementation

## Examples

See existing implementations:
- Simple text generation: `backend/app/agents/title_generator.py`
- Template-based generation: `backend/app/agents/agent_generator.py`
- Structured JSON output: `backend/app/agents/sql_generator.py`
- Prompt templates: `backend/app/agents/prompts/`
- Service integration: `backend/app/services/ai_functions_service.py`
- Usage in routes: `backend/app/api/routes/messages.py:155-175`, `backend/app/api/routes/workspace.py`
- Usage in services: `backend/app/services/agent_service.py:132-156`

## Future Enhancements

Potential AI functions to add:
- Code snippet summarization
- Error message explanation
- Parameter suggestion from context
- Commit message generation

Keep functions focused, fast, and simple!
