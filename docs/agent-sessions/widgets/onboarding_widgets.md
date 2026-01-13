# Onboarding Widgets

## Purpose

Guide new users through initial platform setup (API key configuration) and introduce core concepts (agent building, credentials, session modes) via an encyclopedia-style modal.

## Feature Overview

1. User logs in for the first time without Anthropic API key configured
2. Dashboard displays `ApiKeyOnboarding` screen instead of normal content
3. User enters API key, which is saved via backend endpoint
4. On success, `GettingStartedModal` appears with introductory articles
5. User can browse articles or dismiss modal to start using the platform

## Architecture

```
Dashboard (checks credentials status)
    ↓
[No API Key] → ApiKeyOnboarding → Backend API (save key) → GettingStartedModal
    ↓
[Has API Key] → Normal Dashboard
```

## User Flow States

**ApiKeyOnboarding States:**
- `idle` - Input field empty, button hidden
- `ready` - Input has value, button visible with animation
- `pending` - Mutation in progress, input/button disabled
- `success` - Checkmark shown, brief delay before transition

**GettingStartedModal States:**
- `open` - Modal visible with article content
- `closed` - Modal dismissed, normal dashboard visible

**Trigger Logic:**
- Modal appears when `onComplete` callback fires from `ApiKeyOnboarding` (first-time setup)
- Modal also opens when user clicks on `RotatingHints` widget (anytime access)
- State managed via `showGettingStarted` boolean in Dashboard and MessageInput
- Not persisted - can be opened multiple times per session via RotatingHints

## Backend Implementation

### Routes

**AI Credentials Status:**
- `GET /api/v1/users/me/ai-credentials/status` - Check if user has API key configured
- `backend/app/api/routes/users.py:get_ai_credentials_status()`
- Returns: `UserPublicWithAICredentials` with `has_anthropic_api_key` boolean

**AI Credentials Update:**
- `PATCH /api/v1/users/me/ai-credentials` - Save/update API key
- `backend/app/api/routes/users.py:update_ai_credentials()`
- Accepts: `AIServiceCredentialsUpdate` with optional `anthropic_api_key`

### Models

- `backend/app/models/user.py` - User model with `ai_credentials_encrypted` field
- `backend/app/crud.py:get_user_ai_credentials()` - Decrypt and return credentials
- `backend/app/crud.py:update_user_ai_credentials()` - Encrypt and save credentials

### Database

- Migration: `backend/app/alembic/versions/998040e1ade4_add_ai_credentials_encrypted_to_users.py`
- Field: `ai_credentials_encrypted` (encrypted JSON blob)

## Frontend Implementation

### Components

**ApiKeyOnboarding:** `frontend/src/components/Onboarding/ApiKeyOnboarding.tsx`
- Full-screen welcome component for first-time users
- Password input for API key with animated save button
- Links to Claude API Settings for key generation
- Calls `UsersService.updateAiCredentials()` on submit
- Invokes `onComplete` callback after successful save

**GettingStartedModal:** `frontend/src/components/Onboarding/GettingStartedModal.tsx`
- Encyclopedia-style dialog with sidebar navigation
- Four articles with inter-article navigation support
- Left sidebar: Table of contents with article icons
- Right content: Article content with formatted sections

**RotatingHints:** `frontend/src/components/Common/RotatingHints.tsx`
- Displays rotating tips/hints with fade animation
- Clickable - opens GettingStartedModal on click
- Used in Dashboard and MessageInput components
- Shuffles hints on mount, rotates every 8 seconds

### Articles Configuration

Articles defined in `GettingStartedModal.tsx:articles[]`:

1. **Gmail Agent Quick Start** (`gmail-quickstart`) - *Default article*
   - Visual 3-step flow with clickable cards
   - Step 1: Prepare credentials (links to `share-credentials`)
   - Step 2: Create agent & share credentials (links to `build-agent`)
   - Step 3: Start conversation (links to `conversation-vs-building`)
   - Color-coded steps with gradient connectors

2. **How to Build An Agent** (`build-agent`)
   - Writing build prompts with example
   - How Building mode creates scripts
   - Tips for better agents
   - Clickable callout linking to credentials article

3. **How to Share Credentials** (`share-credentials`)
   - Why credentials matter for external services
   - Step-by-step Gmail OAuth setup guide
   - Other credential types overview

4. **Conversation vs Building** (`conversation-vs-building`)
   - Two modes explanation with visual toggle representations
   - Building mode: setup, powerful model, code generation
   - Conversation mode: daily use, faster model, task execution
   - Typical workflow steps

### Dashboard Integration

**Location:** `frontend/src/routes/_layout/index.tsx`

**State Management:**
- `showGettingStarted` state controls modal visibility
- Query key `["aiCredentialsStatus"]` for credentials check
- Conditional rendering based on `hasAnthropicKey`

**Flow:**
```
useQuery(["aiCredentialsStatus"]) → credentialsStatus
    ↓
hasAnthropicKey = false → render ApiKeyOnboarding
    ↓
onComplete → setShowGettingStarted(true) + invalidateQueries
    ↓
hasAnthropicKey = true → render Dashboard + GettingStartedModal
```

### Services Used

- `frontend/src/client/sdk.gen.ts:UsersService.getAiCredentialsStatus()`
- `frontend/src/client/sdk.gen.ts:UsersService.updateAiCredentials()`

## Key Integration Points

**Dashboard ↔ ApiKeyOnboarding:**
- Dashboard conditionally renders based on `hasAnthropicKey`
- `onComplete` callback triggers modal display and query invalidation

**ApiKeyOnboarding ↔ Backend:**
- Mutation calls `PATCH /api/v1/users/me/ai-credentials`
- Query key invalidation triggers re-fetch of credentials status

**RotatingHints ↔ GettingStartedModal:**
- RotatingHints accepts `onClick` prop to trigger modal
- Dashboard passes `onClick={() => setShowGettingStarted(true)}`
- MessageInput manages its own `showGettingStarted` state and modal instance

**GettingStartedModal ↔ Articles:**
- `setSelectedArticle` passed to article content functions
- Enables clickable cross-references between articles

## UI/UX Details

**ApiKeyOnboarding:**
- Gradient icon with animated sparkles
- Password-masked input field
- Button animates in when input has value
- Success state shows checkmark, 1.2s delay before transition
- External link to Claude API Settings

**GettingStartedModal:**
- 800px max width, 80vh max height
- Left sidebar (224px) with violet accent colors
- Articles show chevron indicator when selected
- "Start Building" button with gradient styling
- Credentials callout uses amber accent for emphasis

**RotatingHints:**
- Renders as clickable button element
- Text darkens on hover to indicate interactivity
- 600ms fade transition between hints
- 8 second rotation interval (configurable)
- Hints shuffled on component mount for variety

## File Locations Reference

### Backend
- `backend/app/api/routes/users.py` - AI credentials endpoints
- `backend/app/models/user.py` - User model with credentials field
- `backend/app/crud.py` - Credentials CRUD operations

### Frontend
- `frontend/src/components/Onboarding/ApiKeyOnboarding.tsx` - API key input screen
- `frontend/src/components/Onboarding/GettingStartedModal.tsx` - Encyclopedia modal
- `frontend/src/components/Common/RotatingHints.tsx` - Clickable rotating hints widget
- `frontend/src/components/Chat/MessageInput.tsx` - Chat input with RotatingHints integration
- `frontend/src/routes/_layout/index.tsx` - Dashboard with onboarding integration

### Related Documentation
- `docs/agent-sessions/agent_env_building_prompt.md` - Building mode concepts
- `docs/agent-sessions/business_logic.md` - Session modes overview

---

**Document Version:** 1.2
**Last Updated:** 2026-01-13
**Status:** Complete
