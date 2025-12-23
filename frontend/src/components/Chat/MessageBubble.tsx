import { formatDistanceToNow } from "date-fns"
import type { MessagePublic } from "@/client"
import ReactMarkdown from "react-markdown"

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
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <ReactMarkdown>{message.content}</ReactMarkdown>
            </div>
          )}
          <p
            className={`text-xs ${
              isUser ? "text-primary-foreground/70" : "text-muted-foreground"
            }`}
          >
            {formatDistanceToNow(new Date(message.timestamp), {
              addSuffix: true,
            })}
          </p>
        </div>
      </div>
    </div>
  )
}
