import { useState, useCallback } from "react"
import { useQueryClient } from "@tanstack/react-query"

interface StreamEvent {
  type: "session_created" | "assistant" | "tool" | "result" | "error" | "done" | "thinking"
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

  const sendMessage = useCallback(async (content: string) => {
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
      }

      return {
        ...old,
        data: [...(old.data || []), newUserMessage],
        count: (old.count || 0) + 1,
      }
    })

    try {
      const token = localStorage.getItem("access_token")
      if (!token) {
        throw new Error("Not authenticated")
      }

      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/api/v1/sessions/${sessionId}/messages/stream`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`,
          },
          body: JSON.stringify({ content }),
        }
      )

      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`Failed to send message: ${response.status} - ${errorText}`)
      }

      if (!response.body) {
        throw new Error("No response body")
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      let buffer = ""
      let streamCompleted = false

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

              // Skip system and done events
              if (event.type === "session_created" || event.type === "done") {
                if (event.type === "done") {
                  streamCompleted = true
                }
                continue
              }

              // Handle errors
              if (event.type === "error") {
                console.error("Stream error:", event)
                throw new Error(event.content || "Unknown stream error")
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
      }

      // Always refresh messages after stream completes successfully
      if (streamCompleted) {
        console.log("Stream completed, refreshing messages...")

        // Small delay to ensure backend finishes writing to database
        await new Promise(resolve => setTimeout(resolve, 300))

        await queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })

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
    }
  }, [sessionId, sessionMode, queryClient, onSuccess, onError])

  return {
    sendMessage,
    isStreaming,
    streamingEvents,
  }
}
