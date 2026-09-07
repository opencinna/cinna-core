import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, EyeOff, PackagePlus, Plus, Trash2 } from "lucide-react"
import { useMemo, useState } from "react"

import { CatalogService, ServerChannelsService } from "@/client"
import {
  ListRow,
  ListRowGroup,
  RowFlag,
  RowInfo,
} from "@/components/Common/ListRow"
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
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { NO_TRIGGER_PROMPT_WARNING, VISIBILITY_WARNING } from "./channelCopy"

export function AutoInstallAgentsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [selected, setSelected] = useState<string>("")
  const [addOpen, setAddOpen] = useState(false)

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
      setAddOpen(false)
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
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <PackagePlus className="h-5 w-5" />
            Auto-install agents
          </CardTitle>
          <Button
            size="sm"
            onClick={() => setAddOpen(true)}
            disabled={catalogError || addable.length === 0}
          >
            <Plus className="mr-1 h-4 w-4" />
            Add bundle
          </Button>
        </div>
        <CardDescription>
          When no agent a sender already has matches their message, these
          bundles are considered and the best match is installed for them
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* The catalog is what the Add button picks from, so a failed fetch is
            reported here rather than silently disabling the button. */}
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
            <Skeleton className="h-[48px] w-full rounded-md" />
            <Skeleton className="h-[48px] w-full rounded-md" />
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
          <ListRowGroup>
            {entries.map((entry) => {
              const isPublic = entry.visibility === "public"
              return (
                <ListRow
                  key={entry.bundle_uuid}
                  title={entry.display_name}
                  flags={
                    // Each flag says WHY the bundle won't be auto-installed —
                    // a bare warning glyph would leave the admin guessing.
                    <>
                      {!isPublic && (
                        <RowFlag
                          icon={EyeOff}
                          tone="warning"
                          label={VISIBILITY_WARNING}
                        />
                      )}
                      {!entry.has_trigger_prompt && (
                        <RowFlag
                          icon={AlertTriangle}
                          tone="warning"
                          label={NO_TRIGGER_PROMPT_WARNING}
                        />
                      )}
                      <RowInfo
                        facts={[
                          entry.bundle_id,
                          `Visibility: ${entry.visibility}`,
                        ]}
                      />
                    </>
                  }
                >
                  {/* One action, and it is destructive, so it is
                      hover-revealed rather than in a menu of one item. */}
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                        onClick={() => removeMutation.mutate(entry.bundle_uuid)}
                        disabled={
                          removeMutation.isPending &&
                          removeMutation.variables === entry.bundle_uuid
                        }
                        aria-label={`Remove ${entry.display_name}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      Remove from auto-install
                    </TooltipContent>
                  </Tooltip>
                </ListRow>
              )
            })}
          </ListRowGroup>
        )}
      </CardContent>

      {/* Adding is a Create story with one field, so it is a dialog off the
          header button — not a Select and a button sitting above the list on
          every visit (§1 Create, §2 card blocks). */}
      <Dialog
        open={addOpen}
        onOpenChange={(next) => {
          if (addMutation.isPending) return
          setAddOpen(next)
          if (!next) setSelected("")
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Add a bundle to auto-install</DialogTitle>
            <DialogDescription>
              Only published bundles that are not already on the list are
              offered.
            </DialogDescription>
          </DialogHeader>
          <Select value={selected} onValueChange={setSelected}>
            <SelectTrigger>
              <SelectValue placeholder="Select a bundle" />
            </SelectTrigger>
            <SelectContent>
              {addable.map((c) => (
                <SelectItem key={c.bundle_uuid} value={c.bundle_uuid}>
                  {c.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button
              disabled={!selected || addMutation.isPending}
              onClick={() => selected && addMutation.mutate(selected)}
            >
              {addMutation.isPending ? "Adding…" : "Add bundle"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
