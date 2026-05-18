import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import {
  type Body_login_login_access_token as AccessToken,
  LoginService,
  type UserPublic,
  type UserRegister,
  UsersService,
} from "@/client"
import { handleError, safeRedirectPath } from "@/utils"
import useCustomToast from "./useCustomToast"

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

const useAuth = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

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

  const login = async (data: AccessToken) => {
    const response = await LoginService.loginAccessToken({
      formData: data,
    })
    localStorage.setItem("access_token", response.access_token)
  }

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: () => {
      navigateToPostAuthTarget(readRedirectFromUrl())
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

export { isLoggedIn }
export default useAuth
