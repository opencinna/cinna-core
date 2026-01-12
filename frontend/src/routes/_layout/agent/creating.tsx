import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useRef, useCallback } from "react"
import { CheckCircle2, Circle, Loader2, XCircle, AlertTriangle, Key } from "lucide-react"
import { OpenAPI, CredentialsService, AgentsService, SessionsService } from "@/client"
import { useQuery } from "@tanstack/react-query"
import useWorkspace from "@/hooks/useWorkspace"

type SearchParams = {
  description: string
  mode: "conversation" | "building"
}

export const Route = createFileRoute("/_layout/agent/creating")({
  component: AgentCreating,
  validateSearch: (search: Record<string, unknown>): SearchParams => {
    return {
      description: (search.description as string) || "",
      mode: (search.mode as "conversation" | "building") || "building",
    }
  },
})

type Step = {
  id: string
  label: string
  status: "pending" | "in_progress" | "completed" | "error"
  message?: string
}

function AgentCreating() {
  const navigate = useNavigate()
  const { description, mode } = Route.useSearch()
  const { activeWorkspaceId } = useWorkspace()
  const [steps, setSteps] = useState<Step[]>([
    { id: "create_agent", label: "Creating agent", status: "pending" },
    { id: "start_environment", label: "Starting default environment", status: "pending" },
    { id: "create_session", label: "Creating conversation session", status: "pending" },
    { id: "redirect", label: "Ready to start", status: "pending" },
  ])
  const [error, setError] = useState<string | null>(null)
  const hasStartedRef = useRef(false)
  const [selectedCredentialIds, setSelectedCredentialIds] = useState<Set<string>>(new Set())
  const [agentId, setAgentId] = useState<string | null>(null)
  const [environmentReady, setEnvironmentReady] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [countdown, setCountdown] = useState(5)
  const [isCountingDown, setIsCountingDown] = useState(false)

  // Fetch user's credentials
  const { data: credentialsData } = useQuery({
    queryKey: ["credentials", activeWorkspaceId],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey
      const response = await CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
      })
      return response
    },
  })

  const updateStepStatus = (
    stepId: string,
    status: Step["status"],
    message?: string,
  ) => {
    setSteps((prev) =>
      prev.map((step) =>
        step.id === stepId ? { ...step, status, message } : step,
      ),
    )
  }

  useEffect(() => {
    // Prevent duplicate requests (React 18 Strict Mode runs effects twice)
    if (hasStartedRef.current) {
      return
    }
    hasStartedRef.current = true

    const createAgentFlow = async () => {
      try {
        updateStepStatus("create_agent", "in_progress")

        // Get the access token from OpenAPI config
        const token = typeof OpenAPI.TOKEN === "function"
          ? await OpenAPI.TOKEN({} as any)
          : OpenAPI.TOKEN || ""

        if (!token) {
          throw new Error("Not authenticated")
          return
        }

        // Make request to SSE endpoint using OpenAPI.BASE
        const response = await fetch(`${OpenAPI.BASE}/api/v1/agents/create-flow`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            description,
            mode,
            auto_create_session: false,  // We'll create session after credential sharing
            user_workspace_id: activeWorkspaceId || undefined,
          }),
        })

        if (!response.ok) {
          throw new Error(`Failed to start agent creation: ${response.statusText}`)
        }

        // Read the stream
        const reader = response.body?.getReader()
        const decoder = new TextDecoder()

        if (!reader) {
          throw new Error("No response body")
        }

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          const chunk = decoder.decode(value)
          const lines = chunk.split("\n\n")

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const data = JSON.parse(line.substring(6))

              switch (data.step) {
                case "creating_agent":
                  updateStepStatus("create_agent", "in_progress", data.message)
                  break
                case "agent_created":
                  updateStepStatus("create_agent", "completed", data.message)
                  setAgentId(data.agent_id)
                  updateStepStatus("start_environment", "in_progress")
                  break
                case "environment_starting":
                  updateStepStatus("start_environment", "in_progress", data.message)
                  break
                case "environment_ready":
                  updateStepStatus("start_environment", "completed", data.message)
                  setEnvironmentReady(true)
                  // Don't auto-proceed to session creation yet
                  // Will be handled by countdown/button logic
                  break
                case "session_creating":
                  updateStepStatus("create_session", "in_progress", data.message)
                  break
                case "completed":
                  // Backend stopped after environment is ready
                  // Frontend will handle credential sharing and session creation
                  break
                case "error":
                  updateStepStatus(
                    data.current_step || "create_agent",
                    "error",
                    data.message,
                  )
                  setError(data.message || "An error occurred during agent creation")
                  break
              }
            }
          }
        }
      } catch (err: any) {
        setError(err.message || "Failed to start agent creation process")
        updateStepStatus("create_agent", "error")
      }
    }

    createAgentFlow()
  }, [description, mode, navigate])

  // Handle manual start session (shares credentials silently before redirect)
  const handleStartSession = useCallback(async () => {
    if (!sessionId || !agentId) return

    try {
      // Silently share selected credentials in the background
      if (selectedCredentialIds.size > 0) {
        await Promise.all(
          Array.from(selectedCredentialIds).map(credentialId =>
            AgentsService.addCredentialToAgent({
              id: agentId,
              requestBody: { credential_id: credentialId }
            }).catch(err => {
              console.error(`Failed to share credential ${credentialId}:`, err)
              // Continue even if one fails
            })
          )
        )
      }

      // Redirect to session
      navigate({
        to: "/session/$sessionId",
        params: { sessionId },
        search: { initialMessage: description, fileIds: undefined },
      })
    } catch (err: any) {
      console.error("Error during session start:", err)
      // Redirect anyway
      navigate({
        to: "/session/$sessionId",
        params: { sessionId },
        search: { initialMessage: description, fileIds: undefined },
      })
    }
  }, [sessionId, agentId, selectedCredentialIds, navigate, description])

  // Handle post-environment-ready flow: create session -> countdown
  useEffect(() => {
    if (!environmentReady || !agentId) return

    const handlePostEnvironmentFlow = async () => {
      try {
        // Create session
        updateStepStatus("create_session", "in_progress")

        const newSession = await SessionsService.createSession({
          requestBody: {
            agent_id: agentId,
            mode,
            title: null
          }
        })

        setSessionId(newSession.id)
        updateStepStatus("create_session", "completed")

        // Start countdown or show button
        if (selectedCredentialIds.size === 0) {
          // No credentials selected - start 5 second countdown
          updateStepStatus("redirect", "in_progress")
          setIsCountingDown(true)
        } else {
          // Credentials were selected - show "Start Session" button (everything is ready)
          updateStepStatus("redirect", "completed", "Click 'Start Session' to begin")
        }

      } catch (err: any) {
        setError(err.message || "Failed to complete setup")
        updateStepStatus("create_session", "error")
      }
    }

    handlePostEnvironmentFlow()
  }, [environmentReady, agentId, mode])

  // Handle countdown timer
  useEffect(() => {
    if (!isCountingDown || !sessionId) return

    if (countdown > 0) {
      const timer = setTimeout(() => {
        setCountdown(countdown - 1)
      }, 1000)
      return () => clearTimeout(timer)
    } else {
      // Countdown finished - share credentials and redirect
      handleStartSession()
    }
  }, [isCountingDown, countdown, sessionId, handleStartSession])

  // Handle credential selection toggle
  const toggleCredential = (credentialId: string) => {
    setSelectedCredentialIds((prev) => {
      const newSet = new Set(prev)
      if (newSet.has(credentialId)) {
        newSet.delete(credentialId)
      } else {
        newSet.add(credentialId)
      }
      return newSet
    })
  }

  const getStepIcon = (status: Step["status"]) => {
    switch (status) {
      case "completed":
        return <CheckCircle2 className="h-5 w-5 text-green-600" />
      case "in_progress":
        return <Loader2 className="h-5 w-5 text-blue-600 animate-spin" />
      case "error":
        return <XCircle className="h-5 w-5 text-red-600" />
      default:
        return <Circle className="h-5 w-5 text-gray-300" />
    }
  }

  const credentials = credentialsData?.data || []
  const hasCredentials = credentials.length > 0

  return (
    <>
      <style>{`
        @keyframes breathe {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.05); }
        }
      `}</style>
      <div className="flex items-center justify-center min-h-[calc(100vh-4rem)] p-6">
        <div className="w-full max-w-5xl space-y-6">
        {/* Header */}
        <div className="text-center space-y-2">
          <h1 className="text-2xl font-semibold">Creating Your Agent</h1>
          <p className="text-muted-foreground">
            {!environmentReady
              ? "Please wait while we set up your new agent..."
              : "Your agent is ready! Start your session when ready."}
          </p>
        </div>

        {/* Warning */}
        <div className="flex items-start gap-3 p-4 bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-900 rounded-lg">
          <AlertTriangle className="h-5 w-5 text-amber-600 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-amber-900 dark:text-amber-200">
            <strong className="font-medium">Do not close this page</strong>
            <br />
            Closing the browser or navigating away will interrupt the agent creation
            process.
          </div>
        </div>

        {/* Main Content: Progress Steps (Left) + Credentials (Right) */}
        <div className="bg-card border rounded-lg p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Left Column: Progress Steps */}
            <div className="space-y-4">
              <h3 className="font-semibold text-sm text-muted-foreground mb-4">Progress</h3>
              {steps.map((step) => (
                <div key={step.id} className="flex items-start gap-3">
                  <div className="flex-shrink-0 mt-0.5">{getStepIcon(step.status)}</div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{step.label}</span>
                      {step.status === "in_progress" && (
                        <span className="text-xs text-muted-foreground animate-pulse">
                          Processing...
                        </span>
                      )}
                    </div>
                    {step.message && (
                      <p className="text-xs text-muted-foreground mt-1">
                        {step.message}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Right Column: Credential Selection */}
            {hasCredentials && (
              <div className="space-y-4">
                <div className="flex items-center gap-2 mb-4">
                  <Key className="h-4 w-4 text-muted-foreground" />
                  <h3 className="font-semibold text-sm text-muted-foreground">
                    Share Credentials {selectedCredentialIds.size > 0 && `(${selectedCredentialIds.size})`}
                  </h3>
                </div>
                <div className="space-y-1 max-h-[300px] overflow-y-auto pr-2">
                  {credentials.map((credential) => (
                    <label
                      key={credential.id}
                      className="flex items-center gap-2 px-2 py-1.5 cursor-pointer hover:bg-accent/50 rounded-lg transition-colors text-sm group"
                    >
                      <div className="relative flex items-center justify-center">
                        <input
                          type="checkbox"
                          checked={selectedCredentialIds.has(credential.id)}
                          onChange={() => toggleCredential(credential.id)}
                          className="peer h-4 w-4 cursor-pointer appearance-none rounded-full border-2 border-gray-300 bg-white checked:border-blue-500 checked:bg-blue-500 hover:border-blue-400 transition-all focus:outline-none focus:ring-2 focus:ring-blue-400 focus:ring-offset-1"
                        />
                        <svg
                          className="absolute h-3 w-3 text-white pointer-events-none hidden peer-checked:block"
                          xmlns="http://www.w3.org/2000/svg"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="3"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <polyline points="20 6 9 17 4 12"></polyline>
                        </svg>
                      </div>
                      <span className="flex-1 truncate font-medium">{credential.name}</span>
                      <span className="px-2 py-0.5 bg-muted text-muted-foreground text-xs rounded-full flex-shrink-0">
                        {credential.type}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Start Session Button/Countdown */}
        {environmentReady && sessionId && (
          <div className="bg-card border rounded-lg p-6 text-center space-y-4">
            <div className="text-lg font-semibold text-green-600 dark:text-green-400">
              Environment Ready!
            </div>
            {isCountingDown ? (
              <div className="space-y-3">
                <p className="text-muted-foreground">
                  Starting session in {countdown} second{countdown !== 1 ? 's' : ''}...
                </p>
                <button
                  onClick={() => {
                    setIsCountingDown(false)
                    handleStartSession()
                  }}
                  className="px-8 py-3 text-base font-medium bg-gradient-to-r from-blue-500 to-purple-600 text-white rounded-lg hover:from-blue-600 hover:to-purple-700 transition-all shadow-md hover:shadow-lg"
                >
                  Start Now
                </button>
              </div>
            ) : (
              <button
                onClick={handleStartSession}
                className="px-8 py-3 text-base font-medium bg-gradient-to-r from-blue-500 to-purple-600 text-white rounded-lg hover:from-blue-600 hover:to-purple-700 transition-all shadow-md hover:shadow-lg"
                style={{
                  animation: 'breathe 2s ease-in-out infinite'
                }}
              >
                Start Session
              </button>
            )}
          </div>
        )}

        {/* Error Display */}
        {error && (
          <div className="bg-red-50 dark:bg-red-950/20 border border-red-200 dark:border-red-900 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <XCircle className="h-5 w-5 text-red-600 flex-shrink-0 mt-0.5" />
              <div>
                <p className="font-medium text-red-900 dark:text-red-200">
                  Creation Failed
                </p>
                <p className="text-sm text-red-700 dark:text-red-300 mt-1">
                  {error}
                </p>
                <button
                  onClick={() => navigate({ to: "/" })}
                  className="mt-3 text-sm font-medium text-red-700 dark:text-red-300 hover:text-red-900 dark:hover:text-red-100 underline"
                >
                  Return to Dashboard
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
    </>
  )
}
