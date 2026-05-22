import { createFileRoute } from "@tanstack/react-router"

import { TwoFactorChallenge } from "@/components/Auth/TwoFactorChallenge"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/login/mfa")({
  component: MfaChallengePage,
  head: () => ({
    meta: [
      {
        title: `Two-factor authentication - ${APP_NAME}`,
      },
    ],
  }),
})

function MfaChallengePage() {
  return (
    <AuthLayout>
      <TwoFactorChallenge />
    </AuthLayout>
  )
}
