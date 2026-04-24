/**
 * LocalDevSyncStatus — small sync-state indicator for CLI token rows.
 *
 * Displays a colored dot and a relative timestamp based on last_sync_connected_at.
 * "Synced" (green) when the last sync was within 5 minutes.
 * "Idle" (gray) otherwise. "Never synced" when null.
 */

interface LocalDevSyncStatusProps {
  lastSyncConnectedAt: string | null | undefined
}

function formatSyncTime(dateStr: string): string {
  const diffMs = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
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
    Date.now() - new Date(lastSyncConnectedAt).getTime() < SYNC_ACTIVE_THRESHOLD_MS

  return (
    <span className="flex items-center gap-1 text-xs text-muted-foreground">
      <span
        className={`inline-block h-1.5 w-1.5 rounded-full ${
          isRecentlySynced ? "bg-green-500" : "bg-muted-foreground/40"
        }`}
      />
      {isRecentlySynced
        ? `Synced ${formatSyncTime(lastSyncConnectedAt)}`
        : `Idle ${formatSyncTime(lastSyncConnectedAt)}`}
    </span>
  )
}
