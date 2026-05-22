import type { MfaStatus, StepUpProof } from "@/client"
import { StepUpProofForm } from "@/components/UserSettings/Security/StepUpProofForm"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { useDisableTotpMutation } from "@/hooks/useMfa"
import { handleError } from "@/utils"

interface DisableTotpDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mfaStatus: MfaStatus | undefined
}

export function DisableTotpDialog({
  open,
  onOpenChange,
  mfaStatus,
}: DisableTotpDialogProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const disable = useDisableTotpMutation()
  const { user } = useAuth()
  const hasPassword = !!user?.has_password

  // Last factor = 2FA is on, TOTP is enrolled, no passkey to fall back on.
  // Removing TOTP here will turn 2FA off entirely (handled server-side).
  const isLastFactor =
    !!mfaStatus?.enabled && !!mfaStatus.has_totp && !mfaStatus.has_passkey

  const handleSubmit = (proof: StepUpProof) => {
    disable.mutate(proof, {
      onSuccess: () => {
        showSuccessToast(
          isLastFactor
            ? "Authenticator app removed — two-factor authentication is now off"
            : "Authenticator app removed",
        )
        onOpenChange(false)
      },
      onError: (err) => handleError.call(showErrorToast, err as never),
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isLastFactor
              ? "Remove authenticator app and turn off 2FA"
              : "Remove authenticator app"}
          </DialogTitle>
          <DialogDescription>
            {isLastFactor
              ? "This is your last 2FA factor. If you remove it, two-factor authentication will be turned off for your account."
              : "Confirm it's you to remove the linked TOTP secret. Your passkey stays enrolled, so 2FA remains on."}
          </DialogDescription>
        </DialogHeader>
        <StepUpProofForm
          mfaStatus={mfaStatus}
          hasPassword={hasPassword}
          loading={disable.isPending}
          submitLabel={isLastFactor ? "Remove and turn off 2FA" : "Remove TOTP"}
          onSubmit={handleSubmit}
          onCancel={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}
