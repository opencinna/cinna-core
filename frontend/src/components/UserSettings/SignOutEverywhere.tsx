import { useMutation } from "@tanstack/react-query"
import { LogOut } from "lucide-react"
import { useState } from "react"

import { LoginService } from "@/client"
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
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"
import { broadcastLogout, clearSession } from "@/lib/authSession"

/**
 * "Sign out everywhere": the trigger and its confirm (S4). The server ends
 * every session, this one included, and clears the refresh cookie, so on
 * success there is no logout call and no toast — a full load of /login is
 * the result, and no React Query cache of this identity survives it.
 */
export default function SignOutEverywhere() {
  const [open, setOpen] = useState(false)
  // Set on success: the dialog stays in its pending state until /login loads.
  const [leaving, setLeaving] = useState(false)
  const { showErrorToast } = useCustomToast()

  const mutation = useMutation({
    mutationFn: () => LoginService.sessionRevokeAll(),
    onSuccess: () => {
      setLeaving(true)
      clearSession()
      broadcastLogout()
      window.location.assign("/login")
    },
    onError: () => {
      showErrorToast("Couldn't sign out everywhere. Try again.")
    },
  })

  const busy = mutation.isPending || leaving

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <LogOut className="h-4 w-4" />
        Sign out everywhere
      </Button>
      <AlertDialog
        open={open}
        // Escape or an outside click would otherwise close the confirm
        // mid-request, leaving the pending state with nowhere to live.
        onOpenChange={(next) => {
          if (!busy) setOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Sign out everywhere?</AlertDialogTitle>
            <AlertDialogDescription>
              Every browser, including this one, and every Cinna Desktop and
              Cinna Mobile app on your account will be signed out, and your CLI,
              API and MCP tokens will stop working. The apps will ask you to
              sign in again, and devices where you chose "Don't ask on this
              device" will ask for your second factor again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Keep the confirm open while the request is in flight.
                e.preventDefault()
                mutation.mutate()
              }}
              disabled={busy}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {busy ? "Signing out…" : "Sign out everywhere"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
