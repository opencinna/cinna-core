import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import {
  LayoutDashboard,
  Plus,
  Lock,
  Unlock,
  EllipsisVertical,
  Pencil,
  Trash2,
  Maximize,
} from "lucide-react"

import { AgentsService, DashboardsService } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import PendingItems from "@/components/Pending/PendingItems"
import { DashboardGrid } from "@/components/Dashboard/UserDashboards/DashboardGrid"
import { AddBlockDialog } from "@/components/Dashboard/UserDashboards/AddBlockDialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
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
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"

export const Route = createFileRoute("/_layout/dashboards/$dashboardId")({
  component: DashboardViewPage,
  head: () => ({
    meta: [
      {
        title: "Dashboard - Workflow Runner",
      },
    ],
  }),
})

const editDashboardSchema = z.object({
  name: z.string().min(1, "Name is required").max(255),
  description: z.string().max(1000).optional().or(z.literal("")),
})

type EditDashboardFormData = z.infer<typeof editDashboardSchema>

function DashboardViewPage() {
  const { dashboardId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()

  const [isEditMode, setIsEditMode] = useState(false)
  const [showAddBlock, setShowAddBlock] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)

  const { data: dashboard, isLoading: dashboardLoading } = useQuery({
    queryKey: ["userDashboard", dashboardId],
    queryFn: () => DashboardsService.getDashboard({ dashboardId }),
  })

  const { data: agentsData, isLoading: agentsLoading } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: !!dashboard,
  })

  const editForm = useForm<EditDashboardFormData>({
    resolver: zodResolver(editDashboardSchema),
    defaultValues: { name: "", description: "" },
  })

  const editMutation = useMutation({
    mutationFn: (data: EditDashboardFormData) =>
      DashboardsService.updateDashboard({
        dashboardId,
        requestBody: { name: data.name, description: data.description || null },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboard", dashboardId] })
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      setShowEditDialog(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => DashboardsService.deleteDashboard({ dashboardId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      navigate({ to: "/dashboards" })
    },
  })

  const handleOpenEdit = () => {
    if (dashboard) {
      editForm.reset({ name: dashboard.name, description: dashboard.description ?? "" })
    }
    setShowEditDialog(true)
    setMenuOpen(false)
  }

  const handleOpenDelete = () => {
    setShowDeleteDialog(true)
    setMenuOpen(false)
  }

  const handleOpenFullscreen = () => {
    setMenuOpen(false)
    const width = window.screen.availWidth
    const height = window.screen.availHeight
    const screen = window.screen as Screen & { availLeft?: number; availTop?: number }
    const left = screen.availLeft ?? 0
    const top = screen.availTop ?? 0
    window.open(
      `/dashboard-fullscreen/${dashboardId}`,
      "_blank",
      `popup,width=${width},height=${height},left=${left},top=${top},menubar=no,toolbar=no,location=no,status=no`,
    )
  }

  useEffect(() => {
    if (dashboard) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-2 min-w-0">
            <LayoutDashboard className="h-4 w-4 text-muted-foreground shrink-0" />
            <span className="font-medium truncate">{dashboard.name}</span>
          </div>
          <div className="flex items-center gap-2">
            {isEditMode && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => setShowAddBlock(true)}
                className="h-8 text-xs"
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                Add Block
              </Button>
            )}
            <Button
              size="icon"
              variant={isEditMode ? "default" : "outline"}
              onClick={() => setIsEditMode((v) => !v)}
              className="h-8 w-8 group"
              title={isEditMode ? "Lock Layout" : "Edit Layout"}
            >
              {isEditMode ? (
                <>
                  <Unlock className="h-4 w-4 group-hover:hidden" />
                  <Lock className="h-4 w-4 hidden group-hover:block" />
                </>
              ) : (
                <>
                  <Lock className="h-4 w-4 group-hover:hidden" />
                  <Unlock className="h-4 w-4 hidden group-hover:block" />
                </>
              )}
            </Button>
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="shrink-0">
                  <EllipsisVertical className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={handleOpenFullscreen}>
                  <Maximize className="mr-2 h-4 w-4" />
                  Open Fullscreen
                </DropdownMenuItem>
                <DropdownMenuItem onClick={handleOpenEdit}>
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit Dashboard
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onClick={handleOpenDelete}
                  className="text-destructive focus:text-destructive"
                >
                  <Trash2 className="mr-2 h-4 w-4" />
                  Delete Dashboard
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [dashboard, setHeaderContent, isEditMode, menuOpen])

  if (dashboardLoading || agentsLoading) {
    return <PendingItems />
  }

  if (!dashboard) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Dashboard not found</p>
      </div>
    )
  }

  const agents = agentsData?.data ?? []

  return (
    <div className="flex flex-col h-full">
      <DashboardGrid
        dashboard={dashboard}
        agents={agents}
        isEditMode={isEditMode}
        showAddBlock={showAddBlock}
        onCloseAddBlock={() => setShowAddBlock(false)}
        onRequestAddBlock={() => setShowAddBlock(true)}
      />

      {showAddBlock && (
        <AddBlockDialog
          dashboardId={dashboard.id}
          open={showAddBlock}
          onOpenChange={setShowAddBlock}
        />
      )}

      {/* Edit dashboard dialog */}
      <Dialog open={showEditDialog} onOpenChange={setShowEditDialog}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Edit Dashboard</DialogTitle>
          </DialogHeader>
          <Form {...editForm}>
            <form
              onSubmit={editForm.handleSubmit((d) => editMutation.mutate(d))}
              className="space-y-4"
            >
              <FormField
                control={editForm.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Name</FormLabel>
                    <FormControl>
                      <Input autoFocus {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={editForm.control}
                name="description"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Description (optional)</FormLabel>
                    <FormControl>
                      <Textarea rows={2} {...field} />
                    </FormControl>
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setShowEditDialog(false)}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={editMutation.isPending}>
                  Save
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Dashboard</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &ldquo;{dashboard.name}&rdquo;? All blocks
              will be removed. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
