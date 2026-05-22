import { Link } from "@tanstack/react-router"
import { ShieldCheck, X } from "lucide-react"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import useAuth from "@/hooks/useAuth"

const DISMISSED_KEY = "mfa.enable_banner.dismissed"

/**
 * One-time dismissable banner shown on the dashboard nudging users who
 * have NOT enrolled 2FA to head over to Settings → Security.
 *
 * Dismissal is stored in `localStorage` keyed per-user (so a fresh
 * sign-in on a new device still gets the prompt). We mirror the
 * dashboard's existing pattern of localStorage-backed onboarding flags
 * (`onboardingSkipped`) — there is no `userdashboard.dismissed_hints`
 * surface in the codebase yet despite the plan's reference, so we use
 * the lighter mechanism instead.
 */
export function EnableTwoFactorBanner() {
  const { user } = useAuth()
  const [dismissed, setDismissed] = useState(true)

  useEffect(() => {
    if (!user?.id) {
      setDismissed(true)
      return
    }
    const stored = localStorage.getItem(`${DISMISSED_KEY}.${user.id}`)
    setDismissed(stored === "true")
  }, [user?.id])

  if (!user) return null
  if (user.two_factor_enabled) return null
  if (dismissed) return null

  const handleDismiss = () => {
    if (user?.id) {
      localStorage.setItem(`${DISMISSED_KEY}.${user.id}`, "true")
    }
    setDismissed(true)
  }

  return (
    <div className="mb-4 flex items-start gap-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 dark:border-blue-900/40 dark:bg-blue-950/30">
      <ShieldCheck className="h-5 w-5 text-blue-500 shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium">
          Protect your account with two-factor authentication
        </p>
        <p className="text-xs text-muted-foreground mt-0.5">
          Add a passkey or set up an authenticator app for a second sign-in step
          that blocks stolen-password attacks.
        </p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Button asChild size="sm">
          <Link to="/settings" hash="security">
            Set up
          </Link>
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={handleDismiss}
          aria-label="Dismiss two-factor authentication banner"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
