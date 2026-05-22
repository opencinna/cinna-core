import { Key } from "lucide-react"
import { useState } from "react"

import type { MfaStatus } from "@/client"
import { RegenerateRecoveryDialog } from "@/components/UserSettings/Security/RegenerateRecoveryDialog"
import { Button } from "@/components/ui/button"
import { useRecoveryCodesStatus } from "@/hooks/useMfa"

interface RecoveryCodesSectionProps {
  mfaStatus: MfaStatus | undefined
}

/**
 * Recovery-codes block inside the parent two-factor card.
 *
 * Only meaningful when 2FA is on (recovery codes are issued at first
 * enrolment), so the parent card hides this section entirely while
 * `mfaStatus.enabled` is false. Plain section — no own `Card` wrapper.
 */
export function RecoveryCodesSection({ mfaStatus }: RecoveryCodesSectionProps) {
  const { data, isLoading } = useRecoveryCodesStatus()
  const [open, setOpen] = useState(false)
  const enrolled = !!mfaStatus?.enabled

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Key className="h-4 w-4 text-blue-500" />
          <h3 className="font-medium text-sm">Recovery codes</h3>
        </div>
        {enrolled && (
          <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
            Regenerate
          </Button>
        )}
      </div>

      {!enrolled ? (
        <p className="text-sm text-muted-foreground">
          Recovery codes are generated automatically the first time you enable
          2FA.
        </p>
      ) : isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : (
        <div className="text-sm text-muted-foreground">
          {data && data.total_count > 0 ? (
            <p>
              <span className="font-medium text-foreground">
                {data.remaining_count}
              </span>{" "}
              of {data.total_count} remaining. Single-use codes that let you
              sign in if you lose every other factor — keep them somewhere safe.
            </p>
          ) : (
            <p>
              No recovery codes generated yet — they're issued the first time
              you enable 2FA.
            </p>
          )}
          {data?.last_regenerated_at && (
            <p className="text-xs mt-1">
              Last generated{" "}
              {new Date(data.last_regenerated_at).toLocaleDateString()}.
            </p>
          )}
        </div>
      )}

      <RegenerateRecoveryDialog
        open={open}
        onOpenChange={setOpen}
        mfaStatus={mfaStatus}
      />
    </div>
  )
}
