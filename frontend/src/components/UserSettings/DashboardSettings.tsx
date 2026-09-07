import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  LayoutDashboard,
  Pencil,
  Plus,
  SquareArrowOutUpRight,
  Trash2,
} from "lucide-react"
import { useState } from "react"
import { DashboardsService } from "@/client"
import { ListRow, RowInfo } from "@/components/Common/ListRow"
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
import useCustomToast from "@/hooks/useCustomToast"

function DashboardFormDialog({
  open,
  onClose,
  dashboard,
  onSubmit,
  isPending,
}: {
  open: boolean
  onClose: () => void
  dashboard?: { id: string; name: string; description: string | null } | null
  onSubmit: (name: string) => void
  isPending: boolean
}) {
  const [name, setName] = useState("")
  const isEdit = !!dashboard

  // Reset form when dialog opens
  if (open && name === "" && dashboard) {
    setName(dashboard.name)
  }

  const handleClose = () => {
    setName("")
    onClose()
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    onSubmit(name.trim())
    setName("")
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[425px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>
              {isEdit ? "Edit Dashboard" : "Create Dashboard"}
            </DialogTitle>
            <DialogDescription>
              {isEdit
                ? "Update the dashboard name."
                : "Create a dashboard to monitor your agents at a glance."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="dashboard-name">Name</Label>
              <Input
                id="dashboard-name"
                placeholder="e.g., Agent Overview"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                required
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose}>
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

export function DashboardSettings() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [createOpen, setCreateOpen] = useState(false)
  const [editDashboard, setEditDashboard] = useState<{
    id: string
    name: string
    description: string | null
  } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{
    id: string
    name: string
  } | null>(null)

  const {
    data: dashboardsData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["userDashboards"],
    queryFn: () => DashboardsService.listDashboards(),
  })

  const dashboards = dashboardsData ?? []

  const createMutation = useMutation({
    mutationFn: (name: string) =>
      DashboardsService.createDashboard({
        requestBody: { name, description: null },
      }),
    onSuccess: (newDashboard) => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      showSuccessToast("Dashboard created")
      setCreateOpen(false)
      navigate({
        to: "/dashboards/$dashboardId",
        params: { dashboardId: newDashboard.id },
      })
    },
    onError: () => showErrorToast("Failed to create dashboard"),
  })

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      DashboardsService.updateDashboard({
        dashboardId: id,
        requestBody: { name },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      showSuccessToast("Dashboard updated")
      setEditDashboard(null)
    },
    onError: () => showErrorToast("Failed to update dashboard"),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      DashboardsService.deleteDashboard({ dashboardId: id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      showSuccessToast("Dashboard deleted")
      setDeleteTarget(null)
    },
    onError: () => showErrorToast("Failed to delete dashboard"),
  })

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 min-w-0">
              <LayoutDashboard className="h-5 w-5 shrink-0" />
              Dashboards
            </CardTitle>
            <Button size="sm" onClick={() => setCreateOpen(true)}>
              <Plus className="mr-1.5 h-4 w-4" />
              New dashboard
            </Button>
          </div>
          <CardDescription>
            Monitor your agents at a glance with custom dashboards.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PreviewList
            items={dashboards}
            getKey={(d) => d.id}
            renderItem={(d) => (
              <ListRow
                icon={
                  <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
                    <LayoutDashboard className="h-3.5 w-3.5 text-muted-foreground" />
                  </span>
                }
                title={d.name}
                // No metadata line: a dashboard is a name, and the description
                // is optional prose most of them do not have. It lives in the
                // detail flag on the rows that do.
                flags={
                  d.description ? (
                    <RowInfo facts={[d.description]} />
                  ) : undefined
                }
              >
                <RowActionsMenu label={`the dashboard ${d.name}`}>
                  <DropdownMenuItem
                    onSelect={() =>
                      navigate({
                        to: "/dashboards/$dashboardId",
                        params: { dashboardId: d.id },
                      })
                    }
                  >
                    <SquareArrowOutUpRight />
                    Open dashboard
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() =>
                      setEditDashboard({
                        id: d.id,
                        name: d.name,
                        description: d.description ?? null,
                      })
                    }
                  >
                    <Pencil />
                    Rename
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    onSelect={(e) => {
                      e.preventDefault()
                      setDeleteTarget({ id: d.id, name: d.name })
                    }}
                  >
                    <Trash2 />
                    Delete dashboard
                  </DropdownMenuItem>
                </RowActionsMenu>
              </ListRow>
            )}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onRetry={() => refetch()}
            errorFallback="Couldn't load your dashboards"
            empty={
              <p className="text-sm text-muted-foreground">
                No dashboards yet — create one to watch your agents at a glance.
              </p>
            }
            // A route, not a Sheet (P5's test): dashboards have a page of their
            // own and a lifecycle that lives there.
            onShowAll={() => navigate({ to: "/dashboards" })}
          />
        </CardContent>
      </Card>

      {/* Create Dashboard Dialog */}
      <DashboardFormDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={(name) => createMutation.mutate(name)}
        isPending={createMutation.isPending}
      />

      {/* Edit Dashboard Dialog */}
      <DashboardFormDialog
        open={!!editDashboard}
        onClose={() => setEditDashboard(null)}
        dashboard={editDashboard}
        onSubmit={(name) =>
          editDashboard && renameMutation.mutate({ id: editDashboard.id, name })
        }
        isPending={renameMutation.isPending}
      />

      {/* Delete Confirmation */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Dashboard</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &ldquo;{deleteTarget?.name}
              &rdquo;? All blocks will be removed. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() =>
                deleteTarget && deleteMutation.mutate(deleteTarget.id)
              }
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
