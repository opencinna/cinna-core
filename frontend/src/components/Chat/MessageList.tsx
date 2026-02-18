import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { ArrowDown } from "lucide-react"
import type { MessagePublic } from "@/client"
import { MessageBubble } from "./MessageBubble"
import { StreamingMessage } from "./StreamingMessage"
import type { StreamEvent } from "@/hooks/useSessionStreaming"

interface MessageListProps {
  messages: MessagePublic[]
  isLoading?: boolean
  streamingEvents?: StreamEvent[]
  isStreaming?: boolean
  onSendAnswer?: (content: string, answersToMessageId: string) => void
  onSendMessage?: (content: string) => void
  conversationModeUi?: string
  agentId?: string
  integrationTyp?: string | null
}

export function MessageList({ messages, isLoading, streamingEvents, isStreaming, onSendAnswer, onSendMessage, conversationModeUi = "detailed", agentId, integrationTyp }: MessageListProps) {
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

    // Track user scroll position - reset flag when user scrolls back to bottom
    setUserHasScrolled(!isNearBottom)
  }

  // Auto-scroll on mount
  useEffect(() => {
    scrollToBottom()
  }, [])

  // Auto-scroll when new messages arrive or streaming events update (only if user hasn't manually scrolled up)
  useEffect(() => {
    if (!userHasScrolled) {
      scrollToBottom()
    }
  }, [messages, streamingEvents, userHasScrolled])

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center min-h-0">
        <p className="text-muted-foreground">Loading messages...</p>
      </div>
    )
  }

  return (
    <div className="relative flex-1 overflow-hidden min-h-0">
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto px-6 py-6"
      >
        <div className="max-w-7xl mx-auto">
          {messages.length === 0 && !isStreaming ? (
            <div className="flex items-center justify-center min-h-[400px]">
              <p className="text-muted-foreground text-center">
                No messages yet. Start the conversation!
              </p>
            </div>
          ) : (
            <>
              {(() => {
                if (!isStreaming) {
                  return messages.map((message) => (
                    <MessageBubble
                      key={message.id}
                      message={message}
                      onSendAnswer={onSendAnswer}
                      onSendMessage={onSendMessage}
                      conversationModeUi={conversationModeUi}
                      agentId={agentId}
                      integrationTyp={integrationTyp}
                    />
                  ))
                }

                // During streaming, find the in-progress message's sequence number
                // so we can also defer messages created after it (e.g. delegation/system
                // messages created by tool calls during streaming)
                const streamingMsg = messages.find((m) => {
                  const metadata = m.message_metadata as Record<string, any> | undefined
                  return metadata?.streaming_in_progress
                })
                const streamingSeq = streamingMsg?.sequence_number ?? Infinity

                const beforeStreaming = messages.filter((m) => {
                  const metadata = m.message_metadata as Record<string, any> | undefined
                  return !metadata?.streaming_in_progress && m.sequence_number < streamingSeq
                })
                const afterStreaming = messages.filter((m) => {
                  const metadata = m.message_metadata as Record<string, any> | undefined
                  return !metadata?.streaming_in_progress && m.sequence_number > streamingSeq
                })

                return (
                  <>
                    {beforeStreaming.map((message) => (
                      <MessageBubble
                        key={message.id}
                        message={message}
                        onSendAnswer={onSendAnswer}
                        onSendMessage={onSendMessage}
                        conversationModeUi={conversationModeUi}
                        agentId={agentId}
                      />
                    ))}
                    <StreamingMessage events={streamingEvents || []} conversationModeUi={conversationModeUi} />
                    {afterStreaming.map((message) => (
                      <MessageBubble
                        key={message.id}
                        message={message}
                        onSendAnswer={onSendAnswer}
                        onSendMessage={onSendMessage}
                        conversationModeUi={conversationModeUi}
                        agentId={agentId}
                      />
                    ))}
                  </>
                )
              })()}
              <div ref={messagesEndRef} />
            </>
          )}
        </div>
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
