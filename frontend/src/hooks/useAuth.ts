import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { redirect, useNavigate } from "@tanstack/react-router"

import {
  type Body_login_login_access_token as AccessToken,
  LoginService,
  type LoginToken,
  type MfaChallenge,
  type UserPublic,
  type UserRegister,
  UsersService,
} from "@/client"
import { useMfaChallenge } from "@/components/Auth/MfaChallengeContext"
import { handleError, safeRedirectPath } from "@/utils"
import useCustomToast from "./useCustomToast"

/**
 * Discriminated-union helper — guards the `kind` literal on the
 * `LoginResponse` (Token | MfaChallenge) returned by `loginAccessToken`
 * and the Google OAuth callback.
 */
const isMfaChallengeResponse = (
  response: LoginToken | MfaChallenge,
): response is MfaChallenge => {
  return (response as MfaChallenge).kind === "mfa_challenge"
}

/**
 * Read a same-origin `?redirect=` target from the current URL, if any.
 * Returns null when absent or unsafe — callers should fall back to "/".
 */
const readRedirectFromUrl = (): string | null => {
  try {
    const params = new URLSearchParams(window.location.search)
    const raw = params.get("redirect")
    if (!raw) return null
    const safe = safeRedirectPath(raw)
    return safe === "/" ? null : safe
  } catch {
    return null
  }
}

/**
 * Navigate to a post-auth target. Uses a full page assign for arbitrary
 * paths (with query strings) so it works regardless of TanStack Router's
 * typed route registry; falls back to "/" for unsafe or missing values.
 */
const navigateToPostAuthTarget = (target: string | null) => {
  if (target && target !== "/") {
    window.location.assign(target)
    return
  }
  window.location.assign("/")
}

const isLoggedIn = () => {
  return localStorage.getItem("access_token") !== null
}

/**
 * Validate the local JWT is still accepted by the backend. Use this in a
 * route's `beforeLoad` for public consent/authorize pages that need an
 * authenticated user but live outside the `_layout` guard. On 401/403/404
 * the local token is cleared and a redirect to `/login` is thrown with
 * `?redirect=<returnTo>` so the user lands back on the consent page after
 * re-authenticating instead of being dropped on the dashboard or stuck on
 * a "Could not validate credentials" error.
 */
const ensureSessionValid = async (returnTo: string): Promise<void> => {
  const loginRedirect = redirect({
    to: "/login",
    search: { redirect: returnTo },
  })
  if (!isLoggedIn()) {
    throw loginRedirect
  }
  try {
    await LoginService.testToken()
  } catch (error: any) {
    if (
      error?.status === 401 ||
      error?.status === 403 ||
      error?.status === 404
    ) {
      localStorage.removeItem("access_token")
      throw loginRedirect
    }
    throw error
  }
}

/**
 * Clear the local token and send the user to `/login`, preserving the
 * current URL as `?redirect=` so they return here after signing in. Use
 * from page-level error handlers when the token expires mid-session
 * (e.g. while sitting on a consent screen).
 */
const redirectToLoginPreservingTarget = () => {
  localStorage.removeItem("access_token")
  const here =
    window.location.pathname + window.location.search + window.location.hash
  const safe = safeRedirectPath(here)
  if (safe !== "/") {
    window.location.href = `/login?redirect=${encodeURIComponent(safe)}`
    return
  }
  window.location.href = "/login"
}

const useAuth = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { setChallenge } = useMfaChallenge()

  const { data: user } = useQuery<UserPublic | null, Error>({
    queryKey: ["currentUser"],
    queryFn: async () => {
      try {
        const userData = await UsersService.readUserMe()
        // If user is inactive, clear token and redirect to login
        if (userData && !userData.is_active) {
          localStorage.removeItem("access_token")
          navigate({ to: "/login" })
          return null
        }
        return userData
      } catch (error: any) {
        // If user not found (404) or unauthorized (401), clear token and redirect to login
        if (error?.status === 404 || error?.status === 401) {
          localStorage.removeItem("access_token")
          navigate({ to: "/login" })
        }
        throw error
      }
    },
    enabled: isLoggedIn(),
    retry: (failureCount, error: any) => {
      // Don't retry on 404 or 401 errors
      if (error?.status === 404 || error?.status === 401) {
        return false
      }
      return failureCount < 3
    },
  })

  const signUpMutation = useMutation({
    mutationFn: (data: UserRegister) =>
      UsersService.registerUser({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast(
        "Your account has been created. You can now log in.",
      )
      const target = readRedirectFromUrl()
      navigate({
        to: "/login",
        search: target ? { redirect: target } : undefined,
      })
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] })
    },
  })

  /**
   * Outcome of the first-factor login step. When `kind === "token"` the
   * caller stores the access token and walks the post-auth redirect
   * chain; when `kind === "mfa_challenge"` the caller stashes the
   * challenge in `MfaChallengeContext` and navigates to `/login/mfa`.
   */
  type LoginOutcome =
    | { kind: "token"; token: LoginToken }
    | { kind: "mfa_challenge"; challenge: MfaChallenge }

  const login = async (data: AccessToken): Promise<LoginOutcome> => {
    const response = await LoginService.loginAccessToken({
      formData: data,
    })
    if (isMfaChallengeResponse(response)) {
      return { kind: "mfa_challenge", challenge: response }
    }
    return { kind: "token", token: response }
  }

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: (outcome) => {
      const target = readRedirectFromUrl()
      if (outcome.kind === "mfa_challenge") {
        setChallenge(outcome.challenge, target)
        void navigate({ to: "/login/mfa" })
        return
      }
      localStorage.setItem("access_token", outcome.token.access_token)
      navigateToPostAuthTarget(target)
    },
    onError: handleError.bind(showErrorToast),
  })

  const logout = () => {
    localStorage.removeItem("access_token")
    navigate({ to: "/login" })
  }

  return {
    signUpMutation,
    loginMutation,
    logout,
    user,
  }
}

export {
  isLoggedIn,
  ensureSessionValid,
  redirectToLoginPreservingTarget,
  isMfaChallengeResponse,
  navigateToPostAuthTarget,
}
export default useAuth
