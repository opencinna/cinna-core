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
import { useDisableTwoFactorMutation } from "@/hooks/useMfa"
import { handleError } from "@/utils"

interface DisableTwoFactorDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mfaStatus: MfaStatus | undefined
}

export function DisableTwoFactorDialog({
  open,
  onOpenChange,
  mfaStatus,
}: DisableTwoFactorDialogProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const disable = useDisableTwoFactorMutation()
  const { user } = useAuth()
  const hasPassword = !!user?.has_password

  const handleSubmit = (proof: StepUpProof) => {
    disable.mutate(proof, {
      onSuccess: () => {
        showSuccessToast("Two-factor authentication disabled")
        onOpenChange(false)
      },
      onError: (err) => handleError.call(showErrorToast, err as never),
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Turn off two-factor authentication</DialogTitle>
          <DialogDescription>
            This removes every registered passkey, the authenticator app, and
            all recovery codes. Confirm with a current factor to continue.
          </DialogDescription>
        </DialogHeader>
        <StepUpProofForm
          mfaStatus={mfaStatus}
          hasPassword={hasPassword}
          loading={disable.isPending}
          submitLabel="Disable 2FA"
          onSubmit={handleSubmit}
          onCancel={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}
