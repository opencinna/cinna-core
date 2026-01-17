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
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { AgentsService, UsersService, AiCredentialsService } from "@/client"
import type { AgentEnvironmentCreate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Plus, AlertCircle, Key } from "lucide-react"

// SDK options
const SDK_OPTIONS = [
  { value: "claude-code/anthropic", label: "Anthropic Claude", requiredKey: "anthropic", credType: "anthropic" },
  { value: "claude-code/minimax", label: "MiniMax M2", requiredKey: "minimax", credType: "minimax" },
  { value: "google-adk-wr/openai-compatible", label: "OpenAI Compatible", requiredKey: "openai_compatible", credType: "openai_compatible" },
]

// Map SDK to credential type for filtering
const getCredentialTypeForSdk = (sdk: string): string | null => {
  const option = SDK_OPTIONS.find(o => o.value === sdk)
  return option?.credType || null
}

interface AddEnvironmentProps {
  agentId: string
}

export function AddEnvironment({ agentId }: AddEnvironmentProps) {
  const [open, setOpen] = useState(false)
  const [envName] = useState("python-env-advanced")
  const [sdkConversation, setSdkConversation] = useState("claude-code/anthropic")
  const [sdkBuilding, setSdkBuilding] = useState("claude-code/anthropic")
  const [useDefaultCredentials, setUseDefaultCredentials] = useState(true)
  const [conversationCredentialId, setConversationCredentialId] = useState<string | null>(null)
  const [buildingCredentialId, setBuildingCredentialId] = useState<string | null>(null)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Get user's AI credentials status to check available API keys
  const { data: credentialsStatus } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  // Get user's AI credentials list when not using defaults
  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: !useDefaultCredentials,
  })

  // Filter credentials by SDK type
  const getCredentialsForSdk = (sdk: string) => {
    const credType = getCredentialTypeForSdk(sdk)
    if (!credType || !aiCredentials?.data) return []
    return aiCredentials.data.filter(c => c.type === credType)
  }

  const conversationCredentials = getCredentialsForSdk(sdkConversation)
  const buildingCredentials = getCredentialsForSdk(sdkBuilding)

  // Check if user has required API keys for selected SDKs
  const hasAnthropicKey = credentialsStatus?.has_anthropic_api_key ?? false
  const hasMinimaxKey = credentialsStatus?.has_minimax_api_key ?? false
  const hasOpenaiCompatibleKey = credentialsStatus?.has_openai_compatible_api_key ?? false

  const getKeyStatus = (sdk: string) => {
    if (sdk === "claude-code/anthropic") return hasAnthropicKey
    if (sdk === "claude-code/minimax") return hasMinimaxKey
    if (sdk === "google-adk-wr/openai-compatible") return hasOpenaiCompatibleKey
    return false
  }

  // Validate based on mode
  const canCreateDefault = getKeyStatus(sdkConversation) && getKeyStatus(sdkBuilding)
  const canCreateCustom = () => {
    // Check if we have credentials selected or can fall back to defaults
    const hasConvCred = conversationCredentialId || conversationCredentials.length > 0
    const hasBuildCred = buildingCredentialId || buildingCredentials.length > 0
    return hasConvCred && hasBuildCred
  }
  const canCreate = useDefaultCredentials ? canCreateDefault : canCreateCustom()

  const createMutation = useMutation({
    mutationFn: (data: AgentEnvironmentCreate) =>
      AgentsService.createAgentEnvironment({ id: agentId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("The new environment has been created successfully.")
      setOpen(false)
      // Reset to defaults
      setSdkConversation("claude-code/anthropic")
      setSdkBuilding("claude-code/anthropic")
      setUseDefaultCredentials(true)
      setConversationCredentialId(null)
      setBuildingCredentialId(null)
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
      use_default_ai_credentials: useDefaultCredentials,
      conversation_ai_credential_id: useDefaultCredentials ? undefined : (conversationCredentialId || undefined),
      building_ai_credential_id: useDefaultCredentials ? undefined : (buildingCredentialId || undefined),
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
              {useDefaultCredentials && !getKeyStatus(sdkConversation) && (
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
              {useDefaultCredentials && !getKeyStatus(sdkBuilding) && (
                <p className="text-sm text-destructive flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  API key not configured
                </p>
              )}
            </div>

            {/* AI Credentials Selection */}
            <div className="border-t pt-4 space-y-4">
              <div className="flex items-center justify-between">
                <div className="space-y-0.5">
                  <Label htmlFor="use-default-credentials" className="flex items-center gap-2">
                    <Key className="h-4 w-4" />
                    Use Default AI Credentials
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    {useDefaultCredentials
                      ? "Using your default AI credentials from settings"
                      : "Select specific AI credentials for this environment"}
                  </p>
                </div>
                <Switch
                  id="use-default-credentials"
                  checked={useDefaultCredentials}
                  onCheckedChange={setUseDefaultCredentials}
                />
              </div>

              {!useDefaultCredentials && (
                <div className="space-y-4 pl-4 border-l-2 border-muted">
                  <div className="space-y-2">
                    <Label htmlFor="conversation-credential">Conversation AI Credential</Label>
                    <Select
                      value={conversationCredentialId || ""}
                      onValueChange={(v) => setConversationCredentialId(v || null)}
                    >
                      <SelectTrigger id="conversation-credential">
                        <SelectValue placeholder="Select credential..." />
                      </SelectTrigger>
                      <SelectContent>
                        {conversationCredentials.length === 0 ? (
                          <div className="py-2 px-2 text-sm text-muted-foreground">
                            No {getCredentialTypeForSdk(sdkConversation)} credentials found
                          </div>
                        ) : (
                          conversationCredentials.map((cred) => (
                            <SelectItem key={cred.id} value={cred.id}>
                              {cred.name} {cred.is_default && "(default)"}
                            </SelectItem>
                          ))
                        )}
                      </SelectContent>
                    </Select>
                    {conversationCredentials.length === 0 && (
                      <p className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="h-3 w-3" />
                        No credentials available for selected SDK
                      </p>
                    )}
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="building-credential">Building AI Credential</Label>
                    <Select
                      value={buildingCredentialId || ""}
                      onValueChange={(v) => setBuildingCredentialId(v || null)}
                    >
                      <SelectTrigger id="building-credential">
                        <SelectValue placeholder="Select credential..." />
                      </SelectTrigger>
                      <SelectContent>
                        {buildingCredentials.length === 0 ? (
                          <div className="py-2 px-2 text-sm text-muted-foreground">
                            No {getCredentialTypeForSdk(sdkBuilding)} credentials found
                          </div>
                        ) : (
                          buildingCredentials.map((cred) => (
                            <SelectItem key={cred.id} value={cred.id}>
                              {cred.name} {cred.is_default && "(default)"}
                            </SelectItem>
                          ))
                        )}
                      </SelectContent>
                    </Select>
                    {buildingCredentials.length === 0 && (
                      <p className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="h-3 w-3" />
                        No credentials available for selected SDK
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>

            {useDefaultCredentials && !canCreate && (
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
