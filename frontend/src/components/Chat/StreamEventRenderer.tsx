import { Lightbulb } from "lucide-react"
import { ToolCallBlock } from "./ToolCallBlock"
import { MarkdownRenderer } from "./MarkdownRenderer"

interface StreamEvent {
  type: "assistant" | "tool" | "thinking" | "system"
  content: string
  tool_name?: string
  metadata?: {
    tool_id?: string
    tool_input?: Record<string, any>
    model?: string
    interrupt_notification?: boolean
  }
}

interface StreamEventRendererProps {
  events: StreamEvent[]
  conversationModeUi?: string
}

export function StreamEventRenderer({ events, conversationModeUi = "detailed" }: StreamEventRendererProps) {
  if (!events || events.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      {events.map((event, idx) => {
        if (event.type === "tool") {
          // Render tool call with structured fields
          return (
            <ToolCallBlock
              key={idx}
              toolName={event.tool_name || "Unknown Tool"}
              toolInput={event.metadata?.tool_input}
              conversationModeUi={conversationModeUi}
            />
          )
        } else if (event.type === "assistant" && event.content.trim()) {
          // Render assistant text with markdown support
          return (
            <MarkdownRenderer
              key={idx}
              content={event.content}
              className="prose dark:prose-invert max-w-none prose-p:leading-normal prose-p:my-2 prose-ul:my-2 prose-li:my-0"
            />
          )
        } else if (event.type === "thinking" && event.content.trim() && conversationModeUi !== "compact") {
          // Render thinking block - strip [Thinking] prefix if present (hidden in compact mode)
          const thinkingContent = event.content.replace(/^\[Thinking\]\s*/i, "")
          return (
            <div key={idx} className="flex items-start gap-2 text-xs text-muted-foreground bg-muted/50 rounded px-3 py-2">
              <Lightbulb className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <MarkdownRenderer
                content={thinkingContent}
                className="prose dark:prose-invert max-w-none text-xs prose-p:text-xs prose-p:leading-tight prose-p:my-0.5 prose-ul:my-0.5 prose-ul:text-xs prose-li:my-0 prose-li:leading-tight prose-headings:text-xs prose-headings:my-1 prose-code:text-xs"
              />
            </div>
          )
        } else if (event.type === "system" && event.content.trim()) {
          // Render system notification (e.g., interrupt notifications)
          const isInterruptNotification = event.metadata?.interrupt_notification
          return (
            <div
              key={idx}
              className={`text-sm px-3 py-2 rounded ${
                isInterruptNotification
                  ? "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-200 border border-yellow-200 dark:border-yellow-800/30"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              {event.content}
            </div>
          )
        }
        return null
      })}
    </div>
  )
}
