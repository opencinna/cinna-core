import { AxiosError } from "axios"
import type { ApiError } from "./client"

export const APP_NAME = import.meta.env.VITE_APP_NAME || "Cinna"

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
