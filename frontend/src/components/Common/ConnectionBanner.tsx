import { useEffect, useRef, useState } from "react"
import { AlertTriangle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useConnectionStatus } from "@/hooks/useEventBus"
import { eventService } from "@/services/eventService"

/**
 * Degraded-mode banner shown when the realtime WebSocket is disconnected.
 *
 * Mounted inside the authenticated layout (so it never appears on login/signup),
 * it floats centered-top below the header as an overlay (does not push page
 * content). To avoid flashing during transient blips, it waits ~3s of continuous
 * "disconnected" status before appearing, and hides immediately on reconnect.
 * While visible it nudges eventService.ensureConnected() on an interval as a
 * recovery backstop (the socket also auto-retries forever on its own).
 */
const SHOW_DELAY_MS = 3000
const RECOVERY_NUDGE_INTERVAL_MS = 12000

export function ConnectionBanner() {
  const status = useConnectionStatus()
  const [visible, setVisible] = useState(false)
  const showTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced visibility: only show after the connection has been down for a
  // short grace period; hide immediately when not disconnected.
  useEffect(() => {
    if (status === "disconnected") {
      if (!showTimerRef.current && !visible) {
        showTimerRef.current = setTimeout(() => {
          setVisible(true)
          showTimerRef.current = null
        }, SHOW_DELAY_MS)
      }
    } else {
      if (showTimerRef.current) {
        clearTimeout(showTimerRef.current)
        showTimerRef.current = null
      }
      setVisible(false)
    }

    return () => {
      if (showTimerRef.current) {
        clearTimeout(showTimerRef.current)
        showTimerRef.current = null
      }
    }
  }, [status, visible])

  // While the banner is visible, periodically nudge a reconnect attempt.
  useEffect(() => {
    if (!visible) return
    eventService.ensureConnected()
    const interval = setInterval(() => {
      eventService.ensureConnected()
    }, RECOVERY_NUDGE_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [visible])

  if (!visible) return null

  return (
    <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex justify-center px-4 pt-2">
      <div className="pointer-events-auto flex items-center gap-3 rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 shadow-md dark:border-amber-700 dark:bg-amber-950/80 dark:text-amber-200">
        <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500" />
        <span>Connection to the server lost — live updates are paused.</span>
        <Button
          variant="outline"
          size="sm"
          className="h-7 border-amber-400 bg-transparent text-amber-900 hover:bg-amber-100 dark:border-amber-600 dark:text-amber-100 dark:hover:bg-amber-900/40"
          onClick={() => window.location.reload()}
        >
          Refresh page
        </Button>
      </div>
    </div>
  )
}
