import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { ensureSessionValid } from "@/hooks/useAuth"
import { DeviceLoginConsentPage } from "@/components/Auth/DeviceLoginConsentPage"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  code: z.string().optional(),
})

export const Route = createFileRoute("/device")({
  component: DeviceConsentRoute,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    // Preserve the user_code through the login bounce so the user lands back
    // on this consent screen with the code still prefilled.
    const returnTo = search.code
      ? `/device?code=${encodeURIComponent(search.code)}`
      : "/device"
    await ensureSessionValid(returnTo)
  },
  head: () => ({
    meta: [{ title: `Authorize Device - ${APP_NAME}` }],
  }),
})

function DeviceConsentRoute() {
  const { code } = Route.useSearch()
  return <DeviceLoginConsentPage code={code} />
}
