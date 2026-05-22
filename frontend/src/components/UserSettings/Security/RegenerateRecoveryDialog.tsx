import { useState } from "react"

import type { MfaStatus, RecoveryCodesPlaintext, StepUpProof } from "@/client"
import { RecoveryCodesDialog } from "@/components/UserSettings/Security/RecoveryCodesDialog"
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
import { useRegenerateRecoveryCodesMutation } from "@/hooks/useMfa"
import { handleError } from "@/utils"

interface RegenerateRecoveryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mfaStatus: MfaStatus | undefined
}

export function RegenerateRecoveryDialog({
  open,
  onOpenChange,
  mfaStatus,
}: RegenerateRecoveryDialogProps) {
  const { showErrorToast } = useCustomToast()
  const regenerate = useRegenerateRecoveryCodesMutation()
  const { user } = useAuth()
  const [codes, setCodes] = useState<RecoveryCodesPlaintext | null>(null)
  const hasPassword = !!user?.has_password

  const handleSubmit = (proof: StepUpProof) => {
    regenerate.mutate(proof, {
      onSuccess: (data) => setCodes(data),
      onError: (err) => handleError.call(showErrorToast, err as never),
    })
  }

  return (
    <>
      <Dialog
        open={open && codes === null}
        onOpenChange={(o) => !o && onOpenChange(false)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Regenerate recovery codes</DialogTitle>
            <DialogDescription>
              This will invalidate your previous recovery codes. Confirm with a
              fresh factor to generate a new batch.
            </DialogDescription>
          </DialogHeader>
          <StepUpProofForm
            mfaStatus={mfaStatus}
            hasPassword={hasPassword}
            loading={regenerate.isPending}
            submitLabel="Regenerate"
            onSubmit={handleSubmit}
            onCancel={() => onOpenChange(false)}
          />
        </DialogContent>
      </Dialog>

      <RecoveryCodesDialog
        open={codes !== null}
        codes={codes}
        title="Your new recovery codes"
        description="Your previous codes are no longer valid. Save these somewhere safe before closing — they cannot be shown again."
        onConfirm={() => {
          setCodes(null)
          onOpenChange(false)
        }}
      />
    </>
  )
}
