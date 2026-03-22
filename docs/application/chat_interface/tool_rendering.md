# Tool Call Rendering

## Purpose

Tool call rendering provides specialized visual feedback for each type of tool the AI agent uses during conversation. When the agent reads a file, runs a command, searches the web, or delegates tasks, each action is displayed with a purpose-built UI block that makes the agent's work process transparent and scannable.

## Core Concepts

- **Tool Call Block** — A specialized visual component that renders a single tool invocation. Each tool type has a dedicated block with contextual display (file path, command, query, etc.)
- **Tool Dispatch** — The `ToolCallBlock` component acts as a router, matching `toolName` to the appropriate specialized block. Unknown tools fall back to a generic JSON parameter display
- **Compact Mode** — An alternate display density where tool blocks show minimal information (filenames instead of full paths, shortened commands). Controlled by the `conversationModeUi` prop
- **Tool Input** — The structured parameters passed to the tool, extracted from `event.metadata.tool_input` in streaming events or from `message_metadata.streaming_events[]` in persisted messages

## Tool Block Registry

| Tool Name | Block Component | What It Shows | Compact Behavior |
|-----------|----------------|---------------|-----------------|
| `read` | ReadToolBlock | File path being read | Filename only with icon |
| `write` | WriteToolBlock | File path + content being written | Same (no compact variant) |
| `edit` | EditToolBlock | File path + old/new string comparison | Filename only with icon |
| `bash` | BashToolBlock | Shell command being executed | CompactBashBlock (shortened) |
| `glob` | GlobToolBlock | Glob pattern for file matching | Same |
| `websearch` | WebSearchToolBlock | Search query text | Same |
| `todowrite` | TodoWriteToolBlock | Todo items with completion status | Same |
| `askuserquestion` | AskUserQuestionToolBlock | Count of questions received | Same |
| `mcp__knowledge__query_integration_knowledge` | KnowledgeQueryToolBlock | Query + article IDs | Same |
| `mcp__task__create_agent_task` | AgentHandoverToolBlock | Target agent + task message, or inbox task | Same |
| `mcp__task__update_session_state` | UpdateSessionStateToolBlock | Result state + summary | Same |
| `webapp_action` | WebappActionBlock | Action name + data payload | Simplified |
| (unknown) | Default renderer | Generic "Using tool: name" + JSON params | Same |

## Dispatch Logic

The `ToolCallBlock` component matches tools by `toolName.toLowerCase()` and checks for required input fields:

- Match requires both name AND required fields present (e.g., `read` needs `file_path`, `edit` needs `file_path` + `old_string` + `new_string`)
- If a known tool name is missing required fields, it falls to the default renderer
- Compact mode is checked via `conversationModeUi === "compact"` — some tools have dedicated compact variants, others render the same in both modes

## Default Tool Renderer

When no specialized block matches, the default renderer shows:
- Wrench icon + "Using tool: `toolName`" header
- All input parameters listed as key-value pairs
- String values with markdown-like content (newlines, code blocks, lists) rendered via MarkdownRenderer
- Non-string values serialized as formatted JSON

## User Interaction

Tool blocks are display-only with two exceptions:
- **AskUserQuestionToolBlock** — clicking opens a debug modal showing the raw questions (the actual answering flow is handled by `AnswerQuestionsModal` at the message level)
- **AgentHandoverToolBlock** — includes navigation links to the created task session or inbox task

## Business Rules

- Tool blocks render from streaming events during active streaming AND from persisted `message_metadata.streaming_events[]` in completed messages
- The same component tree renders in both cases — `StreamEventRenderer` dispatches tool events to `ToolCallBlock` regardless of source
- Compact mode is a UI-only preference — the full tool input data is always stored and available
- New tools without a dedicated block automatically get basic rendering via the default renderer — no registration required
- Tool names from MCP integrations use `mcp__server__tool_name` format and are matched accordingly

## Integration Points

- **[Chat Windows](chat_windows.md)** — Tool blocks are rendered inside the chat message flow via StreamEventRenderer
- **[Tools Approval](../../agents/agent_environment_core/tools_approval_management.md)** — Tools needing approval are flagged in message metadata; approval UI is separate from tool block rendering
- **[Agent Commands](../../agents/agent_commands/agent_commands.md)** — Command responses bypass tool rendering entirely, rendered as markdown
