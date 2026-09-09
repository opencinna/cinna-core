import type { AddonPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { AddonRow, type SyncReporter } from "./AddonRow"

interface AllAddonsSheetProps {
  agentId: string
  /** Every addon, in the order the server delivered them. */
  addons: AddonPublic[]
  canAdd: boolean
  onSyncResult: SyncReporter
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Plugins and skills card.
 *
 * A Sheet rather than a route (P5's route-vs-Sheet test): the full list needs
 * no search, sort or pagination — the server already sorts it attention-first
 * over what is a handful of rows — and an addon has no lifecycle of its own.
 * Its lifecycle is the plugin link's or the skill package's, both of which
 * have pages already. A filter here is exactly what would turn this into a
 * route, which is why there is none.
 *
 * Same row component as the card, so the two hosts cannot drift.
 */
export function AllAddonsSheet({
  agentId,
  addons,
  canAdd,
  onSyncResult,
  open,
  onOpenChange,
}: AllAddonsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Plugins and skills</SheetTitle>
          <SheetDescription>
            {addons.length} in total, most urgent first
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {addons.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              This agent carries no plugins or skills.
            </p>
          ) : (
            <ListRowGroup>
              {addons.map((addon) => (
                <AddonRow
                  key={addon.key}
                  agentId={agentId}
                  addon={addon}
                  canAdd={canAdd}
                  onSyncResult={onSyncResult}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
