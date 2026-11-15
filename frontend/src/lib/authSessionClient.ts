/**
 * Wires the session store (`authSession.ts`) into the generated client:
 * cookies on every request, the bearer resolver, the refresh/logout transport,
 * and the response interceptor that refreshes once on a 401 and retries.
 */
import axios, { AxiosHeaders, type AxiosResponse } from "axios"

import { LoginService, OpenAPI } from "@/client"
import {
  configureAuthSession,
  getGuestToken,
  refreshAfterRejection,
  resolveBearer,
} from "@/lib/authSession"
import { safeRedirectPath } from "@/utils"
import { recordRetryAfter } from "@/utils/loginThrottle"

const REFRESH_PATH = "/api/v1/login/session/refresh"
const LOGOUT_PATH = "/api/v1/login/session/logout"

/** Cookie-authenticated endpoints: a bearer there would be noise. */
const COOKIE_ONLY_PATHS = new Set([REFRESH_PATH, LOGOUT_PATH])

/**
 * 401s here are answers about the session itself, never "refresh and retry".
 * `revoke-all` is not one of them: it is a bearer call like any other.
 */
function isSessionEndpoint(url: string | undefined): boolean {
  if (!url) return false
  return (
    url.includes(REFRESH_PATH) ||
    url.includes(LOGOUT_PATH) ||
    url.includes("/api/v1/login/access-token")
  )
}

/**
 * Public pages that render a dead token as signed out instead of sending the
 * visitor to /login: guest shares, public artifact links, public skill links.
 */
export function isPublicPath(pathname: string = window.location.pathname) {
  return (
    pathname.startsWith("/guest/") ||
    pathname.startsWith("/s/") ||
    pathname.startsWith("/get/")
  )
}

let redirectingToLogin = false

/** One redirect per page, back to where the person was. */
export function redirectToLogin(): void {
  if (redirectingToLogin) return
  redirectingToLogin = true
  const here =
    window.location.pathname + window.location.search + window.location.hash
  const safe = safeRedirectPath(here)
  if (safe !== "/" && safe !== "/login") {
    window.location.href = `/login?redirect=${encodeURIComponent(safe)}`
  } else {
    window.location.href = "/login"
  }
}

type RetryableConfig = AxiosResponse["config"] & { cinnaAuthRetried?: boolean }

async function onResponse(response: AxiosResponse): Promise<AxiosResponse> {
  if (response.status === 429) {
    recordRetryAfter(response.data, response.headers?.["retry-after"])
    return response
  }
  if (response.status !== 401) return response

  const config = response.config as RetryableConfig
  if (config.cinnaAuthRetried || isSessionEndpoint(config.url)) return response
  const authorization = AxiosHeaders.from(config.headers).get("Authorization")
  if (
    typeof authorization !== "string" ||
    !authorization.startsWith("Bearer ")
  ) {
    return response
  }
  const sentToken = authorization.slice("Bearer ".length)
  // A guest-share token has no refresh session behind it.
  if (sentToken === getGuestToken()) return response

  const outcome = await refreshAfterRejection(sentToken)
  if (outcome.kind === "expired") {
    if (!isPublicPath()) redirectToLogin()
    return response
  }
  if (outcome.kind === "failed") return response

  const headers = AxiosHeaders.from(config.headers)
  headers.set("Authorization", `Bearer ${outcome.token}`)
  const retryConfig: RetryableConfig = {
    ...config,
    headers,
    cinnaAuthRetried: true,
  }
  try {
    return await axios.request(retryConfig)
  } catch (error) {
    if (axios.isAxiosError(error) && error.response) return error.response
    throw error
  }
}

/** Call once, before the first request (main.tsx). */
export function installAuthSession(): void {
  OpenAPI.WITH_CREDENTIALS = true
  OpenAPI.TOKEN = async (options) =>
    COOKIE_ONLY_PATHS.has(options.url) ? "" : resolveBearer()
  configureAuthSession({
    transport: {
      refresh: async (csrfToken) =>
        (await LoginService.sessionRefresh({ xCsrfToken: csrfToken }))
          .access_token,
      logout: async (csrfToken) => {
        await LoginService.sessionLogout({ xCsrfToken: csrfToken })
      },
    },
    // S7: another tab logged out. Go to /login, coming back here afterwards.
    onRemoteLogout: () => {
      if (!isPublicPath()) redirectToLogin()
    },
  })
  OpenAPI.interceptors.response.use(onResponse)
}
