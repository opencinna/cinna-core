import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  Pencil,
  Plus,
  SquareArrowOutUpRight,
  Trash2,
  Workflow,
} from "lucide-react"
import { useEffect, useState } from "react"
import { type AgenticTeamPublic, AgenticTeamsService } from "@/client"
import { ListRow } from "@/components/Common/ListRow"
import { PreviewList } from "@/components/Common/PreviewList"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { getWorkspaceIcon, WORKSPACE_ICONS } from "@/config/workspaceIcons"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"

function IconSelector({
  value,
  onChange,
}: {
  value: string
  onChange: (icon: string) => void
}) {
  return (
    <div className="grid grid-cols-5 gap-2">
      {WORKSPACE_ICONS.map((iconOption) => {
        const IconComponent = iconOption.icon
        return (
          <button
            key={iconOption.name}
            type="button"
            onClick={() => onChange(iconOption.name)}
            className={cn(
              "flex items-center justify-center p-2.5 rounded-md border-2 transition-colors",
              value === iconOption.name
                ? "border-primary bg-primary/10"
                : "border-muted hover:border-muted-foreground/50",
            )}
            title={iconOption.label}
          >
            <IconComponent className="h-4 w-4" />
          </button>
        )
      })}
    </div>
  )
}

export function AgenticTeamFormDialog({
  open,
  onClose,
  team,
  onSubmit,
  isPending,
}: {
  open: boolean
  onClose: () => void
  team?: AgenticTeamPublic | null
  onSubmit: (name: string, icon: string, taskPrefix?: string) => void
  isPending: boolean
}) {
  const [name, setName] = useState("")
  const [icon, setIcon] = useState("users")
  const [taskPrefix, setTaskPrefix] = useState("")
  const isEdit = !!team

  useEffect(() => {
    if (open) {
      setName(team?.name ?? "")
      setIcon(team?.icon ?? "users")
      setTaskPrefix(team?.task_prefix ?? "")
    }
  }, [open, team])

  const handleTaskPrefixChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    // Only allow uppercase alphanumeric, max 10 chars
    const value = e.target.value
      .toUpperCase()
      .replace(/[^A-Z0-9]/g, "")
      .slice(0, 10)
    setTaskPrefix(value)
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    onSubmit(name.trim(), icon, taskPrefix || undefined)
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[425px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>
              {isEdit ? "Edit Agentic Team" : "Create Agentic Team"}
            </DialogTitle>
            <DialogDescription>
              {isEdit
                ? "Update the team name, icon, and task settings."
                : "Create a named agentic team to define agent orchestration topology."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="team-name">Name</Label>
              <Input
                id="team-name"
                placeholder="e.g., Content Pipeline, IT Support Team"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                required
              />
            </div>
            <div className="grid gap-2">
              <Label>Icon</Label>
              <IconSelector value={icon} onChange={setIcon} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="task-prefix">Task Prefix</Label>
              <Input
                id="task-prefix"
                placeholder="e.g., HR, OPS (leave empty for TASK)"
                value={taskPrefix}
                onChange={handleTaskPrefixChange}
                maxLength={10}
                className="font-mono uppercase"
              />
              <p className="text-xs text-muted-foreground">
                Custom prefix for task short codes in this team (e.g.,
                HR&nbsp;→&nbsp;HR-1). Leave empty to use the default TASK
                prefix.
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || isPending}>
              {isPending
                ? isEdit
                  ? "Saving..."
                  : "Creating..."
                : isEdit
                  ? "Save"
                  : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function AgenticTeamSettings() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [createOpen, setCreateOpen] = useState(false)
  const [editTeam, setEditTeam] = useState<AgenticTeamPublic | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)

  const {
    data: teamsData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["agenticTeams"],
    queryFn: () => AgenticTeamsService.listAgenticTeams(),
  })

  const teams = teamsData?.data ?? []

  const createMutation = useMutation({
    mutationFn: (data: { name: string; icon: string; task_prefix?: string }) =>
      AgenticTeamsService.createAgenticTeam({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeams"] })
      showSuccessToast("Agentic team created")
      setCreateOpen(false)
    },
    onError: () => showErrorToast("Failed to create agentic team"),
  })

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string
      data: { name?: string; icon?: string; task_prefix?: string | null }
    }) =>
      AgenticTeamsService.updateAgenticTeam({
        teamId: id,
        requestBody: data,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeams"] })
      showSuccessToast("Agentic team updated")
      setEditTeam(null)
    },
    onError: () => showErrorToast("Failed to update agentic team"),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      AgenticTeamsService.deleteAgenticTeam({ teamId: id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agenticTeams"] })
      showSuccessToast("Agentic team deleted")
      setDeleteId(null)
    },
    onError: () => showErrorToast("Failed to delete agentic team"),
  })

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 min-w-0">
              <Workflow className="h-5 w-5 shrink-0" />
              Agentic Teams
            </CardTitle>
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1.5 h-4 w-4" />
              New team
            </Button>
          </div>
          <CardDescription>
            Define agent orchestration teams — visual org-charts that wire
            agents together with handover prompts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PreviewList
            items={teams}
            getKey={(team) => team.id}
            renderItem={(team) => {
              const Icon = getWorkspaceIcon(team.icon)
              return (
                <ListRow
                  icon={
                    <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
                      <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                    </span>
                  }
                  title={team.name}
                  // The task prefix is a short word people scan by when they
                  // are looking for the team a task code came from, so it is a
                  // badge on the title line rather than a second line.
                  badges={
                    team.task_prefix ? (
                      <Badge
                        variant="outline"
                        className="h-5 shrink-0 font-mono text-xs"
                      >
                        {team.task_prefix}
                      </Badge>
                    ) : undefined
                  }
                >
                  <RowActionsMenu label={`the team ${team.name}`}>
                    <DropdownMenuItem
                      onSelect={() =>
                        navigate({
                          to: "/agentic-teams/$teamId",
                          params: { teamId: team.id },
                        })
                      }
                    >
                      <SquareArrowOutUpRight />
                      Open team
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setEditTeam(team)}>
                      <Pencil />
                      Edit team
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      variant="destructive"
                      onSelect={(e) => {
                        e.preventDefault()
                        setDeleteId(team.id)
                      }}
                    >
                      <Trash2 />
                      Delete team
                    </DropdownMenuItem>
                  </RowActionsMenu>
                </ListRow>
              )
            }}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load your agentic teams"
            empty={
              <p className="text-sm text-muted-foreground">
                No teams yet — create one to wire agents into an org-chart.
              </p>
            }
            // A route, not a Sheet (P5's test): a team has a page of its own
            // and its whole lifecycle lives there.
            onShowAll={() => navigate({ to: "/agentic-teams" })}
          />
        </CardContent>
      </Card>

      {/* Create Team Dialog */}
      <AgenticTeamFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={(name, icon, taskPrefix) =>
          createMutation.mutate({ name, icon, task_prefix: taskPrefix })
        }
        isPending={createMutation.isPending}
      />

      {/* Edit Team Dialog */}
      <AgenticTeamFormDialog
        open={!!editTeam}
        onClose={() => setEditTeam(null)}
        team={editTeam}
        onSubmit={(name, icon, taskPrefix) =>
          editTeam &&
          updateMutation.mutate({
            id: editTeam.id,
            data: { name, icon, task_prefix: taskPrefix ?? null },
          })
        }
        isPending={updateMutation.isPending}
      />

      {/* Delete Confirmation */}
      <AlertDialog
        open={!!deleteId}
        onOpenChange={(open) => !open && setDeleteId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Agentic Team</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure? This will delete the team and all its nodes and
              connections. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteId && deleteMutation.mutate(deleteId)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
