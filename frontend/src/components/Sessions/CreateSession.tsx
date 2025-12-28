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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { AgentsService, SessionsService } from "@/client"
import type { SessionCreate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { MessageSquarePlus } from "lucide-react"

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

  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: agentsData } = useQuery({
    queryKey: ["agents"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 100 }),
    enabled: open,
  })

  const createMutation = useMutation({
    mutationFn: (data: SessionCreate) =>
      SessionsService.createSession({ requestBody: data }),
    onSuccess: (session) => {
      setOpen(false)
      resetForm()
      // Navigate to the chat interface
      navigate({ to: "/session/$sessionId", params: { sessionId: session.id } })
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
              <Label htmlFor="agent">Agent*</Label>
              <Select value={agentId} onValueChange={setAgentId}>
                <SelectTrigger>
                  <SelectValue placeholder="Select an agent" />
                </SelectTrigger>
                <SelectContent>
                  {agentsWithActiveEnv.length === 0 && (
                    <div className="px-2 py-1.5 text-sm text-muted-foreground">
                      No agents with active environments
                    </div>
                  )}
                  {agentsWithActiveEnv.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
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
