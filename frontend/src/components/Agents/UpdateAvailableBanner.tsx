/**
 * UpdateAvailableBanner — replaces the legacy CloneManagement/UpdateBanner.
 *
 * Shown on installs where ``pending_update=true``. Action: apply now (calls
 * ``applyUpdate`` from the new ``InstallsService``).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"

import type { AgentPublic } from "@/client"
import { InstallsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"

interface UpdateAvailableBannerProps {
  agent: AgentPublic
}

export function UpdateAvailableBanner({ agent }: UpdateAvailableBannerProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const applyMutation = useMutation({
    mutationFn: () => InstallsService.applyUpdate({ agentId: agent.id }),
    onSuccess: () => {
      showSuccessToast("Update applied")
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Failed to apply update")
    },
  })

  if (!agent.pending_update) return null

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/30 p-4 mb-4 flex items-center justify-between">
      <div>
        <p className="text-sm font-medium">Update available</p>
        <p className="text-xs text-muted-foreground">
          A new revision of this bundle has been published. Apply to update your install.
        </p>
      </div>
      <Button
        onClick={() => applyMutation.mutate()}
        disabled={applyMutation.isPending}
      >
        {applyMutation.isPending ? "Applying..." : "Apply update"}
      </Button>
    </div>
  )
}
