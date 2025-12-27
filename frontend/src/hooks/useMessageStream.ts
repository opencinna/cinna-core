import { useState, useCallback, useRef } from "react"
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
  type: "assistant" | "tool" | "thinking"
  content: string
  tool_name?: string
  metadata?: {
    tool_id?: string
    tool_input?: Record<string, any>
    model?: string
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
  const queryClient = useQueryClient()

  // Track abort controller for current request
  const abortControllerRef = useRef<AbortController | null>(null)

  const sendMessage = useCallback(async (content: string, answersToMessageId?: string) => {
    // Create new AbortController for this request
    const abortController = new AbortController()
    abortControllerRef.current = abortController

    setIsStreaming(true)
    setStreamingEvents([])

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
                break
              }

              // Skip system and done events
              if (event.type === "session_created" || event.type === "done") {
                if (event.type === "done") {
                  streamCompleted = true
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
              if (event.type === "assistant" || event.type === "tool" || event.type === "thinking") {
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
      // Handle abort (user clicked Stop button)
      if (error instanceof Error && error.name === 'AbortError') {
        console.log("Fetch aborted by user")
        // Don't show error - this is intentional
        setIsStreaming(false)
        setStreamingEvents([])

        // Still refresh to get partial message
        await new Promise(resolve => setTimeout(resolve, 300))
        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
        return
      }

      console.error("Message stream error:", error)
      setIsStreaming(false)
      setStreamingEvents([])

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

    // CRITICAL: Send interrupt signal BEFORE aborting the stream!
    // If we abort the stream first, the backend's async generator stops being consumed,
    // causing the Claude SDK to cancel its read task and unregister the session
    // BEFORE the interrupt request can be processed.

    // 1. Send interrupt signal to backend (stops SDK processing)
    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        console.error("Not authenticated, cannot send interrupt")
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
      } else {
        console.warn("Interrupt request returned non-200:", response.status)
      }
    } catch (error) {
      console.error("Failed to send interrupt signal:", error)
    }

    // 2. Abort the fetch request (stops HTTP stream)
    // Do this AFTER sending the interrupt signal to ensure proper cleanup
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      console.log("Fetch request aborted")
    }
  }, [sessionId])

  return {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
  }
}
