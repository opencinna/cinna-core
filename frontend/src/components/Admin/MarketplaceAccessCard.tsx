import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ListChecks, Pencil, ShieldCheck } from "lucide-react"
import { useState } from "react"

import { type LLMPluginMarketplacePublic, LlmPluginsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { accessModeCopy, marketplaceAccessMode } from "@/utils/marketplace"
import { ChangeAccessModeDialog } from "./ChangeAccessModeDialog"

/**
 * S10 — who can browse this marketplace (the Public switch, auto-saving) and
 * which of its entries they see (one summary row; the change opens a dialog
 * because it must show its impact first).
 */
export function MarketplaceAccessCard({
  marketplace,
}: {
  marketplace: LLMPluginMarketplacePublic
}) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const [changing, setChanging] = useState(false)
  const mode = accessModeCopy(marketplaceAccessMode(marketplace.access_mode))

  const visibility = useMutation({
    mutationFn: (public_discovery: boolean) =>
      LlmPluginsService.updateMarketplace({
        marketplaceId: marketplace.id,
        requestBody: { public_discovery },
      }),
    onSuccess: () => showSuccessToast("Marketplace visibility updated"),
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "Failed to update marketplace")),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: ["marketplace", marketplace.id],
      })
      void queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <ShieldCheck className="h-5 w-5" />
          Access
        </CardTitle>
        <CardDescription>
          Who can browse this marketplace and which of its entries they see.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <Label htmlFor="public_discovery" className="text-sm font-medium">
              Public
            </Label>
            <p className="text-xs text-muted-foreground">
              Every user can browse this marketplace; when off, only you can.
            </p>
          </div>
          <div className="shrink-0">
            <Switch
              id="public_discovery"
              checked={marketplace.public_discovery}
              onCheckedChange={(checked) => visibility.mutate(checked)}
              disabled={visibility.isPending}
            />
          </div>
        </div>

        <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
          <div className="flex min-w-0 items-start gap-3">
            <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10">
              <ListChecks className="h-3.5 w-3.5 text-primary" />
            </div>
            <div className="min-w-0">
              <p className="mb-0.5 text-xs text-muted-foreground">
                Plugin access
              </p>
              <p className="text-sm font-medium">{mode.label}</p>
              <p className="text-xs text-muted-foreground">{mode.help}</p>
            </div>
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0"
                aria-label="Change plugin access"
                onClick={() => setChanging(true)}
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Change plugin access
            </TooltipContent>
          </Tooltip>
        </div>
      </CardContent>

      {changing && (
        <ChangeAccessModeDialog
          marketplace={marketplace}
          open
          onOpenChange={setChanging}
        />
      )}
    </Card>
  )
}
