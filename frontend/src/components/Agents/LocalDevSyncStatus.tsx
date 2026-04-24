/**
 * LocalDevSyncStatus — small sync-state indicator for CLI token rows.
 *
 * Displays a colored dot and a relative timestamp based on last_sync_connected_at.
 * "Synced" (green) when the last sync was within 5 minutes.
 * "Idle" (gray) otherwise. "Never synced" when null.
 */

import { RelativeTime } from "@/components/Common/RelativeTime"

interface LocalDevSyncStatusProps {
  lastSyncConnectedAt: string | null | undefined
}

// Backend emits naive UTC timestamps; append 'Z' so the Date parser treats them as UTC.
function parseUtcTimestamp(dateStr: string): Date {
  return new Date(dateStr.endsWith("Z") ? dateStr : dateStr + "Z")
}

const SYNC_ACTIVE_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes

export function LocalDevSyncStatus({ lastSyncConnectedAt }: LocalDevSyncStatusProps) {
  if (!lastSyncConnectedAt) {
    return (
      <span className="flex items-center gap-1 text-xs text-muted-foreground">
        <span className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
        Never synced
      </span>
    )
  }

  const isRecentlySynced =
    Date.now() - parseUtcTimestamp(lastSyncConnectedAt).getTime() < SYNC_ACTIVE_THRESHOLD_MS

  return (
    <span className="flex items-center gap-1 text-xs text-muted-foreground">
      <span
        className={`inline-block h-1.5 w-1.5 rounded-full ${
          isRecentlySynced ? "bg-green-500" : "bg-muted-foreground/40"
        }`}
      />
      {isRecentlySynced ? "Synced " : "Idle "}
      <RelativeTime
        timestamp={lastSyncConnectedAt}
        className="text-xs text-muted-foreground"
        showTooltip
      />
    </span>
  )
}
