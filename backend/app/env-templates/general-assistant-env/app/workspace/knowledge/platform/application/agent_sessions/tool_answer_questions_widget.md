# AskUserQuestion Tool Widget

## Widget Purpose

The AskUserQuestion tool widget allows agents to ask structured questions to users mid-conversation. When an agent calls the `AskUserQuestion` tool, the message is flagged as containing unanswered questions and an interactive **Answer Questions** button appears below the message bubble. Users open a modal, answer all questions, and submit — their responses are formatted and sent as a follow-up message, resuming the conversation.

Part of the [Agent Sessions](./agent_sessions.md) session view.

## User Flow

1. Agent calls `AskUserQuestion` tool with one or more structured questions during a session
2. Backend detects the tool call in streaming events → sets `tool_questions_status = "unanswered"` on the message
3. Frontend renders a compact **AskUserQuestion** tool block inside the message bubble (shows question count)
4. **Answer Questions** button appears below the message bubble (left-aligned, outside bubble)
5. User clicks the button → **Answer Questions** modal opens
6. Modal shows all questions sequentially; progress indicator visible when multiple questions present
7. User answers each question:
   - Single-select: radio buttons; multi-select: checkboxes
   - Every question also has a free-text "Other" option
   - `(Recommended)` options are highlighted with a star icon
8. **Send Answers** button enables once all questions answered
9. User submits → answers formatted as structured text, sent as a new message with `answers_to_message_id` linking back to the original question message
10. Backend updates original message `tool_questions_status = "answered"`
11. **Answer Questions** button disappears; agent receives the formatted answers and continues

**Deduplication:** If the LLM calls `AskUserQuestion` multiple times in one response, duplicate questions (identical text) are collapsed — only the first occurrence is shown.

## Component Structure

- `frontend/src/components/Chat/AskUserQuestionToolBlock.tsx` — Compact block rendered inside the message bubble; shows question count; subtle slate background distinguishes it from message text
- `frontend/src/components/Chat/MessageActions.tsx` — Renders **Answer Questions** button below the message bubble; visible only when `tool_questions_status === "unanswered"`
- `frontend/src/components/Chat/AnswerQuestionsModal.tsx` — Full-screen modal with:
  - Progress indicator (answered / total, shown for multiple questions)
  - Radio buttons for single-select, checkboxes for multi-select
  - Custom text input available on every question
  - Visual highlighting: selected options highlighted, unselected faded
  - `(Recommended)` options highlighted with star icon
  - Send button disabled until all questions answered
- `frontend/src/components/Chat/MessageBubble.tsx` — Extracts questions from `message_metadata.streaming_events`; deduplicates by exact question text via `extractQuestions()`; renders `MessageActions` and manages modal open/close state

## State Management

- `tool_questions_status` on the `AgentSessionMessage` model — persisted in the database, survives page refresh. Values: `null` (no questions), `"unanswered"`, `"answered"`
- `answers_to_message_id` — FK from the answer message back to the question message; creates a traceable Q&A audit trail
- Frontend optimistic update in `frontend/src/hooks/useSessionStreaming.ts`:
  1. User submits answers
  2. React Query cache immediately updated to `"answered"` (hides modal and button instantly)
  3. API request sent with `answers_to_message_id` in body
  4. Backend confirms; cache refreshes with server state

## Answer Format

Answers are formatted as plain text before being sent as a message:

**Single-select:**
```
Question text here?
Answer: Selected option label
```

**Multi-select:**
```
Question text here?
Answers:
- Option one
- Option two
- Custom answer: User typed text
```

Multiple questions are separated by blank lines. Custom answers are prefixed with `"Custom answer: "`.

## API Interactions

No dedicated endpoint for the widget. It reuses the standard session message send endpoint:

- `POST /api/v1/sessions/{session_id}/messages/stream` — Sends the formatted answer message. The request body includes `answers_to_message_id` to link the answer to the original question message

**Backend detection** (`backend/app/services/message_service.py`):
- `detect_ask_user_question_tool()` — Scans `streaming_events` for `AskUserQuestion` tool calls
- `stream_message_with_events()` — Sets `tool_questions_status = "unanswered"` when detected
- `create_message()` — When `answers_to_message_id` is provided, automatically sets the referenced message's `tool_questions_status = "answered"`

**Related files:**
- `backend/app/models/session.py` — `tool_questions_status`, `answers_to_message_id` fields on `AgentSessionMessage`
- `backend/app/services/message_service.py` — Detection and status logic
- `backend/app/api/routes/messages.py` — Message send endpoint
- `frontend/src/hooks/useSessionStreaming.ts` — `sendMessage()` extended with `answersToMessageId` param
- Migration: `backend/app/alembic/versions/bfbae71690bb_add_tool_questions_status_and_answers_.py`
