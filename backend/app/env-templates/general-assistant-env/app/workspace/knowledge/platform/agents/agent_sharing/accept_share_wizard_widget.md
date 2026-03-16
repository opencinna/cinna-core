# Accept Share Wizard Widget

## Widget Purpose

Multi-step dialog wizard that guides a recipient through accepting a shared agent. Handles credential configuration (integration and AI), displays agent/share details, and submits the accept request that triggers clone creation.

## User Flow

1. Recipient sees a pending share card on the Agents page (`PendingAgentCard`)
2. Clicks "Accept" — wizard dialog opens with a numbered step indicator
3. **Step: Overview** — reviews agent name, description, sharer info, and share mode (User/Builder permissions)
4. **Step: AI Credentials** *(conditional)* — selects or confirms AI credentials for conversation/building if owner did not provide them
5. **Step: Integration Credentials** *(conditional)* — configures integration credentials required by the agent; shareable ones default to "use shared from owner", private ones require user selection or creation
6. **Step: Confirm** — reviews a credential summary before submitting
7. Wizard calls `AgentSharesService.acceptShare()` → clone agent created in background → wizard closes → agents list refreshes

## Dynamic Step Configuration

The wizard shows 2–4 steps based on the share's credential requirements:

| Condition | Steps shown |
|-----------|-------------|
| AI creds provided by owner, no integration creds required | Overview → Confirm |
| AI creds provided by owner, integration creds required | Overview → Integration Credentials → Confirm |
| User must supply AI creds, no integration creds required | Overview → AI Credentials → Confirm |
| User must supply AI creds, integration creds required | Overview → AI Credentials → Integration Credentials → Confirm |

**AI Credentials step is shown when:** `!share.ai_credentials_provided && share.required_ai_credential_types.length > 0`

## Component Structure

```
agents.tsx (Agents page)
└── PendingAgentCard
    └── AcceptShareWizard (dialog container + step indicator)
        ├── WizardStepOverview          (always shown)
        ├── WizardStepAICredentials     (conditional — AI creds step)
        ├── WizardStepCredentials       (conditional — integration creds step)
        └── WizardStepConfirm           (always shown)
```

**Component files:**
- `frontend/src/components/Agents/AcceptShareWizard/AcceptShareWizard.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepOverview.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepAICredentials.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepCredentials.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/WizardStepConfirm.tsx`
- `frontend/src/components/Agents/AcceptShareWizard/index.ts`
- `frontend/src/components/Agents/PendingAgentCard.tsx` (entry point)
- `frontend/src/routes/_layout/agents.tsx` (manages open state and passes selected share)

## State Management

**Wizard-level state (in `AcceptShareWizard`):**

| State | Type | Description |
|-------|------|-------------|
| `currentStep` | `"overview" \| "ai_credentials" \| "credentials" \| "confirm"` | Active wizard step |
| `credentialSelections` | `Record<string, CredentialSelection>` | Integration credential selections, keyed by credential name |
| `aiCredentialSelections` | `{conversationCredentialId, buildingCredentialId}` | Selected AI credential IDs |

**`CredentialSelection` structure:**
- `sourceCredentialName` — internal use only, never displayed to user
- `sourceCredentialType` — shown to user (e.g., "Gmail OAuth", "API Token")
- `allowSharing` — whether the original credential can be shared
- `selectedCredentialId` — user's chosen credential ID; `null` means "use shared from owner"

**Privacy rule:** Credential names from the owner's agent are never shown to the recipient — only credential types are displayed.

**Component props pattern:**
- `AcceptShareWizard` receives `open`, `onOpenChange`, `share: PendingSharePublic`, `onComplete`
- Step components receive `share`, current selections, `onChange` callback, and `onNext`/`onBack` navigation callbacks

## AI Credentials Step UI

| Category | Condition | Display |
|----------|-----------|---------|
| Provided by owner | `share.ai_credentials_provided` | Green section with "Provided by owner" badge |
| User default available | User has a default for the required SDK type | Green badge — "Using default: [Name]" |
| Selection required | No default, user has credentials for the type | Dropdown to select credential |
| Setup required | No credentials for the SDK type | Error message with link to Settings |

Validates that all required SDK types have credentials before allowing navigation to next step.

## Integration Credentials Step UI

| Category | `allow_sharing` | Display |
|----------|-----------------|---------|
| Shareable | `true` | Green card; dropdown defaults to "Use shared from owner"; user can override with own credential |
| Private | `false` | Yellow card; dropdown shows only user's own credentials of the same type |

**Dropdown options per credential:**
1. "Use shared from owner" (shareable credentials only, selected by default)
2. User's existing credentials of matching type
3. "Create new credential..." — immediately opens inline creation dialog

**Inline credential creation:**
- Opens as a modal when "Create new credential..." is selected
- Auto-focuses name field; Enter key submits
- Newly created credential is auto-selected after creation
- `useMutation` for `CredentialsService.createCredential()`

**Skip option:** Users can leave non-shareable credentials unselected and configure them after accepting.

## Confirm Step UI

Summary card shows:
- Agent name, access level, sharer name
- Credential status counts: "Using owner's shared credentials" (green), "Using your own credentials" (blue), "Skipped — setup later" (yellow)
- Warning alert when any non-shareable credentials were skipped

## API Interactions

| Action | Service / Endpoint |
|--------|--------------------|
| Submit wizard (accept share) | `AgentSharesService.acceptShare()` → `POST /api/v1/shares/{share_id}/accept` |
| Load user's AI credentials | `AiCredentialsService.listAiCredentials()` (in AI credentials step) |
| Load user's integration credentials | `CredentialsService.readCredentials()` (in integration credentials step) |
| Inline credential creation | `CredentialsService.createCredential()` |

**Query invalidation on success:** `["agents"]`, `["pendingShares"]`, `["credentials"]`

**`AcceptShareRequest` payload:**
- `credentials` — `{credential_name: credential_id}` dict; `null` value for a shareable credential means "use shared from owner"; `null` for a private credential creates a placeholder
- `ai_credential_selections` — `{conversation_credential_id, building_credential_id}`

## Integration Points

- **[Agent Sharing](agent_sharing.md)** — feature context, share lifecycle, credential handling rules
- **[Agent Sharing Tech](agent_sharing_tech.md)** — backend routes, services, models, and database schema
- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** — AI credential model and default selection logic
