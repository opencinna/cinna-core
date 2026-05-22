import { Check, Copy, Loader2 } from "lucide-react"
import { useEffect, useState } from "react"

import { ApiError, type RecoveryCodesPlaintext, type TotpEnrollResponse } from "@/client"
import { TotpForm, type TotpFormData } from "@/components/Auth/TotpForm"
import { RecoveryCodesDialog } from "@/components/UserSettings/Security/RecoveryCodesDialog"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import useCustomToast from "@/hooks/useCustomToast"
import {
  useBeginTotpEnrollmentMutation,
  useFinishTotpEnrollmentMutation,
} from "@/hooks/useMfa"
import { handleError } from "@/utils"

interface EnrollTotpDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EnrollTotpDialog({
  open,
  onOpenChange,
}: EnrollTotpDialogProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const begin = useBeginTotpEnrollmentMutation()
  const finish = useFinishTotpEnrollmentMutation()
  const [enrollment, setEnrollment] = useState<TotpEnrollResponse | null>(null)
  const [recoveryCodes, setRecoveryCodes] =
    useState<RecoveryCodesPlaintext | null>(null)
  const [copied, setCopied] = useState(false)
  const [invalidCode, setInvalidCode] = useState(false)

  // Kick off /begin when the dialog opens so the QR code is ready by the
  // time the user lands on the form.
  useEffect(() => {
    if (open && enrollment === null && !begin.isPending) {
      begin.mutate(undefined, {
        onSuccess: (data) => setEnrollment(data),
        onError: (err) => {
          handleError.call(showErrorToast, err as never)
          onOpenChange(false)
        },
      })
    }
  }, [open, enrollment, begin, onOpenChange, showErrorToast])

  // Reset local state once the dialog has closed so the next open starts
  // from a clean slate.
  useEffect(() => {
    if (!open) {
      setEnrollment(null)
      setRecoveryCodes(null)
      setCopied(false)
      setInvalidCode(false)
    }
  }, [open])

  const handleCopySecret = async () => {
    if (!enrollment) return
    await navigator.clipboard.writeText(enrollment.secret_base32)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const handleVerify = ({ code }: TotpFormData) => {
    if (!enrollment) return
    finish.mutate(
      { secret_token: enrollment.secret_token, code },
      {
        onSuccess: (result) => {
          showSuccessToast("Authenticator app linked")
          if (result.recovery_codes) {
            setRecoveryCodes(result.recovery_codes)
          } else {
            onOpenChange(false)
          }
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 400) {
            const detail = (err.body as { detail?: { code?: string } } | null)
              ?.detail
            if (detail?.code === "invalid_code") {
              setInvalidCode(true)
              return
            }
          }
          handleError.call(showErrorToast, err as never)
        },
      },
    )
  }

  return (
    <>
      <Dialog open={open && recoveryCodes === null} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Set up authenticator app</DialogTitle>
            <DialogDescription>
              Scan the QR code with Google Authenticator, 1Password, Authy, or
              any TOTP app, then enter the 6-digit code it shows.
            </DialogDescription>
          </DialogHeader>

          {!enrollment ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <div className="space-y-4 py-2">
              <div className="flex justify-center">
                <img
                  src={enrollment.qr_svg_data_uri}
                  alt="TOTP QR code"
                  className="h-48 w-48 rounded-md border bg-white p-2"
                />
              </div>
              <div>
                <p className="text-xs text-muted-foreground mb-1">
                  Or enter this secret manually:
                </p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 break-all rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
                    {enrollment.secret_base32}
                  </code>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={handleCopySecret}
                    aria-label="Copy secret"
                  >
                    {copied ? (
                      <Check className="h-4 w-4" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </Button>
                </div>
              </div>

              <TotpForm
                onSubmit={handleVerify}
                loading={finish.isPending}
                buttonLabel="Verify and enable"
                autoSubmit={false}
                invalid={invalidCode}
                onCodeChange={() => {
                  if (invalidCode) setInvalidCode(false)
                }}
              />
            </div>
          )}
        </DialogContent>
      </Dialog>

      <RecoveryCodesDialog
        open={recoveryCodes !== null}
        codes={recoveryCodes}
        onConfirm={() => {
          setRecoveryCodes(null)
          onOpenChange(false)
        }}
      />
    </>
  )
}
