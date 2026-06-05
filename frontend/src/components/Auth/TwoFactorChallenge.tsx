import { useNavigate } from "@tanstack/react-router"
import { useCallback, useEffect, useState } from "react"

import { ApiError, type MfaVerifyRequest } from "@/client"
import { useMfaChallenge } from "@/components/Auth/MfaChallengeContext"
import { PasskeyButton } from "@/components/Auth/PasskeyButton"
import {
  RecoveryCodeForm,
  type RecoveryCodeFormData,
} from "@/components/Auth/RecoveryCodeForm"
import { TotpForm, type TotpFormData } from "@/components/Auth/TotpForm"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { fetchLoginPasskeyOptions, useVerifyMfaMutation } from "@/hooks/useMfa"
import { handleError } from "@/utils"
import { setTrustedDeviceToken } from "@/utils/trustedDevice"
import {
  isWebAuthnSupported,
  isWebAuthnUserCancellation,
  startAuthentication,
} from "@/utils/webauthn"

type Method = "passkey" | "totp" | "recovery"

type RememberDays = 1 | 7 | 30 | null

// Maps the radix Select string values (it only stores strings) to the
// typed `remember_device_days` the API expects.
const REMEMBER_OFF = "off"
const rememberValueToDays = (value: string): RememberDays => {
  if (value === "1") return 1
  if (value === "7") return 7
  if (value === "30") return 30
  return null
}

interface VerifyParams {
  method: Method
  payload: { [key: string]: unknown }
}

type TotpErrorKind = "invalid_code" | "attempt_limit_exceeded"

interface TotpInlineError {
  kind: TotpErrorKind
  message: string
}

/**
 * Login-time second-factor UI shown on `/login/mfa`. Shows whichever
 * primary methods are allowed (passkey button + TOTP form) together,
 * with recovery codes accessible via a footer link. On success it
 * stashes the access token and walks the normal post-login redirect.
 */
export function TwoFactorChallenge() {
  const navigate = useNavigate()
  const { challenge, redirectTo, clearChallenge } = useMfaChallenge()
  const { showErrorToast, showSuccessToast } = useCustomToast()
  const verifyMutation = useVerifyMfaMutation()
  const [passkeyLoading, setPasskeyLoading] = useState(false)
  const [totpError, setTotpError] = useState<TotpInlineError | null>(null)
  const [recoveryMode, setRecoveryMode] = useState(false)
  // "Do not ask on this device" duration — rides along with whichever
  // factor the user completes.
  const [rememberDays, setRememberDays] = useState<RememberDays>(null)

  const allowedMethods = challenge?.allowed_methods ?? []
  const passkeyAllowed =
    allowedMethods.includes("passkey") && isWebAuthnSupported()
  const totpAllowed = allowedMethods.includes("totp")
  const recoveryAllowed = allowedMethods.includes("recovery")

  const subtitle = recoveryMode
    ? "Enter one of your saved recovery codes to finish signing in."
    : passkeyAllowed && totpAllowed
      ? "Confirm it's you with your passkey, or enter the 6-digit code from your authenticator app."
      : passkeyAllowed
        ? "Confirm it's you with your passkey to finish signing in."
        : "Enter the 6-digit code from your authenticator app to finish signing in."

  // No active challenge in memory (likely a refresh) — punt back to /login.
  // Done in an effect so we don't trigger a navigation during render.
  useEffect(() => {
    if (!challenge) {
      void navigate({ to: "/login" })
    }
  }, [challenge, navigate])

  if (!challenge) return null

  const verify = ({ method, payload }: VerifyParams) => {
    const body: MfaVerifyRequest = {
      challenge_token: challenge.challenge_token,
      method,
      payload,
      remember_device_days: rememberDays,
    }
    verifyMutation.mutate(body, {
      onSuccess: (data) => {
        localStorage.setItem("access_token", data.access_token)
        // Persist the trusted-device token (when the user opted in) BEFORE
        // the hard reload below so the next login can skip the challenge.
        // Must stay here — see the clearChallenge() note below.
        if (data.trusted_device_token) {
          setTrustedDeviceToken(data.trusted_device_token)
        }
        showSuccessToast("Signed in")
        const target = redirectTo && redirectTo !== "/" ? redirectTo : "/"
        // Hard assign honors arbitrary same-origin redirect targets and
        // reboots the app cleanly. We intentionally do NOT clearChallenge()
        // here: nulling the in-memory challenge while still mounted on
        // /login/mfa would fire the "no challenge → /login" effect below,
        // starting a client-side route transition that races this reload.
        // That doomed transition's aborted testToken() throws and briefly
        // flashes the root error boundary. The reload discards challenge
        // state anyway, so there is nothing to clear.
        window.location.assign(target)
      },
      onError: (err) => {
        const detail =
          err instanceof ApiError
            ? (err.body as { detail?: { code?: string } } | null)?.detail
            : undefined
        // Invalid TOTP code: surface inline on the form instead of a
        // toast so the user can correct in place.
        if (
          err instanceof ApiError &&
          err.status === 400 &&
          detail?.code === "invalid_code" &&
          method === "totp"
        ) {
          setTotpError({
            kind: "invalid_code",
            message: "That code didn't match. Try again.",
          })
          return
        }
        // 429 with body.detail.code === "attempt_limit_exceeded" — the
        // challenge is dead. Surface inline on the TOTP form (when that
        // was the failing path) so the user sees what happened next to
        // the code they typed; they can hit Cancel to restart sign-in.
        if (
          err instanceof ApiError &&
          err.status === 429 &&
          detail?.code === "attempt_limit_exceeded" &&
          method === "totp"
        ) {
          setTotpError({
            kind: "attempt_limit_exceeded",
            message:
              "Too many incorrect attempts. Cancel and sign in again to retry.",
          })
          return
        }
        // 429 with body.detail.code === "rate_limited" — show a dedicated
        // message and leave the challenge intact so the user can retry.
        if (
          err instanceof ApiError &&
          err.status === 429 &&
          detail?.code === "rate_limited"
        ) {
          showErrorToast(
            "Too many attempts — wait a few minutes and try again.",
          )
          return
        }
        handleError.call(showErrorToast, err as never)
      },
    })
  }

  const handlePasskey = async () => {
    setPasskeyLoading(true)
    try {
      const options = await fetchLoginPasskeyOptions(challenge.challenge_token)
      const assertion = await startAuthentication(options)
      verify({
        method: "passkey",
        payload: assertion as unknown as { [key: string]: unknown },
      })
    } catch (err) {
      if (isWebAuthnUserCancellation(err)) {
        showErrorToast("Cancelled — no changes made")
      } else {
        showErrorToast(
          err instanceof Error ? err.message : "Passkey verification failed",
        )
      }
    } finally {
      setPasskeyLoading(false)
    }
  }

  const handleTotp = ({ code }: TotpFormData) => {
    verify({ method: "totp", payload: { code } })
  }

  const handleRecovery = ({ code }: RecoveryCodeFormData) => {
    verify({ method: "recovery", payload: { code } })
  }

  // Memoised so TotpForm's wave-trigger effect doesn't re-fire each
  // render — the cleanup sequence holds a timeout ref that would
  // otherwise get stomped if `advanceCleanup`'s closure churned.
  const handleTotpCodeChange = useCallback(() => {
    setTotpError((prev) => (prev ? null : prev))
  }, [])

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2 text-center">
        <h1 className="text-2xl font-bold">Two-factor authentication</h1>
        <p className="text-sm text-muted-foreground">{subtitle}</p>
      </div>

      {recoveryMode ? (
        <RecoveryCodeForm
          onSubmit={handleRecovery}
          loading={verifyMutation.isPending}
        />
      ) : (
        <div className="flex flex-col gap-4">
          {passkeyAllowed && (
            <PasskeyButton onClick={handlePasskey} loading={passkeyLoading} />
          )}
          {passkeyAllowed && totpAllowed && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <div className="flex-1 border-t" />
              <span>or</span>
              <div className="flex-1 border-t" />
            </div>
          )}
          {totpAllowed && (
            <TotpForm
              onSubmit={handleTotp}
              loading={verifyMutation.isPending}
              buttonLabel="Verify code"
              label={null}
              invalid={totpError !== null}
              errorMessage={totpError?.message}
              autoClearOnInvalid={totpError?.kind === "invalid_code"}
              onCodeChange={handleTotpCodeChange}
            />
          )}
        </div>
      )}

      {/* "Do not ask on this device" — applies to whichever method the
          user completes (passkey / TOTP / recovery), so it lives outside
          the method blocks and shows in both recovery and primary modes. */}
      <Select
        value={rememberDays === null ? REMEMBER_OFF : String(rememberDays)}
        onValueChange={(value) => setRememberDays(rememberValueToDays(value))}
      >
        <SelectTrigger className="w-full" aria-label="Remember this device">
          <SelectValue placeholder="Ask every time" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={REMEMBER_OFF}>Ask every time</SelectItem>
          <SelectItem value="1">
            Don't ask on this device for 1 day
          </SelectItem>
          <SelectItem value="7">
            Don't ask on this device for 7 days
          </SelectItem>
          <SelectItem value="30">
            Don't ask on this device for 30 days
          </SelectItem>
        </SelectContent>
      </Select>

      <div className="flex items-center justify-center gap-6 text-sm">
        <button
          type="button"
          onClick={() => {
            clearChallenge()
            void navigate({ to: "/login" })
          }}
          className="underline underline-offset-4 text-muted-foreground hover:text-foreground"
        >
          Cancel
        </button>
        {recoveryAllowed && !recoveryMode && (
          <button
            type="button"
            onClick={() => setRecoveryMode(true)}
            className="underline underline-offset-4 text-muted-foreground hover:text-foreground"
          >
            Enter recovery code
          </button>
        )}
        {recoveryAllowed && recoveryMode && (passkeyAllowed || totpAllowed) && (
          <button
            type="button"
            onClick={() => setRecoveryMode(false)}
            className="underline underline-offset-4 text-muted-foreground hover:text-foreground"
          >
            Back
          </button>
        )}
      </div>
    </div>
  )
}
