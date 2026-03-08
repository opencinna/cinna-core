import { useState, useEffect, useRef, useCallback } from "react"
import { MessageCircle, X, Send, Loader2, Square } from "lucide-react"
import { Button } from "@/components/ui/button"
import { MessageBubble } from "@/components/Chat/MessageBubble"
import { StreamingMessage } from "@/components/Chat/StreamingMessage"
import { eventService } from "@/services/eventService"
import type { StreamEvent } from "@/hooks/useSessionStreaming"
import type { MessagePublic } from "@/client"

const API_URL = import.meta.env.VITE_API_URL
const WEBAPP_TOKEN_KEY = "webapp_access_token"

interface WebappChatWidgetProps {
  webappToken: string
  chatMode: "conversation" | "building"
  agentName: string
}

function getWebappJwt(): string | null {
  return localStorage.getItem(WEBAPP_TOKEN_KEY)
}

async function chatFetch(path: string, options: RequestInit = {}) {
  const jwt = getWebappJwt()
  if (!jwt) throw new Error("Not authenticated")
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${jwt}`,
      ...options.headers,
    },
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status}: ${text}`)
  }
  return res.json()
}

export function WebappChatWidget({
  webappToken,
  chatMode,
  agentName,
}: WebappChatWidgetProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<MessagePublic[]>([])
  const [isLoadingSession, setIsLoadingSession] = useState(false)
  const [isLoadingMessages, setIsLoadingMessages] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [hasUnread, setHasUnread] = useState(false)
  const [inputValue, setInputValue] = useState("")
  const [error, setError] = useState<string | null>(null)

  // Streaming state
  const [streamingEvents, setStreamingEvents] = useState<StreamEvent[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const lastKnownSeqRef = useRef<number>(0)
  const streamSubscriptionRef = useRef<string | null>(null)
  const streamRoomRef = useRef<string | null>(null)
  const sessionStatusSubRef = useRef<string | null>(null)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const basePath = `/webapp/${webappToken}/chat`

  // Auto-scroll to bottom
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [])

  useEffect(() => {
    scrollToBottom()
  }, [messages, streamingEvents, scrollToBottom])

  // Fetch existing session on first open
  useEffect(() => {
    if (!isOpen || sessionId || isLoadingSession) return
    loadExistingSession()
  }, [isOpen])

  // Subscribe to session interaction_status changes to track streaming
  useEffect(() => {
    if (!sessionId) return

    const subId = eventService.subscribe(
      "session_interaction_status_changed",
      (event: any) => {
        if (event.model_id !== sessionId && event.meta?.session_id !== sessionId) return
        const newStatus = event.meta?.interaction_status || event.text_content
        if (newStatus === "running" || newStatus === "pending_stream") {
          setIsStreaming(true)
        } else if (newStatus === "" || newStatus === undefined) {
          // Stream ended
          setIsStreaming(false)
          setStreamingEvents([])
          lastKnownSeqRef.current = 0
          refreshMessages()
          if (!isOpen) setHasUnread(true)
        }
      }
    )
    sessionStatusSubRef.current = subId

    return () => {
      eventService.unsubscribe(subId)
      sessionStatusSubRef.current = null
    }
  }, [sessionId, isOpen])

  // Subscribe to stream events when streaming starts
  useEffect(() => {
    if (!sessionId || !isStreaming) {
      // Cleanup stream subscriptions when not streaming
      if (streamSubscriptionRef.current) {
        eventService.unsubscribe(streamSubscriptionRef.current)
        streamSubscriptionRef.current = null
      }
      if (streamRoomRef.current) {
        eventService.unsubscribeFromRoom(streamRoomRef.current)
        streamRoomRef.current = null
      }
      return
    }

    // Subscribe to streaming room
    const streamRoom = `session_${sessionId}_stream`
    streamRoomRef.current = streamRoom
    eventService.subscribeToRoom(streamRoom)

    const subId = eventService.subscribe("stream_event", (event: any) => {
      const { session_id, data } = event
      if (session_id !== sessionId) return

      const seq = data?.event_seq ?? event.event_seq
      if (!seq) return
      if (seq <= lastKnownSeqRef.current) return

      lastKnownSeqRef.current = seq
      const streamEvent: StreamEvent = {
        type: data?.type || event.event_type,
        content: data?.content || "",
        event_seq: seq,
        tool_name: data?.tool_name,
        metadata: data?.metadata,
      }
      setStreamingEvents((prev) => [...prev, streamEvent])
    })
    streamSubscriptionRef.current = subId

    return () => {
      eventService.unsubscribe(subId)
      streamSubscriptionRef.current = null
      eventService.unsubscribeFromRoom(streamRoom)
      streamRoomRef.current = null
    }
  }, [sessionId, isStreaming])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (streamSubscriptionRef.current) {
        eventService.unsubscribe(streamSubscriptionRef.current)
      }
      if (streamRoomRef.current) {
        eventService.unsubscribeFromRoom(streamRoomRef.current)
      }
      if (sessionStatusSubRef.current) {
        eventService.unsubscribe(sessionStatusSubRef.current)
      }
    }
  }, [])

  async function loadExistingSession() {
    setIsLoadingSession(true)
    setError(null)
    try {
      const session = await chatFetch(`${basePath}/sessions`)
      if (session && session.id) {
        setSessionId(session.id)
        setIsStreaming(
          session.interaction_status === "running" ||
            session.interaction_status === "pending_stream"
        )
        await loadMessages(session.id)
      }
    } catch (e: any) {
      console.error("Failed to load chat session:", e)
    } finally {
      setIsLoadingSession(false)
    }
  }

  async function loadMessages(sid: string) {
    setIsLoadingMessages(true)
    try {
      const data = await chatFetch(`${basePath}/sessions/${sid}/messages`)
      setMessages(data.data || [])
    } catch (e: any) {
      console.error("Failed to load messages:", e)
    } finally {
      setIsLoadingMessages(false)
    }
  }

  async function refreshMessages() {
    if (!sessionId) return
    await loadMessages(sessionId)
  }

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId

    const session = await chatFetch(`${basePath}/sessions`, {
      method: "POST",
    })
    setSessionId(session.id)
    return session.id
  }

  async function handleSend() {
    const content = inputValue.trim()
    if (!content || isSending) return

    setIsSending(true)
    setError(null)
    setInputValue("")
    setStreamingEvents([])
    lastKnownSeqRef.current = 0

    try {
      const sid = await ensureSession()

      // Subscribe to streaming room before sending
      const streamRoom = `session_${sid}_stream`
      streamRoomRef.current = streamRoom
      await eventService.subscribeToRoom(streamRoom)

      // Optimistically add user message
      const tempMsg: MessagePublic = {
        id: `temp-${Date.now()}`,
        session_id: sid,
        role: "user",
        content,
        sequence_number: messages.length + 1,
        timestamp: new Date().toISOString(),
        message_metadata: {},
        tool_questions_status: null,
        answers_to_message_id: null,
        status: "",
        status_message: null,
        sent_to_agent_status: "pending",
        files: [],
      } as any
      setMessages((prev) => [...prev, tempMsg])

      // Send message
      const result = await chatFetch(
        `${basePath}/sessions/${sid}/messages/stream`,
        {
          method: "POST",
          body: JSON.stringify({ content, file_ids: [] }),
        }
      )

      if (result.streaming) {
        setIsStreaming(true)
      }

      // Refresh to get real message
      setTimeout(() => loadMessages(sid), 300)
    } catch (e: any) {
      console.error("Failed to send message:", e)
      setError("Failed to send message. Please try again.")
      // Remove optimistic message
      setMessages((prev) =>
        prev.filter((m) => !(m.id as string).startsWith("temp-"))
      )
    } finally {
      setIsSending(false)
    }
  }

  async function handleInterrupt() {
    if (!sessionId) return
    try {
      await chatFetch(`${basePath}/sessions/${sessionId}/messages/interrupt`, {
        method: "POST",
      })
    } catch (e) {
      console.error("Failed to interrupt:", e)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const conversationModeUi = chatMode === "conversation" ? "compact" : "detailed"

  return (
    <>
      {/* Chat FAB */}
      {!isOpen && (
        <button
          onClick={() => {
            setIsOpen(true)
            setHasUnread(false)
          }}
          className="fixed bottom-4 right-4 z-50 h-12 w-12 rounded-full bg-primary text-primary-foreground shadow-lg hover:bg-primary/90 flex items-center justify-center transition-transform hover:scale-105"
        >
          <MessageCircle className="h-5 w-5" />
          {hasUnread && (
            <span className="absolute -top-1 -right-1 h-3 w-3 rounded-full bg-destructive" />
          )}
        </button>
      )}

      {/* Chat Panel */}
      {isOpen && (
        <div className="fixed bottom-4 right-4 z-50 w-96 max-w-[calc(100vw-2rem)] flex flex-col bg-background border rounded-xl shadow-xl overflow-hidden"
          style={{ height: "min(500px, calc(100vh - 6rem))" }}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b bg-muted/30 shrink-0">
            <div className="flex items-center gap-2 min-w-0">
              <MessageCircle className="h-4 w-4 text-primary shrink-0" />
              <span className="text-sm font-medium truncate">{agentName}</span>
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0 ${
                  chatMode === "conversation"
                    ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                    : "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300"
                }`}
              >
                {chatMode === "conversation" ? "Chat" : "Building"}
              </span>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 shrink-0"
              onClick={() => setIsOpen(false)}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-1">
            {isLoadingSession || isLoadingMessages ? (
              <div className="flex items-center justify-center h-full">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : messages.length === 0 && !isStreaming ? (
              <div className="flex items-center justify-center h-full">
                <p className="text-sm text-muted-foreground text-center px-4">
                  {chatMode === "conversation"
                    ? "Ask questions about the data or request view changes"
                    : "Request new widgets, charts, or dashboard modifications"}
                </p>
              </div>
            ) : (
              <>
                {messages.map((msg) => (
                  <MessageBubble
                    key={msg.id}
                    message={msg}
                    conversationModeUi={conversationModeUi}
                    onSendMessage={(content) => {
                      setInputValue(content)
                      handleSend()
                    }}
                  />
                ))}
                {isStreaming && (
                  <StreamingMessage
                    events={streamingEvents}
                    conversationModeUi={conversationModeUi}
                  />
                )}
              </>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Error */}
          {error && (
            <div className="px-4 py-1.5 text-xs text-destructive bg-destructive/5 border-t">
              {error}
            </div>
          )}

          {/* Input */}
          <div className="border-t px-3 py-2.5 shrink-0">
            <div className="flex items-end gap-2">
              <textarea
                ref={textareaRef}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Type a message..."
                rows={1}
                disabled={isSending}
                className="flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary min-h-[36px] max-h-[100px]"
                style={{ height: "36px" }}
                onInput={(e) => {
                  const target = e.target as HTMLTextAreaElement
                  target.style.height = "36px"
                  target.style.height = `${Math.min(target.scrollHeight, 100)}px`
                }}
              />
              {isStreaming ? (
                <Button
                  size="icon"
                  variant="outline"
                  className="h-9 w-9 shrink-0"
                  onClick={handleInterrupt}
                  title="Stop"
                >
                  <Square className="h-3.5 w-3.5" />
                </Button>
              ) : (
                <Button
                  size="icon"
                  className="h-9 w-9 shrink-0"
                  onClick={handleSend}
                  disabled={!inputValue.trim() || isSending}
                >
                  {isSending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Send className="h-4 w-4" />
                  )}
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
