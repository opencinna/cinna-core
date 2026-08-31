# Multi-SDK Support

## Purpose

Allow users to choose different AI SDK engines and providers per agent environment, with per-mode (building vs. conversation) selection, automatic config file generation, and optional per-mode model overrides. Two SDK engines are supported: Claude Code and OpenCode.

## Core Concepts

| Term | Definition |
|------|-----------|
| **SDK Engine** | The runtime technology used (e.g., `claude-code`, `opencode`) |
| **SDK ID** | Full adapter identifier combining engine and provider (e.g., `claude-code/anthropic`, `opencode/openai`) |
| **Adapter** | Runtime component inside the agent environment that translates SDK calls to a unified event stream |
| **SDKEvent** | Unified event object produced by all adapters; the backend processes only this format regardless of SDK |
| **Building Mode** | Environment state used for agent development and configuration |
| **Conversation Mode** | Environment state used for executing tasks and chat |
| **SDK Selection** | Per-environment choice of SDK for each mode — set at creation, immutable afterward |
| **Config Files** | JSON files generated inside the environment container (`.claude/`, `.opencode/`) that configure the adapter at runtime |
| **AI Credential** | Named, encrypted API key for a specific LLM provider; selected per environment mode |
| **Model Override** | Optional per-mode field on the environment that overrides the adapter's default model selection |
| **SDK ↔ Credential Compatibility** | Each SDK engine only works with certain credential types; filtered in UI and validated by backend |
| **MCP Bridge** | Stdio MCP servers that expose platform tools (knowledge, task, collaboration) to OpenCode agents |

## Adapter Pattern — Core Concept

Every SDK adapter follows a single contract: implement `send_message_stream()` and yield `SDKEvent` objects. The backend never speaks to an SDK directly — it only processes `SDKEvent` streams.

This means adding a new SDK requires only:
1. A class implementing `BaseSDKAdapter` with `send_message_stream()` and `interrupt_session()`
2. Registration via `@AdapterRegistry.register`
3. A config file generator in `environment_lifecycle.py`

The event translation layer for each adapter is responsible for converting SDK-specific event formats (streaming JSON, SSE, gRPC) into the six core `SDKEventType` values the backend understands.

## Supported SDKs

### Claude Code Engine

| SDK ID | Display Name | Credential Type | Status |
|--------|-------------|-----------------|--------|
| `claude-code/anthropic` | Anthropic Claude | `anthropic` | Implemented (default) |
| `claude-code/minimax` | MiniMax M2 | `minimax` | Implemented (backend only — **temporarily disabled in UI, not currently supported**) |

### OpenCode Engine

| SDK ID | Display Name | Credential Type | Status |
|--------|-------------|-----------------|--------|
| `opencode/anthropic` | OpenCode + Anthropic | `anthropic` | Implemented |
| `opencode/openai` | OpenCode + OpenAI | `openai` | Implemented |
| `opencode/openai_compatible` | OpenCode + OpenAI-Compatible | `openai_compatible` | Implemented |
| `opencode/google` | OpenCode + Google | `google` | Implemented |

## SDK ↔ Credential Compatibility

Each full SDK id (engine + provider suffix) accepts **exactly one** credential type, taken from `SDK_TO_CREDENTIAL_TYPE` in `backend/app/services/environments/sdk_constants.py`. The provider suffix in the SDK id IS the required credential `type` — there is no engine-wide list. Pairing `opencode/anthropic` with an OpenAI credential is rejected even though both share the `opencode` engine, because the env would otherwise write the OpenAI key into `provider.anthropic.options.apiKey` and runtime would fail with HTTP 401 from `api.anthropic.com`.

| Full SDK ID | Required Credential Type |
|-------------|--------------------------|
| `claude-code/anthropic` | `anthropic` |
| `claude-code/minimax` | `minimax` |
| `opencode/anthropic` | `anthropic` |
| `opencode/openai` | `openai` |
| `opencode/openai_compatible` | `openai_compatible` |
| `opencode/google` | `google` |
| `opencode` (engine-only fallback) | `anthropic` |

The strict match is enforced at six places (see `multi_sdk_tech.md` for file references): the env create / update path, the bundle PATCH endpoint, the pre-publish draft endpoint, the publish-time pre-flight, the env-side rebuild resolver, and the publisher AI credential dropdown filter in the Bundle tab.

A coarser engine matrix (`SDK_CREDENTIAL_COMPATIBILITY` — `claude-code` ↔ `[anthropic, minimax]`, `opencode` ↔ `[anthropic, openai, openai_compatible, google]`) is still used in two non-validating spots: as a fallback when an SDK id isn't in the strict map (forward-compat for custom provider suffixes), and in `resolve_default_credential_for_sdk` to rank candidate user-default credentials by provider priority when several are eligible.

## User Stories / Flows

### Setting Up API Keys and Defaults

1. User opens User Settings → AI Credentials
2. In the **Default SDK Preferences** panel, configures defaults per mode (Conversation / Building) using a cascading three-step selection:
   - **Step 1 — SDK Engine**: Claude Code or OpenCode
   - **Step 2 — Credential**: Dropdown filtered to credentials compatible with the chosen engine; first option is "Use Default" (falls back to the credential of that provider type marked as default)
   - **Step 3 — Model Override** (optional): Free-text field with suggestions per credential type; leave empty to use the SDK adapter's built-in default
3. User clicks **Save Preferences** — all selections are saved together to the user record
4. Saved defaults pre-populate the Add Environment dialog for future environment creation

### Creating an Environment with a Custom SDK

1. User opens Add Environment dialog
2. The dialog pre-populates SDK Engine, Credential, and Model Override from the user's saved defaults
3. User selects **SDK Engine** per mode (building / conversation) — two choices: Claude Code, OpenCode
4. **Credential dropdown** is always visible inline (no separate toggle); first option is "Default (use account default)", followed by credentials filtered to the selected engine's compatible types
5. User optionally sets a **Model Override** for finer control (e.g., `gpt-4o-mini`, `claude-opus-4`)
6. User confirms → backend validates SDK ↔ credential compatibility → environment is created
7. Backend generates config files inside the container for the selected SDK

### Agent Runtime SDK Selection

1. Container starts; `SDK_ADAPTER_BUILDING` and `SDK_ADAPTER_CONVERSATION` env vars are set
2. SDK Manager reads the adapter ID for the current mode
3. Adapter loads its per-mode config file from the expected path
4. For OpenCode: `opencode serve` subprocess is launched on a dedicated port (building: 4096, conversation: 4097)
5. All adapters translate their SDK's native events into `SDKEvent` objects for the backend

## Business Rules

- **SDK immutability:** `agent_sdk_conversation` and `agent_sdk_building` cannot be changed after environment creation
- **Credentials required:** Backend rejects environment creation if the user lacks the API key(s) required by the selected SDK
- **SDK ↔ credential validation (strict provider match):** Backend checks compatibility via `_validate_sdk_credential_compatibility()` in `environment_service.py`, which compares the credential's `type` against `sdk_expected_credential_type(full_sdk_id)` from `sdk_constants.py`. The same strict match is applied at every other entry point that writes a credential id alongside an SDK id — `BundleService.update_bundle`, `InstallService._validate_ai_credentials_draft`, `PublishService._validate_publisher_ai_credentials_sdk`
- **Default cascade:** If no SDK is provided on creation → use user's `default_sdk_*` fields → fall back to `claude-code/anthropic`
- **Per-mode credential resolution:** Each mode resolves its own AI key independently. At creation the keys are computed once and written to `.env` / `opencode.json`. On every reconfigure (start, restart, rebuild) the keys are re-resolved from scratch using only the credential ids stored on the environment plus a per-mode fallback to the user's type-default / legacy-profile credential. The fallback is scoped per mode (never all-or-nothing): a mode whose id was never persisted — because its key came from a type-level default at create time — still re-resolves on every reconfigure, even when the other mode pins a specific credential.
- **OpenCode key delivery:** For an `opencode/*` mode, the provider key is embedded directly into the mode's `opencode.json` (not exported as a container env var). The docker-compose templates forward only `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` to the container — `OPENAI_API_KEY` / `GOOGLE_API_KEY` are intentionally not passed through.
- **MiniMax conflict prevention:** When MiniMax is selected, `ANTHROPIC_API_KEY` is NOT written to `.env` to avoid SDK conflicts
- **OpenAI Compatible requires all three fields:** API key, base URL, and model name must all be set
- **Model override:** `model_override_building` / `model_override_conversation` are optional; when set they override the adapter's default model. Resolution order: explicit override → mode default → SDK default.
- **Rebuild regeneration:** After an environment rebuild (core replacement), config files are regenerated from stored credentials for all SDK types
- **Encrypted storage:** All AI credentials are stored per the named `AICredential` model; no migration needed for new provider types
- **OpenCode per-mode isolation:** Building and conversation modes each run their own `opencode serve` process on separate ports with separate config directories. No shared state between concurrent sessions.
- **Cross-SDK UI parity for ask-user-question:** OpenCode's built-in `question` tool suspends the session until a client calls `/question/{id}/reply` or `/reject`. The OpenCode adapter unifies this behaviour with Claude Code's `AskUserQuestion` tool — the transformer emits the same `askuserquestion` TOOL_USE + DONE event pair. The adapter then relays the **next** user message for that session as the answer via `POST /question/{id}/reply` (parameter-free detection through `GET /question`); `reject` is reserved for interrupt/teardown. (An earlier version rejected the question, which aborted the turn and wedged the session so the next message hung and the UI stayed stuck "streaming" — fixed.) See [OpenCode Interactive Questions](opencode_interactive_questions.md). Claude Code's `AskUserQuestion` reaches the same "next message is the answer" outcome by a different mechanism: its CLI always requires interactive permission approval for this tool, which headless sessions can't satisfy, so the adapter denies the tool call outright with an explicit end-your-turn instruction via `ClaudeAgentOptions.can_use_tool` — see [Claude Code Interactive Tools](claude_code_interactive_tools.md).

## Architecture Overview

```
User Settings → Default SDK Preferences
  (SDK Engine + Credential ID + Model Override per mode)
         │ saved to User.default_ai_credential_*_id, default_model_override_*
         ▼
Add Environment Dialog (pre-populated from user defaults)
  (SDK Engine → Credential → Model Override, per mode)
  Credential dropdown always visible; "Default (use account default)" is first option
         │
         ▼
Backend: environment_service.py (validate SDK ↔ credential, resolve defaults)
         │
         ▼
Backend: environment_lifecycle.py (generate .env + config files per SDK)
         │
         ├── Claude Code / Anthropic → ANTHROPIC_API_KEY in .env
         ├── Claude Code / MiniMax  → .claude/building_settings.json
         │                            .claude/conversation_settings.json
         └── OpenCode               → .opencode/building/opencode.json
                                      .opencode/conversation/opencode.json
                                      (model, provider registration, API key,
                                       permissions, tools, MCP bridges)
         │
         ▼
Agent Environment Container
  SDK_ADAPTER_BUILDING / SDK_ADAPTER_CONVERSATION (env vars)
         │
         ▼
sdk_manager.py → AdapterRegistry → ClaudeCodeAdapter (subprocess python SDK)
                                 → OpenCodeAdapter (HTTP → opencode serve :4096/:4097)
         │
         ▼ Each adapter translates SDK-native events
         ▼
Unified SDKEvent stream → Backend WebSocket → Frontend
```

## Integration Points

- **Agent Environments:** SDK fields are part of `AgentEnvironment` model; selection happens at environment creation — see [Agent Environments](../agent_environments/agent_environments.md)
- **AI Credentials:** User-level credential storage and encryption — see [AI Credentials](../../application/ai_credentials/ai_credentials.md)
- **Agent Environment Core:** The `sdk_manager.py` and adapters live inside the environment core — see [Agent Environment Core](agent_environment_core.md)
- **Environment Data Management:** Rebuild flow must regenerate settings files — see [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md)
- **Tools Approval:** OpenCode permission events (`permission.asked`) are forwarded to the frontend as SYSTEM events — see [Tools Approval Management](tools_approval_management.md)
- **AskUserQuestion widget:** OpenCode's `question` tool is remapped to the unified `askuserquestion` TOOL_USE + DONE pair so the existing widget renders identically across SDKs — see [AskUserQuestion Tool Widget](../../application/chat_interface/tool_answer_questions_widget.md)
- **OpenCode interactive questions:** the blocking `question` tool, the session-wedge bug, and the `/reply`-relay fix — see [OpenCode Interactive Questions](opencode_interactive_questions.md)
- **Claude Code interactive tools:** `AskUserQuestion`/`ExitPlanMode`'s CLI-hardcoded ask-only permission and the `can_use_tool` fix — see [Claude Code Interactive Tools](claude_code_interactive_tools.md)
