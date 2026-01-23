import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useRef, useCallback } from "react"
import { ArrowLeft, EllipsisVertical, Package, Loader2 } from "lucide-react"

import { SessionsService, MessagesService, AgentsService, EnvironmentsService } from "@/client"
import { MessageList } from "@/components/Chat/MessageList"
import { MessageInput } from "@/components/Chat/MessageInput"
import EditSession from "@/components/Sessions/EditSession"
import DeleteSession from "@/components/Sessions/DeleteSession"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import PendingItems from "@/components/Pending/PendingItems"
import useCustomToast from "@/hooks/useCustomToast"
import { useSessionStreaming } from "@/hooks/useSessionStreaming"
import { usePageHeader } from "@/routes/_layout"
import { AnimatedPlaceholder } from "@/components/Common/AnimatedPlaceholder"
import { EnvironmentPanel } from "@/components/Environment/EnvironmentPanel"
import { eventService, EventTypes } from "@/services/eventService"

export const Route = createFileRoute("/_layout/session/$sessionId")({
  component: ChatInterface,
  validateSearch: (search: Record<string, unknown>) => {
    return {
      initialMessage: (search.initialMessage as string) || undefined,
      fileIds: (search.fileIds as string) || undefined,
      // Full file objects for optimistic display (JSON string)
      fileObjects: (search.fileObjects as string) || undefined,
    }
  },
})

function ChatInterface() {
  const { sessionId } = Route.useParams()
  const { initialMessage, fileIds, fileObjects } = Route.useSearch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)
  const [envPanelOpen, setEnvPanelOpen] = useState(false)
  const initialMessageSent = useRef(false)
  const messageInputRef = useRef<HTMLTextAreaElement>(null)
  const [isEnvActivating, setIsEnvActivating] = useState(false)
  const usageIntentSent = useRef(false)

  const [isSessionStreaming, setIsSessionStreaming] = useState(false)

  const {
    data: session,
    isLoading: sessionLoading,
    error: sessionError,
  } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => SessionsService.getSession({ id: sessionId }),
    enabled: !!sessionId,
    refetchInterval: isSessionStreaming ? 3000 : 10000,
  })

  // Derive streaming state from session
  useEffect(() => {
    const streaming = session?.interaction_status === "running" || session?.interaction_status === "pending_stream"
    setIsSessionStreaming(streaming)
  }, [session?.interaction_status])

  const {
    data: messagesData,
    isLoading: messagesLoading,
  } = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => MessagesService.getMessages({ sessionId, offset: 0, limit: 100 }),
    enabled: !!sessionId,
    refetchInterval: isSessionStreaming ? 2000 : undefined,
  })

  const {
    data: agent,
  } = useQuery({
    queryKey: ["agent", session?.agent_id],
    queryFn: () => AgentsService.readAgent({ id: session!.agent_id! }),
    enabled: !!session?.agent_id,
  })

  const {
    data: environment,
  } = useQuery({
    queryKey: ["environment", session?.environment_id],
    queryFn: () => EnvironmentsService.getEnvironment({ id: session!.environment_id! }),
    enabled: !!session?.environment_id,
  })

  const { sendMessage, stopMessage, isStreaming, streamingEvents, isInterruptPending } = useSessionStreaming({
    sessionId,
    session: session ? { interaction_status: session.interaction_status, mode: session.mode } : null,
    messagesData: messagesData ? { data: messagesData.data as any } : null,
    onSuccess: () => {
      // Messages are already refreshed by the hook
      // Agent cache is also refreshed if building mode
    },
    onError: (error) => {
      showErrorToast(error.message || "Failed to send message")
    },
  })

  const handleSendMessage = useCallback(
    async (
      content: string,
      fileIds?: string[],
      fileObjs?: Array<{ id: string; filename: string; file_size: number; mime_type: string }>
    ) => {
      await sendMessage(content, undefined, fileIds, fileObjs)
    },
    [sendMessage]
  )

  const handleSendAnswer = useCallback(
    async (content: string, answersToMessageId: string) => {
      await sendMessage(content, answersToMessageId)
    },
    [sendMessage]
  )

  // Simple message send without linking to another message (for tool approval, etc.)
  const handleSendSimpleMessage = useCallback(
    async (content: string) => {
      await sendMessage(content)
    },
    [sendMessage]
  )

  // Send initial message if provided - wait for session and messages to load
  useEffect(() => {
    if (
      initialMessage &&
      !initialMessageSent.current &&
      !isStreaming &&
      session &&
      messagesData &&
      !sessionLoading &&
      !messagesLoading
    ) {
      initialMessageSent.current = true
      // Parse fileIds from comma-separated string to array
      const fileIdsArray = fileIds ? fileIds.split(',').filter(id => id.trim()) : undefined
      // Parse fileObjects JSON for optimistic display
      let parsedFileObjects: Array<{ id: string; filename: string; file_size: number; mime_type: string }> | undefined
      if (fileObjects) {
        try {
          parsedFileObjects = JSON.parse(fileObjects)
        } catch (e) {
          console.error("Failed to parse fileObjects:", e)
        }
      }
      // Use the same handleSendMessage that the UI uses, with file objects for optimistic display
      handleSendMessage(initialMessage, fileIdsArray, parsedFileObjects)
      // Clear the search param after sending
      navigate({
        to: "/session/$sessionId",
        params: { sessionId },
        search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
        replace: true,
      })
    }
  }, [
    initialMessage,
    fileIds,
    fileObjects,
    isStreaming,
    session,
    messagesData,
    sessionLoading,
    messagesLoading,
    sessionId,
    navigate,
    handleSendMessage,
  ])

  const handleBack = useCallback(() => {
    navigate({ to: "/sessions" })
  }, [navigate])

  const handleDeleteSuccess = useCallback(() => {
    navigate({ to: "/sessions" })
  }, [navigate])

  // Auto-focus message input when page loads
  useEffect(() => {
    if (!sessionLoading && !messagesLoading && messageInputRef.current) {
      messageInputRef.current.focus()
    }
  }, [sessionLoading, messagesLoading])

  // Update isEnvActivating based on environment status
  useEffect(() => {
    if (environment) {
      const status = environment.status
      // Show activating state for suspended, stopped, activating, or starting statuses
      if (status === "suspended" || status === "stopped" || status === "activating" || status === "starting") {
        setIsEnvActivating(true)
      } else if (status === "running") {
        setIsEnvActivating(false)
      }
    }
  }, [environment])

  // Send agent usage intent when session loads
  useEffect(() => {
    if (session && session.environment_id && !usageIntentSent.current) {
      usageIntentSent.current = true
      // Send usage intent to potentially activate suspended environment
      eventService.sendAgentUsageIntent(session.environment_id).catch((error) => {
        console.error("Failed to send agent usage intent:", error)
      })
    }
  }, [session])

  // Listen for environment activation events
  useEffect(() => {
    if (!session?.environment_id) return

    const subscriptions: string[] = []

    // Listen for activating event
    const activatingSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATING, (event) => {
      if (event.model_id === session.environment_id) {
        console.log("Environment is activating...")
        setIsEnvActivating(true)
        // Invalidate environment query to refetch status
        queryClient.invalidateQueries({ queryKey: ["environment", session.environment_id] })
      }
    })
    subscriptions.push(activatingSub)

    // Listen for activated event
    const activatedSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATED, (event) => {
      if (event.model_id === session.environment_id) {
        console.log("Environment activated successfully")
        setIsEnvActivating(false)
        showSuccessToast("Agent environment activated")
        // Invalidate environment query to refetch status
        queryClient.invalidateQueries({ queryKey: ["environment", session.environment_id] })
      }
    })
    subscriptions.push(activatedSub)

    // Listen for activation failed event
    const failedSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATION_FAILED, (event) => {
      if (event.model_id === session.environment_id) {
        console.error("Environment activation failed:", event.meta)
        setIsEnvActivating(false)
        showErrorToast("Failed to activate agent environment")
        // Invalidate environment query to refetch status
        queryClient.invalidateQueries({ queryKey: ["environment", session.environment_id] })
      }
    })
    subscriptions.push(failedSub)

    // Listen for suspended event
    const suspendedSub = eventService.subscribe(EventTypes.ENVIRONMENT_SUSPENDED, (event) => {
      if (event.model_id === session.environment_id) {
        console.log("Environment was suspended")
        setIsEnvActivating(false)
        // Invalidate environment query to refetch status
        queryClient.invalidateQueries({ queryKey: ["environment", session.environment_id] })
      }
    })
    subscriptions.push(suspendedSub)

    // Cleanup subscriptions
    return () => {
      subscriptions.forEach(sub => eventService.unsubscribe(sub))
    }
  }, [session?.environment_id, showSuccessToast, showErrorToast, queryClient])

  // Listen for session_interaction_status_changed WS events
  useEffect(() => {
    const sub = eventService.subscribe(EventTypes.SESSION_INTERACTION_STATUS_CHANGED, (event) => {
      if (event.meta?.session_id === sessionId) {
        // Immediately refetch session to update derived isStreaming state
        queryClient.invalidateQueries({ queryKey: ["session", sessionId] })
        if (event.meta?.interaction_status === "") {
          // Streaming ended - refetch messages for final content
          queryClient.invalidateQueries({ queryKey: ["messages", sessionId] })
        }
      }
    })
    return () => { eventService.unsubscribe(sub) }
  }, [sessionId, queryClient])

  // Update header when session loads
  useEffect(() => {
    if (session) {
      const isBuilding = session.mode === "building"
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">
                {session.title ? session.title : <AnimatedPlaceholder />}
              </h1>
              <p className="text-xs text-muted-foreground">
                <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${
                  isBuilding ? "bg-orange-500" : "bg-blue-500"
                }`} />
                {isBuilding ? "Building Mode" : "Conversation Mode"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {isEnvActivating ? (
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 cursor-wait"
                disabled
              >
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                Activating...
              </Button>
            ) : (
              <Button
                variant={envPanelOpen ? "secondary" : "ghost"}
                size="sm"
                className="shrink-0"
                onClick={() => setEnvPanelOpen(!envPanelOpen)}
              >
                <Package className="h-4 w-4 mr-1.5" />
                App
              </Button>
            )}
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="shrink-0">
                  <EllipsisVertical className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <EditSession session={session} onSuccess={() => setMenuOpen(false)} />
                <DeleteSession
                  id={session.id}
                  onSuccess={handleDeleteSuccess}
                />
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [session, setHeaderContent, menuOpen, envPanelOpen, handleBack, handleDeleteSuccess, isEnvActivating])

  if (sessionLoading || messagesLoading) {
    return <PendingItems />
  }

  if (sessionError || !session) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive mb-4">Error loading session</p>
        <button onClick={handleBack} className="text-primary hover:underline">
          Back to sessions
        </button>
      </div>
    )
  }

  const messages = messagesData?.data || []

  // Note: We no longer show a separate error screen.
  // Errors are now saved as system messages in the chat and will appear in the message list.

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex flex-col flex-1 min-h-0 relative">
        <MessageList
          messages={messages}
          isLoading={messagesLoading}
          streamingEvents={streamingEvents}
          isStreaming={isStreaming}
          onSendAnswer={handleSendAnswer}
          onSendMessage={handleSendSimpleMessage}
          conversationModeUi={session.mode === "building" ? "detailed" : (agent?.conversation_mode_ui || "detailed")}
          agentId={session?.agent_id ?? undefined}
        />
        <EnvironmentPanel isOpen={envPanelOpen} environmentId={session?.environment_id} agentId={session?.agent_id ?? undefined} />
      </div>
      <MessageInput
        ref={messageInputRef}
        onSend={handleSendMessage}
        onStop={stopMessage}
        sendDisabled={isStreaming}
        isInterruptPending={isInterruptPending}
        placeholder={
          isStreaming
            ? "Agent is responding..."
            : "Type your message..."
        }
        agentId={session?.agent_id ?? undefined}
        mode={session?.mode as "building" | "conversation" | undefined}
      />
    </div>
  )
}
