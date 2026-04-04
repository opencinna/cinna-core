# Default AI Functions SDK — Implementation Plan

## Overview

Allow users to optionally use their personal Anthropic API key for AI functions (title generation, agent config generation, prompt refinement, SQL queries, etc.) instead of system-level credentials. A new user preference field `default_ai_functions_sdk` with values `"system"` (default, current behavior) or `"anthropic"` (use personal Anthropic key) controls routing at the AI functions service layer.

**Core capabilities:**
- New user preference stored on the User model
- New Anthropic provider class making direct httpx HTTP calls (no SDK)
- Provider manager extended to support per-call api_key bypass (no cascade fallback)
- AI functions service updated to resolve user preference and thread api_key through calls
- Frontend settings card updated with a new "AI Functions SDK" dropdown

**Flow:**
```
User sets default_ai_functions_sdk = "anthropic"
  → Route calls AIFunctionsService method with user=current_user, db=session
  → Service reads user.default_ai_functions_sdk
  → Service looks up default Anthropic AICredential, decrypts api_key
  → Service calls provider_manager.generate_content(prompt, api_key=key, preferred_provider="anthropic")
  → ProviderManager creates AnthropicProvider(api_key=key) on the fly, calls it directly
  → If it fails: raise (no cascade to system providers)
```

---

## Architecture Overview

```
Frontend (AICredentials.tsx)
  └─ PATCH /api/v1/users/me { default_ai_functions_sdk: "anthropic" }
       └─ users.py → update_user_me (validates + saves)

Route calls AI function (e.g., utils.py, workspace.py)
  └─ AIFunctionsService.refine_user_prompt(db, ..., user=current_user)
       └─ _resolve_provider_kwargs(user, db) → {"api_key": "sk-ant-...", "preferred_provider": "anthropic"}
            └─ provider_manager.generate_content(prompt, **kwargs)
                 └─ AnthropicProvider(api_key=key).generate_content(prompt)
                      └─ httpx POST https://api.anthropic.com/v1/messages
```

---

## Data Models

### User model changes (backend/app/models/users/user.py)

**New constant:**
```python
VALID_AI_FUNCTIONS_SDK_OPTIONS = ["system", "anthropic"]
```

**User table (table=True) — new field:**
```python
default_ai_functions_sdk: str | None = Field(default="system", max_length=50)
```

**UserUpdateMe — new field:**
```python
default_ai_functions_sdk: str | None = Field(default=None, max_length=50)
```

**UserPublic — new field:**
```python
default_ai_functions_sdk: str | None = "system"
```

No new tables. No relationships. No encryption.

---

## Security Architecture

- The `default_ai_functions_sdk` value is not sensitive — it's just a preference.
- When "anthropic" is selected, the service looks up the user's default Anthropic AICredential from the existing `ai_credential` table (already encrypted at rest via `encrypt_field`/`decrypt_field`).
- The API key is decrypted in memory only for the duration of the HTTP call, never logged, never returned to the client.
- Access control: each user can only read/write their own `default_ai_functions_sdk`. The existing `CurrentUser` dependency handles this.
- Input validation: `VALID_AI_FUNCTIONS_SDK_OPTIONS = ["system", "anthropic"]` enforced in `update_user_me`.

---

## Backend Implementation

### 1. backend/app/models/users/user.py

Add to constants section (near VALID_SDK_OPTIONS):
```python
VALID_AI_FUNCTIONS_SDK_OPTIONS = ["system", "anthropic"]
```

Add to `User` table model (after `general_assistant_enabled`):
```python
default_ai_functions_sdk: str | None = Field(default="system", max_length=50)
```

Add to `UserUpdateMe`:
```python
default_ai_functions_sdk: str | None = Field(default=None, max_length=50)
```

Add to `UserPublic`:
```python
default_ai_functions_sdk: str | None = "system"
```

### 2. Alembic migration

New file: `backend/app/alembic/versions/[timestamp]_add_default_ai_functions_sdk_to_user.py`

Adds column: `default_ai_functions_sdk VARCHAR(50) DEFAULT 'system'` to `user` table.
Downgrade: drop the column.

### 3. backend/app/api/routes/users.py

In `update_user_me`, after existing SDK validations, add:
```python
from app.models.user import VALID_AI_FUNCTIONS_SDK_OPTIONS
...
if user_in.default_ai_functions_sdk and user_in.default_ai_functions_sdk not in VALID_AI_FUNCTIONS_SDK_OPTIONS:
    raise HTTPException(
        status_code=400,
        detail=f"Invalid AI functions SDK. Must be one of: {VALID_AI_FUNCTIONS_SDK_OPTIONS}",
    )
```

Import `VALID_AI_FUNCTIONS_SDK_OPTIONS` in the import from `app.models.user`.

### 4. NEW: backend/app/agents/providers/anthropic_provider.py

```python
"""
Anthropic Provider - direct HTTP calls to Anthropic Messages API.
Uses httpx (no Anthropic SDK). Intended for per-user API key usage.
"""
import logging
from typing import Optional
import httpx
from .base import BaseAIProvider, ProviderResponse, ProviderError

logger = logging.getLogger(__name__)

class AnthropicProvider(BaseAIProvider):
    PROVIDER_NAME = "anthropic"
    DEFAULT_MODEL = "claude-haiku-4-5"
    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    REQUEST_TIMEOUT = 30.0

    def __init__(self, api_key: str):
        self._api_key = api_key

    def is_available(self) -> bool:
        return bool(self._api_key)

    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        if not self.is_available():
            raise ProviderError("Anthropic API key not provided", self.PROVIDER_NAME, recoverable=False)
        model_name = model or self.DEFAULT_MODEL
        try:
            response = httpx.post(
                self.API_URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": self.API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": model_name,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=self.REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                error_body = response.text[:200]
                raise ProviderError(
                    f"Anthropic API returned {response.status_code}: {error_body}",
                    self.PROVIDER_NAME,
                    recoverable=False,
                )
            data = response.json()
            text = data["content"][0]["text"].strip()
            return ProviderResponse(text=text, provider_name=self.PROVIDER_NAME, model=model_name)
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("Anthropic API request timed out", self.PROVIDER_NAME, recoverable=False)
        except Exception as e:
            raise ProviderError(f"Anthropic API call failed: {e}", self.PROVIDER_NAME, recoverable=False)
```

Note: `recoverable=False` on all errors — if user chose "anthropic", failures should NOT cascade to system providers.

### 5. backend/app/agents/providers/__init__.py

Add import and export:
```python
from .anthropic_provider import AnthropicProvider
__all__ = [..., "AnthropicProvider"]
```

### 6. backend/app/agents/provider_manager.py

**Register provider:**
```python
from .providers import AnthropicProvider
PROVIDER_REGISTRY["anthropic"] = AnthropicProvider
```

**Modify `generate_content` signature:**
```python
def generate_content(
    self,
    prompt: str,
    model: Optional[str] = None,
    preferred_provider: Optional[str] = None,
    api_key: Optional[str] = None,
) -> ProviderResponse:
```

**When `api_key` is provided** (bypass cascade, use personal key):
```python
if api_key:
    # Direct call with user's personal key — no cascade
    provider = AnthropicProvider(api_key=api_key)
    if not provider.is_available():
        raise ProviderError("Anthropic API key not available", "anthropic", recoverable=False)
    return provider.generate_content(prompt, model)
```
Place this check at the top of `generate_content`, before the cascade loop.

### 7. backend/app/services/ai_functions/ai_functions_service.py

**New imports:**
```python
from app.models.user import User
from app.models.ai_credential import AICredentialType
from app.services.ai_credentials_service import ai_credentials_service
from app.core.security import decrypt_field
import json
```

**New private helper methods:**

```python
@staticmethod
def _get_user_anthropic_key(db: Session, user_id: UUID) -> str | None:
    """Look up user's default Anthropic AICredential and decrypt the api_key."""
    credential = ai_credentials_service.get_default_for_type(db, user_id, AICredentialType.ANTHROPIC)
    if not credential:
        return None
    data = ai_credentials_service._decrypt_credential(credential)
    return data.api_key

@staticmethod
def _resolve_provider_kwargs(user: "User | None", db: "Session | None") -> dict:
    """
    Resolve provider kwargs based on user's ai functions SDK preference.
    Returns {"api_key": key, "preferred_provider": "anthropic"} or {}
    Raises ValueError if user chose anthropic but has no Anthropic credential.
    """
    if not user or not db:
        return {}
    pref = getattr(user, "default_ai_functions_sdk", None) or "system"
    if pref != "anthropic":
        return {}
    api_key = AIFunctionsService._get_user_anthropic_key(db, user.id)
    if not api_key:
        raise ValueError(
            "You selected Personal Anthropic API for AI functions, but no default "
            "Anthropic credential is configured. Please add one in AI Credentials settings."
        )
    return {"api_key": api_key, "preferred_provider": "anthropic"}
```

**Modified service methods** — add `user=None, db=None` optional params and resolve provider kwargs:

For `generate_session_title`, `generate_agent_configuration`, `generate_description_from_workflow`, `generate_sql`: these currently delegate entirely to agent functions. The cleanest approach is to add provider_kwargs to those agent functions.

**Agent functions need minor updates** to accept and pass `provider_kwargs`:
- `title_generator.generate_conversation_title(message_content, provider_kwargs=None)`
- `agent_generator.generate_agent_config(description, provider_kwargs=None)`
- `description_generator.generate_agent_description(workflow_prompt, agent_name=None, provider_kwargs=None)`
- `sql_generator.generate_sql_query(user_request, database_schema, current_query=None, provider_kwargs=None)`
- `prompt_refiner.refine_prompt(..., provider_kwargs=None)`
- `task_refiner.refine_task(..., provider_kwargs=None)`

Each agent function passes `provider_kwargs or {}` to `manager.generate_content(prompt, **(provider_kwargs or {}))`.

Service methods resolve provider_kwargs and pass down:
```python
@staticmethod
def generate_session_title(message_content: str, user=None, db=None) -> str:
    try:
        provider_kwargs = AIFunctionsService._resolve_provider_kwargs(user, db)
        title = generate_conversation_title(message_content, provider_kwargs=provider_kwargs)
        ...
```

For `refine_user_prompt` and `refine_task` (already have db + owner_id): fetch user from db to check preference:
```python
@staticmethod
def refine_user_prompt(db, user_input, ..., owner_id, ...):
    # New: resolve provider kwargs from user preference
    user = db.get(User, owner_id)
    provider_kwargs = AIFunctionsService._resolve_provider_kwargs(user, db)
    ...
    result = refine_prompt(..., provider_kwargs=provider_kwargs)
```

### 8. Route/service integration

**backend/app/api/routes/utils.py** — pass `current_user` to `refine_user_prompt` (already has it, just add param):
```python
# Already has current_user — refine_user_prompt already gets owner_id from current_user.id
# No additional change needed since refine_user_prompt already fetches user from db by owner_id
```

**backend/app/api/routes/workspace.py** — pass user to generate_sql:
```python
result = AIFunctionsService.generate_sql(
    user_request=..., database_schema=..., current_query=...,
    user=current_user, db=session  # NEW
)
```

**backend/app/services/agents/agent_service.py** — `_generate_description_background` creates its own db session; needs user lookup. Pass `user_id` to background function and fetch user from db:
```python
def _generate_description_background(agent_id, workflow_prompt, agent_name, user_id=None):
    with SQLSession(engine) as db_session:
        user = db_session.get(User, user_id) if user_id else None
        description = AIFunctionsService.generate_description_from_workflow(
            workflow_prompt=workflow_prompt, agent_name=agent_name, user=user, db=db_session
        )
```
Pass `user_id` from the calling context in agent_service.py.

For `generate_agent_configuration` in `update_agent`, have access to user object — pass it:
```python
config = AIFunctionsService.generate_agent_configuration(description, user=current_user_obj, db=session)
```
Note: Need to ensure `user` object is available in the calling context (agent_service already has `user_id` in some methods; may need to fetch from db).

**backend/app/services/sessions/session_service.py** — `auto_generate_session_title` is async background task with `get_fresh_db_session`. Modify to accept optional `user_id`:
```python
async def auto_generate_session_title(session_id, first_message_content, get_fresh_db_session, user_id=None):
    with get_fresh_db_session() as db:
        user = db.get(User, user_id) if user_id else None
        title = await asyncio.get_event_loop().run_in_executor(
            None, AIFunctionsService.generate_session_title, message_content, user, db
        )
```
Callers of `auto_generate_session_title` pass `user_id` where available.

---

## Frontend Implementation

### frontend/src/components/UserSettings/AICredentials.tsx

**Changes needed:**

1. Update `updateSdkMutation` type to include new field (the auto-generated client will handle this after backend changes).

2. Inside the "Default SDK Preferences" card, after the Building Mode select row (around line 429) and before the info text (line 432), add a new row:

```tsx
{/* AI Functions SDK */}
<div className="flex items-center justify-between gap-4 py-2">
  <div className="flex items-center gap-3">
    <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-purple-500/10">
      <Sparkles className="h-4 w-4 text-purple-500" />
    </div>
    <div>
      <Label htmlFor="sdk-ai-functions" className="text-sm font-medium">
        AI Functions SDK
      </Label>
      <p className="text-xs text-muted-foreground">
        Provider for AI-powered utilities (titles, suggestions, etc.)
      </p>
    </div>
  </div>
  <Select
    value={status?.default_ai_functions_sdk || "system"}
    onValueChange={(value) => updateSdkMutation.mutate({ default_ai_functions_sdk: value })}
    disabled={updateSdkMutation.isPending}
  >
    <SelectTrigger id="sdk-ai-functions" className="w-[180px]">
      <SelectValue placeholder="Select provider" />
    </SelectTrigger>
    <SelectContent>
      <SelectItem value="system">System (default)</SelectItem>
      <SelectItem value="anthropic">
        Personal Anthropic API
        {!hasDefaultForType("anthropic") && " (no default)"}
      </SelectItem>
    </SelectContent>
  </Select>
</div>
```

3. Add `Sparkles` to the lucide-react import.

4. Add warning to `getMissingKeyWarning` for AI functions SDK if anthropic selected but no key.

---

## Database Migration

**File**: `backend/app/alembic/versions/[timestamp]_add_default_ai_functions_sdk_to_user.py`

```python
def upgrade():
    op.add_column('user', sa.Column('default_ai_functions_sdk', sa.String(length=50), nullable=True, server_default='system'))

def downgrade():
    op.drop_column('user', 'default_ai_functions_sdk')
```

Run: `make migration` then review + `make migrate`.

---

## Error Handling & Edge Cases

1. **User selects "anthropic" but has no default Anthropic credential**: `_resolve_provider_kwargs` raises `ValueError` with user-friendly message. Service methods catch and return error dict. Frontend shows error toast.

2. **Anthropic API returns non-200**: ProviderError with `recoverable=False`. Caller sees error, no cascade.

3. **Anthropic API times out**: ProviderError with timeout message, `recoverable=False`.

4. **User preference is NULL/None**: Treated as "system" (safe default).

5. **Background tasks (description generation, title generation) with no user context**: `_resolve_provider_kwargs(None, None)` returns `{}`, falls back to system providers normally.

6. **Existing callers that don't pass user/db**: All new params are optional with `None` defaults. Backward compatible.

---

## Integration Points

- **AI Credentials Service**: Used to look up and decrypt the user's Anthropic key.
- **Provider Manager**: Extended with `api_key` parameter to support direct provider bypass.
- **User Model**: New field flows through SQLModel → Alembic migration → API response → frontend auto-generated client.
- **Client regeneration**: Required after backend model/route changes: `bash scripts/generate-client.sh`

---

## Implementation Order (with dependencies)

1. `backend/app/models/users/user.py` — add field + constant
2. Create Alembic migration (`make migration`, review, `make migrate`)
3. `backend/app/api/routes/users.py` — add validation
4. `backend/app/agents/providers/anthropic_provider.py` — NEW file
5. `backend/app/agents/providers/__init__.py` — export
6. `backend/app/agents/provider_manager.py` — register + api_key param
7. Individual agent functions — add `provider_kwargs` param pass-through
8. `backend/app/services/ai_functions/ai_functions_service.py` — helpers + user context
9. Route/service integration points
10. `source ./backend/.venv/bin/activate && make gen-client`
11. `frontend/src/components/UserSettings/AICredentials.tsx` — new UI row

---

## Summary Checklist

### Backend
- [ ] Add `default_ai_functions_sdk` field + `VALID_AI_FUNCTIONS_SDK_OPTIONS` to `backend/app/models/users/user.py`
- [ ] Add field to `UserUpdateMe` and `UserPublic` in same file
- [ ] Create and apply Alembic migration
- [ ] Add validation in `backend/app/api/routes/users.py:update_user_me`
- [ ] Create `backend/app/agents/providers/anthropic_provider.py`
- [ ] Export `AnthropicProvider` from `backend/app/agents/providers/__init__.py`
- [ ] Register provider + add `api_key` param to `generate_content` in `backend/app/agents/provider_manager.py`
- [ ] Add `provider_kwargs` param to: `title_generator.py`, `agent_generator.py`, `description_generator.py`, `sql_generator.py`, `prompt_refiner.py`, `task_refiner.py`
- [ ] Add `_get_user_anthropic_key`, `_resolve_provider_kwargs` helpers to `AIFunctionsService`
- [ ] Update `generate_session_title`, `generate_agent_configuration`, `generate_description_from_workflow`, `generate_sql` to accept + use user/db/provider_kwargs
- [ ] Update `refine_user_prompt` and `refine_task` to fetch user and resolve provider_kwargs
- [ ] Update `workspace.py` to pass `user=current_user, db=session` to `generate_sql`
- [ ] Update `agent_service.py` background task to pass `user_id` and use it
- [ ] Update `session_service.py:auto_generate_session_title` to accept + pass `user_id`

### Frontend
- [ ] Add `Sparkles` to lucide-react imports in `AICredentials.tsx`
- [ ] Add AI Functions SDK select row to "Default SDK Preferences" card
- [ ] Regenerate frontend client: `source ./backend/.venv/bin/activate && make gen-client`

### Testing
- [ ] Verify `AnthropicProvider` raises `ProviderError(recoverable=False)` on API errors
- [ ] Verify `_resolve_provider_kwargs` raises `ValueError` when no Anthropic credential
- [ ] Verify `update_user_me` rejects invalid `default_ai_functions_sdk` values
- [ ] Verify migration applies and rolls back cleanly
- [ ] Verify system cascade still works when `default_ai_functions_sdk = "system"`
