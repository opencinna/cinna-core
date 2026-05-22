/**
 * Thin wrapper around `@simplewebauthn/browser` so the rest of the app
 * only depends on a handful of helpers and we have a single place to
 * future-proof browser glue (conditional UI, error normalization, etc.).
 */
import {
  browserSupportsWebAuthn,
  startAuthentication as simplewebauthnStartAuthentication,
  startRegistration as simplewebauthnStartRegistration,
} from "@simplewebauthn/browser"
import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from "@simplewebauthn/types"

/**
 * Whether the current browser exposes the WebAuthn API. We use this to
 * grey out passkey buttons (with a tooltip) on unsupported browsers.
 */
export const isWebAuthnSupported = (): boolean => {
  try {
    return browserSupportsWebAuthn()
  } catch {
    return false
  }
}

/**
 * Run a WebAuthn registration ceremony from server-issued options.
 *
 * @throws DOMException with name `"NotAllowedError"` when the user cancels.
 */
export const startRegistration = (
  options: PublicKeyCredentialCreationOptionsJSON,
): Promise<RegistrationResponseJSON> => {
  return simplewebauthnStartRegistration({ optionsJSON: options })
}

/**
 * Run a WebAuthn authentication ceremony from server-issued options.
 *
 * @throws DOMException with name `"NotAllowedError"` when the user cancels.
 */
export const startAuthentication = (
  options: PublicKeyCredentialRequestOptionsJSON,
): Promise<AuthenticationResponseJSON> => {
  return simplewebauthnStartAuthentication({ optionsJSON: options })
}

/**
 * Best-effort check whether a thrown WebAuthn error is the user cancelling
 * the native authenticator dialog (NOT an actual failure). Callers use this
 * to show a non-destructive toast instead of an error.
 */
export const isWebAuthnUserCancellation = (error: unknown): boolean => {
  if (error instanceof DOMException) {
    return error.name === "NotAllowedError" || error.name === "AbortError"
  }
  if (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    typeof (error as { name?: unknown }).name === "string"
  ) {
    const name = (error as { name: string }).name
    return name === "NotAllowedError" || name === "AbortError"
  }
  return false
}

export type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
}
