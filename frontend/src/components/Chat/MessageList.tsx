import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { ArrowDown } from "lucide-react"
import type { MessagePublic } from "@/client"
import { MessageBubble } from "./MessageBubble"

interface MessageListProps {
  messages: MessagePublic[]
  isLoading?: boolean
}

export function MessageList({ messages, isLoading }: MessageListProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const [userHasScrolled, setUserHasScrolled] = useState(false)

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
    setUserHasScrolled(false)
  }

  const handleScroll = () => {
    if (!scrollContainerRef.current) return

    const { scrollTop, scrollHeight, clientHeight } = scrollContainerRef.current
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 100

    setShowScrollButton(!isNearBottom)

    // If user scrolls manually, mark as scrolled
    if (!isNearBottom) {
      setUserHasScrolled(true)
    }
  }

  // Auto-scroll on mount
  useEffect(() => {
    scrollToBottom()
  }, [])

  // Auto-scroll when new messages arrive (only if user hasn't manually scrolled up)
  useEffect(() => {
    if (!userHasScrolled) {
      scrollToBottom()
    }
  }, [messages.length, userHasScrolled])

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-muted-foreground">Loading messages...</p>
      </div>
    )
  }

  return (
    <div className="relative flex-1 overflow-hidden">
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto px-4 py-6"
      >
        {messages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p className="text-muted-foreground text-center">
              No messages yet. Start the conversation!
            </p>
          </div>
        ) : (
          <>
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      {showScrollButton && (
        <div className="absolute bottom-4 right-4">
          <Button
            size="icon"
            variant="secondary"
            onClick={scrollToBottom}
            className="rounded-full shadow-lg"
          >
            <ArrowDown className="h-4 w-4" />
          </Button>
        </div>
      )}
    </div>
  )
}
