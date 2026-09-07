import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FolderKanban, Plus } from "lucide-react"
import { useEffect, useState } from "react"
import { type UserWorkspacePublic, UserWorkspacesService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { WORKSPACE_ICONS } from "@/config/workspaceIcons"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { cn } from "@/lib/utils"
import { AllWorkspacesSheet } from "./AllWorkspacesSheet"
import { WorkspaceRow } from "./WorkspaceRow"

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

function WorkspaceFormDialog({
  open,
  onClose,
  workspace,
  onSubmit,
  isPending,
}: {
  open: boolean
  onClose: () => void
  workspace?: UserWorkspacePublic | null
  onSubmit: (name: string, icon: string) => void
  isPending: boolean
}) {
  const [name, setName] = useState("")
  const [icon, setIcon] = useState("folder-kanban")
  const isEdit = !!workspace

  useEffect(() => {
    if (open) {
      setName(workspace?.name ?? "")
      setIcon(workspace?.icon ?? "folder-kanban")
    }
  }, [open, workspace])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    onSubmit(name.trim(), icon)
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[425px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>
              {isEdit ? "Edit Workspace" : "Create Workspace"}
            </DialogTitle>
            <DialogDescription>
              {isEdit
                ? "Update workspace name and icon."
                : "Create a workspace to organize your agents, credentials, and sessions."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="ws-name">Name</Label>
              <Input
                id="ws-name"
                placeholder="e.g., Financial Data, Email Management"
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

export function WorkspaceSettings() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { workspacesEnabled, setWorkspacesEnabled } = useWorkspace()
  const [createOpen, setCreateOpen] = useState(false)
  const [editWorkspace, setEditWorkspace] =
    useState<UserWorkspacePublic | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [isSheetOpen, setIsSheetOpen] = useState(false)

  const {
    data: workspacesData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["userWorkspaces"],
    queryFn: () => UserWorkspacesService.readWorkspaces(),
  })

  const createMutation = useMutation({
    mutationFn: (data: { name: string; icon: string }) =>
      UserWorkspacesService.createWorkspace({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
      showSuccessToast("Workspace created")
      setCreateOpen(false)
    },
    onError: () => showErrorToast("Failed to create workspace"),
  })

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string
      data: { name?: string; icon?: string }
    }) =>
      UserWorkspacesService.updateWorkspace({
        workspaceId: id,
        requestBody: data,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
      showSuccessToast("Workspace updated")
      setEditWorkspace(null)
    },
    onError: () => showErrorToast("Failed to update workspace"),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      UserWorkspacesService.deleteWorkspace({ workspaceId: id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userWorkspaces"] })
      showSuccessToast("Workspace deleted")
      setDeleteId(null)
    },
    onError: () => showErrorToast("Failed to delete workspace"),
  })

  const workspaces = workspacesData?.data ?? []

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 min-w-0">
              <FolderKanban className="h-5 w-5 shrink-0" />
              Workspaces
            </CardTitle>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                onClick={() => setCreateOpen(true)}
                disabled={!workspacesEnabled}
              >
                <Plus className="mr-1.5 h-4 w-4" />
                New workspace
              </Button>
              {/* The card's master switch. A `Switch` is right here — it is a
                  card-level auto-saving boolean with a label, not a row
                  control — and it replaces a hand-rolled checkbox-and-div
                  toggle that reimplemented the primitive (R11). */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center">
                    <Switch
                      checked={workspacesEnabled}
                      onCheckedChange={setWorkspacesEnabled}
                      aria-label="Scope this account by workspace"
                    />
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs">
                  {workspacesEnabled
                    ? "Workspace filtering is on"
                    : "Workspace filtering is off"}
                </TooltipContent>
              </Tooltip>
            </div>
          </div>
          <CardDescription>
            Organize agents, credentials, and sessions into workspaces.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {!workspacesEnabled && (
            <p className="text-sm text-muted-foreground">
              Workspace filtering is disabled. Re-enable to scope your agents,
              sessions, tasks, and credentials by workspace.
            </p>
          )}

          <PreviewList
            items={workspaces}
            getKey={(ws) => ws.id}
            renderItem={(ws) => (
              <WorkspaceRow
                workspace={ws}
                onEdit={() => setEditWorkspace(ws)}
                onDelete={() => setDeleteId(ws.id)}
              />
            )}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load your workspaces"
            empty={
              <p className="text-sm text-muted-foreground">
                No workspaces yet — everything lives in the Default workspace.
              </p>
            }
            onShowAll={() => setIsSheetOpen(true)}
          />
        </CardContent>
      </Card>

      <AllWorkspacesSheet
        workspaces={workspaces}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
        onEdit={(ws) => setEditWorkspace(ws)}
        onDelete={(ws) => setDeleteId(ws.id)}
      />

      {/* Create Workspace Dialog */}
      <WorkspaceFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={(name, icon) => createMutation.mutate({ name, icon })}
        isPending={createMutation.isPending}
      />

      {/* Edit Workspace Dialog */}
      <WorkspaceFormDialog
        open={!!editWorkspace}
        onClose={() => setEditWorkspace(null)}
        workspace={editWorkspace}
        onSubmit={(name, icon) =>
          editWorkspace &&
          updateMutation.mutate({ id: editWorkspace.id, data: { name, icon } })
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
            <AlertDialogTitle>Delete Workspace</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure? Entities in this workspace will move to the Default
              workspace. This action cannot be undone.
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
