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
