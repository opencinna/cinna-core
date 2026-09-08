import type { AgentPluginLinkWithUpdateInfo } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { InstalledPluginRow, type SyncReporter } from "./InstalledPluginRow"

interface AllInstalledPluginsSheetProps {
  agentId: string
  /** Every installed plugin, in the order the card previews them. */
  plugins: AgentPluginLinkWithUpdateInfo[]
  onSyncResult: SyncReporter
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Installed plugins card.
 *
 * A Sheet rather than a route (P5's route-vs-Sheet test): an agent's installed
 * set is small enough to need no search, sort or pagination, and a plugin link
 * has no lifecycle of its own beyond the actions its row already carries. Same
 * row component as the card, so the two hosts cannot drift.
 */
export function AllInstalledPluginsSheet({
  agentId,
  plugins,
  onSyncResult,
  open,
  onOpenChange,
}: AllInstalledPluginsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Installed plugins</SheetTitle>
          <SheetDescription>
            {plugins.length} plugin{plugins.length === 1 ? "" : "s"} installed
            for this agent
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          <ListRowGroup>
            {plugins.map((plugin) => (
              <InstalledPluginRow
                key={plugin.id}
                agentId={agentId}
                plugin={plugin}
                onSyncResult={onSyncResult}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
