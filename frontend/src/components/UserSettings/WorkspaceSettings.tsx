import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pencil, Plus, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { type UserWorkspacePublic, UserWorkspacesService } from "@/client"
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
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { getWorkspaceIcon, WORKSPACE_ICONS } from "@/config/workspaceIcons"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
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

  const { data: workspacesData, isLoading } = useQuery({
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

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle>Workspaces</CardTitle>
              <CardDescription>
                Organize agents, credentials, and sessions into workspaces.
              </CardDescription>
            </div>
            <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
              <div className="relative">
                <input
                  type="checkbox"
                  checked={workspacesEnabled}
                  onChange={(e) => setWorkspacesEnabled(e.target.checked)}
                  className="sr-only"
                />
                <div
                  className={`block h-6 w-11 rounded-full transition-colors ${
                    workspacesEnabled
                      ? "bg-primary"
                      : "bg-gray-300 dark:bg-gray-600"
                  }`}
                />
                <div
                  className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                    workspacesEnabled ? "translate-x-5" : ""
                  }`}
                />
              </div>
            </label>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {!workspacesEnabled && (
            <p className="text-sm text-muted-foreground">
              Workspace filtering is disabled. Re-enable to scope your agents,
              sessions, tasks, and credentials by workspace.
            </p>
          )}
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            disabled={!workspacesEnabled}
          >
            <Plus className="h-4 w-4 mr-1.5" />
            New Workspace
          </Button>

          {isLoading ? (
            <div className="text-sm text-muted-foreground">
              Loading workspaces...
            </div>
          ) : workspacesData && workspacesData.data.length > 0 ? (
            <Table>
              <TableBody>
                {workspacesData.data.map((ws) => {
                  const Icon = getWorkspaceIcon(ws.icon)
                  return (
                    <TableRow key={ws.id} className="h-9">
                      <TableCell className="px-2 py-1">
                        <Icon className="h-4 w-4 text-muted-foreground" />
                      </TableCell>
                      <TableCell className="px-2 py-1 font-medium text-sm">
                        {ws.name}
                      </TableCell>
                      <TableCell className="px-2 py-1 text-right">
                        <div className="flex gap-1 justify-end">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => setEditWorkspace(ws)}
                            title="Edit"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => setDeleteId(ws.id)}
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5 text-destructive" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          ) : (
            <div className="text-sm text-muted-foreground">
              No workspaces yet. All entities belong to the Default workspace.
            </div>
          )}
        </CardContent>
      </Card>

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
