import { useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Sparkles, Trash2, Plus, Bot } from "lucide-react"
import type { AgentPublic, HandoverConfigPublic } from "@/client"
import { AgentsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { handleError } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"

interface AgentHandoversProps {
  agent: AgentPublic
}

export function AgentHandovers({ agent }: AgentHandoversProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { activeWorkspaceId } = useWorkspace()

  // State for dialog
  const [isDialogOpen, setIsDialogOpen] = useState(false)

  // State for editing handover prompts
  const [editingPrompts, setEditingPrompts] = useState<Record<string, string>>({})
  const [dirtyPrompts, setDirtyPrompts] = useState<Set<string>>(new Set())

  // Fetch all agents for selection
  const { data: agentsData } = useQuery({
    queryKey: ["agents", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return AgentsService.readAgents({
        userWorkspaceId: workspaceId ?? "",
      })
    },
  })

  // Fetch handover configs for this agent
  const { data: handoversData, refetch: refetchHandovers } = useQuery({
    queryKey: ["agentHandovers", agent.id],
    queryFn: () => AgentsService.listHandoverConfigs({ id: agent.id }),
    enabled: !!agent.id,
  })

  // Create handover mutation
  const createHandoverMutation = useMutation({
    mutationFn: (targetAgentId: string) =>
      AgentsService.createHandoverConfig({
        id: agent.id,
        requestBody: {
          target_agent_id: targetAgentId,
          handover_prompt: "",
        },
      }),
    onSuccess: () => {
      showSuccessToast("Handover configuration created")
      setIsDialogOpen(false)
      refetchHandovers()
    },
    onError: handleError.bind(showErrorToast),
  })

  // Update handover mutation
  const updateHandoverMutation = useMutation({
    mutationFn: ({
      handoverId,
      prompt,
      enabled,
    }: {
      handoverId: string
      prompt?: string
      enabled?: boolean
    }) =>
      AgentsService.updateHandoverConfig({
        id: agent.id,
        handoverId,
        requestBody: {
          handover_prompt: prompt,
          enabled,
        },
      }),
    onSuccess: (_, variables) => {
      showSuccessToast("Handover configuration updated")
      // Remove from dirty set
      const newDirty = new Set(dirtyPrompts)
      newDirty.delete(variables.handoverId)
      setDirtyPrompts(newDirty)
      refetchHandovers()
    },
    onError: handleError.bind(showErrorToast),
  })

  // Delete handover mutation
  const deleteHandoverMutation = useMutation({
    mutationFn: (handoverId: string) =>
      AgentsService.deleteHandoverConfig({
        id: agent.id,
        handoverId,
      }),
    onSuccess: () => {
      showSuccessToast("Handover configuration deleted")
      refetchHandovers()
    },
    onError: handleError.bind(showErrorToast),
  })

  // Generate handover prompt mutation
  const generatePromptMutation = useMutation({
    mutationFn: ({
      targetAgentId,
      handoverId,
    }: {
      targetAgentId: string
      handoverId: string
    }) =>
      AgentsService.generateHandoverPromptEndpoint({
        id: agent.id,
        requestBody: { target_agent_id: targetAgentId },
      }).then((data) => ({ data, handoverId })),
    onSuccess: ({ data, handoverId }) => {
      if (data.success && data.handover_prompt) {
        // Update editing state
        setEditingPrompts((prev) => ({
          ...prev,
          [handoverId]: data.handover_prompt!,
        }))
        // Mark as dirty
        setDirtyPrompts((prev) => new Set(prev).add(handoverId))
        showSuccessToast("Handover prompt generated")
      } else {
        showErrorToast(data.error || "Failed to generate prompt")
      }
    },
    onError: handleError.bind(showErrorToast),
  })

  const handlePromptChange = (handoverId: string, value: string) => {
    setEditingPrompts((prev) => ({
      ...prev,
      [handoverId]: value,
    }))
    setDirtyPrompts((prev) => new Set(prev).add(handoverId))
  }

  const handleSavePrompt = (handoverId: string) => {
    const prompt = editingPrompts[handoverId]
    if (prompt !== undefined) {
      updateHandoverMutation.mutate({ handoverId, prompt })
    }
  }

  const handleToggleEnabled = (handoverId: string, enabled: boolean) => {
    updateHandoverMutation.mutate({ handoverId, enabled })
  }

  const handleDelete = (handoverId: string) => {
    if (confirm("Are you sure you want to delete this handover configuration?")) {
      deleteHandoverMutation.mutate(handoverId)
    }
  }

  const handleGenerate = (handover: HandoverConfigPublic) => {
    generatePromptMutation.mutate({
      targetAgentId: handover.target_agent_id,
      handoverId: handover.id,
    })
  }

  // Get editing value or fallback to current value
  const getPromptValue = (handover: HandoverConfigPublic) => {
    return editingPrompts[handover.id] ?? handover.handover_prompt
  }

  // Filter out current agent and already configured agents
  const availableAgents =
    agentsData?.data.filter(
      (a) =>
        a.id !== agent.id &&
        !handoversData?.data.some((h) => h.target_agent_id === a.id)
    ) || []

  const handovers = handoversData?.data || []

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div>
            <CardTitle>Handover to Agents</CardTitle>
            <CardDescription>
              Configure when and how this agent should trigger other agents with
              specific context
            </CardDescription>
          </div>
          {availableAgents.length > 0 && (
            <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
              <DialogTrigger asChild>
                <Button size="sm">
                  <Plus className="mr-2" />
                  Add Handover
                </Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-md">
                <DialogHeader>
                  <DialogTitle>Add Handover</DialogTitle>
                  <DialogDescription>
                    Select an agent to hand over to. Click on an agent to add it.
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-2 max-h-[400px] overflow-y-auto">
                  {availableAgents.map((a) => {
                    const colorPreset = getColorPreset(a.ui_color_preset)
                    return (
                      <button
                        key={a.id}
                        onClick={() => createHandoverMutation.mutate(a.id)}
                        disabled={createHandoverMutation.isPending}
                        className={`w-full text-left px-4 py-2.5 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed ${colorPreset.badgeBg} ${colorPreset.badgeText} ${colorPreset.badgeHover}`}
                      >
                        <div className="flex items-center gap-3">
                          <div className={`rounded-lg p-2 ${colorPreset.iconBg}`}>
                            <Bot className={`h-4 w-4 ${colorPreset.iconText}`} />
                          </div>
                          <span className="font-medium">{a.name}</span>
                        </div>
                      </button>
                    )
                  })}
                </div>
              </DialogContent>
            </Dialog>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Existing handovers */}
        {handovers.map((handover) => {
          const targetAgent = agentsData?.data.find((a) => a.id === handover.target_agent_id)
          const colorPreset = getColorPreset(targetAgent?.ui_color_preset)

          return (
            <div
              key={handover.id}
              className="border rounded-lg p-4 space-y-3"
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className={`rounded-lg p-1.5 ${colorPreset.iconBg}`}>
                    <Bot className={`h-4 w-4 ${colorPreset.iconText}`} />
                  </div>
                  <span className="font-medium">{handover.target_agent_name}</span>
                </div>
                <div className="flex items-center gap-2">
                  <label className="flex cursor-pointer select-none items-center">
                    <div className="relative">
                      <input
                        type="checkbox"
                        checked={handover.enabled}
                        onChange={(e) =>
                          handleToggleEnabled(handover.id, e.target.checked)
                        }
                        className="sr-only"
                      />
                      <div
                        className={`block h-6 w-11 rounded-full transition-colors ${
                          handover.enabled
                            ? "bg-emerald-500"
                            : "bg-gray-300 dark:bg-gray-600"
                        }`}
                      ></div>
                      <div
                        className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                          handover.enabled ? "translate-x-5" : ""
                        }`}
                      ></div>
                    </div>
                  </label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleGenerate(handover)}
                    disabled={generatePromptMutation.isPending || !handover.enabled}
                  >
                    <Sparkles className="h-4 w-4 mr-1" />
                    Generate
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleDelete(handover.id)}
                    disabled={deleteHandoverMutation.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>

              {handover.enabled && (
                <>
                  <Textarea
                    placeholder="Enter handover prompt..."
                    className="min-h-[100px]"
                    value={getPromptValue(handover)}
                    onChange={(e) => handlePromptChange(handover.id, e.target.value)}
                  />

                  {dirtyPrompts.has(handover.id) && (
                    <div className="flex justify-end">
                      <Button
                        onClick={() => handleSavePrompt(handover.id)}
                        disabled={updateHandoverMutation.isPending}
                      >
                        {updateHandoverMutation.isPending
                          ? "Saving..."
                          : "Apply Prompt"}
                      </Button>
                    </div>
                  )}
                </>
              )}
            </div>
          )
        })}

        {availableAgents.length === 0 && handovers.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-4">
            No other agents available for handover configuration
          </p>
        )}
      </CardContent>
    </Card>
  )
}
