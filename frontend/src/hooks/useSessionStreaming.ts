import { useState, useCallback, useRef, useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { eventService } from "@/services/eventService"

export interface StreamEvent {
  type:
    | "assistant"
    | "tool"
    | "thinking"
    | "system"
    | "webapp_action"
    | "tool_result_delta"
    | "attachment"
    | "attachment_error"
  content: string
  event_seq: number
  tool_name?: string
  metadata?: {
    tool_id?: string
    tool_input?: Record<string, any>
    model?: string
    interrupt_notification?: boolean
    needs_approval?: boolean
    tool_name?: string
    synthesized?: boolean           // true for /run command bash events
    stream?: "stdout" | "stderr"    // present on tool_result_delta events
    exit_code?: number              // present on command done (system) events
    command_done?: boolean          // true on the final system event for a command
    action?: string
    data?: Record<string, unknown>
    // present on `attachment` events (agent-authored file attachments)
    file_id?: string
    filename?: string
    mime_type?: string
    size?: number
    agent_env_path?: string
  }
}

interface UseSessionStreamingOptions {
  sessionId: string
  session?: { interaction_status?: string; mode?: string } | null
  messagesData?: { data?: Array<{ message_metadata?: Record<string, any> }> } | null
  onSuccess?: () => void
  onError?: (error: Error) => void
}

export function useSessionStreaming({
  sessionId,
  session,
  messagesData,
  onSuccess,
  onError,
}: UseSessionStreamingOptions) {
  const queryClient = useQueryClient()
  const [streamingEvents, setStreamingEvents] = useState<StreamEvent[]>([])
  const [isPending, setIsPending] = useState(false)
  const [isInterruptPending, setIsInterruptPending] = useState(false)

  // Derived streaming state from session
  const isStreaming =
    session?.interaction_status === "running" ||
    session?.interaction_status === "pending_stream"

  // Refs for tracking state across renders
  const lastKnownSeqRef = useRef<number>(0)
  const streamSubscriptionRef = useRef<string | null>(null)
  const streamRoomRef = useRef<string | null>(null)
  const wasStreamingRef = useRef(false)

  // Sync streaming events from DB (message refetch fills gaps)
  useEffect(() => {
    if (!messagesData?.data || !isStreaming) return

    // Find the in-progress message
    const inProgressMsg = messagesData.data.find(
      (m) => m.message_metadata?.streaming_in_progress
    )
    if (!inProgressMsg) return

    const dbEvents: StreamEvent[] =
      inProgressMsg.message_metadata?.streaming_events || []
    if (dbEvents.length === 0) return

    const maxDbSeq = Math.max(...dbEvents.map((e) => e.event_seq || 0))
    if (maxDbSeq > lastKnownSeqRef.current) {
      // DB has events we don't have yet - merge them in
      setStreamingEvents((prev) => {
        const existingSeqs = new Set(prev.map((e) => e.event_seq))
        const newFromDb = dbEvents.filter(
          (e) => e.event_seq && !existingSeqs.has(e.event_seq)
        )
        const merged = [...prev, ...newFromDb].sort(
          (a, b) => a.event_seq - b.event_seq
        )
        return merged
      })
      lastKnownSeqRef.current = maxDbSeq
    }
  }, [messagesData, isStreaming])

  // Handle incoming WS stream event
  const handleStreamEvent = useCallback(
    (event: any) => {
      const { session_id, data } = event

      // Verify event is for current session
      if (session_id !== sessionId) return

      // Extract event_seq from data or top-level
      const seq = data?.event_seq ?? event.event_seq
      if (!seq) {
        // `attachment_error` notices carry no event_seq (they are not part of
        // the persisted streaming_events ordering) — render them inline using a
        // synthetic seq above the last known one so they always append.
        const incomingType = data?.type || event.event_type
        if (incomingType === "attachment_error") {
          const syntheticSeq = lastKnownSeqRef.current + 0.5
          setStreamingEvents((prev) => [
            ...prev,
            {
              type: "attachment_error",
              content: data?.content || "",
              event_seq: syntheticSeq,
              metadata: data?.metadata,
            },
          ])
          return
        }
        // Other seq-less events (stream_started, stream_completed, etc.)
        // are handled by session query refetch via session_interaction_status_changed
        return
      }

      // Duplicate detection
      if (seq <= lastKnownSeqRef.current) return

      // Gap detection - trigger message refetch
      if (seq > lastKnownSeqRef.current + 1) {
        queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
      }

      // Append event
      lastKnownSeqRef.current = seq
      const streamEvent: StreamEvent = {
        type: data?.type || event.event_type,
        content: data?.content || "",
        event_seq: seq,
        tool_name: data?.tool_name,
        metadata: data?.metadata,
      }
      setStreamingEvents((prev) => [...prev, streamEvent])
    },
    [sessionId, queryClient]
  )

  // React to isStreaming state changes
  useEffect(() => {
    if (isStreaming && !wasStreamingRef.current) {
      // Streaming just started - subscribe to WS room
      wasStreamingRef.current = true
      lastKnownSeqRef.current = 0
      setStreamingEvents([])

      const streamRoom = `session_${sessionId}_stream`
      streamRoomRef.current = streamRoom
      eventService.subscribeToRoom(streamRoom)

      const subId = eventService.subscribe("stream_event", handleStreamEvent)
      streamSubscriptionRef.current = subId
    } else if (!isStreaming && wasStreamingRef.current) {
      // Streaming just ended - cleanup and refetch
      wasStreamingRef.current = false

      // Cleanup subscriptions
      if (streamSubscriptionRef.current) {
        eventService.unsubscribe(streamSubscriptionRef.current)
        streamSubscriptionRef.current = null
      }
      if (streamRoomRef.current) {
        eventService.unsubscribeFromRoom(streamRoomRef.current)
        streamRoomRef.current = null
      }

      // Clear streaming state
      setStreamingEvents([])
      lastKnownSeqRef.current = 0
      setIsPending(false)
      setIsInterruptPending(false)

      // Refetch messages to get final content
      queryClient.refetchQueries({ queryKey: ["messages", sessionId] })
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
      queryClient.invalidateQueries({ queryKey: ["sessions"] })

      // Poll for AI-generated title
      let pollAttempt = 0
      const pollForTitle = async () => {
        pollAttempt++
        const currentSession = queryClient.getQueryData(["session", sessionId]) as any
        if (currentSession?.title) return
        await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
        await queryClient.invalidateQueries({ queryKey: ["sessions"] })
        const nextDelay = pollAttempt <= 3 ? 500 : 2000
        if (pollAttempt < 8) setTimeout(pollForTitle, nextDelay)
      }
      setTimeout(pollForTitle, 500)

      // Invalidate agent caches if building mode
      if (session?.mode === "building") {
        queryClient.invalidateQueries({ queryKey: ["agent"] })
        queryClient.invalidateQueries({ queryKey: ["agents"] })
      }

      onSuccess?.()
    }
  }, [isStreaming, sessionId, session?.mode, queryClient, handleStreamEvent, onSuccess])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (streamSubscriptionRef.current) {
        eventService.unsubscribe(streamSubscriptionRef.current)
      }
      if (streamRoomRef.current) {
        eventService.unsubscribeFromRoom(streamRoomRef.current)
      }
    }
  }, [])

  const sendMessage = useCallback(
    async (
      content: string,
      answersToMessageId?: string,
      fileIds?: string[],
      fileObjects?: Array<{
        id: string
        filename: string
        file_size: number
        mime_type: string
      }>,
      pageContext?: string
    ) => {
      setIsPending(true)
      // Only reset streaming events if not currently streaming — if the agent
      // is mid-response, preserve the live events so the UI stays consistent
      // until the next stream starts for this queued message.
      if (!isStreaming) {
        setStreamingEvents([])
        lastKnownSeqRef.current = 0
      }
      setIsInterruptPending(false)

      try {
        const token = localStorage.getItem("access_token")
        if (!token) {
          throw new Error("Not authenticated")
        }

        // Optimistically add user message to cache FIRST — before any WS/network
        // interaction — so the message is visible immediately and the REST POST
        // below is never gated on WebSocket health (WS is an enhancement, not a
        // requirement; polling recovers all streaming data from the DB).
        const tempUserMessageId = `temp-${Date.now()}`
        queryClient.setQueryData(["messages", sessionId], (old: any) => {
          if (!old) return old
          const newUserMessage = {
            id: tempUserMessageId,
            session_id: sessionId,
            role: "user",
            content: content,
            sequence_number: (old.data?.length || 0) + 1,
            timestamp: new Date().toISOString(),
            message_metadata: {},
            answers_to_message_id: answersToMessageId || null,
            files: fileObjects || [],
            // Mark as pending so the UI can show a visual indicator until the
            // backend confirms the message was delivered to the agent.
            sent_to_agent_status: "pending",
            tool_questions_status: null,
            status: "",
            status_message: null,
          }
          return {
            ...old,
            data: [...(old.data || []), newUserMessage],
            count: (old.count || 0) + 1,
          }
        })

        // If answering questions, optimistically update the referenced message status
        if (answersToMessageId) {
          queryClient.setQueryData(["messages", sessionId], (old: any) => {
            if (!old) return old
            return {
              ...old,
              data: old.data.map((msg: any) =>
                msg.id === answersToMessageId
                  ? { ...msg, tool_questions_status: "answered" }
                  : msg
              ),
            }
          })
        }

        // Subscribe to the session streaming room as a fire-and-forget
        // enhancement. We do NOT await it: with a dead/disconnected socket the
        // ack never fires, and blocking here would prevent the REST POST below
        // (the real source of truth). The room is tracked and re-subscribed on
        // reconnect; polling fills any gaps from the DB in the meantime.
        const streamRoom = `session_${sessionId}_stream`
        streamRoomRef.current = streamRoom
        eventService.subscribeToRoom(streamRoom).catch((err) => {
          console.warn("Failed to subscribe to stream room (non-blocking):", err)
        })

        // Subscribe to stream events (synchronous, local — safe regardless of WS state)
        if (streamSubscriptionRef.current) {
          eventService.unsubscribe(streamSubscriptionRef.current)
        }
        const subscriptionId = eventService.subscribe(
          "stream_event",
          handleStreamEvent
        )
        streamSubscriptionRef.current = subscriptionId

        // Send message via REST API (triggers background streaming)
        const requestBody: any = { content, file_ids: fileIds || [] }
        if (answersToMessageId) {
          requestBody.answers_to_message_id = answersToMessageId
        }
        // Include page_context when provided (e.g. from dashboard block prompt actions).
        // The backend stores it in message_metadata and injects it into the agent's
        // context using the same diff mechanism as the webapp chat widget.
        if (pageContext) {
          requestBody.page_context = pageContext
        }

        const response = await fetch(
          `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/stream`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify(requestBody),
          }
        )

        if (!response.ok) {
          const errorText = await response.text()
          throw new Error(
            `Failed to send message: ${response.status} - ${errorText}`
          )
        }

        // Refresh messages to get the real message with files
        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })

        // Fetch session to get updated interaction_status
        setTimeout(() => {
          queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
          queryClient.invalidateQueries({ queryKey: ["sessions"] })
        }, 200)

        setIsPending(false)
      } catch (error) {
        console.error("Failed to send message:", error)
        setIsPending(false)
        setStreamingEvents([])

        // Cleanup
        if (streamSubscriptionRef.current) {
          eventService.unsubscribe(streamSubscriptionRef.current)
          streamSubscriptionRef.current = null
        }
        if (streamRoomRef.current) {
          await eventService.unsubscribeFromRoom(streamRoomRef.current)
          streamRoomRef.current = null
        }

        // Remove optimistic message and refresh from server
        try {
          await new Promise((resolve) => setTimeout(resolve, 300))
          await queryClient.invalidateQueries({
            queryKey: ["messages", sessionId],
          })
        } catch (refreshError) {
          console.error("Failed to refresh messages after error:", refreshError)
        }

        const normalizedError =
          error instanceof Error ? error : new Error(String(error))
        onError?.(normalizedError)
        // Re-throw so awaiting callers (e.g. the initial-message effect on the
        // session page) can react to the failure — restore the message text,
        // preserve URL params, and avoid clearing state as if the send worked.
        throw normalizedError
      }
    },
    [sessionId, isStreaming, queryClient, handleStreamEvent, onError]
  )

  const stopMessage = useCallback(async () => {
    setIsInterruptPending(true)

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        console.error("Not authenticated")
        setIsInterruptPending(false)
        return
      }

      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/interrupt`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      )

      if (!response.ok) {
        console.warn("Interrupt request failed:", response.status)
        setIsInterruptPending(false)
      }
    } catch (error) {
      console.error("Failed to send interrupt:", error)
      setIsInterruptPending(false)
    }
  }, [sessionId])

  return {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
    isInterruptPending,
    isPending,
  }
}
