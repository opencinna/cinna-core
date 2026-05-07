# Dashboard Env Pre-Configuration — Implementation Plan

**Feature name**: `dashboard-env-preconfig`
**Document path**: `docs/plans/dashboard_env_preconfig.md`
**Status**: Draft
**Date**: 2026-05-07

---

## Overview

The dashboard's "+ New Agent" pill currently exposes a small cog dropdown (`Environment SDK`) with only two SDK toggles per mode (Anthropic vs MiniMax via `claude-code` engine). The agent's Environments tab, by contrast, has a comprehensive env-creation form (`AddEnvironment.tsx`) that lets the user pick env template, SDK engine per mode, AI credential per mode, and a model override per mode.

This refactor unifies those two surfaces. The cog button on the dashboard is preserved, but its dropdown is replaced by a Modal Dialog that hosts the same form used on the Environments tab. The form is extracted into a shared `EnvironmentConfigForm` component so both surfaces consume identical UI and logic. New env-config values are forwarded from `/` to `/agent/creating` via URL search params, then submitted in the body of `POST /api/v1/agents/create-flow`. The backend `AgentCreateFlowRequest` is extended with optional fields (defaulting to today's behavior), passed through `AgentService.create_agent_flow` into the existing `AgentEnvironmentCreate` constructor.

Out of scope: changing `AgentEnvironmentCreate` (already has every field), changing `AddEnvironment.tsx` behavior (must still call `createAgentEnvironment` and produce identical outcomes), feature flags, backwards-compat shims.

### High-Level Flow

```
Dashboard ("/")                                        Wizard ("/agent/creating")             Backend
─────────────────────────────────────────              ────────────────────────────           ─────────────────────────
[+ New Agent] selected                                                                        
   |                                                                                           
   ├── Cog button → opens Dialog                                                               
   |     └── EnvironmentConfigForm (shared)                                                    
   |           - env template                                                                  
   |           - Conversation summary row → EnvModeEditDialog                                  
   |           - Building summary row    → EnvModeEditDialog                                   
   |                                                                                           
   ├── User clicks Send                                                                        
   |     └── handleSend() composes search params:                                              
   |           description, mode, fileIds, fileObjects,                                        
   |           sdkConversation, sdkBuilding,        ← legacy (kept)                            
   |           envName,                              ← new                                     
   |           modelOverrideConversation,            ← new                                     
   |           modelOverrideBuilding,                ← new                                     
   |           useDefaultAiCredentials,              ← new (string "true"/"false")             
   |           conversationAiCredentialId,           ← new (UUID string)                       
   |           buildingAiCredentialId                ← new (UUID string)                       
   |                                                                                           
   └── navigate("/agent/creating", { search })                                                 
                                              SearchParams parsed → POST /agents/create-flow   
                                              body: { ...legacy, env_name, model_override_*,
                                                       use_default_ai_credentials,
                                                       conversation_ai_credential_id,
                                                       building_ai_credential_id }
                                                                                          create_agent_flow()
                                                                                            └─ AgentEnvironmentCreate(
                                                                                                 env_name=...,
                                                                                                 agent_sdk_*,
                                                                                                 model_override_*,
                                                                                                 use_default_ai_credentials,
                                                                                                 *_ai_credential_id,
                                                                                               )
```

---

## Architectural Considerations

These constraints govern every phase below; cross-reference back to this section while implementing.

1. **Single source of truth for env-config form state.** The shared component must NOT own the canonical state silently. The dashboard surface must mutate URL search params from form state; the agent-tab surface must call `createAgentEnvironment` from form state. To keep both surfaces simple, the shared form takes a controlled-style props shape: `value` (current state) + `onChange` (full-state setter) + `aiCredentials` + `aiCredentialsStatus`. The seeding-from-defaults effect runs **inside** the shared component (driven by an `open` / `enabled` prop) so both surfaces get identical seed behavior without copy-paste.

2. **Snake_case at the request boundary, camelCase in the URL.** TanStack Router search params follow the existing route file conventions, which use camelCase (`sdkConversation`, `fileIds`). Backend request bodies use snake_case (`agent_sdk_conversation`, `env_name`). The mapping happens in `creating.tsx` when building the POST body. Do not leak snake_case into the URL nor camelCase into the request body.

3. **JSON-serializable search params.** TanStack Router search params end up in the URL as strings. Booleans and UUIDs survive round-tripping only because `validateSearch` coerces them. Plan:
   - Booleans: pass as `"true"` / `"false"` strings in the URL; coerce to `boolean` in `validateSearch`.
   - UUIDs: pass as raw strings; keep them as `string` in the `SearchParams` type (no UUID class). Backend pydantic will coerce to `uuid.UUID` from the JSON body.
   - Empty / undefined values: do NOT include the key when undefined (TanStack Router omits keys whose value is `undefined`). Avoid `""` / `"null"` placeholders.

4. **URL length budget.** Worst-case search-param payload now grows by ~6 fields plus possibly long `fileObjects` JSON. Keep all new values short (UUIDs ~36 chars, model names < 40 chars, env_name < 40 chars). No changes needed unless `fileObjects` already crowds the limit; this refactor adds at most ~250 chars.

5. **Sentinel handling.** `EnvironmentConfigForm` uses the `USE_DEFAULT_SENTINEL` (`"__default__"`) value internally for the credential select. When emitting state to the dashboard, normalize sentinel → `null` / `undefined` (so it's not pushed into search params). When seeding from `aiCredentialsStatus`, fall back to sentinel if no default is set (mirrors current `AddEnvironment.tsx` behavior at lines 322-329).

6. **Backwards compatibility — no shims.** New backend fields are all `Optional` with sensible defaults (`env_name=None` → service falls back to `settings.DEFAULT_AGENT_ENV_NAME`; `use_default_ai_credentials=True`; nullable credential IDs). Old callers passing only `agent_sdk_conversation` / `agent_sdk_building` continue to work unchanged. We do NOT introduce a separate code path; the new fields just thread into the same `AgentEnvironmentCreate` call.

7. **Form re-mount on re-open.** `AddEnvironment.tsx` resets state in `handleOpenChange` when the Dialog opens (lines 381-397). The dashboard Dialog must do the same so the user always sees a fresh form seeded from current account defaults. The shared component handles this internally by listening to its `open` prop.

8. **No agent-tab regressions.** `AddEnvironment.tsx` after the refactor must produce a byte-identical `AgentEnvironmentCreate` payload as before. The `handleSubmit` body (lines 348-379) stays in the agent-tab wrapper; only the field rendering moves to the shared component.

---

## Phase 1 — Frontend: Extract `EnvironmentConfigForm` shared component

**Goal**: Move the form body and helpers out of `AddEnvironment.tsx` into a reusable component without changing behavior.

### 1.1 Create new file `frontend/src/components/Environments/EnvironmentConfigForm.tsx`

Move the following symbols from `AddEnvironment.tsx` into this file (and re-export them):

- Constants: `ENV_TEMPLATE_OPTIONS`, `SDK_ENGINE_OPTIONS`, `SDK_CREDENTIAL_COMPATIBILITY`, `SUGGESTED_MODELS`, `DEFAULT_SDK_FOR_ENGINE`, `TYPE_DISPLAY_NAMES`, `USE_DEFAULT_SENTINEL`.
- Helpers: `composeSDKId`, `getCompatibleCredentials`, `extractEngine`, `getEngineLabel`.
- Sub-component: `EnvModeEditDialog` (the per-mode edit sub-dialog, currently lines 95-259 of `AddEnvironment.tsx`).
- Main form body: the env-template Select, the two summary rows for Conversation/Building, and the `EnvModeEditDialog` mounting.

### 1.2 Component shape

```ts
// State value shape (one struct, not 7 props)
export interface EnvConfigValue {
  envName: string
  sdkEngineConversation: string
  conversationCredentialId: string  // UUID string OR USE_DEFAULT_SENTINEL
  modelOverrideConversation: string
  sdkEngineBuilding: string
  buildingCredentialId: string      // UUID string OR USE_DEFAULT_SENTINEL
  modelOverrideBuilding: string
}

export interface EnvironmentConfigFormProps {
  value: EnvConfigValue
  onChange: (next: EnvConfigValue) => void
  // Drives the seeding-from-defaults effect; pass `open` from the parent Dialog
  // so the form re-seeds when re-opened.
  open: boolean
}
```

### 1.3 Internal data fetching

The form fetches its own dependencies (kept inside the shared component so consumers don't duplicate hooks):

- `UsersService.getAiCredentialsStatus()` → drives the seed effect.
- `AiCredentialsService.listAiCredentials()` → drives the credential select inside `EnvModeEditDialog` and the summary rows.
- `AiCredentialsService.resolveDefaultCredential({ sdkEngine })` per mode → resolves the "Default" summary label.

### 1.4 Seed effect

When `open` transitions to `true` AND `aiCredentialsStatus` is loaded, call `onChange` with a fresh value derived from `aiCredentialsStatus` (mirror lines 384-396 of current `AddEnvironment.tsx`):

- `envName` ← `"python-env-advanced"`
- `sdkEngineConversation` ← `extractEngine(status.default_sdk_conversation)` || `"claude-code"`
- `sdkEngineBuilding` ← `extractEngine(status.default_sdk_building)` || `"claude-code"`
- `conversationCredentialId` ← `status.default_ai_credential_conversation_id ?? USE_DEFAULT_SENTINEL`
- `buildingCredentialId` ← `status.default_ai_credential_building_id ?? USE_DEFAULT_SENTINEL`
- `modelOverrideConversation` ← `status.default_model_override_conversation ?? ""`
- `modelOverrideBuilding` ← `status.default_model_override_building ?? ""`

### 1.5 Re-export public API

Export from `EnvironmentConfigForm.tsx` for downstream callers:

- `EnvironmentConfigForm` (default export or named).
- `EnvConfigValue` type.
- `composeSDKId`, `USE_DEFAULT_SENTINEL`, `extractEngine` (used by dashboard `handleSend`).
- `ENV_TEMPLATE_OPTIONS` (in case any other surface needs it).

### 1.6 Files touched in this phase

- **Create**: `frontend/src/components/Environments/EnvironmentConfigForm.tsx`
- **Modify**: `frontend/src/components/Environments/AddEnvironment.tsx`

---

## Phase 2 — Frontend: Refactor `AddEnvironment.tsx` to consume shared component

**Goal**: Replace the in-line form body with `<EnvironmentConfigForm />` while keeping the outer Dialog, the create mutation, and the submit logic identical.

### 2.1 Changes in `AddEnvironment.tsx`

- Remove all moved constants and helpers (now imported from `EnvironmentConfigForm.tsx`).
- Remove the `EnvModeEditDialog` definition (now in shared file).
- Replace the seven individual `useState` calls for env config (lines 269-278) with a single `useState<EnvConfigValue>(initialEnvConfig)`. Provide a sensible initial that matches today's defaults; the shared form will overwrite it when the Dialog opens.
- Replace the inner `<div className="grid gap-4 py-4">…</div>` block (env template Select + the two summary rows + `<EnvModeEditDialog>` mount) with `<EnvironmentConfigForm value={envConfig} onChange={setEnvConfig} open={open} />`.
- Keep:
  - Outer `<Dialog>` + `<DialogTrigger>` (the "Add Environment" button on the agent tab).
  - `<DialogHeader>` with title "Create New Environment" / description.
  - `<DialogFooter>` with Cancel + Create buttons.
  - `createMutation` and `handleSubmit` — but read fields off the new `envConfig` struct (`envConfig.envName`, etc.) instead of seven individual states.

### 2.2 `handleSubmit` continuity

After the refactor, `handleSubmit` (current lines 348-379) reads from `envConfig`:

```text
convIsDefault   = envConfig.conversationCredentialId === USE_DEFAULT_SENTINEL
buildIsDefault  = envConfig.buildingCredentialId    === USE_DEFAULT_SENTINEL
selectedConvCred  = allCredentials.find(c => c.id === envConfig.conversationCredentialId) ?? null
selectedBuildCred = allCredentials.find(c => c.id === envConfig.buildingCredentialId)    ?? null
sdkConversation   = composeSDKId(envConfig.sdkEngineConversation, convIsDefault ? null : selectedConvCred)
sdkBuilding       = composeSDKId(envConfig.sdkEngineBuilding,    buildIsDefault ? null : selectedBuildCred)
useDefaultForAll  = convIsDefault && buildIsDefault
```

Mutation payload remains byte-identical to today.

### 2.3 Files touched

- **Modify**: `frontend/src/components/Environments/AddEnvironment.tsx`

---

## Phase 3 — Frontend: Dashboard refactor (`/`)

**Goal**: Replace the cog `DropdownMenu` with a Modal Dialog that hosts `<EnvironmentConfigForm />`, and wire the new state into the existing `handleSend` → search-param navigation.

### 3.1 Imports / cleanup in `frontend/src/routes/_layout/index.tsx`

- Remove imports: `DropdownMenu`, `DropdownMenuContent`, `DropdownMenuItem`, `DropdownMenuTrigger` (lines 19-23) — no other usage in this file (verify; remove only the unused ones).
- Add imports: `Dialog`, `DialogContent`, `DialogHeader`, `DialogTitle`, `DialogFooter`, `DialogTrigger` from `@/components/ui/dialog`.
- Add imports: `EnvironmentConfigForm`, `EnvConfigValue`, `composeSDKId`, `USE_DEFAULT_SENTINEL`, `extractEngine` from `@/components/Environments/EnvironmentConfigForm`.
- Add import: `AiCredentialsService` from `@/client` (used to resolve the selected credential objects, mirroring `AddEnvironment.tsx` lines 291-294).
- Remove the `SDK_OPTIONS` const (lines 59-62).

### 3.2 State changes

- Remove: `showSdkConfig`, `sdkConversation`, `sdkBuilding` states (lines 76, 78-79).
- Remove: `getKeyStatus` helper (lines 100-104) unless still used; verify and delete.
- Add:
  - `const [envConfigOpen, setEnvConfigOpen] = useState(false)` — controls the Dialog.
  - `const [envConfig, setEnvConfig] = useState<EnvConfigValue>(INITIAL_ENV_CONFIG)` — module-level constant matching the agent-tab initial.
- Add a query (mirror `AddEnvironment.tsx` lines 291-294) to fetch `AiCredentialsService.listAiCredentials()` so `handleSend` can resolve the selected credential objects for `composeSDKId`.

### 3.3 UI changes

Replace the `selectedAgentId === NEW_AGENT_ID` branch (lines 631-696, the `DropdownMenu` block) with:

```text
<Dialog open={envConfigOpen} onOpenChange={setEnvConfigOpen}>
  <DialogTrigger asChild>
    <button type="button" className={...same gradient/active styles, keyed off envConfigOpen instead of showSdkConfig...}>
      <Settings className="h-5 w-5" />
    </button>
  </DialogTrigger>
  <DialogContent className="sm:max-w-[540px]">
    <DialogHeader>
      <DialogTitle>Configure Environment</DialogTitle>
      <DialogDescription>
        Pre-configure the environment your new agent will be created with.
      </DialogDescription>
    </DialogHeader>
    <EnvironmentConfigForm value={envConfig} onChange={setEnvConfig} open={envConfigOpen} />
    <DialogFooter>
      <Button variant="outline" onClick={() => setEnvConfigOpen(false)}>Done</Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

Notes:
- The Dialog is purely a "configure" surface — there's no submit; Done just closes. Actual creation happens when the user hits Send on the dashboard.
- Keep the `else` branch (mode switch for regular agents) untouched.
- Remove `setShowSdkConfig(false)` references in `handleAgentClick` (line 413); replace with `setEnvConfigOpen(false)` if a similar reset is desired when the user clicks a different agent (recommended).

### 3.4 `handleSend` rewrite (the `selectedAgentId === NEW_AGENT_ID` branch, lines 306-322)

Compose the SDK IDs and build the search-param object:

```text
const allCredentials = aiCredentials?.data ?? []
const convIsDefault   = envConfig.conversationCredentialId === USE_DEFAULT_SENTINEL
const buildIsDefault  = envConfig.buildingCredentialId    === USE_DEFAULT_SENTINEL
const selConv  = allCredentials.find(c => c.id === envConfig.conversationCredentialId) ?? null
const selBuild = allCredentials.find(c => c.id === envConfig.buildingCredentialId)    ?? null
const sdkConversation = composeSDKId(envConfig.sdkEngineConversation, convIsDefault ? null : selConv)
const sdkBuilding     = composeSDKId(envConfig.sdkEngineBuilding,    buildIsDefault ? null : selBuild)
const useDefaultForAll = convIsDefault && buildIsDefault

navigate({
  to: "/agent/creating",
  search: {
    description: trimmedMessage,
    mode,
    sdkConversation,
    sdkBuilding,
    envName: envConfig.envName,
    modelOverrideConversation: envConfig.modelOverrideConversation.trim() || undefined,
    modelOverrideBuilding:     envConfig.modelOverrideBuilding.trim() || undefined,
    useDefaultAiCredentials:   useDefaultForAll,                          // boolean → coerced in validateSearch
    conversationAiCredentialId: useDefaultForAll || convIsDefault ? undefined : envConfig.conversationCredentialId,
    buildingAiCredentialId:    useDefaultForAll || buildIsDefault ? undefined : envConfig.buildingCredentialId,
    fileIds:     attachedFiles.length > 0 ? attachedFiles.map(f => f.id).join(',') : undefined,
    fileObjects: attachedFiles.length > 0 ? JSON.stringify(attachedFiles)          : undefined,
  },
})
```

Undefined values are omitted by TanStack Router from the URL.

### 3.5 Files touched

- **Modify**: `frontend/src/routes/_layout/index.tsx`

---

## Phase 4 — Frontend: Wizard route (`/agent/creating`)

**Goal**: Accept the new search params, validate/coerce them, and forward them in the POST body to `/api/v1/agents/create-flow`.

### 4.1 `SearchParams` type and `validateSearch` (lines 8-29 of `frontend/src/routes/_layout/agent/creating.tsx`)

Extend the type:

```ts
type SearchParams = {
  description: string
  mode: "conversation" | "building"
  sdkConversation?: string
  sdkBuilding?: string
  envName?: string
  modelOverrideConversation?: string
  modelOverrideBuilding?: string
  useDefaultAiCredentials?: boolean
  conversationAiCredentialId?: string  // UUID string
  buildingAiCredentialId?: string      // UUID string
  fileIds?: string
  fileObjects?: string
}
```

Update `validateSearch` to coerce:
- Strings: same pattern as today (`(search.x as string) || undefined`).
- `useDefaultAiCredentials`: parse `search.useDefaultAiCredentials` as boolean — accept native `true`/`false`, the strings `"true"` / `"false"`; default to `undefined` if absent. Do NOT default to `true` in the search-param coercion — let absence mean "not specified" so the backend default (`True`) governs.

### 4.2 POST body update (lines 105-119)

Destructure the new search params via `Route.useSearch()` and add to the JSON body, mapping camelCase → snake_case:

```text
body: JSON.stringify({
  description,
  mode,
  auto_create_session: false,
  user_workspace_id: workspaceFilter || undefined,
  agent_sdk_conversation: sdkConversation || undefined,
  agent_sdk_building:     sdkBuilding     || undefined,
  env_name: envName || undefined,
  model_override_conversation: modelOverrideConversation || undefined,
  model_override_building:     modelOverrideBuilding     || undefined,
  use_default_ai_credentials:  useDefaultAiCredentials,                 // omitted when undefined
  conversation_ai_credential_id: conversationAiCredentialId || undefined,
  building_ai_credential_id:     buildingAiCredentialId     || undefined,
}),
```

Pydantic on the backend coerces UUID strings to `uuid.UUID`. Any malformed UUID will return a 422; the existing error-parsing block (lines 121-145) already handles 422 cleanly.

### 4.3 Files touched

- **Modify**: `frontend/src/routes/_layout/agent/creating.tsx`

---

## Phase 5 — Backend: Extend `AgentCreateFlowRequest` model

**Goal**: Add the new optional fields with backend-side defaults that preserve today's behavior when omitted.

### 5.1 `backend/app/models/agents/agent.py` (around line 220)

Add to `AgentCreateFlowRequest`:

```text
env_name: str | None = None
model_override_conversation: str | None = None
model_override_building:     str | None = None
use_default_ai_credentials: bool = True
conversation_ai_credential_id: uuid.UUID | None = None
building_ai_credential_id:     uuid.UUID | None = None
```

Notes:
- `env_name=None` → service falls back to `settings.DEFAULT_AGENT_ENV_NAME`.
- `use_default_ai_credentials` defaults to `True` (matches `AgentEnvironmentCreate` default).
- The model is `SQLModel` (not `table=True`); no migration needed.

### 5.2 Files touched

- **Modify**: `backend/app/models/agents/agent.py`

---

## Phase 6 — Backend: Extend `AgentService.create_agent_flow`

**Goal**: Accept the new optional parameters and pass them into `AgentEnvironmentCreate` (which already supports them).

### 6.1 Signature changes — `backend/app/services/agents/agent_service.py` (line ~454)

Append the new optional kwargs:

```text
async def create_agent_flow(
    session: Session,
    user: User,
    description: str,
    mode: str,
    auto_create_session: bool = False,
    user_workspace_id: UUID | None = None,
    agent_sdk_conversation: str | None = None,
    agent_sdk_building:     str | None = None,
    env_name: str | None = None,
    model_override_conversation: str | None = None,
    model_override_building:     str | None = None,
    use_default_ai_credentials: bool = True,
    conversation_ai_credential_id: UUID | None = None,
    building_ai_credential_id:     UUID | None = None,
):
```

### 6.2 `AgentEnvironmentCreate` construction (line ~541)

Replace the existing `AgentEnvironmentCreate(...)` call with one that threads through the new fields:

```text
default_env_data = AgentEnvironmentCreate(
    env_name=env_name or settings.DEFAULT_AGENT_ENV_NAME,
    env_version=settings.DEFAULT_AGENT_ENV_VERSION,
    instance_name="Default",
    type="docker",
    config={},
    agent_sdk_conversation=agent_sdk_conversation,
    agent_sdk_building=agent_sdk_building,
    model_override_conversation=model_override_conversation,
    model_override_building=model_override_building,
    use_default_ai_credentials=use_default_ai_credentials,
    conversation_ai_credential_id=conversation_ai_credential_id,
    building_ai_credential_id=building_ai_credential_id,
)
```

`env_name` keeps the `or settings.DEFAULT_AGENT_ENV_NAME` fallback so a `None` from the request still produces today's default (`python-env-advanced`).

### 6.3 Files touched

- **Modify**: `backend/app/services/agents/agent_service.py`

---

## Phase 7 — Backend: Forward new fields in the route

**Goal**: Pass the new request fields through to the service.

### 7.1 `backend/app/api/routes/agents.py` (`create_agent_with_flow`, line ~158)

Update the inner `event_generator` (lines 168-179) to forward the new request fields:

```text
async for event in AgentService.create_agent_flow(
    session=session,
    user=current_user,
    description=request.description,
    mode=request.mode,
    auto_create_session=request.auto_create_session,
    user_workspace_id=request.user_workspace_id,
    agent_sdk_conversation=request.agent_sdk_conversation,
    agent_sdk_building=request.agent_sdk_building,
    env_name=request.env_name,
    model_override_conversation=request.model_override_conversation,
    model_override_building=request.model_override_building,
    use_default_ai_credentials=request.use_default_ai_credentials,
    conversation_ai_credential_id=request.conversation_ai_credential_id,
    building_ai_credential_id=request.building_ai_credential_id,
):
    yield f"data: {json.dumps(event)}\n\n"
```

### 7.2 Files touched

- **Modify**: `backend/app/api/routes/agents.py`

---

## Phase 8 — Client regeneration

After Phases 5-7 land, regenerate the frontend OpenAPI client so TypeScript sees the new request fields (and any callers that use the typed `AgentCreateFlowRequest` shape get them).

```text
source ./backend/.venv/bin/activate && make gen-client
```

The dashboard and `creating.tsx` use `fetch` directly (not the generated client) for the SSE call, so the regeneration is mostly defensive — but it keeps `frontend/src/client/types.gen.ts` and `schemas.gen.ts` in sync and prevents future drift.

### Files touched

- **Regenerated**: `frontend/src/client/sdk.gen.ts`, `frontend/src/client/types.gen.ts`, `frontend/src/client/schemas.gen.ts`, `frontend/openapi.json`.

---

## Phase 9 — Backend tests

**Goal**: Cover the new field plumbing in `POST /agents/create-flow`.

### 9.1 Read first

Before writing tests, read:
- `backend/tests/README.md` — overall test architecture (API-only, scenario-based, no direct DB access).
- `backend/tests/api/agents/README.md` — agent-specific autouse fixtures (`patch_create_session`, `patch_environment_creation`, `background_tasks`, etc.). The `patch_environment_creation` fixture stubs `EnvironmentService.create_environment` with `stub_create_environment`, which records the `AgentEnvironmentCreate` it received — this is the assertion target.

### 9.2 New test file

**Create**: `backend/tests/api/agents/agents_create_flow_test.py`

Style: follow other agent tests in this directory (e.g., `agents_general_assistant_test.py`, `agents_email_integration_test.py`) for fixture usage and SSE-stream consumption patterns.

### 9.3 Test cases (descriptions only — do not write code now)

The endpoint streams SSE; tests should consume the stream, then assert on the captured `AgentEnvironmentCreate` from the environment stub.

1. **Default call still works (regression)** — POST with only `description`, `mode`, `auto_create_session=false`. Assert: stream completes, an environment was "created", captured `AgentEnvironmentCreate.env_name == settings.DEFAULT_AGENT_ENV_NAME`, `model_override_*` are `None`, `use_default_ai_credentials is True`, both credential IDs are `None`.

2. **`env_name` override propagates** — POST with `env_name="general-env"`. Assert captured `AgentEnvironmentCreate.env_name == "general-env"`.

3. **`model_override_conversation` and `model_override_building` propagate** — POST with both overrides set (e.g., `"claude-haiku-4-5"`, `"claude-opus-4"`). Assert captured fields equal the request values.

4. **Explicit credentials propagate when `use_default_ai_credentials=false`** — Pre-create two `AICredential` rows for the test user via the existing AI credentials route helper (or fixture). POST with `use_default_ai_credentials=false`, `conversation_ai_credential_id=<uuid>`, `building_ai_credential_id=<uuid>`. Assert captured `AgentEnvironmentCreate` has `use_default_ai_credentials is False` and both credential IDs match the posted UUIDs.

5. **422 on malformed UUID** — POST with `conversation_ai_credential_id="not-a-uuid"`. Assert HTTP 422 (Pydantic validation). Optional but useful for catching the URL-roundtrip path.

### 9.4 Implementation notes

- Use `superuser_token_headers` (developer / admin restricted endpoint).
- Reuse the existing `patch_environment_creation` autouse fixture to capture the `AgentEnvironmentCreate` payload. If `stub_create_environment` doesn't already retain the input, add a per-test wrapper that does, or assert via the row written to the DB (which uses the same field names since `AgentEnvironment.model_validate(data, ...)`).
- Consume the SSE stream with `client.stream("POST", ...)` or by reading the response iter; treat it as a list of `data: {...}` chunks.

### 9.5 Files touched

- **Create**: `backend/tests/api/agents/agents_create_flow_test.py`

---

## Phase 10 — Docs update

**Goal**: Bring `docs/application/agent_management/new_agent_wizard.md` in sync with the new dashboard UI and the extended request shape.

### 10.1 Sections to update

- **`## SDK Pre-Configuration` → `### Dashboard SDK Selector`** (lines 31-56): replace the description of the cog dropdown with the Modal-Dialog full env-config form. Mention:
  - Cog button now opens a Dialog (not a dropdown).
  - Dialog body is the shared `EnvironmentConfigForm` component (also used by `AddEnvironment.tsx` on the agent's Environments tab).
  - Form contents: env template, per-mode Conversation/Building summary rows that open `EnvModeEditDialog` for SDK engine + AI credential + model override.
- **`### Search Params`** (lines 58-65): list the new params: `envName`, `modelOverrideConversation`, `modelOverrideBuilding`, `useDefaultAiCredentials`, `conversationAiCredentialId`, `buildingAiCredentialId`. Note the camelCase → snake_case mapping at the request boundary.
- **`## Backend Components` → `**Agent Model:**`** (lines 67-72): expand the `AgentCreateFlowRequest` field list with the six new fields.
- **`### Frontend Components` → `**Dashboard:**`** (lines 95-99): update to reference the Dialog + shared `EnvironmentConfigForm` instead of the dropdown.
- **`## File Locations Reference`** (line 295+): add `frontend/src/components/Environments/EnvironmentConfigForm.tsx` as the shared form location.

Note: the user-memory `feedback_docs_readme_no_counters` rule is about counts in the README Domain Map; not relevant to this doc edit.

### 10.2 Files touched

- **Modify**: `docs/application/agent_management/new_agent_wizard.md`

---

## Risks and Open Questions

1. **Sentinel leakage into URL.** If the `handleSend` mapping accidentally treats the sentinel string as a real UUID, it lands in the search params and then in the request body, and Pydantic 422s. Mitigation: explicit `convIsDefault` / `buildIsDefault` checks before assigning to the search-param object (Phase 3.4).

2. **Boolean coercion in `validateSearch`.** TanStack Router stores values as parsed JSON when navigated programmatically with object-typed `search`, but as strings when the URL is reloaded. `validateSearch` must accept both `true` (boolean) and `"true"` (string). Test the page-reload path explicitly.

3. **Default-seeding overwriting user edits.** The shared form re-seeds when `open` flips from false → true. If the dashboard user opens the Dialog, edits values, closes it (Done), then re-opens, today's `AddEnvironment.tsx` re-seeds from `aiCredentialsStatus` and overwrites the previous edits. That's the same behavior we're shipping — but it's a UX choice worth flagging. If preservation is desired, gate seeding on "first-open since mount" instead of every open. Recommend matching `AddEnvironment.tsx` behavior for consistency; revisit if user feedback indicates otherwise.

4. **URL length.** Worst case adds ~250 chars (3 UUIDs × ~36 + model-name strings + boolean + env_name). Combined with `fileObjects` JSON, this could push toward common URL caps (~2000 chars). If `fileObjects` is large, the existing path is already at risk; this refactor doesn't materially worsen it. No action unless a real failure surfaces.

5. **AI credential ownership across workspace switch.** A pre-selected credential UUID from the dashboard might no longer be valid if the user switches workspace between opening the form and clicking Send. Backend `AgentEnvironmentCreate` validation already covers this; the frontend will surface the 422. Acceptable for v1.

6. **`getKeyStatus` removal.** Removing the `getKeyStatus` helper and the `*` warning marker on the dashboard is a small UX regression — the new Dialog form does NOT show "API key not configured" warnings on the SDK select. The agent-tab form doesn't show those today either (it relies on the credential select being empty when there are no compatible credentials). Acceptable to drop for parity. Flag in the PR description.

7. **`AddEnvironment.tsx` initial state vs. seeded state.** The shared form's `open=true` seed effect runs on mount when `open` is initially true. The agent-tab Dialog opens on user action (`open=false` initially), so seeding fires correctly. The dashboard Dialog also starts closed. If any future caller mounts the form with `open=true` from the start, ensure the seed effect still runs (use a `useEffect` that depends on `open && status`).

8. **Stub-based test capture.** `stub_create_environment` may need a small extension to retain the `AgentEnvironmentCreate` it received. Verify in `tests/stubs/environment_stub.py` before relying on it; if absent, add a list attribute (`captured_creates`) and assert against it.

---

## Summary Checklist

### Frontend tasks
- [ ] Create `frontend/src/components/Environments/EnvironmentConfigForm.tsx` — extract constants, helpers, `EnvModeEditDialog`, env-template + summary rows; controlled-style props (`value`, `onChange`, `open`); internal seeding from `aiCredentialsStatus`.
- [ ] Refactor `frontend/src/components/Environments/AddEnvironment.tsx` to consume `<EnvironmentConfigForm />`; consolidate seven env-config states into one `EnvConfigValue`; keep `createMutation` and submit payload byte-identical.
- [ ] Refactor `frontend/src/routes/_layout/index.tsx`:
  - [ ] Remove `SDK_OPTIONS`, `showSdkConfig`, `sdkConversation`, `sdkBuilding`, `getKeyStatus`, `DropdownMenu` imports/usage.
  - [ ] Add `envConfigOpen` + `envConfig` state and `aiCredentials` query.
  - [ ] Replace cog `DropdownMenu` block (lines 631-696) with a `<Dialog>` hosting `<EnvironmentConfigForm />`.
  - [ ] Rewrite `handleSend` to compose SDK IDs and forward all env-config fields as URL search params (camelCase, undefined-omitted).
  - [ ] Reset `envConfigOpen` in `handleAgentClick` when leaving the New Agent pill.
- [ ] Extend `frontend/src/routes/_layout/agent/creating.tsx`:
  - [ ] Add `envName`, `modelOverrideConversation`, `modelOverrideBuilding`, `useDefaultAiCredentials`, `conversationAiCredentialId`, `buildingAiCredentialId` to `SearchParams` and `validateSearch` (coerce boolean from `"true"`/`"false"`).
  - [ ] Forward all new params (mapped to snake_case) in the `POST /api/v1/agents/create-flow` body.

### Backend tasks
- [ ] Extend `AgentCreateFlowRequest` in `backend/app/models/agents/agent.py` with `env_name`, `model_override_conversation`, `model_override_building`, `use_default_ai_credentials`, `conversation_ai_credential_id`, `building_ai_credential_id` (all optional with sensible defaults).
- [ ] Extend `AgentService.create_agent_flow` in `backend/app/services/agents/agent_service.py` to accept the new kwargs and thread them through into the existing `AgentEnvironmentCreate(...)` call (line ~541), preserving the `settings.DEFAULT_AGENT_ENV_NAME` fallback when `env_name is None`.
- [ ] Forward the new request fields in `create_agent_with_flow` route in `backend/app/api/routes/agents.py` (line ~158).
- [ ] Run `source ./backend/.venv/bin/activate && make gen-client` to regenerate the OpenAPI TypeScript client.

### Testing tasks
- [ ] Read `backend/tests/README.md` and `backend/tests/api/agents/README.md` first.
- [ ] Verify `tests/stubs/environment_stub.py` retains the `AgentEnvironmentCreate` payload; add a capture attribute if missing.
- [ ] Create `backend/tests/api/agents/agents_create_flow_test.py` covering: legacy minimal call (regression), `env_name` override, `model_override_*` propagation, explicit credential IDs with `use_default_ai_credentials=false`, optional 422 on malformed UUID.

### Docs tasks
- [ ] Update `docs/application/agent_management/new_agent_wizard.md`:
  - [ ] Replace "Dashboard SDK Selector" subsection with Modal-Dialog + shared `EnvironmentConfigForm` description.
  - [ ] Update "Search Params" subsection with the six new params and camelCase→snake_case note.
  - [ ] Expand `AgentCreateFlowRequest` field list under "Backend Components".
  - [ ] Add `frontend/src/components/Environments/EnvironmentConfigForm.tsx` to "File Locations Reference".

### Validation
- [ ] Manual smoke: dashboard `+ New Agent` → cog opens Dialog → configure non-default env_name + model overrides + explicit credentials → Done → Send → wizard SSE creates an environment with the chosen settings (verify via Settings → Environments tab on the new agent).
- [ ] Manual smoke: agent's Environments tab `Add Environment` still works identically (no regression).
- [ ] Manual smoke: dashboard with no env-config customization still produces `python-env-advanced` env with default credentials (regression).
- [ ] Run `cd frontend && npx tsc --noEmit 2>&1 | grep -E "(EnvironmentConfigForm|AddEnvironment|index|creating)"` to confirm no TS errors in touched files.
- [ ] Run `make test-backend` after the new test file is in place (or scope to `tests/api/agents/agents_create_flow_test.py`).
