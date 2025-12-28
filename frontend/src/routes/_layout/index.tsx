import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useState, useMemo, KeyboardEvent } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import { AgentsService, SessionsService } from "@/client"
import type { SessionCreate } from "@/client"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Send, Bot } from "lucide-react"
import { usePageHeader } from "@/routes/_layout"
import AddAgent from "@/components/Agents/AddAgent"
import useCustomToast from "@/hooks/useCustomToast"
import PendingItems from "@/components/Pending/PendingItems"
import { getColorPreset } from "@/utils/colorPresets"
import { RotatingHints } from "@/components/Common/RotatingHints"
import { LatestSessions } from "@/components/Sessions/LatestSessions"

export const Route = createFileRoute("/_layout/")({
  component: Dashboard,
  head: () => ({
    meta: [
      {
        title: "Dashboard - Workflow Runner",
      },
    ],
  }),
})

const NEW_AGENT_ID = "__new_agent__"

function Dashboard() {
  const { setHeaderContent } = usePageHeader()
  const [selectedAgentId, setSelectedAgentId] = useState<string>("")
  const [mode, setMode] = useState<"conversation" | "building">("conversation")
  const [message, setMessage] = useState("")
  const [inputMode, setInputMode] = useState<"automatic" | "manual">("automatic")
  const [previousMode, setPreviousMode] = useState<"conversation" | "building">("conversation")

  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const {
    data: agentsData,
    isLoading: agentsLoading,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 100 }),
  })

  const {
    data: sessionsData,
  } = useQuery({
    queryKey: ["sessions", "latest", 8],
    queryFn: () =>
      SessionsService.listSessions({
        skip: 0,
        limit: 8,
        orderBy: "last_message_at",
        orderDesc: true,
      }),
  })

  const createMutation = useMutation({
    mutationFn: (data: { sessionData: SessionCreate; initialMessage: string }) =>
      SessionsService.createSession({ requestBody: data.sessionData }),
    onSuccess: (session, variables) => {
      const initialMessage = variables.initialMessage
      setMessage("")
      navigate({
        to: "/session/$sessionId",
        params: { sessionId: session.id },
        search: { initialMessage },
      })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create session")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] })
    },
  })

  const agents = useMemo(() => agentsData?.data || [], [agentsData?.data])
  const agentsWithActiveEnv = useMemo(
    () => agents.filter((a) => a.active_environment_id),
    [agents]
  )

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Dashboard</h1>
        <p className="text-xs text-muted-foreground">Start a new conversation with your agent</p>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  useEffect(() => {
    if (agentsWithActiveEnv.length > 0 && !selectedAgentId) {
      setSelectedAgentId(agentsWithActiveEnv[0].id)
    }
  }, [agentsWithActiveEnv, selectedAgentId])

  // Auto-insert entrypoint prompt when agent changes (only in automatic mode)
  useEffect(() => {
    if (!selectedAgentId || inputMode !== "automatic") return

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (selectedAgent?.entrypoint_prompt) {
      setMessage(selectedAgent.entrypoint_prompt)
    } else {
      setMessage("")
    }
  }, [selectedAgentId, agentsWithActiveEnv, inputMode])

  // Handle input text when switching between conversation and building modes
  useEffect(() => {
    // Only apply this logic in automatic mode with a regular agent selected
    if (inputMode !== "automatic" || !selectedAgentId || selectedAgentId === NEW_AGENT_ID) {
      return
    }

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)

    if (mode === "building") {
      // Clear input when switching to building mode (entrypoint prompt doesn't make sense)
      setMessage("")
    } else if (mode === "conversation") {
      // Restore entrypoint prompt when switching back to conversation mode
      if (selectedAgent?.entrypoint_prompt) {
        setMessage(selectedAgent.entrypoint_prompt)
      } else {
        setMessage("")
      }
    }
  }, [mode, inputMode, selectedAgentId, agentsWithActiveEnv])

  const handleSend = () => {
    const trimmedMessage = message.trim()
    if (!trimmedMessage || createMutation.isPending) {
      return
    }

    if (!selectedAgentId) {
      showErrorToast("Please select an agent")
      return
    }

    // Handle "New Agent" flow
    if (selectedAgentId === NEW_AGENT_ID) {
      navigate({
        to: "/agent/creating",
        search: { description: trimmedMessage, mode },
      })
      return
    }

    const selectedAgent = agentsWithActiveEnv.find((a) => a.id === selectedAgentId)
    if (!selectedAgent?.active_environment_id) {
      showErrorToast("Please start an environment for this agent first")
      return
    }

    createMutation.mutate({
      sessionData: {
        agent_id: selectedAgentId,
        mode,
        title: null,
      },
      initialMessage: trimmedMessage,
    })

    // Reset to automatic mode after sending
    setInputMode("automatic")
  }

  const handleMessageChange = (value: string) => {
    setMessage(value)
    // Switch to manual mode when user edits the input
    if (inputMode === "automatic") {
      setInputMode("manual")
    }
  }

  const handleAgentClick = (agentId: string) => {
    if (agentId === NEW_AGENT_ID) {
      // New Agent selected - save current mode and switch to building
      if (selectedAgentId !== NEW_AGENT_ID) {
        setPreviousMode(mode)
      }
      setSelectedAgentId(NEW_AGENT_ID)
      setInputMode("automatic")
      setMessage("")
      setMode("building")
      return
    }

    // Switching from "New Agent" to regular agent - restore previous mode
    if (selectedAgentId === NEW_AGENT_ID) {
      setMode(previousMode)
    }

    if (selectedAgentId === agentId && inputMode === "automatic") {
      // Agent already selected, toggle between empty and entrypoint prompt
      const agent = agentsWithActiveEnv.find((a) => a.id === agentId)
      if (message.trim()) {
        // Clear the input
        setMessage("")
      } else if (agent?.entrypoint_prompt) {
        // Insert entrypoint prompt
        setMessage(agent.entrypoint_prompt)
      }
    } else {
      // Select the agent (which will trigger entrypoint insertion via useEffect)
      setSelectedAgentId(agentId)
      setInputMode("automatic")
    }
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (agentsLoading) {
    return <PendingItems />
  }

  if (agents.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[calc(100vh-4rem)]">
        <div className="flex flex-col items-center justify-center text-center max-w-md">
          <div className="rounded-full bg-muted p-6 mb-6">
            <Bot className="h-12 w-12 text-muted-foreground" />
          </div>
          <h2 className="text-2xl font-semibold mb-2">No Agents Available</h2>
          <p className="text-muted-foreground mb-6">
            You need to create an agent before you can start a conversation.
          </p>
          <AddAgent />
        </div>
      </div>
    )
  }

  if (agentsWithActiveEnv.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[calc(100vh-4rem)]">
        <div className="flex flex-col items-center justify-center text-center max-w-md">
          <div className="rounded-full bg-muted p-6 mb-6">
            <Bot className="h-12 w-12 text-muted-foreground" />
          </div>
          <h2 className="text-2xl font-semibold mb-2">No Active Environments</h2>
          <p className="text-muted-foreground mb-6">
            Please start an environment for your agent before you can start a conversation.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Main centered content area */}
      <div className="flex-1 flex items-center justify-center p-6 overflow-auto">
        <div className="w-full max-w-3xl space-y-6">
          {/* Agent Selector Pills */}
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2">
              {agentsWithActiveEnv.map((agent) => {
                const colorPreset = getColorPreset(agent.ui_color_preset)
                const isSelected = selectedAgentId === agent.id
                return (
                  <button
                    key={agent.id}
                    className={`
                      cursor-pointer px-4 py-2 text-sm rounded-md transition-all
                      ${colorPreset.badgeBg}
                      ${colorPreset.badgeText}
                      ${colorPreset.badgeHover}
                      ${isSelected ? colorPreset.badgeOutline : ""}
                    `}
                    onClick={() => handleAgentClick(agent.id)}
                  >
                    {agent.name}
                  </button>
                )
              })}
              {/* New Agent Badge */}
              <button
                className={`
                  cursor-pointer px-4 py-2 text-sm rounded-md transition-all
                  bg-gradient-to-r from-blue-500 to-purple-600
                  text-white
                  hover:from-blue-600 hover:to-purple-700
                  ${selectedAgentId === NEW_AGENT_ID ? "ring-2 ring-blue-400 ring-offset-2" : ""}
                `}
                onClick={() => handleAgentClick(NEW_AGENT_ID)}
              >
                + New Agent
              </button>
            </div>
          </div>

          {/* Message Input */}
          <div className="space-y-4">
            <Textarea
              value={message}
              onChange={(e) => handleMessageChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                selectedAgentId === NEW_AGENT_ID
                  ? "Describe what you want the agent to do and what result you expect..."
                  : mode === "building"
                    ? "Describe what you want the agent to do and what result you expect..."
                    : "Type your message to start a conversation..."
              }
              className={`min-h-[120px] max-h-[300px] resize-none text-base transition-colors ${
                mode === "building"
                  ? "border-orange-400 bg-orange-50 dark:bg-orange-950/20 focus-visible:ring-orange-400"
                  : ""
              }`}
              rows={4}
            />

            <div className="flex items-center justify-between">
              <RotatingHints />

              {/* Mode Switch and Send Button */}
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {mode === "conversation" ? "Conversation" : "Building"}
                  </span>
                  <label className="flex cursor-pointer select-none items-center">
                    <div className="relative">
                      <input
                        type="checkbox"
                        checked={mode === "building"}
                        onChange={() => {
                          const newMode = mode === "conversation" ? "building" : "conversation"
                          setMode(newMode)
                          // Update previousMode if not on "New Agent" so it's saved for later
                          if (selectedAgentId !== NEW_AGENT_ID) {
                            setPreviousMode(newMode)
                          }
                        }}
                        className="sr-only"
                      />
                      <div
                        className={`block h-6 w-11 rounded-full transition-colors ${
                          mode === "building" ? "bg-orange-400" : "bg-gray-300 dark:bg-gray-600"
                        }`}
                      ></div>
                      <div
                        className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                          mode === "building" ? "translate-x-5" : ""
                        }`}
                      ></div>
                    </div>
                  </label>
                </div>

                <Button
                  onClick={handleSend}
                  disabled={createMutation.isPending || !message.trim()}
                  size="icon"
                  className="h-9 w-9"
                >
                  <Send className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Latest Sessions - Sticky at bottom, growing upward */}
      {sessionsData && sessionsData.data.length > 0 && (
        <div className="bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 px-6 py-4">
          <div className="max-h-[45vh] overflow-y-auto">
            <div className="w-full max-w-3xl mx-auto">
              <LatestSessions limit={5} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
