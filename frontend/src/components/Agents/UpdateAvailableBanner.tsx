/**
 * UpdateAvailableBanner — replaces the legacy CloneManagement/UpdateBanner.
 *
 * Shown on installs where ``pending_update=true``. Action: apply now (calls
 * ``applyUpdate`` from the new ``InstallsService``).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { AgentPublic } from "@/client"
import { InstallsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"

interface UpdateAvailableBannerProps {
  agent: AgentPublic
}

/** Render a revision as ``v<version>`` when a version label exists, else
 *  ``rev <number>``. Returns null when neither is known. */
function revisionLabel(
  version: string | null | undefined,
  number: number | null | undefined,
): string | null {
  if (version) return `v${version}`
  if (number != null) return `rev ${number}`
  return null
}

export function UpdateAvailableBanner({ agent }: UpdateAvailableBannerProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Resolve the version labels so the banner can tell the user exactly what
  // they're moving from/to. ``check-updates`` is a cheap call that also
  // reconciles ``pending_update`` server-side.
  const { data: updateInfo } = useQuery({
    queryKey: ["agent", agent.id, "check-updates"],
    queryFn: () => InstallsService.checkUpdates({ agentId: agent.id }),
    enabled: !!agent.pending_update,
  })

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

  const fromLabel = revisionLabel(
    updateInfo?.installed_version,
    updateInfo?.installed_revision_number,
  )
  const toLabel = revisionLabel(
    updateInfo?.latest_version,
    updateInfo?.latest_revision_number,
  )

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/30 p-4 mb-4 flex items-center justify-between gap-4">
      <div>
        <p className="text-sm font-medium">
          Update available
          {toLabel && (
            <span className="ml-1.5 font-normal text-muted-foreground">
              {fromLabel ? `${fromLabel} → ${toLabel}` : toLabel}
            </span>
          )}
        </p>
        <p className="text-xs text-muted-foreground">
          A new revision of this bundle has been published. Apply to update your
          install{toLabel ? ` to ${toLabel}` : ""}.
        </p>
      </div>
      <Button
        className="shrink-0"
        onClick={() => applyMutation.mutate()}
        disabled={applyMutation.isPending}
      >
        {applyMutation.isPending
          ? "Applying..."
          : toLabel
            ? `Update to ${toLabel}`
            : "Apply update"}
      </Button>
    </div>
  )
}
