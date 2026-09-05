import { createFileRoute } from "@tanstack/react-router"

import { DesktopLandingPage } from "@/components/Desktop/DesktopLandingPage"
import { APP_NAME } from "@/utils"

/**
 * Public — deliberately no `beforeLoad`/`ensureSessionValid`, unlike the
 * sibling `desktop-auth/consent` route. This is the link in the new-account
 * email, so the visitor has not signed in anywhere yet; bouncing them to
 * /login would put a login wall in front of a download button.
 */
export const Route = createFileRoute("/desktop")({
  component: DesktopLandingRoute,
  head: () => ({
    meta: [{ title: `Get Cinna Desktop - ${APP_NAME}` }],
  }),
})

function DesktopLandingRoute() {
  return <DesktopLandingPage />
}
