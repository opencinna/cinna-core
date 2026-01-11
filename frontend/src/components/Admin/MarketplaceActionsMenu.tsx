import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { EllipsisVertical, RefreshCw, Trash, Eye } from "lucide-react"
import { useState } from "react"

import { type LLMPluginMarketplacePublic, LlmPluginsService } from "@/client"
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
import useCustomToast from "@/hooks/useCustomToast"

interface MarketplaceActionsMenuProps {
  marketplace: LLMPluginMarketplacePublic
}

export function MarketplaceActionsMenu({ marketplace }: MarketplaceActionsMenuProps) {
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const syncMutation = useMutation({
    mutationFn: () => LlmPluginsService.syncMarketplace({ marketplaceId: marketplace.id }),
    onSuccess: () => {
      showSuccessToast("Marketplace synced successfully")
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to sync marketplace")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => LlmPluginsService.deleteMarketplace({ marketplaceId: marketplace.id }),
    onSuccess: () => {
      showSuccessToast("Marketplace deleted successfully")
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to delete marketplace")
    },
  })

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0">
            <EllipsisVertical className="h-4 w-4" />
            <span className="sr-only">Open menu</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onClick={() =>
              navigate({
                to: "/admin/marketplace/$marketplaceId",
                params: { marketplaceId: marketplace.id },
              })
            }
          >
            <Eye className="mr-2 h-4 w-4" />
            View Details
          </DropdownMenuItem>
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
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

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
