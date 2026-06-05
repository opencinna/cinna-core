/**
 * Trusted-device ("Do not ask on this device") token storage.
 *
 * The plaintext token is minted once by `POST /login/mfa/verify` when the
 * user opts to skip the 2FA challenge on this device for a bounded window.
 * It is persisted in localStorage (consistent with `access_token`) and sent
 * on every future login so the backend can skip the challenge until it
 * expires. Storage access is centralized here so callers never touch the
 * raw key.
 */
export const TRUSTED_DEVICE_KEY = "mfa.trusted_device_token"

export function getTrustedDeviceToken(): string | null {
  try {
    return localStorage.getItem(TRUSTED_DEVICE_KEY)
  } catch {
    return null
  }
}

export function setTrustedDeviceToken(token: string): void {
  try {
    localStorage.setItem(TRUSTED_DEVICE_KEY, token)
  } catch {
    // Storage unavailable (private mode / quota) — non-fatal: the user
    // simply re-completes the challenge next login.
  }
}

export function clearTrustedDeviceToken(): void {
  try {
    localStorage.removeItem(TRUSTED_DEVICE_KEY)
  } catch {
    // Non-fatal.
  }
}
