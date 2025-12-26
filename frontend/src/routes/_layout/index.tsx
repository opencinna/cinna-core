import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useState, KeyboardEvent } from "react"
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

export const Route = createFileRoute("/_layout/")({
  component: Dashboard,
  head: () => ({
    meta: [
      {
        title: "Dashboard - FastAPI Cloud",
      },
    ],
  }),
})

function Dashboard() {
  const { setHeaderContent } = usePageHeader()
  const [selectedAgentId, setSelectedAgentId] = useState<string>("")
  const [mode, setMode] = useState<"conversation" | "building">("conversation")
  const [message, setMessage] = useState("")

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

  const createMutation = useMutation({
    mutationFn: (data: { sessionData: SessionCreate; initialMessage: string }) =>
      SessionsService.createSession({ requestBody: data.sessionData }),
    onSuccess: (session, variables) => {
      showSuccessToast("Your conversation session has been created.")
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

  const agents = agentsData?.data || []
  const agentsWithActiveEnv = agents.filter((a) => a.active_environment_id)

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

  const handleSend = () => {
    const trimmedMessage = message.trim()
    if (!trimmedMessage || createMutation.isPending) {
      return
    }

    if (!selectedAgentId) {
      showErrorToast("Please select an agent")
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
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex-1 flex items-center justify-center p-6">
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
                    onClick={() => setSelectedAgentId(agent.id)}
                  >
                    {agent.name}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Message Input */}
          <div className="space-y-4">
            <Textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                mode === "building"
                  ? "Type your message to start building..."
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
                        onChange={() =>
                          setMode(mode === "conversation" ? "building" : "conversation")
                        }
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
    </div>
  )
}
