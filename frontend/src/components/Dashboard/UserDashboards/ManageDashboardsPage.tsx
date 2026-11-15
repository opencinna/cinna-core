import { useState, useEffect } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { LayoutDashboard, Plus, Pencil, Trash2, MoreVertical } from "lucide-react"
import { RelativeTime } from "@/components/Common/RelativeTime"

import { DashboardsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
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
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { usePageHeader } from "@/routes/_layout"

const MAX_DASHBOARDS = 10

const createDashboardSchema = z.object({
  name: z.string().min(1, "Name is required").max(255),
  description: z.string().max(1000).optional().or(z.literal("")),
})

const renameDashboardSchema = z.object({
  name: z.string().min(1, "Name is required").max(255),
  description: z.string().max(1000).optional().or(z.literal("")),
})

type CreateDashboardFormData = z.infer<typeof createDashboardSchema>
type RenameDashboardFormData = z.infer<typeof renameDashboardSchema>

export function ManageDashboardsPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()
  const { setHeaderContent } = usePageHeader()

  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string; description: string | null } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null)

  const { data: dashboards, isLoading } = useQuery({
    queryKey: ["userDashboards"],
    queryFn: () => DashboardsService.listDashboards(),
  })

  const atLimit = (dashboards?.length ?? 0) >= MAX_DASHBOARDS

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Dashboards</h1>
          <p className="text-xs text-muted-foreground">Monitor your agents at a glance</p>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <span>
              <Button
                onClick={() => setShowCreateDialog(true)}
                disabled={atLimit}
              >
                <Plus className="mr-2 h-4 w-4" />
                New Dashboard
              </Button>
            </span>
          </TooltipTrigger>
          {atLimit && (
            <TooltipContent>Maximum {MAX_DASHBOARDS} dashboards</TooltipContent>
          )}
        </Tooltip>
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, atLimit])

  const createForm = useForm<CreateDashboardFormData>({
    resolver: zodResolver(createDashboardSchema),
    defaultValues: { name: "", description: "" },
  })

  const renameForm = useForm<RenameDashboardFormData>({
    resolver: zodResolver(renameDashboardSchema),
    defaultValues: { name: "", description: "" },
  })

  const createMutation = useMutation({
    mutationFn: (data: CreateDashboardFormData) =>
      DashboardsService.createDashboard({
        requestBody: { name: data.name, description: data.description || null },
      }),
    onSuccess: (newDashboard) => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      setShowCreateDialog(false)
      createForm.reset()
      navigate({ to: "/dashboards/$dashboardId", params: { dashboardId: newDashboard.id } })
    },
    onError: (error: { body?: { detail?: string } }) => {
      showErrorToast(error.body?.detail || "Failed to create dashboard")
    },
  })

  const renameMutation = useMutation({
    mutationFn: (data: RenameDashboardFormData & { id: string }) =>
      DashboardsService.updateDashboard({
        dashboardId: data.id,
        requestBody: { name: data.name, description: data.description || null },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      setRenameTarget(null)
      renameForm.reset()
    },
    onError: (error: { body?: { detail?: string } }) => {
      showErrorToast(error.body?.detail || "Failed to rename dashboard")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => DashboardsService.deleteDashboard({ dashboardId: id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboards"] })
      setDeleteTarget(null)
    },
    onError: (error: { body?: { detail?: string } }) => {
      showErrorToast(error.body?.detail || "Failed to delete dashboard")
    },
  })

  const handleRenameOpen = (id: string, name: string, description: string | null) => {
    setRenameTarget({ id, name, description })
    renameForm.reset({ name, description: description ?? "" })
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
      {isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-40 w-full rounded-lg" />
          ))}
        </div>
      ) : !dashboards || dashboards.length === 0 ? (
        <div className="flex flex-col items-center justify-center min-h-[300px] text-center">
          <div className="rounded-full bg-muted p-6 mb-4">
            <LayoutDashboard className="h-10 w-10 text-muted-foreground" />
          </div>
          <h2 className="text-xl font-semibold mb-2">No dashboards yet</h2>
          <p className="text-muted-foreground text-sm mb-4">
            Create your first dashboard to monitor your agents at a glance
          </p>
          <Button onClick={() => setShowCreateDialog(true)}>
            <Plus className="mr-2 h-4 w-4" />
            Create Dashboard
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {dashboards.map((dashboard) => (
            <Card
              key={dashboard.id}
              className="hover:border-primary/50 transition-colors cursor-pointer group"
              onClick={() =>
                navigate({ to: "/dashboards/$dashboardId", params: { dashboardId: dashboard.id } })
              }
            >
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <CardTitle className="text-base truncate">{dashboard.name}</CardTitle>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                      >
                        <MoreVertical className="h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
                      <DropdownMenuItem
                        onClick={() =>
                          handleRenameOpen(dashboard.id, dashboard.name, dashboard.description ?? null)
                        }
                      >
                        <Pencil className="mr-2 h-4 w-4" />
                        Rename
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        className="text-destructive focus:text-destructive"
                        onClick={() => setDeleteTarget({ id: dashboard.id, name: dashboard.name })}
                      >
                        <Trash2 className="mr-2 h-4 w-4" />
                        Delete
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {dashboard.description && (
                  <CardDescription className="line-clamp-2">{dashboard.description}</CardDescription>
                )}
              </CardHeader>
              <CardContent className="pb-2">
                <p className="text-xs text-muted-foreground">
                  {dashboard.blocks?.length ?? 0} block{(dashboard.blocks?.length ?? 0) !== 1 ? "s" : ""}
                </p>
              </CardContent>
              <CardFooter>
                <p className="text-xs text-muted-foreground">
                  Updated{" "}
                  <RelativeTime timestamp={dashboard.updated_at} />
                </p>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}
      </div>

      {/* Create dialog */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New Dashboard</DialogTitle>
          </DialogHeader>
          <Form {...createForm}>
            <form onSubmit={createForm.handleSubmit((d) => createMutation.mutate(d))} className="space-y-4">
              <FormField
                control={createForm.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Name</FormLabel>
                    <FormControl>
                      <Input placeholder="My Dashboard" autoFocus {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={createForm.control}
                name="description"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Description (optional)</FormLabel>
                    <FormControl>
                      <Textarea placeholder="A brief description..." rows={2} {...field} />
                    </FormControl>
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setShowCreateDialog(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={createMutation.isPending}>
                  Create
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>

      {/* Rename dialog */}
      <Dialog open={!!renameTarget} onOpenChange={(open) => !open && setRenameTarget(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Rename Dashboard</DialogTitle>
          </DialogHeader>
          <Form {...renameForm}>
            <form
              onSubmit={renameForm.handleSubmit((d) =>
                renameTarget && renameMutation.mutate({ ...d, id: renameTarget.id })
              )}
              className="space-y-4"
            >
              <FormField
                control={renameForm.control}
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
                control={renameForm.control}
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
                <Button type="button" variant="outline" onClick={() => setRenameTarget(null)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={renameMutation.isPending}>
                  Save
                </Button>
              </DialogFooter>
            </form>
          </Form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <AlertDialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Dashboard</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{deleteTarget?.name}"? All blocks will be removed.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
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
