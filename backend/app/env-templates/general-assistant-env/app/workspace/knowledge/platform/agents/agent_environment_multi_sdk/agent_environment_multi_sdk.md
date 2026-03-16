# Agent Environment Multi-SDK

## Purpose

Allow users to choose different AI SDK providers (Anthropic Claude, MiniMax M2, OpenAI-compatible endpoints) per agent environment, with per-mode (building vs. conversation) selection, automatic settings file generation, and user-level default preferences.

## Core Concepts

| Term | Definition |
|------|-----------|
| **SDK** | AI provider integration (e.g., `claude-code/anthropic`, `claude-code/minimax`, `google-adk-wr/openai-compatible`) |
| **Adapter** | Runtime component inside the agent environment that translates SDK calls to a unified event stream |
| **Building Mode** | Environment state used for agent development and configuration |
| **Conversation Mode** | Environment state used for executing tasks and chat |
| **SDK Selection** | Per-environment choice of SDK for each mode — set at creation, immutable afterward |
| **Settings Files** | JSON config files generated inside the environment container that configure the adapter at runtime |
| **AI Credentials** | User-level encrypted API keys: `anthropic_api_key`, `minimax_api_key`, `openai_compatible_api_key` + URL + model |
| **Default SDK Prefs** | User-level preference fields (`default_sdk_conversation`, `default_sdk_building`) used as fallback when creating environments |

## Supported SDKs

| SDK ID | Display Name | Required Credentials | Status |
|--------|-------------|----------------------|--------|
| `claude-code/anthropic` | Anthropic Claude | `anthropic_api_key` | Implemented (default) |
| `claude-code/minimax` | MiniMax M2 | `minimax_api_key` | Implemented |
| `google-adk-wr/openai-compatible` | OpenAI Compatible | `openai_compatible_api_key`, `openai_compatible_base_url`, `openai_compatible_model` | Config ready, adapter skeleton |
| `google-adk-wr/gemini` | Google Gemini ADK | `google_api_key` | Placeholder |
| `google-adk-wr/vertex` | Vertex AI ADK | `vertex_api_key` | Placeholder |

## User Stories / Flows

### Setting Up API Keys and Defaults

1. User opens User Settings → AI Services
2. User enters API keys for desired providers (Anthropic, MiniMax, OpenAI Compatible)
3. User optionally selects default SDK preferences for conversation and building modes
4. Preferences are saved; they will pre-populate SDK dropdowns on future environment creation

### Creating an Environment with a Custom SDK

1. User opens Add Environment dialog
2. Dialog pre-fills SDK dropdowns from user default preferences
3. User can override the SDK per mode (building / conversation)
4. If required credentials are missing, the create button is disabled with a warning
5. User confirms → backend validates credentials → environment is created with SDK fields recorded
6. Backend generates settings files inside the container for non-Anthropic SDKs

### Agent Runtime SDK Selection

1. Container starts; `SDK_ADAPTER_BUILDING` and `SDK_ADAPTER_CONVERSATION` env vars are set
2. SDK Manager reads the adapter ID for the current mode
3. Adapter checks for a settings file at the expected path
4. If found, settings are loaded and the SDK client is configured (base URL, auth token, model)
5. If not found, falls back to environment variable-based credentials (Anthropic default)

## Business Rules

- **SDK immutability:** `agent_sdk_conversation` and `agent_sdk_building` cannot be changed after environment creation
- **Credentials required:** Backend rejects environment creation if the user lacks the API key(s) required by the selected SDK
- **Default cascade:** If no SDK is provided on creation → use user's `default_sdk_*` fields → fall back to `claude-code/anthropic`
- **MiniMax conflict prevention:** When MiniMax is selected, `ANTHROPIC_API_KEY` is NOT written to `.env` to avoid SDK conflicts
- **OpenAI Compatible requires all three fields:** API key, base URL, and model name must all be set
- **Rebuild regeneration:** After an environment rebuild (core replacement), settings files are regenerated from stored credentials
- **Encrypted storage:** All AI credentials are stored as an encrypted JSON blob; adding new credential fields does not require a database migration

## Architecture Overview

```
User Settings (AI Keys + Default Prefs)
         │
         ▼
Add Environment Dialog (SDK dropdowns, credential validation)
         │
         ▼
Backend: environment_service.py (validate SDK, check keys, set defaults)
         │
         ▼
Backend: environment_lifecycle.py (generate .env + settings files per SDK)
         │
         ├── Anthropic → ANTHROPIC_API_KEY in .env
         ├── MiniMax   → .claude/building_settings.json
         │              .claude/conversation_settings.json
         └── OpenAI Compatible → .google-adk/building_settings.json
                                 .google-adk/conversation_settings.json
         │
         ▼
Agent Environment Container
  SDK_ADAPTER_BUILDING / SDK_ADAPTER_CONVERSATION (env vars)
         │
         ▼
sdk_manager.py → AdapterRegistry → ClaudeCodeAdapter | GoogleADKAdapter
         │
         ▼
Unified SDKEvent stream → Backend WebSocket → Frontend
```

## Integration Points

- **Agent Environments:** SDK fields are part of `AgentEnvironment` model; selection happens at environment creation — see [agent_environments](../agent_environments/agent_environments.md)
- **AI Credentials:** User-level credential storage and encryption — see [ai_credentials](../../application/ai_credentials/ai_credentials.md)
- **Agent Environment Core:** The `sdk_manager.py` and adapters live inside the environment core — see [agent_environment_core](../agent_environment_core/agent_environment_core.md)
- **Environment Data Management:** Rebuild flow must regenerate settings files — see [agent_environment_data_management](../agent_environment_data_management/agent_environment_data_management.md)
