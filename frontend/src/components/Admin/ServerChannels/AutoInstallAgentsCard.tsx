import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, EyeOff, PackagePlus, Trash2 } from "lucide-react"
import { useMemo, useState } from "react"

import { CatalogService, ServerChannelsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { NO_TRIGGER_PROMPT_WARNING, VISIBILITY_WARNING } from "./channelCopy"

export function AutoInstallAgentsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [selected, setSelected] = useState<string>("")

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["autoInstallBundles"],
    queryFn: () => ServerChannelsService.listAutoInstallBundles(),
  })

  // Reuses the existing catalog cache — this page is admin-only and the
  // catalog listing is already fetched elsewhere in the app.
  const {
    data: catalog,
    isError: catalogError,
    error: catalogErrorObj,
  } = useQuery({
    queryKey: ["catalog"],
    queryFn: () => CatalogService.listCatalog(),
  })

  const entries = data ?? []

  const addable = useMemo(() => {
    const already = new Set(entries.map((e) => e.bundle_uuid))
    return (catalog?.data ?? []).filter(
      (c) =>
        !already.has(c.bundle_uuid) &&
        // A bundle with no published revision can't be installed at all.
        c.latest_revision_id !== null,
    )
  }, [catalog, entries])

  const addMutation = useMutation({
    mutationFn: (bundleUuid: string) =>
      ServerChannelsService.addAutoInstallBundle({
        requestBody: { bundle_uuid: bundleUuid },
      }),
    onSuccess: (list) => {
      // The route returns the whole list precisely so the UI can re-render
      // from one response; seeding the cache skips a redundant round-trip.
      queryClient.setQueryData(["autoInstallBundles"], list)
      showSuccessToast("Added to the auto-install list")
      setSelected("")
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to add bundle")),
  })

  const removeMutation = useMutation({
    mutationFn: (bundleUuid: string) =>
      ServerChannelsService.removeAutoInstallBundle({ bundleUuid }),
    // `variables` is the uuid in flight — used below to scope the disabled
    // state to that row instead of freezing the whole list.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["autoInstallBundles"] })
      showSuccessToast("Removed from the auto-install list")
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to remove bundle")),
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PackagePlus className="h-4 w-4 text-blue-500" />
          Auto-install agents
        </CardTitle>
        <CardDescription>
          When no agent a sender already has matches their message, these
          bundles are considered and the best match is installed for them
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-2">
          <Select
            value={selected}
            onValueChange={setSelected}
            disabled={catalogError}
          >
            <SelectTrigger className="flex-1">
              <SelectValue
                placeholder={
                  // Never let a failed catalog fetch read as "you've added
                  // them all" — the admin would conclude the list is complete.
                  catalogError
                    ? "Couldn't load the catalog"
                    : addable.length === 0
                      ? "No more bundles available"
                      : "Select a bundle to add"
                }
              />
            </SelectTrigger>
            <SelectContent>
              {addable.map((c) => (
                <SelectItem key={c.bundle_uuid} value={c.bundle_uuid}>
                  {c.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            disabled={!selected || catalogError || addMutation.isPending}
            onClick={() => selected && addMutation.mutate(selected)}
          >
            Add
          </Button>
        </div>

        {catalogError && (
          <p className="text-sm text-destructive">
            {getErrorMessage(
              catalogErrorObj,
              "Couldn't load the catalog to pick from.",
            )}
          </p>
        )}

        {isError ? (
          <p className="text-sm text-destructive">
            {getErrorMessage(error, "Couldn't load the auto-install list.")}
          </p>
        ) : isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-11 w-full" />
          </div>
        ) : entries.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            <PackagePlus className="mx-auto mb-2 h-8 w-8 opacity-50" />
            <p>No agents on the auto-install list</p>
            <p className="mt-1 text-xs">
              Without one, a sender with no matching agent gets a "no agent
              found" reply.
            </p>
          </div>
        ) : (
          <div className="space-y-1.5">
            {entries.map((entry) => {
              const isPublic = entry.visibility === "public"
              return (
                <div
                  key={entry.bundle_uuid}
                  className="flex items-center justify-between rounded-lg border px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate text-sm font-medium">
                        {entry.display_name}
                      </span>
                      {/* Each amber badge says WHY the bundle won't be
                          auto-installed — a bare "warning" would leave the
                          admin guessing. */}
                      {!isPublic && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Badge
                                variant="outline"
                                className="gap-1 border-amber-500/50 text-xs text-amber-600 dark:text-amber-400"
                              >
                                <EyeOff className="h-3 w-3" />
                                {entry.visibility}
                              </Badge>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs text-xs">
                              {VISIBILITY_WARNING}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                      {!entry.has_trigger_prompt && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Badge
                                variant="outline"
                                className="gap-1 border-amber-500/50 text-xs text-amber-600 dark:text-amber-400"
                              >
                                <AlertTriangle className="h-3 w-3" />
                                No trigger prompt
                              </Badge>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs text-xs">
                              {NO_TRIGGER_PROMPT_WARNING}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                    </div>
                    <p className="truncate font-mono text-xs text-muted-foreground">
                      {entry.bundle_id}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="ml-2 h-7 w-7 shrink-0 text-destructive hover:text-destructive"
                    onClick={() => removeMutation.mutate(entry.bundle_uuid)}
                    disabled={
                      removeMutation.isPending &&
                      removeMutation.variables === entry.bundle_uuid
                    }
                    aria-label={`Remove ${entry.display_name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
