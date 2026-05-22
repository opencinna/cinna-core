import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  LoginService,
  type LoginToken,
  MfaService,
  type MfaStatus,
  type MfaVerifyRequest,
  type PasskeyFinishRequest,
  type RecoveryCodeStatus,
  type RecoveryCodesPlaintext,
  type StepUpProof,
  type TotpEnrollResponse,
  type TotpFinishRequest,
  type UserPasskeyPublic,
  type UserPasskeysPublic,
  type UserPasskeyUpdate,
} from "@/client"
import type {
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
} from "@/utils/webauthn"
import { startAuthentication, startRegistration } from "@/utils/webauthn"

/** Stable query keys for the MFA surface. */
export const MFA_QUERY_KEYS = {
  status: ["mfa", "status"] as const,
  passkeys: ["mfa", "passkeys"] as const,
  recovery: ["mfa", "recovery"] as const,
}

/** Read the current user's MFA state for the Security tab header. */
export function useMfaStatus() {
  return useQuery<MfaStatus>({
    queryKey: MFA_QUERY_KEYS.status,
    queryFn: () => MfaService.mfaStatus(),
  })
}

/** List of registered passkeys for the Security tab. */
export function useMfaPasskeys() {
  return useQuery<UserPasskeysPublic>({
    queryKey: MFA_QUERY_KEYS.passkeys,
    queryFn: () => MfaService.listPasskeys(),
  })
}

/** Remaining-count for the recovery-codes card. */
export function useRecoveryCodesStatus() {
  return useQuery<RecoveryCodeStatus>({
    queryKey: MFA_QUERY_KEYS.recovery,
    queryFn: () => MfaService.recoveryCodesStatus(),
  })
}

interface EnrollPasskeyResult {
  passkey: UserPasskeyPublic
  recovery_codes: RecoveryCodesPlaintext | null
}

/**
 * Run the full passkey-enrollment ceremony:
 *   1. POST /mfa/passkeys/begin → server-issued WebAuthn creation options
 *      (nested as ``{ challenge_token, options }`` so the spec-defined
 *      options object stays clean for ``@simplewebauthn/browser``).
 *   2. Browser navigator.credentials.create() prompt
 *   3. POST /mfa/passkeys/finish → persisted passkey + (maybe) one-shot
 *      recovery codes when this enrollment flips 2FA on for the first time.
 */
export function useEnrollPasskeyMutation() {
  const queryClient = useQueryClient()
  return useMutation<EnrollPasskeyResult, Error, { nickname: string }>({
    mutationFn: async ({ nickname }) => {
      // The /begin call no longer takes a nickname; the server only needs
      // it on /finish to label the persisted credential.
      const begin = await MfaService.beginPasskeyRegistration()
      // ``options`` is typed as ``{ [key: string]: unknown }`` because
      // the OpenAPI generator can't statically know it's a
      // ``PublicKeyCredentialCreationOptionsJSON`` — narrow it here so
      // the WebAuthn call site stays strongly typed.
      const options =
        begin.options as unknown as PublicKeyCredentialCreationOptionsJSON
      const credential = await startRegistration(options)
      const finishPayload: PasskeyFinishRequest = {
        challenge_token: begin.challenge_token,
        credential: credential as unknown as { [key: string]: unknown },
        nickname,
      }
      const finishRaw = await MfaService.finishPasskeyRegistration({
        requestBody: finishPayload,
      })
      return finishRaw as unknown as EnrollPasskeyResult
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.status })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.passkeys })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.recovery })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })
}

interface BeginTotpEnrollResult extends TotpEnrollResponse {}

/** Start TOTP enrollment — server returns the QR + signed handle. */
export function useBeginTotpEnrollmentMutation() {
  return useMutation<BeginTotpEnrollResult, Error, void>({
    mutationFn: () => MfaService.beginTotpEnrollment(),
  })
}

interface FinishTotpEnrollResult {
  message: string
  recovery_codes: RecoveryCodesPlaintext | null
}

/** Confirm TOTP enrollment — persists the secret if the code verifies. */
export function useFinishTotpEnrollmentMutation() {
  const queryClient = useQueryClient()
  return useMutation<FinishTotpEnrollResult, Error, TotpFinishRequest>({
    mutationFn: async (payload) => {
      const result = await MfaService.finishTotpEnrollment({
        requestBody: payload,
      })
      return result as unknown as FinishTotpEnrollResult
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.status })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.recovery })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })
}

/** TOTP enroll wrapper that bundles begin/finish for caller convenience. */
export function useEnrollTotpMutation() {
  // Kept for API parity with the plan; consumers normally use the two
  // mutations above directly because the flow needs the user to type a
  // code between begin and finish.
  return {
    begin: useBeginTotpEnrollmentMutation(),
    finish: useFinishTotpEnrollmentMutation(),
  }
}

/** Rename a passkey. */
export function useRenamePasskeyMutation() {
  const queryClient = useQueryClient()
  return useMutation<
    UserPasskeyPublic,
    Error,
    { passkeyId: string; nickname: string }
  >({
    mutationFn: ({ passkeyId, nickname }) => {
      const body: UserPasskeyUpdate = { nickname }
      return MfaService.renamePasskey({
        passkeyId,
        requestBody: body,
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.passkeys })
    },
  })
}

/** Delete a passkey (refused server-side when it's the last factor). */
export function useDeletePasskeyMutation() {
  const queryClient = useQueryClient()
  return useMutation<unknown, Error, string>({
    mutationFn: (passkeyId) => MfaService.deletePasskey({ passkeyId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.status })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.passkeys })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })
}

/** Remove TOTP (requires fresh-factor proof). */
export function useDisableTotpMutation() {
  const queryClient = useQueryClient()
  return useMutation<unknown, Error, StepUpProof>({
    mutationFn: (proof) => MfaService.disableTotp({ requestBody: proof }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.status })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })
}

/** Regenerate the recovery-code batch (one-shot plaintext response). */
export function useRegenerateRecoveryCodesMutation() {
  const queryClient = useQueryClient()
  return useMutation<RecoveryCodesPlaintext, Error, StepUpProof>({
    mutationFn: (proof) =>
      MfaService.regenerateRecoveryCodes({ requestBody: proof }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.recovery })
    },
  })
}

/** Disable 2FA entirely (wipes all factors, requires fresh-factor proof). */
export function useDisableTwoFactorMutation() {
  const queryClient = useQueryClient()
  return useMutation<unknown, Error, StepUpProof>({
    mutationFn: (proof) => MfaService.disableTwoFactor({ requestBody: proof }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.status })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.passkeys })
      queryClient.invalidateQueries({ queryKey: MFA_QUERY_KEYS.recovery })
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
  })
}

interface StepUpPasskeyAssertion {
  passkey_challenge_token: string
  passkey_assertion: { [key: string]: unknown }
}

/**
 * Helper that runs a step-up passkey ceremony and returns the proof the
 * server expects in the next destructive request's body.
 */
export async function runStepUpPasskey(): Promise<StepUpPasskeyAssertion> {
  const begin = await MfaService.beginStepUpPasskey()
  const options =
    begin.options as unknown as PublicKeyCredentialRequestOptionsJSON
  const assertion = await startAuthentication(options)
  return {
    passkey_challenge_token: begin.challenge_token,
    passkey_assertion: assertion as unknown as { [key: string]: unknown },
  }
}

/**
 * Login-time MFA verification. Sends one of:
 *   - method=passkey, payload=AuthenticationResponseJSON
 *   - method=totp,    payload={ code }
 *   - method=recovery, payload={ code }
 * On success, returns the final access token.
 */
export function useVerifyMfaMutation() {
  return useMutation<LoginToken, Error, MfaVerifyRequest>({
    mutationFn: (payload) =>
      LoginService.loginMfaVerify({ requestBody: payload }),
  })
}

/**
 * Helper: ask the backend for WebAuthn assertion options for a pending
 * login challenge. Used by `PasskeyButton` on the /login/mfa page.
 *
 * The backend nests the WebAuthn options under ``options`` so we can
 * pass them straight to ``@simplewebauthn/browser``.
 */
export async function fetchLoginPasskeyOptions(
  challengeToken: string,
): Promise<PublicKeyCredentialRequestOptionsJSON> {
  const result = await LoginService.loginMfaPasskeyOptions({
    requestBody: { challenge_token: challengeToken },
  })
  return result.options as unknown as PublicKeyCredentialRequestOptionsJSON
}
