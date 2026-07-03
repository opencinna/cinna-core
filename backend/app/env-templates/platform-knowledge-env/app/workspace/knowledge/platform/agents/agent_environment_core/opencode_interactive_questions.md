# OpenCode Interactive Questions (ask-user-question)

## Purpose

Describes how OpenCode's built-in `question` tool behaves, the **known bug** where a
question wedges the session (the session gets stuck "streaming" forever), why the current
`reject`-based handling cannot fix it, and the **proper fix**: relay the next user message
as the answer via `POST /question/{requestID}/reply`.

This is an aspect of [Multi-SDK Support](multi_sdk.md); it only concerns the OpenCode
engine. Claude Code's `AskUserQuestion` has a related but distinct fix — see
[Claude Code Interactive Tools](claude_code_interactive_tools.md).

> **Status:** the `/reply`-relay fix described below is **implemented** (the bug section is
> kept as design rationale). The adapter files are baked into agent containers, so the fix
> takes effect in a given environment only after an env rebuild/reconfigure.

## Core Concepts

| Term | Definition |
|------|-----------|
| **`question` tool** | OpenCode's built-in tool the model calls to ask the user a structured (multiple-choice + optional free-text) question |
| **`question.asked` event** | SSE event OpenCode publishes when the `question` tool is invoked; carries the request `id`, `sessionID`, and the `questions` array |
| **Pending question** | A `{info, deferred}` entry OpenCode stores in **per-session server state** (a `pending` map keyed by request id) while the question awaits an answer |
| **`/question/{id}/reply`** | Resolves the pending question's Deferred **with the answers**, so the same suspended assistant turn continues normally |
| **`/question/{id}/reject`** | Fails the Deferred with `QuestionRejectedError` ("The user dismissed this question") — an **abort**, not a completion |
| **Answer relay** | The platform rule that the **next user message** for a session with a pending question is treated as that question's answer — independent of any "reply-to" parameter |

## How OpenCode's `question` tool actually works

The `question` tool is **blocking by construction**. When the model calls it, OpenCode:

1. Creates a Deferred and stores `{info, deferred}` in the session's `pending` map (server
   state, **not** tied to the HTTP request that is generating the turn).
2. Publishes the `question.asked` event.
3. **Suspends the assistant turn**, awaiting the Deferred — the in-flight
   `POST /session/{id}/message` stays open while the turn is parked here.

The Deferred is released only by:
- `POST /question/{id}/reply` with `{answers}` → `await` returns the answers → the tool
  produces a normal result → **the same turn resumes and runs to a clean `session.idle`.**
- `POST /question/{id}/reject` → `await` throws `QuestionRejectedError` → the turn is
  marked blocked and **breaks/aborts** (no answer delivered, no clean finish).

OpenCode also exposes `GET /question`, which lists all pending requests across sessions
(each carries `id`, `sessionID`, `questions`). This is the authoritative, parameter-free
way to detect that a session is waiting on an answer.

Because the pending entry lives in session state, it survives independently of the
originating POST connection — but the **awaiting turn fiber** is what actually needs the
answer, and it must still be alive for `/reply` to resume the turn.

## The bug — sessions wedge after a question

Observed: a conversation-mode session shows the AskUserQuestion widget; the user answers as
a normal chat message; the session then stays stuck in the "streaming" state indefinitely,
and the underlying OpenCode session is eventually deleted ("Session not found").

Sequence:

1. The model calls `question` → `question.asked`. The adapter emits the unified
   `askuserquestion` TOOL_USE + DONE pair and ends the backend stream so the widget shows.
2. In its teardown the adapter fires `POST /question/{id}/reject` **and** cancels the
   still-suspended turn's POST connection.
3. `reject` aborts the turn instead of completing it; combined with the POST cancel, the
   session is left with a non-finalized prior turn (no `session.idle`).
4. The user's answer arrives as a fresh `POST /session/{id}/message`. OpenCode serializes
   message processing per session and will not start a new generation while the prior turn
   is unfinalized → **the POST hangs forever** → the backend stream is torn down
   (interrupt) but the session's `interaction_status` is never cleared → the UI is stuck.

## Why `reject` cannot fix it

- **`reject` is an abort, not a completion.** It injects `QuestionRejectedError`, which
  OpenCode treats as a break condition; the assistant message holding the `question` tool
  call is left aborted and never reaches a terminal `session.idle`.
- **`reject` carries no answer.** Even if a follow-up message went through, the model only
  ever saw "user dismissed the question," never the answer in the context where it asked.
- **The aborted turn wedges per-session processing.** OpenCode runs one turn per session at
  a time; the unfinalized turn blocks the next message's generation.
- **`reject` is not a recovery tool.** Once the turn is aborted and disconnected, the
  pending entry is already cleaned up and the fiber is gone; a later `reject` finds nothing
  to resolve. The only recovery is `DELETE /session`, which discards the session entirely.

In short: `reject` says *"forget this question, the turn is over"* — it discards the answer
and leaves the session half-finished. `reply` says *"here is the answer, continue the same
turn"* — the turn completes normally and the session stays healthy.

## The proper fix — relay the next message as `/reply`

Route the user's answer to `POST /question/{id}/reply` instead of rejecting and re-posting
it as a fresh message. Detection must be **parameter-free** (some interactive flows — MCP,
external callers — may not carry a reliable "reply-to" marker):

- On every resume, the env-core asks OpenCode whether the target session has an outstanding
  question (`GET /question` filtered by `sessionID`, plus an in-memory fast path). If so,
  **the incoming message is the answer**, mapped to `{answers}` and sent to `/reply`; the
  resumed turn's events stream back as that turn's response.
- The blocking question tool's turn must stay alive until the answer arrives (do not reject
  and do not cancel the suspended POST on `question.asked`); reject is reserved for the
  explicit interrupt / teardown path only.

### Answer mapping

`reply` expects `{ "answers": Answer[] }` where `Answer = string[]`, one entry per question
in order (free-text "custom" answers are allowed). The parameter-free fallback wraps the
message text as a single custom answer in slot 0 and pads the remaining slots.

## Alternative — disable the blocking tool (config only)

If the structured widget is not required, the `question` tool can be removed from the
model's toolset entirely, so it never blocks and asks in plain streaming text instead. Two
equivalent levers in the generated `opencode.json` (an explicit key is required because the
current `"*": "allow"` permission wildcard otherwise enables it):

- `permission: { "question": "deny" }`, or
- `tools: { "question": false }`

This is non-blocking by construction but loses OpenCode's native multiple-choice answer UI.
The `/reply` relay is preferred because it preserves the widget.

## Business Rules

- A session with a pending OpenCode question must have its **next user message** treated as
  the answer, regardless of whether any "reply-to" parameter is present.
- `reject` is used only for explicit interrupt / cancel / teardown — never as the normal
  path after a question is asked.
- A cancelled or interrupted stream must still clear the session's `interaction_status`, so
  a transient hang never leaves the UI stuck "streaming".
- Claude Code's `AskUserQuestion` is handled separately, by denying the CLI's ask-only
  permission check with an explicit instruction rather than suspending/relaying — see
  [Claude Code Interactive Tools](claude_code_interactive_tools.md).

## Architecture Overview

```
model calls `question` tool
   │  OpenCode stores {info, deferred} in session `pending` map, publishes question.asked,
   │  suspends the turn awaiting the Deferred
   ▼
OpenCodeAdapter: emit askuserquestion TOOL_USE + DONE → UI shows widget
   │  (proper fix: remember requestID; DO NOT reject, DO NOT kill the suspended turn)
   ▼
user replies (normal chat message, any caller)
   │
   ▼
OpenCodeAdapter resume: GET /question → pending for this session?
   ├── yes → POST /question/{id}/reply { answers }  → turn resumes → stream to session.idle
   └── no  → POST /session/{id}/message (normal turn)
```

## Integration Points

- **Multi-SDK Support:** OpenCode adapter + transformer that own this flow — see
  [Multi-SDK Support](multi_sdk.md) and [Multi-SDK Tech](multi_sdk_tech.md)
- **AskUserQuestion widget:** the unified widget rendered for both SDKs — see
  [AskUserQuestion Tool Widget](../../application/chat_interface/tool_answer_questions_widget.md)
- **Agent Sessions:** `interaction_status` lifecycle and the "streaming" UI state — see
  [Agent Sessions](../../application/agent_sessions/agent_sessions.md)
- **Tools Approval:** sibling OpenCode out-of-band flow (`permission.asked`) — see
  [Tools Approval Management](tools_approval_management.md)
