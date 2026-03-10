import { useState, useEffect, useRef, useCallback, type RefObject } from "react"
import { MessageCircle, X, Send, Loader2, Square } from "lucide-react"
import { Button } from "@/components/ui/button"
import { MessageBubble } from "@/components/Chat/MessageBubble"
import { StreamingMessage } from "@/components/Chat/StreamingMessage"
import { eventService } from "@/services/eventService"
import type { StreamEvent } from "@/hooks/useSessionStreaming"
import type { MessagePublic } from "@/client"

const API_URL = import.meta.env.VITE_API_URL
const WEBAPP_TOKEN_KEY = "webapp_access_token"
const WEBAPP_CHAT_CACHE_PREFIX = "webapp_chat_"

// ── Types ─────────────────────────────────────────────────────────────────

interface WebappChatWidgetProps {
  webappToken: string
  chatMode: "conversation" | "building"
  agentName: string
  iframeRef?: RefObject<HTMLIFrameElement | null>
}

interface WebappChatCache {
  sessionId: string
  messages: MessagePublic[]
  cachedAt: number
}

// ── Cache helpers ─────────────────────────────────────────────────────────

function getCacheKey(webappToken: string): string {
  return `${WEBAPP_CHAT_CACHE_PREFIX}${webappToken}`
}

function readCache(webappToken: string): WebappChatCache | null {
  try {
    const raw = localStorage.getItem(getCacheKey(webappToken))
    if (!raw) return null
    const parsed = JSON.parse(raw) as unknown
    if (
      parsed &&
      typeof parsed === "object" &&
      "sessionId" in parsed &&
      "messages" in parsed &&
      typeof (parsed as WebappChatCache).sessionId === "string" &&
      Array.isArray((parsed as WebappChatCache).messages)
    ) {
      return parsed as WebappChatCache
    }
    return null
  } catch {
    return null
  }
}

function writeCache(
  webappToken: string,
  sessionId: string,
  messages: MessagePublic[]
): void {
  try {
    const entry: WebappChatCache = { sessionId, messages, cachedAt: Date.now() }
    localStorage.setItem(getCacheKey(webappToken), JSON.stringify(entry))
  } catch {
    // Quota exceeded or storage unavailable — degrade gracefully
  }
}

function clearCache(webappToken: string): void {
  try {
    localStorage.removeItem(getCacheKey(webappToken))
  } catch {
    // Storage unavailable — ignore
  }
}

// ── Page context collection ────────────────────────────────────────────────

const MAX_SELECTED_TEXT_CHARS = 2_000
const PAGE_CONTEXT_TIMEOUT_MS = 500

/**
 * Collect schema.org microdata from the webapp iframe via postMessage.
 *
 * Sends a "request_page_context" message to the iframe's contentWindow and
 * waits up to PAGE_CONTEXT_TIMEOUT_MS ms for a "page_context_response" reply.
 * Returns null silently on timeout or if no iframe is available — the message
 * will still send, just without page context.
 */
async function collectIframeContext(
  iframeRef?: RefObject<HTMLIFrameElement | null>
): Promise<Record<string, unknown> | null> {
  const iframe = iframeRef?.current
  // Capture contentWindow into a narrowed non-nullable local so closures below
  // can reference it safely without repeated null checks.
  const contentWindow = iframe?.contentWindow
  if (!contentWindow) return null

  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", handler)
      resolve(null)
    }, PAGE_CONTEXT_TIMEOUT_MS)

    function handler(event: MessageEvent) {
      if (
        !event.data ||
        event.data.type !== "page_context_response" ||
        event.source !== contentWindow
      ) {
        return
      }
      clearTimeout(timer)
      window.removeEventListener("message", handler)
      resolve(event.data.context ?? null)
    }

    window.addEventListener("message", handler)
    contentWindow.postMessage({ type: "request_page_context" }, "*")
  })
}

/**
 * Build the page_context string to attach to a chat message.
 *
 * Combines the user's current text selection with schema.org microdata
 * scraped from the webapp iframe. Returns undefined if both are absent
 * so the field can be omitted from the request body entirely.
 */
async function buildPageContext(
  iframeRef?: RefObject<HTMLIFrameElement | null>
): Promise<string | undefined> {
  const rawSelection = window.getSelection()?.toString() ?? ""
  const selectedText = rawSelection.slice(0, MAX_SELECTED_TEXT_CHARS)

  const iframeContext = await collectIframeContext(iframeRef)

  if (!selectedText && !iframeContext) return undefined

  const payload: Record<string, unknown> = {}
  if (selectedText) payload.selected_text = selectedText
  if (iframeContext) {
    payload.page = {
      url: (iframeContext as any).url ?? "",
      title: (iframeContext as any).title ?? "",
    }
    const microdata = (iframeContext as any).microdata
    if (Array.isArray(microdata) && microdata.length > 0) {
      payload.microdata = microdata
    }
  }

  return Object.keys(payload).length > 0
    ? JSON.stringify(payload)
    : undefined
}

// ── Auth helper ───────────────────────────────────────────────────────────

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

// ── Component ─────────────────────────────────────────────────────────────

export function WebappChatWidget({
  webappToken,
  chatMode,
  agentName,
  iframeRef,
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

  // True when sessionId was restored from localStorage (needs background verify)
  const needsBackgroundVerifyRef = useRef(false)
  // Guards background verify so it only fires once per mount
  const backgroundVerifyDoneRef = useRef(false)

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

  // Scroll to bottom and focus input when widget opens
  useEffect(() => {
    if (isOpen) {
      if (messages.length > 0) scrollToBottom()
      setTimeout(() => textareaRef.current?.focus(), 0)
    }
  }, [isOpen]) // eslint-disable-line react-hooks/exhaustive-deps

  // Restore focus to textarea after it gets re-enabled (disabled during send)
  // or after streaming ends (iframe webapp actions can steal focus)
  const wasSendingRef = useRef(false)
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    if (wasSendingRef.current && !isSending) {
      textareaRef.current?.focus()
    }
    wasSendingRef.current = isSending
  }, [isSending])

  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming && isOpen) {
      textareaRef.current?.focus()
    }
    wasStreamingRef.current = isStreaming
  }, [isStreaming, isOpen])

  // ── Cache restore on mount ──────────────────────────────────────────────

  const cacheRestoredRef = useRef(false)

  useEffect(() => {
    if (cacheRestoredRef.current) return
    const cached = readCache(webappToken)
    if (!cached) return

    cacheRestoredRef.current = true
    setSessionId(cached.sessionId)
    setMessages(cached.messages)
    needsBackgroundVerifyRef.current = true

    // Signal that a prior session exists — show badge on FAB
    if (cached.messages.length > 0) {
      setHasUnread(true)
    }
  }, [webappToken])

  // ── Background verify after cache restore ───────────────────────────────
  // Runs once when sessionId was restored from cache (needsBackgroundVerifyRef).
  // Does NOT run when sessionId is set via ensureSession() during a send.

  useEffect(() => {
    if (!sessionId || !needsBackgroundVerifyRef.current || backgroundVerifyDoneRef.current) return
    backgroundVerifyDoneRef.current = true
    needsBackgroundVerifyRef.current = false
    loadExistingSession(true)
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Persist cache on state change ───────────────────────────────────────

  useEffect(() => {
    if (sessionId) {
      writeCache(webappToken, sessionId, messages)
    }
  }, [sessionId, messages, webappToken])

  // ── Fetch existing session on first open (no cache) ─────────────────────

  useEffect(() => {
    if (!isOpen || sessionId || isLoadingSession) return
    loadExistingSession(false)
  }, [isOpen]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Session interaction status subscription ─────────────────────────────

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

  // ── Stream event subscription ───────────────────────────────────────────

  useEffect(() => {
    if (!sessionId || !isStreaming) {
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

    const streamRoom = `session_${sessionId}_stream`
    streamRoomRef.current = streamRoom
    eventService.subscribeToRoom(streamRoom)

    const subId = eventService.subscribe("stream_event", (event: any) => {
      const { session_id, data } = event
      if (session_id !== sessionId) return

      const eventType = data?.type || event.event_type
      if (eventType === "stream_completed") {
        setIsStreaming(false)
        setStreamingEvents([])
        lastKnownSeqRef.current = 0
        refreshMessages()
        if (!isOpen) setHasUnread(true)
        return
      }

      // Forward webapp_action events to the iframe via postMessage.
      // These events are emitted by the agent when it wants to trigger a UI
      // action (e.g. refresh_page, update_form, show_notification).
      if (eventType === "webapp_action") {
        const action = data?.action ?? event.action
        const actionData = data?.data ?? event.data ?? {}
        if (action && iframeRef?.current?.contentWindow) {
          iframeRef.current.contentWindow.postMessage(
            { type: "webapp_action", action, data: actionData },
            "*"
          )
        }
        return
      }

      const seq = data?.event_seq ?? event.event_seq
      if (!seq) return
      if (seq <= lastKnownSeqRef.current) return

      lastKnownSeqRef.current = seq
      const streamEvent: StreamEvent = {
        type: eventType,
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

  // ── Cleanup on unmount ──────────────────────────────────────────────────

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

  // ── API functions ───────────────────────────────────────────────────────

  /**
   * Fetch or verify the active chat session.
   *
   * When called as a background verify (isBackgroundVerify=true), skips the
   * loading spinner so cached messages remain visible without flickering. If
   * the backend reports no active session, the local cache is cleared and state
   * is reset so the widget starts fresh.
   */
  async function loadExistingSession(isBackgroundVerify = false) {
    if (!isBackgroundVerify) {
      setIsLoadingSession(true)
    }
    setError(null)
    try {
      const session = await chatFetch(`${basePath}/sessions`)
      if (session && session.id) {
        setSessionId(session.id)
        setIsStreaming(
          session.interaction_status === "running" ||
            session.interaction_status === "pending_stream"
        )
        await loadMessages(session.id, isBackgroundVerify)
      } else if (!isBackgroundVerify) {
        // Only clear cache on explicit load, not background verify.
        // Background verify failures should preserve cached state so the
        // user can keep chatting after a page refresh.
        clearCache(webappToken)
        setSessionId(null)
        setMessages([])
      }
    } catch (e: any) {
      if (isBackgroundVerify) {
        // Silent failure — keep cached state, user can still interact
        console.error("Background session verify failed:", e)
      } else {
        console.error("Failed to load chat session:", e)
      }
    } finally {
      if (!isBackgroundVerify) {
        setIsLoadingSession(false)
      }
    }
  }

  async function loadMessages(sid: string, silent = false) {
    if (!silent) {
      setIsLoadingMessages(true)
    }
    try {
      const data = await chatFetch(`${basePath}/sessions/${sid}/messages`)
      setMessages(data.data || [])
    } catch (e: any) {
      console.error("Failed to load messages:", e)
    } finally {
      if (!silent) {
        setIsLoadingMessages(false)
      }
    }
  }

  async function refreshMessages() {
    if (!sessionId) return
    await loadMessages(sessionId, true)
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
      // Collect page context and ensure session concurrently.
      // Context collection has a short timeout and fails silently — the message
      // always sends even if context collection fails or times out.
      const [sid, pageContext] = await Promise.all([
        ensureSession(),
        buildPageContext(iframeRef),
      ])

      // Subscribe to streaming room before sending
      const streamRoom = `session_${sid}_stream`
      streamRoomRef.current = streamRoom
      await eventService.subscribeToRoom(streamRoom)

      // Optimistically add user message (shows user's typed text, not augmented content)
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

      // Build request body — include page_context only if present
      const requestBody: Record<string, unknown> = { content, file_ids: [] }
      if (pageContext) requestBody.page_context = pageContext

      // Send message
      const result = await chatFetch(
        `${basePath}/sessions/${sid}/messages/stream`,
        {
          method: "POST",
          body: JSON.stringify(requestBody),
        }
      )

      if (result.streaming) {
        setIsStreaming(true)
      }

      // Refresh to get real message (silent to avoid spinner flash)
      setTimeout(() => loadMessages(sid, true), 300)
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
        <div className="fixed bottom-4 right-4 z-50 w-[460px] max-w-[calc(100vw-2rem)] flex flex-col bg-background border rounded-xl shadow-xl overflow-hidden"
          style={{ height: "min(600px, calc(100vh - 6rem))" }}
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
                className="flex-1 resize-none rounded-lg border bg-background px-3 py-1.5 text-sm leading-snug focus:outline-none focus:ring-1 focus:ring-primary min-h-[36px] max-h-[100px]"
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
