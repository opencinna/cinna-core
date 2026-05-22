import { Fingerprint, Plus } from "lucide-react"
import { useState } from "react"

import type { MfaStatus } from "@/client"
import { AddPasskeyDialog } from "@/components/UserSettings/Security/AddPasskeyDialog"
import { PasskeyList } from "@/components/UserSettings/Security/PasskeyList"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useMfaPasskeys } from "@/hooks/useMfa"
import { isWebAuthnSupported } from "@/utils/webauthn"

/**
 * Passkeys block inside the parent two-factor card. Renders the section
 * header (icon + title + add CTA) and either the empty hint or the list
 * of registered passkeys. Does not render its own `Card` wrapper — it's
 * a plain section so the surrounding card can keep a single visual rhythm
 * (mirrors the MCP-connectors card pattern).
 */
interface PasskeySectionProps {
  mfaStatus: MfaStatus | undefined
}

export function PasskeySection({ mfaStatus }: PasskeySectionProps) {
  const { data, isLoading } = useMfaPasskeys()
  const [open, setOpen] = useState(false)
  const passkeys = data?.data ?? []
  const supported = isWebAuthnSupported()
  const hasPasskeys = passkeys.length > 0

  const addButton = (
    <Button
      size="sm"
      onClick={() => setOpen(true)}
      disabled={!supported}
      aria-label="Add a passkey"
    >
      <Plus className="h-4 w-4 mr-1" />
      Add passkey
    </Button>
  )

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Fingerprint className="h-4 w-4 text-blue-500" />
          <h3 className="font-medium text-sm">Passkeys</h3>
          {hasPasskeys && (
            <span className="text-xs text-muted-foreground">
              {passkeys.length} registered
            </span>
          )}
        </div>
        {supported ? (
          addButton
        ) : (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>{addButton}</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs text-xs" side="left">
                Your browser doesn't support passkeys; try Chrome, Safari,
                Firefox, or Edge.
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>

      {hasPasskeys ? (
        <PasskeyList
          passkeys={passkeys}
          isLoading={isLoading}
          mfaStatus={mfaStatus}
        />
      ) : isLoading ? (
        <p className="text-sm text-muted-foreground">Loading passkeys...</p>
      ) : (
        <p className="text-sm text-muted-foreground">
          No passkeys registered. Phishing-resistant sign-in with your device,
          security key, or synced authenticator.
        </p>
      )}

      <AddPasskeyDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}
