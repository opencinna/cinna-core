/**
 * DesktopSessionsCard — Settings > Security tab
 *
 * Shows all native app clients (Cinna Desktop and Cinna Mobile) connected to
 * the user's account and allows disconnecting (revoking) individual clients.
 * Both client kinds share the same `desktop_oauth_client` backing table, so
 * this single card lists them together.  Follows the same card layout as other
 * Settings sections.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Monitor, Apple, Chrome, Laptop, Smartphone, Unplug } from "lucide-react"
import { formatDistanceToNow } from "date-fns"

import { DesktopAuthService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
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

// ── Platform icon helper ───────────────────────────────────────────────────

function PlatformIcon({ platform }: { platform?: string | null }) {
  const cls = "h-4 w-4 text-muted-foreground"
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

// ── Relative time helper ───────────────────────────────────────────────────

function RelativeTime({ iso }: { iso?: string | null }) {
  if (!iso) return <span className="text-muted-foreground text-xs">Never</span>
  try {
    return (
      <span className="text-muted-foreground text-xs">
        {formatDistanceToNow(new Date(iso), { addSuffix: true })}
      </span>
    )
  } catch {
    return <span className="text-muted-foreground text-xs">Unknown</span>
  }
}

// ── Main component ─────────────────────────────────────────────────────────

export function DesktopSessionsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: clients = [], isLoading } = useQuery({
    queryKey: ["desktop-clients"],
    queryFn: () => DesktopAuthService.listDesktopClients(),
  })

  const revokeMutation = useMutation({
    mutationFn: (clientId: string) =>
      DesktopAuthService.revokeDesktopClient({ clientId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["desktop-clients"] })
      showSuccessToast("App disconnected.")
    },
    onError: () => {
      showErrorToast("Failed to disconnect app.")
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>App Sessions</CardTitle>
        <CardDescription>
          Manage Cinna Desktop and Cinna Mobile app connections to this account.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : clients.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-6 text-center">
            <Laptop className="h-8 w-8 text-muted-foreground/50" />
            <p className="text-sm text-muted-foreground">
              No apps connected.
            </p>
            <p className="text-xs text-muted-foreground">
              Download Cinna Desktop or Cinna Mobile to get started.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {clients.map((c) => (
              <li
                key={c.client_id}
                className="flex items-center justify-between gap-3 py-3"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <PlatformIcon platform={c.platform} />
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">
                      {c.device_name}
                    </p>
                    <div className="flex items-center gap-2 flex-wrap">
                      {c.app_version && (
                        <Badge variant="outline" className="text-xs px-1 py-0">
                          v{c.app_version}
                        </Badge>
                      )}
                      <RelativeTime iso={c.last_used_at ?? null} />
                    </div>
                  </div>
                </div>

                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="shrink-0 text-muted-foreground hover:text-destructive"
                      title="Disconnect"
                      aria-label={`Disconnect ${c.device_name}`}
                    >
                      <Unplug className="h-4 w-4" />
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Disconnect app?</AlertDialogTitle>
                      <AlertDialogDescription>
                        This will revoke access for{" "}
                        <strong>{c.device_name}</strong>. The app will need to
                        log in again.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction
                        onClick={() => revokeMutation.mutate(c.client_id)}
                        className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      >
                        Disconnect
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
