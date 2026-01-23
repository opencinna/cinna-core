import { Loader2 } from "lucide-react"
import { StreamEventRenderer } from "./StreamEventRenderer"
import type { StreamEvent } from "@/hooks/useSessionStreaming"

interface StreamingMessageProps {
  events: StreamEvent[]
  conversationModeUi?: string
}

export function StreamingMessage({ events, conversationModeUi = "detailed" }: StreamingMessageProps) {
  return (
    <div className="flex justify-start mb-4">
      <div className="max-w-[70%] rounded-lg px-4 py-3 bg-muted text-foreground">
        <div className="space-y-2">
          {events.length > 0 ? (
            <StreamEventRenderer events={events} conversationModeUi={conversationModeUi} />
          ) : (
            <div className="flex items-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-sm">Thinking...</span>
            </div>
          )}
          <div className="flex items-center gap-2">
            <div className="flex gap-1">
              <span className="w-2 h-2 bg-primary rounded-full animate-pulse" style={{ animationDelay: "0ms" }} />
              <span className="w-2 h-2 bg-primary rounded-full animate-pulse" style={{ animationDelay: "150ms" }} />
              <span className="w-2 h-2 bg-primary rounded-full animate-pulse" style={{ animationDelay: "300ms" }} />
            </div>
            <p className="text-xs text-muted-foreground">Streaming...</p>
          </div>
        </div>
      </div>
    </div>
  )
}
