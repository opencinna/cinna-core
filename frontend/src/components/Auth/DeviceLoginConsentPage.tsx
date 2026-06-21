import { useMutation, useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { CliService, UsersService } from "@/client"
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
import { Input } from "@/components/ui/input"
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Monitor,
  ShieldCheck,
  ShieldX,
  Terminal,
  UserCircle2,
} from "lucide-react"

/**
 * Browser consent screen for the `cinna login` device-authorization flow
 * (RFC 8628). Mirrors the native-app consent screen
 * (`NativeAuthConsentPage`) visually, but the data contract differs:
 *
 *   - keyed by a human-readable `user_code` (the anti-phishing confirmation
 *     the user reads off their terminal), NOT a redirect nonce;
 *   - there is no native redirect — the CLI polls in the background, so after
 *     approve/reject we simply tell the user to return to their terminal.
 *
 * The `code` may arrive prefilled via `?code=` (from `verification_uri_complete`)
 * or be entered manually (bare `verification_uri`).
 */
export function DeviceLoginConsentPage({ code }: { code?: string }) {
  // The confirmed user_code we load metadata for. When `code` is prefilled we
  // use it directly; otherwise the user types it into the form first.
  const [userCode, setUserCode] = useState<string>(code?.trim() ?? "")
  const [codeInput, setCodeInput] = useState<string>("")
  const [authorized, setAuthorized] = useState(false)
  const [denied, setDenied] = useState(false)

  const {
    data: requestInfo,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["device-login-request", userCode],
    queryFn: () =>
      CliService.deviceLoginRequestMetadata({ userCode: userCode }),
    // Only fetch once we actually have a code to look up.
    enabled: userCode.length > 0,
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
    mutationFn: (action: "approve" | "deny") =>
      action === "approve"
        ? CliService.deviceLoginApprove({
            requestBody: { user_code: userCode },
          })
        : CliService.deviceLoginReject({
            requestBody: { user_code: userCode },
          }),
    onSuccess: (_data, action) => {
      if (action === "approve") {
        setAuthorized(true)
      } else {
        setDenied(true)
      }
    },
    onError: (error: any) => {
      // Token may have expired between page load and click; bounce through
      // /login preserving the consent URL so the user lands back here.
      if (error?.status === 401 || error?.status === 403) {
        redirectToLoginPreservingTarget()
      }
    },
  })

  // --- Success / denied terminal states (no native redirect) ---

  if (authorized) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <CheckCircle2 className="mx-auto h-12 w-12 text-green-500" />
            <CardTitle className="mt-2">Device Authorized</CardTitle>
            <CardDescription>
              Return to your terminal — the CLI will finish signing in. You can
              close this tab.
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
            <CardTitle className="mt-2">Request Denied</CardTitle>
            <CardDescription>You can close this tab.</CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  // --- Code-entry form (no prefilled `?code=`) ---

  if (userCode.length === 0) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4 bg-background">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <Terminal className="mx-auto h-12 w-12 text-primary" />
            <CardTitle className="mt-2">Authorize Device</CardTitle>
            <CardDescription>
              Enter the code shown in your terminal after running{" "}
              <code className="font-mono">cinna login</code>.
            </CardDescription>
          </CardHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              // Backend normalizes to uppercase + strips dashes; match the
              // CSS-uppercased display so the value we store is consistent.
              const trimmed = codeInput.trim().toUpperCase()
              if (trimmed) setUserCode(trimmed)
            }}
          >
            <CardContent className="space-y-4">
              <Input
                value={codeInput}
                onChange={(e) => setCodeInput(e.target.value)}
                placeholder="WX7K-9Q2P"
                autoComplete="off"
                autoFocus
                className="text-center font-mono tracking-widest uppercase"
              />
            </CardContent>
            <CardFooter>
              <Button
                type="submit"
                className="w-full"
                disabled={codeInput.trim().length === 0}
              >
                Continue
              </Button>
            </CardFooter>
          </form>
        </Card>
      </div>
    )
  }

  // --- Loading metadata for the entered/prefilled code ---

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  // --- Unknown / expired / invalid code ---

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-destructive" />
            <CardTitle className="mt-2">Login Request Not Found</CardTitle>
            <CardDescription>
              This login request is invalid or has expired. Run{" "}
              <code className="font-mono">cinna login</code> again.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  // --- Already-resolved request (approved / denied / expired) ---
  // `status` is a bare string in the generated client; the backend emits
  // "pending" | "approved" | "denied" | "expired" for display. Anything other
  // than "pending" is terminal here (any unknown value is treated as resolved).

  if (requestInfo && requestInfo.status !== "pending") {
    const resolvedExpired = requestInfo.status === "expired"
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-muted-foreground" />
            <CardTitle className="mt-2">
              {resolvedExpired
                ? "Login Request Expired"
                : "Already Resolved"}
            </CardTitle>
            <CardDescription>
              {resolvedExpired ? (
                <>
                  This login request has expired. Run{" "}
                  <code className="font-mono">cinna login</code> again.
                </>
              ) : (
                "This login request has already been handled. You can close this tab."
              )}
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  // --- Pending: the approve / deny consent screen ---

  return (
    <div className="flex min-h-screen items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md">
        <CardHeader className="text-center">
          <Terminal className="mx-auto h-12 w-12 text-primary" />
          <CardTitle className="mt-2">Authorize Device</CardTitle>
          <CardDescription>
            A command-line device is requesting access to your account.
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
          {/* The user_code is the anti-phishing confirmation: it must match
              the code shown in the user's terminal before they approve. */}
          <div className="rounded-lg border bg-muted/40 p-4 text-center">
            <p className="text-xs text-muted-foreground">
              Confirm this matches your terminal
            </p>
            <p className="mt-1 font-mono text-2xl font-semibold tracking-widest">
              {requestInfo?.user_code}
            </p>
          </div>
          <div className="rounded-lg border p-4 space-y-3">
            {requestInfo?.machine_name && (
              <div className="flex justify-between gap-3">
                <span className="text-sm text-muted-foreground">Machine</span>
                <span className="truncate text-sm font-medium">
                  {requestInfo.machine_name}
                </span>
              </div>
            )}
            {requestInfo?.machine_info && (
              <div className="flex items-start justify-between gap-3">
                <span className="text-sm text-muted-foreground shrink-0">
                  Details
                </span>
                <span className="text-right text-sm font-medium break-words">
                  {requestInfo.machine_info}
                </span>
              </div>
            )}
          </div>
          <p className="flex items-center justify-center gap-2 text-sm text-muted-foreground text-center">
            <Monitor className="h-4 w-4 shrink-0" />
            This will let the CLI sign in and act on your behalf.
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
