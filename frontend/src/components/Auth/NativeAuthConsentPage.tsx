import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { UsersService } from "@/client"
import { redirectToLoginPreservingTarget } from "@/hooks/useAuth"
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
  Smartphone,
  ShieldCheck,
  ShieldX,
  AlertTriangle,
  CheckCircle2,
  UserCircle2,
} from "lucide-react"

/** Non-secret metadata returned by the consent-request endpoint. */
export interface NativeAuthRequestInfo {
  device_name?: string | null
  platform?: string | null
  app_version?: string | null
  client_kind?: string | null
}

interface NativeAuthConsentPageProps {
  /** The opaque consent-request nonce from the URL. */
  nonce: string
  /** Fetch non-secret display metadata for the pending request. */
  getRequest: (nonce: string) => Promise<NativeAuthRequestInfo>
  /** Submit the user's approve/deny decision; resolves with the app redirect URL. */
  submitConsent: (
    nonce: string,
    action: "approve" | "deny",
  ) => Promise<{ redirect_to: string }>
  /** React Query key namespace so the two surfaces don't collide in cache. */
  queryKeyPrefix: string
}

/**
 * Shared consent screen for native-client OAuth flows (Cinna Desktop and Cinna
 * Mobile). The desktop and app surfaces share backing storage and logic; this
 * component is parameterized only by which service endpoints to call. The
 * "desktop" vs "mobile" copy is driven by the backend-derived `client_kind`.
 */
export function NativeAuthConsentPage({
  nonce,
  getRequest,
  submitConsent,
  queryKeyPrefix,
}: NativeAuthConsentPageProps) {
  const [authorized, setAuthorized] = useState(false)
  const [denied, setDenied] = useState(false)

  const {
    data: requestInfo,
    isLoading,
    error,
  } = useQuery({
    queryKey: [queryKeyPrefix, nonce],
    queryFn: () => getRequest(nonce),
    retry: false,
  })

  // The account the consent will be granted for. Shares the global
  // ["currentUser"] cache key so it reuses any already-fetched profile.
  const { data: currentUser } = useQuery({
    queryKey: ["currentUser"],
    queryFn: () => UsersService.readUserMe(),
    retry: false,
  })

  const consentMutation = useMutation({
    mutationFn: (action: "approve" | "deny") => submitConsent(nonce, action),
    onSuccess: (data, action) => {
      if (action === "approve") {
        setAuthorized(true)
      } else {
        setDenied(true)
      }
      // Redirect to the native app's callback (loopback or custom scheme)
      window.location.href = data.redirect_to
    },
    onError: (error: any) => {
      // Token may have expired between page load and click; bounce through
      // /login preserving the consent URL so the user lands back here.
      if (error?.status === 401 || error?.status === 403) {
        redirectToLoginPreservingTarget()
      }
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

  // Derived display copy: the backend classifies the client as "mobile" or
  // "desktop" from the (secret) redirect_uri scheme. Default to desktop.
  const clientKind = requestInfo?.client_kind ?? "desktop"
  const isMobile = clientKind === "mobile"
  const appLabel = isMobile ? "Cinna Mobile" : "Cinna Desktop"
  const appKindNoun = isMobile ? "mobile app" : "desktop app"
  const AppIcon = isMobile ? Smartphone : Monitor

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
              You can close this tab and return to {appLabel}.
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
                `This link is invalid or has expired. Please restart the ${appLabel} authorization flow.`}
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
          <AppIcon className="mx-auto h-12 w-12 text-primary" />
          <CardTitle className="mt-2">Authorize {appLabel}</CardTitle>
          <CardDescription>
            A {appKindNoun} is requesting access to your account.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {currentUser && (
            <div className="flex items-center gap-3 rounded-lg border bg-muted/40 p-3">
              <UserCircle2 className="h-5 w-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0">
                <p className="text-xs text-muted-foreground">Signed in as</p>
                <p className="truncate text-sm font-medium">
                  {currentUser.full_name || currentUser.email}
                </p>
                {currentUser.full_name && (
                  <p className="truncate text-xs text-muted-foreground">
                    {currentUser.email}
                  </p>
                )}
              </div>
            </div>
          )}
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
            This will allow {appLabel} to sign in using your account and
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
        <div className="px-6 pb-6 text-center">
          <button
            type="button"
            onClick={() => redirectToLoginPreservingTarget()}
            disabled={consentMutation.isPending}
            className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline disabled:pointer-events-none disabled:opacity-50"
          >
            Use another account
          </button>
        </div>
      </Card>
    </div>
  )
}
