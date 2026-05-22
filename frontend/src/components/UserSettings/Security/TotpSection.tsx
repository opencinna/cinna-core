import { Smartphone } from "lucide-react"
import { useState } from "react"

import type { MfaStatus } from "@/client"
import { DisableTotpDialog } from "@/components/UserSettings/Security/DisableTotpDialog"
import { EnrollTotpDialog } from "@/components/UserSettings/Security/EnrollTotpDialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

interface TotpSectionProps {
  mfaStatus: MfaStatus | undefined
}

/**
 * Authenticator-app (TOTP) block inside the parent two-factor card.
 * Plain section, no `Card` wrapper — see `PasskeySection` for the
 * surrounding visual pattern.
 */
export function TotpSection({ mfaStatus }: TotpSectionProps) {
  const [enrollOpen, setEnrollOpen] = useState(false)
  const [disableOpen, setDisableOpen] = useState(false)
  const enrolled = !!mfaStatus?.has_totp

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Smartphone className="h-4 w-4 text-blue-500" />
          <h3 className="font-medium text-sm">Authenticator app</h3>
          {enrolled && (
            <Badge variant="secondary" className="text-xs">
              Enrolled
            </Badge>
          )}
        </div>
        {enrolled ? (
          <Button
            size="sm"
            variant="destructive"
            onClick={() => setDisableOpen(true)}
          >
            Disable
          </Button>
        ) : (
          <Button size="sm" onClick={() => setEnrollOpen(true)}>
            Set up authenticator app
          </Button>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        {enrolled
          ? "Authenticator app is linked. Use it to confirm your identity at sign-in or for sensitive changes."
          : "Time-based codes from apps like Google Authenticator, 1Password, or Authy. Works offline."}
      </p>

      <EnrollTotpDialog open={enrollOpen} onOpenChange={setEnrollOpen} />
      <DisableTotpDialog
        open={disableOpen}
        onOpenChange={setDisableOpen}
        mfaStatus={mfaStatus}
      />
    </div>
  )
}
