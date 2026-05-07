/**
 * AppDataTab — Settings > App Data tab
 *
 * Lists every per-(user, bundle) AppDataVolume the user owns. App data survives
 * agent uninstall/reinstall; this tab is the only surface where users can see
 * the size + clean up orphaned volumes left behind by deleted installs.
 *
 * Server-side rules (mirrored in the UI):
 *  - Wipe is only available for orphaned volumes (no install currently
 *    references the row). For active installs the user must uninstall first.
 *  - Sizes are recomputed on demand via the per-row Refresh action.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Database } from "lucide-react"

import { AppDataService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AppDataRow } from "./AppDataRow"


export function AppDataTab() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data, isLoading } = useQuery({
    queryKey: ["app-data"],
    queryFn: () => AppDataService.listAppDataVolumes(),
  })

  const recomputeMutation = useMutation({
    mutationFn: (volumeId: string) =>
      AppDataService.recomputeAppDataSize({ volumeId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["app-data"] })
    },
    onError: () => {
      showErrorToast("Failed to recompute size.")
    },
  })

  const wipeMutation = useMutation({
    mutationFn: (volumeId: string) =>
      AppDataService.wipeAppDataVolume({ volumeId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["app-data"] })
      showSuccessToast("App data volume wiped.")
    },
    onError: (err: any) => {
      showErrorToast(err?.body?.detail ?? "Failed to wipe volume.")
    },
  })

  const volumes = data?.data ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle>App Data</CardTitle>
        <CardDescription>
          Each agent stores its private runtime data in a per-user volume that
          survives uninstalls. Volumes left behind by deleted agents appear here
          as <strong>orphaned</strong> and can be wiped to free disk space.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : volumes.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <Database className="h-8 w-8 text-muted-foreground/50" />
            <p className="text-sm text-muted-foreground">
              No app data volumes yet.
            </p>
            <p className="text-xs text-muted-foreground">
              Volumes are created automatically when an agent environment starts.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {volumes.map((volume) => (
              <AppDataRow
                key={volume.id}
                volume={volume}
                onRecompute={() => recomputeMutation.mutate(volume.id)}
                onWipe={() => wipeMutation.mutate(volume.id)}
                isRecomputing={
                  recomputeMutation.isPending &&
                  recomputeMutation.variables === volume.id
                }
                isWiping={
                  wipeMutation.isPending && wipeMutation.variables === volume.id
                }
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
