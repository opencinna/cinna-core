/**
 * AppDataRow — Single AppDataVolume entry in the App Data tab.
 *
 * Visible columns: bundle_id (monospace + copy), human-readable size,
 * linked install name (or "orphaned"), last activity. Per-row actions:
 * Refresh size + Wipe (only on orphaned volumes).
 */
import { Copy, RefreshCw, Trash2 } from "lucide-react"
import { formatDistanceToNow } from "date-fns"

import type { AppDataVolumePublic } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"


function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}


function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "Never"
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true })
  } catch {
    return "Unknown"
  }
}


interface AppDataRowProps {
  volume: AppDataVolumePublic
  onRecompute: () => void
  onWipe: () => void
  isRecomputing: boolean
  isWiping: boolean
}


export function AppDataRow({
  volume,
  onRecompute,
  onWipe,
  isRecomputing,
  isWiping,
}: AppDataRowProps) {
  const { showSuccessToast } = useCustomToast()

  const copyBundleId = () => {
    navigator.clipboard.writeText(volume.bundle_id)
    showSuccessToast("Bundle ID copied to clipboard")
  }

  return (
    <li className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        {/* Bundle ID + copy + orphaned badge */}
        <div className="flex items-center gap-2 flex-wrap">
          <code className="font-mono text-xs bg-muted px-2 py-0.5 rounded">
            {volume.bundle_id}
          </code>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={copyBundleId}
            aria-label="Copy bundle id"
          >
            <Copy className="h-3 w-3" />
          </Button>
          {volume.is_orphaned ? (
            <Badge variant="outline" className="text-xs text-muted-foreground">
              orphaned
            </Badge>
          ) : volume.current_install_name ? (
            <Badge variant="secondary" className="text-xs">
              {volume.current_install_name}
            </Badge>
          ) : null}
        </div>

        {/* Size + last check + last update */}
        <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
          <span>{formatBytes(volume.size_bytes)}</span>
          <span>•</span>
          <span title={volume.last_size_check_at ?? undefined}>
            size checked {formatRelative(volume.last_size_check_at)}
          </span>
          <span>•</span>
          <span title={volume.updated_at}>
            updated {formatRelative(volume.updated_at)}
          </span>
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 shrink-0">
        <Button
          variant="outline"
          size="sm"
          onClick={onRecompute}
          disabled={isRecomputing}
          aria-label="Refresh size"
        >
          <RefreshCw
            className={`h-4 w-4 mr-1 ${isRecomputing ? "animate-spin" : ""}`}
          />
          Refresh
        </Button>

        {volume.is_orphaned ? (
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                variant="destructive"
                size="sm"
                disabled={isWiping}
                aria-label="Wipe volume"
              >
                <Trash2 className="h-4 w-4 mr-1" />
                Wipe
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Wipe app data?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will permanently delete all data stored under{" "}
                  <code className="font-mono">{volume.bundle_id}</code>. This
                  cannot be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  onClick={onWipe}
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                >
                  Wipe permanently
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        ) : (
          <Button
            variant="destructive"
            size="sm"
            disabled
            aria-label="Wipe volume (uninstall agent first)"
            title="Uninstall the agent first to wipe this volume."
          >
            <Trash2 className="h-4 w-4 mr-1" />
            Wipe
          </Button>
        )}
      </div>
    </li>
  )
}
