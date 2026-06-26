import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useRef, useCallback, useMemo } from "react"
import { ArrowLeft, EllipsisVertical, Mail, Package, Loader2, ListTodo, Plug, UserCircle, Hammer, MessageCircle, User } from "lucide-react"

import { SessionsService, MessagesService, AgentsService, EnvironmentsService, OpenAPI } from "@/client"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { SubTasksPanel } from "@/components/Chat/SubTasksPanel"
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
import NotFound from "@/components/Common/NotFound"
import { eventService, EventTypes } from "@/services/eventService"

export const Route = createFileRoute("/_layout/session/$sessionId")({
  component: ChatInterface,
  validateSearch: (search: Record<string, unknown>) => {
    return {
      initialMessage: (search.initialMessage as string) || undefined,
      fileIds: (search.fileIds as string) || undefined,
      // Full file objects for optimistic display (JSON string)
      fileObjects: (search.fileObjects as string) || undefined,
      // Page context collected from a webapp iframe before navigating here
      // (e.g. from a dashboard block prompt action). Forwarded with the first
      // message so the backend stores it in message_metadata and injects it
      // into the agent's context via the context diff mechanism.
      pageContext: (search.pageContext as string) || undefined,
    }
  },
})

function ChatInterface() {
  const { sessionId } = Route.useParams()
  const { initialMessage, fileIds, fileObjects, pageContext } = Route.useSearch()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { setHeaderContent } = usePageHeader()
  const [menuOpen, setMenuOpen] = useState(false)
  const [envPanelOpen, setEnvPanelOpen] = useState(false)
  const initialMessageSent = useRef(false)
  const messageInputRef = useRef<HTMLTextAreaElement>(null)
  const [isEnvActivating, setIsEnvActivating] = useState(false)
  // Distinguish a genuinely suspended/stopped env (no spinner, "Suspended"
  // label) from one that is actively activating/starting/rebuilding (spinner).
  const [isEnvSuspended, setIsEnvSuspended] = useState(false)
  // When a send fails, stash the message text here so MessageInput re-seeds it
  // and the user can retry. The nonce forces a fresh re-seed even when the same
  // text fails twice in a row (a bare string would be deduped by React state).
  const [seed, setSeed] = useState<{ text: string; nonce: number } | undefined>(undefined)
  const seedNonceRef = useRef(0)
  const restoreMessageText = useCallback((text: string) => {
    seedNonceRef.current += 1
    setSeed({ text, nonce: seedNonceRef.current })
  }, [])
  const usageIntentSent = useRef(false)
  const [resolvedEnvId, setResolvedEnvId] = useState<string | null>(null)
  const [showSubTasks, setShowSubTasks] = useState(false)

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

  // Panel must always follow the agent's active environment. The session's
  // environment_id is just the env this session was last bound to and can be
  // stale (NULL if that env was deleted, or pointing at a non-active env if
  // the user activated a new one). Prefer agent.active_environment_id; fall
  // back to session.environment_id only if the agent has no active env.
  const effectiveEnvId = resolvedEnvId || agent?.active_environment_id || session?.environment_id

  const {
    data: environment,
  } = useQuery({
    queryKey: ["environment", effectiveEnvId],
    queryFn: () => EnvironmentsService.getEnvironment({ id: effectiveEnvId! }),
    enabled: !!effectiveEnvId,
  })

  // Query sub-tasks for badge count and state-based coloring
  const { data: subTasksData } = useQuery({
    queryKey: ["subTasksCount", sessionId],
    queryFn: async () => {
      const token = typeof OpenAPI.TOKEN === "function"
        ? await OpenAPI.TOKEN({} as any)
        : OpenAPI.TOKEN || ""
      const response = await fetch(`${OpenAPI.BASE}/api/v1/tasks/by-source-session/${sessionId}`, {
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json",
        },
      })
      if (!response.ok) return { data: [], count: 0 }
      return response.json()
    },
    refetchInterval: 15000,
  })

  const subTaskCount = subTasksData?.count || 0

  // Derive effective state: result_state (agent-declared) takes priority, fallback to task status
  const getEffectiveState = (t: { result_state?: string | null; status?: string }) => {
    if (t.result_state) return t.result_state
    switch (t.status) {
      case "completed": return "completed"
      case "error": return "error"
      case "pending_input": return "needs_input"
      case "new": return "new"
      default: return "running"
    }
  }

  // Compute per-status badge counts
  const subTaskBadges = useMemo(() => {
    const tasks = subTasksData?.data || []
    if (tasks.length === 0) return { running: 0, needsInput: 0, errors: 0, completed: 0, new: 0 }
    const completed = tasks.filter((t: any) => getEffectiveState(t) === "completed").length
    const needsInput = tasks.filter((t: any) => getEffectiveState(t) === "needs_input").length
    const errors = tasks.filter((t: any) => getEffectiveState(t) === "error").length
    const newTasks = tasks.filter((t: any) => getEffectiveState(t) === "new").length
    const running = tasks.length - completed - needsInput - errors - newTasks
    return { running, needsInput, errors, completed, new: newTasks }
  }, [subTasksData?.data])

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
      fileObjs?: Array<{ id: string; filename: string; file_size: number; mime_type: string }>,
      msgPageContext?: string
    ) => {
      // sendMessage re-throws on failure (so the initial-message effect can
      // react). For interactive sends from MessageInput — which call this
      // fire-and-forget — swallow here so we don't produce an unhandled
      // rejection; the user-facing error is already shown via onError.
      try {
        await sendMessage(content, undefined, fileIds, fileObjs, msgPageContext)
      } catch {
        /* handled by onError */
      }
    },
    [sendMessage]
  )

  const handleSendAnswer = useCallback(
    async (content: string, answersToMessageId: string) => {
      try {
        await sendMessage(content, answersToMessageId)
      } catch {
        /* handled by onError */
      }
    },
    [sendMessage]
  )

  // Simple message send without linking to another message (for tool approval, etc.)
  const handleSendSimpleMessage = useCallback(
    async (content: string) => {
      try {
        await sendMessage(content)
      } catch {
        /* handled by onError */
      }
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
      // Set the dedup guard synchronously so the effect doesn't fire twice.
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
      // Forward pageContext (from dashboard block prompt actions) with the first message.
      // The backend stores it in message_metadata and uses it for context diff injection.
      // Only clear the URL search params AFTER the send succeeds; on failure
      // preserve them, reset the guard, surface an error, and restore the text
      // into the input so the user's message is never silently lost.
      // Call sendMessage directly (not handleSendMessage, which swallows errors
      // for the interactive path) so we can observe success/failure here.
      sendMessage(initialMessage, undefined, fileIdsArray, parsedFileObjects, pageContext)
        .then(() => {
          navigate({
            to: "/session/$sessionId",
            params: { sessionId },
            search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined },
            replace: true,
          })
        })
        .catch((error) => {
          console.error("Failed to send initial message:", error)
          // Keep the URL params intact and re-seed the input so the user can
          // retry manually (the primary recovery path). Resetting the guard also
          // allows an opportunistic auto-retry if a later session/messages
          // refetch re-runs this effect. The error toast is shown via onError.
          initialMessageSent.current = false
          restoreMessageText(initialMessage)
        })
    }
  }, [
    initialMessage,
    fileIds,
    fileObjects,
    pageContext,
    isStreaming,
    session,
    messagesData,
    sessionLoading,
    messagesLoading,
    sessionId,
    navigate,
    sendMessage,
  ])

  const { goBack } = useNavigationHistory()

  const handleBack = useCallback(() => {
    goBack("/sessions")
  }, [goBack])

  const handleDeleteSuccess = useCallback(() => {
    navigate({ to: "/sessions" })
  }, [navigate])

  // Auto-focus message input when page loads
  useEffect(() => {
    if (!sessionLoading && !messagesLoading && messageInputRef.current) {
      messageInputRef.current.focus()
    }
  }, [sessionLoading, messagesLoading])

  // Update env-state flags based on environment status. Only an env that is
  // genuinely in flight (activating/starting/rebuilding) shows the "Activating…"
  // spinner; a suspended/stopped env shows a distinct, non-animated "Suspended"
  // state; running clears everything.
  useEffect(() => {
    if (!environment) return
    const status = environment.status
    if (status === "activating" || status === "starting" || status === "rebuilding") {
      setIsEnvActivating(true)
      setIsEnvSuspended(false)
    } else if (status === "suspended" || status === "stopped") {
      setIsEnvActivating(false)
      setIsEnvSuspended(true)
    } else if (status === "running") {
      setIsEnvActivating(false)
      setIsEnvSuspended(false)
    }
  }, [environment])

  // Send agent usage intent once we know which env to target. Prefer the
  // agent's active env (current truth) over session.environment_id (which may
  // be stale or NULL after the prior env was replaced/deleted).
  const intentTargetEnvId = agent?.active_environment_id || session?.environment_id
  useEffect(() => {
    if (intentTargetEnvId && !usageIntentSent.current) {
      usageIntentSent.current = true
      // Use the REST endpoint (not the WebSocket) so env wake-up works even when
      // the socket is permanently disconnected (e.g. after a backend deploy).
      EnvironmentsService.registerEnvironmentUsageIntent({ id: intentTargetEnvId })
        .then((response) => {
          if (response?.environment_id && response.environment_id !== intentTargetEnvId) {
            setResolvedEnvId(response.environment_id)
          }
        })
        .catch((error) => {
          console.error("Failed to send agent usage intent:", error)
          // Reset the guard so a later effect re-run (e.g. after the env query
          // refreshes) can retry the wake-up.
          usageIntentSent.current = false
        })
    }
  }, [intentTargetEnvId])

  // Listen for environment activation events
  useEffect(() => {
    if (!effectiveEnvId) return

    const subscriptions: string[] = []

    // Listen for activating event
    const activatingSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATING, (event) => {
      if (event.model_id === effectiveEnvId) {
        console.log("Environment is activating...")
        setIsEnvActivating(true)
        setIsEnvSuspended(false)
        queryClient.invalidateQueries({ queryKey: ["environment", effectiveEnvId] })
      }
    })
    subscriptions.push(activatingSub)

    // Listen for activated event
    const activatedSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATED, (event) => {
      if (event.model_id === effectiveEnvId) {
        console.log("Environment activated successfully")
        setIsEnvActivating(false)
        setIsEnvSuspended(false)
        showSuccessToast("Agent environment activated")
        queryClient.invalidateQueries({ queryKey: ["environment", effectiveEnvId] })
      }
    })
    subscriptions.push(activatedSub)

    // Listen for activation failed event
    const failedSub = eventService.subscribe(EventTypes.ENVIRONMENT_ACTIVATION_FAILED, (event) => {
      if (event.model_id === effectiveEnvId) {
        console.error("Environment activation failed:", event.meta)
        setIsEnvActivating(false)
        showErrorToast("Failed to activate agent environment")
        queryClient.invalidateQueries({ queryKey: ["environment", effectiveEnvId] })
      }
    })
    subscriptions.push(failedSub)

    // Listen for suspended event
    const suspendedSub = eventService.subscribe(EventTypes.ENVIRONMENT_SUSPENDED, (event) => {
      if (event.model_id === effectiveEnvId) {
        console.log("Environment was suspended")
        setIsEnvActivating(false)
        setIsEnvSuspended(true)
        queryClient.invalidateQueries({ queryKey: ["environment", effectiveEnvId] })
      }
    })
    subscriptions.push(suspendedSub)

    // Listen for generic status changes (e.g. rebuilding, stopped after rebuild, error)
    const statusChangedSub = eventService.subscribe(EventTypes.ENVIRONMENT_STATUS_CHANGED, (event) => {
      if (event.model_id === effectiveEnvId) {
        const status = event.meta?.status
        console.log(`Environment status changed: ${status}`)
        if (status === "rebuilding" || status === "activating" || status === "starting") {
          setIsEnvActivating(true)
          setIsEnvSuspended(false)
        } else if (status === "running" || status === "stopped" || status === "error") {
          setIsEnvActivating(false)
          setIsEnvSuspended(status === "stopped")
          if (status === "error") {
            showErrorToast("Environment rebuild failed")
          }
        }
        queryClient.invalidateQueries({ queryKey: ["environment", effectiveEnvId] })
      }
    })
    subscriptions.push(statusChangedSub)

    // Cleanup subscriptions
    return () => {
      subscriptions.forEach(sub => eventService.unsubscribe(sub))
    }
  }, [effectiveEnvId, showSuccessToast, showErrorToast, queryClient])

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

  // Listen for session state updates to refresh sub-tasks badge
  useEffect(() => {
    const sub = eventService.subscribe(EventTypes.SESSION_STATE_UPDATED, () => {
      queryClient.invalidateQueries({ queryKey: ["subTasksCount", sessionId] })
      queryClient.invalidateQueries({ queryKey: ["subTasks", sessionId] })
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
                {session.title
                  ? session.title
                  : (messagesData?.data?.length ?? 0) > 0
                    ? <AnimatedPlaceholder />
                    : <span className="text-muted-foreground">New session</span>}
              </h1>
              <p className="text-xs text-muted-foreground flex items-center gap-1.5">
                {isBuilding ? (
                  <>
                    <Hammer className="h-3 w-3 text-orange-500" />
                    Building
                  </>
                ) : (
                  <>
                    <MessageCircle className="h-3 w-3 text-blue-500" />
                    Conversation
                  </>
                )}
                {session.integration_type === "app_mcp" && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300">
                    <Plug className="h-2.5 w-2.5" />
                    MCP
                  </span>
                )}
                {session.integration_type === "email" && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300">
                    <Mail className="h-2.5 w-2.5" />
                    Email
                  </span>
                )}
                {session.integration_type === "a2a" && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300">
                    <Plug className="h-2.5 w-2.5" />
                    A2A
                  </span>
                )}
                {session.integration_type === "identity_mcp" && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
                    <UserCircle className="h-2.5 w-2.5" />
                    Via Identity
                    {(session.session_metadata as Record<string, string> | null)?.identity_caller_name && (
                      <span className="ml-0.5">
                        — {(session.session_metadata as Record<string, string>).identity_caller_name}
                      </span>
                    )}
                  </span>
                )}
                {session.integration_type === "external" && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300">
                    <Package className="h-2.5 w-2.5" />
                    {(session.session_metadata as Record<string, string> | null)?.client_kind
                      ? ((session.session_metadata as Record<string, string>).client_kind.charAt(0).toUpperCase() +
                          (session.session_metadata as Record<string, string>).client_kind.slice(1))
                      : "External"}
                  </span>
                )}
                {/*
                  Self-call sessions: when the agent owner is also the
                  caller (e.g. agent-user installs that hit App MCP from
                  their own Claude Desktop), suppress the redundant
                  "called by yourself" email chip. Keep the integration-
                  type badge (MCP / A2A / Email / etc.) so the channel
                  is still visible.
                */}
                {session.caller_email && session.caller_id !== session.user_id && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">
                    <User className="h-2.5 w-2.5" />
                    {session.caller_email}
                  </span>
                )}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {subTaskCount > 0 && (
              <Button
                variant={showSubTasks ? "secondary" : "outline"}
                size="sm"
                onClick={() => { setShowSubTasks(!showSubTasks); setEnvPanelOpen(false) }}
                className="gap-1.5"
              >
                <ListTodo className="h-4 w-4" />
                <span>Tasks</span>
                <div className="flex items-center gap-0.5">
                  {subTaskBadges.new > 0 && (
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
                      {subTaskBadges.new}
                    </span>
                  )}
                  {subTaskBadges.running > 0 && (
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">
                      {subTaskBadges.running}
                    </span>
                  )}
                  {subTaskBadges.needsInput > 0 && (
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300">
                      {subTaskBadges.needsInput}
                    </span>
                  )}
                  {subTaskBadges.errors > 0 && (
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300">
                      {subTaskBadges.errors}
                    </span>
                  )}
                  {subTaskBadges.completed > 0 && (
                    <span className="text-xs font-medium px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">
                      {subTaskBadges.completed}
                    </span>
                  )}
                </div>
              </Button>
            )}
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
            ) : isEnvSuspended ? (
              <Button
                variant={envPanelOpen ? "secondary" : "ghost"}
                size="sm"
                className="shrink-0 text-muted-foreground"
                title="App is suspended — it will wake up on your next message"
                onClick={() => { setEnvPanelOpen(!envPanelOpen); setShowSubTasks(false) }}
              >
                <Package className="h-4 w-4 mr-1.5 opacity-60" />
                Suspended
              </Button>
            ) : (
              <Button
                variant={envPanelOpen ? "secondary" : "ghost"}
                size="sm"
                className="shrink-0"
                onClick={() => { setEnvPanelOpen(!envPanelOpen); setShowSubTasks(false) }}
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
  }, [session, setHeaderContent, menuOpen, envPanelOpen, handleBack, handleDeleteSuccess, isEnvActivating, isEnvSuspended, messagesData?.data?.length, subTaskCount, subTaskBadges, showSubTasks])

  if (sessionLoading || messagesLoading) {
    return <PendingItems />
  }

  if (sessionError || !session) {
    const isMissing =
      !sessionError || (sessionError as { status?: number }).status === 404
    if (isMissing) {
      return (
        <NotFound
          inline
          fallbackPath="/sessions"
          title="Session not found"
          message="This session doesn't exist, was deleted, or belongs to another user."
        />
      )
    }
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
          integrationTyp={session?.integration_type}
          sessionId={sessionId}
        />
        <EnvironmentPanel isOpen={envPanelOpen} environmentId={effectiveEnvId ?? undefined} agentId={session?.agent_id ?? undefined} />
        {showSubTasks && (
          <SubTasksPanel
            sessionId={sessionId}
            onClose={() => setShowSubTasks(false)}
          />
        )}
      </div>
      <MessageInput
        ref={messageInputRef}
        onSend={handleSendMessage}
        onStop={stopMessage}
        isStreaming={isStreaming}
        isInterruptPending={isInterruptPending}
        placeholder="Type your message..."
        agentId={session?.agent_id ?? undefined}
        mode={session?.mode as "building" | "conversation" | undefined}
        sessionId={sessionId}
        seed={seed}
      />
    </div>
  )
}
