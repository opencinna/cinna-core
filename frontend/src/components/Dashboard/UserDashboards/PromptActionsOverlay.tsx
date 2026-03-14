import { useState, useEffect, useRef, type RefObject } from "react"
import { useNavigate } from "@tanstack/react-router"
import { Loader2, MessageCircle } from "lucide-react"

import type { UserDashboardBlockPromptActionPublic } from "@/client"
import { DashboardsService, SessionsService, MessagesService } from "@/client"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import useCustomToast from "@/hooks/useCustomToast"
import { buildPageContext } from "@/utils/webappContext"
import { eventService } from "@/services/eventService"
import type { StreamEvent } from "@/hooks/useSessionStreaming"
import { StreamingMessage } from "@/components/Chat/StreamingMessage"

interface PromptActionsOverlayProps {
  actions: UserDashboardBlockPromptActionPublic[]
  agentId: string
  blockId: string
  dashboardId: string
  isVisible: boolean
  isWebApp: boolean
  iframeRef?: RefObject<HTMLIFrameElement | null>
  onStreamComplete?: () => void
}

export function PromptActionsOverlay({
  actions,
  agentId,
  blockId,
  dashboardId,
  isVisible,
  isWebApp,
  iframeRef,
  onStreamComplete,
}: PromptActionsOverlayProps) {
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()

  // Which action is currently being sent
  const [pendingActions, setPendingActions] = useState<Record<string, boolean>>({})
  // Session for this block (resolved on first action click, or loaded on mount)
  const [sessionId, setSessionId] = useState<string | null>(null)
  // Streaming state
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingEvents, setStreamingEvents] = useState<StreamEvent[]>([])
  const lastKnownSeqRef = useRef<number>(0)
  const streamSubscriptionRef = useRef<string | null>(null)
  const streamRoomRef = useRef<string | null>(null)
  const sessionStatusSubRef = useRef<string | null>(null)

  // Check for existing block session on mount
  useEffect(() => {
    let cancelled = false
    DashboardsService.getBlockLatestSession({ dashboardId, blockId })
      .then((session) => {
        if (!cancelled && session?.id) {
          setSessionId(session.id)
        }
      })
      .catch(() => {
        // No recent session — that's fine
      })
    return () => { cancelled = true }
  }, [dashboardId, blockId])

  // ── Session interaction status subscription ─────────────────────────────
  useEffect(() => {
    if (!sessionId) return

    const subId = eventService.subscribe(
      "session_interaction_status_changed",
      (event: any) => {
        if (event.model_id !== sessionId && event.meta?.session_id !== sessionId) return
        const newStatus = event.meta?.interaction_status ?? event.text_content ?? ""
        if (newStatus === "running" || newStatus === "pending_stream") {
          setIsStreaming(true)
        } else if (newStatus === "" || newStatus === undefined) {
          setIsStreaming(false)
          setStreamingEvents([])
          lastKnownSeqRef.current = 0
          onStreamComplete?.()
        }
      }
    )
    sessionStatusSubRef.current = subId

    return () => {
      eventService.unsubscribe(subId)
      sessionStatusSubRef.current = null
    }
  }, [sessionId])

  // ── Stream event subscription ───────────────────────────────────────────
  useEffect(() => {
    if (!sessionId || !isStreaming) {
      if (streamSubscriptionRef.current) {
        eventService.unsubscribe(streamSubscriptionRef.current)
        streamSubscriptionRef.current = null
      }
      if (streamRoomRef.current && !isStreaming) {
        eventService.unsubscribeFromRoom(streamRoomRef.current)
        streamRoomRef.current = null
      }
      return
    }

    const streamRoom = `session_${sessionId}_stream`
    if (!streamRoomRef.current) {
      streamRoomRef.current = streamRoom
      eventService.subscribeToRoom(streamRoom)
    }

    const subId = eventService.subscribe("stream_event", (event: any) => {
      const { session_id, data } = event
      if (session_id !== sessionId) return

      const eventType = data?.type || event.event_type
      if (eventType === "stream_completed") {
        setIsStreaming(false)
        setStreamingEvents([])
        lastKnownSeqRef.current = 0
        onStreamComplete?.()
        return
      }

      // Forward webapp_action events to the iframe
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

  const getDisplayLabel = (action: UserDashboardBlockPromptActionPublic): string => {
    if (action.label) return action.label
    const text = action.prompt_text
    return text.length > 28 ? text.slice(0, 26) + "…" : text
  }

  const anyPending = Object.values(pendingActions).some(Boolean)

  const handleActionClick = async (action: UserDashboardBlockPromptActionPublic) => {
    if (pendingActions[action.id] || isStreaming) return

    setPendingActions((prev) => ({ ...prev, [action.id]: true }))
    try {
      // Use existing sessionId if available, otherwise resolve
      const sid = sessionId ?? (await resolveSession(agentId, blockId, dashboardId)).id
      setSessionId(sid)

      // Collect page context concurrently with room subscription
      const [pageContext] = await Promise.all([
        isWebApp ? buildPageContext(iframeRef) : Promise.resolve(undefined),
        // Subscribe to streaming room before sending (only if not already subscribed)
        streamRoomRef.current
          ? Promise.resolve()
          : (async () => {
              const streamRoom = `session_${sid}_stream`
              streamRoomRef.current = streamRoom
              await eventService.subscribeToRoom(streamRoom)
            })(),
      ])

      // Build request body
      const requestBody: { content: string; file_ids?: string[]; page_context?: string | null } = {
        content: action.prompt_text,
        file_ids: [],
      }
      if (pageContext) requestBody.page_context = pageContext

      // Send message via regular authenticated API
      const result = (await MessagesService.sendMessageStream({
        sessionId: sid,
        requestBody,
      })) as { streaming?: boolean } | undefined

      if (result?.streaming) {
        setIsStreaming(true)
      }
    } catch {
      showErrorToast("Failed to send prompt action. Please try again.")
    } finally {
      setPendingActions((prev) => {
        const next = { ...prev }
        delete next[action.id]
        return next
      })
    }
  }

  const handleOpenSession = () => {
    if (sessionId) {
      navigate({
        to: "/session/$sessionId",
        params: { sessionId },
        search: {
          initialMessage: undefined,
          fileIds: undefined,
          fileObjects: undefined,
          pageContext: undefined,
        },
      })
    }
  }

  // Don't render if no actions and not streaming
  if (!actions.length && !isStreaming) return null

  return (
    <div
      className={cn(
        "absolute inset-x-0 bottom-0 flex flex-col gap-1.5",
        "bg-background/85 backdrop-blur-sm border-t border-border/50",
        "transition-opacity duration-150",
        isVisible || isStreaming ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none",
      )}
    >
      {/* Streaming indicator - shows when a prompt action response is being processed */}
      {isStreaming && streamingEvents.length > 0 && (
        <div className="px-2 pt-1.5 max-h-[120px] overflow-y-auto">
          <StreamingMessage events={streamingEvents} conversationModeUi="compact" />
        </div>
      )}
      {isStreaming && streamingEvents.length === 0 && (
        <div className="flex items-center gap-1.5 px-2 pt-1.5">
          <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
          <span className="text-xs text-muted-foreground">Processing...</span>
        </div>
      )}

      {/* Action buttons bar */}
      <div className="flex items-center gap-1.5 p-2">
        {/* Left side: chat bubble icon - clickable to open session */}
        {sessionId ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0 shrink-0"
            title="Open session"
            onClick={handleOpenSession}
          >
            <MessageCircle className="h-4 w-4 text-primary" />
          </Button>
        ) : (
          <MessageCircle className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}

        {/* Prompt action buttons */}
        {actions.map((action) => {
          const isPending = pendingActions[action.id]

          return (
            <Button
              key={action.id}
              type="button"
              variant="outline"
              size="sm"
              title={action.prompt_text}
              aria-label={action.prompt_text}
              disabled={isPending || isStreaming || anyPending}
              onClick={() => handleActionClick(action)}
              className="h-6 text-xs px-2 py-0 rounded-full"
            >
              {isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                getDisplayLabel(action)
              )}
            </Button>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Resolve the session to use for a prompt action click.
 */
async function resolveSession(
  agentId: string,
  blockId: string,
  dashboardId: string,
): Promise<{ id: string }> {
  try {
    const recent = await DashboardsService.getBlockLatestSession({
      dashboardId,
      blockId,
    })
    return { id: recent.id }
  } catch {
    const session = await SessionsService.createSession({
      requestBody: {
        agent_id: agentId,
        mode: "conversation",
        dashboard_block_id: blockId,
      },
    })
    return { id: session.id }
  }
}
