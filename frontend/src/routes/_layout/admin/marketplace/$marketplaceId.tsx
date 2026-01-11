import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useState, useEffect } from "react"
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query"
import { ArrowLeft, EllipsisVertical, Trash, RefreshCw } from "lucide-react"

import { LlmPluginsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
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
import { HashTabs } from "@/components/Common/HashTabs"
import { MarketplaceConfigurationTab } from "@/components/Admin/MarketplaceConfigurationTab"
import { MarketplacePluginsTab } from "@/components/Admin/MarketplacePluginsTab"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/admin/marketplace/$marketplaceId")({
  component: MarketplaceDetailPage,
})

function MarketplaceDetailPage() {
  const { marketplaceId } = Route.useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [menuOpen, setMenuOpen] = useState(false)
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)

  const deleteMutation = useMutation({
    mutationFn: () => LlmPluginsService.deleteMarketplace({ marketplaceId }),
    onSuccess: () => {
      showSuccessToast("Marketplace deleted")
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
      navigate({ to: "/admin/marketplaces" })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete marketplace")
    },
  })

  const syncMutation = useMutation({
    mutationFn: () => LlmPluginsService.syncMarketplace({ marketplaceId }),
    onSuccess: () => {
      showSuccessToast("Marketplace synced successfully")
      queryClient.invalidateQueries({ queryKey: ["marketplace", marketplaceId] })
      queryClient.invalidateQueries({ queryKey: ["marketplace-plugins", marketplaceId] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to sync marketplace")
    },
  })

  const {
    data: marketplace,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["marketplace", marketplaceId],
    queryFn: () => LlmPluginsService.getMarketplace({ marketplaceId }),
    enabled: !!marketplaceId,
  })

  const handleBack = () => {
    navigate({ to: "/admin/marketplaces" })
  }

  // Update header when marketplace loads
  useEffect(() => {
    if (marketplace) {
      setHeaderContent(
        <>
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="sm" onClick={handleBack} className="shrink-0">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <h1 className="text-base font-semibold truncate">{marketplace.name}</h1>
              <p className="text-xs text-muted-foreground">Plugin Marketplace</p>
            </div>
          </div>
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="shrink-0">
                <EllipsisVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => syncMutation.mutate()}
                disabled={syncMutation.isPending}
              >
                <RefreshCw className={`mr-2 h-4 w-4 ${syncMutation.isPending ? "animate-spin" : ""}`} />
                {syncMutation.isPending ? "Syncing..." : "Sync Now"}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => setIsDeleteDialogOpen(true)}
                className="text-destructive focus:text-destructive"
              >
                <Trash className="mr-2 h-4 w-4" />
                Delete Marketplace
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )
    }
    return () => setHeaderContent(null)
  }, [marketplace, setHeaderContent, menuOpen, syncMutation.isPending])

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !marketplace) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading marketplace details</p>
      </div>
    )
  }

  const tabs = [
    {
      value: "configuration",
      title: "Configuration",
      content: <MarketplaceConfigurationTab marketplace={marketplace} marketplaceId={marketplaceId} />,
    },
    {
      value: "plugins",
      title: "Plugins",
      content: <MarketplacePluginsTab marketplace={marketplace} marketplaceId={marketplaceId} />,
    },
  ]

  return (
    <>
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <HashTabs tabs={tabs} defaultTab="configuration" />
        </div>
      </div>

      <AlertDialog open={isDeleteDialogOpen} onOpenChange={setIsDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Marketplace</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{marketplace.name}"? This will also remove all associated plugins and agent links. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
