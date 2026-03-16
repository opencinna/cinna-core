# Tool Approval Widget

## Purpose

The tool approval widget allows users to approve agent tool usage directly from the chat interface. When an agent attempts to use tools not yet in its allowed list, an **Approve Tools** button appears below the message bubble, enabling one-click approval that permanently adds the tools to the agent's configuration and resumes the conversation.

## User Flow

1. Agent calls a tool during streaming that is not in the agent's `allowed_tools` list
2. Backend flags the tool in `message_metadata.tools_needing_approval`
3. Backend filters the list against the agent's current `allowed_tools` when returning messages — already-approved tools are excluded automatically
4. Frontend detects `tools_needing_approval` in message metadata via `useToolApproval` hook
5. **Approve Tools** button appears below the message bubble (amber-styled, right-aligned)
6. Button tooltip shows the list of tools pending approval
7. User clicks the button → loading spinner replaces icon
8. API call adds tools to agent's allowed_tools via `AgentsService.addAllowedTools()`
9. On success:
   - Toast notification: "Tools approved"
   - Agent query invalidated to refresh SDK config
   - Auto-sends "Tools approved — continue." message to resume the conversation
10. Button disappears (tools are now approved)
11. On page reload, backend filters out approved tools — button won't reappear

## Business Rules

- Approval is permanent — tools are added to the agent's `allowed_tools` configuration
- Button only visible when `tools_needing_approval` has entries AND tools haven't been approved in current session
- Backend handles deduplication — if a tool is already in `allowed_tools`, the API call is idempotent
- `agentId` is required — if not available (e.g., some integration contexts), the widget is not rendered
- After approval, a follow-up message is automatically sent to prompt the agent to continue from where it left off
- Multiple tools can be approved in a single action (batch approval)

## Integration Points

- **[Chat Windows](chat_windows.md)** — Widget renders as an action button below agent messages in the chat
- **[Tools Approval Management](../../agents/agent_environment_core/tools_approval_management.md)** — Backend logic for tool approval detection, allowed_tools management, and message metadata filtering
- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — The auto-sent "Tools approved — continue." message triggers a new streaming cycle
