import { createFileRoute } from "@tanstack/react-router"
import { z } from "zod"
import { ensureSessionValid } from "@/hooks/useAuth"
import { AppAuthService } from "@/client"
import { NativeAuthConsentPage } from "@/components/Auth/NativeAuthConsentPage"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  request: z.string(),
})

export const Route = createFileRoute("/app-auth/consent")({
  component: AppAuthConsentPage,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    const returnTo = `/app-auth/consent?request=${encodeURIComponent(search.request)}`
    await ensureSessionValid(returnTo)
  },
  head: () => ({
    meta: [{ title: `Authorize App - ${APP_NAME}` }],
  }),
})

function AppAuthConsentPage() {
  const { request: nonce } = Route.useSearch()
  return (
    <NativeAuthConsentPage
      nonce={nonce}
      queryKeyPrefix="app-auth-request"
      getRequest={(nonce) => AppAuthService.getAppAuthRequest({ nonce })}
      submitConsent={(nonce, action) =>
        AppAuthService.appConsent({
          requestBody: { request_nonce: nonce, action },
        })
      }
    />
  )
}
