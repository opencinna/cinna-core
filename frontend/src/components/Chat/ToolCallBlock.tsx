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
import { CompactBashBlock } from "./CompactBashBlock"

interface ToolCallBlockProps {
  toolName: string
  toolInput?: Record<string, any>
  conversationModeUi?: string
}

export function ToolCallBlock({ toolName, toolInput, conversationModeUi = "detailed" }: ToolCallBlockProps) {
  const isCompact = conversationModeUi === "compact"
  const toolNameLower = toolName.toLowerCase()

  // Special rendering for Read tool
  if (toolNameLower === "read" && toolInput?.file_path) {
    if (isCompact) {
      // Compact mode: just show filename
      const fileName = toolInput.file_path.split('/').pop() || toolInput.file_path
      return (
        <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
          <FileText className="h-3.5 w-3.5 flex-shrink-0" />
          <span>Reading file <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{fileName}</code></span>
        </div>
      )
    }
    return <ReadToolBlock filePath={toolInput.file_path} />
  }

  // Special rendering for Write tool
  if (toolNameLower === "write" && toolInput?.file_path && toolInput?.content) {
    return <WriteToolBlock filePath={toolInput.file_path} content={toolInput.content} />
  }

  // Special rendering for Edit tool
  if (toolNameLower === "edit" && toolInput?.file_path && toolInput?.old_string && toolInput?.new_string) {
    if (isCompact) {
      // Compact mode: just show filename
      const fileName = toolInput.file_path.split('/').pop() || toolInput.file_path
      return (
        <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
          <FileEdit className="h-3.5 w-3.5 flex-shrink-0" />
          <span>Editing file <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{fileName}</code> ...</span>
        </div>
      )
    }
    return <EditToolBlock filePath={toolInput.file_path} oldString={toolInput.old_string} newString={toolInput.new_string} />
  }

  // Special rendering for TodoWrite tool
  if (toolNameLower === "todowrite" && toolInput?.todos && Array.isArray(toolInput.todos)) {
    return <TodoWriteToolBlock todos={toolInput.todos} />
  }

  // Special rendering for AskUserQuestion tool
  if (toolNameLower === "askuserquestion" && toolInput?.questions && Array.isArray(toolInput.questions)) {
    return <AskUserQuestionToolBlock questions={toolInput.questions} />
  }

  // Special rendering for Glob tool
  if (toolNameLower === "glob" && toolInput?.pattern) {
    return <GlobToolBlock pattern={toolInput.pattern} />
  }

  // Special rendering for WebSearch tool
  if (toolNameLower === "websearch" && toolInput?.query) {
    return <WebSearchToolBlock query={toolInput.query} />
  }

  // Special rendering for Bash tool
  if (toolNameLower === "bash" && toolInput?.command) {
    if (isCompact) {
      return <CompactBashBlock command={toolInput.command} />
    }
    return <BashToolBlock command={toolInput.command} />
  }

  // Special rendering for Knowledge Query tool
  if (toolNameLower === "mcp__knowledge__query_integration_knowledge" && toolInput?.query) {
    return <KnowledgeQueryToolBlock query={toolInput.query} articleIds={toolInput.article_ids} />
  }

  // Special rendering for Create Agent Task tool (handles both direct handover and inbox task)
  if (toolNameLower === "mcp__task__create_agent_task" && toolInput?.task_message) {
    return (
      <AgentHandoverToolBlock
        targetAgentId={toolInput.target_agent_id}
        targetAgentName={toolInput.target_agent_name}
        taskMessage={toolInput.task_message}
      />
    )
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
