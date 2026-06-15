import {
  createFileRoute,
  Link as RouterLink,
  redirect,
} from "@tanstack/react-router"
import { CheckCircle2, Loader2, XCircle } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { z } from "zod"

import { LoginService } from "@/client"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Button } from "@/components/ui/button"
import { isLoggedIn } from "@/hooks/useAuth"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  token: z.string().catch(""),
})

export const Route = createFileRoute("/confirm-email")({
  component: ConfirmEmail,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    if (!search.token) {
      throw redirect({ to: isLoggedIn() ? "/" : "/login" })
    }
  },
  head: () => ({
    meta: [
      {
        title: `Confirm Email - ${APP_NAME}`,
      },
    ],
  }),
})

type ConfirmStatus = "pending" | "success" | "error"

function ConfirmEmail() {
  const { token } = Route.useSearch()
  const startedRef = useRef(false)
  const [status, setStatus] = useState<ConfirmStatus>("pending")

  // Confirm on mount, exactly once. Driven by local state (not a React Query
  // mutation observer) so the result is reflected reliably even through
  // StrictMode's mount → cleanup → remount cycle, which can otherwise leave
  // a mutation observer stuck on "pending" after the request resolves.
  useEffect(() => {
    if (startedRef.current) return
    startedRef.current = true
    LoginService.confirmEmail({ requestBody: { token } })
      .then(() => setStatus("success"))
      .catch(() => setStatus("error"))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loggedIn = isLoggedIn()

  return (
    <AuthLayout>
      <div className="flex flex-col items-center gap-6 text-center">
        {status === "pending" && (
          <>
            <Loader2 className="h-10 w-10 animate-spin text-muted-foreground" />
            <h1 className="text-2xl font-bold">Confirming your email…</h1>
          </>
        )}

        {status === "success" && (
          <>
            <CheckCircle2 className="h-10 w-10 text-green-600" />
            <div className="flex flex-col gap-2">
              <h1 className="text-2xl font-bold">Email confirmed</h1>
              <p className="text-sm text-muted-foreground">
                Your email address has been confirmed. You can now use all
                platform features.
              </p>
            </div>
            <Button asChild className="w-full">
              <RouterLink to={loggedIn ? "/" : "/login"}>
                {loggedIn ? "Go to dashboard" : "Log in"}
              </RouterLink>
            </Button>
          </>
        )}

        {status === "error" && (
          <>
            <XCircle className="h-10 w-10 text-destructive" />
            <div className="flex flex-col gap-2">
              <h1 className="text-2xl font-bold">Confirmation failed</h1>
              <p className="text-sm text-muted-foreground">
                This confirmation link is invalid or has expired. You can
                request a new one from your profile settings.
              </p>
            </div>
            <Button asChild variant="outline" className="w-full">
              <RouterLink to={loggedIn ? "/settings" : "/login"}>
                {loggedIn ? "Go to settings" : "Log in"}
              </RouterLink>
            </Button>
          </>
        )}
      </div>
    </AuthLayout>
  )
}
