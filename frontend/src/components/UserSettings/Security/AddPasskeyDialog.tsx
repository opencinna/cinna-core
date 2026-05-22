import { zodResolver } from "@hookform/resolvers/zod"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError, type RecoveryCodesPlaintext } from "@/client"
import { RecoveryCodesDialog } from "@/components/UserSettings/Security/RecoveryCodesDialog"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { useEnrollPasskeyMutation } from "@/hooks/useMfa"
import { handleError } from "@/utils"
import { isWebAuthnUserCancellation } from "@/utils/webauthn"

const formSchema = z.object({
  nickname: z
    .string()
    .min(1, { message: "Nickname is required" })
    .max(64, { message: "Nickname must be at most 64 characters" }),
})

type FormData = z.infer<typeof formSchema>

interface AddPasskeyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Dialog that runs the full WebAuthn registration ceremony: ask the
 * user for a nickname, call begin → browser create() → finish. When
 * the enrollment turns 2FA on for the first time the server returns a
 * plaintext recovery-code batch that we pop into the one-shot modal.
 */
export function AddPasskeyDialog({
  open,
  onOpenChange,
}: AddPasskeyDialogProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const enroll = useEnrollPasskeyMutation()
  const [recoveryCodes, setRecoveryCodes] =
    useState<RecoveryCodesPlaintext | null>(null)

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: { nickname: "" },
  })

  useEffect(() => {
    if (open) {
      form.reset({ nickname: "" })
    }
  }, [open, form])

  const onSubmit = (data: FormData) => {
    enroll.mutate(
      { nickname: data.nickname.trim() },
      {
        onSuccess: (result) => {
          showSuccessToast(`Passkey "${result.passkey.nickname}" added`)
          if (result.recovery_codes) {
            // First-factor enrollment — open the one-shot recovery codes
            // modal. The Add Passkey dialog stays mounted but hidden until
            // the user confirms saving the codes.
            setRecoveryCodes(result.recovery_codes)
          } else {
            onOpenChange(false)
          }
        },
        onError: (err) => {
          if (isWebAuthnUserCancellation(err)) {
            showErrorToast("Cancelled — no passkey was added")
            return
          }
          // ApiError → standard handler so detail.code maps to the
          // friendly server message.  Otherwise (WebAuthn / TypeError
          // from a bad options payload, etc.) surface the raw message
          // instead of the generic "Something went wrong" so the next
          // bug is easier to triage.
          if (err instanceof ApiError) {
            handleError.call(showErrorToast, err as never)
            return
          }
          const message =
            err instanceof Error && err.message
              ? `Could not add passkey: ${err.message}`
              : "Could not add passkey"
          showErrorToast(message)
        },
      },
    )
  }

  return (
    <>
      <Dialog open={open && recoveryCodes === null} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Add passkey</DialogTitle>
            <DialogDescription>
              Choose a name to recognise this passkey later (e.g. "YubiKey 5" or
              "iPhone Touch ID"). You'll be prompted by your browser to create
              the passkey on your authenticator.
            </DialogDescription>
          </DialogHeader>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <FormField
                control={form.control}
                name="nickname"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Nickname</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        placeholder="My YubiKey"
                        maxLength={64}
                        autoFocus
                      />
                    </FormControl>
                    <FormMessage className="text-xs" />
                  </FormItem>
                )}
              />
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => onOpenChange(false)}
                  disabled={enroll.isPending}
                >
                  Cancel
                </Button>
                <LoadingButton type="submit" loading={enroll.isPending}>
                  Add passkey
                </LoadingButton>
              </DialogFooter>
            </form>
          </Form>
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
