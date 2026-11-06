import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { DesktopAuthService } from "@/client"
import { NativeAuthConsentPage } from "@/components/Auth/NativeAuthConsentPage"
import { ensureSessionValid } from "@/hooks/useAuth"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  request: z.string(),
})

export const Route = createFileRoute("/desktop-auth/consent")({
  component: DesktopAuthConsentPage,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    const returnTo = `/desktop-auth/consent?request=${encodeURIComponent(search.request)}`
    await ensureSessionValid(returnTo)
  },
  head: () => ({
    meta: [{ title: `Authorize App - ${APP_NAME}` }],
  }),
})

function DesktopAuthConsentPage() {
  const { request: nonce } = Route.useSearch()
  return (
    <NativeAuthConsentPage
      nonce={nonce}
      queryKeyPrefix="desktop-auth-request"
      branded
      getRequest={(nonce) => DesktopAuthService.getAuthRequest({ nonce })}
      submitConsent={(nonce, action) =>
        DesktopAuthService.consent({
          requestBody: { request_nonce: nonce, action },
        })
      }
    />
  )
}
