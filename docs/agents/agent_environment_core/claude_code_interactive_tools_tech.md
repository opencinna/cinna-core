# Claude Code Interactive Tools — Technical Reference

Implementation details for the `can_use_tool` callback and the ask-only tool behavior it
works around. See [business doc](claude_code_interactive_tools.md) for the problem and
design rationale.

## File Locations

**Agent environment core (inside container):**
- `backend/app/env-templates/app_core_base/core/server/adapters/claude_code_sdk_adapter.py`
  - `send_message_stream()` / `generate()` — where the SDK client is constructed per message
  - `can_use_tool(tool_name, tool_input, context)` — local async closure, defined inline
    before `ClaudeAgentOptions` is built; denies `"AskUserQuestion"` with an explicit
    end-your-turn instruction, denies everything else by default
  - Wired via `ClaudeAgentOptions(..., can_use_tool=can_use_tool)`
  - `pre_allowed_tools` / `all_allowed_tools` — the `ClaudeAgentOptions.allowed_tools` list;
    this is what lets `Read`/`Edit`/`Bash`/etc. resolve CLI-side without ever reaching
    `can_use_tool` — unrelated to `tool_name_registry.PRE_APPROVED_TOOLS`, see below

**Backend (host, unrelated "pre-allowed" concept — do not conflate):**
- `backend/app/services/sessions/message_service.py` — `PRE_ALLOWED_TOOLS` frozenset; gates
  whether the chat UI shows an "Approve Tools" button. Includes `askuserquestion` and
  `exitplanmode` — this only means those tools don't trigger the approval-button flow, it has
  no effect on the CLI's own `checkPermissions()` behavior documented here.
- `backend/app/env-templates/app_core_base/core/server/adapters/tool_name_registry.py` —
  `PRE_APPROVED_TOOLS`; canonical source for the constant mirrored above.

**Third-party (not editable, referenced for forensics only):**
- `claude_agent_sdk` Python package (`.venv/lib/python3.13/site-packages/claude_agent_sdk/`):
  - `types.py` — `CanUseTool` callback type, `PermissionResultAllow`, `PermissionResultDeny`,
    `ToolPermissionContext`, `ClaudeAgentOptions.can_use_tool` field
  - `client.py` (`ClaudeSDKClient.connect()`, ~L106-124) — validates `can_use_tool` requires
    streaming mode and is mutually exclusive with `permission_prompt_tool_name`; when
    `can_use_tool` is set, `permission_prompt_tool_name` is auto-forced to `"stdio"` to route
    ask-only tools through the control protocol
  - `_internal/query.py` (`_handle_control_request`, ~L228-278) — the `subtype == "can_use_tool"`
    branch; raises `Exception("canUseTool callback is not provided")` when `self.can_use_tool`
    is `None`, which becomes a `control_response` with `subtype: "error"` sent back to the CLI
- `@anthropic-ai/claude-code` CLI binary (installed at
  `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` inside agent containers;
  compiled/minified, not human-readable source) — verified via `strings` extraction against
  v2.1.119:
  - `AskUserQuestion`'s tool definition: `async checkPermissions(q){return {behavior:"ask",
    message:"Answer questions?", updatedInput:q}}` — unconditional, no dependency on
    `allowed_tools`/`permission_mode`
  - `ExitPlanMode`'s tool definition contains a conditional allow branch followed by
    `return {behavior:"ask", message:"Exit plan mode?", updatedInput:q}` — asks whenever the
    preceding condition (permission_mode already bypass-equivalent) doesn't hold
  - The CLI's generic "registered tool" wrapper (used for dynamically-registered/plugin
    tools): `checkPermissions(){return {behavior:"ask", message:\`Execute registered tool
    "${q.name}"\`}}` — every such tool is ask-only by construction, same shape as the above

## Verified failure sequence (from a captured session log)

Session event log format: `backend/app/env-templates/app_core_base/core/server/sdk_utils.py`
(`SessionEventLogger.log_send` / `log_recv`, used by `claude_code_sdk_adapter.py:187,498,517`)
writes one JSONL event per SDK message; a captured example showed:

1. `AssistantMessage` with a `ToolUseBlock` for `AskUserQuestion` (`t`)
2. `UserMessage` with `ToolResultBlock(content='Answer questions?', is_error=True)` at `t+16ms`
   — too fast to be a real user response; this is the CLI's own control-request-error fallback
3. Model retries `AskUserQuestion` once (same live session/subprocess, before the turn's
   `ResultMessage`) and gets the identical immediate denial
4. Model gives up and asks the questions as plain streaming text instead

Confirms the denial happens synchronously within the same CLI subprocess/turn — the process
is not gone by the time the denial occurs, but nothing on the Python side was intercepting it
before this fix.

## Design choices and rejected alternatives

- **Why not hold the connection open for a real synchronous answer** (mirroring the depth of
  OpenCode's `/reply` relay): `claude_code_sdk_adapter.py`'s `generate()` spawns a fresh
  `ClaudeSDKClient` per chat message and calls `client.disconnect()` in its `finally` block
  once that message's turn reaches a `ResultMessage`. There is no long-running per-session
  server process to suspend against (unlike OpenCode's `opencode serve`), so waiting inside
  `can_use_tool` for an arbitrary-length human response would mean holding an HTTP
  streaming connection, a worker, and a subprocess open indefinitely — fragile against
  timeouts/browser disconnects/worker restarts and not needed, since the "next message is the
  answer" flow already works.
- **Why the fallback denies instead of allows:** verified via CLI binary strings that
  `ExitPlanMode` and the generic "registered tool" wrapper share `AskUserQuestion`'s
  unconditional-ask shape. A blanket `PermissionResultAllow` fallback would have silently
  auto-approved leaving plan mode (and any future ask-only tool) with no real confirmation —
  a security-relevant regression versus the pre-fix behavior, where every ask-only tool was
  effectively denied (via the missing-callback exception path).
- **`interrupt=False` on the `AskUserQuestion` deny:** lets the model add a short trailing
  acknowledgement rather than hard-aborting the turn; the explicit instruction text in the
  deny message is what actually stops retries, not the interrupt flag.

## Tests

None currently cover this path (no unit tests exist for `claude_code_sdk_adapter.py` — it
lives under `env-templates/`, outside `backend/tests/`). Gap: no automated coverage of the
`can_use_tool` callback's branch behavior (AskUserQuestion deny message, default-deny
fallback) or a regression test pinning that the fallback must never become an allow.

## Security

- The fallback deny-by-default is the security-relevant property of this change — see
  "Design choices" above. Any change to this callback should preserve deny-by-default for
  every `tool_name` other than `"AskUserQuestion"`.
- No new external surface: `can_use_tool` only talks to the already-trusted CLI subprocess
  over the existing local control protocol (stdio), same trust boundary as the rest of
  `claude_code_sdk_adapter.py`.
