import { useState, useCallback, useRef, useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { eventService } from "@/services/eventService"

interface StructuredStreamEvent {
  type: "assistant" | "tool" | "thinking" | "system"
  content: string
  tool_name?: string
  metadata?: {
    tool_id?: string
    tool_input?: Record<string, any>
    model?: string
    interrupt_notification?: boolean
  }
}

interface UseMessageStreamOptions {
  sessionId: string
  sessionMode?: "building" | "conversation"
  onSuccess?: () => void
  onError?: (error: Error) => void
}

export function useMessageStream({ sessionId, sessionMode, onSuccess, onError }: UseMessageStreamOptions) {
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingEvents, setStreamingEvents] = useState<StructuredStreamEvent[]>([])
  const [isInterruptPending, setIsInterruptPending] = useState(false)
  const queryClient = useQueryClient()

  // Track subscription ID for cleanup
  const streamSubscriptionRef = useRef<string | null>(null)
  const streamRoomRef = useRef<string | null>(null)
  // Track if we've already checked for active streams on mount
  const hasCheckedForActiveStream = useRef(false)

  const sendMessage = useCallback(async (content: string, answersToMessageId?: string) => {
    setIsStreaming(true)
    setStreamingEvents([])
    setIsInterruptPending(false)

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        throw new Error("Not authenticated")
      }

      // Subscribe to session-specific streaming room
      const streamRoom = `session_${sessionId}_stream`
      streamRoomRef.current = streamRoom

      await eventService.subscribeToRoom(streamRoom)

      // Subscribe to stream events
      const subscriptionId = eventService.subscribe("stream_event", (event: any) => {
        handleStreamEvent(event)
      })
      streamSubscriptionRef.current = subscriptionId

      // Optimistically add user message to cache
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

      // Send message via REST API (triggers background WebSocket streaming)
      const requestBody: any = { content }
      if (answersToMessageId) {
        requestBody.answers_to_message_id = answersToMessageId
      }

      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/stream`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`,
          },
          body: JSON.stringify(requestBody),
        }
      )

      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`Failed to send message: ${response.status} - ${errorText}`)
      }

      const data = await response.json()
      console.log("Message processing started:", data)

      // Fetch session immediately to get temporary title (set before streaming starts)
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
        queryClient.invalidateQueries({ queryKey: ["sessions"] })
      }, 200)

      // WebSocket events will handle the rest

    } catch (error) {
      console.error("Failed to send message:", error)
      setIsStreaming(false)
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
        await new Promise(resolve => setTimeout(resolve, 300))
        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
      } catch (refreshError) {
        console.error("Failed to refresh messages after error:", refreshError)
      }

      onError?.(error instanceof Error ? error : new Error(String(error)))
    }
  }, [sessionId, sessionMode, queryClient, onSuccess, onError])

  const handleStreamEvent = useCallback((event: any) => {
    console.log("Received stream event:", event)

    const { session_id, event_type, data } = event

    // Verify event is for current session
    if (session_id !== sessionId) {
      return
    }

    // Handle different event types
    switch (event_type) {
      case "stream_started":
        console.log("Stream started")
        break

      case "user_message_created":
        // User message already added optimistically
        console.log("User message created:", data)
        break

      case "assistant":
      case "tool":
      case "thinking":
      case "system":
        // Add to streaming events for real-time display
        const structuredEvent: StructuredStreamEvent = {
          type: data.type,
          content: data.content || "",
          tool_name: data.tool_name,
          metadata: data.metadata,
        }
        setStreamingEvents(prev => [...prev, structuredEvent])
        break

      case "interrupted":
        console.log("Stream interrupted")
        setIsInterruptPending(false)
        handleStreamComplete(true)
        break

      case "error":
        console.error("Stream error:", data)
        setIsInterruptPending(false)
        handleStreamComplete(false)
        break

      case "stream_completed":
        console.log("Stream completed")
        setIsInterruptPending(false)
        handleStreamComplete(false)
        break

      default:
        console.log("Unknown event type:", event_type)
    }
  }, [sessionId])

  const handleStreamComplete = useCallback(async (wasInterrupted: boolean) => {
    console.log(`Stream ${wasInterrupted ? 'interrupted' : 'completed'}`)

    // Cleanup subscriptions
    if (streamSubscriptionRef.current) {
      eventService.unsubscribe(streamSubscriptionRef.current)
      streamSubscriptionRef.current = null
    }
    if (streamRoomRef.current) {
      await eventService.unsubscribeFromRoom(streamRoomRef.current)
      streamRoomRef.current = null
    }

    // Refresh messages
    await new Promise(resolve => setTimeout(resolve, 300))
    await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
    await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
    await queryClient.invalidateQueries({ queryKey: ["sessions"] })

    // Poll for AI-generated title (same as before)
    let pollAttempt = 0
    const pollForTitle = async () => {
      pollAttempt++
      const currentSession = queryClient.getQueryData(["session", sessionId]) as any
      if (currentSession?.title) {
        return
      }
      await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
      await queryClient.invalidateQueries({ queryKey: ["sessions"] })

      const nextDelay = pollAttempt <= 3 ? 500 : 2000
      setTimeout(pollForTitle, nextDelay)
    }
    setTimeout(pollForTitle, 500)

    // Invalidate agent caches if building mode
    if (sessionMode === "building") {
      console.log("Building session completed, refreshing agent data...")
      await queryClient.invalidateQueries({ queryKey: ["agent"] })
      await queryClient.invalidateQueries({ queryKey: ["agents"] })
    }

    setIsStreaming(false)
    setStreamingEvents([])
    onSuccess?.()
  }, [sessionId, sessionMode, queryClient, onSuccess])

  const stopMessage = useCallback(async () => {
    console.log("Stop button clicked")
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
            "Authorization": `Bearer ${token}`,
          },
        }
      )

      if (response.ok) {
        console.log("Interrupt request sent")
        // Wait for "interrupted" event via WebSocket
      } else {
        console.warn("Interrupt request failed:", response.status)
        setIsInterruptPending(false)
      }
    } catch (error) {
      console.error("Failed to send interrupt:", error)
      setIsInterruptPending(false)
    }
  }, [sessionId])

  // Check for active stream on mount (reconnection logic)
  const checkAndReconnectToActiveStream = useCallback(async () => {
    if (hasCheckedForActiveStream.current) {
      return // Already checked
    }
    hasCheckedForActiveStream.current = true

    try {
      const token = localStorage.getItem("access_token")
      if (!token) return

      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/streaming-status`,
        {
          headers: {
            "Authorization": `Bearer ${token}`,
          },
        }
      )

      if (response.ok) {
        const data = await response.json()
        if (data.is_streaming) {
          console.log("Detected active stream, reconnecting via WebSocket")
          setIsStreaming(true)

          // Subscribe to streaming room
          const streamRoom = `session_${sessionId}_stream`
          streamRoomRef.current = streamRoom
          await eventService.subscribeToRoom(streamRoom)

          // Subscribe to stream events
          const subscriptionId = eventService.subscribe("stream_event", handleStreamEvent)
          streamSubscriptionRef.current = subscriptionId

          // Refresh messages
          await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })

          // Poll for completion
          const pollInterval = setInterval(async () => {
            const statusResponse = await fetch(
              `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/streaming-status`,
              { headers: { "Authorization": `Bearer ${token}` } }
            )

            if (statusResponse.ok) {
              const statusData = await statusResponse.json()
              if (!statusData.is_streaming) {
                clearInterval(pollInterval)
                handleStreamComplete(false)
              }
            }
          }, 1000)

          return () => clearInterval(pollInterval)
        }
      }
    } catch (error) {
      console.error("Failed to check for active stream:", error)
    }
  }, [sessionId, queryClient, handleStreamEvent, handleStreamComplete])

  useEffect(() => {
    checkAndReconnectToActiveStream()
  }, [checkAndReconnectToActiveStream])

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

  return {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
    isInterruptPending,
  }
}
