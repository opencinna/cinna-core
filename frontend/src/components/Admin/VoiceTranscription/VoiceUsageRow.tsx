import { AlertTriangle } from "lucide-react"

import type { VoiceTranscriptionUsageRow } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { formatMinutes, formatSessions } from "./voiceTranscription"

interface VoiceUsageRowProps {
  row: VoiceTranscriptionUsageRow
  /** The saved monthly limit in minutes; `null` is no limit. */
  limitMinutes: number | null
}

/**
 * One person's dictation this month — a read-only row shared by the usage card
 * and its Sheet, so the two hosts cannot drift.
 *
 * No status dot (there is no on/off state), no actions; the row's minutes sit in
 * the action slot, right-aligned, because it is the fact the list is sorted by.
 */
export function VoiceUsageRow({ row, limitMinutes }: VoiceUsageRowProps) {
  const reachedLimit = limitMinutes != null && row.seconds >= limitMinutes * 60

  return (
    <ListRow
      title={row.full_name || row.email}
      flags={
        <>
          {reachedLimit && (
            <RowFlag
              icon={AlertTriangle}
              tone="warning"
              label="Reached the monthly limit"
            />
          )}
          <RowInfo
            facts={[
              row.full_name ? row.email : null,
              formatSessions(row.sessions),
            ]}
          />
        </>
      }
    >
      {/* Minutes only: the fact the list is sorted by. Sessions live in the
          detail flag so the name keeps its width at 1024. */}
      <span className="text-sm tabular-nums text-muted-foreground">
        {formatMinutes(row.seconds)}
      </span>
    </ListRow>
  )
}
