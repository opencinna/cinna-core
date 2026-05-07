import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { AgentsService, SessionsService } from "@/client"
import type { SessionCreate } from "@/client"
import { AgentSelectorDialog } from "@/components/Common/AgentSelectorDialog"
import type { AgentOption } from "@/components/Common/AgentSelectorDialog"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { getColorPreset } from "@/utils/colorPresets"
import { cn } from "@/lib/utils"
import { MessageSquarePlus, Bot } from "lucide-react"

interface CreateSessionProps {
  variant?: "default" | "outline" | "secondary"
  size?: "default" | "sm" | "lg"
  className?: string
}

export function CreateSession({ variant = "default", size = "default", className }: CreateSessionProps) {
  const [open, setOpen] = useState(false)
  const [agentId, setAgentId] = useState("")
  const [mode, setMode] = useState<"conversation" | "building">("conversation")
  const [title, setTitle] = useState("")
  const [agentSelectorOpen, setAgentSelectorOpen] = useState(false)

  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()
  const { workspaceFilter } = useWorkspace()

  const { data: agentsData } = useQuery({
    queryKey: ["agents", workspaceFilter],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return AgentsService.readAgents({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
    },
    enabled: open,
  })

  const createMutation = useMutation({
    mutationFn: (data: SessionCreate) =>
      SessionsService.createSession({ requestBody: data }),
    onSuccess: (session) => {
      setOpen(false)
      resetForm()
      // Navigate to the chat interface
      navigate({ to: "/session/$sessionId", params: { sessionId: session.id }, search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined, pageContext: undefined } })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create session")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["sessions"] })
    },
  })

  const resetForm = () => {
    setAgentId("")
    setMode("conversation")
    setTitle("")
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()

    if (!agentId) {
      showErrorToast("Please select an agent")
      return
    }

    const selectedAgent = agentsData?.data.find((a) => a.id === agentId)
    if (!selectedAgent?.active_environment_id) {
      showErrorToast("Please start an environment for this agent first")
      return
    }

    createMutation.mutate({
      agent_id: agentId,
      mode,
      title: title.trim() || null,
    })
  }

  const agents = agentsData?.data || []
  const agentsWithActiveEnv = agents.filter((a) => a.active_environment_id)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={variant} size={size} className={className}>
          <MessageSquarePlus className="h-4 w-4 mr-2" />
          New Session
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create New Session</DialogTitle>
            <DialogDescription>
              Start a new conversation with your agent. Choose between building mode for setup or
              conversation mode for task execution.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label>Agent*</Label>
              {(() => {
                const selectedAgent = agentsWithActiveEnv.find((a) => a.id === agentId)
                const preset = selectedAgent ? getColorPreset(selectedAgent.ui_color_preset) : null
                const agentOptions: AgentOption[] = agentsWithActiveEnv.map((a) => ({
                  id: a.id,
                  name: a.name,
                  colorPreset: a.ui_color_preset,
                }))
                return (
                  <>
                    <button
                      type="button"
                      onClick={() => setAgentSelectorOpen(true)}
                      className={cn(
                        "flex items-center gap-1.5 px-3 py-2 rounded-md text-sm transition-all w-full text-left",
                        preset
                          ? `${preset.badgeBg} ${preset.badgeText} ${preset.badgeHover}`
                          : "bg-muted text-muted-foreground hover:bg-muted/80"
                      )}
                    >
                      <Bot className="h-4 w-4 shrink-0" />
                      <span className="truncate">{selectedAgent?.name || "Select an agent"}</span>
                    </button>
                    <AgentSelectorDialog
                      open={agentSelectorOpen}
                      onOpenChange={setAgentSelectorOpen}
                      onSelect={setAgentId}
                      selectedAgentId={agentId}
                      agents={agentOptions}
                      title="Select Agent"
                    />
                  </>
                )
              })()}
              {agents.length > 0 && agentsWithActiveEnv.length === 0 && (
                <p className="text-sm text-muted-foreground text-amber-600">
                  Please start an environment for your agent first
                </p>
              )}
            </div>

            <div className="grid gap-2">
              <Label>Session Mode*</Label>
              <div className="grid gap-2">
                <div
                  className={`border rounded-lg p-3 cursor-pointer transition-colors ${
                    mode === "conversation"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/50"
                  }`}
                  onClick={() => setMode("conversation")}
                >
                  <div className="font-medium">Conversation Mode</div>
                  <div className="text-sm text-muted-foreground">
                    Execute tasks using pre-built tools and workflows. Faster responses, lower
                    cost.
                  </div>
                </div>
                <div
                  className={`border rounded-lg p-3 cursor-pointer transition-colors ${
                    mode === "building"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/50"
                  }`}
                  onClick={() => setMode("building")}
                >
                  <div className="font-medium">Building Mode</div>
                  <div className="text-sm text-muted-foreground">
                    Set up capabilities, write scripts, configure integrations. Comprehensive
                    context.
                  </div>
                </div>
              </div>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="title">Session Title (Optional)</Label>
              <Input
                id="title"
                placeholder="e.g., Email automation setup"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
              <p className="text-sm text-muted-foreground">
                Leave empty to auto-generate from first message
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? "Creating..." : "Create Session"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
