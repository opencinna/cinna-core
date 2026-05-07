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
import { AgentsService, AiCredentialsService } from "@/client"
import type { AgentEnvironmentCreate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Plus } from "lucide-react"
import {
  EnvironmentConfigForm,
  EnvConfigValue,
  INITIAL_ENV_CONFIG,
  USE_DEFAULT_SENTINEL,
  composeSDKId,
} from "./EnvironmentConfigForm"

interface AddEnvironmentProps {
  agentId: string
}

export function AddEnvironment({ agentId }: AddEnvironmentProps) {
  const [open, setOpen] = useState(false)
  const [envConfig, setEnvConfig] = useState<EnvConfigValue>(INITIAL_ENV_CONFIG)

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Used by handleSubmit to resolve the selected credential objects for composeSDKId.
  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  const createMutation = useMutation({
    mutationFn: (data: AgentEnvironmentCreate) =>
      AgentsService.createAgentEnvironment({ id: agentId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("The new environment has been created successfully.")
      setOpen(false)
      setEnvConfig(INITIAL_ENV_CONFIG)
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

    const allCredentials = aiCredentials?.data ?? []
    const convIsDefault = envConfig.conversationCredentialId === USE_DEFAULT_SENTINEL
    const buildIsDefault = envConfig.buildingCredentialId === USE_DEFAULT_SENTINEL

    const selectedConversationCredential =
      allCredentials.find((c) => c.id === envConfig.conversationCredentialId) ?? null
    const selectedBuildingCredential =
      allCredentials.find((c) => c.id === envConfig.buildingCredentialId) ?? null

    const sdkConversation = composeSDKId(
      envConfig.sdkEngineConversation,
      convIsDefault ? null : selectedConversationCredential,
    )
    const sdkBuilding = composeSDKId(
      envConfig.sdkEngineBuilding,
      buildIsDefault ? null : selectedBuildingCredential,
    )

    const useDefaultForAll = convIsDefault && buildIsDefault

    createMutation.mutate({
      env_name: envConfig.envName,
      agent_sdk_conversation: sdkConversation,
      agent_sdk_building: sdkBuilding,
      model_override_conversation: envConfig.modelOverrideConversation.trim() || undefined,
      model_override_building: envConfig.modelOverrideBuilding.trim() || undefined,
      use_default_ai_credentials: useDefaultForAll,
      conversation_ai_credential_id: useDefaultForAll
        ? undefined
        : (convIsDefault ? undefined : (envConfig.conversationCredentialId || undefined)),
      building_ai_credential_id: useDefaultForAll
        ? undefined
        : (buildIsDefault ? undefined : (envConfig.buildingCredentialId || undefined)),
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="gap-2">
          <Plus className="h-4 w-4" />
          Add Environment
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[540px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create New Environment</DialogTitle>
            <DialogDescription>
              Create a new Docker container environment for your agent.
            </DialogDescription>
          </DialogHeader>
          <EnvironmentConfigForm
            value={envConfig}
            onChange={setEnvConfig}
            open={open}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? "Creating..." : "Create Environment"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
