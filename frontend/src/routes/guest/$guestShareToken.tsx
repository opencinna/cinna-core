import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useState, useEffect, useRef, useCallback } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  GuestShareService,
  SessionsService,
  MessagesService,
} from "@/client"
import type { SessionPublicExtended } from "@/client"
import { GuestShareProvider } from "@/hooks/useGuestShare"
import { MessageList } from "@/components/Chat/MessageList"
import { MessageInput } from "@/components/Chat/MessageInput"
import { useSessionStreaming } from "@/hooks/useSessionStreaming"
import { Button } from "@/components/ui/button"
import {
  MessageSquarePlus,
  MessageCircle,
  Loader2,
  AlertCircle,
  Clock,
  Bot,
  Package,
} from "lucide-react"
import { EnvironmentPanel } from "@/components/Environment/EnvironmentPanel"

export const Route = createFileRoute("/guest/$guestShareToken")({
  component: GuestChatPage,
  validateSearch: (search: Record<string, unknown>) => ({
    sessionId: (search.sessionId as string) || undefined,
  }),
  head: () => ({
    meta: [
      {
        title: "Guest Chat",
      },
    ],
  }),
})

// ── Types for untyped backend responses ─────────────────────────────────

interface GuestShareInfoResponse {
  agent_name: string | null
  agent_description: string | null
  is_valid: boolean
  guest_share_id: string | null
  requires_code: boolean
  is_code_blocked: boolean
  allow_env_panel: boolean
}

interface GuestShareAuthResponse {
  access_token: string
  token_type: string
  guest_share_id: string
  agent_id: string
}

interface GuestShareActivateResponse {
  guest_share_id: string
  agent_id: string
  agent_name: string
}

// ── Helpers ──────────────────────────────────────────────────────────────

/**
 * Decode a JWT payload without verification and return guest claims
 * if it's a guest share token. Returns null for regular user JWTs.
 */
function parseGuestJwt(
  token: string
): { sub: string; agent_id: string; exp: number } | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]))
    if (payload.role === "chat-guest" && payload.token_type === "guest_share") {
      return { sub: payload.sub, agent_id: payload.agent_id, exp: payload.exp }
    }
  } catch {
    // Malformed token — treat as not a guest JWT
  }
  return null
}

// ── Main Component ──────────────────────────────────────────────────────

function GuestChatPage() {
  const { guestShareToken } = Route.useParams()
  const { sessionId: selectedSessionId } = Route.useSearch()
  const navigate = useNavigate()

  // Auth flow state
  const [authState, setAuthState] = useState<
    "loading" | "code_entry" | "authenticating" | "ready" | "error"
  >("loading")
  const [errorMessage, setErrorMessage] = useState<string>("")
  const [guestShareId, setGuestShareId] = useState<string | null>(null)
  const [agentId, setAgentId] = useState<string | null>(null)
  const [agentName, setAgentName] = useState<string>("")
  const [agentDescription, setAgentDescription] = useState<string | null>(null)
  const [allowEnvPanel, setAllowEnvPanel] = useState(false)

  // Session state
  const [envPanelOpen, setEnvPanelOpen] = useState(false)

  const selectSession = useCallback(
    (id: string) => {
      navigate({
        to: "/guest/$guestShareToken",
        params: { guestShareToken },
        search: { sessionId: id },
        replace: true,
      })
    },
    [navigate, guestShareToken]
  )

  const authAttempted = useRef(false)
  const messageInputRef = useRef<HTMLTextAreaElement>(null)

  // Step 1: Fetch share info (no auth required)
  const {
    data: shareInfo,
    isLoading: infoLoading,
    error: infoError,
  } = useQuery({
    queryKey: ["guestShareInfo", guestShareToken],
    queryFn: async () => {
      const result = await GuestShareService.guestShareInfo({
        token: guestShareToken,
      })
      return result as unknown as GuestShareInfoResponse
    },
    retry: false,
  })

  // Step 2: Auth flow after share info is loaded
  useEffect(() => {
    if (authAttempted.current) return
    if (infoLoading || infoError) return
    if (!shareInfo) return

    if (!shareInfo.is_valid) {
      setAuthState("error")
      setErrorMessage(
        shareInfo.guest_share_id
          ? "This guest share link has expired. Contact the owner for a new link."
          : "This guest share link is invalid or has been removed."
      )
      return
    }

    setAgentName(shareInfo.agent_name || "Agent")
    setAgentDescription(shareInfo.agent_description || null)

    // Check if code blocked
    if (shareInfo.is_code_blocked) {
      authAttempted.current = true
      setAuthState("error")
      setErrorMessage("This share link has been blocked due to too many failed attempts. Contact the owner.")
      return
    }

    // Check for an existing valid guest JWT before requiring code entry.
    // This handles page refresh after the user already entered the code.
    const existingToken = localStorage.getItem("access_token")
    if (existingToken) {
      const guestClaims = parseGuestJwt(existingToken)
      if (guestClaims && guestClaims.exp * 1000 > Date.now()) {
        authAttempted.current = true
        setAuthState("authenticating")
        performAuth()
        return
      }
    }

    // If code is required and no valid token exists, go to code entry state
    if (shareInfo.requires_code) {
      authAttempted.current = true
      setAuthState("code_entry")
      return
    }

    // No code required — proceed with auth
    authAttempted.current = true
    setAuthState("authenticating")
    performAuth()
  }, [shareInfo, infoLoading, infoError, guestShareToken])

  const performAuth = useCallback(async (securityCode?: string) => {
    try {
      const existingToken = localStorage.getItem("access_token")

      if (existingToken) {
        const guestClaims = parseGuestJwt(existingToken)

        if (guestClaims) {
          if (guestClaims.exp * 1000 > Date.now()) {
            setGuestShareId(guestClaims.sub)
            setAgentId(guestClaims.agent_id)
            setAuthState("ready")
            return
          }
          localStorage.removeItem("access_token")
          await authenticateAnonymous(securityCode)
        } else {
          try {
            const activateResult = (await GuestShareService.guestShareActivate({
              token: guestShareToken,
              requestBody: securityCode ? { security_code: securityCode } : undefined,
            })) as unknown as GuestShareActivateResponse

            setGuestShareId(activateResult.guest_share_id)
            setAgentId(activateResult.agent_id)
            setAgentName(activateResult.agent_name || agentName || "Agent")
            setAuthState("ready")
          } catch (activateError: any) {
            if (activateError?.status === 403) {
              throw activateError
            }
            if ([401, 404].includes(activateError?.status)) {
              localStorage.removeItem("access_token")
              await authenticateAnonymous(securityCode)
            } else {
              throw activateError
            }
          }
        }
      } else {
        await authenticateAnonymous(securityCode)
      }
    } catch (error: any) {
      console.error("Guest auth failed:", error)
      if (error?.status === 403) {
        const detail = error?.body?.detail || ""
        if (detail.toLowerCase().includes("blocked")) {
          setAuthState("error")
          setErrorMessage("This share link has been blocked due to too many failed attempts. Contact the owner.")
        } else {
          throw error // Re-throw so code entry can handle it
        }
      } else if (error?.status === 410) {
        setAuthState("error")
        setErrorMessage("This guest share link has expired. Contact the owner for a new link.")
      } else if (error?.status === 404) {
        setAuthState("error")
        setErrorMessage("This guest share link is invalid or has been removed.")
      } else {
        setAuthState("error")
        setErrorMessage("Something went wrong. Please try again.")
      }
    }
  }, [guestShareToken, agentName])

  const authenticateAnonymous = async (securityCode?: string) => {
    const authResult = (await GuestShareService.guestShareAuthenticate({
      token: guestShareToken,
      requestBody: securityCode ? { security_code: securityCode } : undefined,
    })) as unknown as GuestShareAuthResponse

    localStorage.setItem("access_token", authResult.access_token)
    setGuestShareId(authResult.guest_share_id)
    setAgentId(authResult.agent_id)
    setAuthState("ready")
  }

  // Keep allowEnvPanel in sync with share info (survives cache updates)
  useEffect(() => {
    if (shareInfo?.is_valid) {
      setAllowEnvPanel(shareInfo.allow_env_panel || false)
    }
  }, [shareInfo?.allow_env_panel, shareInfo?.is_valid])

  // Handle info loading error
  useEffect(() => {
    if (infoError) {
      setAuthState("error")
      setErrorMessage("Something went wrong. Please try again.")
    }
  }, [infoError])

  // Render based on auth state
  if (authState === "loading" || authState === "authenticating") {
    return (
      <GuestLoadingScreen
        agentName={agentName || "the agent"}
        isAuthenticating={authState === "authenticating"}
      />
    )
  }

  if (authState === "code_entry") {
    return (
      <SecurityCodeScreen
        agentName={agentName || "Agent"}
        onSubmit={async (code) => {
          try {
            await performAuth(code)
          } catch (error: any) {
            const detail = error?.body?.detail || ""
            throw new Error(detail || "Incorrect security code")
          }
        }}
      />
    )
  }

  if (authState === "error") {
    return <GuestErrorScreen message={errorMessage} />
  }

  return (
    <GuestShareProvider
      guestShareId={guestShareId}
      agentId={agentId}
      guestShareToken={guestShareToken}
    >
      <div className="flex flex-col h-screen bg-background">
        {/* Header */}
        <GuestChatHeader
          agentName={agentName}
          agentDescription={agentDescription}
          envPanelOpen={envPanelOpen}
          onToggleEnvPanel={() => setEnvPanelOpen(!envPanelOpen)}
          hasSession={selectedSessionId !== undefined}
          allowEnvPanel={allowEnvPanel}
        />

        {/* Content area */}
        <div className="flex flex-1 min-h-0">
          {/* Sidebar */}
          <GuestSessionSidebar
            guestShareId={guestShareId!}
            selectedSessionId={selectedSessionId ?? null}
            onSelectSession={selectSession}
            onNewChat={() => {
              navigate({
                to: "/guest/$guestShareToken",
                params: { guestShareToken },
                search: { sessionId: undefined },
                replace: true,
              })
            }}
          />

          {/* Main chat area */}
          <div className="flex-1 flex flex-col min-h-0 min-w-0 relative">
            {selectedSessionId ? (
              <GuestChatArea
                sessionId={selectedSessionId}
                agentId={agentId}
                messageInputRef={messageInputRef}
                envPanelOpen={envPanelOpen}
                allowEnvPanel={allowEnvPanel}
              />
            ) : (
              <GuestNewChatState
                agentName={agentName}
                agentId={agentId!}
                guestShareId={guestShareId!}
                onSessionCreated={selectSession}
                messageInputRef={messageInputRef}
              />
            )}
          </div>
        </div>
      </div>
    </GuestShareProvider>
  )
}

// ── Loading Screen ──────────────────────────────────────────────────────

function GuestLoadingScreen({
  agentName,
  isAuthenticating,
}: {
  agentName: string
  isAuthenticating: boolean
}) {
  return (
    <div className="flex flex-col items-center justify-center h-screen bg-background">
      <div className="flex flex-col items-center gap-4">
        <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
          <Bot className="h-8 w-8 text-primary" />
        </div>
        <div className="flex items-center gap-2">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          <p className="text-muted-foreground">
            {isAuthenticating
              ? `Connecting to ${agentName}...`
              : "Loading..."}
          </p>
        </div>
      </div>
    </div>
  )
}

// ── Error Screen ────────────────────────────────────────────────────────

function GuestErrorScreen({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center h-screen bg-background">
      <div className="flex flex-col items-center gap-4 max-w-md text-center px-4">
        <div className="h-16 w-16 rounded-full bg-destructive/10 flex items-center justify-center">
          <AlertCircle className="h-8 w-8 text-destructive" />
        </div>
        <h1 className="text-xl font-semibold">Unable to access</h1>
        <p className="text-muted-foreground">{message}</p>
      </div>
    </div>
  )
}

// ── Security Code Screen ─────────────────────────────────────────────────

function SecurityCodeScreen({
  agentName,
  onSubmit,
}: {
  agentName: string
  onSubmit: (code: string) => Promise<void>
}) {
  const [digits, setDigits] = useState(["", "", "", ""])
  const [error, setError] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)
  const inputRefs = useRef<(HTMLInputElement | null)[]>([null, null, null, null])

  const handleDigitChange = (index: number, value: string) => {
    if (value && !/^\d$/.test(value)) return

    const newDigits = [...digits]
    newDigits[index] = value
    setDigits(newDigits)
    setError("")

    if (value && index < 3) {
      inputRefs.current[index + 1]?.focus()
    }

    // Auto-submit on first attempt when all digits are filled
    if (!error && newDigits.every((d) => d !== "")) {
      submitCode(newDigits.join(""))
    }
  }

  const handleKeyDown = (index: number, e: React.KeyboardEvent) => {
    if (e.key === "Backspace" && !digits[index] && index > 0) {
      inputRefs.current[index - 1]?.focus()
    }
    if (e.key === "Enter") {
      submitCode(digits.join(""))
    }
  }

  const handlePaste = (e: React.ClipboardEvent) => {
    e.preventDefault()
    const text = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 4)
    if (text.length > 0) {
      const newDigits = ["", "", "", ""]
      for (let i = 0; i < text.length; i++) {
        newDigits[i] = text[i]
      }
      setDigits(newDigits)
      const focusIndex = Math.min(text.length, 3)
      inputRefs.current[focusIndex]?.focus()

      // Auto-submit on first attempt when all digits are pasted
      if (!error && newDigits.every((d) => d !== "")) {
        submitCode(newDigits.join(""))
      }
    }
  }

  const submitCode = async (code: string) => {
    if (code.length !== 4 || isSubmitting) return

    setIsSubmitting(true)
    setError("")
    try {
      await onSubmit(code)
    } catch (err: any) {
      setError(err.message || "Incorrect security code")
      setIsSubmitting(false)
    }
  }

  const isFilled = digits.every((d) => d !== "")

  return (
    <div className="flex flex-col items-center justify-center h-screen bg-background">
      <div className="flex flex-col items-center gap-6 max-w-sm w-full px-4">
        <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
          <Bot className="h-8 w-8 text-primary" />
        </div>

        <div className="text-center">
          <h1 className="text-xl font-semibold">{agentName}</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Enter the 4-digit security code to continue
          </p>
        </div>

        <div className="flex gap-3" onPaste={handlePaste}>
          {digits.map((digit, i) => (
            <input
              key={i}
              ref={(el) => { inputRefs.current[i] = el }}
              type="text"
              inputMode="numeric"
              maxLength={1}
              value={digit}
              onChange={(e) => handleDigitChange(i, e.target.value)}
              onKeyDown={(e) => handleKeyDown(i, e)}
              autoFocus={i === 0}
              className="w-14 h-16 text-center text-2xl font-bold font-mono border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
              disabled={isSubmitting}
            />
          ))}
        </div>

        {error && (
          <p className="text-sm text-destructive text-center">{error}</p>
        )}

        <Button
          onClick={() => submitCode(digits.join(""))}
          disabled={!isFilled || isSubmitting}
          className="w-full"
          size="lg"
        >
          {isSubmitting ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Verifying...
            </>
          ) : (
            "Continue"
          )}
        </Button>
      </div>
    </div>
  )
}

// ── Header ──────────────────────────────────────────────────────────────

function GuestChatHeader({
  agentName,
  agentDescription,
  envPanelOpen,
  onToggleEnvPanel,
  hasSession,
  allowEnvPanel,
}: {
  agentName: string
  agentDescription: string | null
  envPanelOpen: boolean
  onToggleEnvPanel: () => void
  hasSession: boolean
  allowEnvPanel: boolean
}) {
  return (
    <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-4 border-b px-4 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-3 min-w-0 flex-1">
        <div className="h-8 w-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
          <Bot className="h-4 w-4 text-primary" />
        </div>
        <div className="min-w-0">
          <h1 className="text-sm font-semibold truncate">{agentName}</h1>
          <p className="text-xs text-muted-foreground truncate">
            {agentDescription || "Guest Session"}
          </p>
        </div>
      </div>
      {hasSession && allowEnvPanel && (
        <Button
          variant={envPanelOpen ? "secondary" : "ghost"}
          size="sm"
          onClick={onToggleEnvPanel}
          className="shrink-0"
        >
          <Package className="h-4 w-4 mr-1.5" />
          App
        </Button>
      )}
    </header>
  )
}

// ── Session Sidebar ─────────────────────────────────────────────────────

function GuestSessionSidebar({
  guestShareId,
  selectedSessionId,
  onSelectSession,
  onNewChat,
}: {
  guestShareId: string
  selectedSessionId: string | null
  onSelectSession: (id: string) => void
  onNewChat: () => void
}) {
  const {
    data: sessionsData,
    isLoading: sessionsLoading,
  } = useQuery({
    queryKey: ["guestSessions", guestShareId],
    queryFn: () =>
      SessionsService.listSessions({
        guestShareId,
        orderBy: "last_message_at",
        orderDesc: true,
        limit: 100,
      }),
    refetchInterval: 10000,
  })

  const sessions = sessionsData?.data || []

  return (
    <div className="w-64 border-r bg-muted/30 flex flex-col shrink-0 hidden md:flex">
      {/* New Chat button */}
      <div className="p-3 border-b">
        <Button
          onClick={onNewChat}
          variant="outline"
          size="sm"
          className="w-full gap-2"
        >
          <MessageSquarePlus className="h-4 w-4" />
          New Chat
        </Button>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto">
        {sessionsLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        ) : sessions.length === 0 ? (
          <div className="px-3 py-8 text-center">
            <p className="text-xs text-muted-foreground">No sessions yet</p>
          </div>
        ) : (
          <div className="py-1">
            {sessions.map((session: SessionPublicExtended) => (
              <button
                key={session.id}
                onClick={() => onSelectSession(session.id)}
                className={`w-full text-left px-3 py-2 text-sm transition-colors hover:bg-accent ${
                  selectedSessionId === session.id
                    ? "bg-accent"
                    : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">
                      {session.title || `Session`}
                    </p>
                    <div className="flex items-center gap-1 text-xs text-muted-foreground mt-0.5">
                      <Clock className="h-3 w-3" />
                      <span>
                        {new Date(
                          session.last_message_at || session.created_at
                        ).toLocaleDateString()}
                      </span>
                    </div>
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── New Chat State (deferred session creation) ──────────────────────────

function GuestNewChatState({
  agentName,
  agentId,
  guestShareId,
  onSessionCreated,
  messageInputRef,
}: {
  agentName: string
  agentId: string
  guestShareId: string
  onSessionCreated: (id: string) => void
  messageInputRef: React.RefObject<HTMLTextAreaElement | null>
}) {
  const queryClient = useQueryClient()
  const [isSending, setIsSending] = useState(false)

  const handleSendFirstMessage = useCallback(
    async (content: string) => {
      if (!content.trim() || isSending) return
      setIsSending(true)
      try {
        const session = await SessionsService.createSession({
          requestBody: {
            agent_id: agentId,
            mode: "conversation",
            guest_share_id: guestShareId,
          },
        })
        await MessagesService.sendMessageStream({
          sessionId: session.id,
          requestBody: { content: content.trim() },
        })
        queryClient.invalidateQueries({ queryKey: ["guestSessions", guestShareId] })
        onSessionCreated(session.id)
      } catch (error) {
        console.error("Failed to create session:", error)
        setIsSending(false)
      }
    },
    [agentId, guestShareId, onSessionCreated, queryClient, isSending]
  )

  return (
    <>
      <div className="flex-1 flex flex-col items-center justify-center gap-3 px-4">
        <div className="h-12 w-12 rounded-full bg-primary/10 flex items-center justify-center">
          <Bot className="h-6 w-6 text-primary" />
        </div>
        <p className="text-muted-foreground text-sm">
          Start chatting with {agentName}
        </p>
      </div>
      <MessageInput
        ref={messageInputRef}
        onSend={async (content) => { await handleSendFirstMessage(content) }}
        onStop={() => {}}
        sendDisabled={isSending}
        isInterruptPending={false}
        placeholder={isSending ? "Starting session..." : "Type your message..."}
        agentId={agentId}
        mode="conversation"
      />
    </>
  )
}

// ── Chat Area ───────────────────────────────────────────────────────────

function GuestChatArea({
  sessionId,
  agentId,
  messageInputRef,
  envPanelOpen,
  allowEnvPanel,
}: {
  sessionId: string
  agentId: string | null
  messageInputRef: React.RefObject<HTMLTextAreaElement | null>
  envPanelOpen: boolean
  allowEnvPanel: boolean
}) {
  const [isSessionStreaming, setIsSessionStreaming] = useState(false)

  const {
    data: session,
    isLoading: sessionLoading,
  } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => SessionsService.getSession({ id: sessionId }),
    enabled: !!sessionId,
    refetchInterval: isSessionStreaming ? 3000 : 10000,
  })

  // Derive streaming state from session
  useEffect(() => {
    const streaming =
      session?.interaction_status === "running" ||
      session?.interaction_status === "pending_stream"
    setIsSessionStreaming(streaming)
  }, [session?.interaction_status])

  const {
    data: messagesData,
    isLoading: messagesLoading,
  } = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () =>
      MessagesService.getMessages({ sessionId, offset: 0, limit: 100 }),
    enabled: !!sessionId,
    refetchInterval: isSessionStreaming ? 2000 : undefined,
  })

  const {
    sendMessage,
    stopMessage,
    isStreaming,
    streamingEvents,
    isInterruptPending,
  } = useSessionStreaming({
    sessionId,
    session: session
      ? { interaction_status: session.interaction_status, mode: session.mode }
      : null,
    messagesData: messagesData
      ? { data: messagesData.data as any }
      : null,
    onError: (error) => {
      console.error("Guest message error:", error)
    },
  })

  const handleSendMessage = useCallback(
    async (content: string, fileIds?: string[]) => {
      await sendMessage(content, undefined, fileIds)
    },
    [sendMessage]
  )

  const handleSendAnswer = useCallback(
    async (content: string, answersToMessageId: string) => {
      await sendMessage(content, answersToMessageId)
    },
    [sendMessage]
  )

  const handleSendSimpleMessage = useCallback(
    async (content: string) => {
      await sendMessage(content)
    },
    [sendMessage]
  )

  // Auto-focus input when loaded
  useEffect(() => {
    if (!sessionLoading && !messagesLoading && messageInputRef.current) {
      messageInputRef.current.focus()
    }
  }, [sessionLoading, messagesLoading, messageInputRef])

  if (sessionLoading || messagesLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (!session) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-muted-foreground">Session not found</p>
      </div>
    )
  }

  const messages = messagesData?.data || []

  return (
    <>
      <div className="flex flex-col flex-1 min-h-0 relative">
        <MessageList
          messages={messages}
          isLoading={messagesLoading}
          streamingEvents={streamingEvents}
          isStreaming={isStreaming}
          onSendAnswer={handleSendAnswer}
          onSendMessage={handleSendSimpleMessage}
          conversationModeUi="compact"
          agentId={agentId ?? undefined}
          sessionId={sessionId}
        />
        {allowEnvPanel && (
          <EnvironmentPanel
            isOpen={envPanelOpen}
            environmentId={session.environment_id}
            agentId={agentId ?? undefined}
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
        agentId={agentId ?? undefined}
        mode="conversation"
      />
    </>
  )
}
