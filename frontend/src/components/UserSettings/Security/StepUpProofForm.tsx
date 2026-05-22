import { useState } from "react"

import type { MfaStatus, StepUpProof } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { PasswordInput } from "@/components/ui/password-input"
import useCustomToast from "@/hooks/useCustomToast"
import { runStepUpPasskey } from "@/hooks/useMfa"
import {
  isWebAuthnSupported,
  isWebAuthnUserCancellation,
} from "@/utils/webauthn"

type Factor = "password" | "totp" | "passkey"

interface StepUpProofFormProps {
  mfaStatus: MfaStatus | undefined
  /**
   * If the user authenticates with email + password we always allow
   * "password". OAuth-only users can't use password — call sites pass
   * `hasPassword=false` to hide that option.
   */
  hasPassword: boolean
  loading?: boolean
  submitLabel?: string
  onSubmit: (proof: StepUpProof) => void
  onCancel?: () => void
}

/**
 * Step-up factor picker reused by every destructive MFA action (disable
 * 2FA, disable TOTP, regenerate recovery codes). The user picks one of
 * the factors they have, supplies the proof, and we build the right
 * `StepUpProof` body for the parent mutation.
 */
export function StepUpProofForm({
  mfaStatus,
  hasPassword,
  loading = false,
  submitLabel = "Confirm",
  onSubmit,
  onCancel,
}: StepUpProofFormProps) {
  const { showErrorToast } = useCustomToast()

  const passkeyAvailable = !!mfaStatus?.has_passkey && isWebAuthnSupported()
  const totpAvailable = !!mfaStatus?.has_totp

  const availableFactors: Factor[] = []
  if (hasPassword) availableFactors.push("password")
  if (totpAvailable) availableFactors.push("totp")
  if (passkeyAvailable) availableFactors.push("passkey")

  const [factor, setFactor] = useState<Factor>(
    availableFactors[0] ?? "password",
  )
  const [password, setPassword] = useState("")
  const [totpCode, setTotpCode] = useState("")
  const [passkeyBusy, setPasskeyBusy] = useState(false)

  if (availableFactors.length === 0) {
    return (
      <p className="text-sm text-destructive">
        No verification method available. Contact support if you can't sign in.
      </p>
    )
  }

  const handleSubmit = async () => {
    if (factor === "password") {
      if (password.length === 0) {
        showErrorToast("Enter your password to continue")
        return
      }
      onSubmit({ password })
      return
    }
    if (factor === "totp") {
      if (!/^[0-9]{6}$/u.test(totpCode)) {
        showErrorToast("Enter the 6-digit code from your authenticator")
        return
      }
      onSubmit({ totp_code: totpCode })
      return
    }
    if (factor === "passkey") {
      setPasskeyBusy(true)
      try {
        const assertion = await runStepUpPasskey()
        onSubmit(assertion)
      } catch (err) {
        if (isWebAuthnUserCancellation(err)) {
          showErrorToast("Cancelled — no changes made")
        } else {
          showErrorToast(
            err instanceof Error ? err.message : "Passkey verification failed",
          )
        }
      } finally {
        setPasskeyBusy(false)
      }
    }
  }

  return (
    <div className="space-y-4">
      {availableFactors.length > 1 && (
        <div className="flex flex-wrap gap-2">
          {availableFactors.includes("password") && (
            <Button
              type="button"
              variant={factor === "password" ? "default" : "outline"}
              size="sm"
              onClick={() => setFactor("password")}
            >
              Password
            </Button>
          )}
          {availableFactors.includes("totp") && (
            <Button
              type="button"
              variant={factor === "totp" ? "default" : "outline"}
              size="sm"
              onClick={() => setFactor("totp")}
            >
              Authenticator code
            </Button>
          )}
          {availableFactors.includes("passkey") && (
            <Button
              type="button"
              variant={factor === "passkey" ? "default" : "outline"}
              size="sm"
              onClick={() => setFactor("passkey")}
            >
              Passkey
            </Button>
          )}
        </div>
      )}

      {factor === "password" && (
        <div className="space-y-2">
          <Label htmlFor="step-up-password">Password</Label>
          <PasswordInput
            id="step-up-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
      )}
      {factor === "totp" && (
        <div className="space-y-2">
          <Label htmlFor="step-up-totp">Authentication code</Label>
          <Input
            id="step-up-totp"
            value={totpCode}
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            placeholder="123456"
            onChange={(e) =>
              setTotpCode(e.target.value.replace(/[^0-9]/gu, "").slice(0, 6))
            }
          />
        </div>
      )}
      {factor === "passkey" && (
        <p className="text-sm text-muted-foreground">
          You'll be prompted by your browser to confirm with your passkey.
        </p>
      )}

      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={loading || passkeyBusy}
          >
            Cancel
          </Button>
        )}
        <LoadingButton
          type="button"
          onClick={handleSubmit}
          loading={loading || passkeyBusy}
        >
          {submitLabel}
        </LoadingButton>
      </div>
    </div>
  )
}
