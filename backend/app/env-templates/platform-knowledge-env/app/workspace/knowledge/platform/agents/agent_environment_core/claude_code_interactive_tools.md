# Claude Code Interactive Tools (ask-only permission)

## Purpose

Describes a class of Claude Code SDK tools — `AskUserQuestion`, `ExitPlanMode`, and any
dynamically-registered/plugin tool — whose permission check is hardcoded to require
interactive approval regardless of `allowed_tools`/`permission_mode`, why that broke
headlessly inside agent environments (a confusing denied tool_result instead of a working
flow), and the `can_use_tool` fix.

This is an aspect of [Multi-SDK Support](multi_sdk.md); it only concerns the Claude Code
engine. See [OpenCode Interactive Questions](opencode_interactive_questions.md) for the
equivalent OpenCode-side story — the two engines solve the same UX problem (an agent asking
the user a structured question mid-turn) with different mechanisms.

> **Status:** implemented. The adapter file is baked into agent containers, so the fix takes
> effect in a given environment only after an env rebuild/reconfigure.

## Core Concepts

| Term | Definition |
|------|-----------|
| **ask-only tool** | A Claude Code SDK tool whose `checkPermissions()` unconditionally returns `behavior: "ask"` — `AskUserQuestion` always, `ExitPlanMode` whenever `permission_mode` isn't `bypassPermissions`, and any tool registered through the CLI's generic "registered tool" wrapper (used for custom/plugin tools). `allowed_tools` and `permission_mode` cannot pre-approve these — the CLI treats the ask as intrinsic to the tool, not a configurable policy. |
| **`can_use_tool` callback** | `ClaudeAgentOptions.can_use_tool` — the Python SDK's control-protocol hook the CLI calls for any ask-only tool. Required whenever an ask-only tool might be invoked; without it the CLI's control request errors out. |
| **`PermissionResultAllow` / `PermissionResultDeny`** | The two response types `can_use_tool` must return. Allow can rewrite the tool's input (`updated_input`) — this is how the interactive terminal merges a human's typed answer back into `AskUserQuestion`'s input before the tool actually executes. |
| **Two unrelated "pre-allowed" concepts** | `PRE_ALLOWED_TOOLS` / `PRE_APPROVED_TOOLS` (see [Tools Approval Management](tools_approval_management.md)) gate whether the **chat UI** shows an "Approve Tools" button — a product-level allowlist that already includes `askuserquestion` and `exitplanmode`. This is unrelated to the **CLI-internal** `checkPermissions()` behavior described here, which `allowed_tools`/`permission_mode` cannot override. A tool being in one list says nothing about the other — this is exactly the trap that produced the original bug. |

## How ask-only tools actually work

In the real interactive terminal, an ask-only tool's `checkPermissions()` request is
intercepted by the CLI's own UI: for `AskUserQuestion` this renders the multi-choice prompt
and, once the user answers, resolves the permission check with `behavior: "allow"` and
`updated_input` containing the collected answers — the tool then executes and returns those
answers as its result. For `ExitPlanMode`, the CLI renders a plan-approval prompt and
resolves allow/deny based on the user's choice.

Headless SDK sessions (this backend spawns the CLI as a subprocess per chat message, with no
TTY) have no such terminal UI. The **only** way to answer an ask-only tool's permission
request in headless mode is the `can_use_tool` callback — supplying real logic there is the
sole substitute for the terminal's built-in renderer.

## The bug

`claude_code_sdk_adapter.py` built `ClaudeSDKClient` without a `can_use_tool` callback. When
the model called `AskUserQuestion`, the CLI's control request for `checkPermissions()`
reached the Python SDK, which raised `"canUseTool callback is not provided"` (no handler
registered) and returned that as an error to the CLI. The CLI treated this as a denial and
reported a `tool_result` back to the model using its own permission-prompt text as the
content: `content: "Answer questions?"`, `is_error: true` — appearing roughly 16ms after the
tool call, far too fast to be a real user answering. The model, seeing what looks like an
unexplained tool failure, would sometimes retry the same tool once before giving up and
asking the questions as plain text instead — functionally recoverable, but confusing,
wasteful, and dropping the structured multi-choice widget experience.

`ExitPlanMode` was equally affected before the fix (same missing-callback error path), though
less visible in practice since plan-mode workflows are a smaller slice of usage.

## The fix — a scoped `can_use_tool` callback

`claude_code_sdk_adapter.py` now passes a `can_use_tool` callback to `ClaudeAgentOptions`:

- **`AskUserQuestion` → clean, intentional deny.** Rather than leaving the tool call to fail
  with the CLI's confusing generic text, the callback denies it immediately with an explicit
  instruction telling the model the questions were already shown to the user and the answer
  will arrive as a separate chat message — so it stops retrying/apologizing and ends its turn
  cleanly. This works because the frontend widget doesn't depend on the tool_result at all:
  [`MessageService.detect_ask_user_question_tool()`](../../application/chat_interface/tool_answer_questions_widget.md)
  flags a message as having unanswered questions purely from the `ToolUseBlock` appearing in
  the stream, and the "Answer Questions" modal already formats the user's submission as a
  normal follow-up chat message — this **already** matches the design intent described for
  OpenCode's `/reply` relay (next message = the answer), just without an engine-specific
  relay step, since a fresh Claude Code CLI process is spawned per chat message regardless.
- **Every other tool → deny by default, not allow.** The fallback branch explicitly denies
  with a stated reason, rather than blanket-allowing. This matters because *any* tool that
  reaches this callback at all is, by construction, one the CLI decided needs interactive
  approval outside of `allowed_tools`/`permission_mode` — most concretely `ExitPlanMode`,
  which exists specifically to gate an agent leaving plan mode and starting to make changes.
  A permissive fallback here would have silently defeated that gate (and any future CLI tool
  with the same ask-only shape) instead of fixing the one tool this change targets.

No tool the backend actually intends to run reaches this callback at all — `Read`, `Edit`,
`Bash`, `Write`, and the rest are resolved CLI-side via `allowed_tools`/`permission_mode`
without ever entering the control-protocol round trip this callback handles.

## Business Rules

- `can_use_tool` is scoped to exactly one tool name (`"AskUserQuestion"`) for the allow-ish
  (deny-with-instruction) path; every other tool_name is denied by default.
- The fallback deny must **never** become a blanket allow — doing so silently disables
  `ExitPlanMode`'s plan-approval gate and any other current-or-future ask-only tool.
- The user's answer to `AskUserQuestion` is relayed exclusively via the existing "next chat
  message" mechanism (the Answer Questions modal), not via any synchronous in-process wait —
  the CLI subprocess for a given turn already exits before the human can plausibly respond,
  so there is nothing to hold open.
- Being listed in `PRE_ALLOWED_TOOLS`/`PRE_APPROVED_TOOLS` (UI approval-button gating) does
  not exempt a tool from the CLI's own `checkPermissions()` logic — the two systems are
  independent and must both be checked when reasoning about whether a tool "just works"
  headlessly.

## Architecture Overview

```
Model calls AskUserQuestion (or ExitPlanMode, or a registered plugin tool)
        │
        ▼
CLI: checkPermissions() → behavior:"ask" (unconditional — allowed_tools can't bypass this)
        │
        ▼
SDK control protocol → ClaudeAgentOptions.can_use_tool(tool_name, input, context)
        │
        ├── tool_name == "AskUserQuestion"
        │     → PermissionResultDeny("answer arrives as next message, end turn")
        │
        └── anything else (e.g. ExitPlanMode)
              → PermissionResultDeny("requires interactive approval, unavailable here")
        │
        ▼
CLI reports a denied tool_result to the model; turn ends
        │
        ▼
Frontend: ToolUseBlock already in the stream → "Answer Questions" widget shown
        │
        ▼
User submits the modal → formatted as a normal follow-up chat message
        │
        ▼
New CLI subprocess (session resume) processes it like any other turn
```

## Integration Points

- **Multi-SDK Support:** engine selection and the adapter contract both engines implement —
  see [Multi-SDK Support](multi_sdk.md)
- **OpenCode Interactive Questions:** the equivalent problem and fix on the OpenCode engine
  (session-suspend + `/reply` relay, rather than deny-and-relay-via-next-message) — see
  [OpenCode Interactive Questions](opencode_interactive_questions.md)
- **Tools Approval Management:** the unrelated "pre-allowed tools" concept that gates the
  chat UI's approval button — see [Tools Approval Management](tools_approval_management.md)
- **AskUserQuestion widget:** the unified widget rendered for both SDKs, driven purely by the
  `ToolUseBlock` appearing in the stream — see
  [AskUserQuestion Tool Widget](../../application/chat_interface/tool_answer_questions_widget.md)
