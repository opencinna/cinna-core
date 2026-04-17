import { createFileRoute, redirect } from "@tanstack/react-router"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { z } from "zod"
import { isLoggedIn } from "@/hooks/useAuth"
import { DesktopAuthService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Loader2,
  Monitor,
  ShieldCheck,
  ShieldX,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react"
import { APP_NAME } from "@/utils"

const searchSchema = z.object({
  request: z.string(),
})

export const Route = createFileRoute("/desktop-auth/consent")({
  component: DesktopAuthConsentPage,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    if (!isLoggedIn()) {
      // Redirect to login; user must navigate back to the consent URL after logging in.
      // The desktop app can restart the authorization flow if needed.
      throw redirect({
        to: "/login",
        search: {
          redirect: `/desktop-auth/consent?request=${search.request}`,
        },
      })
    }
  },
  head: () => ({
    meta: [{ title: `Authorize Cinna Desktop - ${APP_NAME}` }],
  }),
})

function DesktopAuthConsentPage() {
  const { request: nonce } = Route.useSearch()
  const [authorized, setAuthorized] = useState(false)
  const [denied, setDenied] = useState(false)

  const {
    data: requestInfo,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["desktop-auth-request", nonce],
    queryFn: () => DesktopAuthService.getAuthRequest({ nonce }),
    retry: false,
  })

  const consentMutation = useMutation({
    mutationFn: (action: "approve" | "deny") =>
      DesktopAuthService.consent({
        requestBody: { request_nonce: nonce, action },
      }),
    onSuccess: (data, action) => {
      if (action === "approve") {
        setAuthorized(true)
      } else {
        setDenied(true)
      }
      // Redirect to the desktop app's local callback server
      window.location.href = data.redirect_to
    },
  })

  // After authorization or denial, try to close the tab.
  // Wait long enough for the browser to dispatch the redirect URL
  // (custom protocol URLs may need a moment) before attempting close.
  // window.close() only works for script-opened tabs.
  useEffect(() => {
    if (!authorized && !denied) return
    const timer = setTimeout(() => {
      window.close()
    }, 10000)
    return () => clearTimeout(timer)
  }, [authorized, denied])

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (authorized) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-500" />
            <CardTitle className="mt-2">Authorization Successful</CardTitle>
            <CardDescription>
              You can close this tab and return to Cinna Desktop.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  if (denied) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <ShieldX className="mx-auto h-12 w-12 text-muted-foreground" />
            <CardTitle className="mt-2">Authorization Denied</CardTitle>
            <CardDescription>You can close this tab.</CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-destructive" />
            <CardTitle className="mt-2">Authorization Error</CardTitle>
            <CardDescription>
              {(error as Error).message ||
                "This link is invalid or has expired. Please restart the Cinna Desktop authorization flow."}
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  const deviceLabel = requestInfo?.device_name as string | undefined
  const platform = requestInfo?.platform as string | undefined
  const appVersion = requestInfo?.app_version as string | undefined

  return (
    <div className="flex min-h-screen items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <Monitor className="mx-auto h-12 w-12 text-primary" />
          <CardTitle className="mt-2">Authorize Cinna Desktop</CardTitle>
          <CardDescription>
            A desktop app is requesting access to your account.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-lg border p-4 space-y-3">
            {deviceLabel && (
              <div className="flex justify-between">
                <span className="text-sm text-muted-foreground">Device</span>
                <span className="text-sm font-medium">{deviceLabel}</span>
              </div>
            )}
            {platform && (
              <div className="flex justify-between">
                <span className="text-sm text-muted-foreground">Platform</span>
                <span className="text-sm font-medium capitalize">{platform}</span>
              </div>
            )}
            {appVersion && (
              <div className="flex justify-between">
                <span className="text-sm text-muted-foreground">Version</span>
                <span className="text-sm font-medium">v{appVersion}</span>
              </div>
            )}
          </div>
          <p className="text-sm text-muted-foreground text-center">
            This will allow Cinna Desktop to sign in using your account and
            access the platform on your behalf.
          </p>
        </CardContent>
        <CardFooter className="flex gap-3">
          <Button
            variant="outline"
            className="flex-1"
            onClick={() => consentMutation.mutate("deny")}
            disabled={consentMutation.isPending}
          >
            <ShieldX className="mr-2 h-4 w-4" />
            Deny
          </Button>
          <Button
            className="flex-1"
            onClick={() => consentMutation.mutate("approve")}
            disabled={consentMutation.isPending}
          >
            {consentMutation.isPending ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <ShieldCheck className="mr-2 h-4 w-4" />
            )}
            Approve
          </Button>
        </CardFooter>
        {consentMutation.isError && (
          <div className="px-6 pb-4">
            <p className="text-sm text-destructive text-center">
              {(consentMutation.error as Error).message ||
                "Something went wrong. Please try again."}
            </p>
          </div>
        )}
      </Card>
    </div>
  )
}
