import { Wrench, FileText, FileEdit } from "lucide-react"
import { MarkdownRenderer } from "./MarkdownRenderer"
import { ReadToolBlock } from "./ReadToolBlock"
import { TodoWriteToolBlock } from "./TodoWriteToolBlock"
import { WriteToolBlock } from "./WriteToolBlock"
import { EditToolBlock } from "./EditToolBlock"
import { AskUserQuestionToolBlock } from "./AskUserQuestionToolBlock"
import { GlobToolBlock } from "./GlobToolBlock"
import { WebSearchToolBlock } from "./WebSearchToolBlock"
import { BashToolBlock } from "./BashToolBlock"
import { KnowledgeQueryToolBlock } from "./KnowledgeQueryToolBlock"
import { AgentHandoverToolBlock } from "./AgentHandoverToolBlock"
import { UpdateSessionStateToolBlock } from "./UpdateSessionStateToolBlock"
import { CompactBashBlock } from "./CompactBashBlock"

interface ToolCallBlockProps {
  toolName: string
  toolInput?: Record<string, any>
  conversationModeUi?: string
}

export function ToolCallBlock({ toolName, toolInput, conversationModeUi = "detailed" }: ToolCallBlockProps) {
  const isCompact = conversationModeUi === "compact"
  const toolNameLower = toolName.toLowerCase()

  // Helper: get a tool input value by snake_case key, falling back to camelCase.
  // Claude Code uses snake_case (file_path), OpenCode uses camelCase (filePath).
  const getInput = (snakeKey: string): any => {
    if (!toolInput) return undefined
    if (toolInput[snakeKey] !== undefined) return toolInput[snakeKey]
    // Convert snake_case to camelCase for fallback lookup
    const camelKey = snakeKey.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
    return toolInput[camelKey]
  }

  // Special rendering for Read tool
  const filePath = getInput("file_path")
  if (toolNameLower === "read" && filePath) {
    if (isCompact) {
      const fileName = filePath.split('/').pop() || filePath
      return (
        <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
          <FileText className="h-3.5 w-3.5 flex-shrink-0" />
          <span>Reading file <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{fileName}</code></span>
        </div>
      )
    }
    return <ReadToolBlock filePath={filePath} />
  }

  // Special rendering for Write tool
  const writeContent = getInput("content")
  if (toolNameLower === "write" && filePath && writeContent) {
    return <WriteToolBlock filePath={filePath} content={writeContent} />
  }

  // Special rendering for Edit tool
  const oldString = getInput("old_string")
  const newString = getInput("new_string")
  if (toolNameLower === "edit" && filePath && oldString && newString) {
    if (isCompact) {
      const fileName = filePath.split('/').pop() || filePath
      return (
        <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
          <FileEdit className="h-3.5 w-3.5 flex-shrink-0" />
          <span>Editing file <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{fileName}</code> ...</span>
        </div>
      )
    }
    return <EditToolBlock filePath={filePath} oldString={oldString} newString={newString} />
  }

  // Special rendering for TodoWrite tool
  const todos = getInput("todos")
  if (toolNameLower === "todowrite" && todos && Array.isArray(todos)) {
    return <TodoWriteToolBlock todos={todos} />
  }

  // Special rendering for AskUserQuestion tool
  const questions = getInput("questions")
  if (toolNameLower === "askuserquestion" && questions && Array.isArray(questions)) {
    return <AskUserQuestionToolBlock questions={questions} />
  }

  // Special rendering for Glob tool
  const globPattern = getInput("pattern") || toolInput?.glob
  if (toolNameLower === "glob" && globPattern) {
    return <GlobToolBlock pattern={globPattern} />
  }

  // Special rendering for WebSearch tool
  const searchQuery = getInput("query")
  if (toolNameLower === "websearch" && searchQuery) {
    return <WebSearchToolBlock query={searchQuery} />
  }

  // Special rendering for Bash tool
  const command = getInput("command")
  if (toolNameLower === "bash" && command) {
    if (isCompact) {
      return <CompactBashBlock command={command} />
    }
    return <BashToolBlock command={command} />
  }

  // Special rendering for Knowledge Query tool
  const knowledgeQuery = getInput("query")
  const articleIds = getInput("article_ids")
  if (toolNameLower === "mcp__knowledge__query_integration_knowledge" && knowledgeQuery) {
    return <KnowledgeQueryToolBlock query={knowledgeQuery} articleIds={articleIds} />
  }

  // Special rendering for Create Agent Task tool (handles both direct handover and inbox task)
  const taskMessage = getInput("task_message")
  if (toolNameLower === "mcp__task__create_agent_task" && taskMessage) {
    return (
      <AgentHandoverToolBlock
        targetAgentId={getInput("target_agent_id")}
        targetAgentName={getInput("target_agent_name")}
        taskMessage={taskMessage}
      />
    )
  }

  // Special rendering for Update Session State tool
  const sessionState = getInput("state")
  if (toolNameLower === "mcp__task__update_session_state" && sessionState) {
    return <UpdateSessionStateToolBlock state={sessionState} summary={getInput("summary")} />
  }

  // Default rendering for other tools
  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <Wrench className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="font-medium text-foreground/90 mb-1">
          Using tool: <code className="font-mono bg-muted px-1.5 py-0.5 rounded text-xs">{toolName}</code>
        </div>
        {toolInput && Object.keys(toolInput).length > 0 && (
          <div className="space-y-1 text-xs">
            {Object.entries(toolInput).map(([key, value]) => (
              <div key={key} className="flex flex-col gap-0.5">
                <span className="font-semibold text-foreground/80">{key}:</span>
                <div className="pl-3 text-foreground/70">
                  {typeof value === 'string' ? (
                    // Check if the value contains markdown-like content (code blocks, lists, etc.)
                    value.includes('\n') || value.includes('```') || value.includes('- ') ? (
                      <MarkdownRenderer
                        content={value}
                        className="prose prose-xs dark:prose-invert max-w-none"
                      />
                    ) : (
                      <span className="whitespace-pre-wrap break-words">{value}</span>
                    )
                  ) : (
                    <pre className="whitespace-pre-wrap break-words font-mono">
                      {JSON.stringify(value, null, 2)}
                    </pre>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
