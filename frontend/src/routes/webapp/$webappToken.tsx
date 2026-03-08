import { createFileRoute } from "@tanstack/react-router"
import { useState, useEffect, useRef, useCallback } from "react"
import { useQuery } from "@tanstack/react-query"
import { WebappShareService } from "@/client"
import { Button } from "@/components/ui/button"
import { Loader2, AlertCircle, Globe } from "lucide-react"
import { WebappChatWidget } from "@/components/Webapp/WebappChatWidget"
import { eventService } from "@/services/eventService"

export const Route = createFileRoute("/webapp/$webappToken")({
  component: WebappPage,
  validateSearch: (search: Record<string, unknown>) => ({
    embed: search.embed === "1" || search.embed === true ? true : undefined,
  }),
  head: () => ({
    meta: [{ title: "Web App" }],
  }),
})

// ── Types ────────────────────────────────────────────────────────────────

interface WebappInterfaceConfig {
  show_header: boolean
  chat_mode: string | null  // "conversation" | "building" | null
}

interface WebappShareInfoResponse {
  agent_name: string | null
  is_valid: boolean
  webapp_share_id: string | null
  requires_code: boolean
  is_code_blocked: boolean
  interface_config?: WebappInterfaceConfig
}

interface WebappShareAuthResponse {
  access_token: string
  token_type: string
  webapp_share_id: string
  agent_id: string
}

// ── Helpers ──────────────────────────────────────────────────────────────

const WEBAPP_TOKEN_KEY = "webapp_access_token"

function parseWebappJwt(
  token: string
): { sub: string; agent_id: string; exp: number } | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]))
    if (payload.role === "webapp-viewer" && payload.token_type === "webapp_share") {
      return { sub: payload.sub, agent_id: payload.agent_id, exp: payload.exp }
    }
  } catch {
    // Malformed token
  }
  return null
}

// ── Main Component ───────────────────────────────────────────────────────

function WebappPage() {
  const { webappToken } = Route.useParams()
  const { embed } = Route.useSearch()

  const [authState, setAuthState] = useState<
    "loading" | "code_entry" | "authenticating" | "ready" | "error"
  >("loading")
  const [errorMessage, setErrorMessage] = useState("")
  const [agentName, setAgentName] = useState("")
  const [showHeader, setShowHeader] = useState(true)
  const [chatMode, setChatMode] = useState<"conversation" | "building" | null>(null)

  const authAttempted = useRef(false)
  // iframeRef is passed to the chat widget so it can collect schema.org microdata
  // from the webapp page via postMessage (context aggregation feature).
  // Must be declared here (before any conditional returns) to satisfy Rules of Hooks.
  const iframeRef = useRef<HTMLIFrameElement>(null)

  // Step 1: Fetch share info
  const {
    data: shareInfo,
    isLoading: infoLoading,
    error: infoError,
  } = useQuery({
    queryKey: ["webappShareInfo", webappToken],
    queryFn: async () => {
      const result = await WebappShareService.webappShareInfo({
        token: webappToken,
      })
      return result as unknown as WebappShareInfoResponse
    },
    retry: false,
  })

  // Step 2: Auth flow
  useEffect(() => {
    if (authAttempted.current) return
    if (infoLoading || infoError) return
    if (!shareInfo) return

    if (!shareInfo.is_valid) {
      setAuthState("error")
      setErrorMessage(
        shareInfo.webapp_share_id
          ? "This webapp link has expired. Contact the owner for a new link."
          : "This webapp link is invalid or has been removed."
      )
      return
    }

    setAgentName(shareInfo.agent_name || "Agent")
    if (shareInfo.interface_config) {
      setShowHeader(shareInfo.interface_config.show_header)
      setChatMode(
        shareInfo.interface_config.chat_mode as "conversation" | "building" | null
      )
    }

    if (shareInfo.is_code_blocked) {
      authAttempted.current = true
      setAuthState("error")
      setErrorMessage(
        "This link has been blocked due to too many failed attempts. Contact the owner."
      )
      return
    }

    // Check for existing valid webapp JWT
    const existingToken = localStorage.getItem(WEBAPP_TOKEN_KEY)
    if (existingToken) {
      const claims = parseWebappJwt(existingToken)
      if (claims && claims.exp * 1000 > Date.now()) {
        authAttempted.current = true
        setAuthState("ready")
        return
      }
      localStorage.removeItem(WEBAPP_TOKEN_KEY)
    }

    if (shareInfo.requires_code) {
      authAttempted.current = true
      setAuthState("code_entry")
      return
    }

    // No code required
    authAttempted.current = true
    setAuthState("authenticating")
    performAuth()
  }, [shareInfo, infoLoading, infoError])

  const performAuth = useCallback(
    async (securityCode?: string) => {
      try {
        setAuthState("authenticating")
        const result = (await WebappShareService.webappShareAuthenticate({
          token: webappToken,
          requestBody: securityCode ? { security_code: securityCode } : undefined,
        })) as unknown as WebappShareAuthResponse

        localStorage.setItem(WEBAPP_TOKEN_KEY, result.access_token)
        setAuthState("ready")
      } catch (error: any) {
        if (error?.status === 403) {
          const detail = error?.body?.detail || ""
          if (detail.toLowerCase().includes("blocked")) {
            setAuthState("error")
            setErrorMessage(
              "This link has been blocked due to too many failed attempts. Contact the owner."
            )
          } else {
            throw error // Re-throw for code entry to handle
          }
        } else if (error?.status === 410) {
          setAuthState("error")
          setErrorMessage("This webapp link has expired. Contact the owner for a new link.")
        } else if (error?.status === 404) {
          setAuthState("error")
          setErrorMessage("This webapp link is invalid or has been removed.")
        } else {
          setAuthState("error")
          setErrorMessage("Something went wrong. Please try again.")
        }
      }
    },
    [webappToken]
  )

  useEffect(() => {
    if (infoError) {
      setAuthState("error")
      setErrorMessage("Something went wrong. Please try again.")
    }
  }, [infoError])

  // Connect to Socket.IO for real-time events when chat is enabled
  useEffect(() => {
    if (authState !== "ready" || !chatMode) return

    const jwt = localStorage.getItem(WEBAPP_TOKEN_KEY)
    if (!jwt) return
    const claims = parseWebappJwt(jwt)
    if (!claims) return

    // Connect using webapp_share_id (from JWT sub) as user identifier
    eventService.connect(claims.sub)

    return () => {
      eventService.disconnect()
    }
  }, [authState, chatMode])

  // ── Render states ────────────────────────────────────────────────────

  if (authState === "loading" || authState === "authenticating") {
    return (
      <div className="flex flex-col items-center justify-center h-screen bg-background">
        <div className="flex flex-col items-center gap-4">
          <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
            <Globe className="h-8 w-8 text-primary" />
          </div>
          <div className="flex items-center gap-2">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            <p className="text-muted-foreground">
              {authState === "authenticating"
                ? `Connecting to ${agentName || "the app"}...`
                : "Loading..."}
            </p>
          </div>
        </div>
      </div>
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
    return (
      <div className="flex flex-col items-center justify-center h-screen bg-background">
        <div className="flex flex-col items-center gap-4 max-w-md text-center px-4">
          <div className="h-16 w-16 rounded-full bg-destructive/10 flex items-center justify-center">
            <AlertCircle className="h-8 w-8 text-destructive" />
          </div>
          <h1 className="text-xl font-semibold">Unable to access</h1>
          <p className="text-muted-foreground">{errorMessage}</p>
        </div>
      </div>
    )
  }

  // Ready — render full-page iframe
  const iframeSrc = `${import.meta.env.VITE_API_URL}/api/v1/webapp/${webappToken}/`

  const chatWidget = chatMode ? (
    <WebappChatWidget
      webappToken={webappToken}
      chatMode={chatMode}
      agentName={agentName || "Agent"}
      iframeRef={iframeRef}
    />
  ) : null

  if (embed) {
    return (
      <>
        <iframe
          ref={iframeRef}
          src={iframeSrc}
          className="w-full h-screen border-0"
          title="Agent Web App"
        />
        {chatWidget}
      </>
    )
  }

  if (!showHeader) {
    return (
      <>
        <iframe
          ref={iframeRef}
          src={iframeSrc}
          className="w-full h-screen border-0"
          title="Agent Web App"
        />
        {chatWidget}
      </>
    )
  }

  return (
    <div className="flex flex-col h-screen bg-background">
      {/* Thin header */}
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-4 bg-background/95 backdrop-blur">
        <div className="h-7 w-7 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
          <Globe className="h-4 w-4 text-primary" />
        </div>
        <h1 className="text-sm font-semibold truncate">{agentName}</h1>
      </header>
      <iframe
        ref={iframeRef}
        src={iframeSrc}
        className="flex-1 w-full border-0"
        title="Agent Web App"
      />
      {chatWidget}
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
          <Globe className="h-8 w-8 text-primary" />
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
              ref={(el) => {
                inputRefs.current[i] = el
              }}
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
