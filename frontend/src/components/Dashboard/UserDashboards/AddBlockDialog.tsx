import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Globe, MessageSquare, ClipboardList, FileText } from "lucide-react"

import { AgentsService, DashboardsService } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { getColorPreset } from "@/utils/colorPresets"
import useCustomToast from "@/hooks/useCustomToast"

interface AddBlockDialogProps {
  dashboardId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

type ViewType = "latest_session" | "latest_tasks" | "webapp" | "agent_env_file"

const VIEW_TYPES: { value: ViewType; label: string; icon: React.ElementType }[] = [
  { value: "latest_session", label: "Latest Session", icon: MessageSquare },
  { value: "latest_tasks", label: "Latest Tasks", icon: ClipboardList },
  { value: "webapp", label: "Web App", icon: Globe },
  { value: "agent_env_file", label: "Agent Env File", icon: FileText },
]

export function AddBlockDialog({ dashboardId, open, onOpenChange }: AddBlockDialogProps) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [selectedAgentId, setSelectedAgentId] = useState<string>("")
  const [viewType, setViewType] = useState<ViewType>("latest_session")
  const [filePath, setFilePath] = useState<string>("")

  // Fetch all agents without workspace filter
  const { data: agentsData, isLoading: agentsLoading } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: open,
  })

  // Fetch available files for agent_env_file view type
  const { data: availableFiles, isLoading: filesLoading } = useQuery({
    queryKey: ["agentEnvFiles", selectedAgentId],
    queryFn: () => DashboardsService.listAgentEnvFiles({
      agentId: selectedAgentId,
      subfolder: "files",
    }),
    enabled: viewType === "agent_env_file" && !!selectedAgentId && open,
  })

  const files = (availableFiles ?? []).map((f) => `files/${f}`)

  const agents = agentsData?.data ?? []
  const selectedAgent = agents.find((a) => a.id === selectedAgentId)
  const webappNotEnabled = viewType === "webapp" && selectedAgent && !selectedAgent.webapp_enabled

  const addBlockMutation = useMutation({
    mutationFn: () => {
      const config = viewType === "agent_env_file" && filePath
        ? { file_path: filePath }
        : null

      return DashboardsService.addBlock({
        dashboardId,
        requestBody: {
          agent_id: selectedAgentId,
          view_type: viewType,
          title: null,
          show_border: true,
          grid_x: 0,
          grid_y: 0,
          grid_w: 2,
          grid_h: 2,
          config,
        },
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboard", dashboardId] })
      onOpenChange(false)
      setSelectedAgentId("")
      setViewType("latest_session")
      setFilePath("")
    },
    onError: (error: { body?: { detail?: string } }) => {
      showErrorToast(error.body?.detail || "Failed to add block")
    },
  })

  const handleAdd = () => {
    if (!selectedAgentId) return
    addBlockMutation.mutate()
  }

  const handleAgentChange = (agentId: string) => {
    setSelectedAgentId(agentId)
    setFilePath("")
  }

  const handleViewTypeChange = (vt: string) => {
    setViewType(vt as ViewType)
    setFilePath("")
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add Block</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* Agent picker */}
          <div className="flex items-center gap-4">
            <Label className="w-24 shrink-0 text-right text-sm">Agent</Label>
            <Select value={selectedAgentId} onValueChange={handleAgentChange} disabled={agentsLoading}>
              <SelectTrigger className="ml-auto w-56">
                <SelectValue placeholder={agentsLoading ? "Loading agents..." : "Select an agent"} />
              </SelectTrigger>
              <SelectContent>
                {agents.map((agent) => {
                  const colorPreset = getColorPreset(agent.ui_color_preset)
                  return (
                    <SelectItem key={agent.id} value={agent.id}>
                      <div className="flex items-center gap-2">
                        <span className={`h-2 w-2 rounded-full shrink-0 ${colorPreset.badgeBg} bg-current`} />
                        {agent.name}
                      </div>
                    </SelectItem>
                  )
                })}
              </SelectContent>
            </Select>
          </div>

          {/* View type */}
          <div className="flex items-center gap-4">
            <Label className="w-24 shrink-0 text-right text-sm">View Type</Label>
            <Select value={viewType} onValueChange={handleViewTypeChange}>
              <SelectTrigger className="ml-auto w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {VIEW_TYPES.map(({ value, label, icon: Icon }) => {
                  const isDisabled = value === "webapp" && selectedAgent && !selectedAgent.webapp_enabled
                  return (
                    <SelectItem key={value} value={value} disabled={!!isDisabled}>
                      <div className="flex items-center gap-2">
                        <Icon className="h-4 w-4 text-muted-foreground" />
                        {label}
                      </div>
                    </SelectItem>
                  )
                })}
              </SelectContent>
            </Select>
          </div>

          {webappNotEnabled && (
            <p className="text-xs text-destructive text-right">
              Web App is not enabled for this agent.
            </p>
          )}

          {/* Agent Env File: file selector */}
          {viewType === "agent_env_file" && selectedAgentId && (
            <div className="flex items-center gap-4">
              <Label className="w-24 shrink-0 text-right text-sm">File</Label>
              <Select
                value={filePath}
                onValueChange={setFilePath}
                disabled={filesLoading}
              >
                <SelectTrigger className="ml-auto w-56">
                  <SelectValue
                    placeholder={
                      filesLoading
                        ? "Loading files..."
                        : files.length === 0
                        ? "No files found"
                        : "Select a file"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {files.map((file) => (
                    <SelectItem key={file} value={file}>
                      {file}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleAdd}
            disabled={
              !selectedAgentId ||
              !!webappNotEnabled ||
              addBlockMutation.isPending
            }
          >
            Add Block
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
