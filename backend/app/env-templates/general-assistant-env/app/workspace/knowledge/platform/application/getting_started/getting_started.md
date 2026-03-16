# Getting Started

## Purpose

Guide new instances and first-time users through initial platform setup — configuring an AI provider key, understanding the two agent modes, and discovering supplementary features (knowledge sources, plugins, workspaces) that improve agent quality and organization.

## Core Concepts

- **Onboarding Gate** — Blocks the main Dashboard until the user has configured at least one AI credential. Without a valid API key the agent runtime cannot function, so setup is mandatory before any other feature is accessible
- **Getting Started Modal** — Encyclopedia-style dialog with four articles covering the essential patterns: quick-start workflow, building agents, sharing credentials, and the building/conversation mode distinction
- **Rotating Hints** — Persistent inline widget in the Dashboard and message input that surfaces contextual tips and re-opens the Getting Started Modal on demand
- **New Instance Setup** — When a platform instance is deployed for the first time, the first registered user follows the same onboarding gate flow. Admins may pre-configure plugin marketplaces or shared knowledge sources to make the experience richer for subsequent users

## User Stories / Flows

### First Login — No API Key

1. User signs up or logs in for the first time
2. Dashboard detects no AI credential configured (`has_anthropic_api_key: false`)
3. `ApiKeyOnboarding` screen is displayed instead of the normal dashboard
4. User enters their LLM provider API key (e.g., Anthropic API key)
5. Key is validated and saved via AI Credentials backend
6. `GettingStartedModal` opens automatically to introduce core concepts
7. User browses articles or dismisses the modal
8. Normal Dashboard is now accessible

### Getting Started Modal — Article Navigation

1. Modal opens showing four educational articles in a sidebar
2. Default article: **Gmail Agent Quick Start** — a visual 3-step flow (credentials → build → converse)
3. User navigates between articles via the sidebar:
   - **How to Build An Agent** — building mode prompt patterns, script creation, tips
   - **How to Share Credentials** — OAuth setup guide, credential types overview
   - **Conversation vs Building** — mode comparison, typical workflow steps
4. Articles contain cross-links enabling jump navigation between related topics
5. Modal dismissed via "Start Building" button or backdrop close

### Returning User — Hints Access

1. Dashboard and message input show a `RotatingHints` widget with cycling contextual tips
2. Tips rotate every 8 seconds with a fade transition
3. User clicks the widget to re-open `GettingStartedModal` at any time
4. Modal state is ephemeral — not persisted, accessible multiple times per session

### New Instance — Admin Preparation

1. Platform admin deploys a new instance
2. Admin registers plugin marketplaces (Git-based plugin catalogs) if specialised agent capabilities are needed
3. Admin or power users add knowledge sources pointing to documentation repositories for RAG-based agent assistance
4. First user registers and goes through the standard onboarding gate flow
5. They can organise their agents, credentials, and sessions into workspaces from the start

## Business Rules

- **Mandatory gate** — The Dashboard only renders if the credentials status check returns `has_anthropic_api_key: true`; the gate cannot be bypassed
- **One-time trigger** — `GettingStartedModal` opens automatically only once, immediately after successful API key submission (the `onComplete` callback)
- **Persistent re-access** — The modal can be re-opened any number of times via `RotatingHints` click; this is intentional and has no session limit
- **Non-persisted modal state** — Whether the user has seen the modal is not tracked server-side; the trigger is purely client-side (`showGettingStarted` boolean state)
- **Credential type agnostic** — The gate currently checks for `has_anthropic_api_key`, but the underlying credential system supports multiple provider types; see [AI Credentials](../ai_credentials/ai_credentials.md)
- **Onboarding scope** — The gate and modal cover the minimum viable setup. Knowledge sources, plugin marketplaces, and workspaces are supplementary and introduced after initial setup

## Architecture Overview

```
New User Login
    ↓
Dashboard (aiCredentialsStatus query)
    ↓
[has_anthropic_api_key = false]
    → ApiKeyOnboarding (full-screen gate)
        → User submits key
        → PATCH /api/v1/users/me/ai-credentials
        → onComplete callback
            → GettingStartedModal (auto-open)
            → invalidateQueries(["aiCredentialsStatus"])
    ↓
[has_anthropic_api_key = true]
    → Normal Dashboard
        → RotatingHints widget (always visible)
            → onClick → GettingStartedModal (on-demand)
```

## Integration Points

- **AI Credentials** — The onboarding gate is a direct consequence of the credentials status check. The `ApiKeyOnboarding` component saves credentials via the same endpoint as the full credentials management UI. First-time setup creates a named credential and marks it as default. See [AI Credentials](../ai_credentials/ai_credentials.md)
- **Knowledge Sources** — Users who want agents to answer questions about their own documentation can add a knowledge source after initial setup. Agents in building mode gain access to a RAG query tool that searches indexed articles. Recommended for more capable, context-aware agents. See [Knowledge Sources](../knowledge_sources/knowledge_sources.md)
- **Plugin Marketplaces** — When agents need specialised capabilities beyond the default toolset (e.g., domain-specific integrations), platform admins register Git-based plugin catalogs. Users can then discover and install plugins per-agent. New instance admins should configure this before users start building. See [Plugin Marketplaces](../plugin_marketplaces/plugin_marketplaces.md)
- **User Workspaces** — Once users have multiple agents and credentials, workspaces help separate concerns (e.g., "Email Automation" vs "Data Analysis"). Workspace assignment is available from the first agent creation and the active workspace is preserved across browser sessions. See [User Workspaces](../user_workspaces/user_workspaces.md)

---

*Last updated: 2026-03-03*
