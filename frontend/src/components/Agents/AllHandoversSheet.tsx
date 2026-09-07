import type { HandoverConfigPublic } from "@/client"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { HandoverRow } from "./HandoverRow"

interface AllHandoversSheetProps {
  /** The agent that hands work over (the card's own agent). */
  agentId: string
  /** Every handover, already sorted the way the card sorts them. */
  handovers: HandoverConfigPublic[]
  /**
   * The list's N, read from the same `count` the card's "Show all (N)" uses,
   * so the trigger and the header it opens always print the same number.
   */
  totalCount: number
  /** Target agent id → colour preset, used to tint each row's icon tile. */
  colorPresetByAgentId: Record<string, string | null | undefined>
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Handover card.
 *
 * A Sheet rather than a route: handovers have no route of their own, the full
 * list needs no search, sort or pagination, and the entity has no lifecycle
 * beyond the row's own menu. The rows are the card's rows — same component,
 * same actions — so the two hosts cannot drift.
 */
export function AllHandoversSheet({
  agentId,
  handovers,
  totalCount,
  colorPresetByAgentId,
  open,
  onOpenChange,
}: AllHandoversSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Handover to Agents</SheetTitle>
          <SheetDescription>{totalCount} handovers</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto space-y-1.5 px-4 pb-4">
          {handovers.length === 0 ? (
            // Reachable without closing the Sheet: the rows carry Delete.
            <p className="text-sm text-muted-foreground">
              This agent doesn&apos;t hand work to any other agent yet.
            </p>
          ) : (
            handovers.map((handover) => (
              <HandoverRow
                key={handover.id}
                agentId={agentId}
                handover={handover}
                targetColorPreset={
                  colorPresetByAgentId[handover.target_agent_id]
                }
              />
            ))
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
