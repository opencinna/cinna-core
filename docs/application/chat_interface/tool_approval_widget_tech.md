# Tool Approval Widget — Technical Reference

## File Locations

### Frontend

- `frontend/src/hooks/useToolApproval.ts` — Hook managing approval state: extracts `tools_needing_approval` from `message.message_metadata`, provides `approveTools()` function via `useMutation`, tracks `isApproving`/`isApproved`/`error` state
- `frontend/src/components/Chat/MessageActions.tsx` — Renders the **Approve Tools** button (amber styling) alongside **Answer Questions** button. Shows tool count in label, full tool list in tooltip. Spinner during approval
- `frontend/src/components/Chat/MessageBubble.tsx` — Integrates `useToolApproval` hook, passes props to `MessageActions`, handles post-approval toast and auto-continue message via `useEffect` on `isApproved`

### Backend

- `backend/app/services/sessions/message_service.py` — Detects tools needing approval from streaming events, sets `message_metadata.tools_needing_approval`
- `backend/app/api/routes/messages.py` — When returning messages, filters `tools_needing_approval` against agent's current `allowed_tools` (removes already-approved)
- `backend/app/api/routes/agents.py` — `addAllowedTools` endpoint: adds tools to agent's `allowed_tools` list

## Hook Interface

`useToolApproval({ message, agentId })` returns:
- `toolsNeedingApproval: string[]` — Tool names extracted from `message_metadata.tools_needing_approval`
- `hasToolsNeedingApproval: boolean` — True when list is non-empty AND not yet approved in this session
- `isApproving: boolean` — Mutation pending state
- `isApproved: boolean` — Local flag set on mutation success
- `error: string | null` — Error message from failed mutation
- `approveTools: () => Promise<void>` — Calls `AgentsService.addAllowedTools()` with all pending tools

## Post-Approval Flow

In `MessageBubble.tsx`, a `useEffect` watches `isApproved`:
1. When `isApproved` becomes true (guarded by `approvalMessageSentRef` to prevent double-send):
   - Shows success toast via `sonner`
   - Calls `onSendMessage("Tools approved — continue.")` to trigger a new streaming cycle
2. Agent query invalidated (`queryKey: ["agent", agentId]`) to refresh SDK config

## Message Metadata Fields

- `tools_needing_approval: string[]` — Set by backend during streaming, filtered on read
- Backend filtering in message endpoint ensures the list reflects only tools that are STILL not in `allowed_tools` — provides consistency across page refreshes without requiring frontend state persistence
