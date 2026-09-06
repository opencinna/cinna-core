import { createFileRoute, Link as RouterLink } from "@tanstack/react-router"
import { Monitor } from "lucide-react"
import { z } from "zod"

import { AuthLayout } from "@/components/Common/AuthLayout"
import { Button } from "@/components/ui/button"
import { ensureSessionValid } from "@/hooks/useAuth"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  // Carried over from the accept page rather than re-read from the policy:
  // whether desktop was offered is a property of *this invitation*, not of the
  // instance, and the invitation is no longer reachable once its token is
  // spent.
  desktop: z.boolean().catch(false),
})

export const Route = createFileRoute("/accept-invite/done")({
  component: AcceptInviteDone,
  validateSearch: searchSchema,
  // The person is signed in by the time they land here, but this page lives
  // outside the `_layout` guard — so it validates the token it was just handed
  // the same way the desktop consent page does, and bounces back here after a
  // re-login rather than dropping them on the dashboard.
  beforeLoad: async ({ search }) => {
    await ensureSessionValid(`/accept-invite/done?desktop=${search.desktop}`)
  },
  head: () => ({
    meta: [{ title: `Welcome - ${APP_NAME}` }],
  }),
})

function AcceptInviteDone() {
  const { desktop } = Route.useSearch()

  return (
    <AuthLayout>
      <div className="flex flex-col gap-6">
        <div className="flex flex-col items-center gap-2 text-center">
          <h1 className="text-2xl font-bold">You're in.</h1>
          <p className="text-sm text-muted-foreground">
            Your {APP_NAME} account is ready.
          </p>
        </div>

        <Button className="w-full" asChild>
          <RouterLink to="/">Continue in browser</RouterLink>
        </Button>

        {desktop && (
          <div className="flex flex-col gap-2 border-t pt-6 text-center">
            <p className="text-sm text-muted-foreground">
              You were also invited to Cinna Desktop — the same account, on your
              machine.
            </p>
            <Button variant="outline" className="w-full" asChild>
              <RouterLink to="/desktop">
                <Monitor className="mr-2 size-4" />
                Get Cinna Desktop
              </RouterLink>
            </Button>
          </div>
        )}
      </div>
    </AuthLayout>
  )
}
