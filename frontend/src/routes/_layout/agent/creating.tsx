import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useRef } from "react"
import { CheckCircle2, Circle, Loader2, XCircle, AlertTriangle } from "lucide-react"
import { OpenAPI } from "@/client"

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
  const [steps, setSteps] = useState<Step[]>([
    { id: "create_agent", label: "Creating agent", status: "pending" },
    { id: "start_environment", label: "Starting default environment", status: "pending" },
    { id: "create_session", label: "Creating conversation session", status: "pending" },
    { id: "redirect", label: "Redirecting to session", status: "pending" },
  ])
  const [error, setError] = useState<string | null>(null)
  const hasStartedRef = useRef(false)

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
          ? await OpenAPI.TOKEN()
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
                  updateStepStatus("start_environment", "in_progress")
                  break
                case "environment_starting":
                  updateStepStatus("start_environment", "in_progress", data.message)
                  break
                case "environment_ready":
                  updateStepStatus("start_environment", "completed", data.message)
                  updateStepStatus("create_session", "in_progress")
                  break
                case "session_creating":
                  updateStepStatus("create_session", "in_progress", data.message)
                  break
                case "session_created":
                  updateStepStatus("create_session", "completed", data.message)
                  updateStepStatus("redirect", "in_progress")
                  break
                case "completed":
                  updateStepStatus("redirect", "completed")
                  // Redirect to session with initial message
                  setTimeout(() => {
                    navigate({
                      to: "/session/$sessionId",
                      params: { sessionId: data.session_id },
                      search: { initialMessage: description },
                    })
                  }, 500)
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

  return (
    <div className="flex items-center justify-center min-h-[calc(100vh-4rem)] p-6">
      <div className="w-full max-w-2xl space-y-6">
        {/* Header */}
        <div className="text-center space-y-2">
          <h1 className="text-2xl font-semibold">Creating Your Agent</h1>
          <p className="text-muted-foreground">
            Please wait while we set up your new agent...
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

        {/* Progress Steps */}
        <div className="bg-card border rounded-lg p-6 space-y-4">
          {steps.map((step, index) => (
            <div key={step.id} className="flex items-start gap-3">
              <div className="flex-shrink-0 mt-1">{getStepIcon(step.status)}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{step.label}</span>
                  {step.status === "in_progress" && (
                    <span className="text-xs text-muted-foreground animate-pulse">
                      Processing...
                    </span>
                  )}
                </div>
                {step.message && (
                  <p className="text-sm text-muted-foreground mt-1">
                    {step.message}
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>

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
  )
}
