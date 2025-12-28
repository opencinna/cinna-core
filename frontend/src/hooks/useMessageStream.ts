import React, { useState, useCallback, useRef, useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"

interface StreamEvent {
  type: "session_created" | "assistant" | "tool" | "result" | "error" | "done" | "thinking" | "interrupted"
  content?: string
  session_id?: string
  metadata?: Record<string, any>
  error_type?: string
  tool_name?: string
}

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

  // Track abort controller for current request
  const abortControllerRef = useRef<AbortController | null>(null)
  // Track if we've already checked for active streams on mount
  const hasCheckedForActiveStream = useRef(false)

  const sendMessage = useCallback(async (content: string, answersToMessageId?: string) => {
    // Create new AbortController for this request
    const abortController = new AbortController()
    abortControllerRef.current = abortController

    setIsStreaming(true)
    setStreamingEvents([])
    setIsInterruptPending(false) // Reset interrupt pending state

    // Optimistically add user message to the cache immediately
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

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        throw new Error("Not authenticated")
      }

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
          signal: abortController.signal,  // Add abort signal
        }
      )

      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`Failed to send message: ${response.status} - ${errorText}`)
      }

      if (!response.body) {
        throw new Error("No response body")
      }

      // Fetch session immediately to get temporary title (set before streaming starts)
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
        queryClient.invalidateQueries({ queryKey: ["sessions"] })
      }, 200)

      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      let buffer = ""
      let streamCompleted = false
      let wasInterrupted = false  // Track if stream was interrupted

      while (true) {
        const { done, value } = await reader.read()

        if (done) {
          streamCompleted = true
          break
        }

        // Decode the chunk and add to buffer
        buffer += decoder.decode(value, { stream: true })

        // Process complete SSE messages
        const lines = buffer.split("\n")
        buffer = lines.pop() || "" // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const dataStr = line.slice(6)

            try {
              const event: StreamEvent = JSON.parse(dataStr)

              // Handle interrupted event
              if (event.type === "interrupted") {
                console.log("Stream was interrupted by user")
                wasInterrupted = true
                streamCompleted = true
                setIsInterruptPending(false) // Clear pending state
                break
              }

              // Skip system and done events
              if (event.type === "session_created" || event.type === "done") {
                if (event.type === "done") {
                  streamCompleted = true
                  setIsInterruptPending(false) // Clear pending state on completion
                  // Check metadata for interrupt status
                  if (event.metadata?.interrupted) {
                    wasInterrupted = true
                  }
                }
                continue
              }

              // Handle errors from backend
              if (event.type === "error") {
                console.error("Stream error:", event)
                // Error is saved as a system message by the backend
                // Just mark stream as completed and refresh to show the error message
                streamCompleted = true
                break
              }

              // Convert to structured event
              if (event.type === "assistant" || event.type === "tool" || event.type === "thinking" || event.type === "system") {
                const structuredEvent: StructuredStreamEvent = {
                  type: event.type,
                  content: event.content || "",
                }

                if (event.tool_name) {
                  structuredEvent.tool_name = event.tool_name
                }

                if (event.metadata) {
                  structuredEvent.metadata = {
                    tool_id: event.metadata.tool_id,
                    tool_input: event.metadata.tool_input,
                    model: event.metadata.model,
                    interrupt_notification: event.metadata.interrupt_notification,
                  }
                }

                setStreamingEvents(prev => [...prev, structuredEvent])
              }
            } catch (parseError) {
              console.error("Failed to parse SSE event:", dataStr, parseError)
            }
          }
        }

        // Break outer loop if interrupted
        if (wasInterrupted) {
          break
        }
      }

      // Always refresh messages after stream ends (completed or interrupted)
      if (streamCompleted || wasInterrupted) {
        console.log(`Stream ${wasInterrupted ? 'interrupted' : 'completed'}, refreshing messages...`)

        // Small delay to ensure backend finishes writing to database
        await new Promise(resolve => setTimeout(resolve, 300))

        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })

        // Invalidate session query to get updated title (generated on first message)
        await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
        // Also invalidate sessions list to update dashboard
        await queryClient.invalidateQueries({ queryKey: ["sessions"] })

        // Poll for AI-generated title (backend generates in background)
        // Strategy: 3 quick attempts every 500ms, then slower checks every 2s until title appears
        let pollAttempt = 0
        const pollForTitle = async () => {
          pollAttempt++

          // Fetch session to check if title exists
          const currentSession = queryClient.getQueryData(["session", sessionId]) as any

          // If we have a title, stop polling
          if (currentSession?.title) {
            return
          }

          // Invalidate to fetch fresh data
          await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
          await queryClient.invalidateQueries({ queryKey: ["sessions"] })

          // Determine next poll delay
          let nextDelay: number
          if (pollAttempt <= 3) {
            // First 3 attempts: every 500ms (at 500ms, 1000ms, 1500ms)
            nextDelay = 500
          } else {
            // After that: every 2 seconds until title appears
            nextDelay = 2000
          }

          // Schedule next poll
          setTimeout(pollForTitle, nextDelay)
        }

        // Start polling after 500ms
        setTimeout(pollForTitle, 500)

        // Invalidate agent caches if building mode (prompts may have been updated)
        if (sessionMode === "building") {
          console.log("Building session completed, refreshing agent data...")
          // Invalidate all agent queries to ensure fresh prompt data
          await queryClient.invalidateQueries({ queryKey: ["agent"] })
          await queryClient.invalidateQueries({ queryKey: ["agents"] })
        }

        onSuccess?.()
      }

      setIsStreaming(false)
      setStreamingEvents([])
    } catch (error) {
      // Handle network errors or browser disconnections
      // Note: We no longer manually abort, so AbortError means browser disconnected (e.g., page refresh)
      if (error instanceof Error && error.name === 'AbortError') {
        console.log("Stream disconnected (likely page refresh/navigation)")
        // Backend will continue processing, so just clean up frontend state
        setIsStreaming(false)
        setStreamingEvents([])

        // Refresh to get any messages that were saved
        await new Promise(resolve => setTimeout(resolve, 300))
        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
        return
      }

      console.error("Message stream error:", error)
      setIsStreaming(false)
      setStreamingEvents([])
      setIsInterruptPending(false) // Clear pending state on error

      // Remove optimistic message and refresh from server
      try {
        await new Promise(resolve => setTimeout(resolve, 300))
        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
      } catch (refreshError) {
        console.error("Failed to refresh messages after error:", refreshError)
      }

      onError?.(error instanceof Error ? error : new Error(String(error)))
    } finally {
      // Clear abort controller reference
      abortControllerRef.current = null
    }
  }, [sessionId, sessionMode, queryClient, onSuccess, onError])

  const stopMessage = useCallback(async () => {
    console.log("Stop button clicked")

    // Set interrupt pending state immediately for UI feedback
    setIsInterruptPending(true)

    // NEW BEHAVIOR: Send interrupt signal but DON'T abort the stream
    // The backend will continue streaming from the agent environment,
    // receive the "interrupted" event, and send it to us through the existing stream.
    // This ensures proper cleanup in the SDK and prevents session corruption.

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        console.error("Not authenticated, cannot send interrupt")
        setIsInterruptPending(false) // Clear pending state on error
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
        const data = await response.json()
        console.log("Interrupt request sent successfully:", data)
        // The stream will continue and receive the "interrupted" event
        // isInterruptPending will be cleared when we receive interrupted/done event
      } else {
        console.warn("Interrupt request returned non-200:", response.status)
        setIsInterruptPending(false) // Clear pending state if request failed
      }
    } catch (error) {
      console.error("Failed to send interrupt signal:", error)
      setIsInterruptPending(false) // Clear pending state on error
    }

    // DON'T abort the fetch - let it receive the "interrupted" event naturally
    // The stream will end when the backend sends the "done" event
  }, [sessionId])

  // Check if session is actively streaming and reconnect if needed
  // This handles the case where user refreshes page during streaming
  const checkAndReconnectToActiveStream = useCallback(async () => {
    if (hasCheckedForActiveStream.current) {
      return // Already checked
    }
    hasCheckedForActiveStream.current = true

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        return
      }

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
          console.log("Detected active stream, reconnecting...", data.stream_info)
          setIsStreaming(true)

          // Refresh messages to show any partial content
          await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })

          // Note: We don't try to reconnect to the SSE stream because:
          // 1. The backend is still processing independently
          // 2. Messages are being saved to database
          // 3. We'll get the final result via query invalidation
          // 4. The "active" indicator shows user something is happening

          // Poll for completion
          const pollInterval = setInterval(async () => {
            const statusResponse = await fetch(
              `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/streaming-status`,
              {
                headers: {
                  "Authorization": `Bearer ${token}`,
                },
              }
            )

            if (statusResponse.ok) {
              const statusData = await statusResponse.json()
              if (!statusData.is_streaming) {
                // Stream completed
                console.log("Active stream completed, refreshing messages")
                clearInterval(pollInterval)
                setIsStreaming(false)
                await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
                await queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
                await queryClient.invalidateQueries({ queryKey: ["sessions"] })
                onSuccess?.()
              }
            }
          }, 1000) // Poll every second

          // Cleanup on unmount
          return () => clearInterval(pollInterval)
        }
      }
    } catch (error) {
      console.error("Failed to check for active stream:", error)
    }
  }, [sessionId, queryClient, onSuccess])

  // Check for active stream on mount
  useEffect(() => {
    checkAndReconnectToActiveStream()
  }, [checkAndReconnectToActiveStream])

  return {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
    isInterruptPending,
  }
}
