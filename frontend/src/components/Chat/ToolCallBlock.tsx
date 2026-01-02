import { Wrench } from "lucide-react"
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

interface ToolCallBlockProps {
  toolName: string
  toolInput?: Record<string, any>
}

export function ToolCallBlock({ toolName, toolInput }: ToolCallBlockProps) {
  // Special rendering for Read tool
  if (toolName === "Read" && toolInput?.file_path) {
    return <ReadToolBlock filePath={toolInput.file_path} />
  }

  // Special rendering for Write tool
  if (toolName === "Write" && toolInput?.file_path && toolInput?.content) {
    return <WriteToolBlock filePath={toolInput.file_path} content={toolInput.content} />
  }

  // Special rendering for Edit tool
  if (toolName === "Edit" && toolInput?.file_path && toolInput?.old_string && toolInput?.new_string) {
    return <EditToolBlock filePath={toolInput.file_path} oldString={toolInput.old_string} newString={toolInput.new_string} />
  }

  // Special rendering for TodoWrite tool
  if (toolName === "TodoWrite" && toolInput?.todos && Array.isArray(toolInput.todos)) {
    return <TodoWriteToolBlock todos={toolInput.todos} />
  }

  // Special rendering for AskUserQuestion tool
  if (toolName === "AskUserQuestion" && toolInput?.questions && Array.isArray(toolInput.questions)) {
    return <AskUserQuestionToolBlock questions={toolInput.questions} />
  }

  // Special rendering for Glob tool
  if (toolName === "Glob" && toolInput?.pattern) {
    return <GlobToolBlock pattern={toolInput.pattern} />
  }

  // Special rendering for WebSearch tool
  if (toolName === "WebSearch" && toolInput?.query) {
    return <WebSearchToolBlock query={toolInput.query} />
  }

  // Special rendering for Bash tool
  if (toolName === "Bash" && toolInput?.command) {
    return <BashToolBlock command={toolInput.command} />
  }

  // Special rendering for Knowledge Query tool
  if (toolName === "mcp__knowledge__query_integration_knowledge" && toolInput?.query) {
    return <KnowledgeQueryToolBlock query={toolInput.query} />
  }

  // Special rendering for Agent Handover tool
  if (
    toolName === "mcp__handover__agent_handover" &&
    toolInput?.target_agent_id &&
    toolInput?.target_agent_name &&
    toolInput?.handover_message
  ) {
    return (
      <AgentHandoverToolBlock
        targetAgentId={toolInput.target_agent_id}
        targetAgentName={toolInput.target_agent_name}
        handoverMessage={toolInput.handover_message}
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
