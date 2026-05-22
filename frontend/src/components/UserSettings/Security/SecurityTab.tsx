import { Fingerprint, Loader2, ShieldCheck, Smartphone } from "lucide-react"
import { useState } from "react"

import { AddPasskeyDialog } from "@/components/UserSettings/Security/AddPasskeyDialog"
import { DisableTwoFactorDialog } from "@/components/UserSettings/Security/DisableTwoFactorDialog"
import { EnrollTotpDialog } from "@/components/UserSettings/Security/EnrollTotpDialog"
import { PasskeySection } from "@/components/UserSettings/Security/PasskeySection"
import { RecoveryCodesSection } from "@/components/UserSettings/Security/RecoveryCodesSection"
import { TotpSection } from "@/components/UserSettings/Security/TotpSection"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useMfaPasskeys, useMfaStatus } from "@/hooks/useMfa"
import { isWebAuthnSupported } from "@/utils/webauthn"

/**
 * Settings → Security tab.
 *
 * Renders a single Two-factor authentication card with three inner
 * blocks (passkeys, authenticator app, recovery codes) separated by
 * `Separator` rules — mirrors the MCP-connectors card pattern of one
 * outer Card with internal sections rather than a grid of nested cards.
 *
 * When neither factor is enrolled we collapse to a single inline empty
 * state with both CTAs side-by-side so the user has one clear next step.
 */
export function SecurityTab() {
  const { data: mfaStatus, isLoading: isStatusLoading } = useMfaStatus()
  const { data: passkeyData, isLoading: isPasskeysLoading } = useMfaPasskeys()
  const [disableOpen, setDisableOpen] = useState(false)
  const [addPasskeyOpen, setAddPasskeyOpen] = useState(false)
  const [enrollTotpOpen, setEnrollTotpOpen] = useState(false)

  const enabled = !!mfaStatus?.enabled
  const hasTotp = !!mfaStatus?.has_totp
  const passkeys = passkeyData?.data ?? []
  const passkeyCount = passkeys.length
  const supported = isWebAuthnSupported()

  // Loading both queries together so the card doesn't pop in piecemeal.
  if (isStatusLoading || isPasskeysLoading) {
    return (
      <div className="flex justify-center items-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  const summary = buildSummary({ passkeyCount, hasTotp })
  const showEmptyState = passkeyCount === 0 && !hasTotp

  const handleToggle = (next: boolean) => {
    if (!enabled) return
    if (!next) setDisableOpen(true)
  }

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-blue-500" />
                Two-factor authentication
              </CardTitle>
              <CardDescription>
                {enabled && summary
                  ? summary
                  : "Add a second step to every sign-in to protect your account against phishing and stolen passwords."}
              </CardDescription>
            </div>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <label
                    className={`flex select-none items-center ml-4 mt-1 ${
                      enabled ? "cursor-pointer" : "cursor-not-allowed"
                    }`}
                  >
                    <div className="relative">
                      <input
                        type="checkbox"
                        checked={enabled}
                        onChange={(e) => handleToggle(e.target.checked)}
                        disabled={!enabled}
                        className="sr-only"
                      />
                      <div
                        className={`block h-6 w-11 rounded-full transition-colors ${
                          enabled
                            ? "bg-emerald-500"
                            : "bg-gray-300 dark:bg-gray-600"
                        }`}
                      />
                      <div
                        className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                          enabled ? "translate-x-5" : ""
                        }`}
                      />
                    </div>
                  </label>
                </TooltipTrigger>
                {!enabled && (
                  <TooltipContent className="max-w-xs text-xs">
                    Add a passkey or set up an authenticator app below to turn
                    on two-factor authentication.
                  </TooltipContent>
                )}
              </Tooltip>
            </TooltipProvider>
          </div>
        </CardHeader>

        <CardContent>
          {showEmptyState ? (
            <EmptyState
              supported={supported}
              onAddPasskey={() => setAddPasskeyOpen(true)}
              onEnrollTotp={() => setEnrollTotpOpen(true)}
            />
          ) : (
            <div className="space-y-5">
              <PasskeySection mfaStatus={mfaStatus} />
              <Separator />
              <TotpSection mfaStatus={mfaStatus} />
              {enabled && (
                <>
                  <Separator />
                  <RecoveryCodesSection mfaStatus={mfaStatus} />
                </>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <DisableTwoFactorDialog
        open={disableOpen}
        onOpenChange={setDisableOpen}
        mfaStatus={mfaStatus}
      />
      <AddPasskeyDialog open={addPasskeyOpen} onOpenChange={setAddPasskeyOpen} />
      <EnrollTotpDialog open={enrollTotpOpen} onOpenChange={setEnrollTotpOpen} />
    </>
  )
}

interface SummaryArgs {
  passkeyCount: number
  hasTotp: boolean
}

/**
 * Short header summary for the enrolled state, e.g.
 * "2 passkeys · authenticator app". Returns empty string when nothing
 * is enrolled so the caller can fall back to the default CTA copy.
 */
function buildSummary({ passkeyCount, hasTotp }: SummaryArgs): string {
  const parts: string[] = []
  if (passkeyCount === 1) {
    parts.push("1 passkey")
  } else if (passkeyCount > 1) {
    parts.push(`${passkeyCount} passkeys`)
  }
  if (hasTotp) {
    parts.push("authenticator app")
  }
  return parts.join(" · ")
}

interface EmptyStateProps {
  supported: boolean
  onAddPasskey: () => void
  onEnrollTotp: () => void
}

/**
 * Shown when neither passkeys nor TOTP are enrolled. Inline, single
 * row of CTAs — recovery codes are intentionally absent here because
 * they only exist once 2FA is on.
 */
function EmptyState({ supported, onAddPasskey, onEnrollTotp }: EmptyStateProps) {
  const passkeyButton = (
    <Button
      size="sm"
      onClick={onAddPasskey}
      disabled={!supported}
      aria-label="Add a passkey"
    >
      <Fingerprint className="h-4 w-4 mr-2" />
      Add passkey
    </Button>
  )

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Add a passkey or set up an authenticator app to turn on two-factor
        authentication. Recovery codes are issued automatically once your
        first factor is enrolled.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {supported ? (
          passkeyButton
        ) : (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>{passkeyButton}</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs text-xs">
                Your browser doesn't support passkeys; try Chrome, Safari,
                Firefox, or Edge.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        <Button size="sm" variant="outline" onClick={onEnrollTotp}>
          <Smartphone className="h-4 w-4 mr-2" />
          Set up authenticator app
        </Button>
      </div>
    </div>
  )
}
