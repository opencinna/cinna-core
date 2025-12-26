import { useState, KeyboardEvent } from "react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Send } from "lucide-react"
import { RotatingHints } from "@/components/Common/RotatingHints"

interface MessageInputProps {
  onSend: (content: string) => void
  sendDisabled?: boolean
  placeholder?: string
}

export function MessageInput({
  onSend,
  sendDisabled = false,
  placeholder = "Type your message...",
}: MessageInputProps) {
  const [message, setMessage] = useState("")

  const handleSend = () => {
    const trimmedMessage = message.trim()
    if (trimmedMessage && !sendDisabled) {
      onSend(trimmedMessage)
      setMessage("")
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="border-t p-4 bg-background shrink-0">
      <div className="flex gap-2 items-end max-w-7xl mx-auto">
        <Textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className="min-h-[60px] max-h-[200px] resize-none"
          rows={2}
        />
        <Button
          onClick={handleSend}
          disabled={sendDisabled || !message.trim()}
          size="icon"
          className="h-[60px] w-[60px] shrink-0"
        >
          <Send className="h-5 w-5" />
        </Button>
      </div>
      <RotatingHints className="mt-2 max-w-7xl mx-auto" />
    </div>
  )
}
