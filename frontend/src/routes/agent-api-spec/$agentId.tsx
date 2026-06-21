import { createFileRoute, redirect } from "@tanstack/react-router"
import { AlertCircle, Loader2, RefreshCw } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { AgentApiService } from "@/client"
import { OpenApiSpecViewer } from "@/components/Agents/OpenApiSpecViewer"
import { Button } from "@/components/ui/button"
import type { AgentApiStatus } from "@/hooks/useAgentApi"
import { isLoggedIn } from "@/hooks/useAuth"

/**
 * Standalone, full-page viewer for a producer agent's harvested OpenAPI spec,
 * opened in its own browser tab from the "View Spec" buttons (producer
 * Integrations card and consumer connection credential).
 *
 * Auth: this is a normal SPA route, so it runs inside the same app shell and
 * reuses the JWT in localStorage (``OpenAPI.TOKEN``) — the spec is fetched
 * through the authenticated client exactly like every other request, no
 * separate token handling. ``beforeLoad`` bounces unauthenticated tabs to login.
 *
 * Wake-on-open: the producer env may be suspended (idle). Instead of immediately
 * showing "could not load", we poll ``POST /_refresh`` — which wakes a suspended
 * env and re-harvests the spec — showing a "Waking up agent..." loader until the
 * env reports ``running``, then fetch and render the spec. This mirrors the
 * webapp owner-status poll in ``WebAppView``.
 *
 * Rendering: a custom, read-only ``OpenApiSpecViewer`` (built from the app's
 * own primitives, themed to match) renders the spec object fetched here.
 */
export const Route = createFileRoute("/agent-api-spec/$agentId")({
  component: AgentApiSpecPage,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
  },
  head: () => ({
    meta: [{ title: "Agent REST API — Spec" }],
  }),
})

type ViewerPhase = "waking" | "loading" | "ready" | "error"

// Pause between polls. _refresh blocks up to ~10s server-side while activating,
// so we chain (await + sleep) rather than a fixed interval — a fixed interval
// would stack overlapping blocking calls (each kicking off another activation).
const POLL_PAUSE_MS = 1500
// Stop waking after this many failed/booting polls so a genuinely broken
// producer surfaces the error state instead of spinning forever.
const MAX_WAKE_POLLS = 12

function AgentApiSpecPage() {
  const { agentId } = Route.useParams()

  const [phase, setPhase] = useState<ViewerPhase>("waking")
  const [spec, setSpec] = useState<Record<string, unknown> | null>(null)
  const [errorMessage, setErrorMessage] = useState("")

  // Drives the chained poll loop: set false to abort (unmount / new run).
  const pollActiveRef = useRef(false)
  // Identifies the current run so a stale loop (after a Retry restart) self-cancels.
  const activeRunRef = useRef<object | null>(null)

  const startPolling = useCallback(() => {
    // Abort any in-flight loop, then begin a fresh one.
    pollActiveRef.current = false
    const runId = {}
    activeRunRef.current = runId
    pollActiveRef.current = true

    setPhase("waking")
    setSpec(null)
    setErrorMessage("")

    const sleep = (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms))

    const isCurrent = () => pollActiveRef.current && activeRunRef.current === runId

    const loop = async () => {
      let attempt = 0
      while (isCurrent() && attempt < MAX_WAKE_POLLS) {
        attempt += 1
        let status: AgentApiStatus
        try {
          // _refresh wakes a suspended env, re-harvests when running, and
          // returns the live status.
          status = (await AgentApiService.refreshAgentApiStatus({
            agentId,
          })) as AgentApiStatus
        } catch {
          if (!isCurrent()) return
          setErrorMessage("Could not reach the agent. Please try again.")
          setPhase("error")
          return
        }

        if (!isCurrent()) return

        if (status.state === "disabled") {
          setErrorMessage(
            "The Agent REST API is disabled for this agent. Enable it on the agent's Integrations tab.",
          )
          setPhase("error")
          return
        }

        // Mirrors AgentRestApiCard: the serving child app being idle/stopped is
        // irrelevant — the spec is served from cache / import-only harvest
        // without it. The ONLY wakeable case is `not_running` (env genuinely not
        // up yet). Once the env is running every outcome is terminal: ready,
        // failure (`state === "error"` or `last_error`), or empty (nothing to
        // harvest — no endpoints exposed).
        const envRunning = status.state !== "not_running"
        const harvestFailed = status.state === "error" || !!status.last_error
        const specReady = !!status.spec_available && !status.last_error

        if (envRunning && specReady) {
          setPhase("loading")
          try {
            const data = (await AgentApiService.getAgentApiSpec({
              agentId,
            })) as Record<string, unknown>
            if (!isCurrent()) return
            setSpec(data)
            setPhase("ready")
          } catch {
            if (!isCurrent()) return
            setErrorMessage(
              "The producer's API may be failing to build. Check the build status on the agent's Integrations tab and try again.",
            )
            setPhase("error")
          }
          return
        }

        // Genuine harvest/boot error on a running env — surface it instead of
        // spinning (the spec will never come without a fix).
        if (envRunning && harvestFailed) {
          setErrorMessage(
            status.last_error ||
              "The producer's API is failing to build. Check the build status on the agent's Integrations tab and try again.",
          )
          setPhase("error")
          return
        }

        // Running but nothing to harvest — no endpoints exposed. Terminal: there
        // is no spec to render, so stop polling and explain (don't spin to the
        // budget then show a misleading "not running" message).
        if (envRunning) {
          setErrorMessage(
            "This agent's REST API exposes no endpoints yet. Add endpoints under the agent's agent_api/ directory and refresh.",
          )
          setPhase("error")
          return
        }

        // not_running / still activating — keep polling until it comes up or we
        // exhaust the budget (a producer that never boots).
        if (attempt >= MAX_WAKE_POLLS) {
          setErrorMessage(
            status.last_error ||
              "The producer's API is not running. Check the build status on the agent's Integrations tab and try again.",
          )
          setPhase("error")
          return
        }
        await sleep(POLL_PAUSE_MS)
      }
    }

    void loop()
  }, [agentId])

  useEffect(() => {
    startPolling()
    return () => {
      pollActiveRef.current = false
    }
  }, [startPolling])

  if (phase === "waking" || phase === "loading") {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-background p-6">
        <div className="flex flex-col items-center gap-2 text-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          <p className="text-sm text-muted-foreground">
            {phase === "waking" ? "Waking up agent..." : "Loading spec..."}
          </p>
        </div>
      </div>
    )
  }

  if (phase === "error" || !spec) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-background p-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <AlertCircle className="h-7 w-7 text-muted-foreground" />
          <p className="text-base font-medium">Could not load the OpenAPI spec</p>
          <p className="max-w-md text-sm text-muted-foreground">
            {errorMessage ||
              "The producer's API may be stopped or failing to build. Check the build status on the agent's Integrations tab and try again."}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="gap-1"
            onClick={startPolling}
          >
            <RefreshCw className="h-4 w-4" />
            Retry
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="h-screen w-screen overflow-auto bg-background">
      <OpenApiSpecViewer spec={spec} />
    </div>
  )
}
