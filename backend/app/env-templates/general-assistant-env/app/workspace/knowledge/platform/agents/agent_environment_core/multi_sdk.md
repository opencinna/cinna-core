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
| `claude-code/minimax` | MiniMax M2 | `minimax` | Implemented |

### OpenCode Engine

| SDK ID | Display Name | Credential Type | Status |
|--------|-------------|-----------------|--------|
| `opencode/anthropic` | OpenCode + Anthropic | `anthropic` | Implemented |
| `opencode/openai` | OpenCode + OpenAI | `openai` | Implemented |
| `opencode/openai_compatible` | OpenCode + OpenAI-Compatible | `openai_compatible` | Implemented |
| `opencode/google` | OpenCode + Google | `google` | Implemented |

## SDK ↔ Credential Compatibility Matrix

Each SDK engine only supports certain credential types. This is enforced by backend validation and used for frontend filtering.

| SDK Engine | Compatible Credential Types |
|------------|----------------------------|
| `claude-code` | `anthropic`, `minimax` |
| `opencode` | `anthropic`, `openai`, `openai_compatible`, `google` |

Defined as `SDK_CREDENTIAL_COMPATIBILITY` in `backend/app/services/environments/environment_service.py`.

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
- **SDK ↔ credential validation:** Backend checks compatibility via `_validate_sdk_credential_compatibility()` in `environment_service.py`
- **Default cascade:** If no SDK is provided on creation → use user's `default_sdk_*` fields → fall back to `claude-code/anthropic`
- **MiniMax conflict prevention:** When MiniMax is selected, `ANTHROPIC_API_KEY` is NOT written to `.env` to avoid SDK conflicts
- **OpenAI Compatible requires all three fields:** API key, base URL, and model name must all be set
- **Model override:** `model_override_building` / `model_override_conversation` are optional; when set they override the adapter's default model. Resolution order: explicit override → mode default → SDK default.
- **Rebuild regeneration:** After an environment rebuild (core replacement), config files are regenerated from stored credentials for all SDK types
- **Encrypted storage:** All AI credentials are stored per the named `AICredential` model; no migration needed for new provider types
- **OpenCode per-mode isolation:** Building and conversation modes each run their own `opencode serve` process on separate ports with separate config directories. No shared state between concurrent sessions.
- **Cross-SDK UI parity for ask-user-question:** OpenCode's built-in `question` tool suspends the session until a client calls `/question/{id}/reply` or `/reject`. The OpenCode adapter unifies this behaviour with Claude Code's `AskUserQuestion` tool — the transformer emits the same `askuserquestion` TOOL_USE + DONE event pair, then the adapter fires `POST /question/{id}/reject` so the user's answer can land as a fresh turn.

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
