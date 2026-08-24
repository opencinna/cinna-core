import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { ArrowLeft } from "lucide-react"
import { useEffect } from "react"

import { AutoRoutingTuningCard } from "@/components/Admin/ServerChannels/AutoRoutingTuningCard"
import { Button } from "@/components/ui/button"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/routing-tuning")({
  component: AdminRoutingTuning,
  head: () => ({
    meta: [
      {
        title: `Auto Routing Tuning - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as any)?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminRoutingTuning() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  useEffect(() => {
    setHeaderContent(
      // This page is reached only from the Server Debug Tools card and is
      // deliberately absent from the admin menu, so the way back has to be on
      // the page itself. `hash` lands on the tab that card lives on.
      <div className="flex items-center gap-3 min-w-0">
        <Button variant="ghost" size="sm" asChild className="shrink-0">
          <Link
            to="/admin/server-configuration"
            hash="channels"
            aria-label="Back to server configuration"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
        </Button>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">
            Auto Routing Tuning
          </h1>
          {/* Deliberately not a restatement of the card's own description
              below it — that one carries the read-only caveat. */}
          <p className="text-xs text-muted-foreground">Server debug tools</p>
        </div>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <AutoRoutingTuningCard />
      </div>
    </div>
  )
}
