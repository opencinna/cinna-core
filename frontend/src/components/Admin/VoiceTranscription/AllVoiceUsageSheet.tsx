import type { VoiceTranscriptionUsageReport } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { VoiceUsageRow } from "./VoiceUsageRow"
import { formatMinutes, monthLabel } from "./voiceTranscription"

interface AllVoiceUsageSheetProps {
  report: VoiceTranscriptionUsageReport
  limitMinutes: number | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The "Show all (N)" destination for the Voice usage card.
 *
 * A Sheet rather than a route: the list needs no search, sort or paging and a
 * usage row has no lifecycle. Same row component as the card.
 */
export function AllVoiceUsageSheet({
  report,
  limitMinutes,
  open,
  onOpenChange,
}: AllVoiceUsageSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Voice usage</SheetTitle>
          <SheetDescription>
            {monthLabel(report.period_start)} ·{" "}
            {formatMinutes(report.total_seconds)}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pr-2 pb-4">
          <ListRowGroup>
            {report.rows.map((row) => (
              <VoiceUsageRow
                key={row.user_id}
                row={row}
                limitMinutes={limitMinutes}
              />
            ))}
          </ListRowGroup>
        </div>
      </SheetContent>
    </Sheet>
  )
}
