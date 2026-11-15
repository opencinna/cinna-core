import type { RefObject } from "react"
import { useState, useEffect, useRef, useCallback } from "react"
import { Globe, Loader2, AlertCircle, RefreshCw } from "lucide-react"
import { OpenAPI, WebappService } from "@/client"
import { Button } from "@/components/ui/button"
import { getAccessToken } from "@/lib/authSession"

interface WebAppViewProps {
  agentId: string
  webappEnabled: boolean
  iframeRef?: RefObject<HTMLIFrameElement | null>
}

type ActivationStatus = "checking" | "activating" | "running" | "error"

interface OwnerStatusResponse {
  status: "running" | "activating" | "error"
  step: "ready" | "waking_up" | "loading_app"
  message?: string
}

const STEP_MESSAGES: Record<string, string> = {
  waking_up: "Waking up agent...",
  loading_app: "Loading app...",
}

export function WebAppView({ agentId, webappEnabled, iframeRef }: WebAppViewProps) {
  const [activationStatus, setActivationStatus] = useState<ActivationStatus>("checking")
  const [stepMessage, setStepMessage] = useState("Connecting...")
  const [errorMessage, setErrorMessage] = useState("")
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const isMountedRef = useRef(true)

  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current !== null) {
      clearInterval(pollIntervalRef.current)
      pollIntervalRef.current = null
    }
  }, [])

  const checkStatus = useCallback(async () => {
    try {
      const result = (await WebappService.getWebappOwnerStatus({
        agentId,
      })) as OwnerStatusResponse

      if (!isMountedRef.current) return

      if (result.status === "running") {
        setActivationStatus("running")
        stopPolling()
      } else if (result.status === "activating") {
        setActivationStatus("activating")
        setStepMessage(STEP_MESSAGES[result.step] ?? "Waking up agent...")
      } else {
        // error status
        setActivationStatus("error")
        setErrorMessage(result.message ?? "Environment is unavailable.")
        stopPolling()
      }
    } catch {
      if (!isMountedRef.current) return
      setActivationStatus("error")
      setErrorMessage("Could not reach the agent. Please try again.")
      stopPolling()
    }
  }, [agentId, stopPolling])

  const startPolling = useCallback(() => {
    stopPolling()
    checkStatus()
    pollIntervalRef.current = setInterval(checkStatus, 2000)
  }, [checkStatus, stopPolling])

  const handleRetry = useCallback(() => {
    setActivationStatus("checking")
    setStepMessage("Connecting...")
    setErrorMessage("")
    startPolling()
  }, [startPolling])

  useEffect(() => {
    isMountedRef.current = true

    if (!webappEnabled) {
      // Webapp is disabled — no polling needed; show placeholder below.
      return
    }

    startPolling()

    return () => {
      isMountedRef.current = false
      stopPolling()
    }
  }, [agentId, webappEnabled, startPolling, stopPolling])

  // ── Not enabled ──────────────────────────────────────────────────────────

  if (!webappEnabled) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-4 text-center">
        <Globe className="h-8 w-8 text-muted-foreground mb-2" />
        <p className="text-sm text-muted-foreground">
          Web App not enabled for this agent
        </p>
        <p className="text-xs text-muted-foreground mt-1">
          Enable it in agent settings
        </p>
      </div>
    )
  }

  // ── Checking / activating ────────────────────────────────────────────────

  if (activationStatus === "checking" || activationStatus === "activating") {
    return (
      <div className="flex flex-col items-center justify-center h-full p-4 text-center gap-2">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        <p className="text-xs text-muted-foreground">{stepMessage}</p>
      </div>
    )
  }

  // ── Error ────────────────────────────────────────────────────────────────

  if (activationStatus === "error") {
    return (
      <div className="flex flex-col items-center justify-center h-full p-4 text-center gap-2">
        <AlertCircle className="h-6 w-6 text-muted-foreground" />
        <p className="text-xs text-muted-foreground">{errorMessage}</p>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 text-xs gap-1"
          onClick={handleRetry}
        >
          <RefreshCw className="h-3 w-3" />
          Retry
        </Button>
      </div>
    )
  }

  // ── Running — render the iframe ──────────────────────────────────────────

  const baseUrl = OpenAPI.BASE || ""
  // The iframe keeps the token it loaded with; past its 15 min the webapp
  // needs a reload (accepted, plan §13 Q2).
  const accessToken = getAccessToken() || ""
  const webappUrl = `${baseUrl}/api/v1/agents/${agentId}/webapp/?token=${encodeURIComponent(accessToken)}`

  return (
    <iframe
      ref={iframeRef}
      src={webappUrl}
      className="w-full h-full border-0"
      sandbox="allow-scripts allow-same-origin allow-forms"
      title="Agent Web App"
    />
  )
}
