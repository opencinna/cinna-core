import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Bot } from "lucide-react"
import { useState } from "react"

import type { AgentPublic, AgentUpdate } from "@/client"
import { AgentsService } from "@/client"
import { COLOR_PRESETS, getColorPreset } from "@/utils/colorPresets"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Switch } from "@/components/ui/switch"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface AgentConfigurationTabProps {
  agent: AgentPublic
}

export function AgentConfigurationTab({ agent }: AgentConfigurationTabProps) {
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const updateMutation = useMutation({
    mutationFn: (data: AgentUpdate) =>
      AgentsService.updateAgent({ id: agent.id, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Agent color updated successfully")
      setIsDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update agent color")
    },
  })

  const updateDashboardVisibilityMutation = useMutation({
    mutationFn: (showOnDashboard: boolean) =>
      AgentsService.updateAgent({
        id: agent.id,
        requestBody: { show_on_dashboard: showOnDashboard }
      }),
    onSuccess: () => {
      showSuccessToast("Dashboard visibility updated successfully")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to update dashboard visibility")
    },
  })

  const handleColorChange = (colorPreset: string) => {
    updateMutation.mutate({ ui_color_preset: colorPreset })
  }

  const handleDashboardVisibilityChange = (checked: boolean) => {
    updateDashboardVisibilityMutation.mutate(checked)
  }

  const currentPreset = getColorPreset(agent.ui_color_preset)

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {/* Appearance Card */}
      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
          <CardDescription>
            Customize how your agent appears in the interface
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div>
            <label className="text-sm font-medium mb-3 block">Color Preset</label>
            <div className="flex items-center gap-4">
              <button
                onClick={() => setIsDialogOpen(true)}
                className="rounded-lg p-3 hover:opacity-80 transition-opacity cursor-pointer"
              >
                <div className={`rounded-lg p-3 ${currentPreset.iconBg}`}>
                  <Bot className={`h-8 w-8 ${currentPreset.iconText}`} />
                </div>
              </button>
              <div>
                <p className="text-sm font-medium">{currentPreset.name}</p>
                <p className="text-xs text-muted-foreground">
                  Click on the icon to change the color preset
                </p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Usability Card */}
      <Card>
        <CardHeader>
          <CardTitle>Usability</CardTitle>
          <CardDescription>
            Control where and how this agent appears in the application
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Show on Dashboard</p>
              <p className="text-xs text-muted-foreground">
                Display this agent in the agent list on the main dashboard
              </p>
            </div>
            <Switch
              checked={agent.show_on_dashboard}
              onCheckedChange={handleDashboardVisibilityChange}
              disabled={updateDashboardVisibilityMutation.isPending}
            />
          </div>
        </CardContent>
      </Card>

      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Select Color Preset</DialogTitle>
            <DialogDescription>
              Choose a color for your agent's icon and badge
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-3 sm:grid-cols-4 gap-3 py-4">
            {COLOR_PRESETS.map((preset) => {
              const isSelected = currentPreset.value === preset.value
              return (
                <button
                  key={preset.value}
                  onClick={() => handleColorChange(preset.value)}
                  disabled={updateMutation.isPending}
                  className={`
                    flex flex-col items-center gap-2 p-4 rounded-lg border-2 transition-all
                    ${isSelected ? "border-primary" : "border-transparent hover:border-muted-foreground/30"}
                    ${updateMutation.isPending ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}
                  `}
                >
                  <div className={`rounded-lg p-3 ${preset.iconBg}`}>
                    <Bot className={`h-6 w-6 ${preset.iconText}`} />
                  </div>
                  <span className="text-xs font-medium">{preset.name}</span>
                </button>
              )
            })}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
