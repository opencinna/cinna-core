# AI Functions Development Guide

## Overview

AI Functions provide simple, fast LLM-powered utilities for text generation tasks like creating conversation titles, generating agent configurations, and other quick processing needs. They support multiple LLM providers with cascade fallback for reliability.

## Architecture

### Key Components

- **`backend/app/services/ai_functions_service.py`** - Service layer that encapsulates AI function calls
- **`backend/app/agents/`** - Individual agent implementations for specific tasks
- **`backend/app/agents/providers/`** - Provider implementations for different LLM backends
- **`backend/app/agents/provider_manager.py`** - Cascade provider selection logic
- **`backend/app/core/config.py`** - Configuration for providers and API keys

### Multi-Provider Support

AI Functions support multiple LLM providers with cascade fallback:

1. **Gemini** - Google Gemini via google-genai SDK (default)
2. **OpenAI-Compatible** - Any OpenAI-compatible endpoint via litellm (Ollama, vLLM, local deployments)

Configure provider order with `AI_FUNCTIONS_PROVIDERS` environment variable:
```bash
# Try OpenAI-compatible first, fall back to Gemini
AI_FUNCTIONS_PROVIDERS=openai-compatible,gemini

# Only use Gemini (default)
AI_FUNCTIONS_PROVIDERS=gemini
```

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

4. **Prompt Refiner** (`backend/app/agents/prompt_refiner.py`)
   - Refines user prompts to make them more effective for AI agents
   - Takes context: user input, files attached flag, agent details (fetched by ID), session mode, new agent flag
   - Returns JSON: `{success: true, refined_prompt: "..."}` or `{success: false, error: "..."}`
   - Used in: `backend/app/api/routes/utils.py` (POST `/api/v1/utils/refine-prompt/`)
   - **Frontend integration**: Sparkles button appears on hover over message input (Dashboard and Session pages)
   - **Context-aware**: Adapts refinement based on:
     - Agent's name, entrypoint prompt, and workflow prompt (fetched from DB)
     - Session mode (building vs conversation)
     - Whether user is creating a new agent
     - Whether files are attached to the message

## Implementation Pattern

All AI functions follow this simple pattern using the provider manager:

1. **Use the Provider Manager** (not direct client)
   - Import: `from .provider_manager import get_provider_manager`
   - Get manager: `manager = get_provider_manager()`

2. **Simple synchronous calls**
   - Use `manager.generate_content(prompt)` for automatic provider cascade
   - Extract response: `response.text`

3. **Graceful fallbacks**
   - Provider manager handles cascade fallback automatically
   - Always include application-level fallback logic for complete failures
   - Return sensible defaults

## Adding New AI Functions

### Step 1: Create Agent File

Create a new file in `backend/app/agents/` (e.g., `my_new_agent.py`):

```python
"""
My new agent - description of what it does.

Uses the provider manager for cascade provider selection.
"""
from .provider_manager import get_provider_manager


def my_function(input_data: str) -> str:
    """Generate something from input data."""
    manager = get_provider_manager()
    prompt = f"Your prompt here with {input_data}"
    response = manager.generate_content(prompt)
    return response.text
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
        result = my_function(input_data)
        logger.info(f"Generated result: {result}")
        return result
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

### Environment Variables

Add to `.env`:

```bash
# Provider selection (comma-separated, tried in order)
AI_FUNCTIONS_PROVIDERS=gemini  # Default

# Gemini provider settings
GOOGLE_API_KEY=your-api-key-here

# OpenAI-compatible provider settings (for local/custom endpoints)
OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1  # e.g., Ollama
OPENAI_COMPATIBLE_API_KEY=optional-api-key
OPENAI_COMPATIBLE_MODEL=llama3.2:latest
```

### Provider Configuration

**Gemini (default)**
- Get API key from: https://makersuite.google.com/app/apikey
- Default model: `gemini-2.5-flash-lite` (fast, cheap)

**OpenAI-Compatible**
- Works with: Ollama, vLLM, LM Studio, any OpenAI-compatible API
- Set `OPENAI_COMPATIBLE_BASE_URL` to your endpoint
- API key optional for local deployments

### Example Configurations

**Local development with Ollama fallback:**
```bash
AI_FUNCTIONS_PROVIDERS=openai-compatible,gemini
OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1
OPENAI_COMPATIBLE_MODEL=llama3.2:latest
GOOGLE_API_KEY=your-backup-key
```

**Cloud-only (Gemini):**
```bash
AI_FUNCTIONS_PROVIDERS=gemini
GOOGLE_API_KEY=your-api-key
```

### Model Selection

Gemini models:
- `gemini-2.5-flash-lite` - Fast and cheap (default)
- `gemini-2.5-flash` - Balanced performance
- `gemini-2.5-pro` - Best quality

For OpenAI-compatible, model depends on your endpoint (e.g., `llama3.2:latest`, `gpt-4o-mini`).

## Testing

When adding new AI functions:

1. **Test with API key configured** - Verify actual LLM calls work
2. **Test without API key** - Verify fallbacks work correctly
3. **Test with malformed responses** - Ensure parsing errors are handled
4. **Test edge cases** - Empty inputs, very long inputs, special characters

## Common Pitfalls

### ❌ Don't Use ADK for Simple Tasks
Google ADK (Agent Development Kit) is for complex multi-agent workflows. For simple text generation, use the provider manager.

### ❌ Don't Make Async Functions
Unless integrating with async routes, keep AI functions synchronous. The provider manager handles I/O internally.

### ❌ Don't Forget Error Handling
Network issues, API errors, and quota limits can occur. The provider manager handles cascade fallback, but always provide application-level fallbacks.

### ❌ Don't Hardcode API Keys
Always use settings from config via the provider manager, never hardcode keys in code.

### ❌ Don't Bypass the Provider Manager
Always use `get_provider_manager().generate_content()` instead of creating direct clients. This ensures cascade fallback works correctly.

## Adding New Providers

To add a new LLM provider:

### Step 1: Create Provider Implementation

Create a new file in `backend/app/agents/providers/` (e.g., `my_provider.py`):

```python
"""
My Provider - description of what it supports.
"""
import logging
from typing import Optional

from app.core.config import settings
from .base import BaseAIProvider, ProviderResponse, ProviderError

logger = logging.getLogger(__name__)


class MyProvider(BaseAIProvider):
    """My provider implementation."""

    PROVIDER_NAME = "my-provider"

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or settings.MY_PROVIDER_API_KEY

    def is_available(self) -> bool:
        return bool(self._api_key)

    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        if not self.is_available():
            raise ProviderError("MY_PROVIDER_API_KEY not configured", self.PROVIDER_NAME)

        try:
            # Your LLM client implementation here
            text = "..."  # Generated text
            return ProviderResponse(
                text=text,
                provider_name=self.PROVIDER_NAME,
                model=model or "default-model",
            )
        except Exception as e:
            raise ProviderError(f"Failed: {e}", self.PROVIDER_NAME, recoverable=True)
```

### Step 2: Register Provider

Add to `backend/app/agents/provider_manager.py`:

```python
from .providers import MyProvider

PROVIDER_REGISTRY: dict[str, type[BaseAIProvider]] = {
    "gemini": GeminiProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "my-provider": MyProvider,  # Add here
}
```

### Step 3: Export from `__init__.py`

Add to `backend/app/agents/providers/__init__.py`:
```python
from .my_provider import MyProvider
__all__ = [..., "MyProvider"]
```

### Step 4: Add Configuration

Add to `backend/app/core/config.py`:
```python
MY_PROVIDER_API_KEY: str | None = None
MY_PROVIDER_MODEL: str = "default-model"
```

Now users can add `my-provider` to `AI_FUNCTIONS_PROVIDERS` in their `.env`.

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
from .provider_manager import get_provider_manager

PROMPTS_DIR = Path(__file__).parent / "prompts"
ENTRYPOINT_PROMPT = PROMPTS_DIR / "entrypoint_generator_prompt.md"

def _load_prompt_template(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8")

def generate_entrypoint(description: str) -> str:
    manager = get_provider_manager()
    template = _load_prompt_template(ENTRYPOINT_PROMPT)
    prompt = f"{template}\n\n---\n\n## User's Description\n\n{description}"
    response = manager.generate_content(prompt)
    return response.text
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
- Context-aware refinement: `backend/app/agents/prompt_refiner.py`
- Prompt templates: `backend/app/agents/prompts/`
- Service integration: `backend/app/services/ai_functions_service.py`
- Usage in routes: `backend/app/api/routes/messages.py:155-175`, `backend/app/api/routes/workspace.py`, `backend/app/api/routes/utils.py`
- Usage in services: `backend/app/services/agent_service.py:132-156`

## Future Enhancements

Potential AI functions to add:
- Code snippet summarization
- Error message explanation
- Parameter suggestion from context
- Commit message generation

Keep functions focused, fast, and simple!
