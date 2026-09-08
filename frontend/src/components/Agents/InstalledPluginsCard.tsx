import { Puzzle } from "lucide-react"
import { useMemo, useState } from "react"

import type { AgentPluginLinkWithUpdateInfo } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AllInstalledPluginsSheet } from "./AllInstalledPluginsSheet"
import { InstalledPluginRow, type SyncReporter } from "./InstalledPluginRow"

interface InstalledPluginsCardProps {
  agentId: string
  plugins: AgentPluginLinkWithUpdateInfo[]
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  onSyncResult: SyncReporter
}

/**
 * "Which plugins and catalog skills does this agent carry, are they on, and is
 * there an update?" — S11.
 *
 * The A10 rebuild of what used to be a `<Table>` of rows carrying three
 * `Switch`es, four `Badge`s and an unconfirmed Uninstall. It is a P5 preview
 * list now: five rows and a "Show all (N)" Sheet, house rows, one interaction
 * model (the segmented toggle auto-saves; everything else is in the `⋯` menu or
 * its confirm).
 *
 * Full tab width, like its "Available plugins" sibling: the Plugins tab is a
 * single-column `space-y-6` stack, not a card grid, so A8 — which is about a
 * card spanning two columns of a grid — does not apply.
 */
export function InstalledPluginsCard({
  agentId,
  plugins,
  isLoading,
  isError,
  error,
  onRetry,
  onSyncResult,
}: InstalledPluginsCardProps) {
  const [sheetOpen, setSheetOpen] = useState(false)

  // Attention first: a plugin with an update waiting, then the disabled ones
  // last, then by name. The card previews five rows, so the row that needs a
  // decision has to be one of them.
  const sorted = useMemo(() => {
    const rank = (plugin: AgentPluginLinkWithUpdateInfo) =>
      plugin.disabled ? 2 : plugin.has_update ? 0 : 1
    return [...plugins].sort((a, b) => {
      const byRank = rank(a) - rank(b)
      if (byRank !== 0) return byRank
      return (a.plugin_name ?? "").localeCompare(b.plugin_name ?? "")
    })
  }, [plugins])

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <Puzzle className="h-5 w-5 shrink-0" />
          Installed plugins
        </CardTitle>
        <CardDescription>
          Plugins and catalog skills this agent carries. Each one can be enabled
          for conversation mode, building mode, or both.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <PreviewList
          items={sorted}
          getKey={(plugin) => plugin.id}
          renderItem={(plugin) => (
            <InstalledPluginRow
              agentId={agentId}
              plugin={plugin}
              onSyncResult={onSyncResult}
            />
          )}
          isLoading={isLoading}
          isError={isError}
          error={error}
          onRetry={onRetry}
          errorFallback="Couldn't load the installed plugins"
          empty={
            <p className="text-sm text-muted-foreground">
              No plugins installed yet — browse the available plugins below.
            </p>
          }
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <AllInstalledPluginsSheet
        agentId={agentId}
        plugins={sorted}
        onSyncResult={onSyncResult}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
    </Card>
  )
}
