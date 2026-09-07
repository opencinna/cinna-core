import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useAccessPolicy } from "@/hooks/useAccessPolicy"
import {
  describeSignIn,
  pendingConfigField,
  REGISTRATION_MODE_OPEN,
  useServerConfig,
  useServerConfigUpdate,
} from "./accessPolicy"

/**
 * Which doors are open, and why one of them is bolted.
 *
 * Reads two queries, and neither may be papered over: whether Google sign-in
 * is *configured* is a deployment fact rather than a row in `server_config`,
 * so it only reaches this card through the public projection. If either read
 * fails the card says so — rendering the defaults would claim Google is
 * unconfigured and offer to switch password sign-in off, which is exactly the
 * combination the backend refuses.
 */
export function SignInMethodsCard() {
  const { data: config, isError, error, refetch } = useServerConfig()
  const {
    data: publicPolicy,
    isError: policyError,
    error: policyErrorValue,
    refetch: refetchPolicy,
  } = useAccessPolicy()

  const updateMutation = useServerConfigUpdate()
  // Which field is in flight, so the switch the admin clicked is the only one
  // that goes dead — the other stays usable.
  const pendingField = pendingConfigField(updateMutation)

  const passwordAuthEnabled = config?.password_auth_enabled ?? true
  const googleAutoRegister = config?.google_auto_register ?? true
  const inviteOnly =
    (config?.registration_mode ?? REGISTRATION_MODE_OPEN) !==
    REGISTRATION_MODE_OPEN
  const googleAuthEnabled = publicPolicy?.google_auth_enabled ?? false

  // Turning password sign-in *off* without Google configured would lock
  // everyone out, and the backend rejects it. Turning it back on is always
  // safe, so the switch is only frozen in the direction that would fail — and
  // only once the projection has actually answered, so a slow read does not
  // briefly claim Google is unconfigured.
  const passwordSwitchLocked =
    passwordAuthEnabled &&
    publicPolicy !== undefined &&
    !publicPolicy.google_auth_enabled

  const passwordSwitch = (
    <Switch
      id="password-auth-enabled"
      checked={passwordAuthEnabled}
      onCheckedChange={(checked) =>
        updateMutation.mutate({ password_auth_enabled: checked })
      }
      disabled={
        pendingField === "password_auth_enabled" || passwordSwitchLocked
      }
      aria-label="Password sign-in"
      // A disabled control swallows hover, which would leave the tooltip
      // below unreachable. It is already non-interactive, so nothing is lost
      // by letting the events through to the wrapper that owns the tooltip.
      className={passwordSwitchLocked ? "pointer-events-none" : undefined}
    />
  )

  const body = () => {
    // Only when the failure left nothing to render: a background refetch can
    // report `error` with the last good policy still in the cache, and this
    // card must not blank itself over a blip.
    if ((isError || policyError) && (!config || !publicPolicy)) {
      return (
        <QueryErrorAlert
          error={isError ? error : policyErrorValue}
          fallback="Couldn't read the sign-in policy."
          onRetry={() => {
            if (isError) refetch()
            if (policyError) refetchPolicy()
          }}
        />
      )
    }

    // Everything else without both answers is still on its way. The
    // projection is `retry: false`, so a read that fails releases the card
    // through the error branch above rather than holding it here forever.
    if (!config || !publicPolicy) {
      return (
        <div className="space-y-4">
          <Skeleton className="h-11 w-full" />
          <Skeleton className="h-11 w-full" />
        </div>
      )
    }

    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <Label
              htmlFor="password-auth-enabled"
              className="text-sm font-medium"
            >
              Password sign-in
            </Label>
            <p className="text-xs text-muted-foreground">
              {passwordSwitchLocked
                ? "Google OAuth is not configured on this server, so password sign-in is the only way in."
                : "Turn this off for a Google-only server. Administrators keep password sign-in as a break-glass path."}
            </p>
          </div>
          <div className="shrink-0">
            {passwordSwitchLocked ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  {/* The wrapper is the hover target; it carries no tabIndex
                      because the switch it wraps is not focusable either, and
                      the same explanation is in the visible helper text
                      beside it for anyone who never hovers. */}
                  <span className="inline-flex">{passwordSwitch}</span>
                </TooltipTrigger>
                <TooltipContent>
                  Configure Google OAuth first — otherwise nobody could sign in.
                </TooltipContent>
              </Tooltip>
            ) : (
              passwordSwitch
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <Label
              htmlFor="google-auto-register"
              className={
                inviteOnly
                  ? "text-sm font-medium text-muted-foreground"
                  : "text-sm font-medium"
              }
            >
              Create accounts on Google sign-in
            </Label>
            <p className="text-xs text-muted-foreground">
              {inviteOnly
                ? "Ignored while registration is invite-only — a Google sign-in never creates an account in that mode."
                : "A Google sign-in from an allowed, unknown address creates the account."}
            </p>
          </div>
          <div className="shrink-0">
            <Switch
              id="google-auto-register"
              checked={googleAutoRegister}
              onCheckedChange={(checked) =>
                updateMutation.mutate({ google_auto_register: checked })
              }
              disabled={pendingField === "google_auto_register" || inviteOnly}
              aria-label="Create accounts on Google sign-in"
            />
          </div>
        </div>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Sign-in methods</CardTitle>
        <CardDescription>
          {config && publicPolicy
            ? describeSignIn({ passwordAuthEnabled, googleAuthEnabled })
            : "How employees get in."}
        </CardDescription>
      </CardHeader>
      <CardContent>{body()}</CardContent>
    </Card>
  )
}
