import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ShieldCheck } from "lucide-react"
import { useState } from "react"

import { ServerConfigService, type ServerConfigUpdate } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useAccessPolicy } from "@/hooks/useAccessPolicy"
import useCustomToast from "@/hooks/useCustomToast"
import {
  accessPolicyReasonCopy,
  parseAccessPolicyReason,
} from "@/utils/accessPolicyReasons"

const REGISTRATION_MODE_OPEN = "open"
const REGISTRATION_MODE_INVITE_ONLY = "invite_only"

const ROLE_AGENT_USER = "agent-user"
const ROLE_AGENT_DEVELOPER = "agent-developer"

const ROLE_LABELS: Record<string, string> = {
  [ROLE_AGENT_USER]: "Agent User",
  [ROLE_AGENT_DEVELOPER]: "Agent Developer",
}

/**
 * Copy for a refused save. The reason codes and their prose live in
 * `@/utils/accessPolicyReasons` because the same codes are raised at signup,
 * at password login and on the Google button; only the fallback — what to say
 * about a code no release of this frontend has heard of — is local, because
 * only here is the reader an administrator looking at a form.
 */
function reasonMessage(error: unknown): string {
  const known = accessPolicyReasonCopy(error)
  if (known) return known
  const { code } = parseAccessPolicyReason(error)
  return code
    ? `Could not save the access policy (${code}).`
    : "Could not save the access policy."
}

/** The non-empty, whitespace-trimmed entries of a comma-separated glob list. */
function parsePatterns(raw: string): string[] {
  return raw
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
}

/**
 * One sentence describing the policy as it currently stands, so an admin can
 * check the whole front door without reading four controls and combining them
 * in their head.
 */
function summarize(args: {
  registrationOpen: boolean
  passwordAuthEnabled: boolean
  googleAuthEnabled: boolean
  patternCount: number
  defaultRole: string
}): string {
  const { registrationOpen, passwordAuthEnabled, googleAuthEnabled } = args

  let signIn: string
  if (passwordAuthEnabled && googleAuthEnabled) {
    signIn = "Employees sign in with Google or a password"
  } else if (passwordAuthEnabled) {
    signIn = "Employees sign in with a password"
  } else {
    signIn = "Employees sign in with Google only"
  }

  let registration: string
  if (!registrationOpen) {
    registration = "registration is invite-only"
  } else if (args.patternCount > 0) {
    registration = `anyone with a matching email address can register (${args.patternCount} ${args.patternCount === 1 ? "pattern" : "patterns"})`
  } else {
    registration = "anyone can register"
  }

  const role = ROLE_LABELS[args.defaultRole] ?? args.defaultRole
  return `${signIn}; ${registration}; new users become ${role}s.`
}

/**
 * Who may join this instance, how they sign in, and what they become.
 *
 * Shares the `["serverConfig"]` query and the single partial-update endpoint
 * with the other cards on this page — two cards writing different fields of the
 * same singleton cannot conflict as long as they invalidate the same cache
 * entry. This one also invalidates `["accessPolicy"]`: the public projection
 * this card just changed is what the login and signup pages render from, and a
 * stale copy in the admin's own session would show them the old front door.
 */
export function AccessPolicyCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // `null` means "showing the persisted value". Keeping the draft nullable
  // rather than mirroring the server value into state removes the effect that
  // would otherwise have to re-sync it after every save.
  const [patternsDraft, setPatternsDraft] = useState<string | null>(null)
  const [patternsError, setPatternsError] = useState<string | null>(null)

  const { data: config, isLoading } = useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
  })
  // Whether Google sign-in is *configured* is a deployment fact, not a row in
  // `server_config`, so it only reaches this card through the public
  // projection.
  const { data: publicPolicy, isPending: publicPolicyPending } =
    useAccessPolicy()

  const updateMutation = useMutation({
    mutationFn: (data: ServerConfigUpdate) =>
      ServerConfigService.updateServerConfig({ requestBody: data }),
    onSuccess: (data, variables) => {
      // The endpoint returns the updated row, so publish it before the
      // refetch: `isPending` goes false the moment this handler runs, and
      // without this every control would re-render from the stale cache —
      // visibly snapping back to the old value under a success toast — until
      // the invalidation resolves, or forever if that refetch fails.
      queryClient.setQueryData(["serverConfig"], data)
      queryClient.invalidateQueries({ queryKey: ["serverConfig"] })
      queryClient.invalidateQueries({ queryKey: ["accessPolicy"] })
      // Scoped to the save that actually carried the patterns: a switch toggle
      // must not throw away a half-typed pattern list sitting next to it.
      if ("allowed_email_patterns" in variables) {
        setPatternsError(null)
        setPatternsDraft(null)
      }
      showSuccessToast("Access policy updated")
    },
    onError: (error, variables) => {
      const message = reasonMessage(error)
      // The patterns field is the only control with somewhere better than a
      // toast to put its error: the text that caused it is still on screen.
      if ("allowed_email_patterns" in variables) {
        setPatternsError(message)
        return
      }
      showErrorToast(message)
    },
  })

  const registrationMode = config?.registration_mode ?? REGISTRATION_MODE_OPEN
  const inviteOnly = registrationMode !== REGISTRATION_MODE_OPEN
  const passwordAuthEnabled = config?.password_auth_enabled ?? true
  const googleAutoRegister = config?.google_auto_register ?? true
  const defaultRole = config?.default_user_role ?? ROLE_AGENT_USER
  const inviteIncludeDesktop = config?.invite_include_desktop_default ?? true
  const googleAuthEnabled = publicPolicy?.google_auth_enabled ?? false

  const persistedPatterns = config?.allowed_email_patterns ?? ""
  const patternsValue = patternsDraft ?? persistedPatterns
  const patternsDirty = patternsValue !== persistedPatterns
  // Two counts on purpose: the label counter reports the draft the admin is
  // typing, while the summary sentence below describes the *saved* policy and
  // must not narrate unsaved edits as if they were in force.
  const draftPatternCount = parsePatterns(patternsValue).length
  const persistedPatternCount = parsePatterns(persistedPatterns).length

  // `isPending` rather than `data !== undefined`: the projection is
  // `retry: false`, so a failed read must still release the summary rather
  // than hold it on "Reading…" forever.
  const policyKnown = !isLoading && !publicPolicyPending

  const busy = isLoading || updateMutation.isPending

  // Turning password sign-in *off* without Google configured would lock
  // everyone out, and the backend rejects it. Turning it back on is always
  // safe, so the switch is only frozen in the direction that would fail — and
  // only once the projection has actually answered, so a slow read does not
  // briefly claim Google is unconfigured.
  const passwordSwitchLocked =
    passwordAuthEnabled &&
    publicPolicy !== undefined &&
    !publicPolicy.google_auth_enabled

  const savePatterns = () => {
    setPatternsError(null)
    // Never `null`: the backend reads a null field as "not being changed", so
    // clearing the list has to travel as an empty string.
    updateMutation.mutate({ allowed_email_patterns: patternsValue.trim() })
  }

  const passwordSwitch = (
    <Switch
      id="password-auth-enabled"
      checked={passwordAuthEnabled}
      onCheckedChange={(checked) =>
        updateMutation.mutate({ password_auth_enabled: checked })
      }
      disabled={busy || passwordSwitchLocked}
      aria-label="Password sign-in"
      // A disabled control swallows hover, which would leave the tooltip
      // below unreachable. It is already non-interactive, so nothing is lost
      // by letting the events through to the wrapper that owns the tooltip.
      className={passwordSwitchLocked ? "pointer-events-none" : undefined}
    />
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5" />
          Access &amp; New Users
        </CardTitle>
        <CardDescription>
          {policyKnown
            ? summarize({
                registrationOpen: !inviteOnly,
                passwordAuthEnabled,
                googleAuthEnabled,
                patternCount: persistedPatternCount,
                defaultRole,
              })
            : "Reading the current policy…"}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* ── Who can join ─────────────────────────────────────────── */}
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">Who can join</h3>

          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <Label
                htmlFor="registration-mode"
                className="text-sm font-medium"
              >
                Registration
              </Label>
              <p className="text-xs text-muted-foreground">
                {inviteOnly
                  ? "Only people you invite get an account. Signing in with Google never creates one."
                  : "Anyone whose email matches the patterns below can create an account."}
              </p>
            </div>
            <Select
              value={registrationMode}
              onValueChange={(value) =>
                updateMutation.mutate({ registration_mode: value })
              }
              disabled={busy}
            >
              <SelectTrigger
                id="registration-mode"
                className="w-[240px] shrink-0"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={REGISTRATION_MODE_OPEN}>
                  Anyone with an allowed email
                </SelectItem>
                <SelectItem value={REGISTRATION_MODE_INVITE_ONLY}>
                  Invite only
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between gap-4">
              <Label
                htmlFor="allowed-email-patterns"
                className="text-sm font-medium"
              >
                Allowed email addresses
              </Label>
              <span className="text-xs text-muted-foreground shrink-0">
                {draftPatternCount === 0
                  ? "No restriction"
                  : `${draftPatternCount} ${draftPatternCount === 1 ? "pattern" : "patterns"}`}
              </span>
            </div>
            <Textarea
              id="allowed-email-patterns"
              value={patternsValue}
              onChange={(event) => {
                setPatternsDraft(event.target.value)
                // The error and `aria-invalid` describe the text that was
                // rejected; once that text changes they describe nothing.
                if (patternsError) setPatternsError(null)
              }}
              placeholder="*@acme.com, *@*.acme.com"
              className="min-h-[80px] font-mono text-sm"
              // Only the initial read, not every in-flight save: a switch
              // toggle elsewhere on the card must not disable the field and
              // steal focus mid-sentence. The Save button below is what guards
              // against a concurrent patterns write.
              disabled={isLoading}
              aria-invalid={patternsError ? true : undefined}
              aria-describedby={
                patternsError
                  ? "allowed-email-patterns-error allowed-email-patterns-help"
                  : "allowed-email-patterns-help"
              }
            />
            {patternsError && (
              <p
                id="allowed-email-patterns-error"
                role="alert"
                className="text-xs text-destructive"
              >
                {patternsError}
              </p>
            )}
            <p
              id="allowed-email-patterns-help"
              className="text-xs text-muted-foreground"
            >
              Comma-separated glob patterns, for example <code>*@acme.com</code>
              , <code>*@*.acme.com</code>, or <code>jane@acme.com</code>.{" "}
              <code>*</code> matches every address and means no restriction,
              which is also what an empty list means. Patterns gate registration
              only — editing them never locks out an existing user.
            </p>
            {patternsDirty && (
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={savePatterns}
                  disabled={updateMutation.isPending}
                >
                  {updateMutation.isPending ? "Saving..." : "Save patterns"}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setPatternsDraft(null)
                    setPatternsError(null)
                  }}
                  disabled={updateMutation.isPending}
                >
                  Cancel
                </Button>
              </div>
            )}
          </div>
        </section>

        <Separator />

        {/* ── How they sign in ─────────────────────────────────────── */}
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">How they sign in</h3>

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
                    Configure Google OAuth first — otherwise nobody could sign
                    in.
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
                disabled={busy || inviteOnly}
                aria-label="Create accounts on Google sign-in"
              />
            </div>
          </div>
        </section>

        <Separator />

        {/* ── New users ────────────────────────────────────────────── */}
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">New users</h3>

          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <Label
                htmlFor="default-user-role"
                className="text-sm font-medium"
              >
                Default role
              </Label>
              <p className="text-xs text-muted-foreground">
                {defaultRole === ROLE_AGENT_DEVELOPER
                  ? "New accounts can build and configure agents."
                  : "New accounts can use agents that were shared with them."}
              </p>
            </div>
            <Select
              value={defaultRole}
              onValueChange={(value) =>
                updateMutation.mutate({ default_user_role: value })
              }
              disabled={busy}
            >
              <SelectTrigger
                id="default-user-role"
                className="w-[240px] shrink-0"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ROLE_AGENT_USER}>
                  {ROLE_LABELS[ROLE_AGENT_USER]}
                </SelectItem>
                <SelectItem value={ROLE_AGENT_DEVELOPER}>
                  {ROLE_LABELS[ROLE_AGENT_DEVELOPER]}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center justify-between gap-4">
            <div className="min-w-0">
              <Label
                htmlFor="invite-include-desktop"
                className="text-sm font-medium"
              >
                Offer Cinna Desktop in invitations
              </Label>
              <p className="text-xs text-muted-foreground">
                Pre-ticks the desktop download block when you write an
                invitation.
              </p>
            </div>
            <div className="shrink-0">
              <Switch
                id="invite-include-desktop"
                checked={inviteIncludeDesktop}
                onCheckedChange={(checked) =>
                  updateMutation.mutate({
                    invite_include_desktop_default: checked,
                  })
                }
                disabled={busy}
                aria-label="Offer Cinna Desktop in invitations"
              />
            </div>
          </div>
        </section>
      </CardContent>
    </Card>
  )
}
