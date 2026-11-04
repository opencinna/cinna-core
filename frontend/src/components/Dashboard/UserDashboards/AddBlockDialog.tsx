import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Globe, MessageSquare, ClipboardList, FileText, LayoutGrid } from "lucide-react"

import {
  AgentsService,
  DashboardsService,
  type DashboardComponentPublic,
  type UserDashboardBlockCreate,
} from "@/client"
import { AgentSelectorList } from "@/components/Common/AgentSelectorDialog"
import { ComponentPicker } from "@/components/Dashboard/AgentComponents/ComponentPicker"
import { readSize, SIZE_GRID } from "@/components/Dashboard/AgentComponents/componentSpec"
import { componentErrorMessage } from "@/components/Dashboard/AgentComponents/tileConfig"
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
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import useCustomToast from "@/hooks/useCustomToast"

interface AddBlockDialogProps {
  dashboardId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

type ViewType = NonNullable<UserDashboardBlockCreate["view_type"]>

const VIEW_TYPES: { value: ViewType; label: string; icon: React.ElementType }[] = [
  { value: "latest_session", label: "Latest session", icon: MessageSquare },
  { value: "latest_tasks", label: "Latest tasks", icon: ClipboardList },
  { value: "webapp", label: "Web app", icon: Globe },
  { value: "agent_env_file", label: "Env file", icon: FileText },
  { value: "agent_component", label: "Agent component", icon: LayoutGrid },
]

const DEFAULT_SIZE = { w: 2, h: 2 }

/**
 * Add block (UI spec S2): Agent · View · (Component | File), stacked. The
 * agent is a trigger + popover (no dialog over this one), the view a pill
 * radiogroup whose disabled pill says why in its tooltip.
 */
export function AddBlockDialog({ dashboardId, open, onOpenChange }: AddBlockDialogProps) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [selectedAgentId, setSelectedAgentId] = useState<string>("")
  const [viewType, setViewType] = useState<ViewType>("latest_session")
  const [filePath, setFilePath] = useState<string>("")
  const [component, setComponent] = useState<DashboardComponentPublic | null>(null)

  // Same key as the agent picker's own list: one request for both.
  const { data: agentsData } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 200 }),
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
  const webappDisabled = !!selectedAgent && !selectedAgent.webapp_enabled

  const reset = () => {
    setSelectedAgentId("")
    setViewType("latest_session")
    setFilePath("")
    setComponent(null)
  }

  const addBlockMutation = useMutation({
    mutationFn: () => {
      let config: Record<string, unknown> | null = null
      let size = DEFAULT_SIZE
      if (viewType === "agent_env_file" && filePath) {
        config = { file_path: filePath }
      } else if (viewType === "agent_component" && component) {
        config = { component: component.name, skill: component.skill || null, params: {}, refresh: {} }
        const hint = readSize(component)
        if (hint) size = SIZE_GRID[hint]
      }

      return DashboardsService.addBlock({
        dashboardId,
        requestBody: {
          agent_id: selectedAgentId,
          view_type: viewType,
          title: null,
          show_border: true,
          grid_x: 0,
          grid_y: 0,
          grid_w: size.w,
          grid_h: size.h,
          config,
        },
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboard", dashboardId] })
      onOpenChange(false)
      reset()
    },
    onError: (error: unknown) => {
      showErrorToast(componentErrorMessage(error, "Failed to add block"))
    },
  })

  const handleAgentChange = (agentId: string) => {
    setSelectedAgentId(agentId)
    setFilePath("")
    setComponent(null)
    const agent = agents.find((a) => a.id === agentId)
    if (viewType === "webapp" && agent && !agent.webapp_enabled) setViewType("latest_session")
  }

  const handleViewTypeChange = (vt: ViewType) => {
    setViewType(vt)
    setFilePath("")
  }

  const canAdd =
    !!selectedAgentId &&
    !(viewType === "webapp" && webappDisabled) &&
    !(viewType === "agent_component" && !component)

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!addBlockMutation.isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>Add Block</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label>Agent</Label>
            <AgentSelectorList
              mode="single"
              active={open}
              selectedAgentId={selectedAgentId}
              onSelect={handleAgentChange}
              disabled={addBlockMutation.isPending}
            />
          </div>

          <div className="space-y-1.5">
            <Label id="add-block-view">View</Label>
            <ViewTypePills
              value={viewType}
              onChange={handleViewTypeChange}
              webappDisabled={webappDisabled}
            />
          </div>

          {viewType === "agent_env_file" && selectedAgentId && (
            <div className="space-y-1.5">
              <Label htmlFor="add-block-file">File</Label>
              <Select
                value={filePath}
                onValueChange={setFilePath}
                disabled={filesLoading}
              >
                <SelectTrigger id="add-block-file" className="w-full">
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

          {viewType === "agent_component" && selectedAgentId && (
            <div className="space-y-1.5">
              <Label>Component</Label>
              <ComponentPicker
                agentId={selectedAgentId}
                dashboardId={dashboardId}
                value={component}
                onChange={setComponent}
                disabled={addBlockMutation.isPending}
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={addBlockMutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            onClick={() => addBlockMutation.mutate()}
            loading={addBlockMutation.isPending}
            disabled={!canAdd}
          >
            Add block
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The view as a one-of-n pill group (P4 pill skeleton). */
function ViewTypePills({
  value,
  onChange,
  webappDisabled,
}: {
  value: ViewType
  onChange: (value: ViewType) => void
  webappDisabled: boolean
}) {
  return (
    <div role="radiogroup" aria-labelledby="add-block-view" className="flex flex-wrap gap-2">
      {VIEW_TYPES.map(({ value: option, label, icon: Icon }) => {
        const disabled = option === "webapp" && webappDisabled
        const checked = option === value
        const pill = (
          // biome-ignore lint/a11y/useSemanticElements: a pill with a glyph and a label; §2 Choice-control ARIA asks for role="radio" on hand-rolled one-of-n options.
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-disabled={disabled || undefined}
            onClick={() => {
              if (!disabled) onChange(option)
            }}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
              "outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              checked
                ? "border-primary bg-primary text-primary-foreground"
                : "bg-background hover:bg-muted",
              disabled && "cursor-not-allowed opacity-50 hover:bg-background",
            )}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" />
            <span>{label}</span>
          </button>
        )
        if (!disabled) return pill
        return (
          <Tooltip key={option}>
            <TooltipTrigger asChild>{pill}</TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Web App is not enabled for this agent
            </TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}
