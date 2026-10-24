import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertCircle } from "lucide-react"
import { useState } from "react"

import { type LLMPluginMarketplacePublic, LlmPluginsService } from "@/client"
import { Fact } from "@/components/Admin/MarketplacePlugin/PluginFacts"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { marketplaceEntriesKey } from "@/hooks/useMarketplaceSync"
import { getErrorMessage } from "@/utils"
import {
  accessModeCopy,
  type MarketplaceAccessMode,
  marketplaceAccessMode,
  plural,
} from "@/utils/marketplace"
import { MarketplaceAccessModeField } from "./MarketplaceAccessModeField"

/** "Only plugins I allow" → "only plugins I allow", for "Switch to …". */
function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1)
}

/**
 * S11 — switch who sees this marketplace's entries, after seeing how many
 * users would gain or lose. Choosing the other mode runs the server's dry run
 * (nothing changes); the primary button applies it. A `Dialog`, not a
 * confirm: it carries fetched impact (§2 Confirmation).
 */
export function ChangeAccessModeDialog({
  marketplace,
  open,
  onOpenChange,
}: {
  marketplace: LLMPluginMarketplacePublic
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const current = marketplaceAccessMode(marketplace.access_mode)
  const [chosen, setChosen] = useState<MarketplaceAccessMode>(current)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const changing = chosen !== current
  // Two modes: the button always names the one that is not current, so it
  // reads the same before and after the admin picks it.
  const target: MarketplaceAccessMode =
    current === "allow_all" ? "allowlist" : "allow_all"

  const preview = useQuery({
    queryKey: ["marketplace-access-mode-preview", marketplace.id, chosen],
    queryFn: () =>
      LlmPluginsService.setMarketplaceAccessMode({
        marketplaceId: marketplace.id,
        requestBody: { access_mode: chosen, dry_run: true },
      }),
    enabled: open && changing,
    // A count of what users see now — never served from an earlier open.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  })

  const apply = useMutation({
    mutationFn: () =>
      LlmPluginsService.setMarketplaceAccessMode({
        marketplaceId: marketplace.id,
        requestBody: { access_mode: chosen, dry_run: false },
      }),
    onSuccess: ({ visible_after }) => {
      showSuccessToast(
        `Plugin access changed — ${plural(visible_after, "plugin")} visible`,
      )
      onOpenChange(false)
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Couldn't change plugin access")),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ["marketplace", marketplace.id],
      })
      void queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
      void queryClient.invalidateQueries({
        queryKey: marketplaceEntriesKey(marketplace.id),
      })
    },
  })

  const locked = apply.isPending
  const handleOpenChange = (next: boolean) => {
    if (locked) return
    onOpenChange(next)
  }

  const impact = () => {
    if (!changing) return null
    if (preview.isError) {
      return (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
            <span className="break-words">
              {getErrorMessage(preview.error, "Couldn't count the entries.")}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void preview.refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )
    }
    const result = preview.data
    return (
      <div className="space-y-2">
        <Fact label="Visible to users">
          {result ? (
            `${result.visible_before} → ${plural(result.visible_after, "plugin")}`
          ) : (
            <Skeleton className="h-5 w-32" />
          )}
        </Fact>
        {result && chosen === "allowlist" && result.visible_after === 0 && (
          <p className="text-xs text-muted-foreground">
            Nothing is allowed yet — users will see none of its plugins until
            you allow some on the Plugins tab.
          </p>
        )}
        {result && result.visible_after < result.visible_before && (
          <p className="text-xs text-muted-foreground">
            Hidden plugins disappear for every user, including from Cinna
            Desktop&apos;s picker. Agents that already installed one keep it;
            desktop pins keep working from cache but stop updating.
          </p>
        )}
      </div>
    )
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0"
        onEscapeKeyDown={(e) => {
          if (locked) e.preventDefault()
        }}
        onInteractOutside={(e) => {
          if (locked) e.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle>Plugin access</DialogTitle>
          <DialogDescription className="break-words">
            {marketplace.name}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <p id="change-access-mode-label" className="sr-only">
            Plugin access
          </p>
          <MarketplaceAccessModeField
            value={chosen}
            onChange={setChosen}
            currentMode={current}
            labelledBy="change-access-mode-label"
            disabled={locked}
          />
          {impact()}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            disabled={locked}
            onClick={() => handleOpenChange(false)}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={locked}
            disabled={!changing || !preview.data || preview.isFetching}
            onClick={() => apply.mutate()}
          >
            {locked
              ? "Switching…"
              : `Switch to ${lowerFirst(accessModeCopy(target).label)}`}
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
