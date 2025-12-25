import { formatDistanceToNow } from "date-fns"
import type { MessagePublic } from "@/client"
import ReactMarkdown from "react-markdown"
import { StreamEventRenderer } from "./StreamEventRenderer"
import { Info } from "lucide-react"

interface MessageBubbleProps {
  message: MessagePublic
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user"
  const isSystem = message.role === "system"

  if (isSystem) {
    return (
      <div className="flex justify-center my-4">
        <div className="bg-muted text-muted-foreground text-sm px-4 py-2 rounded-full max-w-md text-center">
          {message.content}
        </div>
      </div>
    )
  }

  // Extract metadata for display
  const model = message.message_metadata?.model as string | undefined
  const totalCost = message.message_metadata?.total_cost_usd as number | undefined
  const durationMs = message.message_metadata?.duration_ms as number | undefined
  const numTurns = message.message_metadata?.num_turns as number | undefined
  const streamingEvents = (message.message_metadata?.streaming_events || []) as any[]

  // Format UTC timestamp
  const utcTimestamp = new Date(message.timestamp + 'Z').toUTCString()
  const durationSec = durationMs ? (durationMs / 1000).toFixed(2) : null

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      <div
        className={`max-w-[70%] rounded-lg px-4 py-3 ${
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground"
        }`}
      >
        <div className="space-y-2">
          {isUser ? (
            <p className="whitespace-pre-wrap break-words">{message.content}</p>
          ) : (
            <StreamEventRenderer events={streamingEvents} />
          )}
          <div className="flex items-center justify-between gap-2">
            <p
              className={`text-xs ${
                isUser ? "text-primary-foreground/70" : "text-muted-foreground"
              }`}
            >
              {formatDistanceToNow(new Date(message.timestamp + 'Z'), {
                addSuffix: true,
              })}
            </p>
            {!isUser && (model || totalCost || durationSec || numTurns) && (
              <div
                className="cursor-help"
                title={[
                  `UTC Time: ${utcTimestamp}`,
                  model ? `Model: ${model}` : null,
                  totalCost ? `Cost: $${totalCost.toFixed(6)} USD` : null,
                  durationSec ? `Duration: ${durationSec}s` : null,
                  numTurns ? `Turns: ${numTurns}` : null,
                ]
                  .filter(Boolean)
                  .join('\n')}
              >
                <Info className="w-3.5 h-3.5 text-muted-foreground/40 hover:text-muted-foreground/70 transition-colors" />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
