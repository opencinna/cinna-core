import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Label } from "@/components/ui/label"
import { AgentsService, UsersService } from "@/client"
import type { AgentEnvironmentCreate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Plus, AlertCircle } from "lucide-react"

// SDK options
const SDK_OPTIONS = [
  { value: "claude-code/anthropic", label: "Anthropic Claude", requiredKey: "anthropic" },
  { value: "claude-code/minimax", label: "MiniMax M2", requiredKey: "minimax" },
]

interface AddEnvironmentProps {
  agentId: string
}

export function AddEnvironment({ agentId }: AddEnvironmentProps) {
  const [open, setOpen] = useState(false)
  const [envName] = useState("python-env-advanced")
  const [sdkConversation, setSdkConversation] = useState("claude-code/anthropic")
  const [sdkBuilding, setSdkBuilding] = useState("claude-code/anthropic")

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Get user's AI credentials status to check available API keys
  const { data: credentialsStatus } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  // Check if user has required API keys for selected SDKs
  const hasAnthropicKey = credentialsStatus?.has_anthropic_api_key ?? false
  const hasMinimaxKey = credentialsStatus?.has_minimax_api_key ?? false

  const getKeyStatus = (sdk: string) => {
    if (sdk === "claude-code/anthropic") return hasAnthropicKey
    if (sdk === "claude-code/minimax") return hasMinimaxKey
    return false
  }

  const canCreate = getKeyStatus(sdkConversation) && getKeyStatus(sdkBuilding)

  const createMutation = useMutation({
    mutationFn: (data: AgentEnvironmentCreate) =>
      AgentsService.createAgentEnvironment({ id: agentId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("The new environment has been created successfully.")
      setOpen(false)
      // Reset to defaults
      setSdkConversation("claude-code/anthropic")
      setSdkBuilding("claude-code/anthropic")
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || error.message || "Failed to create environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()

    createMutation.mutate({
      env_name: envName,
      agent_sdk_conversation: sdkConversation,
      agent_sdk_building: sdkBuilding,
    })
  }

  const getMissingKeyMessage = () => {
    const missing: string[] = []
    if (!getKeyStatus(sdkConversation)) {
      const sdk = SDK_OPTIONS.find(o => o.value === sdkConversation)
      missing.push(`${sdk?.label} API key for conversation mode`)
    }
    if (!getKeyStatus(sdkBuilding) && sdkBuilding !== sdkConversation) {
      const sdk = SDK_OPTIONS.find(o => o.value === sdkBuilding)
      missing.push(`${sdk?.label} API key for building mode`)
    }
    return missing.join(" and ")
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="gap-2">
          <Plus className="h-4 w-4" />
          Add Environment
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create New Environment</DialogTitle>
            <DialogDescription>
              Create a new Python Advanced environment for your agent. This will be a Docker
              container with advanced Python capabilities.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="text-sm text-muted-foreground">
              <p>
                <span className="font-medium">Template:</span> Python Advanced
              </p>
              <p>
                <span className="font-medium">Version:</span> 1.0.0
              </p>
              <p>
                <span className="font-medium">Type:</span> Docker Container
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="sdk-conversation">Conversation Mode SDK</Label>
              <Select value={sdkConversation} onValueChange={setSdkConversation}>
                <SelectTrigger id="sdk-conversation">
                  <SelectValue placeholder="Select SDK" />
                </SelectTrigger>
                <SelectContent>
                  {SDK_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!getKeyStatus(sdkConversation) && (
                <p className="text-sm text-destructive flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  API key not configured
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="sdk-building">Building Mode SDK</Label>
              <Select value={sdkBuilding} onValueChange={setSdkBuilding}>
                <SelectTrigger id="sdk-building">
                  <SelectValue placeholder="Select SDK" />
                </SelectTrigger>
                <SelectContent>
                  {SDK_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!getKeyStatus(sdkBuilding) && (
                <p className="text-sm text-destructive flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  API key not configured
                </p>
              )}
            </div>

            {!canCreate && (
              <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                <p>Missing {getMissingKeyMessage()}. Add it in User Settings &gt; AI Credentials.</p>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending || !canCreate}>
              {createMutation.isPending ? "Creating..." : "Create Environment"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
