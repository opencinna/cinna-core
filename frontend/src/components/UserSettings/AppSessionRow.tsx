import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import {
  Apple,
  Chrome,
  Laptop,
  Monitor,
  Smartphone,
  Unplug,
} from "lucide-react"

import type { DesktopOAuthClientPublic } from "@/client"
import { DesktopAuthService } from "@/client"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { APP_SESSIONS_QUERY_KEY } from "@/utils/appSessions"

function PlatformIcon({ platform }: { platform?: string | null }) {
  const cls = "h-3.5 w-3.5 text-muted-foreground"
  switch (platform?.toLowerCase()) {
    case "macos":
    case "darwin":
      return <Apple className={cls} />
    case "windows":
    case "win32":
      return <Monitor className={cls} />
    case "linux":
      return <Chrome className={cls} />
    case "ios":
    case "android":
      return <Smartphone className={cls} />
    default:
      return <Laptop className={cls} />
  }
}

/** Relative time, or the carried-over "Unknown" when the date will not parse. */
function relativeTime(iso: string): string | null {
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true })
  } catch {
    return null
  }
}

/**
 * The row's metadata line: **at most two** facts joined by ` · ` (P3).
 *
 * The version is a fact, not a state, so it lives here rather than in a badge.
 * "Last used" and "Connected" are alternatives, never both: a session that has
 * never been used has no last-use time to print, and the bare "Never" the old
 * card showed reads as a fault rather than as "connected, not yet run".
 */
function metadataLine(client: DesktopOAuthClientPublic): string {
  const facts: string[] = []
  if (client.app_version) facts.push(`v${client.app_version}`)

  if (client.last_used_at) {
    const rel = relativeTime(client.last_used_at)
    facts.push(rel ? `Last used ${rel}` : "Last used at an unknown time")
  } else {
    const rel = relativeTime(client.created_at)
    facts.push(rel ? `Connected ${rel}` : "Connected at an unknown time")
  }

  return facts.join(" · ")
}

interface AppSessionRowProps {
  client: DesktopOAuthClientPublic
}

/**
 * One connected app, as a compact P3 row.
 *
 * Rendered by both the card and the "Show all" Sheet so the two hosts cannot
 * drift. The row's only action *is* its purpose — disconnect this app — and it
 * is destructive, so §2 "Row actions" allows it inline and asks for it to be
 * hover-revealed rather than buried in a `⋯` menu holding a single item.
 *
 * The confirm is owned by the row, not by the host: that is what lets the Sheet
 * stay open behind it and keeps card and Sheet on one code path.
 */
export function AppSessionRow({ client }: AppSessionRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const revokeMutation = useMutation({
    mutationFn: () =>
      DesktopAuthService.revokeDesktopClient({ clientId: client.client_id }),
    onSuccess: () => {
      showSuccessToast("App disconnected.")
      setConfirmOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    // On both paths: the likeliest failure is a row that is already gone —
    // revoked from another tab or by the device itself — which 404s, and
    // refetching is what clears it instead of leaving a dead row that can
    // only ever 404 again.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: APP_SESSIONS_QUERY_KEY })
    },
  })

  return (
    <div className="group flex items-center justify-between px-3 py-2 border rounded-lg">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md shrink-0 flex items-center justify-center bg-muted">
            <PlatformIcon platform={client.platform} />
          </div>
          <span className="text-sm font-medium truncate min-w-0">
            {client.device_name}
          </span>
          {/* Only the CLI-exchanged origin is badged: a browser consent is the
              ordinary way to connect an app, and a badge on every row would
              bury the one that matters. */}
          {client.origin === "cli_exchange" && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="secondary" className="text-xs shrink-0">
                  CLI link
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Linked from a CLI account token on this machine, without a
                browser sign-in.
              </TooltipContent>
            </Tooltip>
          )}
        </div>
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {metadataLine(client)}
        </p>
      </div>

      {/* Hover-revealed (P3 variant): this is a View surface, and a destructive
          control that is visible on every row invites the accident. */}
      <div className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-destructive"
              disabled={revokeMutation.isPending}
              aria-label={`Disconnect ${client.device_name}`}
              onClick={() => setConfirmOpen(true)}
            >
              <Unplug className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Disconnect
          </TooltipContent>
        </Tooltip>
      </div>

      <AlertDialog
        open={confirmOpen}
        // Escape would otherwise close the confirm mid-request, leaving the
        // pending state with nowhere to live.
        onOpenChange={(next) => {
          if (!revokeMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect app</AlertDialogTitle>
            <AlertDialogDescription>
              Revoke access for <strong>{client.device_name}</strong>? The app
              will need to sign in again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Keep the confirm open while the request is in flight so the
                // pending state has somewhere to live.
                e.preventDefault()
                revokeMutation.mutate()
              }}
              disabled={revokeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {revokeMutation.isPending ? "Disconnecting…" : "Disconnect"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
