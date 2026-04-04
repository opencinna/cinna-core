# AI Functions SDK Routing

## Purpose

Allow users to optionally route AI utility function calls (title generation, schedule generation, agent config generation, prompt refinement, SQL generation, email reply generation, handover prompt generation) through their personal Anthropic API key instead of the system-level AI provider cascade.

## Core Concepts

- **`default_ai_functions_sdk`** — A user preference field (stored on the `User` model) that controls which provider is used for AI utility calls. Two valid values: `"system"` (default) or `"personal:anthropic"`.
- **`default_ai_functions_credential_id`** — An optional user preference field that pins a specific Anthropic `AICredential` UUID to use for AI functions. When `null`, the user's default Anthropic credential is used automatically.
- **System routing** — AI utility calls use the system-configured provider cascade (`AI_FUNCTIONS_PROVIDERS` env variable). This is the default and requires no user API keys.
- **Personal Anthropic routing** — AI utility calls bypass the system cascade and use the user's own Anthropic AI Credential directly.
- **No cascade on personal key** — When personal Anthropic routing is selected, any failure raises an error immediately. There is no fallback to system providers.

## AI Functions Covered

The `default_ai_functions_sdk` preference applies to all AI utility calls routed through `AIFunctionsService`. All functions in the table below respect the user's SDK preference:

| Function | Description |
|---|---|
| `generate_session_title` | Generates a concise title for a new chat session |
| `generate_agent_configuration` | Generates agent name, entrypoint prompt, and workflow prompt from a description |
| `generate_description_from_workflow` | Generates a short agent description from its workflow prompt |
| `generate_sql` | Generates SQL queries from natural language in the database viewer |
| `refine_user_prompt` | Improves a user's chat message before sending |
| `refine_task` | Improves an input task description based on user feedback |
| `generate_schedule` | Generates a CRON expression from a natural language schedule description |
| `generate_handover_prompt` | Generates an AI handover prompt between two agents |
| `generate_email_reply` | Generates a professional email reply from agent session results |

## User Flow

1. User opens **Settings > AI Credentials**
2. In the "Default SDK Preferences" card, the "AI Functions" section (below the separator, after Conversation/Building mode rows) shows a dropdown
3. User selects "Personal Anthropic" — this calls `PATCH /api/v1/users/me` with `{"default_ai_functions_sdk": "personal:anthropic"}`
4. A second "Credential" dropdown appears below the provider selector
5. User can leave it at "Use Default" (which uses their default Anthropic credential) or select a specific named Anthropic credential
6. Selecting a specific credential calls `PATCH /api/v1/users/me` with `{"default_ai_functions_credential_id": "<uuid>"}`
7. Going forward, every AI utility call made by this user uses the selected credential
8. If the user selects "System (default)", `default_ai_functions_sdk` is set to `"system"` and `default_ai_functions_credential_id` is cleared to `null` automatically

## Prerequisite: Anthropic Credential

Selecting "Personal Anthropic" requires a valid Anthropic AI Credential (not an OAuth token):

- User must have at least one `anthropic`-type AI Credential with an API key (prefix `sk-ant-api*`)
- OAuth tokens (prefix `sk-ant-oat*`) are shown in the credential picker with "(OAuth - incompatible)" and are disabled for selection
- If the user's default Anthropic credential is an OAuth token, a warning is shown in the UI
- If no Anthropic credentials exist at all, a message is shown in the picker area
- The settings UI shows a "Missing default credential" warning badge if "Personal Anthropic" is selected but no valid default Anthropic credential exists

## OAuth Token Restriction

OAuth tokens (`sk-ant-oat*`) cannot be used with the Anthropic Messages API (they work only with the Claude CLI). The system enforces this at two points:

1. **At save time** (`PATCH /api/v1/users/me`): If `default_ai_functions_credential_id` points to a credential whose API key starts with `sk-ant-oat`, the request is rejected with HTTP 400.
2. **At call time** (`_resolve_provider_kwargs`): Even if a credential slips through (e.g., the credential was an API key when pinned but was later edited to an OAuth token), the service checks the key prefix and raises `ValueError` before calling the API.

## Error Cases

| Situation | Result |
|---|---|
| No default Anthropic credential configured (no `credential_id` set) | Service raises `ValueError`: "You selected Personal Anthropic API for AI functions, but no default Anthropic credential is configured. Please add one in AI Credentials settings." |
| Pinned credential not found or no longer owned | Service raises `ValueError`: "The selected AI functions credential was not found or you no longer have access. Please update your AI Functions settings." |
| API key is an OAuth token (`sk-ant-oat*`) | Service raises `ValueError`: "OAuth tokens cannot be used with the Anthropic API for AI functions. Please select a credential with an API key (sk-ant-api*)." |
| Setting `credential_id` to an OAuth token | `PATCH /api/v1/users/me` returns HTTP 400: "OAuth tokens cannot be used with the Anthropic API for AI functions. Please select a credential with an API key (sk-ant-api*)." |
| Setting `credential_id` to a non-Anthropic credential | `PATCH /api/v1/users/me` returns HTTP 400: "Only Anthropic credentials can be used for AI functions" |
| Anthropic API returns non-200 | `ProviderError` with `recoverable=False` — no cascade to system providers |
| Anthropic API request times out | `ProviderError` with `recoverable=False` — no cascade |
| `default_ai_functions_sdk` is `null` or unset | Treated as `"system"` (safe default) |
| Background tasks without user context | `_resolve_provider_kwargs(None, None)` returns `{}` — system routing, never fails |

## Data Flow

```
User sets default_ai_functions_sdk = "personal:anthropic" via PATCH /users/me
  → Validated against VALID_AI_FUNCTIONS_SDK_OPTIONS = ["system", "personal:anthropic"]
  → Persisted to user.default_ai_functions_sdk in database
  → If value does not start with "personal:": default_ai_functions_credential_id is automatically cleared to null

User optionally sets default_ai_functions_credential_id via PATCH /users/me
  → Validates ownership (cred.owner_id == current_user.id)
  → Validates type == AICredentialType.ANTHROPIC
  → Validates credential is not an OAuth token (sk-ant-oat*)
  → Persisted to user.default_ai_functions_credential_id in database

Route calls AI function (e.g., refine_user_prompt in routes/agents.py)
  → AIFunctionsService._resolve_provider_kwargs(user, db)
       → pref = user.default_ai_functions_sdk  (or "system" if None)
       → If pref == "personal:anthropic":
            → credential_id = user.default_ai_functions_credential_id
            → If credential_id set:
                 → Look up credential by ID, verify ownership, decrypt → api_key
            → Else:
                 → ai_credentials_service.get_default_for_type(db, user.id, ANTHROPIC)
                 → ai_credentials_service._decrypt_credential(credential) → api_key
            → Validate api_key does not start with "sk-ant-oat"
            → Returns {"api_key": "sk-ant-..."}
       → Else: Returns {}

  → provider_manager.generate_content(prompt, **provider_kwargs)
       → If api_key in kwargs:
            → AnthropicProvider(api_key=key).generate_content(prompt)
            → Direct httpx POST to https://api.anthropic.com/v1/messages
            → Model: claude-haiku-4-5 (default, fast/cheap)
            → No fallback on error
       → Else: Normal cascade through configured system providers
```

## Security Notes

- `default_ai_functions_sdk` and `default_ai_functions_credential_id` are not sensitive — they are preference values.
- The API key is decrypted in memory only for the duration of the HTTP call — it is never logged or returned to the client.
- Access control is handled by the existing `CurrentUser` dependency — users can only read and update their own preferences.
- The `AnthropicProvider` does not read API keys from environment variables; it only accepts an explicitly passed `api_key` parameter.
- OAuth token rejection prevents silent failures when a user has only OAuth credentials configured.

## Integration Points

- **AI Credentials** — The user's Anthropic credential is looked up via `ai_credentials_service.get_default_for_type` (when no credential ID is pinned) or via direct `db.get(AICredential, credential_id)` (when a specific credential is pinned). See [AI Credentials](ai_credentials.md).
- **`AICredentialPublic.is_oauth_token`** — The `is_oauth_token: bool` field (computed in `_to_public()`, not stored) tells the frontend whether a credential is an OAuth token so it can disable incompatible options in the picker. See `backend/app/models/credentials/ai_credential.py`.
- **Provider Manager** — `generate_content(prompt, api_key=key)` triggers the bypass path (bypasses cascade, calls `AnthropicProvider` directly). See `backend/app/agents/provider_manager.py`.
- **User Model** — `default_ai_functions_sdk` and `default_ai_functions_credential_id` fields on the `User` table, exposed in `UserPublic` and `UserUpdateMe`. See `backend/app/models/users/user.py`.
- **Background Tasks** — `session_service.auto_generate_session_title` and `agent_service._generate_description_background` both accept a `user_id` parameter and resolve provider kwargs when user context is available.

## Database Migration

Migration: `f1g2h3i4j5k6_add_default_ai_functions_sdk_to_user.py`

Adds `default_ai_functions_sdk VARCHAR(50) DEFAULT 'system'` to the `user` table. Existing rows are set to `'system'` on migration.

Migration `g2h3i4j5k6l7_add_ai_functions_credential_id_to_user.py` adds `default_ai_functions_credential_id UUID` (nullable, no FK constraint) to the `user` table.

---

*Last updated: 2026-03-18*
