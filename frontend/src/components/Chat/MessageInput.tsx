import { useState, KeyboardEvent, forwardRef } from "react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Send, Square, Loader2 } from "lucide-react"
import { RotatingHints } from "@/components/Common/RotatingHints"

interface MessageInputProps {
  onSend: (content: string) => void
  onStop?: () => void
  sendDisabled?: boolean
  isInterruptPending?: boolean
  placeholder?: string
}

export const MessageInput = forwardRef<HTMLTextAreaElement, MessageInputProps>(
  function MessageInput({
    onSend,
    onStop,
    sendDisabled = false,
    isInterruptPending = false,
    placeholder = "Type your message...",
  }, ref) {
  const [message, setMessage] = useState("")

  const handleSend = () => {
    const trimmedMessage = message.trim()
    if (trimmedMessage && !sendDisabled) {
      onSend(trimmedMessage)
      setMessage("")
    }
  }

  const handleStop = () => {
    if (onStop) {
      onStop()
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Don't allow sending while streaming
    if (e.key === "Enter" && !e.shiftKey && !sendDisabled) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="border-t p-4 bg-background shrink-0">
      <div className="flex gap-2 items-end max-w-7xl mx-auto">
        <Textarea
          ref={ref}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className="min-h-[60px] max-h-[200px] resize-none"
          rows={2}
          disabled={sendDisabled}
        />
        {sendDisabled ? (
          // Show Stop button when streaming
          <Button
            onClick={handleStop}
            variant="destructive"
            size="icon"
            className="h-[60px] w-[60px] shrink-0"
            disabled={isInterruptPending}
          >
            {isInterruptPending ? (
              <Loader2 className="h-5 w-5 animate-spin" />
            ) : (
              <Square className="h-5 w-5" />
            )}
          </Button>
        ) : (
          // Show Send button when not streaming
          <Button
            onClick={handleSend}
            disabled={!message.trim()}
            size="icon"
            className="h-[60px] w-[60px] shrink-0"
          >
            <Send className="h-5 w-5" />
          </Button>
        )}
      </div>
      <RotatingHints className="mt-2 max-w-7xl mx-auto" />
    </div>
  )
})
