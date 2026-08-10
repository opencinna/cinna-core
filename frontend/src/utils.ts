import { AxiosError } from "axios"
import type { ApiError } from "./client"
import { UsersService } from "./client"

export const APP_NAME = import.meta.env.VITE_APP_NAME || "Cinna"

/**
 * Fire-and-forget: persist the browser-detected timezone/language/locale to the
 * current user, filling only fields that are still NULL server-side. Called
 * right after a token is stored on browser login (password + Google OAuth).
 *
 * The server enforces the NULL-only guarantee, so it is safe to send detected
 * values on every login — an explicit choice the user made in Settings is never
 * overwritten. Best-effort: all errors are swallowed and it never blocks
 * navigation. Detection is guarded so unsupported environments simply send less.
 */
export const persistDetectedLocaleDefaults = (): void => {
  try {
    const timezone =
      typeof Intl !== "undefined" &&
      typeof Intl.DateTimeFormat === "function"
        ? Intl.DateTimeFormat().resolvedOptions().timeZone
        : undefined
    const fullLocale =
      typeof navigator !== "undefined" ? navigator.language : undefined
    const language = fullLocale ? fullLocale.split("-")[0] : undefined

    const requestBody: {
      timezone?: string | null
      language?: string | null
      locale?: string | null
    } = {}
    if (timezone) requestBody.timezone = timezone
    if (language) requestBody.language = language
    if (fullLocale) requestBody.locale = fullLocale

    if (Object.keys(requestBody).length === 0) return

    void UsersService.updateUserLocaleDefaults({ requestBody }).catch(() => {
      // Best-effort: detection/persistence failures must not affect login.
    })
  } catch {
    // Detection itself failed (very old browser) — ignore.
  }
}

function extractErrorMessage(err: ApiError): string {
  if (err instanceof AxiosError) {
    return err.message
  }

  const errDetail = (err.body as any)?.detail
  if (Array.isArray(errDetail) && errDetail.length > 0) {
    return errDetail[0].msg
  }
  if (typeof errDetail === "string") {
    return errDetail
  }
  if (errDetail && typeof errDetail === "object") {
    return errDetail.message || errDetail.msg || errDetail.code || "Something went wrong."
  }
  return "Something went wrong."
}

/**
 * Clear the per-login disclaimer acknowledgment(s).
 *
 * The "Every Login" disclaimer mode stores its acknowledgment in
 * `sessionStorage` under `disclaimer_session_v<version>`. sessionStorage
 * survives a logout→login cycle within the same browser tab, which would
 * suppress the disclaimer on the next login. Call this on every successful
 * login so "Every Login" truly re-shows after each sign-in. (The "New User
 * Only" mode lives in localStorage and is intentionally left untouched.)
 */
export const clearLoginScopedDisclaimerAck = () => {
  try {
    const store = window.sessionStorage
    const keys: string[] = []
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i)
      if (key && key.startsWith("disclaimer_session_")) {
        keys.push(key)
      }
    }
    keys.forEach((key) => store.removeItem(key))
  } catch {
    // sessionStorage may be unavailable (e.g. privacy mode) — ignore.
  }
}

export const handleError = function (
  this: (msg: string) => void,
  err: ApiError,
) {
  const errorMessage = extractErrorMessage(err)
  this(errorMessage)
}

/**
 * Best-effort error-message extraction for `useMutation` `onError` handlers that
 * want a custom fallback. Accepts the loosely-typed React Query error (defaults
 * to `Error`) and prefers a FastAPI `body.detail` string, then `message`.
 *
 * `detail` is NOT always a string: the recoverable-409 endpoints (git connect's
 * `existing_agent_folder`, git pull's `local_changes`) return a structured
 * object carrying `code` / `message`. Returning that object from a function
 * typed `string` renders as `[object Object]` in a toast, so an object detail
 * falls through to its own `message` — mirroring `extractErrorMessage` above.
 */
export const getErrorMessage = (error: unknown, fallback: string): string => {
  const e = error as { body?: { detail?: unknown }; message?: string }
  const detail = e?.body?.detail
  if (typeof detail === "string" && detail) return detail
  if (detail && typeof detail === "object") {
    const nested = (detail as { message?: unknown }).message
    if (typeof nested === "string" && nested) return nested
  }
  return e?.message || fallback
}

export const getInitials = (name: string): string => {
  return name
    .split(" ")
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase()
}

/**
 * Validate a post-auth `?redirect=` target. Only same-origin local paths
 * are allowed, to prevent open-redirect attacks. Returns "/" for anything
 * unsafe or empty.
 */
export const safeRedirectPath = (
  input: string | null | undefined,
): string => {
  if (!input || typeof input !== "string") return "/"
  if (!input.startsWith("/")) return "/"
  // Reject protocol-relative URLs and backslash tricks
  if (input.startsWith("//") || input.startsWith("/\\")) return "/"
  try {
    const url = new URL(input, window.location.origin)
    if (url.origin !== window.location.origin) return "/"
    return url.pathname + url.search + url.hash
  } catch {
    return "/"
  }
}
