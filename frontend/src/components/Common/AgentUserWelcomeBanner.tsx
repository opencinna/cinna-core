/**
 * AgentUserWelcomeBanner — first-login notice for agent-user accounts.
 *
 * Phase 3 of the Agent Bundles & Installs plan.  Surfaces a one-time
 * dismissible banner explaining that the user is in "user" mode and
 * cannot build agents.  Persists the dismissal in ``localStorage``
 * keyed per user so it never re-appears.
 *
 * Hidden for developers / admins (their tier already supports
 * everything).  Hidden until the current-user query has resolved.
 */
import { useState, useEffect } from "react"
import { Sparkles, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import useAuth from "@/hooks/useAuth"
import useRole from "@/hooks/useRole"

const STORAGE_KEY_PREFIX = "agent-user-welcome-dismissed-"

export default function AgentUserWelcomeBanner() {
  const { user } = useAuth()
  const { isAgentUser } = useRole()
  const [dismissed, setDismissed] = useState(true)

  useEffect(() => {
    if (!user || !("id" in user)) return
    const key = `${STORAGE_KEY_PREFIX}${user.id}`
    setDismissed(localStorage.getItem(key) === "1")
  }, [user])

  if (!isAgentUser || dismissed) return null

  const handleDismiss = () => {
    if (user && "id" in user) {
      localStorage.setItem(`${STORAGE_KEY_PREFIX}${user.id}`, "1")
    }
    setDismissed(true)
  }

  return (
    <div className="border-b border-violet-200 bg-violet-50 dark:border-violet-900/60 dark:bg-violet-950/40">
      <div className="mx-auto flex max-w-7xl items-start gap-3 px-4 py-3 md:px-8">
        <Sparkles className="h-5 w-5 shrink-0 text-violet-600 dark:text-violet-300" />
        <div className="flex-1 text-sm text-violet-900 dark:text-violet-100">
          <p className="font-medium">Welcome — your account is in Agent User mode.</p>
          <p className="mt-1 text-violet-800/90 dark:text-violet-200/90">
            You can install agents from the catalog and chat with them.
            To build or publish your own agents, ask an admin to enable
            Developer mode for your account.
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0 text-violet-700 hover:bg-violet-100 dark:text-violet-200 dark:hover:bg-violet-900/40"
          onClick={handleDismiss}
          aria-label="Dismiss welcome banner"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
