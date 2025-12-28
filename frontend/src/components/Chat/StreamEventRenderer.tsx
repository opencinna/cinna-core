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
}

export function StreamEventRenderer({ events }: StreamEventRendererProps) {
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
        } else if (event.type === "thinking" && event.content.trim()) {
          // Render thinking block
          return (
            <div key={idx} className="text-xs italic text-muted-foreground bg-muted/50 rounded px-3 py-2">
              {event.content}
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
